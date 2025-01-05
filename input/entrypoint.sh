#!/usr/bin/env bash

set -e
trap 'echo "Error occurred in the script at line ${LINENO}"' ERR

cp /input/.rpmmacros /root/

INPUT_DIR=${INPUT_DIR:=/input}
OUTPUT_DIR=${OUTPUT_DIR:=/output}
CACHE_DIR=${CACHE_DIR:=/cache}
REPO_DIR=${REPO_DIR:=/repo}
BUILD_DIR=${BUILD_DIR:=/build}

echo "input dir: ${INPUT_DIR}"
echo "output dir: ${OUTPUT_DIR}"
echo "cache dir: ${CACHE_DIR}"
echo "repo dir: ${REPO_DIR}"
echo "build dir: ${BUILD_DIR}"
echo "RPM source dir: $(rpm --eval "%{_sourcedir}")"

source ${INPUT_DIR}/variables
source ${INPUT_DIR}/library

for arg in "$@"
do
  if [ "$arg" == "prepare" ]; then
    echo "Kernel vendor: ${KRN_VENDOR}"
    create_dirs
    download_kernel_sources
    prepare_kernel_sources
    prepare_zfs_sources
  elif [ "$arg" == "zfs_rpms" ]; then
    build_zfs_rpms
    move_zfs_rpms
  elif [ "$arg" == "kernel_rpms" ]; then
    build_kernel_rpms
    move_kernel_rpms
  elif [ "$arg" == "cleanup" ]; then
    cleanup_zfs
    cleanup_kernel
  elif [ "$arg" == "update_repo" ]; then
    update_repo
  fi
done
