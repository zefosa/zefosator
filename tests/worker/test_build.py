from pathlib import Path

import pytest

from zefosator.config import load_plan
from zefosator.worker import build
from zefosator.worker.repackage import PackageInput, RepackContext, RepackPaths
from zefosator.worker.zfs import ZfsPaths, ZfsResult

ROOT = Path(__file__).resolve().parents[2]
PLAN = load_plan(ROOT / "targets" / "almalinux-9-x86_64.yaml")


def test_run_build_runs_stages_in_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    dirs = build.WorkerDirs(
        src=tmp_path / "src", cache=tmp_path / "cache", output=tmp_path / "output"
    )
    (dirs.output / "stale").mkdir(parents=True)
    rpms = {p.name: tmp_path / p.file for p in PLAN.packages}

    def fake_fetch(plan: object, downloads: Path) -> dict[str, Path]:
        calls.append(f"fetch {downloads}")
        return rpms

    def fake_run(argv: list[str], **_: object) -> str:
        calls.append(" ".join(argv))
        return ""

    def fake_zfs(plan: object, paths: ZfsPaths) -> ZfsResult:
        calls.append(f"zfs {paths.outdir}")
        return ZfsResult(modules_root=tmp_path / "kmod", commit="c0ffee")

    def fake_repackage(
        packages: list[PackageInput], ctx: RepackContext, paths: RepackPaths
    ) -> list[Path]:
        calls.append(f"repackage {len(packages)} {ctx.zefosa_release} {paths.outdir}")
        assert packages[0].name == "kernel"
        assert ctx.changelog_time % 86400 == 43200
        return []

    def fake_manifest(path: Path, *args: object, **kwargs: object) -> None:
        calls.append(f"manifest {path}")

    monkeypatch.setattr(build, "fetch_all", fake_fetch)
    monkeypatch.setattr(build, "run", fake_run)
    monkeypatch.setattr(build, "build_zfs", fake_zfs)
    monkeypatch.setattr(build, "repackage", fake_repackage)
    monkeypatch.setattr(build, "write_manifest", fake_manifest)

    build.run_build(PLAN, dirs)

    devel = rpms["kernel-devel"]
    assert calls == [
        f"fetch {dirs.cache / 'downloads'}",
        f"dnf install -y {devel}",
        f"zfs {dirs.output / 'zfs'}",
        f"repackage {len(PLAN.packages)} 687.52.1.zefosa.el9_8 {dirs.output / 'kernel'}",
        f"manifest {dirs.output / 'manifest.json'}",
    ]
    assert not (dirs.output / "stale").exists()
