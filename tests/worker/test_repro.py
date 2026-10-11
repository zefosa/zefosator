import shutil
import subprocess
from pathlib import Path

import pytest

from tests.rpmfiles import fake_rpm
from zefosator.worker.repro import (
    changelog_time,
    install_mktemp_wrapper,
    kernel_epoch,
    rpm_buildtime,
    write_rpm_macros,
)

ROOT = Path(__file__).resolve().parents[2]


def test_rpm_macros_make_builds_reproducible(tmp_path: Path) -> None:
    macros = write_rpm_macros(tmp_path).read_text().splitlines()
    assert macros == [
        "%use_source_date_epoch_as_buildtime 1",
        "%clamp_mtime_to_source_date_epoch 1",
        "%source_date_epoch_from_changelog 0",
        "%_buildhost zefosa",
    ]


def run(wrapper: Path, *args: str) -> str:
    return subprocess.run(
        [str(wrapper), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def test_mktemp_wrapper_gives_openzfs_a_fixed_empty_directory(tmp_path: Path) -> None:
    fixed = tmp_path / "rpmbuild"
    wrapper = install_mktemp_wrapper(tmp_path / "bin", fixed)
    assert wrapper == tmp_path / "bin" / "mktemp"
    (fixed / "BUILD").mkdir(parents=True)
    (fixed / "BUILD" / "stale").write_text("left over")
    assert run(wrapper, "-t", "-d", "zfs-build-root-XXXXXXXX") == str(fixed)
    assert fixed.is_dir()
    assert list(fixed.iterdir()) == []


@pytest.mark.parametrize(
    "args",
    [
        ("-t", "-d", "other-XXXXXXXX"),
        ("-d", "-p", "{fixed}", "zfs-build-root-XXXXXXXX"),  # rpm helpers inside the build dir
        (
            "-p",
            "{fixed}",
        ),
    ],
)
def test_mktemp_wrapper_passes_every_other_call_to_mktemp(
    tmp_path: Path, args: tuple[str, ...]
) -> None:
    fixed = tmp_path / "rpmbuild"
    fixed.mkdir()
    wrapper = install_mktemp_wrapper(tmp_path / "bin", fixed)
    made = Path(run(wrapper, *(a.format(fixed=fixed) for a in args)))
    assert made != fixed
    assert made.exists()
    made.unlink() if made.is_file() else made.rmdir()


def test_rpm_buildtime_reads_the_main_header(tmp_path: Path) -> None:
    assert rpm_buildtime(fake_rpm(tmp_path / "a.rpm", buildtime=1787329187)) == 1787329187


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (b"not an rpm at all" * 10, "not an RPM"),
        (b"\xed\xab\xee\xdb" + b"\x00" * 92 + b"garbage!" * 4, "not an RPM"),
    ],
)
def test_rpm_buildtime_rejects_other_files(tmp_path: Path, data: bytes, message: str) -> None:
    path = tmp_path / "x.rpm"
    path.write_bytes(data)
    with pytest.raises(ValueError, match=message):
        rpm_buildtime(path)


def test_rpm_buildtime_requires_the_tag(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="no build time"):
        rpm_buildtime(fake_rpm(tmp_path / "a.rpm", buildtime=None))


@pytest.mark.skipif(shutil.which("rpm") is None, reason="rpm required")
def test_rpm_buildtime_agrees_with_rpm() -> None:
    rpms = sorted((ROOT / "cache" / "downloads").glob("*.rpm"))
    if not rpms:
        pytest.skip("no downloaded RPMs to compare with")
    for path in rpms:
        expected = subprocess.run(
            ["rpm", "-qp", "--qf", "%{BUILDTIME}", str(path)],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        assert rpm_buildtime(path) == int(expected)


def test_kernel_epoch_is_never_older_than_any_input(tmp_path: Path) -> None:
    a, b = tmp_path / "a.rpm", tmp_path / "b.rpm"
    buildtimes = {a: 200, b: 300}
    mtimes = {a: [150, 350], b: [250]}
    epoch = kernel_epoch(
        100, [a, b], buildtime=buildtimes.__getitem__, file_mtimes=mtimes.__getitem__
    )
    assert epoch == 350
    assert (
        kernel_epoch(999, [a, b], buildtime=buildtimes.__getitem__, file_mtimes=mtimes.__getitem__)
        == 999
    )


def test_changelog_time_is_noon_of_the_epoch_day() -> None:
    assert changelog_time(1791056615) == 1790985600 + 43200
