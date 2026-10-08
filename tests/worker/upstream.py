"""Build small synthetic 'upstream' kernel RPMs with rpmbuild for repackaging tests."""

import shutil
import subprocess
from pathlib import Path

import pytest

UNAME_R = "7.2.9-200.fc44.x86_64"

needs_rpmbuild = pytest.mark.skipif(
    shutil.which("rpmbuild") is None or shutil.which("rpm2cpio") is None,
    reason="rpmbuild and rpm2cpio are required",
)

SPEC = r"""
%global debug_package %{nil}
%global _build_id_links none
%global _binary_payload w3T.xzdio
%global uname_r @UNAME_R@
Name: kernel
Version: 7.2.9
Release: 200.fc44
Summary: The Linux kernel
License: GPL-2.0-only
URL: https://www.kernel.org/
Requires: kernel-core-uname-r = %{uname_r}
Requires: kernel-modules-core-uname-r = %{uname_r}
Provides: installonlypkg(kernel)

%description
The kernel meta package

%package core
Summary: The Linux kernel
Requires(pre): dracut >= 027
Requires(pre): ((linux-firmware >= 20150904) if linux-firmware)
Provides: kernel = %{version}-%{release}
Provides: kernel-core-uname-r = %{uname_r}
Provides: kernel-uname-r = %{uname_r}
Conflicts: xfsprogs < 4.3.0-1

%description core
The core kernel, uses 100%% of nothing.

%package modules-core
Summary: Core kernel modules
Requires: kernel-uname-r = %{uname_r}
Provides: kernel-modules-core-uname-r = %{uname_r}
Provides: kmod(dummy.ko)

%description modules-core
Core modules.

%package devel
Summary: Development package
Provides: kernel-devel-uname-r = %{uname_r}

%description devel
Headers.

%package devel-matched
Summary: Meta package
Requires: kernel-core = %{version}-%{release}
Requires: kernel-devel = %{version}-%{release}

%description devel-matched
Matched devel.

%package abi-stablelists
Summary: kABI lists
BuildArch: noarch

%description abi-stablelists
Lists.

%install
mkdir -p %{buildroot}/boot %{buildroot}/lib/modules/%{uname_r}/kernel/fs
mkdir -p %{buildroot}/usr/share/licenses/kernel-core %{buildroot}/usr/src/kernels/%{uname_r}
mkdir -p %{buildroot}/lib/modules/kabi-current %{buildroot}/etc/modprobe.d
echo vmlinuz > %{buildroot}/lib/modules/%{uname_r}/vmlinuz
dd if=/dev/zero of=%{buildroot}/boot/initramfs-%{uname_r}.img bs=1k count=64 status=none
touch %{buildroot}/boot/vmlinuz-%{uname_r}
echo license > %{buildroot}/usr/share/licenses/kernel-core/COPYING
echo module | xz --check=crc32 > %{buildroot}/lib/modules/%{uname_r}/kernel/fs/dummy.ko.xz
ln -s /usr/src/kernels/%{uname_r} %{buildroot}/lib/modules/%{uname_r}/build
echo readme > %{buildroot}/lib/modules/%{uname_r}/README
echo header > %{buildroot}/usr/src/kernels/%{uname_r}/a.h
ln %{buildroot}/usr/src/kernels/%{uname_r}/a.h %{buildroot}/usr/src/kernels/%{uname_r}/b.h
echo blacklist > %{buildroot}/etc/modprobe.d/blacklist.conf
echo list > %{buildroot}/lib/modules/kabi-current/kabi_stablelist
chmod 0600 %{buildroot}/boot/initramfs-%{uname_r}.img

%post core
mkdir -p /var/lib/rpm-state/kernel
touch /var/lib/rpm-state/kernel/installing_core_%{uname_r}

%posttrans core
echo "100%%" > /dev/null
/bin/kernel-install add %{uname_r} /lib/modules/%{uname_r}/vmlinuz || exit 0

%posttrans modules-core
/sbin/depmod -a %{uname_r}

%files

%files core
%ghost %attr(0600, root, root) /boot/initramfs-%{uname_r}.img
%ghost /boot/vmlinuz-%{uname_r}
%dir /lib/modules
%dir /lib/modules/%{uname_r}
/lib/modules/%{uname_r}/vmlinuz
%license /usr/share/licenses/kernel-core/COPYING

%files modules-core
%dir /lib/modules
%dir /lib/modules/%{uname_r}
%dir /lib/modules/%{uname_r}/kernel
/lib/modules/%{uname_r}/kernel/fs
/lib/modules/%{uname_r}/build
%doc /lib/modules/%{uname_r}/README
%config(noreplace) /etc/modprobe.d/blacklist.conf

%files devel
%defverify(not mtime)
/usr/src/kernels/%{uname_r}

%files devel-matched
%config(noreplace) /etc/modprobe.d/blacklist.conf

%files abi-stablelists
/lib/modules/kabi-current

%changelog
* Sat Oct 03 2026 Upstream Packager <u@example.org> - 7.2.9-200
- Linux v7.2.9 with 100%% more tests
"""

PACKAGES = (
    "kernel",
    "kernel-core",
    "kernel-modules-core",
    "kernel-devel",
    "kernel-devel-matched",
    "kernel-abi-stablelists",
)


def build_upstream(workdir: Path) -> dict[str, Path]:
    """Build the synthetic upstream packages; return name -> rpm path."""
    spec = workdir / "upstream.spec"
    spec.write_text(SPEC.replace("@UNAME_R@", UNAME_R))
    out = workdir / "upstream"
    subprocess.run(
        [
            "rpmbuild",
            "-bb",
            "--quiet",
            "--target",
            "x86_64",
            "--define",
            f"_topdir {workdir / 'upstream-build'}",
            "--define",
            f"_rpmdir {out}",
            "--define",
            "_build_name_fmt %%{NAME}-%%{VERSION}-%%{RELEASE}.%%{ARCH}.rpm",
            str(spec),
        ],
        check=True,
        capture_output=True,
    )
    result = {}
    for name in PACKAGES:
        matches = list(out.glob(f"{name}-7.2.9-200.fc44.*.rpm"))
        assert len(matches) == 1, (name, matches)
        result[name] = matches[0]
    return result


def build_kmod_payload(root: Path, *, extra_file: bool = False) -> Path:
    """Lay out an extracted kmod-zfs payload like `rpm2cpio kmod-zfs-*.rpm | cpio -idm`."""
    moddir = root / "lib" / "modules" / UNAME_R / "extra" / "zfs"
    moddir.mkdir(parents=True)
    (moddir / "zfs.ko").write_bytes(b"\x7fELF zfs module")
    (moddir / "spl.ko").write_bytes(b"\x7fELF spl module")
    if extra_file:
        (root / "etc").mkdir()
        (root / "etc" / "zfs.conf").write_text("x")
    return root
