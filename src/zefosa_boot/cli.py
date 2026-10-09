"""zefosa-boot command line. Python 3.9, standard library only."""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from zefosa_boot.system import RealSystem, Result, System

if TYPE_CHECKING:
    from collections.abc import Sequence

LOADER = "\\EFI\\zefosa\\shimx64.efi"
LABEL = "zefosa"
REQUIRED = ("shimx64.efi", "mmx64.efi", "grubx64.efi", "zfsbootmenu.efi")
OPTIONAL = ("zfsbootmenu-recovery.efi",)
PACKAGES = (
    "shim-x64",
    "zefosa-refind",
    "zefosa-zfsbootmenu",
    "zefosa-zfsbootmenu-recovery",
    "zefosa-secureboot-ca",
    "zefosa-boot",
)


@dataclass(frozen=True)
class Paths:
    template: Path = Path("/usr/share/zefosa-boot/refind.conf.in")
    ca: Path = Path("/usr/share/zefosa/keys/zefosa-ca.der")
    efi_vars: Path = Path("/sys/firmware/efi")
    sys_block: Path = Path("/sys/class/block")


class UserError(Exception):
    """Reported to the user without a traceback."""


def _out(text: str) -> None:
    sys.stdout.write(text + "\n")


def _checked(system: System, argv: Sequence[str]) -> Result:
    result = system.run(argv)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        msg = f"{' '.join(argv)} failed: {detail}"
        raise UserError(msg)
    return result


def _root_dataset(system: System) -> str | None:
    fields = system.run(["findmnt", "-no", "SOURCE,FSTYPE", "/"]).stdout.split()
    return fields[0] if len(fields) == 2 and fields[1] == "zfs" else None  # noqa: PLR2004


def _pool(args: argparse.Namespace, system: System) -> str:
    if args.pool:
        return str(args.pool)
    root = _root_dataset(system)
    if root is None:
        msg = "the root filesystem is not ZFS; pass --pool POOL"
        raise UserError(msg)
    return root.split("/", 1)[0]


def _write_refind(zdir: Path, template: Path, pool: str) -> None:
    conf = zdir / "refind.conf"
    if conf.exists():
        _out(f"Kept existing {conf}")
        return
    conf.write_text(template.read_text().replace("@POOL@", pool))
    _out(f"Wrote {conf} (zbm.prefer={pool})")


@dataclass(frozen=True)
class BootEntries:
    order: list[str]
    text: dict[str, str]
    inactive: set[str]


def _boot_entries(system: System) -> BootEntries:
    output = _checked(system, ["efibootmgr", "-v"]).stdout
    entries = BootEntries([], {}, set())
    for line in output.splitlines():
        if line.startswith("BootOrder:"):
            entries.order.extend(n for n in line.split(":", 1)[1].strip().split(",") if n)
        match = re.match(r"^Boot([0-9A-Fa-f]{4})(\*?) (.*)$", line)
        if match:
            number = match.group(1).upper()
            entries.text[number] = match.group(3)
            if not match.group(2):
                entries.inactive.add(number)
    return entries


def _zefosa_entry(entries: dict[str, str]) -> str | None:
    for number, text in entries.items():
        if LOADER.lower() in text.lower():
            return number
    return None


def _check_esp(system: System, esp: Path) -> None:
    fields = system.run(["findmnt", "-no", "TARGET,FSTYPE", "--target", str(esp)]).stdout.split()
    target, fstype = [*fields, "", ""][:2]
    if target != str(esp) or fstype != "vfat":
        found = f"is on {target} ({fstype})" if target != str(esp) else f"is {fstype}, not vfat"
        msg = (
            f"{esp} is not a mounted EFI System Partition: it {found}. The zefosa boot "
            "packages install into /boot/efi; mount the ESP there and reinstall them"
        )
        raise UserError(msg)


def _esp_disk(system: System, esp: Path, sys_block: Path) -> tuple[str, str]:
    source = _checked(system, ["findmnt", "-no", "SOURCE", "--target", str(esp)]).stdout.strip()
    name = Path(source).name
    parent = _checked(system, ["lsblk", "-no", "PKNAME", source]).stdout.strip()
    part_file = sys_block / name / "partition"
    if not parent or not part_file.is_file():
        msg = f"cannot determine the disk and partition number of the ESP {source}"
        raise UserError(msg)
    return f"/dev/{parent}", part_file.read_text().strip()


def _ensure_entry(system: System, esp: Path, paths: Paths) -> None:
    if not paths.efi_vars.is_dir():
        msg = "this system was not booted in UEFI mode; use --no-firmware"
        raise UserError(msg)
    entries = _boot_entries(system)
    order = entries.order
    number = _zefosa_entry(entries.text)
    if number is None:
        disk, part = _esp_disk(system, esp, paths.sys_block)
        create = ["efibootmgr", "--create", "--disk", disk, "--part", part]
        _checked(system, [*create, "--label", LABEL, "--loader", LOADER])
        _out(f"Created firmware boot entry '{LABEL}' for {LOADER} (first in BootOrder)")
        return
    if number in entries.inactive:
        _checked(system, ["efibootmgr", "-b", number, "-a"])
    if order[:1] != [number]:
        new_order = [number, *(n for n in order if n != number)]
        _checked(system, ["efibootmgr", "--bootorder", ",".join(new_order)])
    _out(f"Firmware boot entry Boot{number} ({LOADER}) is active and first in BootOrder")


def _mok_state(system: System, ca: Path) -> str:
    if "already enrolled" in system.run(["mokutil", "--test-key", str(ca)]).stdout:
        return "enrolled"
    # --test-key only knows enrolled keys; pending requests are listed by fingerprint
    pending = system.run(["mokutil", "--list-new"]).stdout.replace(":", "").lower()
    if ca.is_file() and hashlib.sha1(ca.read_bytes(), usedforsecurity=False).hexdigest() in pending:
        return "pending (confirm in MokManager at next boot)"
    return "not enrolled"


def _ensure_mok(system: System, ca: Path) -> None:
    state = _mok_state(system, ca)
    if state != "not enrolled":
        _out(f"zefosa CA: {state}")
        return
    _checked(system, ["mokutil", "--import", str(ca), "--root-pw"])
    _out(
        "zefosa CA import requested. At the next boot MokManager starts: choose 'Enroll MOK',\n"
        "'Continue', 'Yes' and enter the root password to confirm."
    )


def setup(args: argparse.Namespace, system: System, paths: Paths) -> None:
    esp = Path(args.esp)
    zdir = esp / "EFI" / "zefosa"
    _check_esp(system, esp)
    missing = [n for n in REQUIRED if not (zdir / n).is_file()]
    if missing:
        msg = (
            f"missing {', '.join(str(zdir / n) for n in missing)}: install the zefosa-boot packages"
        )
        raise UserError(msg)
    _write_refind(zdir, paths.template, _pool(args, system))
    if not args.no_firmware:
        _ensure_entry(system, esp, paths)
    if not args.no_mok:
        _ensure_mok(system, paths.ca)


def status(args: argparse.Namespace, system: System, paths: Paths) -> None:
    esp = Path(args.esp)
    zdir = esp / "EFI" / "zefosa"
    sb = system.run(["mokutil", "--sb-state"]).stdout
    _out(f"Secure Boot: {'enabled' if 'SecureBoot enabled' in sb else 'disabled or unknown'}")
    _out(f"zefosa CA: {_mok_state(system, paths.ca)}")
    if paths.efi_vars.is_dir():
        entries = _boot_entries(system)
        number = _zefosa_entry(entries.text)
        where = "missing" if number is None else f"Boot{number}"
        if number is not None:
            where += " (first)" if entries.order[:1] == [number] else " (not first in BootOrder)"
            where += " (inactive)" if number in entries.inactive else ""
        _out(f"Boot entry: {where}")
    for name in (*REQUIRED, *OPTIONAL):
        _out(f"{zdir / name}: {'present' if (zdir / name).is_file() else 'missing'}")
    conf = zdir / "refind.conf"
    pools = re.findall(r"zbm\.prefer=(\S+)", conf.read_text()) if conf.is_file() else []
    _out(f"refind.conf pool: {pools[0] if pools else 'not set'}")
    layout = re.search(r"Layout:\s*(\S+)", system.run(["kernel-install", "inspect"]).stdout)
    if layout and layout.group(1) != "other":
        _out(
            f"WARNING: kernel-install layout is '{layout.group(1)}', ZFSBootMenu expects 'other' "
            "(/boot/vmlinuz-<ver> and /boot/initramfs-<ver>.img)"
        )
    _out(system.run(["rpm", "-q", *PACKAGES]).stdout.strip())


def _is_dataset(system: System, name: str) -> bool:
    return system.run(["zfs", "list", "-H", "-o", "name", name]).returncode == 0


def cmdline(args: argparse.Namespace, system: System) -> int:
    target = args.target or _root_dataset(system)
    if target is None:
        msg = "the root filesystem is not ZFS; name a boot environment"
        raise UserError(msg)
    for name in (target, args.output):
        if name is not None and not _is_dataset(system, name):
            msg = (
                f"{name} is not a ZFS filesystem. zefosa ZFSBootMenu images are signed and carry "
                "no command line; set ZFSBootMenu options in /boot/efi/EFI/zefosa/refind.conf"
            )
            raise UserError(msg)
    argv = ["zbm-kcl"]
    for value in args.remove:
        argv += ["-r", value]
    for value in args.append:
        argv += ["-a", value]
    if args.edit:
        argv.append("-e")
    if args.delete:
        argv.append("-d")
    if args.output:
        argv += ["-o", args.output]
    return system.execute([*argv, target])


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="zefosa-boot", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    setup_p = commands.add_parser("setup", help="write refind.conf, boot entry, enroll the CA")
    setup_p.add_argument("--pool", help="pool for zbm.prefer (default: pool of the root dataset)")
    setup_p.add_argument("--esp", default="/boot/efi", help="ESP mount point (default /boot/efi)")
    setup_p.add_argument("--no-firmware", action="store_true", help="do not touch boot entries")
    setup_p.add_argument("--no-mok", action="store_true", help="do not request CA enrollment")
    status_p = commands.add_parser("status", help="show the state of the zefosa boot chain")
    status_p.add_argument("--esp", default="/boot/efi")
    kcl = commands.add_parser(
        "cmdline", help="review or change org.zfsbootmenu:commandline (like zbm-kcl)"
    )
    kcl.add_argument("-a", dest="append", action="append", default=[], help="append ARG")
    kcl.add_argument("-r", dest="remove", action="append", default=[], help="remove ARG")
    kcl.add_argument("-e", dest="edit", action="store_true", help="edit in $EDITOR")
    kcl.add_argument("-d", dest="delete", action="store_true", help="inherit (delete) the property")
    kcl.add_argument("-o", dest="output", help="write to this filesystem instead")
    kcl.add_argument("target", nargs="?", help="boot environment (default: root filesystem)")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    system: System | None = None,
    paths: Paths | None = None,
) -> int:
    args = _parser().parse_args(argv)
    system = system or RealSystem()
    paths = paths or Paths()
    try:
        if args.command == "setup":
            setup(args, system, paths)
        elif args.command == "status":
            status(args, system, paths)
        else:
            return cmdline(args, system)
    except UserError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 1
    return 0
