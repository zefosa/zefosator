#!/usr/bin/bash

set -e

if [ "$(id -u)" -eq 0 ]
  then echo "Please run as non-root"
  exit
fi

source ./input/config
source ./localconfig
source ./library

build_image
copy_zfs_userspace_rpms_to_repos
update_userspace_repo
copy_zfs_kmod_rpms_to_repos
copy_kernel_rpms_to_repos
update_kernel_repo
list_kernel_repo
list_userspace_repo
