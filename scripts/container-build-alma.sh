#!/usr/bin/env bash
set -euo pipefail

TARGET=${SUBVIRT_ALMA_TARGET:-almalinux-10}
RUNTIME=${SUBVIRT_CONTAINER_RUNTIME:-podman}

if ! command -v "$RUNTIME" >/dev/null 2>&1; then
  echo "$RUNTIME is required for containerized builds" >&2
  exit 1
fi

if [[ "$(basename "$RUNTIME")" == "podman" ]]; then
  "$RUNTIME" system migrate || true
fi

eval "$(PYTHONPATH=scripts python3 - "$TARGET" <<'PYVARS'
import shlex
import sys
from alma_targets import alma_target

target = alma_target(None, target_id=sys.argv[1])
print(f"TARGET_ID={shlex.quote(target.id)}")
print(f"IMAGE={shlex.quote(target.image)}")
print(f"CONTAINERFILE={shlex.quote(target.containerfile)}")
print(f"MOCK_CONFIG={shlex.quote(target.mock_config)}")
PYVARS
)"

IMAGE=${SUBVIRT_ALMA_BUILD_IMAGE:-$IMAGE}
CONTAINERFILE=${SUBVIRT_ALMA_CONTAINERFILE:-$CONTAINERFILE}

"$RUNTIME" build -t "$IMAGE" -f "$CONTAINERFILE" .
"$RUNTIME" run --rm \
  --security-opt label=disable \
  -e "SUBVIRT_ALMA_TARGET=$TARGET_ID" \
  -e "SUBVIRT_MOCK_CONFIG=$MOCK_CONFIG" \
  -e SUBVIRT_RPM_BUILD_JOBS=${SUBVIRT_RPM_BUILD_JOBS:-2} \
  -v "$(pwd):/work" \
  -w /work \
  "$IMAGE" \
  bash -lc './scripts/refresh-locked-libvirt-sources.sh "$SUBVIRT_ALMA_TARGET" && dnf builddep -y build/libvirt.spec && rm -rf dist && mkdir -p dist && ./scripts/build-provider-rpm.sh && SUBVIRT_NATIVE_BUILD=1 ./scripts/build-libvirt-rpm.sh && ./scripts/build-virt-manager-rpm.sh'
