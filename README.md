# Zefosator

*Binary* OpenZFS packages build system for Fedora and AlmaLinux systems. For perfect root-on-zfs setups.

## Building RPM packages

1. Fetch original OS kernel packages. 
2. Checkout, patch and build OpenZFS RPM packages.
3. Modify OS kernel package to depend on OpenZFS RPM kmod package.
4. Copy all artifacts to `output` directory.

Build latest Fedora 39 kernel with latest OpenZFS release using prepared parameters file:
```
./zefosator --build --params-file params/fedora-39.params
```

Building with custom parameters, e.g. OpenZFS 2.2.4 for Fedora 40 kernel 6.8.9-300:
```
./zefosator --build --os-version 40 --krn-version 6.8.9 --krn-release 300 --git-tag zfs-2.2.4 --pkg-version 2.2.4 --pkg-release 1
```

Building OpenZFS 2.2.7 for Fedora 40 using linux-6.12.22.tar.xz from kernel.org as kernel source based on Fedora's kernel-6.12.15-100.fc40.src.rpm:
```
./zefosator --build --krn-vendor zefosa --os-version 40 --krn-version 6.12.15 --krn-tarball-version 6.12.22 --krn-release 100 --git-tag zfs-2.2.7 --pkg-version 2.2.7 --pkg-release 1
```

Building OpenZFS 2.2.4 for AlmaLinux 9.4 kernel 5.14.0-362.24.1:
```
./zefosator --build --krn-vendor almalinux --os-version 9.4 --krn-version 5.14.0 --krn-release 427.13.1 --git-tag zfs-2.2.4 --pkg-version 2.2.4 --pkg-release 1
```
