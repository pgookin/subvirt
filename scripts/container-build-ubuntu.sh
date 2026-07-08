#!/usr/bin/env bash
set -euo pipefail

TARGET=${SUBVIRT_UBUNTU_TARGET:-ubuntu-24.04}
RUNTIME=${SUBVIRT_CONTAINER_RUNTIME:-podman}

if ! command -v "$RUNTIME" >/dev/null 2>&1; then
  echo "$RUNTIME is required for containerized builds" >&2
  exit 1
fi

eval "$(PYTHONPATH=scripts python3 - "$TARGET" <<'PYVARS'
import shlex
import sys
from ubuntu_targets import ubuntu_target

target = ubuntu_target(None, target_id=sys.argv[1])
print(f"TARGET_ID={shlex.quote(target.id)}")
print(f"SUITE={shlex.quote(target.suite)}")
print(f"IMAGE={shlex.quote(target.image)}")
print(f"CONTAINERFILE={shlex.quote(target.containerfile)}")
print(f"SRC_GLOB={shlex.quote('build/' + target.build_dir_prefix + '-*')}")
PYVARS
)"

IMAGE=${SUBVIRT_UBUNTU_BUILD_IMAGE:-$IMAGE}
CONTAINERFILE=${SUBVIRT_UBUNTU_CONTAINERFILE:-$CONTAINERFILE}
SRC_DIR=${SUBVIRT_UBUNTU_LIBVIRT_SRC:-}
if [[ -z "$SRC_DIR" ]]; then
  SRC_DIR=$(find build -maxdepth 1 -type d -name "${SRC_GLOB#build/}" | sort | tail -1)
fi
if [[ -z "$SRC_DIR" ]]; then
  echo "no refreshed libvirt source tree found for $TARGET_ID" >&2
  exit 1
fi

"$RUNTIME" build -t "$IMAGE" -f "$CONTAINERFILE" .
"$RUNTIME" run --rm   --security-opt label=disable   -e "SUBVIRT_UBUNTU_TARGET=$TARGET_ID"   -e "SUBVIRT_UBUNTU_DIST=$SUITE"   -e "SUBVIRT_UBUNTU_LIBVIRT_SRC=$SRC_DIR"   -v "$(pwd):/work"   -w /work   "$IMAGE"   bash -lc 'apt-get update && mk-build-deps -i -r -t "apt-get -y --no-install-recommends" "$SUBVIRT_UBUNTU_LIBVIRT_SRC/debian/control" && rm -rf dist && mkdir -p dist && ./scripts/build-provider-deb.sh && SUBVIRT_NATIVE_BUILD=1 ./scripts/build-libvirt-deb.sh && ./scripts/build-virt-manager-deb.sh'
