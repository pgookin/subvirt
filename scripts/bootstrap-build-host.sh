#!/usr/bin/env bash
set -euo pipefail

if [[ "$(id -u)" != "0" ]]; then
  echo "run as root" >&2
  exit 1
fi

apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y \
  ca-certificates \
  git \
  podman \
  python3 \
  rsync \
  sudo \
  uidmap

echo "Build host bootstrap installed Podman and baseline tools."
echo "Next: verify the build images can be built with ./scripts/container-build-ubuntu.sh and ./scripts/container-build-alma.sh."
