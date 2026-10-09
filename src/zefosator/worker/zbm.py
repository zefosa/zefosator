"""ZFSBootMenu UKIs built with the zefosa Fedora kernel (spec sections 2 and 6.3).

The kernel inside the image locks down under Secure Boot, so it loads only signed modules and
verifies the kernel it kexecs: unlike the upstream ZBM release kernel, which enforces nothing.
"""

import hashlib
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

import yaml

from zefosator.config import BootConfig, Plan
from zefosator.errors import BuildError
from zefosator.worker.bootpkgs import ZEFOSA_ESP, BootBuild
from zefosator.worker.bootspec import BuildDirs, SpecPackage, render_boot_spec, rpmbuild
from zefosator.worker.proc import run
from zefosator.worker.rpmheader import Dependency, DepKind, FileEntry
from zefosator.worker.zfs import Runner

_RPM_EXCLUDE = re.compile(r"debuginfo|debugsource|-devel-|\.src\.rpm$|-test-")
_KERNEL_RPMS = re.compile(r"^kernel-(core|modules-core|modules)-\d")
_ZFS_RPMS = re.compile(r"^(zfs|zfs-dracut|libnvpair\d+|libuutil\d+|libzfs\d+|libzpool\d+)-\d")
FLAVORS = ("zfsbootmenu", "zfsbootmenu-recovery")


def which(name: str) -> str | None:
    return shutil.which(name)


@dataclass(frozen=True)
class ZbmPaths:
    cache: Path
    workdir: Path
    outdir: Path
    logs: Path
    kernel_rpms: Path  # output/<fedora target>/kernel
    zfs_rpms: Path  # output/<fedora target>/zfs


@dataclass(frozen=True)
class ZbmResult:
    regular: Path
    recovery: Path
    checkout: Path  # ZBM source at the pinned commit (zbm-kcl, man pages, license)


@dataclass(frozen=True)
class _Job:
    plan: Plan
    runner: Runner
    log: Path
    workdir: Path
    sign: tuple[Path, Path]


def _select(directory: Path, pattern: re.Pattern[str]) -> list[Path]:
    return sorted(
        p
        for p in directory.glob("*.rpm")
        if pattern.match(p.name) and not _RPM_EXCLUDE.search(p.name)
    )


def _checkout(config: BootConfig, cache: Path, runner: Runner, log: Path) -> Path:
    pin = config.zfsbootmenu
    dest = cache / "zbm-src" / pin.commit
    if not dest.is_dir():
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = Path(tempfile.mkdtemp(dir=dest.parent, prefix=f"{pin.commit}.tmp-"))
        try:
            runner(["git", "clone", "--quiet", pin.repo, str(tmp)], log=log)
            runner(["git", "-C", str(tmp), "checkout", "--quiet", pin.commit], log=log)
            tmp.rename(dest)
        except (BuildError, OSError):
            shutil.rmtree(tmp, ignore_errors=True)
            if not dest.is_dir():
                raise
    head = runner(["git", "-C", str(dest), "rev-parse", "HEAD"]).strip()
    if head != pin.commit:
        msg = f"ZFSBootMenu checkout {dest} is at commit {head}, expected {pin.commit}"
        raise BuildError(msg)
    return dest


def _conf_dir(checkout: Path, dest: Path, extra: dict[str, str]) -> Path:
    shutil.rmtree(dest, ignore_errors=True)
    shutil.copytree(checkout / "etc/zfsbootmenu/dracut.conf.d", dest)
    # dracut >= 108 runs ZBM's installkernel() before install() defines its module list, so ZBM
    # silently adds no ZFS modules; name them explicitly.
    (dest / "zefosa.conf").write_text('add_drivers+=" zfs spl "\n')
    for name, text in extra.items():
        (dest / name).write_text(text)
    return dest


def _recovery_conf(config: BootConfig) -> str:
    paths = []
    missing = []
    for tool in config.zfsbootmenu.recovery_tools:
        found = which(tool)
        if found is None:
            missing.append(tool)
        else:
            paths.append(found)
    if missing:
        msg = f"recovery tools not found after installing recovery packages: {', '.join(missing)}"
        raise BuildError(msg)
    return f'install_items+=" {" ".join(paths)} "\n'


def _generate(job: _Job, flavor: str, conf_dir: Path) -> Path:
    plan = job.plan
    image_dir = job.workdir / flavor
    shutil.rmtree(image_dir, ignore_errors=True)
    kernel = {"Path": f"/lib/modules/{plan.uname_r}/vmlinuz", "Version": plan.uname_r}
    config = {
        "Global": {
            "ManageImages": True,
            # generate-zbm mounts BootMountPoint unless it is a mount point already; "/" always is
            "BootMountPoint": "/",
            "DracutConfDir": str(conf_dir),
        },
        "Components": {"ImageDir": str(image_dir), "Versions": False, "Enabled": True},
        "EFI": {"Enabled": False},
        "Kernel": {**kernel, "CommandLine": ""},
    }
    config_file = job.workdir / f"{flavor}.yaml"
    config_file.write_text(yaml.safe_dump(config))
    env = {**os.environ, "DRACUT_NO_XATTR": "1"}  # rootless containers cannot copy security xattrs
    job.runner(["generate-zbm", "--config", str(config_file)], env=env, log=job.log)
    return image_dir


def _uki(job: _Job, flavor: str, image_dir: Path) -> Path:
    initrd = image_dir / "initramfs-bootmenu.img"
    listing = job.runner(["lsinitrd", str(initrd)])
    if f"/{job.plan.uname_r}/extra/zfs/zfs.ko" not in listing:
        msg = f"{flavor}: ZFS kernel modules (extra/zfs/zfs.ko) missing from the ZBM initramfs"
        raise BuildError(msg)
    unsigned, signed = image_dir / f"{flavor}-unsigned.efi", image_dir / f"{flavor}.efi"
    # No --cmdline: rEFInd passes the ZBM options and systemd-stub accepts them.
    job.runner(
        [
            "ukify", "build", f"--linux={image_dir / 'vmlinuz-bootmenu'}", f"--initrd={initrd}",
            f"--uname={job.plan.uname_r}", '--os-release=NAME="ZFSBootMenu (zefosa)"',
            f"--output={unsigned}",
        ],
        log=job.log,
    )  # fmt: skip
    key, cert = job.sign
    sbsign = ["sbsign", "--key", str(key), "--cert", str(cert), f"--output={signed}"]
    job.runner([*sbsign, str(unsigned)], log=job.log)
    job.runner(["sbverify", "--cert", str(cert), str(signed)], log=job.log)
    return signed


def _package(plan: Plan, config: BootConfig, flavor: str, efi: Path, build: BootBuild) -> Path:
    pin = config.zfsbootmenu
    workdir = build.workdir / f"pkg-{flavor}"
    shutil.rmtree(workdir, ignore_errors=True)
    root = workdir / "root"
    target = root / ZEFOSA_ESP.lstrip("/") / f"{flavor}.efi"
    target.parent.mkdir(parents=True)
    shutil.copy2(efi, target)
    target.chmod(0o700)
    data = target.read_bytes()
    entry = FileEntry(
        path=f"{ZEFOSA_ESP}/{flavor}.efi", mode=0o100700, user="root", group="root", size=len(data),
        digest=hashlib.sha256(data).hexdigest(), linkto="", flags=0, verifyflags=0xFFFFFFFF,
    )  # fmt: skip
    kernel = f"{plan.kernel_version}-{plan.zefosa_release}"
    package = SpecPackage(
        name=f"zefosa-{flavor}",
        version=pin.version,
        release=str(pin.release),
        summary=f"ZFSBootMenu {'recovery ' if 'recovery' in flavor else ''}image signed for zefosa",
        license="MIT",
        url="https://zfsbootmenu.org",
        description=(
            f"ZFSBootMenu {pin.version} (commit {pin.commit}) as a signed UKI built with the "
            f"zefosa kernel {kernel}: it loads only signed modules and kexecs only verified "
            "kernels under Secure Boot. Options come from rEFInd (no embedded command line)."
        ),
        arch="x86_64",
        deps={DepKind.REQUIRE: [Dependency("efi-filesystem", 0, "")]},
        files=[entry],
        changelog=f"- ZFSBootMenu {pin.version} with zefosa kernel {kernel}",
    )
    rpmbuild(
        render_boot_spec(package),
        BuildDirs(root, workdir, build.outdir, build.logs / f"{flavor}-rpm.log"),
        "x86_64",
    )
    return build.outdir / f"zefosa-{flavor}-{pin.version}-{pin.release}.x86_64.rpm"


def _install_inputs(
    plan: Plan, config: BootConfig, paths: ZbmPaths, runner: Runner, log: Path
) -> None:
    pin = config.zfsbootmenu
    runner(
        [
            "dnf",
            "install",
            "-y",
            "--setopt=install_weak_deps=False",
            *pin.build_packages,
            *pin.recovery_packages,
        ],
        log=log,
    )
    rpms: list[Path] = [
        *_select(paths.kernel_rpms, _KERNEL_RPMS),
        *_select(paths.zfs_rpms, _ZFS_RPMS),
    ]
    runner(["dnf", "install", "-y", "--setopt=tsflags=noscripts", *map(str, rpms)], log=log)
    runner(["depmod", "-a", plan.uname_r], log=log)
    signer = runner(["modinfo", "-k", plan.uname_r, "-F", "signer", "zfs"]).strip()
    if not signer:
        msg = "the zefosa kernel build has unsigned ZFS modules; build it with a signing key first"
        raise BuildError(msg)


def build_zbm(
    plan: Plan,
    config: BootConfig,
    paths: ZbmPaths,
    sign: tuple[Path, Path],
    *,
    runner: Runner = run,
) -> ZbmResult:
    log = paths.logs / "zbm.log"
    _install_inputs(plan, config, paths, runner, log)
    checkout = _checkout(config, paths.cache, runner, log)
    source = paths.workdir / "zbm-src"
    shutil.rmtree(source, ignore_errors=True)
    shutil.copytree(checkout, source, symlinks=True, ignore=shutil.ignore_patterns(".git"))
    runner(["make", "-C", str(source), "core", "dracut"], log=log)
    build = BootBuild(workdir=paths.workdir, outdir=paths.outdir, logs=paths.logs)
    rpms = []
    job = _Job(plan=plan, runner=runner, log=log, workdir=paths.workdir, sign=sign)
    for flavor in FLAVORS:
        extra = {"zefosa-recovery.conf": _recovery_conf(config)} if "recovery" in flavor else {}
        conf = _conf_dir(checkout, paths.workdir / f"{flavor}.conf.d", extra)
        image_dir = _generate(job, flavor, conf)
        efi = _uki(job, flavor, image_dir)
        rpms.append(_package(plan, config, flavor, efi, build))
    return ZbmResult(regular=rpms[0], recovery=rpms[1], checkout=checkout)
