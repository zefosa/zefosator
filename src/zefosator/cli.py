import argparse
import getpass
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from zefosator import boottest, keys, pack, podman, signing, upload, verify
from zefosator.config import Plan, load_boot_config, load_common_config, load_plan, project_root
from zefosator.errors import ConfigError, ZefosatorError
from zefosator.worker.boot import BootJob

TARGET_COMMANDS = (
    ("plan", "print the resolved build plan as JSON"),
    ("build", "build OpenZFS and repackage the kernel into output/<target-id>/"),
    ("verify", "install output/<target-id>/ in a clean vendor container and check it"),
    ("build-boot", "build shim (and on Fedora the common boot packages) into output/"),
    ("boottest", "boot the built packages through the Secure Boot chain in QEMU"),
    ("pack", "pack built RPMs into the tar + sha256 archives listed under archives:"),
    ("upload", "publish packed archives to upload.webdav_url in common.yaml"),
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
        if name in {"build", "build-boot"}:
            _signing_arguments(command, unsigned=name == "build")
        if name in {"pack", "upload"}:
            command.add_argument(
                "names", nargs="*", metavar="ARCHIVE", help=f"archives to {name} (default: all)"
            )
        if name == "upload":
            command.add_argument(
                "--upload-key-file",
                type=Path,
                help=f"file holding the key (or env {upload.KEY_ENV})",
            )
            command.add_argument(
                "--force", action="store_true", help="upload even archives published unchanged"
            )
            command.add_argument(
                "--dry-run", action="store_true", help="only report what would be uploaded"
            )
        if name in {"build-boot", "boottest"}:
            command.add_argument("--ca-cert", type=Path, help="zefosa CA certificate (PEM or DER)")
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


def _signing_arguments(parser: argparse.ArgumentParser, *, unsigned: bool) -> None:
    parser.add_argument(
        "--signing-key", type=Path, help=f"leaf private key (or env {signing.KEY_ENV})"
    )
    parser.add_argument(
        "--signing-cert", type=Path, help=f"leaf certificate (or env {signing.CERT_ENV})"
    )
    if unsigned:
        parser.add_argument(
            "--unsigned", action="store_true", help="development build: unsigned modules"
        )


def _build(plan: Plan, args: argparse.Namespace) -> None:
    source = signing.resolve_signing(
        key=args.signing_key, cert=args.signing_cert, unsigned=args.unsigned, env=os.environ
    )
    if source is not None:
        signing.check_key_location(source, project_root(args.target))
        signing.check_key_matches(source)
        plan = plan.model_copy(update={"signing": signing.describe_cert(source.cert)})
    podman.build(plan, project_root(args.target), signing=source)


def _build_boot(plan: Plan, args: argparse.Namespace) -> None:
    root = project_root(args.target)
    if plan.boot is None:
        msg = f"{args.target}: no boot section; build-boot needs boot.shim and boot.cloud_image"
        raise ConfigError(msg)
    source, ca = None, None
    if plan.boot.common:
        source = signing.resolve_signing(
            key=args.signing_key, cert=args.signing_cert, unsigned=False, env=os.environ
        )
        if args.ca_cert is None:
            msg = "--ca-cert is required to build the common boot packages (zefosa-secureboot-ca)"
            raise ConfigError(msg)
        assert source is not None  # noqa: S101 -- resolve_signing(unsigned=False) never returns None
        signing.check_key_location(source, root)
        signing.check_key_matches(source)
        ca = signing.ca_der(args.ca_cert, source, root / "cache" / "signing" / "ca.der")
        plan = plan.model_copy(update={"signing": signing.describe_cert(source.cert)})
    job = BootJob(plan=plan, config=load_boot_config(root))
    podman.build_boot(job, root, signing=source, ca_cert=ca)


def _new_ca_passphrase() -> str:
    passphrase = getpass.getpass("Passphrase for the new CA key: ")
    if getpass.getpass("Repeat the passphrase: ") != passphrase:
        msg = "the passphrases do not match"
        raise ConfigError(msg)
    return passphrase


def _keys(args: argparse.Namespace) -> None:
    if args.key_command == "ca":
        pair = keys.create_ca(args.out, passphrase=_new_ca_passphrase(), cn=args.cn, days=args.days)
        print(f"Created {args.cn}: {pair.cert_pem} (keep {pair.key} offline)")
    else:
        ca_key = args.ca / "ca.key"
        passphrase = (
            getpass.getpass(f"Passphrase for {ca_key}: ")
            if ca_key.is_file() and keys.ca_key_encrypted(args.ca)
            else None
        )
        pair = keys.create_leaf(
            args.ca, args.out, ca_passphrase=passphrase, cn=args.cn, days=args.days
        )
        print(f"Issued {args.cn}: {pair.cert_pem}, key {pair.key}")
    print(f"SHA-256 fingerprint (DER): {pair.sha256}")


def _upload(plan: Plan, args: argparse.Namespace) -> None:
    root = project_root(args.target)
    config = load_common_config(root).upload
    if config is None:
        msg = f"{root / 'common.yaml'} has no upload section (upload.webdav_url)"
        raise ConfigError(msg)
    key = upload.resolve_upload_key(args.upload_key_file, root)
    share = upload.WebDavShare(config.webdav_url, key)
    for outcome in upload.upload(
        plan, root, args.names, share, force=args.force, dry_run=args.dry_run
    ):
        print(f"{outcome.name}: {outcome.action} ({outcome.size} bytes, sha256 {outcome.sha256})")


def _target_command(plan: Plan, args: argparse.Namespace) -> None:
    if args.command == "plan":
        print(plan.model_dump_json(indent=2))
    elif args.command == "build":
        _build(plan, args)
    elif args.command == "build-boot":
        _build_boot(plan, args)
    elif args.command == "boottest":
        if args.ca_cert is None:
            msg = "--ca-cert is required: boottest enrolls the CA as a MOK key"
            raise ConfigError(msg)
        boottest.boottest(plan, project_root(args.target), args.ca_cert.resolve())
    elif args.command == "verify":
        verify.verify(plan, project_root(args.target))
    elif args.command == "pack":
        for archive in pack.pack(plan, project_root(args.target), args.names):
            print(f"{archive.tar} ({archive.size} bytes) sha256 {archive.sha256}")
    elif args.command == "upload":
        _upload(plan, args)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "keys":
            _keys(args)
        else:
            _target_command(load_plan(args.target, buildid=args.buildid), args)
    except ZefosatorError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0
