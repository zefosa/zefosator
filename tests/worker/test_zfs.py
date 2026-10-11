from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from tests.project import FIXTURE_ROOT
from zefosator.config import load_plan
from zefosator.errors import BuildError
from zefosator.worker.zfs import ZfsPaths, build_zfs

PLAN = load_plan(FIXTURE_ROOT / "targets" / "almalinux-9-x86_64.yaml")
UNAME = PLAN.uname_r
META = """Meta:          1
Name:          zfs
Branch:        1.0
Version:       2.3.9
Release:       1
Release-Tags:  relext
License:       CDDL
Author:        OpenZFS
Linux-Maximum: 6.15
Linux-Minimum: 4.18
"""
RPMS = {
    f"kmod-zfs-{UNAME}-2.3.9-0.el9.x86_64.rpm": f"kmod-zfs-{UNAME}",
    f"kmod-zfs-{UNAME}-debuginfo-2.3.9-0.el9.x86_64.rpm": f"kmod-zfs-{UNAME}-debuginfo",
    "kmod-zfs-devel-2.3.9-0.el9.x86_64.rpm": "kmod-zfs-devel",
    "zfs-2.3.9-0.el9.x86_64.rpm": "zfs",
    "zfs-dracut-2.3.9-0.el9.noarch.rpm": "zfs-dracut",
    "zfs-2.3.9-0.el9.src.rpm": "zfs",
}


class FakeRunner:
    def __init__(self, rpms: Mapping[str, str] = RPMS, *, fail_clone: bool = False) -> None:
        self.fail_clone = fail_clone
        self.commands: list[list[str]] = []
        self.rpms = rpms
        self.meta_at_make = ""

    def __call__(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        log: Path | None = None,
        stdin: str | None = None,
    ) -> str:
        argv = list(argv)
        self.commands.append(argv)
        if argv[:2] == ["git", "clone"]:
            if self.fail_clone:
                msg = "clone failed"
                raise BuildError(msg)
            dest = Path(argv[-1])
            (dest / ".git").mkdir(parents=True, exist_ok=True)
            (dest / ".git" / "HEAD").write_text("ref")
            (dest / "META").write_text(META)
        elif argv[:2] == ["git", "-C"] and "--verify" in argv:
            if not (Path(argv[2]) / ".git" / "HEAD").is_file():
                msg = "fatal: Needed a single revision"
                raise BuildError(msg)
            return "0123abcd\n"
        elif argv[:2] == ["git", "rev-parse"]:
            return "0123abcd\n"
        elif argv[0] == "make":
            assert cwd is not None
            self.meta_at_make = (cwd / "META").read_text()
            for name in self.rpms:
                (cwd / name).write_text("rpm")
        elif argv[:3] == ["rpm", "-qp", "--qf"]:
            return self.rpms[Path(argv[-1]).name]
        elif argv[:2] == ["sh", "-ec"] and "rpm2cpio" in argv[2]:
            assert cwd is not None
            moddir = cwd / "lib" / "modules" / UNAME / "extra" / "zfs"
            moddir.mkdir(parents=True)
            (moddir / "zfs.ko").write_text("ko")
        return ""


def paths(tmp_path: Path) -> ZfsPaths:
    return ZfsPaths(
        src_root=tmp_path / "src",
        cache=tmp_path / "cache",
        workdir=tmp_path / "work",
        outdir=tmp_path / "out",
        logs=tmp_path / "logs",
    )


def test_build_zfs_command_sequence_and_outputs(tmp_path: Path) -> None:
    runner = FakeRunner()
    result = build_zfs(PLAN, paths(tmp_path), runner=runner)
    programs = [c[0] if c[0] != "git" else f"git {c[1]}" for c in runner.commands]
    assert programs[:3] == ["git clone", "git rev-parse", "sh"]
    clone = runner.commands[0]
    assert clone[2:6] == ["--quiet", "--depth", "1", "--branch"]
    assert "zfs-2.3.9" in clone
    assert ["sh", "autogen.sh"] in runner.commands
    configure = next(c for c in runner.commands if c[0] == "./configure")
    assert f"--with-linux=/usr/src/kernels/{UNAME}" in configure
    builddep = next(c for c in runner.commands if c[:2] == ["dnf", "builddep"])
    assert builddep == [
        "dnf",
        "builddep",
        "-y",
        "--enablerepo=crb",
        "--enablerepo=epel",
        "rpm/generic/zfs.spec",
    ]
    assert runner.commands.index(configure) < runner.commands.index(builddep)
    make = next(c for c in runner.commands if c[0] == "make")
    assert runner.commands.index(builddep) < runner.commands.index(make)
    assert make[-2:] == ["rpm-utils", "rpm-kmod"]
    assert "Version:       2.3.9\n" in runner.meta_at_make
    assert "Release:       0\n" in runner.meta_at_make
    assert "Linux-Maximum: 5.14\n" in runner.meta_at_make
    assert result.commit == "0123abcd"
    assert (result.modules_root / "lib" / "modules" / UNAME / "extra" / "zfs" / "zfs.ko").is_file()
    outputs = sorted(p.name for p in (tmp_path / "out").iterdir())
    assert outputs == sorted(n for n in RPMS if n != f"kmod-zfs-{UNAME}-2.3.9-0.el9.x86_64.rpm")
    assert not (tmp_path / "work" / "zfs" / ".git").exists()
    assert (tmp_path / "cache" / "zfs-src" / "zfs-2.3.9" / ".git").is_dir()


def test_cached_clone_is_reused(tmp_path: Path) -> None:
    build_zfs(PLAN, paths(tmp_path), runner=FakeRunner())
    runner = FakeRunner()
    build_zfs(PLAN, paths(tmp_path), runner=runner)
    assert not [c for c in runner.commands if c[:2] == ["git", "clone"]]


def test_cherry_picks_use_full_clone_and_patches_apply(tmp_path: Path) -> None:
    (tmp_path / "src" / "patches").mkdir(parents=True)
    (tmp_path / "src" / "patches" / "fix.patch").write_text("diff")
    plan = PLAN.model_copy(
        update={
            "zfs": PLAN.zfs.model_copy(
                update={"cherry_picks": ("abc123",), "patches": ("patches/fix.patch",)}
            )
        }
    )
    runner = FakeRunner()
    build_zfs(plan, paths(tmp_path), runner=runner)
    clone = runner.commands[0]
    assert "--depth" not in clone
    assert Path(clone[-1]).name.startswith("zfs-2.3.9-full.tmp-")
    assert (tmp_path / "cache" / "zfs-src" / "zfs-2.3.9-full" / ".git").is_dir()
    picks = [c for c in runner.commands if "cherry-pick" in c]
    assert picks
    assert picks[0][-1] == "abc123"
    applies = [c for c in runner.commands if c[:2] == ["git", "apply"]]
    assert applies == [["git", "apply", str(tmp_path / "src" / "patches" / "fix.patch")]]


def test_missing_kmod_package_fails(tmp_path: Path) -> None:
    rpms = {k: v for k, v in RPMS.items() if v != f"kmod-zfs-{UNAME}"}
    with pytest.raises(BuildError, match=f"kmod-zfs-{UNAME}"):
        build_zfs(PLAN, paths(tmp_path), runner=FakeRunner(rpms))


def test_failed_clone_leaves_no_cache_entry(tmp_path: Path) -> None:
    with pytest.raises(BuildError, match="clone failed"):
        build_zfs(PLAN, paths(tmp_path), runner=FakeRunner(fail_clone=True))
    assert list((tmp_path / "cache" / "zfs-src").iterdir()) == []


def test_incomplete_cached_clone_is_replaced(tmp_path: Path) -> None:
    broken = tmp_path / "cache" / "zfs-src" / "zfs-2.3.9"
    (broken / ".git").mkdir(parents=True)
    runner = FakeRunner()
    build_zfs(PLAN, paths(tmp_path), runner=runner)
    assert [c for c in runner.commands if c[:2] == ["git", "clone"]]
    assert (broken / ".git" / "HEAD").is_file()


def test_valid_cached_clone_is_checked_and_reused(tmp_path: Path) -> None:
    build_zfs(PLAN, paths(tmp_path), runner=FakeRunner())
    runner = FakeRunner()
    build_zfs(PLAN, paths(tmp_path), runner=runner)
    dest = str(tmp_path / "cache" / "zfs-src" / "zfs-2.3.9")
    assert runner.commands[0] == ["git", "-C", dest, "rev-parse", "--verify", "--quiet", "HEAD"]


def test_clone_finished_by_a_concurrent_build_is_kept(tmp_path: Path) -> None:
    dest = tmp_path / "cache" / "zfs-src" / "zfs-2.3.9"

    class RacingRunner(FakeRunner):
        def __call__(
            self,
            argv: Sequence[str],
            *,
            cwd: Path | None = None,
            env: Mapping[str, str] | None = None,
            log: Path | None = None,
            stdin: str | None = None,
        ) -> str:
            result = super().__call__(argv, cwd=cwd, env=env, log=log, stdin=stdin)
            if list(argv[:2]) == ["git", "clone"]:
                (dest / ".git").mkdir(parents=True)
                (dest / ".git" / "HEAD").write_text("other build")
                (dest / "META").write_text(META)
            return result

    build_zfs(PLAN, paths(tmp_path), runner=RacingRunner())
    assert (dest / ".git" / "HEAD").read_text() == "other build"
    assert [p.name for p in dest.parent.iterdir()] == ["zfs-2.3.9"]
