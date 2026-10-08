import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from zefosator import podman
from zefosator.config import load_plan, project_root
from zefosator.errors import ZefosatorError

COMMANDS = (
    ("plan", "print the resolved build plan as JSON"),
    ("build", "build OpenZFS and repackage the kernel into output/<target-id>/"),
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="zefosator",
        description="Build OpenZFS and repackage upstream kernel RPMs with ZFS modules.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    for name, text in COMMANDS:
        command = commands.add_parser(name, help=text)
        command.add_argument(
            "target", type=Path, help="target YAML file, e.g. targets/fedora-44-x86_64.yaml"
        )
        command.add_argument(
            "--buildid", help="override the kernel release build ID (e.g. zefosa1)"
        )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        plan = load_plan(args.target, buildid=args.buildid)
        if args.command == "plan":
            print(plan.model_dump_json(indent=2))
        elif args.command == "build":
            podman.build(plan, project_root(args.target))
    except ZefosatorError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0
