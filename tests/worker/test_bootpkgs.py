import shutil
import struct
import subprocess
import zlib
from pathlib import Path

import pytest

from tests.worker.upstream import needs_rpmbuild
from zefosator.config import load_boot_config
from zefosator.errors import BuildError
from zefosator.keys import KeyPair
from zefosator.worker.bootpkgs import (
    BootBuild,
    build_ca_package,
    build_refind_package,
    build_tools_package,
    build_zipapp,
    zefosa_icon_png,
)
from zefosator.worker.rpmheader import DepKind, ScriptKind, read_header

ROOT = Path(__file__).resolve().parents[2]
CONFIG = load_boot_config(ROOT)
Keys = tuple[KeyPair, KeyPair]
KEYS = "usr/share/zefosa/keys"


def files_of(rpm: Path) -> set[str]:
    return {f.path for f in read_header(rpm).files}


def fake_zbm_checkout(path: Path) -> Path:
    (path / "bin").mkdir(parents=True)
    (path / "bin" / "zbm-kcl").write_text("#!/bin/bash\necho kcl\n")
    (path / "docs/man/dist/man8").mkdir(parents=True)
    (path / "docs/man/dist/man8/zbm-kcl.8").write_text(".TH ZBM-KCL 8\n")
    (path / "LICENSE").write_text("MIT License\n")
    return path


def build_dirs(tmp_path: Path) -> BootBuild:
    return BootBuild(workdir=tmp_path / "work", outdir=tmp_path / "out", logs=tmp_path / "logs")


def test_icon_is_a_valid_png() -> None:
    data = zefosa_icon_png(128)
    assert data.startswith(b"\x89PNG\r\n\x1a\n")
    _, kind = struct.unpack(">I4s", data[8:16])
    assert kind == b"IHDR"
    width, height = struct.unpack(">II", data[16:24])
    assert (width, height) == (128, 128)
    idat = data.index(b"IDAT")
    size = struct.unpack(">I", data[idat - 4 : idat])[0]
    raw = zlib.decompress(data[idat + 4 : idat + 4 + size])
    assert len(raw) == 128 * (1 + 128 * 4)


def test_zipapp_runs_with_system_python(tmp_path: Path) -> None:
    app = build_zipapp(ROOT / "src", tmp_path / "zefosa-boot")
    assert app.read_bytes().startswith(b"#!/usr/bin/python3\n")
    for args in (["--help"], ["cmdline", "--help"]):
        result = subprocess.run(
            ["python3", str(app), *args], capture_output=True, text=True, check=False
        )
        assert result.returncode == 0, result.stderr
    bad = subprocess.run(["python3", str(app), "nope"], capture_output=True, text=True, check=False)
    assert bad.returncode == 2


@needs_rpmbuild
def test_ca_package(tmp_path: Path, signing_keys: Keys) -> None:
    ca, _ = signing_keys
    rpm = build_ca_package(CONFIG, ca.cert_der, build_dirs(tmp_path))
    assert rpm.name == "zefosa-secureboot-ca-1-1.noarch.rpm"
    assert files_of(rpm) >= {
        "/usr/share/zefosa/keys/zefosa-ca.der",
        "/usr/share/zefosa/keys/zefosa-ca.pem",
        "/usr/share/zefosa/keys/zefosa-ca.sha256",
    }
    text = subprocess.run(
        ["sh", "-ec", f"rpm2cpio {rpm} | cpio -i --quiet --to-stdout ./{KEYS}/zefosa-ca.sha256"],
        capture_output=True, text=True, check=True,
    ).stdout  # fmt: skip
    assert text == f"{ca.sha256}  zefosa-ca.der\n"


@needs_rpmbuild
def test_tools_package(tmp_path: Path) -> None:
    zbm = fake_zbm_checkout(tmp_path / "zbm")
    rpm = build_tools_package(CONFIG, ROOT, zbm, build_dirs(tmp_path))
    assert rpm.name == "zefosa-boot-1.0-1.noarch.rpm"
    assert files_of(rpm) >= {
        "/usr/bin/zefosa-boot",
        "/usr/bin/zbm-kcl",
        "/usr/share/man/man8/zbm-kcl.8",
        "/usr/lib/kernel/install.d/20-zbm.install",
        "/usr/share/zefosa-boot/refind.conf.in",
        "/usr/share/licenses/zefosa-boot/LICENSE.zfsbootmenu",
    }
    header = read_header(rpm)
    requires = {d.name for d in header.dep_list(DepKind.REQUIRE)}
    assert {
        "zefosa-shim",
        "zefosa-refind",
        "zefosa-zfsbootmenu",
        "zefosa-zfsbootmenu-recovery",
        "zefosa-secureboot-ca",
        "efibootmgr",
        "mokutil",
        "python3",
        "zfs",
    } <= requires
    assert {"grub2-efi-x64", "grub2-efi-x64-cdboot"} <= {
        d.name for d in header.dep_list(DepKind.OBSOLETE)
    }
    posttrans = next(s for s in header.scripts if s.kind is ScriptKind.POSTTRANS)
    assert posttrans.body is not None
    assert "zefosa-boot setup" in posttrans.body
    modes = {f.path: f.mode & 0o777 for f in header.files}
    assert modes["/usr/bin/zefosa-boot"] == 0o755
    assert modes["/usr/lib/kernel/install.d/20-zbm.install"] == 0o755


@pytest.mark.container
@needs_rpmbuild
@pytest.mark.skipif(shutil.which("sbsign") is None, reason="sbsigntools required")
def test_refind_package_from_pinned_fedora_rpm(tmp_path: Path, signing_keys: Keys) -> None:
    _, leaf = signing_keys
    source = ROOT / "cache" / "downloads" / CONFIG.refind.url.rsplit("/", 1)[1]
    if not source.is_file():
        pytest.skip("pinned rEFInd RPM not downloaded")
    rpm = build_refind_package(CONFIG, source, (leaf.key, leaf.cert_pem), build_dirs(tmp_path))
    assert rpm.name == "zefosa-refind-0.14.2-1.x86_64.rpm"
    paths = files_of(rpm)
    assert "/boot/efi/EFI/zefosa/grubx64.efi" in paths
    modes = {f.path: f.mode for f in read_header(rpm).files}
    # shared with shim-x64: rpm allows shared directories only with identical attributes
    assert modes["/boot/efi/EFI/zefosa"] == 0o40700
    assert "/boot/efi/EFI/zefosa/icons/os_zefosa.png" in paths
    out = tmp_path / "x"
    out.mkdir()
    subprocess.run(["sh", "-ec", f"rpm2cpio {rpm} | cpio -idm --quiet"], cwd=out, check=True)
    verify = subprocess.run(
        ["sbverify", "--cert", str(leaf.cert_pem), str(out / "boot/efi/EFI/zefosa/grubx64.efi")],
        capture_output=True, text=True, check=False,
    )  # fmt: skip
    assert verify.returncode == 0, verify.stderr


def test_refind_rpm_checksum_is_enforced(tmp_path: Path, signing_keys: Keys) -> None:
    _, leaf = signing_keys
    bogus = tmp_path / "rEFInd.rpm"
    bogus.write_bytes(b"not the pinned rpm")
    with pytest.raises(BuildError, match="sha256"):
        build_refind_package(CONFIG, bogus, (leaf.key, leaf.cert_pem), build_dirs(tmp_path))
