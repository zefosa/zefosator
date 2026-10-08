"""Rebuild upstream kernel RPMs as-is with zefosa edits and the ZFS modules injected."""

import hashlib
import lzma
import os
import shlex
import shutil
import stat
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from zefosator.config import Role
from zefosator.errors import BuildError
from zefosator.worker.compare import diff_headers
from zefosator.worker.edits import EditContext, apply_edits
from zefosator.worker.proc import run
from zefosator.worker.rpmheader import FILE_GHOST, VERIFY_ALL, FileEntry, Header, read_header
from zefosator.worker.specgen import render_spec

MAX_REPORTED_DIFFS = 50
DIR_MODE = 0o40755
MODULE_MODE = 0o100644
# Same container settings the kernel build uses for .ko.xz modules (scripts/Makefile.modinst).
_XZ_FILTERS = [{"id": lzma.FILTER_LZMA2, "dict_size": 1 << 20}]


@dataclass(frozen=True)
class PackageInput:
    name: str
    role: Role | None
    rpm: Path


@dataclass(frozen=True)
class RepackPaths:
    kmod_root: Path  # extracted kmod-zfs payload
    workdir: Path  # wiped: build root, spec, rpmbuild topdir
    outdir: Path  # rebuilt RPMs
    logs: Path


@dataclass(frozen=True)
class RepackContext:
    version: str
    upstream_release: str
    zefosa_release: str
    uname_r: str
    arch: str
    zfs_version: str
    zfs_tag: str
    changelog_time: int


def _entry(path: str, mode: int, data: bytes = b"") -> FileEntry:
    regular = stat.S_ISREG(mode)
    return FileEntry(
        path=path,
        mode=mode,
        user="root",
        group="root",
        size=len(data) if regular else 0,
        digest=hashlib.sha256(data).hexdigest() if regular else "",
        linkto="",
        flags=0,
        verifyflags=VERIFY_ALL,
    )


def stage_modules(
    kmod_root: Path, uname_r: str, root: Path, *, compress: bool, known_paths: frozenset[str]
) -> tuple[FileEntry, ...]:
    """Copy kmod-zfs modules into the build root; return their file entries (new dirs included)."""
    moddir = kmod_root / "lib" / "modules" / uname_r
    # rpmbuild adds /usr/lib/.build-id links to kmod packages; the kernel packages do not need them.
    build_ids = kmod_root / "usr" / "lib" / ".build-id"
    files = sorted(
        p for p in kmod_root.rglob("*") if not p.is_dir() and not p.is_relative_to(build_ids)
    )
    unexpected = [p for p in files if not p.is_relative_to(moddir) or ".ko" not in p.name]
    if unexpected:
        listed = ", ".join(str(p.relative_to(kmod_root)) for p in unexpected)
        msg = (
            f"kmod-zfs payload has files other than modules under /lib/modules/{uname_r}: {listed}"
        )
        raise BuildError(msg)
    if not files:
        msg = f"no kernel modules found in kmod-zfs payload under /lib/modules/{uname_r}"
        raise BuildError(msg)
    entries: list[FileEntry] = []
    seen_dirs: set[str] = set(known_paths)
    for module in files:
        rel = module.relative_to(kmod_root)
        for parent in reversed(rel.parents[:-1]):
            path = f"/{parent}"
            if Path(path).is_relative_to(f"/lib/modules/{uname_r}") and path not in seen_dirs:
                if path != f"/lib/modules/{uname_r}":
                    entries.append(_entry(path, DIR_MODE))
                seen_dirs.add(path)
            (root / parent).mkdir(parents=True, exist_ok=True)
        data = module.read_bytes()
        name = rel.name
        if compress and name.endswith(".ko"):
            data = lzma.compress(
                data, format=lzma.FORMAT_XZ, check=lzma.CHECK_CRC32, filters=_XZ_FILTERS
            )
            name += ".xz"
        dest = root / rel.parent / name
        dest.write_bytes(data)
        dest.chmod(stat.S_IMODE(MODULE_MODE))
        entries.append(_entry(f"/{rel.parent / name}", MODULE_MODE, data))
    return tuple(entries)


def _vendor_compresses_modules(header: Header) -> bool:
    suffixes = {
        f.path.rsplit(".ko", 1)[1] for f in header.files if ".ko" in f.path.rsplit("/", 1)[-1]
    }
    if suffixes - {"", ".xz"}:
        msg = f"{header.name}: unsupported module compression {sorted(suffixes - {'', '.xz'})}"
        raise BuildError(msg)
    return ".xz" in suffixes


def _create_ghosts(header: Header, root: Path) -> None:
    """%ghost files are not in the payload; recreate them so rpmbuild records their attributes."""
    for entry in header.files:
        if not entry.flags & FILE_GHOST:
            continue
        target = root / entry.path.lstrip("/")
        if target.exists() or target.is_symlink():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if stat.S_ISDIR(entry.mode):
            target.mkdir()
        elif stat.S_ISLNK(entry.mode):
            target.symlink_to(entry.linkto)
            continue
        else:
            with target.open("wb") as handle:
                handle.truncate(entry.size)
        target.chmod(stat.S_IMODE(entry.mode))


def _shared_view(entry: FileEntry) -> FileEntry:
    return replace(entry, mtime=0)


def check_inputs(headers: Sequence[Header], ctx: RepackContext) -> None:
    """Upstream versions must match the plan; files shared by packages must be identical."""
    owners: dict[str, tuple[str, FileEntry]] = {}
    for header in headers:
        if (header.version, header.release) != (ctx.version, ctx.upstream_release):
            msg = (
                f"{header.name}: downloaded package is {header.version}-{header.release}, "
                f"expected {ctx.version}-{ctx.upstream_release}"
            )
            raise BuildError(msg)
        for entry in header.files:
            if stat.S_ISDIR(entry.mode):
                continue
            other, first = owners.setdefault(entry.path, (header.name, entry))
            if _shared_view(first) != _shared_view(entry):
                msg = f"{entry.path} differs between {other} and {header.name}"
                raise BuildError(msg)


def _extract(rpm: Path, root: Path, log: Path) -> None:
    command = f"rpm2cpio {shlex.quote(str(rpm))} | cpio -idm --quiet"
    run(["sh", "-ec", command], cwd=root, log=log)


def _fresh_dir(path: Path) -> Path:
    if path.exists():
        # Extracted trees contain read-only directories; make them writable before removal.
        for dirpath, _, _ in os.walk(path):
            Path(dirpath).chmod(0o755)
        shutil.rmtree(path)
    path.mkdir(parents=True)
    return path


def repackage(
    packages: Sequence[PackageInput], ctx: RepackContext, paths: RepackPaths
) -> list[Path]:
    """Rebuild `packages` (the first one is the spec's main package) and verify every result."""
    _fresh_dir(paths.workdir)
    paths.outdir.mkdir(parents=True, exist_ok=True)
    root = paths.workdir / "root"
    root.mkdir()
    headers = [read_header(p.rpm) for p in packages]
    check_inputs(headers, ctx)
    for package in packages:
        _extract(package.rpm, root, paths.logs / "extract.log")
    for header in headers:
        _create_ghosts(header, root)

    expected: list[Header] = []
    for package, header in zip(packages, headers, strict=True):
        injected: tuple[FileEntry, ...] = ()
        if package.role == "modules":
            injected = stage_modules(
                paths.kmod_root,
                ctx.uname_r,
                root,
                compress=_vendor_compresses_modules(header),
                known_paths=frozenset(f.path for f in header.files),
            )
        edit_ctx = EditContext(
            version=ctx.version,
            upstream_release=ctx.upstream_release,
            zefosa_release=ctx.zefosa_release,
            uname_r=ctx.uname_r,
            zfs_version=ctx.zfs_version,
            zfs_tag=ctx.zfs_tag,
            role=package.role,
            injected=injected,
            changelog_time=ctx.changelog_time,
            source_name=packages[0].name,
        )
        expected.append(apply_edits(header, edit_ctx))

    spec = paths.workdir / "kernel.spec"
    spec.write_text(render_spec(expected[0], expected[1:]))
    run(
        [
            "rpmbuild",
            "-bb",
            "--target",
            ctx.arch,
            "--define",
            f"_topdir {paths.workdir / 'rpmbuild'}",
            "--define",
            f"_rpmdir {paths.outdir}",
            "--define",
            "_build_name_fmt %%{NAME}-%%{VERSION}-%%{RELEASE}.%%{ARCH}.rpm",
            "--define",
            f"_zefosa_root {root}",
            str(spec),
        ],
        log=paths.logs / "rpmbuild.log",
    )

    outputs: list[Path] = []
    diffs: list[str] = []
    for header in expected:
        built = paths.outdir / f"{header.name}-{header.version}-{header.release}.{header.arch}.rpm"
        if not built.is_file():
            msg = f"rpmbuild did not produce {built.name}"
            raise BuildError(msg)
        diffs += diff_headers(header, read_header(built))
        outputs.append(built)
    if diffs:
        shown = "\n".join(diffs[:MAX_REPORTED_DIFFS])
        more = len(diffs) - MAX_REPORTED_DIFFS
        tail = f"\n... and {more} more" if more > 0 else ""
        msg = f"rebuilt packages differ from upstream beyond the zefosa edits:\n{shown}{tail}"
        raise BuildError(msg)
    return outputs
