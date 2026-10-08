import hashlib
import lzma
import subprocess
from pathlib import Path

import pytest

from tests.worker.upstream import UNAME_R, build_kmod_payload, build_upstream, needs_rpmbuild
from zefosator.config import load_plan
from zefosator.errors import BuildError
from zefosator.worker.repackage import (
    PackageInput,
    RepackContext,
    RepackPaths,
    check_inputs,
    repackage,
    stage_modules,
)
from zefosator.worker.rpmheader import (
    SENSE_EQUAL,
    SENSE_GREATER,
    Dependency,
    DepKind,
    FileEntry,
    Header,
    read_header,
)

ROOT = Path(__file__).resolve().parents[2]
DOWNLOADS = ROOT / "cache" / "downloads"


def context() -> RepackContext:
    return RepackContext(
        version="7.2.9",
        upstream_release="200.fc44",
        zefosa_release="200.zefosa.fc44",
        uname_r=UNAME_R,
        arch="x86_64",
        zfs_version="2.4.4",
        zfs_tag="zfs-2.4.4",
        changelog_time=1791892800,
    )


def paths(tmp_path: Path, kmod: Path) -> RepackPaths:
    return RepackPaths(
        kmod_root=kmod, workdir=tmp_path / "work", outdir=tmp_path / "out", logs=tmp_path / "logs"
    )


def inputs(rpms: dict[str, Path]) -> list[PackageInput]:
    roles = {"kernel-core": "core", "kernel-modules-core": "modules", "kernel-devel": "devel"}
    return [PackageInput(name=n, role=roles.get(n), rpm=p) for n, p in rpms.items()]  # type: ignore[arg-type] # roles values are valid Role literals


@needs_rpmbuild
def test_repackage_synthetic_upstream(tmp_path: Path) -> None:
    rpms = build_upstream(tmp_path)
    kmod = build_kmod_payload(tmp_path / "kmod")
    outputs = repackage(inputs(rpms), context(), paths(tmp_path, kmod))
    names = sorted(p.name for p in outputs)
    assert names == sorted(
        [
            "kernel-7.2.9-200.zefosa.fc44.x86_64.rpm",
            "kernel-core-7.2.9-200.zefosa.fc44.x86_64.rpm",
            "kernel-modules-core-7.2.9-200.zefosa.fc44.x86_64.rpm",
            "kernel-devel-7.2.9-200.zefosa.fc44.x86_64.rpm",
            "kernel-devel-matched-7.2.9-200.zefosa.fc44.x86_64.rpm",
            "kernel-abi-stablelists-7.2.9-200.zefosa.fc44.noarch.rpm",
        ]
    )
    out = {p.name.split("-7.2.9")[0]: p for p in outputs}

    core = read_header(out["kernel-core"])
    assert Dependency("zfs-dracut", SENSE_GREATER | SENSE_EQUAL, "2.4.4") in core.dep_list(
        DepKind.REQUIRE
    )
    assert Dependency("zfs-dkms", 0, "") in core.dep_list(DepKind.CONFLICT)
    ghost = next(f for f in core.files if f.path == f"/boot/initramfs-{UNAME_R}.img")
    assert ghost.size == 65536

    modules = read_header(out["kernel-modules-core"])
    zfs = next(f for f in modules.files if f.path.endswith("/extra/zfs/zfs.ko.xz"))
    zfs_bytes = subprocess.run(
        [
            "sh",
            "-c",
            (
                f"rpm2cpio {out['kernel-modules-core']} | cpio -i --quiet --to-stdout "
                f"./lib/modules/{UNAME_R}/extra/zfs/zfs.ko.xz"
            ),
        ],
        capture_output=True,
        check=True,
    ).stdout
    assert lzma.decompress(zfs_bytes) == b"\x7fELF zfs module"
    assert zfs.digest == hashlib.sha256(zfs_bytes).hexdigest()
    provides = set(modules.dep_list(DepKind.PROVIDE))
    assert Dependency("zfs-kmod", SENSE_EQUAL, "2.4.4") in provides
    assert Dependency("kmod(spl.ko)", 0, "") in provides

    matched = read_header(out["kernel-devel-matched"])
    assert Dependency("kernel-core", SENSE_EQUAL, "7.2.9-200.zefosa.fc44") in matched.dep_list(
        DepKind.REQUIRE
    )
    upstream_scripts = subprocess.run(
        ["rpm", "-qp", "--scripts", str(rpms["kernel-core"])], capture_output=True, check=True
    ).stdout
    rebuilt_scripts = subprocess.run(
        ["rpm", "-qp", "--scripts", str(out["kernel-core"])], capture_output=True, check=True
    ).stdout
    assert rebuilt_scripts == upstream_scripts
    assert (tmp_path / "work" / "kernel.spec").is_file()
    assert (tmp_path / "logs" / "rpmbuild.log").is_file()


@needs_rpmbuild
def test_unexpected_files_in_kmod_payload_fail(tmp_path: Path) -> None:
    rpms = build_upstream(tmp_path)
    kmod = build_kmod_payload(tmp_path / "kmod", extra_file=True)
    with pytest.raises(BuildError, match=r"etc/zfs\.conf"):
        repackage(inputs(rpms), context(), paths(tmp_path, kmod))


def test_stage_modules_without_compression(tmp_path: Path) -> None:
    kmod = build_kmod_payload(tmp_path / "kmod")
    root = tmp_path / "root"
    entries = stage_modules(kmod, UNAME_R, root, compress=False, known_paths=frozenset())
    paths = {e.path for e in entries}
    assert f"/lib/modules/{UNAME_R}/extra/zfs/zfs.ko" in paths
    assert f"/lib/modules/{UNAME_R}/extra" in paths
    assert f"/lib/modules/{UNAME_R}/extra/zfs" in paths
    zfs = next(e for e in entries if e.path.endswith("zfs.ko"))
    assert zfs.digest == hashlib.sha256(b"\x7fELF zfs module").hexdigest()
    assert (root / f"lib/modules/{UNAME_R}/extra/zfs/zfs.ko").read_bytes() == b"\x7fELF zfs module"


def test_stage_modules_skips_directories_the_package_already_owns(tmp_path: Path) -> None:
    kmod = build_kmod_payload(tmp_path / "kmod")
    known = frozenset({f"/lib/modules/{UNAME_R}/extra"})
    entries = stage_modules(kmod, UNAME_R, tmp_path / "root", compress=True, known_paths=known)
    assert f"/lib/modules/{UNAME_R}/extra" not in {e.path for e in entries}


def test_stage_modules_requires_modules(tmp_path: Path) -> None:
    (tmp_path / "kmod").mkdir()
    with pytest.raises(BuildError, match="no kernel modules"):
        stage_modules(
            tmp_path / "kmod", UNAME_R, tmp_path / "root", compress=True, known_paths=frozenset()
        )


@pytest.mark.container
@needs_rpmbuild
@pytest.mark.parametrize(
    "target", ["fedora-44-x86_64", "almalinux-9-x86_64", "almalinux-10-x86_64"]
)
def test_repackage_real_upstream_packages(tmp_path: Path, target: str) -> None:
    plan = load_plan(ROOT / "targets" / f"{target}.yaml")
    missing = [p.file for p in plan.packages if not (DOWNLOADS / p.file).is_file()]
    if missing:
        pytest.skip(f"not downloaded: {missing}")
    packages = [
        PackageInput(name=p.name, role=p.role, rpm=DOWNLOADS / p.file) for p in plan.packages
    ]
    ctx = RepackContext(
        version=plan.kernel_version,
        upstream_release=plan.upstream_release,
        zefosa_release=plan.zefosa_release,
        uname_r=plan.uname_r,
        arch=plan.arch,
        zfs_version=plan.zfs.version,
        zfs_tag=plan.zfs.tag,
        changelog_time=1791892800,
    )
    moddir = tmp_path / "kmod" / "lib" / "modules" / plan.uname_r / "extra" / "zfs"
    moddir.mkdir(parents=True)
    (moddir / "zfs.ko").write_bytes(b"\x7fELF zfs")
    outputs = repackage(packages, ctx, paths(tmp_path, tmp_path / "kmod"))
    assert len(outputs) == len(plan.packages)


def _file(path: str, digest: str) -> FileEntry:
    return FileEntry(path, 0o100644, "root", "root", 1, digest, "", 0, 0xFFFFFFFF)


def _header(name: str, *files: FileEntry) -> Header:
    return Header(name, None, "7.2.9", "200.fc44", "x86_64", "s", "d", "GPL", files=files)


def test_identical_shared_files_are_allowed() -> None:
    shared = _file("/etc/a.conf", "aa")
    check_inputs([_header("a", shared), _header("b", shared)], context())


def test_differing_shared_files_are_rejected() -> None:
    with pytest.raises(BuildError, match=r"/etc/a\.conf differs between a and b"):
        check_inputs(
            [_header("a", _file("/etc/a.conf", "aa")), _header("b", _file("/etc/a.conf", "bb"))],
            context(),
        )


def test_wrong_upstream_release_rejected() -> None:
    header = Header("a", None, "7.2.9", "201.fc44", "x86_64", "s", "d", "GPL")
    with pytest.raises(BuildError, match=r"downloaded package is 7\.2\.9-201\.fc44"):
        check_inputs([header], context())


def test_stage_modules_ignores_build_id_links(tmp_path: Path) -> None:
    kmod = build_kmod_payload(tmp_path / "kmod")
    links = kmod / "usr" / "lib" / ".build-id" / "2e"
    links.mkdir(parents=True)
    (links / "e58fccae").symlink_to(f"../../../../lib/modules/{UNAME_R}/extra/zfs/zfs.ko")
    entries = stage_modules(
        kmod, UNAME_R, tmp_path / "root", compress=True, known_paths=frozenset()
    )
    assert not [e for e in entries if ".build-id" in e.path]
    assert not (tmp_path / "root" / "usr").exists()
