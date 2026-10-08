"""Render one rpm spec that rebuilds a set of upstream binary packages as-is."""

import stat
import time
from collections.abc import Sequence
from dataclasses import dataclass

from jinja2 import Environment, PackageLoader, StrictUndefined

from zefosator.errors import BuildError
from zefosator.worker.rpmheader import (
    FILE_ARTIFACT,
    FILE_CONFIG,
    FILE_DOC,
    FILE_GHOST,
    FILE_LICENSE,
    FILE_MISSINGOK,
    FILE_NOREPLACE,
    FILE_README,
    SENSE_COMPARE,
    SENSE_EQUAL,
    SENSE_GENERATOR_ORIGIN,
    SENSE_GREATER,
    SENSE_INTERP,
    SENSE_LESS,
    SENSE_POSTTRANS,
    SENSE_POSTUNTRANS,
    SENSE_PRETRANS,
    SENSE_PREUNTRANS,
    SENSE_RPMLIB,
    SENSE_SCRIPT_POST,
    SENSE_SCRIPT_POSTUN,
    SENSE_SCRIPT_PRE,
    SENSE_SCRIPT_PREUN,
    SENSE_SCRIPT_VERIFY,
    VERIFY_ALL,
    Dependency,
    DepKind,
    FileEntry,
    Header,
    Script,
    ScriptKind,
)

_ENV = Environment(
    loader=PackageLoader("zefosator", "templates"),
    undefined=StrictUndefined,
    autoescape=False,  # noqa: S701 -- renders an rpm spec, not HTML; escaping is done in Python
    keep_trailing_newline=True,
    trim_blocks=True,
    lstrip_blocks=True,
)

DEP_TAG = {
    DepKind.REQUIRE: "Requires",
    DepKind.PROVIDE: "Provides",
    DepKind.CONFLICT: "Conflicts",
    DepKind.OBSOLETE: "Obsoletes",
    DepKind.RECOMMEND: "Recommends",
    DepKind.SUGGEST: "Suggests",
    DepKind.SUPPLEMENT: "Supplements",
    DepKind.ENHANCE: "Enhances",
    DepKind.ORDER: "OrderWithRequires",
}
# Qualifier order as rpm prints it.
_QUALIFIERS = (
    (SENSE_PRETRANS, "pretrans"),
    (SENSE_SCRIPT_PRE, "pre"),
    (SENSE_SCRIPT_POST, "post"),
    (SENSE_SCRIPT_PREUN, "preun"),
    (SENSE_SCRIPT_POSTUN, "postun"),
    (SENSE_POSTTRANS, "posttrans"),
    (SENSE_SCRIPT_VERIFY, "verify"),
    (SENSE_PREUNTRANS, "preuntrans"),
    (SENSE_POSTUNTRANS, "postuntrans"),
)
_QUALIFIER_BITS = sum(bit for bit, _ in _QUALIFIERS)
_OPERATORS = {
    SENSE_LESS: "<",
    SENSE_GREATER: ">",
    SENSE_EQUAL: "=",
    SENSE_LESS | SENSE_EQUAL: "<=",
    SENSE_GREATER | SENSE_EQUAL: ">=",
}
_SECTIONS = {
    ScriptKind.PRETRANS: "%pretrans",
    ScriptKind.PRE: "%pre",
    ScriptKind.POST: "%post",
    ScriptKind.PREUN: "%preun",
    ScriptKind.POSTUN: "%postun",
    ScriptKind.POSTTRANS: "%posttrans",
    ScriptKind.PREUNTRANS: "%preuntrans",
    ScriptKind.POSTUNTRANS: "%postuntrans",
    ScriptKind.VERIFY: "%verifyscript",
}
# Verify bits (rpmvf.h, RPMVERIFY_*) and their %verify keywords.
_VERIFY = (
    (1 << 0, "md5"),
    (1 << 1, "size"),
    (1 << 2, "link"),
    (1 << 3, "user"),
    (1 << 4, "group"),
    (1 << 5, "mtime"),
    (1 << 6, "mode"),
    (1 << 7, "rdev"),
    (1 << 8, "caps"),
)
_VERIFY_BITS = sum(bit for bit, _ in _VERIFY)
_ISA = {"x86_64": "x86-64", "aarch64": "aarch-64", "ppc64le": "ppc-64", "s390x": "s390-64"}
_UNSAFE_PATH_CHARS = frozenset('[]*?{}"\\% \t\n')
# Dependencies rpmbuild generates itself; emitting them would duplicate or contradict them.
_GENERATED = SENSE_INTERP | SENSE_RPMLIB


def escape(text: str) -> str:
    return text.replace("%", "%%")


def changelog_text(text: str) -> str:
    """rpmbuild reads a changelog line starting with `*` as a new entry; indent such lines."""
    return "\n".join(f" {line}" if line.startswith("*") else line for line in text.split("\n"))


def dependency_line(kind: DepKind, dep: Dependency) -> str:
    allowed = SENSE_COMPARE | SENSE_GENERATOR_ORIGIN
    if kind is DepKind.REQUIRE:
        allowed |= _QUALIFIER_BITS
    if dep.flags & ~allowed:
        msg = f"unsupported dependency flags {dep.flags:#x} on {DEP_TAG[kind]}: {dep.name}"
        raise BuildError(msg)
    qualifiers = [name for bit, name in _QUALIFIERS if dep.flags & bit]
    tag = DEP_TAG[kind] + (f"({','.join(qualifiers)})" if qualifiers else "")
    line = f"{tag}: {escape(dep.name)}"
    compare = dep.flags & SENSE_COMPARE
    if compare:
        line += f" {_OPERATORS[compare]} {escape(dep.version)}"
    return line


def _check_path(path: str) -> None:
    if _UNSAFE_PATH_CHARS & set(path):
        msg = f"unsupported characters in file path {path!r}"
        raise BuildError(msg)


def file_line(entry: FileEntry) -> str:
    _check_path(entry.path)
    parts = []
    if entry.flags & FILE_GHOST:
        parts.append("%ghost")
    if entry.flags & FILE_CONFIG:
        options = [
            name
            for bit, name in ((FILE_MISSINGOK, "missingok"), (FILE_NOREPLACE, "noreplace"))
            if entry.flags & bit
        ]
        parts.append(f"%config({','.join(options)})" if options else "%config")
    for bit, name in (
        (FILE_DOC, "%doc"),
        (FILE_LICENSE, "%license"),
        (FILE_README, "%readme"),
        (FILE_ARTIFACT, "%artifact"),
    ):
        if entry.flags & bit:
            parts.append(name)
    if stat.S_ISDIR(entry.mode):
        parts.append("%dir")
    if entry.verifyflags != VERIFY_ALL:
        if ~entry.verifyflags & VERIFY_ALL & ~_VERIFY_BITS:
            msg = f"unsupported verify flags {entry.verifyflags:#x} for {entry.path}"
            raise BuildError(msg)
        missing = [name for bit, name in _VERIFY if not entry.verifyflags & bit]
        parts.append(f"%verify(not {' '.join(missing)})")
    if entry.lang:
        parts.append(f"%lang({entry.lang})")
    if entry.caps:
        parts.append(f"%caps({entry.caps})")
    mode = "-" if stat.S_ISLNK(entry.mode) else f"{stat.S_IMODE(entry.mode):04o}"
    parts.append(f"%attr({mode},{entry.user},{entry.group})")
    parts.append(f'"{entry.path}"')
    return " ".join(parts)


@dataclass(frozen=True)
class ScriptView:
    header: str
    body: str | None


@dataclass(frozen=True)
class PackageView:
    name: str
    is_main: bool
    noarch: bool
    preamble: tuple[str, ...]
    dependencies: tuple[str, ...]
    description: str
    scripts: tuple[ScriptView, ...]
    files: tuple[str, ...]


@dataclass(frozen=True)
class ChangelogView:
    heading: str
    text: str


def _is_self_provide(header: Header, dep: Dependency) -> bool:
    names = {header.name}
    if header.arch in _ISA:
        names.add(f"{header.name}({_ISA[header.arch]})")
    return (
        dep.name in names and dep.flags & SENSE_COMPARE == SENSE_EQUAL and dep.version == header.evr
    )


def _dependencies(header: Header) -> tuple[str, ...]:
    lines = []
    for kind in DepKind:
        for dep in header.dep_list(kind):
            if dep.flags & _GENERATED:
                continue
            if kind is DepKind.PROVIDE and _is_self_provide(header, dep):
                continue
            lines.append(dependency_line(kind, dep))
    return tuple(lines)


def _script_view(header: Header, script: Script, *, is_main: bool) -> ScriptView:
    if len(script.prog) != 1:
        msg = f"unsupported script interpreter {script.prog!r} in {header.name}"
        raise BuildError(msg)
    line = _SECTIONS[script.kind]
    if not is_main:
        line += f" -n {header.name}"
    if script.prog != ("/bin/sh",) or script.body is None:
        line += f" -p {script.prog[0]}"
    return ScriptView(header=line, body=escape(script.body) if script.body is not None else None)


def _preamble(header: Header, *, is_main: bool) -> tuple[str, ...]:
    lines = []
    if is_main:
        if header.epoch is not None:
            lines.append(f"Epoch: {header.epoch}")
        lines += [
            f"Name: {header.name}",
            f"Version: {header.version}",
            f"Release: {header.release}",
        ]
    lines.append(f"Summary: {escape(header.summary)}")
    if header.arch == "noarch":
        lines.append("BuildArch: noarch")
    lines.append(f"License: {escape(header.license)}")
    optional = (
        ("URL", header.url),
        ("BugURL", header.bugurl),
        ("Group", header.group),
    )
    lines += [f"{tag}: {escape(value)}" for tag, value in optional if value]
    if is_main:
        global_tags = (
            ("Vendor", header.vendor),
            ("Distribution", header.distribution),
            ("Packager", header.packager),
        )
        lines += [f"{tag}: {escape(value)}" for tag, value in global_tags if value]
    lines.append("AutoReqProv: no")
    return tuple(lines)


def _package_view(header: Header, *, is_main: bool) -> PackageView:
    return PackageView(
        name=header.name,
        is_main=is_main,
        noarch=header.arch == "noarch",
        preamble=_preamble(header, is_main=is_main),
        dependencies=_dependencies(header),
        description=escape(header.description),
        scripts=tuple(_script_view(header, s, is_main=is_main) for s in header.scripts),
        files=tuple(file_line(f) for f in header.files),
    )


def _check_same_evr(main: Header, subpackages: Sequence[Header]) -> None:
    for sub in subpackages:
        if (sub.epoch, sub.version, sub.release) != (main.epoch, main.version, main.release):
            msg = f"{sub.name}: epoch/version/release {sub.evr} differs from {main.name} {main.evr}"
            raise BuildError(msg)


def render_spec(main: Header, subpackages: Sequence[Header]) -> str:
    """One spec: `main` is the spec's own package, the rest are `%package -n` subpackages."""
    _check_same_evr(main, subpackages)
    changelog = tuple(
        ChangelogView(
            heading=f"{time.strftime('%a %b %d %Y', time.gmtime(e.time))} {escape(e.name)}",
            text=escape(changelog_text(e.text)),
        )
        for e in main.changelog
    )
    return _ENV.get_template("repackaged.spec.j2").render(
        payload=f"w{main.payload_flags}.{main.payload_compressor}dio",
        digest_algo=main.digest_algo,
        packages=[
            _package_view(main, is_main=True),
            *(_package_view(s, is_main=False) for s in subpackages),
        ],
        changelog=changelog,
    )
