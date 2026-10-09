"""Access to system commands; replaced by a fake in tests. Python 3.9, standard library only."""

from __future__ import annotations

import subprocess
import sys
from typing import TYPE_CHECKING, NamedTuple, Protocol

if TYPE_CHECKING:
    from collections.abc import Sequence


class Result(NamedTuple):
    returncode: int
    stdout: str
    stderr: str


class System(Protocol):
    def run(self, argv: Sequence[str]) -> Result: ...

    def execute(self, argv: Sequence[str]) -> int: ...


class RealSystem:
    def run(self, argv: Sequence[str]) -> Result:
        try:
            done = subprocess.run(list(argv), capture_output=True, text=True, check=False)
        except OSError as exc:
            return Result(127, "", f"{argv[0]}: {exc.strerror or exc}")
        return Result(done.returncode, done.stdout, done.stderr)

    def execute(self, argv: Sequence[str]) -> int:
        """Run interactively (inherits the terminal), e.g. zbm-kcl -e opening $EDITOR."""
        try:
            return subprocess.run(list(argv), check=False).returncode
        except OSError as exc:
            sys.stderr.write(f"error: cannot run {argv[0]}: {exc.strerror or exc}\n")
            return 127
