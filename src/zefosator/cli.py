import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from zefosator import keys, podman, signing, verify
from zefosator.config import Plan, load_plan, project_root
from zefosator.errors import ZefosatorError

TARGET_COMMANDS = (
    ("plan", "print the resolved build plan as JSON"),
    ("build", "build OpenZFS and repackage the kernel into output/<target-id>/"),
    ("verify", "install output/<target-id>/ in a clean vendor container and check it"),
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="zefosator",
        description="Build OpenZFS and repackage upstream kernel RPMs with ZFS modules.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    for name, text in TARGET_COMMANDS:
        command = commands.add_parser(name, help=text)
        command.add_argument(
            "target", type=Path, help="target YAML file, e.g. targets/fedora-44-x86_64.yaml"
        )
        command.add_argument(
            "--buildid", help="override the kernel release build ID (e.g. zefosa1)"
        )
        if name == "build":
            _signing_arguments(command)
    key_parser = commands.add_parser("keys", help="create the Secure Boot CA or a signing leaf")
    key_commands = key_parser.add_subparsers(dest="key_command", required=True)
    ca = key_commands.add_parser("ca", help="create the zefosa Secure Boot CA")
    ca.add_argument("--out", type=Path, required=True, help="directory for ca.key, ca.pem, ca.der")
    ca.add_argument("--cn", default=keys.CA_CN)
    ca.add_argument("--days", type=int, default=keys.CA_DAYS)
    leaf = key_commands.add_parser("leaf", help="issue a signing leaf from the CA")
    leaf.add_argument("--ca", type=Path, required=True, help="directory holding ca.key and ca.pem")
    leaf.add_argument("--out", type=Path, required=True, help="directory for signing.key/.pem/.der")
    leaf.add_argument("--cn", default=keys.LEAF_CN)
    leaf.add_argument("--days", type=int, default=keys.LEAF_MAX_DAYS)
    return parser


def _signing_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--signing-key", type=Path, help=f"leaf private key (or env {signing.KEY_ENV})"
    )
    parser.add_argument(
        "--signing-cert", type=Path, help=f"leaf certificate (or env {signing.CERT_ENV})"
    )
    parser.add_argument(
        "--unsigned", action="store_true", help="development build: unsigned modules"
    )


def _build(plan: Plan, args: argparse.Namespace) -> None:
    source = signing.resolve_signing(
        key=args.signing_key, cert=args.signing_cert, unsigned=args.unsigned, env=os.environ
    )
    if source is not None:
        signing.check_key_matches(source)
        plan = plan.model_copy(update={"signing": signing.describe_cert(source.cert)})
    podman.build(plan, project_root(args.target), signing=source)


def _keys(args: argparse.Namespace) -> None:
    if args.key_command == "ca":
        pair = keys.create_ca(args.out, cn=args.cn, days=args.days)
        print(f"Created {args.cn}: {pair.cert_pem} (keep {pair.key} offline)")
    else:
        pair = keys.create_leaf(args.ca, args.out, cn=args.cn, days=args.days)
        print(f"Issued {args.cn}: {pair.cert_pem}, key {pair.key}")
    print(f"SHA-256 fingerprint (DER): {pair.sha256}")


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "keys":
            _keys(args)
            return 0
        plan = load_plan(args.target, buildid=args.buildid)
        if args.command == "plan":
            print(plan.model_dump_json(indent=2))
        elif args.command == "build":
            _build(plan, args)
        elif args.command == "verify":
            verify.verify(plan, project_root(args.target))
    except ZefosatorError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0
