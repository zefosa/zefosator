from pathlib import Path

import pytest

from tests.project import FIXTURE_ROOT
from zefosator.config import load_boot_config, load_plan
from zefosator.errors import ConfigError


def test_fedora_boot_plan() -> None:
    boot = load_plan(FIXTURE_ROOT / "targets" / "fedora-44-x86_64.yaml").boot
    assert boot is not None
    assert boot.common
    assert boot.esp_dir == "fedora"
    assert (boot.shim_version, boot.shim_release) == ("16.1", "7")
    assert boot.shim_file == "shim-x64-16.1-7.x86_64.rpm"
    assert boot.shim_url == (
        "https://kojipkgs.fedoraproject.org/packages/shim/16.1/7/x86_64/shim-x64-16.1-7.x86_64.rpm"
    )
    assert boot.zefosa_shim_release == "7.zefosa"
    assert boot.cloud_image.endswith("Fedora-Cloud-Base-Generic-44-1.7.x86_64.qcow2")


def test_almalinux_boot_plans() -> None:
    el9 = load_plan(FIXTURE_ROOT / "targets" / "almalinux-9-x86_64.yaml").boot
    assert el9 is not None
    assert not el9.common
    assert el9.esp_dir == "almalinux"
    assert el9.shim_url == (
        "https://raw.repo.almalinux.org/almalinux/9.8/BaseOS/x86_64/os/Packages/"
        "shim-x64-16.1-7.el9.alma.1.x86_64.rpm"
    )
    assert el9.zefosa_shim_release == "7.el9.alma.1.zefosa"
    el10 = load_plan(FIXTURE_ROOT / "targets" / "almalinux-10-x86_64.yaml").boot
    assert el10 is not None
    assert el10.shim_url.startswith("https://repo.almalinux.org/almalinux/10.2/BaseOS/")


def test_boot_config_file() -> None:
    config = load_boot_config(FIXTURE_ROOT)
    assert config.zfsbootmenu.version == "3.1.0"
    assert config.zfsbootmenu.tag == "v3.1.0"
    assert "efibootmgr" in config.zfsbootmenu.recovery_tools
    assert (
        config.refind.sha256 == "b00339df78acc802151275a0afddfa3d368ba5aaaa357cea44681b781033fc8b"
    )
    assert (config.refind.version, config.refind.upstream_release) == ("0.14.2", "5.fc44")
    assert config.refind.release == 1
    assert config.refind.url == (
        "https://kojipkgs.fedoraproject.org/packages/rEFInd/0.14.2/5.fc44/noarch/"
        "rEFInd-unsigned-x64-0.14.2-5.fc44.noarch.rpm"
    )


def test_refind_url_is_rendered_from_its_pins(tmp_path: Path) -> None:
    text = (FIXTURE_ROOT / "boot" / "boot.yaml").read_text()
    start, end = text.index("refind:"), text.index("ca:")
    refind = """\
refind:
  version: "9.9.9"
  upstream_release: "3.fc50"
  release: 2
  url: "https://example.org/{{ refind.version }}/{{ refind.upstream_release }}/r.rpm"
  sha256: b00339df78acc802151275a0afddfa3d368ba5aaaa357cea44681b781033fc8b
"""
    (tmp_path / "boot").mkdir()
    (tmp_path / "boot" / "boot.yaml").write_text(text[:start] + refind + text[end:])
    assert load_boot_config(tmp_path).refind.url == "https://example.org/9.9.9/3.fc50/r.rpm"


@pytest.mark.parametrize("tag", ["-x", "v3 1", ""])
def test_invalid_zfsbootmenu_tags_rejected(tmp_path: Path, tag: str) -> None:
    text = (FIXTURE_ROOT / "boot" / "boot.yaml").read_text().replace("tag: v3.1.0", f'tag: "{tag}"')
    (tmp_path / "boot").mkdir()
    (tmp_path / "boot" / "boot.yaml").write_text(text)
    with pytest.raises(ConfigError, match=r"zfsbootmenu\.tag"):
        load_boot_config(tmp_path)


def test_zfsbootmenu_tag_may_be_a_commit(tmp_path: Path) -> None:
    commit = "7174e420590a270ece3c6426bdb1d5cdd9ef27b0"
    text = (
        (FIXTURE_ROOT / "boot" / "boot.yaml").read_text().replace("tag: v3.1.0", f"tag: {commit}")
    )
    (tmp_path / "boot").mkdir()
    (tmp_path / "boot" / "boot.yaml").write_text(text)
    assert load_boot_config(tmp_path).zfsbootmenu.tag == commit


def test_target_without_boot_section_has_no_boot_plan(tmp_path: Path) -> None:
    for d in ("vendors", "targets"):
        (tmp_path / d).mkdir()
    (tmp_path / "vendors" / "fedora.yaml").write_text(
        (FIXTURE_ROOT / "vendors" / "fedora.yaml").read_text()
    )
    text = (FIXTURE_ROOT / "targets" / "fedora-44-x86_64.yaml").read_text()
    text = text[: text.index("boot:")]
    (tmp_path / "targets" / "t.yaml").write_text(text)
    assert load_plan(tmp_path / "targets" / "t.yaml").boot is None


def test_invalid_drop_requires_regex(tmp_path: Path) -> None:
    for d in ("vendors", "targets"):
        (tmp_path / d).mkdir()
    vendor = (
        (FIXTURE_ROOT / "vendors" / "fedora.yaml").read_text().replace("'^grub2-'", "'grub2-('")
    )
    (tmp_path / "vendors" / "fedora.yaml").write_text(vendor)
    (tmp_path / "targets" / "t.yaml").write_text(
        (FIXTURE_ROOT / "targets" / "fedora-44-x86_64.yaml").read_text()
    )
    with pytest.raises(ConfigError, match="drop_requires"):
        load_plan(tmp_path / "targets" / "t.yaml")
