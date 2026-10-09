"""Repackage the vendor shim without GRUB requirements, EFI binaries untouched (spec section 5)."""

import hashlib
import re
import shlex
import shutil
import stat
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from zefosator.config import Plan
from zefosator.errors import BuildError
from zefosator.worker.bootspec import BuildDirs, SpecPackage, render_boot_spec, rpmbuild
from zefosator.worker.proc import run
from zefosator.worker.rpmheader import (
    SENSE_INTERP,
    SENSE_RPMLIB,
    Dependency,
    DepKind,
    FileEntry,
    Header,
    read_header,
)
from zefosator.worker.specgen import payload_macro

ZEFOSA_DIR = "/boot/efi/EFI/zefosa"
BOOT_DIR = "/boot/efi/EFI/BOOT"
# shim's fallback (fbx64.efi) recreates boot entries from this file; UTF-16LE + BOM like the vendor.
ZEFOSA_CSV = b"\xff\xfe" + "shimx64.efi,zefosa,,This is the boot entry for zefosa\n".encode(
    "utf-16-le"
)
_BOOT_FILES = ("BOOTX64.EFI", "fbx64.efi")
EFI_MODE = 0o100700


@dataclass(frozen=True)
class ShimPaths:
    workdir: Path
    outdir: Path
    logs: Path


def filter_requires(deps: Sequence[Dependency], patterns: Sequence[str]) -> tuple[Dependency, ...]:
    """Drop rpmbuild-generated requirements and those matching any GRUB pattern."""
    compiled = [re.compile(p) for p in patterns]
    return tuple(
        d
        for d in deps
        if not d.flags & (SENSE_INTERP | SENSE_RPMLIB)
        and not any(c.search(d.name) for c in compiled)
    )


def collect_vendor_efi(root: Path, esp_dir: str) -> dict[str, Path]:
    """Map zefosa ESP paths to extracted vendor files (pristine /usr/lib copies preferred)."""
    found: dict[str, Path] = {}
    for efi in sorted(root.rglob("EFI"), key=lambda p: "usr/lib" not in str(p)):
        vendor = efi / esp_dir
        for src in sorted(vendor.glob("*.efi")) if vendor.is_dir() else ():
            found.setdefault(f"{ZEFOSA_DIR}/{src.name}", src)
        for name in _BOOT_FILES:
            if (efi / "BOOT" / name).is_file():
                found.setdefault(f"{BOOT_DIR}/{name}", efi / "BOOT" / name)
    if f"{ZEFOSA_DIR}/shimx64.efi" not in found:
        msg = f"vendor shim payload has no EFI/{esp_dir}/shimx64.efi"
        raise BuildError(msg)
    return found


def _deps(
    header: Header, plan_drop: Sequence[str], new_evr: str
) -> dict[DepKind, list[Dependency]]:
    deps: dict[DepKind, list[Dependency]] = {
        DepKind.REQUIRE: list(filter_requires(header.dep_list(DepKind.REQUIRE), plan_drop))
    }
    own = {header.name, f"{header.name}({header.arch.replace('_', '-')})"}
    deps[DepKind.PROVIDE] = [
        replace(d, version=new_evr) if d.version == header.evr else d
        for d in header.dep_list(DepKind.PROVIDE)
        if not (d.name in own and d.version == header.evr)  # rpmbuild adds self-provides
    ]
    for kind in (DepKind.CONFLICT, DepKind.OBSOLETE):
        deps[kind] = list(header.dep_list(kind))
    return deps


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


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _check_vendor(header: Header, plan: Plan, vendor_rpm: Path) -> None:
    assert plan.boot is not None  # noqa: S101 -- checked by build_shim
    want = ("shim-x64", plan.boot.shim_version, plan.boot.shim_release)
    if (header.name, header.version, header.release) != want:
        msg = f"{vendor_rpm.name} is {header.name}-{header.evr}, expected {'-'.join(want)}"
        raise BuildError(msg)


def _stage(vendor: Path, root: Path, files: dict[str, Path]) -> list[FileEntry]:
    entries = [_entry(ZEFOSA_DIR, 0o40700)]
    staged = {dest: src.read_bytes() for dest, src in files.items()}
    staged[f"{ZEFOSA_DIR}/BOOTX64.CSV"] = ZEFOSA_CSV
    for dest, data in sorted(staged.items()):
        target = root / dest.lstrip("/")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        target.chmod(stat.S_IMODE(EFI_MODE))
        entries.append(_entry(dest, EFI_MODE, data))
    protected = vendor / "etc/dnf/protected.d/shim.conf"
    if protected.is_file():
        copy = root / "etc/dnf/protected.d/shim.conf"
        copy.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(protected, copy)
        copy.chmod(0o644)
        data = protected.read_bytes()
        entries.append(_entry("/etc/dnf/protected.d/shim.conf", 0o100644, data))
    return entries


def _extract(rpm: Path, dest: Path, log: Path | None = None) -> None:
    dest.mkdir(parents=True)
    run(["sh", "-ec", f"rpm2cpio {shlex.quote(str(rpm))} | cpio -idm --quiet"], cwd=dest, log=log)


def build_shim(plan: Plan, vendor_rpm: Path, paths: ShimPaths) -> Path:
    if plan.boot is None:
        msg = f"{plan.target_id}: no boot settings for shim"
        raise BuildError(msg)
    boot = plan.boot
    header = read_header(vendor_rpm)
    _check_vendor(header, plan, vendor_rpm)
    shutil.rmtree(paths.workdir, ignore_errors=True)
    vendor, root = paths.workdir / "vendor", paths.workdir / "root"
    _extract(vendor_rpm, vendor, paths.logs / "shim.log")
    files = collect_vendor_efi(vendor, boot.esp_dir)
    release = boot.zefosa_shim_release
    nevr = f"{header.name}-{header.evr}"
    package = SpecPackage(
        name="shim-x64",
        version=header.version,
        release=release,
        summary=header.summary,
        license=header.license,
        url=header.url,
        description=(
            f"{header.description}\n\nzefosa: {nevr} from {plan.vendor}; its vendor-signed EFI "
            f"binaries are unchanged and installed in {ZEFOSA_DIR}; GRUB is not required."
        ),
        arch=header.arch,
        deps=_deps(header, boot.drop_requires, f"{header.version}-{release}"),
        files=_stage(vendor, root, files),
        changelog=f"- Repackaged {nevr}: EFI binaries in {ZEFOSA_DIR}, no GRUB requirement",
    )
    spec = render_boot_spec(
        package, payload=payload_macro(header.payload_compressor, header.payload_flags)
    )
    dirs = BuildDirs(root, paths.workdir, paths.outdir, paths.logs / "shim-rpmbuild.log")
    rpmbuild(spec, dirs, header.arch)
    built = paths.outdir / f"shim-x64-{header.version}-{release}.{header.arch}.rpm"
    check = paths.workdir / "check"
    _extract(built, check)
    for dest, src in files.items():
        if _sha256(check / dest.lstrip("/")) != _sha256(src):
            msg = f"{dest} in {built.name} differs from the vendor's {src.name}"
            raise BuildError(msg)
    return built
