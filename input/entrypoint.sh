#!/usr/bin/bash

set -e

source ./config
source ./library

for arg in "$@"
do
  if [ "$arg" == "prepare" ]; then    
    prepare_all
  elif [ "$arg" == "zfs_rpms" ]; then
    build_zfs_rpms
    move_zfs_rpms
  elif [ "$arg" == "kernel_rpms" ]; then
    download_kernel_rpms
    patch_kernel_meta_rpm
    move_kernel_rpms
  elif [ "$arg" == "cleanup" ]; then
    cleanup_zfs
    cleanup_kernel
  elif [ "$arg" == "update_repo" ]; then
    update_repo
  fi
done
