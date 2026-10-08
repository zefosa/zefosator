"""The `build` command inside the target container: fetch, ZFS, repackage, manifest."""

import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from zefosator.config import Plan
from zefosator.worker.fetch import fetch_all
from zefosator.worker.manifest import write_manifest
from zefosator.worker.proc import run
from zefosator.worker.repackage import PackageInput, RepackContext, RepackPaths, repackage
from zefosator.worker.zfs import ZfsPaths, build_zfs

DAY = 86400


@dataclass(frozen=True)
class WorkerDirs:
    src: Path  # zefosator source tree (read-only)
    cache: Path  # persistent cache
    output: Path  # output/<target_id>


def _banner(step: int, text: str) -> None:
    print(f"==> [{step}/5] {text}", flush=True)  # noqa: T201 -- progress for the person running the build


def _empty(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    for child in path.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()


def run_build(plan: Plan, dirs: WorkerDirs) -> None:
    workdir = dirs.cache / "build" / plan.target_id
    logs = workdir / "logs"
    shutil.rmtree(workdir, ignore_errors=True)
    logs.mkdir(parents=True)
    _empty(dirs.output)

    _banner(1, f"Fetch upstream kernel packages {plan.kernel_version}-{plan.upstream_release}")
    rpms = fetch_all(plan, dirs.cache / "downloads")

    _banner(2, "Install kernel-devel")
    run(["dnf", "install", "-y", str(rpms[plan.role("devel").name])], log=logs / "dnf.log")

    _banner(3, f"Build OpenZFS {plan.zfs.version} ({plan.zfs.tag})")
    zfs = build_zfs(
        plan,
        ZfsPaths(
            src_root=dirs.src,
            cache=dirs.cache,
            workdir=workdir,
            outdir=dirs.output / "zfs",
            logs=logs,
        ),
    )

    _banner(4, f"Repackage kernel as {plan.kernel_version}-{plan.zefosa_release}")
    outputs = repackage(
        [PackageInput(name=p.name, role=p.role, rpm=rpms[p.name]) for p in plan.packages],
        RepackContext(
            version=plan.kernel_version,
            upstream_release=plan.upstream_release,
            zefosa_release=plan.zefosa_release,
            uname_r=plan.uname_r,
            arch=plan.arch,
            zfs_version=plan.zfs.version,
            zfs_tag=plan.zfs.tag,
            changelog_time=int(time.time()) // DAY * DAY + DAY // 2,
        ),
        RepackPaths(
            kmod_root=zfs.modules_root,
            workdir=workdir / "repackage",
            outdir=dirs.output / "kernel",
            logs=logs,
        ),
    )

    _banner(5, "Write manifest")
    zfs_outputs = sorted((dirs.output / "zfs").glob("*.rpm"))
    write_manifest(dirs.output / "manifest.json", plan, rpms, zfs.commit, [*outputs, *zfs_outputs])
