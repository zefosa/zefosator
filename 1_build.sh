#!/usr/bin/bash

set -e

source parse_args

if [ -n "${ZFS_GIT_TAG}" ] && [ -n "${ZFS_PKG_VERSION}" ] && [ -n "${ZFS_PKG_RELEASE}" ] && \
   [ -n "${KRN_VERSION}" ] && [ -n "${KRN_RELEASE}" ] && [ -n "${OS_VERSION_MAJOR}" ]; then
  if [ "$(id -u)" -eq 0 ]
    then echo "Please run as non-root"
    exit
  fi
  source ./config 
  source ./library
  create_variables_file
  build_image
  make_all_rpms
else
  show_help
fi
