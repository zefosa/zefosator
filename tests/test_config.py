from pathlib import Path

import pytest

from tests.project import FIXTURE_ROOT
from zefosator.config import Plan, load_plan
from zefosator.errors import ConfigError

MINI_VENDOR = """\
defaults:
  image:
    base: "img:{{ os_major }}"
    setup_packages: []
    enable_repos: []
    packages: [gcc]
  dist_tag: "el{{ os_major }}_{{ os_minor }}"
  gpg_keys: []
  kernel_url: "https://example.org/{{ os_version }}/{{ repo }}/{{ file }}"
  packages:
    - {name: kernel, repo: BaseOS}
    - {name: kernel-core, repo: BaseOS, role: core}
    - {name: kernel-modules-core, repo: BaseOS, role: modules}
    - {name: kernel-devel, repo: AppStream, role: devel}
overrides:
  - when: {os_version: ">=10"}
    set:
      image: {packages: [gcc, clang]}
  - when: {os_version: ">=10", arch: aarch64}
    set:
      gpg_keys: [arm-key]
"""

MINI_TARGET = """\
vendor: mini
os_version: "{os_version}"
arch: {arch}
kernel:
  version: 5.14.0
  release: 687.52.1
zfs:
  tag: zfs-2.3.9
  version: 2.3.9
"""


def make_root(
    tmp_path: Path,
    *,
    vendor: str = MINI_VENDOR,
    target: str | None = None,
    os_version: str = "9.8",
    arch: str = "x86_64",
) -> Path:
    (tmp_path / "vendors").mkdir(parents=True)
    (tmp_path / "targets").mkdir()
    (tmp_path / "vendors" / "mini.yaml").write_text(vendor)
    text = target if target is not None else MINI_TARGET.format(os_version=os_version, arch=arch)
    path = tmp_path / "targets" / "t.yaml"
    path.write_text(text)
    return path


def real_plan(name: str, buildid: str | None = None) -> Plan:
    return load_plan(FIXTURE_ROOT / "targets" / f"{name}.yaml", buildid=buildid)


def test_fedora_plan_values() -> None:
    plan = real_plan("fedora-44-x86_64")
    assert plan.target_id == "fedora-44-x86_64"
    assert (plan.os_major, plan.os_minor) == (44, 0)
    assert plan.dist == "fc44"
    assert plan.upstream_release == "200.fc44"
    assert plan.uname_r == "7.2.9-200.fc44.x86_64"
    assert plan.zefosa_release == "200.zefosa.fc44"
    core = plan.role("core")
    assert core.name == "kernel-core"
    assert core.file == "kernel-core-7.2.9-200.fc44.x86_64.rpm"
    assert core.url == (
        "https://kojipkgs.fedoraproject.org/packages/kernel/7.2.9/200.fc44/x86_64/"
        "kernel-core-7.2.9-200.fc44.x86_64.rpm"
    )
    assert plan.image.base == "docker.io/library/fedora:44"
    assert plan.gpg_keys == ()


def test_almalinux9_plan_uses_raw_mirror_and_noarch_file() -> None:
    plan = real_plan("almalinux-9-x86_64")
    assert plan.zefosa_release == "687.52.1.zefosa.el9_8"
    assert plan.uname_r == "5.14.0-687.52.1.el9_8.x86_64"
    devel = plan.role("devel")
    assert devel.url == (
        "https://raw.repo.almalinux.org/almalinux/9.8/AppStream/x86_64/os/Packages/"
        "kernel-devel-5.14.0-687.52.1.el9_8.x86_64.rpm"
    )
    abi = next(p for p in plan.packages if p.name == "kernel-abi-stablelists")
    assert abi.pkg_arch == "noarch"
    assert abi.file == "kernel-abi-stablelists-5.14.0-687.52.1.el9_8.noarch.rpm"
    assert abi.url.endswith("/BaseOS/x86_64/os/Packages/" + abi.file)
    assert plan.gpg_keys == ("/etc/pki/rpm-gpg/RPM-GPG-KEY-AlmaLinux-9",)


def test_almalinux10_plan_uses_main_mirror() -> None:
    plan = real_plan("almalinux-10-x86_64")
    assert plan.dist == "el10_2"
    assert plan.role("core").url.startswith("https://repo.almalinux.org/almalinux/10.2/BaseOS/")


def test_buildid_derived_from_zfs_release(tmp_path: Path) -> None:
    text = MINI_TARGET.format(os_version="9.8", arch="x86_64") + "  release: 2\n"
    plan = load_plan(make_root(tmp_path, target=text))
    assert plan.zfs.release == 2
    assert plan.buildid == "zefosa2"
    assert plan.zefosa_release == "687.52.1.zefosa2.el9_8"


def test_buildid_precedence(tmp_path: Path) -> None:
    text = "buildid: zefosatest\n" + MINI_TARGET.format(os_version="9.8", arch="x86_64")
    path = make_root(tmp_path, target=text)
    assert load_plan(path).buildid == "zefosatest"
    assert load_plan(path, buildid="zefosa7").buildid == "zefosa7"


@pytest.mark.parametrize("bad", ["Zefosa", "zefosa.1", "zefosa-1", "1zefosa", ""])
def test_invalid_buildid_rejected(tmp_path: Path, bad: str) -> None:
    with pytest.raises(ConfigError, match="buildid"):
        load_plan(make_root(tmp_path), buildid=bad)


def test_override_replaces_lists_and_matches_arch(tmp_path: Path) -> None:
    el9 = load_plan(make_root(tmp_path / "a", os_version="9.8"))
    assert el9.image.packages == ("gcc",)
    assert el9.image.base == "img:9"
    el10 = load_plan(make_root(tmp_path / "b", os_version="10.2"))
    assert el10.image.packages == ("gcc", "clang")
    assert el10.gpg_keys == ()
    el10arm = load_plan(make_root(tmp_path / "c", os_version="10.2", arch="aarch64"))
    assert el10arm.gpg_keys == ("arm-key",)


def test_role_must_appear_exactly_once(tmp_path: Path) -> None:
    vendor = MINI_VENDOR.replace(
        "{name: kernel, repo: BaseOS}", "{name: kernel, repo: BaseOS, role: core}"
    )
    with pytest.raises(ConfigError, match="role 'core'"):
        load_plan(make_root(tmp_path, vendor=vendor))


def test_unknown_key_rejected(tmp_path: Path) -> None:
    text = MINI_TARGET.format(os_version="9.8", arch="x86_64") + "colour: blue\n"
    with pytest.raises(ConfigError, match=r"t\.yaml: colour: Extra inputs are not permitted"):
        load_plan(make_root(tmp_path, target=text))


def test_missing_field_names_key(tmp_path: Path) -> None:
    text = MINI_TARGET.format(os_version="9.8", arch="x86_64").replace("  tag: zfs-2.3.9\n", "")
    with pytest.raises(ConfigError, match=r"t\.yaml: zfs\.tag: Field required"):
        load_plan(make_root(tmp_path, target=text))


def test_unquoted_template_mapping_is_reported_at_key(tmp_path: Path) -> None:
    vendor = MINI_VENDOR.replace('base: "img:{{ os_major }}"', "base: {os_major}")
    with pytest.raises(
        ConfigError, match=r"mini\.yaml: .*image\.base: Input should be a valid string"
    ):
        load_plan(make_root(tmp_path, vendor=vendor))


def test_unquoted_double_brace_template_gives_yaml_hint(tmp_path: Path) -> None:
    vendor = MINI_VENDOR.replace('base: "img:{{ os_major }}"', "base: {{ os_major }}")
    with pytest.raises(ConfigError, match=r"mini\.yaml: invalid YAML.*must be quoted"):
        load_plan(make_root(tmp_path, vendor=vendor))


def test_undefined_template_variable_names_key(tmp_path: Path) -> None:
    vendor = MINI_VENDOR.replace("{{ os_version }}/{{ repo }}", "{{ nope }}")
    with pytest.raises(ConfigError, match=r"mini\.yaml: kernel_url .*'nope' is undefined"):
        load_plan(make_root(tmp_path, vendor=vendor))


def test_unquoted_float_os_version_rejected(tmp_path: Path) -> None:
    text = MINI_TARGET.format(os_version="9.8", arch="x86_64").replace('"9.8"', "9.10")
    with pytest.raises(ConfigError, match="os_version: must be quoted"):
        load_plan(make_root(tmp_path, target=text))


def test_integer_values_are_accepted_as_strings(tmp_path: Path) -> None:
    text = MINI_TARGET.format(os_version="9.8", arch="x86_64").replace(
        "release: 687.52.1", "release: 200"
    )
    assert load_plan(make_root(tmp_path, target=text)).kernel_release == "200"


def test_invalid_zfs_version_rejected(tmp_path: Path) -> None:
    text = MINI_TARGET.format(os_version="9.8", arch="x86_64").replace(
        "version: 2.3.9", "version: 2.3.9-rc1"
    )
    with pytest.raises(ConfigError, match=r"zfs\.version"):
        load_plan(make_root(tmp_path, target=text))


def test_missing_patch_file_rejected(tmp_path: Path) -> None:
    text = (
        MINI_TARGET.format(os_version="9.8", arch="x86_64") + "  patches: [patches/missing.patch]\n"
    )
    with pytest.raises(ConfigError, match=r"patches/missing\.patch"):
        load_plan(make_root(tmp_path, target=text))


def test_target_strings_are_templates(tmp_path: Path) -> None:
    text = MINI_TARGET.format(os_version="9.8", arch="x86_64").replace(
        "tag: zfs-2.3.9", 'tag: "zfs-{{ zfs.version }}"'
    )
    assert load_plan(make_root(tmp_path, target=text)).zfs.tag == "zfs-2.3.9"


def test_unknown_vendor_rejected(tmp_path: Path) -> None:
    text = MINI_TARGET.format(os_version="9.8", arch="x86_64").replace(
        "vendor: mini", "vendor: nope"
    )
    with pytest.raises(ConfigError, match=r"vendor 'nope'.*nope\.yaml"):
        load_plan(make_root(tmp_path, target=text))


def test_plan_round_trips_through_json() -> None:
    plan = real_plan("almalinux-9-x86_64")
    assert Plan.model_validate_json(plan.model_dump_json()) == plan


def archive_root(tmp_path: Path, archives: str, *, boot_yaml: bool = False) -> Path:
    target = make_root(
        tmp_path, target=MINI_TARGET.format(os_version="9.8", arch="x86_64") + archives
    )
    if boot_yaml:
        (tmp_path / "boot").mkdir()
        (tmp_path / "boot" / "boot.yaml").write_text(
            (FIXTURE_ROOT / "boot" / "boot.yaml").read_text()
        )
    return target


def test_archive_paths_are_rendered_from_the_plan(tmp_path: Path) -> None:
    target = archive_root(
        tmp_path,
        """\
archives:
  almalinux9-x86_64-zfs-userspace-packages:
    - "{{ target_id }}/zfs/zfs-{{ zfs.version }}-{{ zfs.release }}.el{{ os_major }}.{{ arch }}.rpm"
    - "{{ target_id }}/kernel/kernel-{{ kernel_version }}-{{ zefosa_release }}.{{ arch }}.rpm"
    - "{{ target_id }}/zfs/kmod-zfs-devel-{{ uname_r }}-{{ zfs.version }}-0.el9.x86_64.rpm"
    - "{{ target_id }}/boot/shim-x64-{{ dist }}.rpm"
""",
    )
    plan = load_plan(target)
    assert [(a.name, a.files) for a in plan.archives] == [
        (
            "almalinux9-x86_64-zfs-userspace-packages",
            (
                "mini-9.8-x86_64/zfs/zfs-2.3.9-0.el9.x86_64.rpm",
                "mini-9.8-x86_64/kernel/kernel-5.14.0-687.52.1.zefosa.el9_8.x86_64.rpm",
                "mini-9.8-x86_64/zfs/kmod-zfs-devel-5.14.0-687.52.1.el9_8.x86_64-2.3.9-0.el9.x86_64.rpm",
                "mini-9.8-x86_64/boot/shim-x64-el9_8.rpm",
            ),
        )
    ]


def test_target_without_archives_has_none(tmp_path: Path) -> None:
    assert load_plan(make_root(tmp_path)).archives == ()


def test_archive_paths_can_name_the_common_boot_packages(tmp_path: Path) -> None:
    def nvr(pin: str) -> str:
        return f"{{{{ boot_packages.{pin}.version }}}}-{{{{ boot_packages.{pin}.release }}}}"

    target = archive_root(
        tmp_path,
        f"""\
archives:
  boot:
    - "boot/zefosa-zfsbootmenu-{nvr("zfsbootmenu")}.x86_64.rpm"
    - "boot/zefosa-refind-{nvr("refind")}.x86_64.rpm"
    - "boot/zefosa-secureboot-ca-{nvr("ca")}.noarch.rpm"
    - "boot/zefosa-boot-{nvr("tools")}.noarch.rpm"
""",
        boot_yaml=True,
    )
    (archive,) = load_plan(target).archives
    assert archive.files == (
        "boot/zefosa-zfsbootmenu-3.1.0-1.x86_64.rpm",
        "boot/zefosa-refind-0.14.2-1.x86_64.rpm",
        "boot/zefosa-secureboot-ca-1-1.noarch.rpm",
        "boot/zefosa-boot-1.0-1.noarch.rpm",
    )


@pytest.mark.parametrize(
    ("archives", "message"),
    [
        ('archives:\n  "bad name!": ["a.rpm"]\n', r"archives: archive name 'bad name!'"),
        ("archives:\n  empty: []\n", r"archives: archive 'empty' lists no files"),
        ('archives:\n  a: ["/abs/x.rpm"]\n', r"archives\.a\[0\]: '/abs/x\.rpm' must be relative"),
        (
            'archives:\n  a: ["{{ target_id }}/../x.rpm"]\n',
            r"archives\.a\[0\]: .* must not contain '\.\.'",
        ),
        ('archives:\n  a: ["{{ target_id }}/"]\n', r"archives\.a\[0\]: .* must name a file"),
        (
            'archives:\n  a: ["x/z.rpm", "y/z.rpm"]\n',
            r"archives\.a\[1\]: 'y/z\.rpm' has the same file name as 'x/z\.rpm'",
        ),
        ('archives:\n  a: ["{{ nope }}.rpm"]\n', r"archives\.a\[0\]: 'nope' is undefined"),
    ],
)
def test_invalid_archive_definitions_are_rejected(
    tmp_path: Path, archives: str, message: str
) -> None:
    with pytest.raises(ConfigError, match=message):
        load_plan(archive_root(tmp_path, archives))
