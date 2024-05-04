# Zefosator

*Binary* OpenZFS packages build system for Fedora and AlmaLinux systems. For perfect root-on-zfs setups.

## Step 1. Build RPM packages

1. Fetch original OS kernel packages. 
2. Checkout, patch and build OpenZFS RPM packages.
3. Modify OS kernel package to depend on OpenZFS RPM kmod package.
4. Copy all artifacts to `output` directory.

```
./1_build.sh --git-tag zfs-2.2.4 --pkg-version 2.2.4 --pkg-release 1 --os-version 40 --krn-version 6.8.9 --krn-release 300 
```

## Step 2. Create/update DNF repositories

1. Copy relevant kernel RPM packages to OpenZFS kernel packages repository directory.
2. Update repository metadata.
3. Copy relevant kernel RPM packages to OpenZFS userspace packages repository directory.
4. Update repository metadata.
5. TODO: rsync repository with remote webserver.

```
./2_update_repos.sh --pkg-version 2.2.4 --pkg-release 1 --os-version 40 --krn-version 6.8.9 --krn-release 300 --kernel-repo-dir /home/giedriusm/Repo/zfs/40/kernel --userspace-repo-dir /home/giedriusm/Repo/zfs/40/user
```

## Step 3. Install/upgrade kernel+OpenZFS packages

1. Optional. Create local ZFS snapshot before making package upgrades. Remove `--snapshot-dataset` parameter if not required.
2. Upgrade kernel and OpenZFS packages in single DNF transaction.

```
./3_install.sh --pkg-version 2.2.4 --pkg-release 1 --os-version 40 --krn-version 6.8.9 --krn-release 300 --snapshot-dataset gmp1/fedora/40
```
