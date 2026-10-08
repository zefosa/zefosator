from dataclasses import replace
from pathlib import Path

from zefosator.worker.compare import diff_headers
from zefosator.worker.rpmheader import (
    SENSE_EQUAL,
    SENSE_FIND_PROVIDES,
    SENSE_INTERP,
    SENSE_LESS,
    SENSE_RPMLIB,
    SENSE_SCRIPT_POST,
    Dependency,
    DepKind,
    Header,
    Script,
    ScriptKind,
    parse_header_xml,
)

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


def load(name: str) -> Header:
    return parse_header_xml((FIXTURES / f"{name}.xml").read_bytes(), source=name)


def test_identical_headers_have_no_differences() -> None:
    header = load("f44-kernel-core")
    assert diff_headers(header, header) == []


def test_identity_differences_reported() -> None:
    header = load("f44-kernel-core")
    diffs = diff_headers(header, replace(header, release="201.fc44"))
    assert diffs == ["kernel-core: release: expected '200.fc44', got '201.fc44'"]


def test_file_attribute_difference_reported() -> None:
    header = load("f44-kernel-core")
    files = list(header.files)
    files[0] = replace(files[0], digest="ff" * 32, mode=0o100600)
    diffs = diff_headers(header, replace(header, files=tuple(files)))
    path = header.files[0].path
    assert f"kernel-core: file {path}: mode: expected 0o100644, got 0o100600" in diffs
    assert any(d.startswith(f"kernel-core: file {path}: digest:") for d in diffs)


def test_missing_and_unexpected_files_reported() -> None:
    header = load("f44-kernel-core")
    extra = replace(header.files[0], path="/new")
    actual = replace(header, files=(*header.files[1:], extra))
    diffs = diff_headers(header, actual)
    assert f"kernel-core: file missing: {header.files[0].path}" in diffs
    assert "kernel-core: unexpected file: /new" in diffs


def test_script_difference_reported() -> None:
    header = load("f44-kernel-core")
    scripts = tuple(
        replace(s, body="changed") if s.kind is ScriptKind.POSTTRANS else s for s in header.scripts
    )
    diffs = diff_headers(header, replace(header, scripts=scripts))
    assert diffs == ["kernel-core: script Posttrans differs"]
    no_post = tuple(s for s in header.scripts if s.kind is not ScriptKind.POST)
    assert diff_headers(header, replace(header, scripts=no_post)) == [
        "kernel-core: script Postin missing"
    ]
    added = (*header.scripts, Script(ScriptKind.PRE, ("/bin/sh",), "x"))
    assert diff_headers(header, replace(header, scripts=added)) == [
        "kernel-core: unexpected script Prein"
    ]


def test_dependencies_compared_as_multisets_ignoring_generated() -> None:
    header = load("f44-kernel-core")
    deps = dict(header.deps)
    deps[DepKind.REQUIRE] = (
        *reversed(deps[DepKind.REQUIRE]),
        Dependency("/bin/sh", SENSE_INTERP | SENSE_SCRIPT_POST, ""),
        Dependency("rpmlib(PayloadIsZstd)", SENSE_RPMLIB | SENSE_LESS | SENSE_EQUAL, "5.4.18-1"),
    )
    deps[DepKind.PROVIDE] = tuple(
        replace(d, flags=d.flags & ~SENSE_FIND_PROVIDES) for d in deps[DepKind.PROVIDE]
    )
    assert diff_headers(header, replace(header, deps=deps)) == []


def test_dependency_changes_reported() -> None:
    header = load("f44-kernel-core")
    deps = dict(header.deps)
    deps[DepKind.CONFLICT] = (*deps[DepKind.CONFLICT], Dependency("zfs-dkms", 0, ""))
    deps[DepKind.REQUIRE] = tuple(d for d in deps[DepKind.REQUIRE] if d.name != "dracut")
    diffs = diff_headers(header, replace(header, deps=deps))
    assert "kernel-core: unexpected Conflict: zfs-dkms" in diffs
    assert any(d.startswith("kernel-core: missing Require: dracut") for d in diffs)
