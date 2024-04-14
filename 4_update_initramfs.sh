#!/usr/bin/bash

set -e

if [ "$(id -u)" -ne 0 ]
  then echo "Please run as root"
  exit
fi

source ./input/config
source ./localconfig
source ./library

update_initramfs
list_initramfs_zfs
list_installed_kernels
