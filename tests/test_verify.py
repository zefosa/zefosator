from pathlib import Path

import pytest

from tests.project import FIXTURE_ROOT
from zefosator.config import load_plan
from zefosator.errors import BuildError
from zefosator.verify import render_script, verify, verify_argv

PLAN = load_plan(FIXTURE_ROOT / "targets" / "almalinux-9-x86_64.yaml")


def test_script_sets_up_priority_repo_from_output_copy() -> None:
    script = render_script(PLAN)
    assert script.startswith("#!/bin/sh\n")
    assert "cp -a /output /tmp/repo" in script
    assert "createrepo_c -q /tmp/repo" in script
    assert "priority=10" in script
    assert "gpgcheck=0" in script


def test_script_contains_every_check() -> None:
    script = render_script(PLAN)
    for name in ("preference", "install", "modules", "initramfs", "integrity"):
        assert f"check {name} " in script
    assert "dnf repoquery" not in script
    assert "687.52.1.zefosa.el9_8" in script
    assert "modinfo -k 5.14.0-687.52.1.el9_8.x86_64 -n zfs" in script
    assert (
        "DRACUT_NO_XATTR=1 dracut --no-hostonly --force --kver 5.14.0-687.52.1.el9_8.x86_64"
        in script
    )
    assert "lsinitrd -m" in script
    assert "rpm -V" in script


def test_verify_argv_uses_plain_base_image_and_read_only_output(tmp_path: Path) -> None:
    argv = verify_argv(PLAN, tmp_path, tmp_path / "verify.sh")
    assert argv[:4] == ["podman", "run", "--rm", "--security-opt=label=disable"]
    assert f"--volume={tmp_path / 'output' / PLAN.target_id}:/output:ro" in argv
    assert f"--volume={tmp_path / 'verify.sh'}:/verify.sh:ro" in argv
    assert argv[-3:] == ["docker.io/almalinux/9-base", "sh", "/verify.sh"]


def test_verify_writes_script_and_runs_it(tmp_path: Path) -> None:
    (tmp_path / "output" / PLAN.target_id / "kernel").mkdir(parents=True)
    calls: list[list[str]] = []

    def execute(argv: object) -> int:
        calls.append(list(argv))  # type: ignore[call-overload] # argv is a sequence of str
        return 0

    verify(PLAN, tmp_path, execute=execute)
    script = tmp_path / "cache" / "verify" / f"{PLAN.target_id}.sh"
    assert script.read_text() == render_script(PLAN)
    assert calls == [verify_argv(PLAN, tmp_path, script)]


def test_verify_requires_build_output(tmp_path: Path) -> None:
    with pytest.raises(BuildError, match="run `zefosator build"):
        verify(PLAN, tmp_path, execute=lambda _: 0)


def test_verify_failure_is_reported(tmp_path: Path) -> None:
    (tmp_path / "output" / PLAN.target_id / "kernel").mkdir(parents=True)
    with pytest.raises(BuildError, match="verification failed"):
        verify(PLAN, tmp_path, execute=lambda _: 1)


def test_preference_check_uses_newer_fake_upstream_at_default_priority() -> None:
    script = render_script(PLAN)
    assert "Version: 99.0" in script
    assert "Name: kernel-core" in script
    assert "[fake-upstream]" in script
    assert "priority=99" in script
    assert "dnf install --assumeno kernel-core" in script
    assert "grep -F 5.14.0-687.52.1.zefosa.el9_8" in script
    assert "rm -f /etc/yum.repos.d/fake-upstream.repo" in script
