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
make_all_rpms
