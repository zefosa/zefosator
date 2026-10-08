"""The only changes zefosa makes to upstream kernel package headers."""

import posixpath
import re
from dataclasses import dataclass, replace

from zefosator.config import Role
from zefosator.worker.rpmheader import (
    SENSE_EQUAL,
    SENSE_GREATER,
    ChangelogEntry,
    Dependency,
    DepKind,
    FileEntry,
    Header,
)

PACKAGER = "Zefosa"
CHANGELOG_AUTHOR = "Zefosa <zefosa@fogas.net>"
_COMPRESSION_SUFFIXES = (".xz", ".gz", ".zst")


@dataclass(frozen=True)
class EditContext:
    version: str
    upstream_release: str
    zefosa_release: str
    uname_r: str
    zfs_version: str
    zfs_tag: str
    role: Role | None
    injected: tuple[FileEntry, ...]
    changelog_time: int


def rewrite_dep_version(version: str, ctx: EditContext) -> str:
    """Rewrite `[E:]V-upstream_release` exactly; leave every other version alone."""
    upstream = f"{ctx.version}-{ctx.upstream_release}"
    match = re.fullmatch(r"(\d+:)?(.*)", version)
    if match is None or match.group(2) != upstream:
        return version
    return f"{match.group(1) or ''}{ctx.version}-{ctx.zefosa_release}"


def kmod_name(path: str) -> str:
    """Provide name in the vendor's format: kmod(zfs.ko) for zfs.ko.xz."""
    base = posixpath.basename(path)
    for suffix in _COMPRESSION_SUFFIXES:
        base = base.removesuffix(suffix)
    return f"kmod({base})"


def _role_additions(ctx: EditContext) -> dict[DepKind, tuple[Dependency, ...]]:
    if ctx.role == "core":
        return {
            DepKind.REQUIRE: (
                Dependency("zfs-dracut", SENSE_GREATER | SENSE_EQUAL, ctx.zfs_version),
            ),
            DepKind.CONFLICT: (Dependency("zfs-dkms", 0, ""),),
        }
    if ctx.role == "modules":
        kmods = tuple(Dependency(kmod_name(f.path), 0, "") for f in ctx.injected)
        return {
            DepKind.PROVIDE: (Dependency("zfs-kmod", SENSE_EQUAL, ctx.zfs_version), *kmods),
            DepKind.OBSOLETE: (Dependency(f"kmod-zfs-{ctx.uname_r}", 0, ""),),
        }
    return {}


def apply_edits(header: Header, ctx: EditContext) -> Header:
    deps = {
        kind: tuple(replace(d, version=rewrite_dep_version(d.version, ctx)) for d in entries)
        for kind, entries in header.deps.items()
    }
    for kind, added in _role_additions(ctx).items():
        deps[kind] = (*deps.get(kind, ()), *added)
    files = (*header.files, *ctx.injected) if ctx.role == "modules" else header.files
    edited = replace(header, release=ctx.zefosa_release, packager=PACKAGER, deps=deps, files=files)
    upstream_nevr = f"{header.name}-{header.evr}"
    entry = ChangelogEntry(
        time=ctx.changelog_time,
        name=f"{CHANGELOG_AUTHOR} - {edited.evr}",
        text=f"- Repackaged {upstream_nevr} with OpenZFS {ctx.zfs_version} ({ctx.zfs_tag})",
    )
    return replace(edited, changelog=(entry, *header.changelog))
