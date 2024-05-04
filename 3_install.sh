#!/usr/bin/bash

set -e

source parse_args

if [ -n "${ZFS_PKG_VERSION}" ] && [ -n "${OS_VERSION_MAJOR}" ] && \
   [ -n "${KRN_VERSION}" ] && [ -n "${KRN_RELEASE}" ]; then

  if [ "$(id -u)" -ne 0 ]
    then echo "Please run as root"
    exit
  fi

  source ./config
  source ./library
  zfs_snapshot
  install_kernel_with_zfs
  list_initramfs_zfs
  list_versionlock
  print_kernel_changelog
  list_installed_kernels
else
  show_help
fi
