import hashlib
import json
import os
import subprocess
import tarfile
from collections.abc import Callable
from pathlib import Path

import pytest

from tests.rpmfiles import fake_rpm
from tests.test_config import MINI_TARGET, make_root
from zefosator.cli import main
from zefosator.config import load_plan
from zefosator.errors import BuildError, ConfigError
from zefosator.pack import pack

TID = "mini-9.8-x86_64"
ARCHIVES = """\
archives:
  mini-zfs:
    - "{{ target_id }}/zfs/zfs-{{ zfs.version }}.rpm"
    - "{{ target_id }}/kernel/kernel-{{ kernel_version }}.rpm"
    - "boot/zefosa-boot.rpm"
    - "{{ target_id }}/boot/shim.rpm"
  mini-kernel:
    - "{{ target_id }}/kernel/kernel-{{ kernel_version }}.rpm"
"""


BUILDTIMES = {b"zfs": 1787329187, b"kern": 1791056615, b"shim": 1780000000, b"boot": 1785000000}


def write_outputs(output: Path, manifest_dir: str, files: dict[str, bytes]) -> None:
    """Write RPMs under output/<manifest_dir>/ and record them in its manifest.json."""
    base = output / manifest_dir
    entries = []
    for rel, payload in files.items():
        (base / rel).parent.mkdir(parents=True, exist_ok=True)
        fake_rpm(base / rel, buildtime=BUILDTIMES[payload], payload=payload)
        data = (base / rel).read_bytes()
        entries.append({"path": rel, "nevra": rel, "sha256": hashlib.sha256(data).hexdigest()})
    (base / "manifest.json").write_text(json.dumps({"outputs": entries}))


@pytest.fixture
def target(tmp_path: Path) -> Path:
    path = make_root(
        tmp_path, target=MINI_TARGET.format(os_version="9.8", arch="x86_64") + ARCHIVES
    )
    output = tmp_path / "output"
    write_outputs(output, TID, {"zfs/zfs-2.3.9.rpm": b"zfs", "kernel/kernel-5.14.0.rpm": b"kern"})
    write_outputs(output, f"{TID}/boot", {"shim.rpm": b"shim"})
    write_outputs(output, "boot", {"zefosa-boot.rpm": b"boot"})
    return path


def archives_dir(tmp_path: Path) -> Path:
    return tmp_path / "output" / TID / "archives"


def test_pack_writes_a_flat_tar_and_its_checksum(tmp_path: Path, target: Path) -> None:
    (archive,) = pack(load_plan(target), tmp_path, ["mini-zfs"])
    assert archive.tar == archives_dir(tmp_path) / "mini-zfs.tar"
    with tarfile.open(archive.tar) as tar:
        members = tar.getmembers()
        assert [m.name for m in members] == [
            "zfs-2.3.9.rpm",
            "kernel-5.14.0.rpm",
            "zefosa-boot.rpm",
            "shim.rpm",
        ]
        assert [m.mtime for m in members] == [1787329187, 1791056615, 1785000000, 1780000000]
        for member in members:
            assert member.isreg()
            assert (member.uid, member.gid, member.uname, member.gname) == (0, 0, "root", "root")
            assert member.mode == 0o644
        extracted = tar.extractfile("shim.rpm")
        assert extracted is not None
        assert extracted.read().endswith(b"shim")
    digest = hashlib.sha256(archive.tar.read_bytes()).hexdigest()
    assert archive.sha256 == digest
    assert archive.size == archive.tar.stat().st_size
    assert archive.checksum.read_text() == f"{digest}  mini-zfs.tar\n"
    check = subprocess.run(
        ["sha256sum", "-c", "mini-zfs.sha256"],
        cwd=archives_dir(tmp_path),
        capture_output=True,
        text=True,
        check=False,
    )
    assert check.returncode == 0, check.stdout + check.stderr
    assert sorted(p.name for p in archives_dir(tmp_path).iterdir()) == [
        "mini-zfs.sha256",
        "mini-zfs.tar",
    ]


def test_pack_depends_only_on_the_rpms(tmp_path: Path, target: Path) -> None:
    (first,) = pack(load_plan(target), tmp_path, ["mini-zfs"])
    for rpm in (tmp_path / "output").rglob("*.rpm"):  # e.g. copied between CI jobs
        os.utime(rpm, (1_000_000_000, 1_000_000_000))
    (second,) = pack(load_plan(target), tmp_path, ["mini-zfs"])
    assert first.sha256 == second.sha256


def test_pack_refuses_a_file_that_is_not_an_rpm(tmp_path: Path, target: Path) -> None:
    write_outputs(tmp_path / "output", "boot", {"zefosa-boot.rpm": b"boot"})
    bogus = tmp_path / "output" / "boot" / "zefosa-boot.rpm"
    bogus.write_bytes(b"not an rpm")
    manifest = {"outputs": [{"path": "zefosa-boot.rpm", "nevra": "x",
                             "sha256": hashlib.sha256(b"not an rpm").hexdigest()}]}  # fmt: skip
    (tmp_path / "output" / "boot" / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(BuildError, match=r"boot/zefosa-boot\.rpm: zefosa-boot\.rpm: not an RPM"):
        pack(load_plan(target), tmp_path, ["mini-zfs"])
    assert not archives_dir(tmp_path).exists()


def test_pack_without_names_packs_every_archive(tmp_path: Path, target: Path) -> None:
    packed = pack(load_plan(target), tmp_path, [])
    assert [a.tar.name for a in packed] == ["mini-zfs.tar", "mini-kernel.tar"]


def test_unknown_archive_name_is_rejected(tmp_path: Path, target: Path) -> None:
    with pytest.raises(ConfigError, match="unknown archive 'nope'; defined: mini-zfs, mini-kernel"):
        pack(load_plan(target), tmp_path, ["mini-kernel", "nope"])
    assert not archives_dir(tmp_path).exists()


def remove_zfs(output: Path) -> None:
    (output / TID / "zfs" / "zfs-2.3.9.rpm").unlink()


def unrecord_shim(output: Path) -> None:
    (output / TID / "boot" / "manifest.json").write_text(json.dumps({"outputs": []}))


def change_boot(output: Path) -> None:
    fake_rpm(output / "boot" / "zefosa-boot.rpm", buildtime=1, payload=b"rebuilt by hand")


def remove_boot_manifest(output: Path) -> None:
    (output / "boot" / "manifest.json").unlink()


@pytest.mark.parametrize(
    ("damage", "message"),
    [
        (remove_zfs, rf"{TID}/zfs/zfs-2\.3\.9\.rpm: not found"),
        (unrecord_shim, rf"{TID}/boot/shim\.rpm: not listed in output/{TID}/boot/manifest\.json"),
        (change_boot, r"boot/zefosa-boot\.rpm: sha256 differs from output/boot/manifest\.json"),
        (remove_boot_manifest, r"boot/zefosa-boot\.rpm: no manifest\.json records it"),
    ],
)
def test_pack_refuses_files_the_build_did_not_produce(
    tmp_path: Path, target: Path, damage: Callable[[Path], None], message: str
) -> None:
    (before,) = pack(load_plan(target), tmp_path, ["mini-zfs"])
    tar_bytes, checksum = before.tar.read_bytes(), before.checksum.read_text()
    damage(tmp_path / "output")
    with pytest.raises(BuildError, match=message):
        pack(load_plan(target), tmp_path, ["mini-zfs"])
    assert before.tar.read_bytes() == tar_bytes
    assert before.checksum.read_text() == checksum
    assert sorted(p.name for p in archives_dir(tmp_path).iterdir()) == [
        "mini-zfs.sha256",
        "mini-zfs.tar",
    ]


def test_pack_reports_every_problem_before_writing_anything(tmp_path: Path, target: Path) -> None:
    remove_zfs(tmp_path / "output")
    change_boot(tmp_path / "output")
    with pytest.raises(BuildError) as error:
        pack(load_plan(target), tmp_path, [])
    assert "zfs-2.3.9.rpm: not found" in str(error.value)
    assert "zefosa-boot.rpm: sha256 differs" in str(error.value)
    assert not archives_dir(tmp_path).exists()


def test_cli_pack(tmp_path: Path, target: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["pack", str(target), "mini-kernel"]) == 0
    out = capsys.readouterr().out
    tar = archives_dir(tmp_path) / "mini-kernel.tar"
    assert str(tar) in out
    assert hashlib.sha256(tar.read_bytes()).hexdigest() in out
    assert main(["pack", str(target), "nope"]) == 1
    assert "unknown archive 'nope'" in capsys.readouterr().err
