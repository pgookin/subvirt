#!/usr/bin/env bash
set -euo pipefail

IMAGE=${SUBVIRT_UBUNTU_BUILD_IMAGE:-localhost/subvirt-ubuntu-24.04-build:latest}
CONTAINERFILE=${SUBVIRT_UBUNTU_CONTAINERFILE:-containers/ubuntu-24.04-build/Containerfile}
RUNTIME=${SUBVIRT_CONTAINER_RUNTIME:-podman}

if ! command -v "$RUNTIME" >/dev/null 2>&1; then
  echo "$RUNTIME is required for containerized builds" >&2
  exit 1
fi

if [[ "$(basename "$RUNTIME")" == "podman" ]]; then
  "$RUNTIME" system migrate || true
fi

"$RUNTIME" build -t "$IMAGE" -f "$CONTAINERFILE" .
"$RUNTIME" run --rm \
  --security-opt label=disable \
  -v "$(pwd):/work" \
  -w /work \
  "$IMAGE" \
  bash -lc 'rm -rf dist && mkdir -p dist && ./scripts/build-provider-deb.sh'
