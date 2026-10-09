"""The `boot` command inside the target container: shim for every target, and on the target
that builds common packages (Fedora) also ZFSBootMenu, rEFInd, the CA and zefosa-boot."""

import json
import shutil
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from zefosator.config import BootConfig, Plan
from zefosator.errors import BuildError
from zefosator.worker.bootpkgs import (
    BootBuild,
    build_ca_package,
    build_refind_package,
    build_tools_package,
)
from zefosator.worker.fetch import fetch_file, import_keys, rpm_verifier, sha256_verifier
from zefosator.worker.manifest import rpm_nevra, sha256
from zefosator.worker.shim import ShimPaths, build_shim
from zefosator.worker.zbm import ZbmPaths, build_zbm

SECRET_KEY = Path("/run/secrets/zefosa-signing-key")
SIGNING_CERT = Path("/signing/cert.pem")
CA_CERT = Path("/signing/ca.der")


class BootJob(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    plan: Plan
    config: BootConfig


@dataclass(frozen=True)
class BootDirs:
    src: Path  # zefosator source tree (read-only)
    cache: Path
    output: Path  # output/<target-id>: kernel/, zfs/ in; boot/ out
    common_output: Path  # output/boot: distro-independent packages


def _banner(text: str) -> None:
    print(f"==> {text}", flush=True)  # noqa: T201 -- progress for the person running the build


def _empty(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    for child in path.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()


def _manifest(path: Path, job: BootJob, inputs: dict[str, str], outputs: list[Path]) -> None:
    data = {
        "target": job.plan.target_id,
        "kernel": f"{job.plan.kernel_version}-{job.plan.zefosa_release}",
        "signing": job.plan.signing.model_dump() if job.plan.signing else None,
        "inputs": inputs,
        "outputs": [
            {"path": out.name, "nevra": rpm_nevra(out), "sha256": sha256(out)} for out in outputs
        ],
    }
    path.write_text(json.dumps(data, indent=2) + "\n")


def _require(paths: tuple[tuple[Path, str], ...]) -> None:
    for path, what in paths:
        if not path.exists():
            msg = f"{what} {path} is not available"
            raise BuildError(msg)


def run_boot(job: BootJob, dirs: BootDirs) -> None:
    plan, config = job.plan, job.config
    if plan.boot is None:
        msg = f"{plan.target_id}: target has no boot section"
        raise BuildError(msg)
    workdir = dirs.cache / "build" / f"{plan.target_id}-boot"
    logs = workdir / "logs"
    shutil.rmtree(workdir, ignore_errors=True)
    logs.mkdir(parents=True)
    shim_out = dirs.output / "boot"
    _empty(shim_out)
    downloads = dirs.cache / "downloads"

    _banner(f"Repackage shim {plan.boot.shim_version}-{plan.boot.shim_release} without GRUB")
    import_keys(plan.gpg_keys)
    verify = rpm_verifier(require_signature=bool(plan.gpg_keys))
    vendor_shim = fetch_file(plan.boot.shim_url, downloads / plan.boot.shim_file, verify)
    shim = build_shim(plan, vendor_shim, ShimPaths(workdir / "shim", shim_out, logs))
    _manifest(shim_out / "manifest.json", job, {plan.boot.shim_file: plan.boot.shim_url}, [shim])
    if not plan.boot.common:
        return

    _require(
        (
            (SECRET_KEY, "signing key"),
            (SIGNING_CERT, "signing certificate"),
            (CA_CERT, "CA certificate"),
        )
    )
    _require(((dirs.output / "kernel", "kernel output (run `zefosator build` first):"),))
    _empty(dirs.common_output)
    sign = (SECRET_KEY, SIGNING_CERT)
    refind_file = config.refind.url.rsplit("/", 1)[1]
    refind = fetch_file(
        config.refind.url, downloads / refind_file, sha256_verifier(config.refind.sha256)
    )
    _banner(f"Build ZFSBootMenu {config.zfsbootmenu.version} images")
    zbm = build_zbm(
        plan,
        config,
        ZbmPaths(
            cache=dirs.cache,
            workdir=workdir / "zbm",
            outdir=dirs.common_output,
            logs=logs,
            kernel_rpms=dirs.output / "kernel",
            zfs_rpms=dirs.output / "zfs",
        ),
        sign,
    )
    build = BootBuild(workdir=workdir / "packages", outdir=dirs.common_output, logs=logs)
    _banner("Build zefosa-secureboot-ca, zefosa-refind and zefosa-boot")
    outputs = [
        zbm.regular,
        zbm.recovery,
        build_ca_package(config, CA_CERT, build),
        build_refind_package(config, refind, sign, build),
        build_tools_package(config, dirs.src, zbm.checkout, build),
    ]
    inputs = {refind_file: config.refind.url, "zfsbootmenu": config.zfsbootmenu.commit}
    _manifest(dirs.common_output / "manifest.json", job, inputs, outputs)
