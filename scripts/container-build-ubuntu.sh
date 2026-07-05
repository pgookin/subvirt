#!/usr/bin/env bash
set -euo pipefail

IMAGE=${SUBVIRT_UBUNTU_BUILD_IMAGE:-localhost/subvirt-ubuntu-24.04-build:latest}
CONTAINERFILE=${SUBVIRT_UBUNTU_CONTAINERFILE:-containers/ubuntu-24.04-build/Containerfile}
RUNTIME=${SUBVIRT_CONTAINER_RUNTIME:-podman}
SRC_DIR=${SUBVIRT_UBUNTU_LIBVIRT_SRC:-build/libvirt-u24-10.0.0}

if ! command -v "$RUNTIME" >/dev/null 2>&1; then
  echo "$RUNTIME is required for containerized builds" >&2
  exit 1
fi

"$RUNTIME" build -t "$IMAGE" -f "$CONTAINERFILE" .
"$RUNTIME" run --rm \
  --security-opt label=disable \
  -e "SUBVIRT_UBUNTU_LIBVIRT_SRC=$SRC_DIR" \
  -v "$(pwd):/work" \
  -w /work \
  "$IMAGE" \
  bash -lc './scripts/refresh-locked-libvirt-sources.sh ubuntu && apt-get update && mk-build-deps -i -r -t "apt-get -y --no-install-recommends" "$SUBVIRT_UBUNTU_LIBVIRT_SRC/debian/control" && rm -rf dist && mkdir -p dist && ./scripts/build-provider-deb.sh && SUBVIRT_NATIVE_BUILD=1 ./scripts/build-libvirt-deb.sh && ./scripts/build-virt-manager-deb.sh'
