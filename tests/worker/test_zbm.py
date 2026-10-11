from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest
import yaml

from tests.project import FIXTURE_ROOT
from zefosator.config import load_boot_config, load_plan
from zefosator.errors import BuildError
from zefosator.worker.zbm import ZbmPaths, build_zbm

PLAN = load_plan(FIXTURE_ROOT / "targets" / "fedora-44-x86_64.yaml")
CONFIG = load_boot_config(FIXTURE_ROOT)
UNAME = PLAN.uname_r
COMMIT = "7174e420590a270ece3c6426bdb1d5cdd9ef27b0"


class FakeRunner:
    def __init__(self, *, zfs_in_initramfs: bool = True) -> None:
        self.commands: list[list[str]] = []
        self.envs: list[Mapping[str, str] | None] = []
        self.zfs_in_initramfs = zfs_in_initramfs
        self.configs: list[dict[str, object]] = []

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
        self.envs.append(env)
        if argv[:2] == ["git", "clone"]:
            dest = Path(argv[-1])
            (dest / "etc/zfsbootmenu/dracut.conf.d").mkdir(parents=True)
            (dest / "etc/zfsbootmenu/dracut.conf.d/zfsbootmenu.conf").write_text("x\n")
            (dest / "bin").mkdir()
            (dest / "bin/zbm-kcl").write_text("kcl")
        elif argv[:2] == ["git", "-C"] and "rev-parse" in argv:
            return COMMIT + "\n"
        elif argv[0] == "generate-zbm":
            config = yaml.safe_load(Path(argv[-1]).read_text())
            self.configs.append(config)
            image_dir = Path(config["Components"]["ImageDir"])
            image_dir.mkdir(parents=True, exist_ok=True)
            (image_dir / "vmlinuz-bootmenu").write_text("k")
            (image_dir / "initramfs-bootmenu.img").write_text("i")
        elif argv[0] in {"ukify", "sbsign"}:
            out = next(a.split("=", 1)[1] for a in argv if a.startswith("--output"))
            Path(out).write_text("efi")
        elif argv[0] == "lsinitrd":
            listing = "usr/bin/efibootmgr\n"
            if self.zfs_in_initramfs:
                listing += f"usr/lib/modules/{UNAME}/extra/zfs/zfs.ko.xz\n"
            return listing
        elif argv[0] == "modinfo":
            return "Zefosa TEST CA\n"
        return ""


def paths(tmp_path: Path) -> ZbmPaths:
    kernel, zfs = tmp_path / "output/kernel", tmp_path / "output/zfs"
    kernel.mkdir(parents=True)
    zfs.mkdir(parents=True)
    for name in ("kernel-core", "kernel-modules-core", "kernel-modules", "kernel-devel"):
        (kernel / f"{name}-7.2.9-200.zefosa.fc44.x86_64.rpm").write_text("rpm")
    for name in ("zfs-2.4.4-0.fc44.x86_64", "zfs-dracut-2.4.4-0.fc44.noarch",
                 "libzfs7-2.4.4-0.fc44.x86_64", "libzfs7-devel-2.4.4-0.fc44.x86_64",
                 "zfs-debuginfo-2.4.4-0.fc44.x86_64"):  # fmt: skip
        (zfs / f"{name}.rpm").write_text("rpm")
    return ZbmPaths(
        cache=tmp_path / "cache",
        workdir=tmp_path / "work",
        outdir=tmp_path / "out",
        logs=tmp_path / "logs",
        kernel_rpms=kernel,
        zfs_rpms=zfs,
    )


def fake_rpmbuild(spec_text: str, dirs: object, arch: str) -> None:
    del spec_text, arch
    out = dirs.outdir  # type: ignore[attr-defined] # BuildDirs
    out.mkdir(parents=True, exist_ok=True)


def run_build(tmp_path: Path, runner: FakeRunner, monkeypatch: pytest.MonkeyPatch) -> object:
    monkeypatch.setattr("zefosator.worker.zbm.rpmbuild", fake_rpmbuild)
    monkeypatch.setattr("zefosator.worker.zbm.which", lambda name: f"/usr/bin/{name}")
    key, cert = tmp_path / "k", tmp_path / "c"
    return build_zbm(PLAN, CONFIG, paths(tmp_path), (key, cert), runner=runner)


def test_command_sequence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner = FakeRunner()
    result = run_build(tmp_path, runner, monkeypatch)
    programs = [c[0] for c in runner.commands]
    assert programs[:2] == ["dnf", "dnf"]
    install = runner.commands[1]
    assert "--setopt=tsflags=noscripts" in install
    names = [Path(a).name for a in install if a.endswith(".rpm")]
    assert "kernel-devel-7.2.9-200.zefosa.fc44.x86_64.rpm" not in names
    assert "zfs-debuginfo-2.4.4-0.fc44.x86_64.rpm" not in names
    assert "libzfs7-devel-2.4.4-0.fc44.x86_64.rpm" not in names
    assert "zfs-dracut-2.4.4-0.fc44.noarch.rpm" in names
    assert ["depmod", "-a", UNAME] in runner.commands
    assert programs.count("generate-zbm") == 2
    ukify = [c for c in runner.commands if c[0] == "ukify"]
    assert len(ukify) == 2
    assert not [a for c in ukify for a in c if a.startswith("--cmdline")]
    assert len([c for c in runner.commands if c[0] == "sbsign"]) == 2
    assert len([c for c in runner.commands if c[0] == "sbverify"]) == 2
    gen_env = next(
        e for c, e in zip(runner.commands, runner.envs, strict=True) if c[0] == "generate-zbm"
    )
    assert gen_env is not None
    assert gen_env["DRACUT_NO_XATTR"] == "1"
    assert result.checkout.joinpath("bin/zbm-kcl").is_file()  # type: ignore[attr-defined] # ZbmResult
    assert result.commit == COMMIT  # type: ignore[attr-defined] # ZbmResult
    git = [c for c in runner.commands if c[0] == "git"]
    assert [c[3:] for c in git[1:]] == [
        ["checkout", "--quiet", "v3.1.0"],
        ["rev-parse", "HEAD"],
        ["rev-parse", "v3.1.0^{commit}"],
    ]


def test_dracut_config_forces_zfs_modules_and_recovery_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = FakeRunner()
    run_build(tmp_path, runner, monkeypatch)
    regular, recovery = runner.configs
    regular_conf = Path(str(regular["Global"]["DracutConfDir"]))  # type: ignore[index] # yaml dict
    recovery_conf = Path(str(recovery["Global"]["DracutConfDir"]))  # type: ignore[index] # yaml dict
    assert 'add_drivers+=" zfs spl "' in (regular_conf / "zefosa.conf").read_text()
    assert (regular_conf / "zfsbootmenu.conf").is_file()
    assert not (regular_conf / "zefosa-recovery.conf").exists()
    tools = (recovery_conf / "zefosa-recovery.conf").read_text()
    assert "/usr/bin/efibootmgr" in tools
    assert regular["Kernel"]["Path"] == f"/lib/modules/{UNAME}/vmlinuz"  # type: ignore[index] # yaml dict
    assert regular["EFI"]["Enabled"] is False  # type: ignore[index] # yaml dict
    # generate-zbm mounts BootMountPoint unless it already is a mount point
    assert regular["Global"]["BootMountPoint"] == "/"  # type: ignore[index] # yaml dict


def test_image_without_zfs_modules_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(BuildError, match=r"zfs\.ko"):
        run_build(tmp_path, FakeRunner(zfs_in_initramfs=False), monkeypatch)


def test_wrong_checkout_commit_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner = FakeRunner()
    original = runner.__call__

    def wrong(argv: Sequence[str], **kwargs: object) -> str:
        if list(argv[:2]) == ["git", "-C"] and argv[-1] == "HEAD":
            return "0" * 40 + "\n"
        return original(argv, **kwargs)  # type: ignore[arg-type] # passthrough of keyword args

    monkeypatch.setattr("zefosator.worker.zbm.rpmbuild", fake_rpmbuild)
    monkeypatch.setattr("zefosator.worker.zbm.which", lambda name: f"/usr/bin/{name}")
    with pytest.raises(BuildError, match=rf"at commit 0{{40}}, but v3\.1\.0 is {COMMIT}"):
        build_zbm(PLAN, CONFIG, paths(tmp_path), (tmp_path / "k", tmp_path / "c"), runner=wrong)
