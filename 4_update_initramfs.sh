#!/usr/bin/bash

set -e


source parse_args

if [ -n "${KRN_VERSION}" ] && [ -n "${KRN_RELEASE}" ] && [ -n "${OS_VERSION_MAJOR}" ]; then

  if [ "$(id -u)" -ne 0 ]
    then echo "Please run as root"
    exit
  fi

  source ./config
  source ./library
  update_initramfs
  list_initramfs_zfs
  list_installed_kernels
else
  show_help
fi
