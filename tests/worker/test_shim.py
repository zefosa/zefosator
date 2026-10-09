import hashlib
import subprocess
from pathlib import Path

import pytest

from tests.worker.upstream import needs_rpmbuild
from zefosator.config import load_plan
from zefosator.errors import BuildError
from zefosator.worker.rpmheader import (
    SENSE_EQUAL,
    SENSE_GREATER,
    Dependency,
    DepKind,
    read_header,
)
from zefosator.worker.shim import (
    ZEFOSA_CSV,
    ShimPaths,
    build_shim,
    collect_vendor_efi,
    filter_requires,
)

ROOT = Path(__file__).resolve().parents[2]
DOWNLOADS = ROOT / "cache" / "downloads"


def test_csv_is_utf16le_with_bom_pointing_at_zefosa() -> None:
    assert ZEFOSA_CSV.startswith(b"\xff\xfe")
    assert ZEFOSA_CSV[2:].decode("utf-16-le") == (
        "shimx64.efi,zefosa,,This is the boot entry for zefosa\n"
    )


def test_filter_requires_drops_only_grub() -> None:
    deps = (
        Dependency("almalinux(grub2-sig-key)", SENSE_GREATER | SENSE_EQUAL, "202303"),
        Dependency("almalinux(kernel-sig-key)", SENSE_GREATER | SENSE_EQUAL, "202303"),
        Dependency("grub2-efi-x64", SENSE_GREATER | SENSE_EQUAL, "2.06-63"),
        Dependency("mokutil", SENSE_GREATER | SENSE_EQUAL, "1:0.3.0-1"),
        Dependency("/bin/sh", 288, ""),
        Dependency("rpmlib(FileDigests)", (1 << 24) | 10, "4.6.0-1"),
    )
    kept = filter_requires(deps, ("^grub2-", "grub2-sig-key"))
    assert [d.name for d in kept] == ["almalinux(kernel-sig-key)", "mokutil"]


def test_collect_vendor_efi_handles_both_layouts(tmp_path: Path) -> None:
    fedora = tmp_path / "fedora"
    for d in ("usr/lib/efi/shim/16.1-7/EFI/fedora", "usr/lib/efi/shim/16.1-7/EFI/BOOT"):
        (fedora / d).mkdir(parents=True)
    for name in ("shimx64.efi", "mmx64.efi", "shim.efi", "BOOTX64.CSV"):
        (fedora / "usr/lib/efi/shim/16.1-7/EFI/fedora" / name).write_text(name)
    for name in ("BOOTX64.EFI", "fbx64.efi"):
        (fedora / "usr/lib/efi/shim/16.1-7/EFI/BOOT" / name).write_text(name)
    found = collect_vendor_efi(fedora, "fedora")
    assert sorted(found) == [
        "/boot/efi/EFI/BOOT/BOOTX64.EFI",
        "/boot/efi/EFI/BOOT/fbx64.efi",
        "/boot/efi/EFI/zefosa/mmx64.efi",
        "/boot/efi/EFI/zefosa/shim.efi",
        "/boot/efi/EFI/zefosa/shimx64.efi",
    ]
    alma = tmp_path / "alma"
    (alma / "boot/efi/EFI/almalinux").mkdir(parents=True)
    (alma / "boot/efi/EFI/BOOT").mkdir(parents=True)
    for name in ("shimx64.efi", "shimx64-almalinux.efi", "mmx64.efi"):
        (alma / "boot/efi/EFI/almalinux" / name).write_text(name)
    (alma / "boot/efi/EFI/BOOT/BOOTX64.EFI").write_text("b")
    assert "/boot/efi/EFI/zefosa/shimx64-almalinux.efi" in collect_vendor_efi(alma, "almalinux")


def test_collect_vendor_efi_requires_shim(tmp_path: Path) -> None:
    (tmp_path / "boot/efi/EFI/almalinux").mkdir(parents=True)
    with pytest.raises(BuildError, match=r"shimx64\.efi"):
        collect_vendor_efi(tmp_path, "almalinux")


def _payload(rpm: Path, dest: Path) -> None:
    dest.mkdir(parents=True)
    subprocess.run(["sh", "-ec", f"rpm2cpio '{rpm}' | cpio -idm --quiet"], cwd=dest, check=True)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.container
@needs_rpmbuild
@pytest.mark.parametrize(
    "target", ["fedora-44-x86_64", "almalinux-9-x86_64", "almalinux-10-x86_64"]
)
def test_build_shim_from_real_vendor_rpm(tmp_path: Path, target: str) -> None:
    plan = load_plan(ROOT / "targets" / f"{target}.yaml")
    assert plan.boot is not None
    vendor = DOWNLOADS / plan.boot.shim_file
    if not vendor.is_file():
        pytest.skip(f"not downloaded: {vendor.name}")
    out = build_shim(
        plan,
        vendor,
        ShimPaths(workdir=tmp_path / "work", outdir=tmp_path / "out", logs=tmp_path / "logs"),
    )
    assert out.name == f"shim-x64-16.1-{plan.boot.zefosa_shim_release}.x86_64.rpm"
    header = read_header(out)
    requires = {d.name for d in header.dep_list(DepKind.REQUIRE)}
    assert not [r for r in requires if "grub2" in r]
    assert "mokutil" in requires
    conflicts = {d.name for d in header.dep_list(DepKind.CONFLICT)}
    assert "grub2-efi-x64" in conflicts
    _payload(vendor, tmp_path / "vendor")
    _payload(out, tmp_path / "ours")
    ours = tmp_path / "ours" / "boot/efi/EFI/zefosa"
    assert (ours / "BOOTX64.CSV").read_bytes() == ZEFOSA_CSV
    vendor_shim = next((tmp_path / "vendor").rglob(f"EFI/{plan.boot.esp_dir}/shimx64.efi"))
    assert _sha(ours / "shimx64.efi") == _sha(vendor_shim)
