"""Distro-independent boot packages: zefosa-secureboot-ca, zefosa-refind, zefosa-boot (spec 5)."""

import hashlib
import shlex
import shutil
import stat
import struct
import tempfile
import zipapp
import zlib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from zefosator.config import BootConfig
from zefosator.errors import BuildError
from zefosator.worker.bootspec import BuildDirs, SpecPackage, render_boot_spec, rpmbuild
from zefosator.worker.proc import run
from zefosator.worker.rpmheader import SENSE_EQUAL, Dependency, DepKind, FileEntry, read_header

ZEFOSA_ESP = "/boot/efi/EFI/zefosa"
KEYS_DIR = "/usr/share/zefosa/keys"
TEMPLATES = Path(__file__).resolve().parent.parent / "templates" / "boot"
PLUGIN = Path(__file__).resolve().parent.parent / "boot_files" / "20-zbm.install"
URL = "https://github.com/zefosa"
LICENSE = "MIT"


@dataclass(frozen=True)
class BootBuild:
    workdir: Path
    outdir: Path
    logs: Path


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


def zefosa_icon_png(size: int = 128) -> bytes:
    """A plain white "Z" on a rounded teal square (rEFInd os_* icon); replace with real art."""
    bg, fg, clear = (0x1F, 0x6F, 0x8B, 0xFF), (0xFF, 0xFF, 0xFF, 0xFF), (0, 0, 0, 0)
    margin, radius, stroke = size // 16, size // 6, size // 9
    lo, hi = size // 4, size - size // 4

    def pixel(x: int, y: int) -> tuple[int, int, int, int]:
        inner = (
            max(margin + radius - min(x, size - 1 - x), 0),
            max(margin + radius - min(y, size - 1 - y), 0),
        )
        if (
            min(x, y, size - 1 - x, size - 1 - y) < margin
            or inner[0] ** 2 + inner[1] ** 2 > radius**2
        ):
            return clear
        in_bar = lo <= x <= hi and (lo <= y < lo + stroke or hi - stroke < y <= hi)
        diagonal_x = hi - (y - lo) * (hi - lo) // max(hi - lo, 1)
        if in_bar or (lo <= y <= hi and abs(x - diagonal_x) <= stroke // 2):
            return fg
        return bg

    rows = b"".join(
        b"\x00" + b"".join(bytes(pixel(x, y)) for x in range(size)) for y in range(size)
    )
    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", header)
        + _png_chunk(b"IDAT", zlib.compress(rows, 9))
        + _png_chunk(b"IEND", b"")
    )


def build_zipapp(src_root: Path, out: Path) -> Path:
    """Single-file zefosa-boot for /usr/bin, run by the system python3 (3.9 on EL9)."""
    with tempfile.TemporaryDirectory() as tmp:
        stage = Path(tmp) / "app"
        shutil.copytree(
            src_root / "zefosa_boot",
            stage / "zefosa_boot",
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "ruff.toml"),
        )
        (stage / "__main__.py").write_text(
            "import sys\nfrom zefosa_boot.cli import main\nsys.exit(main())\n"
        )
        out.parent.mkdir(parents=True, exist_ok=True)
        zipapp.create_archive(stage, out, interpreter="/usr/bin/python3", compressed=True)
    out.chmod(0o755)
    return out


def _entry(path: str, mode: int, data: bytes = b"") -> FileEntry:
    regular = stat.S_ISREG(mode)
    return FileEntry(
        path=path,
        mode=mode,
        user="root",
        group="root",
        size=len(data),
        digest=hashlib.sha256(data).hexdigest() if regular else "",
        linkto="",
        flags=0,
        verifyflags=0xFFFFFFFF,
    )


def _stage(
    root: Path, files: Mapping[str, tuple[bytes, int]], dirs: tuple[str, ...]
) -> list[FileEntry]:
    # ESP directories are 0700 like efi-filesystem's and the vendor shim's; a directory shared by
    # several packages (EFI/zefosa) must have identical attributes in all of them.
    entries = [_entry(d, 0o40700 if d.startswith("/boot/efi/") else 0o40755) for d in dirs]
    for path, (data, mode) in sorted(files.items()):
        target = root / path.lstrip("/")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        target.chmod(mode)
        entries.append(_entry(path, 0o100000 | mode, data))
    return entries


def _fresh(build: BootBuild, name: str) -> BuildDirs:
    workdir = build.workdir / name
    shutil.rmtree(workdir, ignore_errors=True)
    (workdir / "root").mkdir(parents=True)
    return BuildDirs(workdir / "root", workdir, build.outdir, build.logs / f"{name}.log")


def build_ca_package(config: BootConfig, ca_der: Path, build: BootBuild) -> Path:
    dirs = _fresh(build, "ca")
    der = ca_der.read_bytes()
    pem = run(["openssl", "x509", "-inform", "DER", "-in", str(ca_der)]).encode()
    digest = hashlib.sha256(der).hexdigest()
    files = {
        f"{KEYS_DIR}/zefosa-ca.der": (der, 0o644),
        f"{KEYS_DIR}/zefosa-ca.pem": (pem, 0o644),
        f"{KEYS_DIR}/zefosa-ca.sha256": (f"{digest}  zefosa-ca.der\n".encode(), 0o644),
    }
    package = SpecPackage(
        name="zefosa-secureboot-ca",
        version=config.ca.version,
        release=str(config.ca.release),
        summary="zefosa Secure Boot CA certificate",
        license=LICENSE,
        url=URL,
        description=(
            "The zefosa Secure Boot CA certificate (SHA-256 of the DER file: "
            f"{digest}). Enroll it with `mokutil --import {KEYS_DIR}/zefosa-ca.der` "
            "to trust zefosa-signed ZFS modules and boot loaders."
        ),
        arch="noarch",
        deps={},
        files=_stage(dirs.root, files, ("/usr/share/zefosa", KEYS_DIR)),
        changelog=f"- zefosa Secure Boot CA {config.ca.version} ({digest[:16]})",
    )
    rpmbuild(render_boot_spec(package), dirs, "noarch")
    return build.outdir / f"zefosa-secureboot-ca-{config.ca.version}-{config.ca.release}.noarch.rpm"


def _extract(rpm: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    run(["sh", "-ec", f"rpm2cpio {shlex.quote(str(rpm))} | cpio -idm --quiet"], cwd=dest)


def build_refind_package(
    config: BootConfig, refind_rpm: Path, sign: tuple[Path, Path], build: BootBuild
) -> Path:
    digest = hashlib.sha256(refind_rpm.read_bytes()).hexdigest()
    if digest != config.refind.sha256:
        msg = f"{refind_rpm.name}: sha256 {digest} does not match the pinned {config.refind.sha256}"
        raise BuildError(msg)
    upstream = read_header(refind_rpm)
    if upstream.version != config.refind.version:
        msg = (
            f"{refind_rpm.name}: rEFInd version {upstream.version}, but refind.version is "
            f"{config.refind.version} in boot/boot.yaml"
        )
        raise BuildError(msg)
    dirs = _fresh(build, "refind")
    source = dirs.workdir / "source"
    _extract(refind_rpm, source)
    efi = source / "usr/share/rEFInd/refind/refind_x64.efi"
    if ".sbat" not in run(["objdump", "-h", str(efi)]):
        msg = f"{efi.name} has no .sbat section; shim would refuse it"
        raise BuildError(msg)
    key, cert = sign
    signed = dirs.workdir / "grubx64.efi"
    run(["sbsign", "--key", str(key), "--cert", str(cert), "--output", str(signed), str(efi)])
    run(["sbverify", "--cert", str(cert), str(signed)])
    # rEFInd falls back to its built-in icons; the menu only references the zefosa icon.
    files = {
        f"{ZEFOSA_ESP}/grubx64.efi": (signed.read_bytes(), 0o700),
        f"{ZEFOSA_ESP}/icons/os_zefosa.png": (zefosa_icon_png(), 0o700),
    }
    package = SpecPackage(
        name="zefosa-refind",
        version=config.refind.version,
        release=str(config.refind.release),
        summary="rEFInd boot manager signed for the zefosa Secure Boot chain",
        license=upstream.license,
        url=upstream.url,
        description=(
            f"rEFInd {upstream.version} from {refind_rpm.name}, signed with the zefosa signing "
            f"key and installed as shim's second stage {ZEFOSA_ESP}/grubx64.efi."
        ),
        arch="x86_64",
        deps={
            DepKind.REQUIRE: [Dependency("efi-filesystem", 0, "")],
            DepKind.CONFLICT: [Dependency("rEFInd", 0, "")],
        },
        files=_stage(dirs.root, files, (ZEFOSA_ESP, f"{ZEFOSA_ESP}/icons")),
        changelog=f"- Signed rEFInd {upstream.version} ({refind_rpm.name}) for zefosa",
    )
    rpmbuild(render_boot_spec(package), dirs, "x86_64")
    return (
        build.outdir / f"zefosa-refind-{config.refind.version}-{config.refind.release}.x86_64.rpm"
    )


_TOOLS_REQUIRES = (
    "zefosa-shim",
    "zefosa-refind",
    "zefosa-zfsbootmenu",
    "zefosa-zfsbootmenu-recovery",
    "zefosa-secureboot-ca",
    "efibootmgr",
    "mokutil",
    "python3",
    "zfs",
    "bash",
)
_POSTTRANS = """\
if [ ! -f /boot/efi/EFI/zefosa/refind.conf ]; then
    echo "zefosa-boot: run 'zefosa-boot setup' to configure the boot entry and enroll the zefosa CA"
fi
"""


def build_tools_package(
    config: BootConfig, src_root: Path, zbm_checkout: Path, build: BootBuild
) -> Path:
    dirs = _fresh(build, "tools")
    app = build_zipapp(src_root / "src", dirs.workdir / "zefosa-boot")
    files = {
        "/usr/bin/zefosa-boot": (app.read_bytes(), 0o755),
        "/usr/bin/zbm-kcl": ((zbm_checkout / "bin" / "zbm-kcl").read_bytes(), 0o755),
        "/usr/share/man/man8/zbm-kcl.8": (
            (zbm_checkout / "docs/man/dist/man8/zbm-kcl.8").read_bytes(),
            0o644,
        ),
        "/usr/share/licenses/zefosa-boot/LICENSE.zfsbootmenu": (
            (zbm_checkout / "LICENSE").read_bytes(),
            0o644,
        ),
        "/usr/lib/kernel/install.d/20-zbm.install": (PLUGIN.read_bytes(), 0o755),
        "/usr/share/zefosa-boot/refind.conf.in": (
            (TEMPLATES / "refind.conf.in").read_bytes(),
            0o644,
        ),
    }
    version, release = config.tools.version, str(config.tools.release)
    package = SpecPackage(
        name="zefosa-boot",
        version=version,
        release=release,
        summary="zefosa Secure Boot chain: shim, rEFInd, ZFSBootMenu and tools",
        license=LICENSE,
        url=URL,
        description=(
            "Installs the zefosa boot chain (vendor shim -> zefosa-signed rEFInd -> "
            "zefosa-signed ZFSBootMenu) instead of GRUB, the zefosa-boot command, zbm-kcl and a "
            "kernel-install plugin placing kernels where ZFSBootMenu finds them."
        ),
        arch="noarch",
        deps={
            DepKind.REQUIRE: [Dependency(name, 0, "") for name in _TOOLS_REQUIRES],
            DepKind.CONFLICT: [
                Dependency("grub2-efi-x64", 0, ""),
                Dependency("grub2-efi-x64-cdboot", 0, ""),
            ],
            DepKind.OBSOLETE: [
                Dependency("grub2-efi-x64", 0, ""),
                Dependency("grub2-efi-x64-cdboot", 0, ""),
            ],
            DepKind.PROVIDE: [Dependency("zefosa-boot-tools", SENSE_EQUAL, f"{version}-{release}")],
        },
        files=_stage(
            dirs.root, files, ("/usr/share/zefosa-boot", "/usr/share/licenses/zefosa-boot")
        ),
        changelog=f"- zefosa-boot {version}",
        scripts={"posttrans": _POSTTRANS},
    )
    rpmbuild(render_boot_spec(package), dirs, "noarch")
    return build.outdir / f"zefosa-boot-{version}-{release}.noarch.rpm"
