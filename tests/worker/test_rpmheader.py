import stat
from pathlib import Path

import pytest

from zefosator.errors import BuildError
from zefosator.worker.rpmheader import (
    FILE_CONFIG,
    FILE_DOC,
    FILE_GHOST,
    FILE_LICENSE,
    FILE_NOREPLACE,
    SENSE_EQUAL,
    SENSE_GREATER,
    SENSE_SCRIPT_PRE,
    DepKind,
    Header,
    ScriptKind,
    parse_header_xml,
)

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


def load(name: str) -> Header:
    return parse_header_xml((FIXTURES / f"{name}.xml").read_bytes(), source=name)


def test_meta_package_has_no_files_and_rich_dependency() -> None:
    header = load("f44-kernel")
    assert (header.name, header.version, header.release, header.arch) == (
        "kernel",
        "7.2.9",
        "200.fc44",
        "x86_64",
    )
    assert header.epoch is None
    assert header.files == ()
    requires = {d.name for d in header.deps[DepKind.REQUIRE]}
    assert "kernel-core-uname-r" in requires
    assert any(name.startswith("((kernel-modules-extra-uname-r") for name in requires)
    provides = {(d.name, d.version) for d in header.deps[DepKind.PROVIDE]}
    assert ("kernel", "7.2.9-200.fc44") in provides


def test_metadata_fields() -> None:
    header = load("f44-kernel-core")
    assert header.summary == "The Linux kernel"
    assert header.url == "https://www.kernel.org/"
    assert header.bugurl == "https://bugz.fedoraproject.org/kernel"
    assert header.vendor == "Fedora Project"
    assert header.payload_compressor == "xz"
    assert header.payload_flags == "3T"
    assert header.digest_algo == 8
    assert header.description.startswith("The kernel package contains the Linux kernel")


def test_dependency_flags_and_versions() -> None:
    header = load("f44-kernel-core")
    dracut = next(d for d in header.deps[DepKind.REQUIRE] if d.name == "dracut")
    assert dracut.flags == SENSE_SCRIPT_PRE | SENSE_GREATER | SENSE_EQUAL
    assert dracut.version == "027"
    conflicts = {d.name for d in header.deps[DepKind.CONFLICT]}
    assert conflicts == {"xfsprogs", "xorg-x11-drv-vmmouse"}
    assert header.deps[DepKind.RECOMMEND][0].name == "linux-firmware"


def test_scripts_keep_interpreter_and_body() -> None:
    header = load("f44-kernel-core")
    kinds = {s.kind for s in header.scripts}
    assert kinds == {ScriptKind.POST, ScriptKind.PREUN, ScriptKind.POSTTRANS}
    posttrans = next(s for s in header.scripts if s.kind is ScriptKind.POSTTRANS)
    assert posttrans.prog == ("/bin/sh",)
    assert posttrans.body is not None
    assert "/bin/kernel-install add 7.2.9-200.fc44.x86_64" in posttrans.body
    post = next(s for s in header.scripts if s.kind is ScriptKind.POST)
    assert post.body is not None
    assert post.body.startswith("\nmkdir -p /var/lib/rpm-state/kernel")


def test_ghost_and_license_files() -> None:
    files = {f.path: f for f in load("f44-kernel-core").files}
    vmlinuz = files["/boot/vmlinuz-7.2.9-200.fc44.x86_64"]
    assert vmlinuz.flags & FILE_GHOST
    assert vmlinuz.verifyflags == 0xFFFFFFD8
    assert vmlinuz.digest == ""
    license_file = files["/usr/share/licenses/kernel-core/COPYING-7.2.9-200.fc44"]
    assert license_file.flags == FILE_LICENSE
    assert len(license_file.digest) == 64
    assert stat.S_ISDIR(files["/lib/modules"].mode)
    assert (files["/lib/modules"].user, files["/lib/modules"].group) == ("root", "root")


def test_symlink_doc_and_directories() -> None:
    files = load("f44-kernel-modules-core").files
    links = [f for f in files if stat.S_ISLNK(f.mode)]
    assert links
    assert all(f.linkto for f in links)
    assert any(f.flags == FILE_DOC for f in files)


def test_config_noreplace_and_verify_flags() -> None:
    extra = load("f44-kernel-modules-extra").files
    assert any(f.flags == FILE_CONFIG | FILE_NOREPLACE for f in extra)
    devel = load("f44-kernel-devel").files
    assert {f.verifyflags for f in devel} == {0xFFFFFFDF}


def test_noarch_and_signed_headers_parse() -> None:
    assert load("el9-kernel-abi-stablelists").arch == "noarch"
    core = load("el9-kernel-core")
    assert core.release == "687.52.1.el9_8"
    provides = {d.name for d in core.deps[DepKind.PROVIDE]}
    assert "almalinux(kernel-sig-key)" in provides


def test_changelog_entries_in_header_order() -> None:
    entries = load("f44-kernel").changelog
    assert len(entries) == 3
    assert entries[0].time >= entries[-1].time
    assert entries[0].name


SYNTHETIC = """<rpmHeader>
  <rpmTag name="Name"><string>pkg</string></rpmTag>
  <rpmTag name="Epoch"><integer>2</integer></rpmTag>
  <rpmTag name="Version"><string>1.0</string></rpmTag>
  <rpmTag name="Release"><string>1</string></rpmTag>
  <rpmTag name="Arch"><string>x86_64</string></rpmTag>
  <rpmTag name="Summary"><string>s</string></rpmTag>
  <rpmTag name="Description"><string>uses 100% of %{name}</string></rpmTag>
  <rpmTag name="License"><string>MIT</string></rpmTag>
  <rpmTag name="Payloadcompressor"><string>xz</string></rpmTag>
  <rpmTag name="Payloadflags"><string>2</string></rpmTag>
  <rpmTag name="Postinprog"><string>/sbin/ldconfig</string></rpmTag>
  {extra}
</rpmHeader>"""


def test_script_without_body_and_epoch() -> None:
    header = parse_header_xml(SYNTHETIC.replace("{extra}", "").encode(), source="synthetic")
    assert header.epoch == 2
    assert header.description == "uses 100% of %{name}"
    assert header.scripts[0].kind is ScriptKind.POST
    assert header.scripts[0].prog == ("/sbin/ldconfig",)
    assert header.scripts[0].body is None
    assert header.files == ()


def test_unknown_tag_fails_with_tag_and_source() -> None:
    xml = SYNTHETIC.replace("{extra}", '<rpmTag name="Triggername"><string>x</string></rpmTag>')
    with pytest.raises(BuildError, match="unsupported tag Triggername in synthetic"):
        parse_header_xml(xml.encode(), source="synthetic")


def test_device_files_are_rejected() -> None:
    extra = """
  <rpmTag name="Basenames"><string>dev0</string></rpmTag>
  <rpmTag name="Dirnames"><string>/dev/</string></rpmTag>
  <rpmTag name="Dirindexes"><integer>0</integer></rpmTag>
  <rpmTag name="Filemodes"><integer>8630</integer></rpmTag>
  <rpmTag name="Fileusername"><string>root</string></rpmTag>
  <rpmTag name="Filegroupname"><string>root</string></rpmTag>
  <rpmTag name="Filesizes"><integer>0</integer></rpmTag>
  <rpmTag name="Filedigests"><string></string></rpmTag>
  <rpmTag name="Filelinktos"><string></string></rpmTag>
  <rpmTag name="Fileflags"><integer>0</integer></rpmTag>
  <rpmTag name="Fileverifyflags"><integer>4294967295</integer></rpmTag>
  <rpmTag name="Filelangs"><string></string></rpmTag>
  <rpmTag name="Filemtimes"><integer>0</integer></rpmTag>"""
    with pytest.raises(BuildError, match=r"unsupported file type.*/dev/dev0"):
        parse_header_xml(SYNTHETIC.replace("{extra}", extra).encode(), source="synthetic")
