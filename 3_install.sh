#!/usr/bin/bash

set -e

if [ "$(id -u)" -ne 0 ]
  then echo "Please run as root"
  exit
fi

source ./input/config
source ./localconfig
source ./library

zfs_snapshot
install_kernel_with_zfs
list_initramfs_zfs
list_versionlock
print_kernel_changelog