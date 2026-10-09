"""`zefosator boottest`: boot the built packages through the Secure Boot chain in QEMU.

A builder VM (Secure Boot off) installs a root-on-ZFS target with the zefosa kernel and boot
packages; the target then boots with OVMF Secure Boot, Microsoft keys and the zefosa CA as MOK:
firmware -> vendor shim -> zefosa rEFInd -> zefosa ZFSBootMenu -> kexec -> zefosa kernel.
Every VM runs on a tight leash (spec section 8): idle and total limits, stop patterns.
"""

import os
import re
import shlex
import shutil
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from jinja2 import Environment, PackageLoader, StrictUndefined

from zefosator.config import Plan
from zefosator.errors import BuildError, ConfigError
from zefosator.worker.fetch import http_download
from zefosator.worker.proc import run

REQUIRED_TOOLS = {
    "qemu-system-x86_64": "qemu-system-x86-core",
    "qemu-img": "qemu-img",
    "virt-customize": "guestfs-tools",
    "guestfish": "libguestfs",
    "virt-fw-vars": "python3-virt-firmware",
    "sbattach": "sbsigntools",
}
OVMF_DIR = Path("/usr/share/edk2/ovmf")
OVMF_FILES = ("OVMF_CODE.secboot.fd", "OVMF_VARS.secboot.fd")
SHIM_OWNER_GUID = "605dab50-e046-4300-abb6-3dd810dd8b23"
LOADER = "\\EFI\\zefosa\\shimx64.efi"
TEMPLATES = Path(__file__).resolve().parent / "templates" / "boottest"
# The builder VM needs the zefosa kernel and ZFS tools; Secure Boot is off there.
BUILDER_PACKAGES = (
    "dnf install -y gdisk dosfstools /root/t/rpms/kernel*.rpm /root/t/rpms/zfs-[0-9]*.rpm "
    "/root/t/rpms/zfs-dracut-*.rpm /root/t/rpms/lib*.rpm"
)
_GUESTFS_ENV = {"LIBGUESTFS_BACKEND": "direct"}
_RPMS = re.compile(
    r"^(kernel-(\d|core-\d|modules-core-\d|modules-\d)|zfs-\d|zfs-dracut-\d|"
    r"(libnvpair|libuutil|libzfs|libzpool)\d+-\d|shim-x64-|zefosa-)"
)
_SKIP = re.compile(r"debuginfo|debugsource|-devel-|\.src\.rpm$|-test-")

_ENV = Environment(
    loader=PackageLoader("zefosator", "templates"),
    undefined=StrictUndefined,
    autoescape=False,  # noqa: S701 -- renders a shell script; values quoted with the sh filter
    keep_trailing_newline=True,
)
_ENV.filters["sh"] = shlex.quote


def check_host_tools(
    *, which: Callable[[str], str | None] = shutil.which, ovmf_dir: Path = OVMF_DIR
) -> None:
    missing = [
        f"{tool} (package {pkg})" for tool, pkg in REQUIRED_TOOLS.items() if which(tool) is None
    ]
    missing += [
        f"{ovmf_dir / f} (package edk2-ovmf)" for f in OVMF_FILES if not (ovmf_dir / f).is_file()
    ]
    if missing:
        msg = "boottest needs these host tools; please install them:\n  " + "\n  ".join(missing)
        raise ConfigError(msg)


@dataclass(frozen=True)
class VmLimits:
    idle: float  # seconds without serial output
    total: float  # hard cap
    poll: float = 5.0
    stop_patterns: tuple[str, ...] = ()  # e.g. an emergency shell prompt: no point waiting


@dataclass(frozen=True)
class VmOutcome:
    killed: bool
    reason: str
    returncode: int | None


def run_vm(argv: Sequence[str], log: Path, limits: VmLimits) -> VmOutcome:
    """Run a VM with serial output in `log`; kill it on silence, timeout or a stop pattern."""
    log.unlink(missing_ok=True)
    stderr = log.with_suffix(".stderr")
    with stderr.open("w") as err:
        process = subprocess.Popen(list(argv), stdout=subprocess.DEVNULL, stderr=err)
        start = last = time.monotonic()
        seen = -1
        reason = ""
        while process.poll() is None:
            time.sleep(limits.poll)
            now = time.monotonic()
            size = log.stat().st_size if log.exists() else 0
            if size != seen:
                seen, last = size, now
            text = log.read_text(errors="replace") if size else ""
            hit = next((p for p in limits.stop_patterns if p in text), None)
            if hit is not None:
                reason = f"stop pattern {hit!r} seen"
            elif now - last > limits.idle:
                reason = f"serial log idle for {now - last:.0f}s"
            elif now - start > limits.total:
                reason = f"total time {now - start:.0f}s exceeded"
            if reason:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                return VmOutcome(killed=True, reason=reason, returncode=process.returncode)
    return VmOutcome(killed=False, reason="exited", returncode=process.returncode)


def parse_target_report(serial: str) -> dict[str, str]:
    text = serial.replace("\r", "")
    match = re.search(r"=== TARGET BEGIN\n(.*?)\n=== TARGET END", text, re.DOTALL)
    if match is None:
        return {}
    report = {}
    for line in match.group(1).splitlines():
        key, sep, value = line.partition(": ")
        if sep:
            report[key.strip()] = value.strip()
    return report


def render_builder_script(plan: Plan) -> str:
    return _ENV.get_template("boottest/builder.sh.j2").render(plan=plan)


def _service(name: str) -> str:
    return (
        f"[Unit]\nDescription=zefosator boottest {name}\nAfter=multi-user.target\n"
        f"[Service]\nType=oneshot\nExecStart=/usr/local/sbin/{name}.sh\n"
        "[Install]\nWantedBy=multi-user.target\n"
    )


def _qemu(drives: Sequence[str], log: Path, *, secure: Path | None, network: bool) -> list[str]:
    argv = ["qemu-system-x86_64", "-enable-kvm", "-cpu", "host", "-m", "4096", "-smp", "4"]
    if secure is not None:
        argv += [
            "-machine", "q35,smm=on", "-global", "driver=cfi.pflash01,property=secure,value=on",
            "-drive", f"if=pflash,format=raw,unit=0,file={OVMF_DIR / OVMF_FILES[0]},readonly=on",
            "-drive", f"if=pflash,format=raw,unit=1,file={secure}",
        ]  # fmt: skip
    for drive in drives:
        argv += ["-drive", f"file={drive},if=virtio"]
    argv += ["-nic", "user,model=virtio-net-pci"] if network else ["-nic", "none"]
    return [*argv, "-display", "none", "-serial", f"file:{log}", "-no-reboot"]


def _pick_rpms(dirs: Sequence[Path]) -> list[Path]:
    rpms = [
        p
        for d in dirs
        for p in sorted(d.glob("*.rpm"))
        if _RPMS.match(p.name) and not _SKIP.search(p.name)
    ]
    if not any(p.name.startswith("zefosa-boot-") for p in rpms):
        msg = "output/boot has no zefosa-boot package; run `zefosator build-boot` for Fedora"
        raise ConfigError(msg)
    return rpms


def _strip_signature(rpm: Path, member: str, out: Path) -> Path:
    source, target = shlex.quote(str(rpm)), shlex.quote(str(out))
    extract = f"rpm2cpio {source} | cpio -i --quiet --to-stdout {shlex.quote(member)} > {target}"
    run(["sh", "-ec", extract])
    run(["sbattach", "--remove", str(out)])
    return out


@dataclass(frozen=True)
class _Check:
    name: str
    ok: bool
    detail: str


class BootTest:
    def __init__(self, plan: Plan, root: Path, ca_cert: Path) -> None:
        if plan.boot is None:
            msg = f"{plan.target_id}: target has no boot section"
            raise ConfigError(msg)
        self.plan, self.root = plan, root
        self.work = root / "cache" / "boottest" / plan.target_id
        self.ca_der = self.work / "ca.der"
        self.ca_cert = ca_cert
        self.checks: list[_Check] = []

    def _check(self, name: str, ok: bool, detail: str) -> None:  # noqa: FBT001 -- reads naturally
        self.checks.append(_Check(name, ok, detail))
        print(f"{'PASS' if ok else 'FAIL'} {name}: {detail}", flush=True)  # noqa: T201 -- test report

    def _prepare(self) -> list[Path]:
        plan, root = self.plan, self.root
        out = root / "output" / plan.target_id
        rpms = _pick_rpms([out / "kernel", out / "zfs", out / "boot", root / "output" / "boot"])
        shutil.rmtree(self.work, ignore_errors=True)
        t = self.work / "t"
        (t / "rpms").mkdir(parents=True)
        for rpm in rpms:
            shutil.copy2(rpm, t / "rpms" / rpm.name)
        shutil.copy2(TEMPLATES / "target-test.sh", t / "target-test.sh")
        (t / "target-test.service").write_text(_service("target-test"))
        (t / "zefosa-builder.service").write_text(_service("zefosa-builder"))
        (self.work / "zefosa-builder.sh").write_text(render_builder_script(plan))
        core = next(p for p in rpms if p.name.startswith("kernel-core-"))
        _strip_signature(core, f"./lib/modules/{plan.uname_r}/vmlinuz", t / "vmlinuz-unsigned")
        zbm = next(p for p in rpms if re.match(r"zefosa-zfsbootmenu-\d", p.name))
        _strip_signature(
            zbm, "./boot/efi/EFI/zefosa/zfsbootmenu.efi", self.work / "zfsbootmenu-unsigned.efi"
        )
        form = "DER" if self.ca_cert.read_bytes()[:1] == b"\x30" else "PEM"
        run(
            [
                "openssl",
                "x509",
                "-inform",
                form,
                "-in",
                str(self.ca_cert),
                "-outform",
                "DER",
                "-out",
                str(self.ca_der),
            ]
        )
        return rpms

    def _builder(self) -> None:
        assert self.plan.boot is not None  # noqa: S101 -- checked in __init__
        images = self.root / "cache" / "images"
        images.mkdir(parents=True, exist_ok=True)
        cloud = images / self.plan.boot.cloud_image.rsplit("/", 1)[1]
        if not cloud.is_file():
            part = cloud.with_name(cloud.name + ".part")
            http_download(self.plan.boot.cloud_image, part)
            part.rename(cloud)
        builder = self.work / "builder.qcow2"
        shutil.copy2(cloud, builder)
        log = self.work / "logs" / "virt-customize.log"
        run(
            [
                "virt-customize", "-q", "-a", str(builder),
                "--copy-in", f"{self.work / 't'}:/root",
                "--upload", f"{self.work / 'zefosa-builder.sh'}:/usr/local/sbin/zefosa-builder.sh",
                "--chmod", "0755:/usr/local/sbin/zefosa-builder.sh",
                "--run-command", "cp /root/t/zefosa-builder.service /etc/systemd/system/",
                "--run-command", BUILDER_PACKAGES,
                "--run-command", f"grubby --set-default /boot/vmlinuz-{self.plan.uname_r}",
                "--run-command", "systemctl enable zefosa-builder.service",
                "--run-command", "systemctl mask serial-getty@ttyS0.service",
                "--touch", "/etc/cloud/cloud-init.disabled", "--selinux-relabel",
            ],
            env={**dict(os.environ), **_GUESTFS_ENV},
            log=log,
        )  # fmt: skip
        run(["qemu-img", "create", "-q", "-f", "qcow2", str(self.work / "target.qcow2"), "8G"])
        serial = self.work / "builder.log"
        print("==> builder VM: installing the root-on-ZFS target", flush=True)  # noqa: T201
        outcome = run_vm(
            _qemu(
                [str(builder), str(self.work / "target.qcow2")], serial, secure=None, network=True
            ),
            serial,
            VmLimits(idle=300, total=3600, poll=10),
        )
        text = serial.read_text(errors="replace") if serial.exists() else ""
        if "=== BUILDER DONE" not in text:
            tail = "\n".join(text.replace("\r", "").splitlines()[-30:])
            msg = f"builder VM failed ({outcome.reason}); serial log {serial}:\n{tail}"
            raise BuildError(msg)

    def _vars(self, name: str) -> Path:
        out = self.work / f"vars-{name}.fd"
        run(
            [
                "virt-fw-vars", "--input", str(OVMF_DIR / OVMF_FILES[1]), "--output", str(out),
                "--add-mok", SHIM_OWNER_GUID, str(self.ca_der), "--append-boot-filepath", LOADER,
            ]
        )  # fmt: skip
        return out

    def _esp(self, *ops: tuple[str, str, str]) -> None:
        """guestfish upload/download on the target ESP: (op, local, remote)."""
        for op, local, remote in ops:
            run(
                [
                    "guestfish",
                    "-a",
                    str(self.work / "target.qcow2"),
                    "-m",
                    "/dev/sda1",
                    op,
                    *((local, remote) if op == "upload" else (remote, local)),
                ],
                env={**dict(os.environ), **_GUESTFS_ENV},
            )

    def _boot(self, name: str) -> str:
        serial = self.work / f"target-{name}.log"
        print(f"==> Secure Boot run: {name}", flush=True)  # noqa: T201
        outcome = run_vm(
            _qemu(
                [str(self.work / "target.qcow2")], serial, secure=self._vars(name), network=False
            ),
            serial,
            VmLimits(idle=90, total=300, stop_patterns=("zfsbootmenu / >", "emergency shell")),
        )
        print(f"    {name}: {outcome.reason}", flush=True)  # noqa: T201
        return serial.read_text(errors="replace") if serial.exists() else ""

    def _ca_name(self) -> str:
        subject = run(
            [
                "openssl",
                "x509",
                "-inform",
                "DER",
                "-in",
                str(self.ca_der),
                "-noout",
                "-subject",
                "-nameopt",
                "multiline",
            ]
        )
        match = re.search(r"commonName\s*=\s*(.+)", subject)
        return match.group(1).strip() if match else ""

    def run(self) -> None:
        self._prepare()
        self._builder()
        ca = self._ca_name()
        report = parse_target_report(self._boot("positive"))
        self._check("boot", bool(report), "final OS reached multi-user" if report else "no report")
        self._check(
            "secure-boot",
            "SecureBoot enabled" in report.get("sb-state", ""),
            report.get("sb-state", "-"),
        )
        self._check(
            "lockdown", "[integrity]" in report.get("lockdown", ""), report.get("lockdown", "-")
        )
        self._check("root-on-zfs", report.get("root", "").endswith(" zfs"), report.get("root", "-"))
        self._check(
            "zfs-signer",
            report.get("zfs signer") == ca,
            f"{report.get('zfs signer', '-')} (CA {ca})",
        )
        self._check(
            "ca-in-machine-keyring",
            ca in report.get("machine keyring", ""),
            report.get("machine keyring", "-"),
        )

        conf = self.work / "refind.conf"
        self._esp(("download", str(conf), "/EFI/zefosa/refind.conf"))
        original = conf.read_text()
        conf.write_text(original.replace("zbm.prefer=zroot ", "zbm.prefer=zroot/ROOT/unsigned ", 1))
        self._esp(("upload", str(conf), "/EFI/zefosa/refind.conf"))
        serial = self._boot("unsigned-kernel")
        refused = "TARGET BEGIN" not in serial and "Unable to load kernel" in serial
        self._check(
            "unsigned-kernel-refused",
            refused,
            "ZFSBootMenu refused the kexec" if refused else "kernel was not refused",
        )

        conf.write_text(original)
        self._esp(
            ("upload", str(conf), "/EFI/zefosa/refind.conf"),
            ("upload", str(self.work / "zfsbootmenu-unsigned.efi"), "/EFI/zefosa/zfsbootmenu.efi"),
        )
        serial = self._boot("unsigned-zbm")
        rlog = self.work / "refind.log"
        self._esp(("download", str(rlog), "/EFI/zefosa/refind.log"))
        refind_log = rlog.read_bytes().decode("utf-16-le", errors="replace")
        refused = "Linux version" not in serial and "Secure boot error" in refind_log
        self._check(
            "unsigned-zbm-refused",
            refused,
            "shim refused the unsigned image" if refused else "image was not refused",
        )

        failed = [c.name for c in self.checks if not c.ok]
        if failed:
            names = ", ".join(failed)
            msg = f"boottest {self.plan.target_id}: failed checks: {names}; logs in {self.work}"
            raise BuildError(msg)
        print(f"boottest {self.plan.target_id}: all {len(self.checks)} checks passed", flush=True)  # noqa: T201


def boottest(plan: Plan, root: Path, ca_cert: Path) -> None:
    check_host_tools()
    BootTest(plan, root, ca_cert).run()
