# Zefosator

*Binary* OpenZFS packages build system for Fedora and AlmaLinux systems. For perfect root-on-zfs setups.

## Step 1. Build RPM packages

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

## Step 2. Publish RPM packages to DNF repositories

1. Copy relevant kernel RPM packages to OpenZFS kernel packages repository directory.
2. Update repository metadata.
3. Copy relevant kernel RPM packages to OpenZFS userspace packages repository directory.
4. Update repository metadata.
5. TODO: rsync repository with remote webserver.

Fedora 40:
```
./zefosator --publish --pkg-version 2.2.4 --pkg-release 1 --os-version 40 --krn-version 6.8.9 --krn-release 300 --kernel-repo-dir /home/giedriusm/Repo/zfs/40/kernel --userspace-repo-dir /home/giedriusm/Repo/zfs/40/user
```

AlmaLinux 9.4:
```
./zefosator --publish  --krn-vendor almalinux --os-version 9.4 --krn-version 5.14.0 --krn-release 427.13.1 --pkg-version 2.2.4 --pkg-release 1 --kernel-repo-dir /home/giedriusm/Repo/zfs/el9/kernel --userspace-repo-dir /home/giedriusm/Repo/zfs/el9/user
```

## Step 3. Install/upgrade kernel+OpenZFS packages on local system

1. Optional. Create local ZFS snapshot before making package upgrades. Remove `--snapshot-dataset` parameter if not required.
2. Upgrade kernel and OpenZFS packages in single DNF transaction.

```
./zefosator --install --pkg-version 2.2.4 --pkg-release 1 --os-version 40 --krn-version 6.8.9 --krn-release 300 --snapshot-dataset gmp1/fedora/40
```
