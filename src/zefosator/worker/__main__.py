"""Worker entry point, run inside the target container by the host CLI."""

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from zefosator.config import Plan
from zefosator.errors import ZefosatorError
from zefosator.worker.build import WorkerDirs, run_build


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="zefosator.worker")
    parser.add_argument("command", choices=["build"])
    parser.add_argument("plan", type=Path, help="resolved plan JSON written by the host")
    parser.add_argument("--src", type=Path, default=Path("/src"))
    parser.add_argument("--cache", type=Path, default=Path("/cache"))
    parser.add_argument("--output", type=Path, default=Path("/output"))
    args = parser.parse_args(argv)
    try:
        plan = Plan.model_validate_json(args.plan.read_text())
        run_build(plan, WorkerDirs(src=args.src, cache=args.cache, output=args.output))
    except ZefosatorError as exc:
        print(f"error: {exc}", file=sys.stderr)  # noqa: T201 -- error report for the person running the build
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
