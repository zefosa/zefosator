import os
import shutil
import signal
import time
from pathlib import Path

import pytest

from zefosator.boottest import (
    BUILDER_PACKAGES,
    REQUIRED_TOOLS,
    VmLimits,
    check_host_tools,
    parse_target_report,
    render_builder_script,
    run_vm,
)
from zefosator.config import load_plan
from zefosator.errors import ConfigError

ROOT = Path(__file__).resolve().parent.parent
PLAN = load_plan(ROOT / "targets" / "almalinux-9-x86_64.yaml")


def test_missing_host_tools_are_listed_with_packages(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as info:
        check_host_tools(which=lambda name: None, ovmf_dir=tmp_path)
    message = str(info.value)
    for tool, package in REQUIRED_TOOLS.items():
        assert tool in message
        assert package in message
    assert "OVMF_CODE.secboot.fd" in message


def test_present_host_tools_pass(tmp_path: Path) -> None:
    for name in ("OVMF_CODE.secboot.fd", "OVMF_VARS.secboot.fd"):
        (tmp_path / name).write_text("fd")
    check_host_tools(which=lambda name: f"/usr/bin/{name}", ovmf_dir=tmp_path)


def test_watchdog_kills_silent_vm(tmp_path: Path) -> None:
    log = tmp_path / "serial.log"
    start = time.monotonic()
    outcome = run_vm(
        ["sh", "-c", f"echo hello > {log}; sleep 60"],
        log,
        VmLimits(idle=1, total=30, poll=0.2),
    )
    assert time.monotonic() - start < 10
    assert outcome.killed
    assert "idle" in outcome.reason


def test_watchdog_stops_on_stop_pattern(tmp_path: Path) -> None:
    log = tmp_path / "serial.log"
    outcome = run_vm(
        ["sh", "-c", f"echo 'zfsbootmenu / >' > {log}; sleep 60"],
        log,
        VmLimits(idle=30, total=30, poll=0.2, stop_patterns=("zfsbootmenu / >",)),
    )
    assert outcome.killed
    assert "zfsbootmenu / >" in outcome.reason


def test_vm_that_exits_is_not_killed(tmp_path: Path) -> None:
    log = tmp_path / "serial.log"
    outcome = run_vm(["sh", "-c", f"echo done > {log}"], log, VmLimits(idle=5, total=5, poll=0.2))
    assert not outcome.killed
    assert outcome.returncode == 0


def test_parse_target_report() -> None:
    serial = (
        "noise\r\n=== TARGET BEGIN\r\nkernel: 5.14.0\nsb-state: SecureBoot enabled\n"
        "lockdown: none [integrity] confidentiality\nroot: zroot/ROOT/os zfs\n"
        "zfs signer: Zefosa TEST CA\nmachine keyring: Zefosa TEST CA: abcd;\n=== TARGET END\n"
    )
    report = parse_target_report(serial)
    assert report["kernel"] == "5.14.0"
    assert report["zfs signer"] == "Zefosa TEST CA"
    assert parse_target_report("no report here") == {}


def test_builder_script_installs_boot_chain_and_checks_plugin() -> None:
    script = render_builder_script(PLAN)
    assert script.startswith("#!/bin/bash\n")
    assert "HEARTBEAT" in script
    assert "export TERM=dumb" in script
    assert "| cat" in script
    assert "mount /dev/vdb1 /mnt/boot/efi" in script
    assert "--releasever=9" in script
    assert "UNAME=5.14.0-687.52.1.el9_8.x86_64\n" in script
    assert "test -f /mnt/boot/vmlinuz-$UNAME" in script
    assert "zefosa-boot setup --pool zroot --esp /boot/efi --no-firmware --no-mok" in script
    assert "zefosa-boot cmdline -a" in script
    assert "zroot/ROOT/unsigned" in script


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")
def test_builder_script_is_valid_bash(tmp_path: Path) -> None:
    script = tmp_path / "b.sh"
    script.write_text(render_builder_script(PLAN))
    import subprocess  # noqa: PLC0415 -- only needed here

    assert subprocess.run(["bash", "-n", str(script)], check=False).returncode == 0


def test_builder_vm_packages_include_zfs_dracut() -> None:
    # zefosa kernel-core requires zfs-dracut; the builder installs kernel and ZFS together
    assert "/root/t/rpms/zfs-dracut-*.rpm" in BUILDER_PACKAGES


def test_builder_queries_target_packages_from_outside_the_chroot() -> None:
    # the minimal target root has no rpm binary; query its database from the builder
    script = render_builder_script(PLAN)
    assert "chroot /mnt rpm" not in script
    assert "rpm --root=/mnt -q shim-x64 zefosa-boot" in script


def test_builder_never_pipes_into_grep_q() -> None:
    # under pipefail, grep -q exiting early SIGPIPEs the producer and fails the step
    assert "| grep -q" not in render_builder_script(PLAN)


def _alive(pid: int) -> bool:
    # a zombie still answers kill(0); count only processes that have not exited
    stat = Path(f"/proc/{pid}/stat")
    return stat.exists() and stat.read_text().split(") ", 1)[1][0] != "Z"


def _wait_dead(pid: int, seconds: float = 5) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if not _alive(pid):
            return True
        time.sleep(0.1)
    os.kill(pid, signal.SIGKILL)  # don't leave the stand-in VM behind
    return False


def test_watchdog_stops_vm_when_the_watcher_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log, pidfile = tmp_path / "serial.log", tmp_path / "vm.pid"
    real_sleep = time.sleep

    def boom(seconds: float) -> None:
        while not pidfile.exists():
            real_sleep(0.05)
        raise RuntimeError

    monkeypatch.setattr("zefosator.boottest.time.sleep", boom)
    with pytest.raises(RuntimeError):
        run_vm(
            ["sh", "-c", f"echo $$ > {pidfile}; exec sleep 60"], log, VmLimits(idle=30, total=30)
        )
    monkeypatch.undo()
    assert _wait_dead(int(pidfile.read_text()))


def test_vm_dies_with_a_killed_zefosator(tmp_path: Path) -> None:
    import subprocess  # noqa: PLC0415
    import sys  # noqa: PLC0415

    log, pidfile = tmp_path / "serial.log", tmp_path / "vm.pid"
    code = (
        "import sys; from pathlib import Path; from zefosator.boottest import run_vm, VmLimits; "
        "run_vm(['sh', '-c', f'echo $$ > {sys.argv[1]}; exec sleep 60'], Path(sys.argv[2]), "
        "VmLimits(idle=60, total=60))"
    )
    watcher = subprocess.Popen([sys.executable, "-c", code, str(pidfile), str(log)])
    try:
        end = time.monotonic() + 10
        while not pidfile.exists() and time.monotonic() < end:
            time.sleep(0.05)
        os.kill(watcher.pid, signal.SIGKILL)
        watcher.wait()
        assert _wait_dead(int(pidfile.read_text()))
    finally:
        watcher.kill()
