"""Typed model of an RPM header, parsed from `rpm -qp --xml` output."""

import stat
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from zefosator.errors import BuildError
from zefosator.worker.proc import run

# Dependency flag bits (rpmds.h, RPMSENSE_*)
SENSE_LESS = 1 << 1
SENSE_GREATER = 1 << 2
SENSE_EQUAL = 1 << 3
SENSE_POSTTRANS = 1 << 5
SENSE_PREREQ = 1 << 6
SENSE_PRETRANS = 1 << 7
SENSE_INTERP = 1 << 8
SENSE_SCRIPT_PRE = 1 << 9
SENSE_SCRIPT_POST = 1 << 10
SENSE_SCRIPT_PREUN = 1 << 11
SENSE_SCRIPT_POSTUN = 1 << 12
SENSE_SCRIPT_VERIFY = 1 << 13
SENSE_FIND_REQUIRES = 1 << 14
SENSE_FIND_PROVIDES = 1 << 15
SENSE_MISSINGOK = 1 << 19
SENSE_PREUNTRANS = 1 << 20
SENSE_POSTUNTRANS = 1 << 21
SENSE_RPMLIB = 1 << 24
SENSE_CONFIG = 1 << 28
SENSE_COMPARE = SENSE_LESS | SENSE_GREATER | SENSE_EQUAL
# Set by rpmbuild's dependency generators; records origin only, not meaning.
SENSE_GENERATOR_ORIGIN = SENSE_FIND_REQUIRES | SENSE_FIND_PROVIDES

# File flag bits (rpmfiles.h, RPMFILE_*)
FILE_CONFIG = 1 << 0
FILE_DOC = 1 << 1
FILE_MISSINGOK = 1 << 3
FILE_NOREPLACE = 1 << 4
FILE_GHOST = 1 << 6
FILE_LICENSE = 1 << 7
FILE_README = 1 << 8
FILE_ARTIFACT = 1 << 12
FILE_KNOWN = (
    FILE_CONFIG
    | FILE_DOC
    | FILE_MISSINGOK
    | FILE_NOREPLACE
    | FILE_GHOST
    | FILE_LICENSE
    | FILE_README
    | FILE_ARTIFACT
)

VERIFY_ALL = 0xFFFFFFFF


class DepKind(StrEnum):
    REQUIRE = "Require"
    PROVIDE = "Provide"
    CONFLICT = "Conflict"
    OBSOLETE = "Obsolete"
    RECOMMEND = "Recommend"
    SUGGEST = "Suggest"
    SUPPLEMENT = "Supplement"
    ENHANCE = "Enhance"
    ORDER = "Order"


class ScriptKind(StrEnum):
    """Script kinds; values are the header tag names of the script bodies."""

    PRETRANS = "Pretrans"
    PRE = "Prein"
    POST = "Postin"
    PREUN = "Preun"
    POSTUN = "Postun"
    POSTTRANS = "Posttrans"
    PREUNTRANS = "Preuntrans"
    POSTUNTRANS = "Postuntrans"
    VERIFY = "Verifyscript"


@dataclass(frozen=True)
class Dependency:
    name: str
    flags: int
    version: str


@dataclass(frozen=True)
class Script:
    kind: ScriptKind
    prog: tuple[str, ...]
    body: str | None


@dataclass(frozen=True)
class FileEntry:
    path: str
    mode: int
    user: str
    group: str
    size: int
    digest: str
    linkto: str
    flags: int
    verifyflags: int
    lang: str = ""
    caps: str = ""
    mtime: int = 0


@dataclass(frozen=True)
class ChangelogEntry:
    time: int
    name: str
    text: str


@dataclass(frozen=True)
class Header:
    name: str
    epoch: int | None
    version: str
    release: str
    arch: str
    summary: str
    description: str
    license: str
    url: str = ""
    bugurl: str = ""
    group: str = ""
    vendor: str = ""
    distribution: str = ""
    packager: str = ""
    payload_compressor: str = "xz"
    payload_flags: str = "2"
    digest_algo: int = 8
    deps: dict[DepKind, tuple[Dependency, ...]] = field(default_factory=dict)
    scripts: tuple[Script, ...] = ()
    files: tuple[FileEntry, ...] = ()
    changelog: tuple[ChangelogEntry, ...] = ()

    @property
    def evr(self) -> str:
        prefix = f"{self.epoch}:" if self.epoch is not None else ""
        return f"{prefix}{self.version}-{self.release}"

    def dep_list(self, kind: DepKind) -> tuple[Dependency, ...]:
        return self.deps.get(kind, ())


_SCALAR_STRING_TAGS = {
    "Name": "name",
    "Version": "version",
    "Release": "release",
    "Arch": "arch",
    "Summary": "summary",
    "Description": "description",
    "License": "license",
    "Url": "url",
    "Bugurl": "bugurl",
    "Group": "group",
    "Vendor": "vendor",
    "Distribution": "distribution",
    "Packager": "packager",
    "Payloadcompressor": "payload_compressor",
    "Payloadflags": "payload_flags",
}
_FILE_TAGS = frozenset(
    {
        "Basenames",
        "Dirnames",
        "Dirindexes",
        "Filemodes",
        "Fileusername",
        "Filegroupname",
        "Filesizes",
        "Filedigests",
        "Filelinktos",
        "Fileflags",
        "Fileverifyflags",
        "Filelangs",
        "Filecaps",
        "Filemtimes",
    }
)
# Tags rpmbuild regenerates or that carry no packaging intent; they are not reproduced.
IGNORED_TAGS = frozenset(
    {
        "Headeri18ntable",
        "Sigsize",
        "Sigmd5",
        "Sha1header",
        "Sha256header",
        "Rsaheader",
        "Dsaheader",
        "Sigpgp",
        "Siggpg",
        "Openpgp",
        "Payloadsha256",
        "Payloadsha256algo",
        "Payloadsha256alt",
        "Payloaddigest",
        "Payloaddigestalgo",
        "Payloaddigestalt",
        "Payloadsize",
        "Payloadsizealt",
        "Payloadformat",
        "Buildtime",
        "Buildhost",
        "Cookie",
        "Size",
        "Longsize",
        "Archivesize",
        "Longarchivesize",
        "Os",
        "Platform",
        "Optflags",
        "Rpmversion",
        "Sourcerpm",
        "Encoding",
        "Classdict",
        "Fileclass",
        "Dependsdict",
        "Filedependsx",
        "Filedependsn",
        "Filecolors",
        "Filedevices",
        "Fileinodes",
        "Filerdevs",
        "Filesignatures",
        "Filesignaturelength",
        "Filedigestalgo",
        "Epoch",
    }
)
_DEP_TAGS = frozenset(
    f"{kind.value}{suffix}" for kind in DepKind for suffix in ("name", "flags", "version")
)
_SCRIPT_TAGS = frozenset(f"{kind.value}{suffix}" for kind in ScriptKind for suffix in ("", "prog"))
_CHANGELOG_TAGS = frozenset({"Changelogtime", "Changelogname", "Changelogtext"})
KNOWN_TAGS = (
    frozenset(_SCALAR_STRING_TAGS)
    | _FILE_TAGS
    | IGNORED_TAGS
    | _DEP_TAGS
    | _SCRIPT_TAGS
    | _CHANGELOG_TAGS
)


def _read_tags(xml: bytes, source: str) -> dict[str, list[str]]:
    try:
        root = ET.fromstring(xml)  # noqa: S314 -- rpm output for packages we downloaded and verified
    except ET.ParseError as exc:
        msg = f"cannot parse rpm --xml output for {source}: {exc}"
        raise BuildError(msg) from exc
    tags: dict[str, list[str]] = {}
    for tag in root:
        name = tag.get("name", "")
        if name not in KNOWN_TAGS:
            msg = f"unsupported tag {name} in {source}"
            raise BuildError(msg)
        tags[name] = [child.text or "" for child in tag]
    return tags


def _ints(tags: dict[str, list[str]], name: str) -> list[int]:
    return [int(v) for v in tags.get(name, [])]


def _deps(tags: dict[str, list[str]]) -> dict[DepKind, tuple[Dependency, ...]]:
    deps: dict[DepKind, tuple[Dependency, ...]] = {}
    for kind in DepKind:
        names = tags.get(f"{kind.value}name", [])
        if not names:
            continue
        flags = _ints(tags, f"{kind.value}flags")
        versions = tags.get(f"{kind.value}version", [""] * len(names))
        deps[kind] = tuple(
            Dependency(n, f, v) for n, f, v in zip(names, flags, versions, strict=True)
        )
    return deps


def _scripts(tags: dict[str, list[str]]) -> tuple[Script, ...]:
    scripts = []
    for kind in ScriptKind:
        body = tags.get(kind.value)
        prog = tuple(tags.get(f"{kind.value}prog", []))
        if body is None and not prog:
            continue
        scripts.append(Script(kind=kind, prog=prog or ("/bin/sh",), body=body[0] if body else None))
    return tuple(scripts)


def _files(tags: dict[str, list[str]], source: str) -> tuple[FileEntry, ...]:
    basenames = tags.get("Basenames", [])
    count = len(basenames)
    dirnames = tags.get("Dirnames", [])
    dirindexes = _ints(tags, "Dirindexes")
    modes = _ints(tags, "Filemodes")
    sizes = _ints(tags, "Filesizes")
    flags = _ints(tags, "Fileflags")
    verify = _ints(tags, "Fileverifyflags")
    mtimes = _ints(tags, "Filemtimes") or [0] * count
    users = tags.get("Fileusername", [])
    groups = tags.get("Filegroupname", [])
    digests = tags.get("Filedigests", [])
    links = tags.get("Filelinktos", [])
    langs = tags.get("Filelangs") or [""] * count
    caps = tags.get("Filecaps") or [""] * count
    files = []
    for i in range(count):
        path = dirnames[dirindexes[i]] + basenames[i]
        mode = modes[i]
        if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode) or stat.S_ISLNK(mode)):
            msg = f"unsupported file type {stat.filemode(mode)} for {path} in {source}"
            raise BuildError(msg)
        if flags[i] & ~FILE_KNOWN:
            msg = f"unsupported file flags {flags[i]:#x} for {path} in {source}"
            raise BuildError(msg)
        files.append(
            FileEntry(
                path=path,
                mode=mode,
                user=users[i],
                group=groups[i],
                size=sizes[i],
                digest=digests[i],
                linkto=links[i],
                flags=flags[i],
                verifyflags=verify[i],
                lang=langs[i],
                caps=caps[i],
                mtime=mtimes[i],
            )
        )
    return tuple(files)


def parse_header_xml(xml: bytes, *, source: str) -> Header:
    tags = _read_tags(xml, source)
    scalars = {attr: tags[tag][0] for tag, attr in _SCALAR_STRING_TAGS.items() if tags.get(tag)}
    epoch = _ints(tags, "Epoch")
    digest_algo = _ints(tags, "Filedigestalgo")
    changelog = tuple(
        ChangelogEntry(time=t, name=n, text=x)
        for t, n, x in zip(
            _ints(tags, "Changelogtime"),
            tags.get("Changelogname", []),
            tags.get("Changelogtext", []),
            strict=True,
        )
    )
    return Header(
        **scalars,
        epoch=epoch[0] if epoch else None,
        digest_algo=digest_algo[0] if digest_algo else 8,
        deps=_deps(tags),
        scripts=_scripts(tags),
        files=_files(tags, source),
        changelog=changelog,
    )


def read_header(path: Path) -> Header:
    xml = run(["rpm", "-qp", "--xml", str(path)])
    return parse_header_xml(xml.encode(), source=path.name)
