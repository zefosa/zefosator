import shutil
import time
from pathlib import Path

import pytest

from zefosator.boottest import (
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
