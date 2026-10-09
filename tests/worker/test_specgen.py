import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from tests.worker.upstream import needs_rpmbuild
from zefosator.errors import BuildError
from zefosator.worker.compare import diff_headers
from zefosator.worker.rpmheader import (
    FILE_CONFIG,
    FILE_GHOST,
    FILE_NOREPLACE,
    SENSE_EQUAL,
    SENSE_GREATER,
    SENSE_INTERP,
    SENSE_LESS,
    SENSE_POSTTRANS,
    SENSE_RPMLIB,
    SENSE_SCRIPT_POST,
    SENSE_SCRIPT_PRE,
    ChangelogEntry,
    Dependency,
    DepKind,
    FileEntry,
    Header,
    Script,
    ScriptKind,
    parse_header_xml,
    read_header,
)
from zefosator.worker.specgen import dependency_line, file_line, payload_macro, render_spec

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


def load(name: str) -> Header:
    return parse_header_xml((FIXTURES / f"{name}.xml").read_bytes(), source=name)


def regular(path: str, **changes: object) -> FileEntry:
    entry = FileEntry(
        path=path,
        mode=0o100644,
        user="root",
        group="root",
        size=1,
        digest="00",
        linkto="",
        flags=0,
        verifyflags=0xFFFFFFFF,
    )
    return replace(entry, **changes)  # type: ignore[arg-type] # test helper: keyword overrides


def minimal(name: str = "kernel", **changes: object) -> Header:
    header = Header(
        name=name,
        epoch=None,
        version="7.2.9",
        release="200.zefosa.fc44",
        arch="x86_64",
        summary="The Linux kernel",
        description="desc",
        license="GPL-2.0-only",
        payload_compressor="xz",
        payload_flags="3T",
        changelog=(ChangelogEntry(time=1791892800, name="Zefosa <z@f> - 7.2.9-200", text="- x"),),
    )
    return replace(header, **changes)  # type: ignore[arg-type] # test helper: keyword overrides


@pytest.mark.parametrize(
    ("dep", "kind", "expected"),
    [
        (
            Dependency("dracut", SENSE_SCRIPT_PRE | SENSE_GREATER | SENSE_EQUAL, "027"),
            DepKind.REQUIRE,
            "Requires(pre): dracut >= 027",
        ),
        (
            Dependency("systemd", SENSE_SCRIPT_PRE | SENSE_SCRIPT_POST, ""),
            DepKind.REQUIRE,
            "Requires(pre,post): systemd",
        ),
        (Dependency("x", SENSE_POSTTRANS, ""), DepKind.REQUIRE, "Requires(posttrans): x"),
        (
            Dependency("xfsprogs", SENSE_LESS, "4.3.0-1"),
            DepKind.CONFLICT,
            "Conflicts: xfsprogs < 4.3.0-1",
        ),
        (
            Dependency("k-uname-r", SENSE_EQUAL, "7.2.9-200.fc44.x86_64"),
            DepKind.REQUIRE,
            "Requires: k-uname-r = 7.2.9-200.fc44.x86_64",
        ),
        (Dependency("a", SENSE_LESS | SENSE_EQUAL, "1"), DepKind.OBSOLETE, "Obsoletes: a <= 1"),
        (
            Dependency("((fw >= 1) if fw)", SENSE_SCRIPT_PRE, ""),
            DepKind.REQUIRE,
            "Requires(pre): ((fw >= 1) if fw)",
        ),
        (Dependency("linux-firmware", 0, ""), DepKind.RECOMMEND, "Recommends: linux-firmware"),
        (Dependency("a%b", 0, ""), DepKind.PROVIDE, "Provides: a%%b"),
    ],
)
def test_dependency_line(dep: Dependency, kind: DepKind, expected: str) -> None:
    assert dependency_line(kind, dep) == expected


def test_generator_origin_bits_are_accepted_and_not_emitted() -> None:
    dep = Dependency("kernel(BUG_func)", SENSE_EQUAL | (1 << 15), "0x00000000")
    assert dependency_line(DepKind.PROVIDE, dep) == "Provides: kernel(BUG_func) = 0x00000000"


def test_dependency_with_unknown_flag_bits_rejected() -> None:
    with pytest.raises(BuildError, match="unsupported dependency flags"):
        dependency_line(DepKind.REQUIRE, Dependency("x", 1 << 22, ""))


@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        (regular("/a/b"), '%attr(0644,root,root) "/a/b"'),
        (regular("/a", mode=0o40755), '%dir %attr(0755,root,root) "/a"'),
        (regular("/l", mode=0o120777, linkto="t"), '%attr(-,root,root) "/l"'),
        (
            regular("/g", flags=FILE_GHOST, verifyflags=0xFFFFFFD8),
            '%ghost %verify(not md5 size link mtime) %attr(0644,root,root) "/g"',
        ),
        (
            regular("/c", flags=FILE_CONFIG | FILE_NOREPLACE),
            '%config(noreplace) %attr(0644,root,root) "/c"',
        ),
        (regular("/m", verifyflags=0xFFFFFFDF), '%verify(not mtime) %attr(0644,root,root) "/m"'),
        (regular("/d", flags=1 << 1), '%doc %attr(0644,root,root) "/d"'),
        (regular("/lic", flags=1 << 7), '%license %attr(0644,root,root) "/lic"'),
        (regular("/x", mode=0o104755, user="u", group="g"), '%attr(4755,u,g) "/x"'),
        (regular("/l", lang="de"), '%lang(de) %attr(0644,root,root) "/l"'),
        (regular("/p", caps="cap_net_raw=ep"), '%caps(cap_net_raw=ep) %attr(0644,root,root) "/p"'),
    ],
)
def test_file_line(entry: FileEntry, expected: str) -> None:
    assert file_line(entry) == expected


@pytest.mark.parametrize("path", ["/a b", "/a*", "/a[1]", '/a"', "/a%b", "/a?", "/a{b}", "/a\\b"])
def test_file_line_rejects_special_paths(path: str) -> None:
    with pytest.raises(BuildError, match="unsupported characters"):
        file_line(regular(path))


def test_spec_globals_and_payload() -> None:
    spec = render_spec(minimal(), [])
    assert "%global _binary_payload w3T.xzdio" in spec
    assert "%global _binary_filedigest_algorithm 8" in spec
    assert "%global __os_install_post %{nil}" in spec
    assert "%global debug_package %{nil}" in spec
    assert "AutoReqProv: no" in spec
    assert "cp -a %{_zefosa_root}/. %{buildroot}/" in spec
    assert "Name: kernel\nVersion: 7.2.9\nRelease: 200.zefosa.fc44\n" in spec
    assert "\n%files\n" in spec


def test_self_provides_and_regenerated_dependencies_are_omitted() -> None:
    header = minimal(
        deps={
            DepKind.PROVIDE: (
                Dependency("kernel", SENSE_EQUAL, "7.2.9-200.zefosa.fc44"),
                Dependency("kernel(x86-64)", SENSE_EQUAL, "7.2.9-200.zefosa.fc44"),
                Dependency("kernel-x86_64", SENSE_EQUAL, "7.2.9-200.zefosa.fc44"),
                Dependency("config(kernel)", SENSE_EQUAL | (1 << 28), "7.2.9-200.zefosa.fc44"),
            ),
            DepKind.REQUIRE: (
                Dependency("/bin/sh", SENSE_INTERP | SENSE_SCRIPT_POST, ""),
                Dependency(
                    "rpmlib(FileDigests)", SENSE_RPMLIB | SENSE_LESS | SENSE_EQUAL, "4.6.0-1"
                ),
            ),
        }
    )
    spec = render_spec(header, [])
    assert "Provides: kernel-x86_64 = 7.2.9-200.zefosa.fc44" in spec
    assert "Provides: kernel = " not in spec
    assert "Provides: kernel(x86-64)" not in spec
    assert "Provides: config(kernel) = 7.2.9-200.zefosa.fc44" in spec
    assert "/bin/sh" not in spec
    assert "rpmlib(" not in spec


def test_subpackage_sections_scripts_and_noarch() -> None:
    core = minimal(
        "kernel-core",
        description="Uses 100% CPU",
        scripts=(
            Script(ScriptKind.POSTTRANS, ("/bin/sh",), "\nrm -f %{x}\n"),
            Script(ScriptKind.POST, ("/sbin/ldconfig",), None),
            Script(ScriptKind.PREUN, ("<lua>",), "print(1)"),
        ),
        files=(regular("/boot/vmlinuz"),),
    )
    abi = minimal("kernel-abi-stablelists", arch="noarch")
    spec = render_spec(minimal(), [core, abi])
    assert "%package -n kernel-core\nSummary: The Linux kernel\n" in spec
    assert "%description -n kernel-core\nUses 100%% CPU\n" in spec
    assert "%posttrans -n kernel-core\n\nrm -f %%{x}\n" in spec
    assert "%post -n kernel-core -p /sbin/ldconfig\n" in spec
    assert "%preun -n kernel-core -p <lua>\nprint(1)\n" in spec
    assert '%files -n kernel-core\n%attr(0644,root,root) "/boot/vmlinuz"\n' in spec
    assert (
        "%package -n kernel-abi-stablelists\nSummary: The Linux kernel\nBuildArch: noarch\n" in spec
    )


def test_changelog_rendered_from_main_package_with_escaping() -> None:
    header = minimal(
        changelog=(
            ChangelogEntry(
                time=1791892800, name="Zefosa <z@f> - 7.2.9-200.zefosa.fc44", text="- new"
            ),
            ChangelogEntry(time=1790985600, name="Someone <s@x> - 7.2.9-200", text="- 50% faster"),
        )
    )
    spec = render_spec(header, [])
    assert spec.endswith(
        "%changelog\n"
        "* Tue Oct 13 2026 Zefosa <z@f> - 7.2.9-200.zefosa.fc44\n- new\n\n"
        "* Sat Oct 03 2026 Someone <s@x> - 7.2.9-200\n- 50%% faster\n"
    )


def test_subpackages_must_share_version_and_release() -> None:
    with pytest.raises(BuildError, match=r"kernel-core.*version"):
        render_spec(minimal(), [minimal("kernel-core", release="201.fc44")])


def test_multi_word_script_interpreter_rejected() -> None:
    header = minimal(scripts=(Script(ScriptKind.POST, ("/bin/sh", "-x"), "true"),))
    with pytest.raises(BuildError, match="interpreter"):
        render_spec(header, [])


def test_real_headers_render() -> None:
    main = load("f44-kernel")
    subs = [load("f44-kernel-core"), load("f44-kernel-modules-core"), load("f44-kernel-devel")]
    spec = render_spec(main, subs)
    assert "Requires(pre): dracut >= 027" in spec
    assert "%ghost %verify(not md5 size link mtime) %attr(0600,root,root)" in spec
    assert spec.count("%changelog") == 1


def test_changelog_lines_starting_with_star_are_indented() -> None:
    header = minimal(
        changelog=(ChangelogEntry(time=1791892800, name="A <a@b> - 1", text="- a\n* star\n- b"),)
    )
    assert "- a\n * star\n- b" in render_spec(header, [])


@needs_rpmbuild
def test_star_changelog_lines_survive_rpmbuild(tmp_path: Path) -> None:
    evr = "7.2.9-200.zefosa.fc44"
    header = minimal(
        group="Unspecified",
        deps={
            DepKind.PROVIDE: (
                Dependency("kernel", SENSE_EQUAL, evr),
                Dependency("kernel(x86-64)", SENSE_EQUAL, evr),
            )
        },
        description="Line\n%%not a macro",
        changelog=(
            ChangelogEntry(time=1791892800, name="A <a@b> - 7.2.9-200", text="- a\n* star\n- 5%"),
        ),
    )
    spec = tmp_path / "t.spec"
    spec.write_text(render_spec(header, []))
    (tmp_path / "root").mkdir()
    subprocess.run(
        [
            "rpmbuild",
            "-bb",
            "--quiet",
            "--target",
            "x86_64",
            "--define",
            f"_topdir {tmp_path / 'top'}",
            "--define",
            f"_rpmdir {tmp_path / 'out'}",
            "--define",
            "_build_name_fmt %%{NAME}.rpm",
            "--define",
            f"_zefosa_root {tmp_path / 'root'}",
            str(spec),
        ],
        check=True,
        capture_output=True,
    )
    assert diff_headers(header, read_header(tmp_path / "out" / "kernel.rpm")) == []


@pytest.mark.parametrize(
    ("compressor", "flags", "expected"),
    [
        ("xz", "3T", "w3T.xzdio"),
        ("zstd", "19", "w19.zstdio"),
        ("gzip", "9", "w9.gzdio"),
        ("bzip2", "9", "w9.bzdio"),
    ],
)
def test_payload_macro(compressor: str, flags: str, expected: str) -> None:
    assert payload_macro(compressor, flags) == expected


def test_unknown_payload_compressor_rejected() -> None:
    with pytest.raises(BuildError, match="lz4"):
        payload_macro("lz4", "1")
