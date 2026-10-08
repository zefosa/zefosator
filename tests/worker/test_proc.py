from pathlib import Path

import pytest

from zefosator.errors import BuildError
from zefosator.worker.proc import run


def test_run_returns_stdout() -> None:
    assert run(["sh", "-c", "echo hello"]) == "hello\n"


def test_run_failure_reports_command_exit_code_and_output_tail() -> None:
    with pytest.raises(BuildError) as info:
        run(["sh", "-c", "echo first; echo boom >&2; exit 3"])
    message = str(info.value)
    assert "sh -c 'echo first; echo boom >&2; exit 3'" in message
    assert "exit code 3" in message
    assert "boom" in message


def test_run_writes_log_and_keeps_stdout(tmp_path: Path) -> None:
    log = tmp_path / "logs" / "step.log"
    assert run(["sh", "-c", "echo out; echo err >&2"], log=log) == "out\n"
    text = log.read_text()
    assert "$ sh -c" in text
    assert "out" in text
    assert "err" in text


def test_run_missing_program_is_a_build_error() -> None:
    with pytest.raises(BuildError, match="no-such-program"):
        run(["no-such-program-zefosator"])
