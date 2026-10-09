import json
from collections.abc import Callable
from pathlib import Path

import pytest

from zefosator.config import BootPlan, SigningInfo, load_boot_config, load_plan
from zefosator.errors import BuildError
from zefosator.worker import boot
from zefosator.worker.zbm import ZbmResult

ROOT = Path(__file__).resolve().parents[2]
CONFIG = load_boot_config(ROOT)
SIGNED = SigningInfo(cert_sha256="ab" * 32, cert_subject="CN=x")


def job_for(target: str) -> boot.BootJob:
    plan = load_plan(ROOT / "targets" / f"{target}.yaml").model_copy(update={"signing": SIGNED})
    assert plan.boot is not None
    return boot.BootJob(plan=plan, config=CONFIG)


def dirs(tmp_path: Path) -> boot.BootDirs:
    out = tmp_path / "output"
    (out / "boot").mkdir(parents=True)
    (out / "boot" / "stale.rpm").write_text("old")
    (out / "kernel").mkdir()
    (out / "zfs").mkdir()
    common = tmp_path / "boot-output"
    common.mkdir()
    (common / "stale.rpm").write_text("old")
    return boot.BootDirs(src=ROOT, cache=tmp_path / "cache", output=out, common_output=common)


@pytest.fixture
def fakes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []
    key, cert, ca = tmp_path / "key", tmp_path / "cert", tmp_path / "ca.der"
    for p in (key, cert, ca):
        p.write_text("x")
    monkeypatch.setattr(boot, "SECRET_KEY", key)
    monkeypatch.setattr(boot, "SIGNING_CERT", cert)
    monkeypatch.setattr(boot, "CA_CERT", ca)

    def rpm(outdir: Path, name: str) -> Path:
        outdir.mkdir(parents=True, exist_ok=True)
        (outdir / name).write_text(name)
        return outdir / name

    def fake_fetch(url: str, dest: Path, verify: object) -> Path:
        calls.append(f"fetch {dest.name}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text("rpm")
        return dest

    monkeypatch.setattr(boot, "fetch_file", fake_fetch)
    monkeypatch.setattr(boot, "import_keys", lambda keys: calls.append(f"keys {len(keys)}"))
    monkeypatch.setattr(boot, "rpm_verifier", lambda **_: lambda path: None)

    def recorder(label: str, filename: str, out_index: int) -> Callable[..., Path]:
        def fake(*args: object) -> Path:
            calls.append(label)
            target = args[out_index]
            outdir = target.outdir  # type: ignore[attr-defined] # ShimPaths / BootBuild
            return rpm(outdir, filename)

        return fake

    monkeypatch.setattr(boot, "build_shim", recorder("shim", "shim-x64.rpm", 2))

    def fake_zbm(plan: object, config: object, paths: object, sign: object) -> ZbmResult:
        calls.append(f"zbm {sign == (key, cert)}")
        out = paths.outdir  # type: ignore[attr-defined] # ZbmPaths
        return ZbmResult(
            rpm(out, "zefosa-zfsbootmenu.rpm"),
            rpm(out, "zefosa-zfsbootmenu-recovery.rpm"),
            tmp_path,
        )

    monkeypatch.setattr(boot, "build_zbm", fake_zbm)
    monkeypatch.setattr(boot, "build_ca_package", recorder("ca", "ca.rpm", 2))
    monkeypatch.setattr(boot, "build_refind_package", recorder("refind", "refind.rpm", 3))
    monkeypatch.setattr(boot, "build_tools_package", recorder("tools", "boot.rpm", 3))
    monkeypatch.setattr(boot, "rpm_nevra", lambda path: path.stem)
    return calls


def test_fedora_builds_shim_and_common_packages(tmp_path: Path, fakes: list[str]) -> None:
    d = dirs(tmp_path)
    boot.run_boot(job_for("fedora-44-x86_64"), d)
    assert fakes == [
        "keys 0",
        "fetch shim-x64-16.1-7.x86_64.rpm",
        "shim",
        "fetch rEFInd-unsigned-x64-0.14.2-5.fc44.noarch.rpm",
        "zbm True",
        "ca",
        "refind",
        "tools",
    ]
    assert not (d.output / "boot" / "stale.rpm").exists()
    assert not (d.common_output / "stale.rpm").exists()
    manifest = json.loads((d.common_output / "manifest.json").read_text())
    assert sorted(o["path"] for o in manifest["outputs"]) == [
        "boot.rpm",
        "ca.rpm",
        "refind.rpm",
        "zefosa-zfsbootmenu-recovery.rpm",
        "zefosa-zfsbootmenu.rpm",
    ]
    assert manifest["signing"]["cert_sha256"] == "ab" * 32
    shim_manifest = json.loads((d.output / "boot" / "manifest.json").read_text())
    assert shim_manifest["outputs"][0]["path"] == "shim-x64.rpm"


def test_almalinux_builds_only_shim(tmp_path: Path, fakes: list[str]) -> None:
    boot.run_boot(job_for("almalinux-9-x86_64"), dirs(tmp_path))
    assert fakes == ["keys 1", "fetch shim-x64-16.1-7.el9.alma.1.x86_64.rpm", "shim"]


def test_common_build_requires_signing_material(
    tmp_path: Path, fakes: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(boot, "CA_CERT", tmp_path / "missing.der")
    with pytest.raises(BuildError, match="CA certificate"):
        boot.run_boot(job_for("fedora-44-x86_64"), dirs(tmp_path))


def test_common_build_requires_kernel_output(tmp_path: Path, fakes: list[str]) -> None:
    d = dirs(tmp_path)
    (d.output / "kernel").rmdir()
    with pytest.raises(BuildError, match="zefosator build"):
        boot.run_boot(job_for("fedora-44-x86_64"), d)


def test_boot_plan_type_is_exported() -> None:
    assert BootPlan.__name__ == "BootPlan"
