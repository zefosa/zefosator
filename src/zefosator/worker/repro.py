"""Reproducible RPM builds: the same inputs built in the same image give byte-identical RPMs.

rpmbuild takes build times and file times from SOURCE_DATE_EPOCH, which zefosator derives from
the inputs only (the zfs.tag commit time, the vendor kernel RPMs), and records a fixed build host.
"""

import struct
from collections.abc import Callable, Iterable
from pathlib import Path

from zefosator.worker.rpmheader import read_header

DAY = 86400
RPM_MACROS = (
    "%use_source_date_epoch_as_buildtime 1\n"
    "%clamp_mtime_to_source_date_epoch 1\n"
    "%source_date_epoch_from_changelog 0\n"
    "%_buildhost zefosa\n"
)
# OpenZFS's rpm rules build in `mktemp -t -d zfs-build-$USER-XXXXXXXX`. That random path reaches
# the debug info and so every ELF build ID; give exactly that call a fixed, emptied directory.
_MKTEMP_WRAPPER = """\
#!/bin/sh
case "$*" in
"-t -d zfs-build-"*) rm -rf '{fixed}' && mkdir -p '{fixed}' && echo '{fixed}' ;;
*) exec /usr/bin/mktemp "$@" ;;
esac
"""
_LEAD_SIZE = 96
_LEAD_MAGIC = b"\xed\xab\xee\xdb"
_HEADER_MAGIC = b"\x8e\xad\xe8\x01"
_HEADER_INTRO = 16  # magic, reserved, entry count, data size
_RPMTAG_BUILDTIME = 1006
_RPM_INT32_TYPE = 4


def write_rpm_macros(home: Path) -> Path:
    path = home / ".rpmmacros"
    path.write_text(RPM_MACROS)
    return path


def install_mktemp_wrapper(bindir: Path, fixed: Path) -> Path:
    """A `mktemp` to put first on PATH for OpenZFS's `make rpm-*`."""
    bindir.mkdir(parents=True, exist_ok=True)
    wrapper = bindir / "mktemp"
    wrapper.write_text(_MKTEMP_WRAPPER.format(fixed=fixed))
    wrapper.chmod(0o755)
    return wrapper


def rpm_buildtime(path: Path) -> int:
    """BUILDTIME from the RPM's main header, without the rpm tool."""
    with path.open("rb") as handle:
        if handle.read(_LEAD_SIZE)[:4] != _LEAD_MAGIC:
            msg = f"{path.name}: not an RPM"
            raise ValueError(msg)
        for which in ("signature", "main"):
            intro = handle.read(_HEADER_INTRO)
            if len(intro) != _HEADER_INTRO or intro[:4] != _HEADER_MAGIC:
                msg = f"{path.name}: not an RPM (bad {which} header)"
                raise ValueError(msg)
            count, size = struct.unpack(">II", intro[8:])
            index = handle.read(16 * count)
            store = handle.read(size)
            if which == "signature":
                handle.read((8 - (_HEADER_INTRO + 16 * count + size) % 8) % 8)  # padding
    for tag, kind, offset, _ in struct.iter_unpack(">iiii", index):
        if tag == _RPMTAG_BUILDTIME and kind == _RPM_INT32_TYPE:
            (value,) = struct.unpack(">I", store[offset : offset + 4])
            return int(value)
    msg = f"{path.name}: no build time in the RPM header"
    raise ValueError(msg)


def _file_mtimes(path: Path) -> list[int]:
    return [f.mtime for f in read_header(path).files]


def kernel_epoch(
    zfs_epoch: int,
    vendor_rpms: Iterable[Path],
    *,
    buildtime: Callable[[Path], int] = rpm_buildtime,
    file_mtimes: Callable[[Path], Iterable[int]] = _file_mtimes,
) -> int:
    """No older than the ZFS sources or any vendor file, so clamping never changes vendor files."""
    times = [zfs_epoch]
    for rpm in vendor_rpms:
        times.append(buildtime(rpm))
        times.extend(file_mtimes(rpm))
    return max(times)


def changelog_time(epoch: int) -> int:
    """Noon of the epoch's day: rpm keeps only the date of a changelog entry."""
    return epoch // DAY * DAY + DAY // 2
