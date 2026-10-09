import json
from pathlib import Path

import pytest

from zefosator import podman, verify
from zefosator.cli import main
from zefosator.config import Plan
from zefosator.keys import KeyPair
from zefosator.signing import SigningInput

ROOT = Path(__file__).resolve().parent.parent


def test_plan_prints_resolved_json(capsys: pytest.CaptureFixture[str]) -> None:
    assert (
        main(["plan", str(ROOT / "targets" / "fedora-44-x86_64.yaml"), "--buildid", "zefosa3"]) == 0
    )
    data = json.loads(capsys.readouterr().out)
    assert data["zefosa_release"] == "200.zefosa3.fc44"
    assert data["packages"][0]["name"] == "kernel"


def test_config_error_exits_1_with_message(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "targets" / "nope.yaml"
    assert main(["plan", str(missing)]) == 1
    assert "nope.yaml: cannot read" in capsys.readouterr().err


def test_build_runs_podman_build_with_project_root(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, Path]] = []

    def fake_build(plan: Plan, root: Path, *, signing: SigningInput | None) -> None:
        assert signing is None
        assert plan.signing is None
        calls.append((plan.zefosa_release, root))

    monkeypatch.setattr(podman, "build", fake_build)
    target = ROOT / "targets" / "almalinux-10-x86_64.yaml"
    assert main(["build", str(target), "--buildid", "zefosa1", "--unsigned"]) == 0
    assert calls == [("211.56.1.zefosa1.el10_2", ROOT)]


def test_verify_runs_host_verify(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, Path]] = []

    def fake_verify(plan: Plan, root: Path) -> None:
        calls.append((plan.target_id, root))

    monkeypatch.setattr(verify, "verify", fake_verify)
    assert main(["verify", str(ROOT / "targets" / "fedora-44-x86_64.yaml")]) == 0
    assert calls == [("fedora-44-x86_64", ROOT)]


def test_build_without_signing_choice_fails(capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch_target = ROOT / "targets" / "fedora-44-x86_64.yaml"
    assert main(["build", str(monkeypatch_target)]) == 1
    assert "--unsigned" in capsys.readouterr().err


def test_signed_build_records_certificate_in_plan(
    monkeypatch: pytest.MonkeyPatch, signing_keys: tuple[KeyPair, KeyPair]
) -> None:
    _, leaf = signing_keys
    seen: list[Plan] = []

    def fake_build(plan: Plan, root: Path, *, signing: SigningInput | None) -> None:
        assert signing == SigningInput(cert=leaf.cert_pem, key_file=leaf.key)
        seen.append(plan)

    monkeypatch.setattr(podman, "build", fake_build)
    target = str(ROOT / "targets" / "fedora-44-x86_64.yaml")
    args = ["build", target, "--signing-key", str(leaf.key), "--signing-cert", str(leaf.cert_pem)]
    assert main(args) == 0
    assert seen[0].signing is not None
    assert seen[0].signing.cert_sha256 == leaf.sha256
