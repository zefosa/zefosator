#!/usr/bin/python3
"""Find what differs between same-named RPMs of two builds (a reproducibility check).

    tools/compare-rpms.py DIR_A DIR_B [SUBSTRING ...]

For each RPM in both trees (only names containing a SUBSTRING, if given): byte equality, then the
header tags, the per-file attributes (size, mtime, digest, mode, owner) and, for files whose
contents differ, readelf/objdump differences for ELF files or the differing byte count otherwise.
Needs the system python3 with the rpm bindings (python3-rpm), rpm2cpio, cpio and binutils.
"""

import difflib
import hashlib
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import rpm  # type: ignore[import-not-found]  # system python3-rpm

MAX_DIFF_LINES = 30
MAX_CONTENT_FILES = 10
MAX_VALUE = 120
FILE_COLUMNS = (
    rpm.RPMTAG_FILESIZES,
    rpm.RPMTAG_FILEMTIMES,
    rpm.RPMTAG_FILEDIGESTS,
    rpm.RPMTAG_FILEMODES,
    rpm.RPMTAG_FILEUSERNAME,
)
# Per-file arrays are shown per file instead of as whole-tag differences.
PER_FILE_TAGS = {*FILE_COLUMNS, rpm.RPMTAG_FILEGROUPNAME, rpm.RPMTAG_FILEINODES}
DIGEST = 2  # index of the digest in a file's attribute tuple


def header(path: Path) -> rpm.hdr:
    ts = rpm.TransactionSet()
    ts.setVSFlags(rpm._RPMVSF_NOSIGNATURES | rpm._RPMVSF_NODIGESTS)  # noqa: SLF001
    fd = os.open(path, os.O_RDONLY)
    try:
        return ts.hdrFromFdno(fd)
    finally:
        os.close(fd)


def short(value: object) -> str:
    text = repr(value)
    return text if len(text) <= MAX_VALUE else text[: MAX_VALUE - 3] + "..."


def files(hdr: rpm.hdr) -> dict[str, tuple[object, ...]]:
    columns = [hdr[tag] for tag in FILE_COLUMNS]
    return {name: tuple(c[i] for c in columns) for i, name in enumerate(hdr[rpm.RPMTAG_FILENAMES])}


def extract(path: Path, dest: Path) -> None:
    dest.mkdir()
    with subprocess.Popen(["rpm2cpio", str(path)], stdout=subprocess.PIPE) as payload:
        subprocess.run(["cpio", "-idm", "--quiet"], stdin=payload.stdout, cwd=dest, check=True)


def output(argv: list[str]) -> list[str]:
    return subprocess.run(argv, capture_output=True, text=True, check=False).stdout.splitlines()


def show_diff(a: list[str], b: list[str], label: str) -> None:
    lines = list(difflib.unified_diff(a, b, "A", "B", lineterm="", n=1))
    print(f"      {label}: {len(lines)} diff lines")
    for line in lines[:MAX_DIFF_LINES]:
        print("        " + line[:160])


def compare_content(a: Path, b: Path, name: str) -> None:
    kind = subprocess.run(["file", "-b", str(a)], capture_output=True, text=True, check=False)
    print(f"    {name}: {kind.stdout.strip()[:100]}")
    if kind.stdout.startswith("ELF"):
        for argv in (
            ["readelf", "-W", "-S", "-n"],
            ["readelf", "-W", "--debug-dump=info"],
            ["objdump", "-d", "--no-show-raw-insn"],
        ):
            left = [line.replace(str(a), "F") for line in output([*argv, str(a)])]
            right = [line.replace(str(b), "F") for line in output([*argv, str(b)])]
            if left != right:
                show_diff(left, right, " ".join(argv))
    else:
        differing = output(["cmp", "-l", str(a), str(b)])
        print(f"      {len(differing)} differing bytes; first: {differing[:5]}")


def compare(a: Path, b: Path) -> bool:
    if a.read_bytes() == b.read_bytes():
        return True
    print(f"\n== {a.name}: DIFFERENT")
    ha, hb = header(a), header(b)
    for tag in sorted(set(ha.keys()) | set(hb.keys())):
        if tag not in PER_FILE_TAGS and ha[tag] != hb[tag]:
            print(f"  tag {rpm.tagnames.get(tag, tag)}: {short(ha[tag])} -> {short(hb[tag])}")
    fa, fb = files(ha), files(hb)
    changed = []
    for name in sorted(set(fa) | set(fb)):
        if fa.get(name) != fb.get(name):
            print(f"  file {name}: {short(fa.get(name))} -> {short(fb.get(name))}")
            if (
                name in fa
                and name in fb
                and fa[name][DIGEST]
                and fa[name][DIGEST] != fb[name][DIGEST]
            ):
                changed.append(name)
    if changed:
        with tempfile.TemporaryDirectory() as tmp:
            ea, eb = Path(tmp) / "a", Path(tmp) / "b"
            extract(a, ea)
            extract(b, eb)
            for name in changed[:MAX_CONTENT_FILES]:
                compare_content(ea / name.lstrip("/"), eb / name.lstrip("/"), name)
    return False


def rpms(directory: str, substrings: list[str]) -> dict[str, Path]:
    return {
        p.name: p
        for p in Path(directory).rglob("*.rpm")
        if not substrings or any(s in p.name for s in substrings)
    }


def main(argv: list[str]) -> int:
    if len(argv) < 2:  # noqa: PLR2004 -- two directories
        print(__doc__, file=sys.stderr)
        return 2
    ra, rb = rpms(argv[0], argv[2:]), rpms(argv[1], argv[2:])
    for name in sorted(set(ra) ^ set(rb)):
        print(f"only in {'A' if name in ra else 'B'}: {name}")
    common = sorted(set(ra) & set(rb))
    same = [name for name in common if compare(ra[name], rb[name])]
    print(f"\n{len(same)} of {len(common)} common RPMs byte-identical")
    for name in same:
        print(f"  identical: {name} {hashlib.sha256(ra[name].read_bytes()).hexdigest()[:12]}")
    return 0 if len(same) == len(common) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
