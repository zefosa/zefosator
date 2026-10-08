"""Regenerate trimmed `rpm --xml` header fixtures from cache/downloads/.

Run: uv run python -m tests.fixtures.make_fixtures
Keeps every tag kind but trims long arrays (files, provides, changelog) so fixtures stay small.
"""

import stat
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOWNLOADS = ROOT / "cache" / "downloads"
OUT = Path(__file__).resolve().parent

FIXTURES = {
    "f44-kernel": "kernel-7.2.9-200.fc44.x86_64.rpm",
    "f44-kernel-core": "kernel-core-7.2.9-200.fc44.x86_64.rpm",
    "f44-kernel-modules-core": "kernel-modules-core-7.2.9-200.fc44.x86_64.rpm",
    "f44-kernel-modules-extra": "kernel-modules-extra-7.2.9-200.fc44.x86_64.rpm",
    "f44-kernel-devel": "kernel-devel-7.2.9-200.fc44.x86_64.rpm",
    "f44-kernel-devel-matched": "kernel-devel-matched-7.2.9-200.fc44.x86_64.rpm",
    "el9-kernel-core": "kernel-core-5.14.0-687.52.1.el9_8.x86_64.rpm",
    "el9-kernel-abi-stablelists": "kernel-abi-stablelists-5.14.0-687.52.1.el9_8.noarch.rpm",
}
MAX_FLAGGED_FILES = 16
MAX_FILES_PER_KIND = 8
MAX_KERNEL_SYMBOLS = 5
MAX_CHANGELOG = 3
PER_FILE_PREFIXES = ("File", "Basenames", "Dirindexes")
DEP_KINDS = ("Provide", "Require", "Conflict", "Obsolete", "Recommend", "Suggest", "Supplement")


def values(tag: ET.Element) -> list[ET.Element]:
    return list(tag)


def keep(tag: ET.Element, indices: list[int]) -> None:
    children = values(tag)
    for child in children:
        tag.remove(child)
    for i in indices:
        tag.append(children[i])


def pick_files(tags: dict[str | None, ET.Element]) -> list[int]:
    """Prefer flagged files, then non-default verify flags, symlinks, dirs, regular files."""
    count = len(values(tags["Basenames"]))
    modes = [int(e.text or "0") for e in values(tags["Filemodes"])]
    flags = [int(e.text or "0") for e in values(tags["Fileflags"])]
    verify = [int(e.text or "0") for e in values(tags["Fileverifyflags"])]

    def rank(i: int) -> int:
        if flags[i]:
            return 0
        if verify[i] != 0xFFFFFFFF:
            return 1
        if stat.S_ISLNK(modes[i]):
            return 2
        if stat.S_ISDIR(modes[i]):
            return 3
        return 4

    by_rank = sorted(range(count), key=lambda i: (rank(i), i))
    chosen: list[int] = []
    for wanted in range(5):
        limit = MAX_FLAGGED_FILES if wanted == 0 else MAX_FILES_PER_KIND
        chosen += [i for i in by_rank if rank(i) == wanted][:limit]
    return sorted(chosen)


def trim_files(tags: dict[str | None, ET.Element]) -> None:
    count = len(values(tags["Basenames"]))
    indices = pick_files(tags)
    for name, tag in tags.items():
        if name and name.startswith(PER_FILE_PREFIXES) and len(values(tag)) == count:
            keep(tag, indices)
    dir_elems = values(tags["Dirindexes"])
    used = sorted({int(e.text or "0") for e in dir_elems})
    remap = {old: new for new, old in enumerate(used)}
    for elem in dir_elems:
        elem.text = str(remap[int(elem.text or "0")])
    keep(tags["Dirnames"], used)


def trim_deps(tags: dict[str | None, ET.Element]) -> None:
    for kind in DEP_KINDS:
        names = tags.get(f"{kind}name")
        if names is None:
            continue
        texts = [e.text or "" for e in values(names)]
        symbols = [
            i
            for i, t in enumerate(texts)
            if "(" in t
            and not t.startswith(("(", "installonlypkg(", "rpmlib("))
            and not t.endswith("(x86-64)")
        ]
        indices = sorted(
            [i for i in range(len(texts)) if i not in symbols] + symbols[:MAX_KERNEL_SYMBOLS]
        )
        for suffix in ("name", "flags", "version"):
            keep(tags[f"{kind}{suffix}"], indices)


def trim(root: ET.Element) -> None:
    tags = {t.get("name"): t for t in root}
    if "Basenames" in tags:
        trim_files(tags)
    trim_deps(tags)
    if "Changelogtext" in tags:
        texts = [e.text or "" for e in values(tags["Changelogtext"])]
        by_size = sorted(range(len(texts)), key=lambda i: len(texts[i]))
        indices = sorted(by_size[:MAX_CHANGELOG])
        for suffix in ("time", "name", "text"):
            keep(tags[f"Changelog{suffix}"], indices)
    for name in ("Dependsdict", "Filedependsx", "Filedependsn", "Classdict"):
        if name in tags:
            root.remove(tags[name])


def main() -> None:
    for fixture, rpm in FIXTURES.items():
        xml = subprocess.run(
            ["rpm", "-qp", "--xml", str(DOWNLOADS / rpm)], capture_output=True, check=True
        ).stdout
        root = ET.fromstring(xml)  # noqa: S314 -- trusted local rpm output
        trim(root)
        (OUT / f"{fixture}.xml").write_bytes(ET.tostring(root, encoding="utf-8"))


if __name__ == "__main__":
    main()
