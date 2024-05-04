#!/usr/bin/bash

set -e

source parse_args

if [ -n "${KERNEL_REPO_DIR}" ] && [ -n "${USERSPACE_REPO_DIR}" ] && \
   [ -n "${ZFS_PKG_VERSION}" ] && [ -n "${ZFS_PKG_RELEASE}" ] && \
   [ -n "${KRN_VERSION}" ] && [ -n "${KRN_RELEASE}" ] && [ -n "${OS_VERSION_MAJOR}" ]; then

  if [ "$(id -u)" -eq 0 ]
    then echo "Please run as non-root"
    exit
  fi
  source ./config
  source ./library
  create_variables_file
  build_image
  copy_zfs_userspace_rpms_to_repos
  update_userspace_repo
  copy_zfs_kmod_rpms_to_repos
  copy_kernel_rpms_to_repos
  update_kernel_repo
  list_kernel_repo
  list_userspace_repo
else
  show_help
fi