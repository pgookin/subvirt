#!/usr/bin/env bash
set -euo pipefail

IMAGE=${SUBVIRT_ALMA_BUILD_IMAGE:-localhost/subvirt-almalinux-10-build:latest}
CONTAINERFILE=${SUBVIRT_ALMA_CONTAINERFILE:-containers/almalinux-10-build/Containerfile}
RUNTIME=${SUBVIRT_CONTAINER_RUNTIME:-podman}

if ! command -v "$RUNTIME" >/dev/null 2>&1; then
  echo "$RUNTIME is required for containerized builds" >&2
  exit 1
fi

"$RUNTIME" build -t "$IMAGE" -f "$CONTAINERFILE" .
"$RUNTIME" run --rm \
  --security-opt label=disable \
  -e SUBVIRT_RPM_BUILD_JOBS=${SUBVIRT_RPM_BUILD_JOBS:-2} \
  -v "$(pwd):/work" \
  -w /work \
  "$IMAGE" \
  bash -lc 'dnf builddep -y build/libvirt.spec && rm -rf dist && mkdir -p dist && ./scripts/build-provider-rpm.sh && SUBVIRT_NATIVE_BUILD=1 ./scripts/build-libvirt-rpm.sh && ./scripts/build-virt-manager-rpm.sh'
