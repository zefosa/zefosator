"""Build OpenZFS userspace and kmod RPMs against the target's kernel-devel."""

import os
import re
import shlex
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from zefosator.config import Plan
from zefosator.errors import BuildError
from zefosator.worker.proc import run

GIT_IDENTITY = (
    "-c",
    "user.email=zefosator@build.system",
    "-c",
    "user.name=Zefosator Build System",
)


class Runner(Protocol):
    def __call__(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        log: Path | None = None,
        stdin: str | None = None,
    ) -> str: ...


@dataclass(frozen=True)
class ZfsPaths:
    src_root: Path  # zefosator source tree (patches are relative to it)
    cache: Path  # persistent: git clones
    workdir: Path  # wiped per build
    outdir: Path  # ZFS RPMs for distribution
    logs: Path


@dataclass(frozen=True)
class ZfsResult:
    modules_root: Path  # extracted kmod-zfs payload
    commit: str  # commit of zfs.tag before cherry-picks and patches


def _fresh(path: Path) -> Path:
    shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True)
    return path


def _set_meta(meta: Path, values: Mapping[str, str]) -> None:
    text = meta.read_text()
    for key, value in values.items():
        pattern = re.compile(rf"^{re.escape(key)}:.*$", re.MULTILINE)
        if not pattern.search(text):
            msg = f"{meta}: no {key} line to set"
            raise BuildError(msg)
        text = pattern.sub(f"{key}:".ljust(15) + value, text)
    meta.write_text(text)


def _clone(plan: Plan, cache: Path, runner: Runner, log: Path) -> Path:
    zfs = plan.zfs
    full = bool(zfs.cherry_picks)
    dest = cache / "zfs-src" / (zfs.tag + ("-full" if full else ""))
    if dest.is_dir():
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    depth = () if full else ("--depth", "1")
    try:
        runner(
            ["git", "clone", "--quiet", *depth, "--branch", zfs.tag, zfs.repo, str(dest)], log=log
        )
    except BuildError:
        shutil.rmtree(dest, ignore_errors=True)
        raise
    return dest


def build_zfs(plan: Plan, paths: ZfsPaths, *, runner: Runner = run) -> ZfsResult:
    log = paths.logs / "zfs.log"
    cached = _clone(plan, paths.cache, runner, log)
    src = paths.workdir / "zfs"
    shutil.rmtree(src, ignore_errors=True)
    shutil.copytree(cached, src, symlinks=True)
    commit = runner(["git", "rev-parse", "HEAD"], cwd=src).strip()
    for pick in plan.zfs.cherry_picks:
        runner(["git", *GIT_IDENTITY, "cherry-pick", pick], cwd=src, log=log)
    for patch in plan.zfs.patches:
        runner(["git", "apply", str(paths.src_root / patch)], cwd=src, log=log)
    # Without .git, OpenZFS takes its version from META instead of `git describe`.
    shutil.rmtree(src / ".git")
    major, minor = plan.kernel_version.split(".")[:2]
    _set_meta(
        src / "META",
        {
            "Version": plan.zfs.version,
            "Release": str(plan.zfs.release),
            "Linux-Maximum": f"{major}.{minor}",
        },
    )
    kernel_dir = f"/usr/src/kernels/{plan.uname_r}"
    runner(["sh", "autogen.sh"], cwd=src, log=log)
    runner(
        ["./configure", f"--with-linux={kernel_dir}", f"--with-linux-obj={kernel_dir}"],
        cwd=src,
        log=log,
    )
    runner(["make", "-s", f"-j{os.cpu_count() or 2}", "rpm-utils", "rpm-kmod"], cwd=src, log=log)

    kmod_name = f"kmod-zfs-{plan.uname_r}"
    kmods = []
    paths.outdir.mkdir(parents=True, exist_ok=True)
    for rpm in sorted(src.glob("*.rpm")):
        name = runner(["rpm", "-qp", "--qf", "%{NAME}", str(rpm)]).strip()
        if name == kmod_name and not rpm.name.endswith(".src.rpm"):
            kmods.append(rpm)
        else:
            shutil.copy2(rpm, paths.outdir / rpm.name)
    if len(kmods) != 1:
        msg = f"expected one {kmod_name} binary RPM from the OpenZFS build, found {len(kmods)}"
        raise BuildError(msg)
    modules_root = _fresh(paths.workdir / "kmod-root")
    command = f"rpm2cpio {shlex.quote(str(kmods[0]))} | cpio -idm --quiet"
    runner(["sh", "-ec", command], cwd=modules_root, log=log)
    return ZfsResult(modules_root=modules_root, commit=commit)
