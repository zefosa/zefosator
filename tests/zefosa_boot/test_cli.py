import hashlib
from collections.abc import Sequence
from pathlib import Path

import pytest

from zefosa_boot import cli
from zefosa_boot.system import Result

TEMPLATE = """timeout 5
menuentry "ZFSBootMenu" {
    loader  /EFI/zefosa/zfsbootmenu.efi
    options "zbm.prefer=@POOL@ zbm.timeout=5 ro quiet loglevel=0"
}
"""
EFIBOOTMGR = """BootCurrent: 0000
BootOrder: 0001,0000
Boot0000* rEFInd\tHD(1,GPT,5,0x800,0x200000)/\\EFI\\refind\\refind_x64.efi
Boot0001* Fedora\tHD(1,GPT,5,0x800,0x200000)/\\EFI\\fedora\\shimx64.efi
"""


ESP_MOUNT = ("findmnt", "-no", "TARGET,FSTYPE", "--target")


class FakeSystem:
    def __init__(self, responses: dict[tuple[str, ...], Result] | None = None) -> None:
        self.responses = responses or {}
        self.calls: list[list[str]] = []
        self.executed: list[list[str]] = []

    def run(self, argv: Sequence[str]) -> Result:
        self.calls.append(list(argv))
        for key, result in self.responses.items():
            if tuple(argv[: len(key)]) == key:
                return result
        return Result(0, "", "")

    def execute(self, argv: Sequence[str]) -> int:
        self.executed.append(list(argv))
        return 0


def ok(stdout: str) -> Result:
    return Result(0, stdout, "")


def esp_tree(tmp_path: Path) -> tuple[Path, Path, Path]:
    esp = tmp_path / "esp"
    zefosa = esp / "EFI" / "zefosa"
    zefosa.mkdir(parents=True)
    for name in ("shimx64.efi", "mmx64.efi", "grubx64.efi", "zfsbootmenu.efi"):
        (zefosa / name).write_bytes(b"x")
    template = tmp_path / "refind.conf.in"
    template.write_text(TEMPLATE)
    ca = tmp_path / "zefosa-ca.der"
    ca.write_bytes(b"0")
    part = tmp_path / "sys" / "nvme0n1p1"
    part.mkdir(parents=True)
    (part / "partition").write_text("1\n")
    return esp, template, ca


def setup_system(sb: str = "SecureBoot enabled", *, enrolled: bool = False) -> FakeSystem:
    test_key = "zefosa-ca.der is already enrolled" if enrolled else "zefosa-ca.der is not enrolled"
    return FakeSystem(
        {
            ("findmnt", "-no", "SOURCE,FSTYPE", "/"): ok("zroot/ROOT/fedora zfs\n"),
            ("findmnt", "-no", "SOURCE", "--target"): ok("/dev/nvme0n1p1\n"),
            ("lsblk", "-no", "PKNAME"): ok("nvme0n1\n"),
            ("efibootmgr", "-v"): ok(EFIBOOTMGR),
            ("mokutil", "--sb-state"): ok(f"{sb}\n"),
            ("mokutil", "--test-key"): ok(f"{test_key}\n"),
        }
    )


def run_setup(system: FakeSystem, esp: Path, template: Path, ca: Path, *extra: str) -> int:
    system.responses.setdefault(ESP_MOUNT, ok(f"{esp} vfat\n"))
    return cli.main(
        ["setup", "--esp", str(esp), *extra],
        system=system,
        paths=cli.Paths(template=template, ca=ca, efi_vars=esp, sys_block=esp.parent / "sys"),
    )


def test_setup_writes_refind_conf_with_detected_pool(tmp_path: Path) -> None:
    esp, template, ca = esp_tree(tmp_path)
    assert run_setup(setup_system(), esp, template, ca) == 0
    conf = (esp / "EFI" / "zefosa" / "refind.conf").read_text()
    assert "zbm.prefer=zroot " in conf


def test_setup_keeps_existing_refind_conf(tmp_path: Path) -> None:
    esp, template, ca = esp_tree(tmp_path)
    (esp / "EFI" / "zefosa" / "refind.conf").write_text("mine")
    assert run_setup(setup_system(), esp, template, ca, "--pool", "other") == 0
    assert (esp / "EFI" / "zefosa" / "refind.conf").read_text() == "mine"


def test_setup_creates_boot_entry_without_load_options(tmp_path: Path) -> None:
    esp, template, ca = esp_tree(tmp_path)
    system = setup_system()
    assert run_setup(system, esp, template, ca) == 0
    create = next(c for c in system.calls if c[:2] == ["efibootmgr", "--create"])
    assert create == [
        "efibootmgr", "--create", "--disk", "/dev/nvme0n1", "--part", "1",
        "--label", "zefosa", "--loader", "\\EFI\\zefosa\\shimx64.efi",
    ]  # fmt: skip
    assert not [c for c in system.calls if "--unicode" in c or "-u" in c]


def test_setup_moves_existing_entry_first(tmp_path: Path) -> None:
    esp, template, ca = esp_tree(tmp_path)
    system = setup_system()
    system.responses[("efibootmgr", "-v")] = ok(
        EFIBOOTMGR + "Boot0007* zefosa\tHD(1,GPT,5,0x800,0x200000)/\\EFI\\zefosa\\shimx64.efi\n"
    )
    assert run_setup(system, esp, template, ca) == 0
    assert ["efibootmgr", "--bootorder", "0007,0001,0000"] in system.calls
    assert not [c for c in system.calls if c[:2] == ["efibootmgr", "--create"]]


def test_setup_activates_inactive_existing_entry(tmp_path: Path) -> None:
    esp, template, ca = esp_tree(tmp_path)
    system = setup_system()
    system.responses[("efibootmgr", "-v")] = ok(
        EFIBOOTMGR + "Boot0007  zefosa\tHD(1,GPT,5,0x800,0x200000)/\\EFI\\zefosa\\shimx64.efi\n"
    )
    assert run_setup(system, esp, template, ca) == 0
    assert ["efibootmgr", "-b", "0007", "-a"] in system.calls
    assert ["efibootmgr", "--bootorder", "0007,0001,0000"] in system.calls


@pytest.mark.parametrize(
    ("mount", "why"),
    [("/ xfs\n", "/ (xfs)"), ("{esp} xfs\n", "is xfs, not vfat"), ("/boot vfat\n", "/boot (vfat)")],
)
def test_setup_refuses_esp_that_is_not_a_mounted_vfat_partition(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], mount: str, why: str
) -> None:
    esp, template, ca = esp_tree(tmp_path)
    system = setup_system()
    system.responses[ESP_MOUNT] = ok(mount.format(esp=esp))
    assert run_setup(system, esp, template, ca) == 1
    err = capsys.readouterr().err
    assert f"{esp} is not a mounted EFI System Partition" in err
    assert why in err
    assert not (esp / "EFI" / "zefosa" / "refind.conf").exists()
    assert not [c for c in system.calls if c[0] in {"efibootmgr", "mokutil"}]


def test_setup_does_not_reimport_pending_ca(tmp_path: Path) -> None:
    esp, template, ca = esp_tree(tmp_path)
    system = setup_system()
    fingerprint = ":".join(f"{b:02x}" for b in hashlib.sha1(ca.read_bytes()).digest())  # noqa: S324
    system.responses[("mokutil", "--list-new")] = ok(
        f"[key 1]\nSHA1 Fingerprint: {fingerprint}\nCertificate:\n"
    )
    assert run_setup(system, esp, template, ca) == 0
    assert not [c for c in system.calls if "--import" in c]


def test_setup_imports_ca_when_not_enrolled(tmp_path: Path) -> None:
    esp, template, ca = esp_tree(tmp_path)
    system = setup_system()
    assert run_setup(system, esp, template, ca) == 0
    assert ["mokutil", "--import", str(ca), "--root-pw"] in system.calls


def test_setup_skips_enrolled_ca_and_honours_flags(tmp_path: Path) -> None:
    esp, template, ca = esp_tree(tmp_path)
    system = setup_system(enrolled=True)
    assert run_setup(system, esp, template, ca) == 0
    assert not [c for c in system.calls if "--import" in c]
    system = setup_system()
    assert run_setup(system, esp, template, ca, "--no-firmware", "--no-mok", "--pool", "p") == 0
    assert not [c for c in system.calls if c[0] in {"efibootmgr", "mokutil"}]


def test_setup_requires_installed_files(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    esp, template, ca = esp_tree(tmp_path)
    (esp / "EFI" / "zefosa" / "grubx64.efi").unlink()
    assert run_setup(setup_system(), esp, template, ca) == 1
    assert "grubx64.efi" in capsys.readouterr().err


def test_setup_without_pool_on_non_zfs_root(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    esp, template, ca = esp_tree(tmp_path)
    system = setup_system()
    system.responses[("findmnt", "-no", "SOURCE,FSTYPE", "/")] = ok("/dev/sda2 xfs\n")
    assert run_setup(system, esp, template, ca, "--no-firmware", "--no-mok") == 1
    assert "--pool" in capsys.readouterr().err


def test_status_reports_everything(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    esp, template, ca = esp_tree(tmp_path)
    (esp / "EFI" / "zefosa" / "refind.conf").write_text(TEMPLATE.replace("@POOL@", "zroot"))
    system = setup_system(enrolled=True)
    system.responses[("kernel-install", "inspect")] = ok("     Layout: bls\n")
    system.responses[("rpm", "-q")] = ok("shim-x64-16.1-7.zefosa.x86_64\n")
    assert cli.main(["status", "--esp", str(esp)], system=system,
                    paths=cli.Paths(template=template, ca=ca, efi_vars=esp)) == 0  # fmt: skip
    out = capsys.readouterr().out
    assert "Secure Boot: enabled" in out
    assert "zefosa CA: enrolled" in out
    assert "Boot entry: missing" in out
    assert "refind.conf pool: zroot" in out
    assert "WARNING: kernel-install layout is 'bls'" in out
    assert "zfsbootmenu-recovery.efi: missing" in out


def test_status_reports_pending_ca_and_inactive_entry(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    esp, template, ca = esp_tree(tmp_path)
    system = setup_system()
    sha1 = hashlib.sha1(ca.read_bytes()).hexdigest()  # noqa: S324
    system.responses[("mokutil", "--list-new")] = ok(f"SHA1 Fingerprint: {sha1.upper()}\n")
    system.responses[("efibootmgr", "-v")] = ok(
        "BootOrder: 0007\nBoot0007  zefosa\tHD(1)/\\EFI\\zefosa\\shimx64.efi\n"
    )
    assert cli.main(["status", "--esp", str(esp)], system=system,
                    paths=cli.Paths(template=template, ca=ca, efi_vars=esp)) == 0  # fmt: skip
    out = capsys.readouterr().out
    assert "zefosa CA: pending" in out
    assert "Boot entry: Boot0007 (first) (inactive)" in out


def test_cmdline_delegates_to_zbm_kcl_for_root_filesystem() -> None:
    system = setup_system()
    system.responses[("zfs", "list")] = ok("zroot/ROOT/fedora\n")
    rc = cli.main(["cmdline", "-r", "loglevel", "-a", "loglevel=7", "-a", 'x="a b"'], system=system)
    assert rc == 0
    assert system.executed == [
        ["zbm-kcl", "-r", "loglevel", "-a", "loglevel=7", "-a", 'x="a b"', "zroot/ROOT/fedora"]
    ]


def test_cmdline_passes_edit_delete_and_output() -> None:
    system = setup_system()
    system.responses[("zfs", "list")] = ok("zroot/ROOT\n")
    assert cli.main(["cmdline", "-e", "-o", "zroot/ROOT/os", "zroot/ROOT"], system=system) == 0
    assert system.executed == [["zbm-kcl", "-e", "-o", "zroot/ROOT/os", "zroot/ROOT"]]
    system.executed.clear()
    assert cli.main(["cmdline", "-d", "zroot/ROOT"], system=system) == 0
    assert system.executed == [["zbm-kcl", "-d", "zroot/ROOT"]]


def test_cmdline_refuses_efi_images(capsys: pytest.CaptureFixture[str]) -> None:
    system = setup_system()
    system.responses[("zfs", "list")] = Result(1, "", "dataset does not exist")
    assert cli.main(["cmdline", "/boot/efi/EFI/zefosa/zfsbootmenu.efi"], system=system) == 1
    err = capsys.readouterr().err
    assert "refind.conf" in err
    assert system.executed == []


def test_cmdline_refuses_non_zfs_destination(capsys: pytest.CaptureFixture[str]) -> None:
    system = setup_system()
    system.responses[("zfs", "list", "-H", "-o", "name", "zroot/ROOT")] = ok("zroot/ROOT\n")
    system.responses[("zfs", "list", "-H", "-o", "name", "out.efi")] = Result(1, "", "no")
    assert cli.main(["cmdline", "-a", "x", "-o", "out.efi", "zroot/ROOT"], system=system) == 1
    assert "out.efi" in capsys.readouterr().err
