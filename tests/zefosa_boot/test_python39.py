import shutil
import subprocess
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src"


@pytest.mark.container
@pytest.mark.skipif(shutil.which("podman") is None, reason="podman is required")
def test_runs_under_almalinux9_python39() -> None:
    """zefosa-boot ships to EL9, whose system Python is 3.9 (mypy can no longer target it)."""
    script = (
        "python3 --version && cp -r /src/zefosa_boot /tmp/ && "
        "python3 -m compileall -q /tmp/zefosa_boot && "
        "PYTHONPATH=/tmp python3 -m zefosa_boot --help && "
        "PYTHONPATH=/tmp python3 -m zefosa_boot cmdline --help"
    )
    result = subprocess.run(
        [
            "podman", "run", "--rm", "--security-opt=label=disable",
            f"--volume={SRC}:/src:ro", "docker.io/almalinux/9-base", "sh", "-ec", script,
        ],
        capture_output=True, text=True, check=False,
    )  # fmt: skip
    assert result.returncode == 0, result.stderr
    assert "Python 3.9" in result.stdout
    assert "setup" in result.stdout
    assert "zbm-kcl" in result.stdout
