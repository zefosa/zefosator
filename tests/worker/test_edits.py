from dataclasses import replace
from pathlib import Path

from zefosator.config import Role
from zefosator.worker.edits import EditContext, apply_edits, kmod_name, rewrite_dep_version
from zefosator.worker.rpmheader import (
    SENSE_EQUAL,
    SENSE_GREATER,
    Dependency,
    DepKind,
    FileEntry,
    Header,
    parse_header_xml,
)

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
ZFS_KO = FileEntry(
    path="/lib/modules/7.2.9-200.fc44.x86_64/extra/zfs/zfs.ko.xz",
    mode=0o100644,
    user="root",
    group="root",
    size=123,
    digest="ab" * 32,
    linkto="",
    flags=0,
    verifyflags=0xFFFFFFFF,
)


def load(name: str) -> Header:
    return parse_header_xml((FIXTURES / f"{name}.xml").read_bytes(), source=name)


def ctx(role: Role | None = None, injected: tuple[FileEntry, ...] = ()) -> EditContext:
    return EditContext(
        version="7.2.9",
        upstream_release="200.fc44",
        zefosa_release="200.zefosa.fc44",
        uname_r="7.2.9-200.fc44.x86_64",
        zfs_version="2.4.4",
        zfs_tag="zfs-2.4.4",
        role=role,
        injected=injected,
        changelog_time=1791892800,
    )


def test_rewrite_dep_version_only_exact_upstream_evr() -> None:
    c = ctx()
    assert rewrite_dep_version("7.2.9-200.fc44", c) == "7.2.9-200.zefosa.fc44"
    assert rewrite_dep_version("1:7.2.9-200.fc44", c) == "1:7.2.9-200.zefosa.fc44"
    assert rewrite_dep_version("7.2.9-200.fc44.x86_64", c) == "7.2.9-200.fc44.x86_64"
    assert rewrite_dep_version("027", c) == "027"
    assert rewrite_dep_version("17.2.9-200.fc44", c) == "17.2.9-200.fc44"
    assert rewrite_dep_version("", c) == ""


def test_release_packager_and_versioned_provides_rewritten() -> None:
    edited = apply_edits(load("f44-kernel-core"), ctx())
    assert edited.release == "200.zefosa.fc44"
    assert edited.packager == "Zefosa"
    assert edited.vendor == "Fedora Project"
    provides = {(d.name, d.version) for d in edited.dep_list(DepKind.PROVIDE)}
    assert ("kernel-core", "7.2.9-200.zefosa.fc44") in provides
    assert ("kernel-core-uname-r", "7.2.9-200.fc44.x86_64") in provides
    assert ("kernel-core", "7.2.9-200.fc44") not in provides


def test_requires_with_release_rewritten_in_devel_matched() -> None:
    edited = apply_edits(load("f44-kernel-devel-matched"), ctx())
    requires = {(d.name, d.version) for d in edited.dep_list(DepKind.REQUIRE)}
    assert ("kernel-core", "7.2.9-200.zefosa.fc44") in requires
    assert ("kernel-devel", "7.2.9-200.zefosa.fc44") in requires


def test_core_role_adds_zfs_dracut_and_dkms_conflict() -> None:
    original = load("f44-kernel-core")
    edited = apply_edits(original, ctx(role="core"))
    added_req = set(edited.dep_list(DepKind.REQUIRE)) - set(original.dep_list(DepKind.REQUIRE))
    assert added_req == {Dependency("zfs-dracut", SENSE_GREATER | SENSE_EQUAL, "2.4.4")}
    assert Dependency("zfs-dkms", 0, "") in edited.dep_list(DepKind.CONFLICT)


def test_modules_role_adds_files_provides_and_obsoletes() -> None:
    original = load("f44-kernel-modules-core")
    edited = apply_edits(original, ctx(role="modules", injected=(ZFS_KO,)))
    assert edited.files == (*original.files, ZFS_KO)
    provides = set(edited.dep_list(DepKind.PROVIDE))
    assert Dependency("zfs-kmod", SENSE_EQUAL, "2.4.4") in provides
    assert Dependency("kmod(zfs.ko)", 0, "") in provides
    obsoletes = edited.dep_list(DepKind.OBSOLETE)
    assert Dependency("kmod-zfs-7.2.9-200.fc44.x86_64", 0, "") in obsoletes


def test_other_packages_get_no_role_additions() -> None:
    original = load("f44-kernel-modules-extra")
    edited = apply_edits(original, ctx())
    assert edited.dep_list(DepKind.CONFLICT) == original.dep_list(DepKind.CONFLICT)
    assert edited.files == original.files
    names = {d.name for d in edited.dep_list(DepKind.PROVIDE)}
    assert "zfs-kmod" not in names


def test_changelog_entry_prepended() -> None:
    original = load("f44-kernel")
    edited = apply_edits(original, ctx())
    first = edited.changelog[0]
    assert first.name == "Zefosa <zefosa@fogas.net> - 7.2.9-200.zefosa.fc44"
    assert first.text == "- Repackaged kernel-7.2.9-200.fc44 with OpenZFS 2.4.4 (zfs-2.4.4)"
    assert first.time == 1791892800
    assert edited.changelog[1:] == original.changelog


def test_epoch_appears_in_changelog_nevr() -> None:
    original = replace(load("f44-kernel"), epoch=1)
    first = apply_edits(original, ctx()).changelog[0]
    assert first.name == "Zefosa <zefosa@fogas.net> - 1:7.2.9-200.zefosa.fc44"
    assert "kernel-1:7.2.9-200.fc44" in first.text


def test_kmod_name_strips_compression_suffix() -> None:
    assert kmod_name("/lib/modules/x/extra/zfs/zfs.ko.xz") == "kmod(zfs.ko)"
    assert kmod_name("/lib/modules/x/extra/spl.ko") == "kmod(spl.ko)"
    assert kmod_name("/lib/modules/x/extra/spl.ko.zst") == "kmod(spl.ko)"


def test_kmod_provides_only_for_module_files() -> None:
    extra_dir = replace(ZFS_KO, path="/lib/modules/7.2.9-200.fc44.x86_64/extra", mode=0o40755)
    edited = apply_edits(load("f44-kernel-modules-core"), ctx("modules", (extra_dir, ZFS_KO)))
    kmods = [d.name for d in edited.dep_list(DepKind.PROVIDE) if d.name.startswith("kmod(")]
    assert "kmod(extra)" not in kmods
    assert "kmod(zfs.ko)" in kmods
