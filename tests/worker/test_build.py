from pathlib import Path

import pytest

from tests.project import FIXTURE_ROOT
from zefosator.config import SigningInfo, load_plan
from zefosator.errors import BuildError
from zefosator.worker import build
from zefosator.worker.repackage import PackageInput, RepackContext, RepackPaths
from zefosator.worker.repro import changelog_time
from zefosator.worker.zfs import ZfsPaths, ZfsResult

PLAN = load_plan(FIXTURE_ROOT / "targets" / "almalinux-9-x86_64.yaml")


@pytest.mark.parametrize("signed", [False, True])
def test_run_build_runs_stages_in_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, signed: bool
) -> None:
    calls: list[str] = []
    plan = PLAN
    expected_sign = None
    if signed:
        plan = PLAN.model_copy(
            update={"signing": SigningInfo(cert_sha256="ab" * 32, cert_subject="CN=x")}
        )
        secret = tmp_path / "secret.key"
        cert = tmp_path / "cert.pem"
        secret.write_text("key")
        cert.write_text("cert")
        monkeypatch.setattr(build, "SECRET_KEY", secret)
        monkeypatch.setattr(build, "SIGNING_CERT", cert)
        expected_sign = (secret, cert)
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
        return ZfsResult(modules_root=tmp_path / "kmod", commit="c0ffee", source_date_epoch=100)

    def fake_repackage(
        packages: list[PackageInput], ctx: RepackContext, paths: RepackPaths
    ) -> list[Path]:
        calls.append(f"repackage {len(packages)} {ctx.zefosa_release} {paths.outdir}")
        assert packages[0].name == "kernel"
        assert ctx.source_date_epoch == 1791056615
        assert ctx.changelog_time == changelog_time(1791056615)
        assert ctx.sign == expected_sign
        return []

    def fake_manifest(path: Path, *args: object, **kwargs: object) -> None:
        calls.append(f"manifest {path}")
        assert kwargs["source_date_epochs"] == {"zfs": 100, "kernel": 1791056615}

    def fake_kernel_epoch(zfs_epoch: int, vendor_rpms: list[Path]) -> int:
        calls.append(f"kernel epoch {zfs_epoch} {len(vendor_rpms)}")
        assert sorted(vendor_rpms) == sorted(rpms.values())
        return 1791056615

    monkeypatch.setattr(build, "fetch_all", fake_fetch)
    monkeypatch.setattr(build, "run", fake_run)
    monkeypatch.setattr(build, "build_zfs", fake_zfs)
    monkeypatch.setattr(build, "repackage", fake_repackage)
    monkeypatch.setattr(build, "write_manifest", fake_manifest)
    monkeypatch.setattr(build, "kernel_epoch", fake_kernel_epoch)
    monkeypatch.setattr(build, "write_rpm_macros", lambda home: calls.append("rpm macros"))

    build.run_build(plan, dirs)

    devel = rpms["kernel-devel"]
    assert calls == [
        "rpm macros",
        f"fetch {dirs.cache / 'downloads'}",
        f"dnf install -y --enablerepo=crb --enablerepo=epel {devel}",
        f"zfs {dirs.output / 'zfs'}",
        f"kernel epoch 100 {len(PLAN.packages)}",
        f"repackage {len(PLAN.packages)} 687.52.1.zefosa.el9_8 {dirs.output / 'kernel'}",
        f"manifest {dirs.output / 'manifest.json'}",
    ]
    assert not (dirs.output / "stale").exists()


def test_signed_plan_without_secret_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plan = PLAN.model_copy(
        update={"signing": SigningInfo(cert_sha256="ab" * 32, cert_subject="CN=x")}
    )
    monkeypatch.setattr(build, "SECRET_KEY", tmp_path / "absent")
    dirs = build.WorkerDirs(src=tmp_path, cache=tmp_path / "cache", output=tmp_path / "out")
    with pytest.raises(BuildError, match="signing key"):
        build.run_build(plan, dirs)
