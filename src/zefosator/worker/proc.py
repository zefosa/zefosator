import shlex
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path

from zefosator.errors import BuildError

TAIL_LINES = 25


def run(
    argv: Sequence[str],
    *,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    log: Path | None = None,
    stdin: str | None = None,
) -> str:
    """Run a command and return its stdout; raise BuildError with the output tail on failure."""
    command = shlex.join(argv)
    try:
        result = subprocess.run(
            list(argv),
            cwd=cwd,
            env=dict(env) if env is not None else None,
            input=stdin,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        msg = f"cannot run {command}: {exc.strerror or exc}"
        raise BuildError(msg) from exc
    if log is not None:
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a") as handle:
            handle.write(f"$ {command}\n{result.stdout}")
            if result.stderr:
                handle.write(f"--- stderr ---\n{result.stderr}")
    if result.returncode != 0:
        tail = "\n".join((result.stdout + result.stderr).splitlines()[-TAIL_LINES:])
        where = f" (full log: {log})" if log is not None else ""
        msg = f"command failed with exit code {result.returncode}: {command}{where}\n{tail}"
        raise BuildError(msg)
    return result.stdout
