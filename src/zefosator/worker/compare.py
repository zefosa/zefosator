"""Compare an expected (edited upstream) header with the header rpmbuild produced."""

from zefosator.worker.rpmheader import (
    SENSE_GENERATOR_ORIGIN,
    SENSE_INTERP,
    SENSE_RPMLIB,
    Dependency,
    DepKind,
    FileEntry,
    Header,
)

_IDENTITY = ("name", "epoch", "version", "release", "arch")
_FILE_FIELDS = ("mode", "user", "group", "size", "flags", "verifyflags", "digest", "linkto", "caps")
# rpmbuild derives these from the payload and scriptlets it builds; they are not package intent.
_REGENERATED = SENSE_INTERP | SENSE_RPMLIB


def _show(field: str, value: object) -> str:
    if field == "mode" and isinstance(value, int):
        return oct(value)
    if field in {"flags", "verifyflags"} and isinstance(value, int):
        return hex(value)
    return repr(value)


def _diff_files(
    name: str, expected: tuple[FileEntry, ...], actual: tuple[FileEntry, ...]
) -> list[str]:
    want = {f.path: f for f in expected}
    got = {f.path: f for f in actual}
    diffs = [f"{name}: file missing: {p}" for p in sorted(want.keys() - got.keys())]
    diffs += [f"{name}: unexpected file: {p}" for p in sorted(got.keys() - want.keys())]
    for path in sorted(want.keys() & got.keys()):
        for field in _FILE_FIELDS:
            a, b = getattr(want[path], field), getattr(got[path], field)
            if a != b:
                shown = f"expected {_show(field, a)}, got {_show(field, b)}"
                diffs.append(f"{name}: file {path}: {field}: {shown}")
    return diffs


def _dep_key(dep: Dependency) -> tuple[str, int, str]:
    return dep.name, dep.flags & ~SENSE_GENERATOR_ORIGIN, dep.version


def _describe(kind: DepKind, key: tuple[str, int, str]) -> str:
    name, flags, version = key
    suffix = f" (flags {flags:#x}, version {version!r})" if flags or version else ""
    return f"{kind.value}: {name}{suffix}"


def _diff_deps(name: str, expected: Header, actual: Header) -> list[str]:
    diffs = []
    for kind in DepKind:
        # Sets, not multisets: rpm merges dependencies that differ only in origin bits.
        want = {_dep_key(d) for d in expected.dep_list(kind) if not d.flags & _REGENERATED}
        got = {_dep_key(d) for d in actual.dep_list(kind) if not d.flags & _REGENERATED}
        diffs += [f"{name}: missing {_describe(kind, k)}" for k in sorted(want - got)]
        diffs += [f"{name}: unexpected {_describe(kind, k)}" for k in sorted(got - want)]
    return diffs


def _diff_scripts(name: str, expected: Header, actual: Header) -> list[str]:
    want = {s.kind: s for s in expected.scripts}
    got = {s.kind: s for s in actual.scripts}
    diffs = []
    for kind in sorted(want.keys() | got.keys()):
        if kind not in got:
            diffs.append(f"{name}: script {kind.value} missing")
        elif kind not in want:
            diffs.append(f"{name}: unexpected script {kind.value}")
        elif want[kind] != got[kind]:
            diffs.append(f"{name}: script {kind.value} differs")
    return diffs


def diff_headers(expected: Header, actual: Header) -> list[str]:
    """Return human-readable differences; an empty list means the rebuild is faithful."""
    name = expected.name
    diffs = [
        f"{name}: {field}: expected {getattr(expected, field)!r}, got {getattr(actual, field)!r}"
        for field in _IDENTITY
        if getattr(expected, field) != getattr(actual, field)
    ]
    diffs += _diff_files(name, expected.files, actual.files)
    diffs += _diff_scripts(name, expected, actual)
    diffs += _diff_deps(name, expected, actual)
    return diffs
