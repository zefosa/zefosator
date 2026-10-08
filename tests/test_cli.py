import json
from pathlib import Path

import pytest

from zefosator.cli import main

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
