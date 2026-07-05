#!/usr/bin/env bash
set -euo pipefail

LOCK=${SUBVIRT_UPSTREAM_LOCK:-release/upstream-lock.json}
CONFIG=${SUBVIRT_REFRESH_CONFIG:-release/release.json}
if [[ ! -f "$CONFIG" ]]; then
  CONFIG=release/release.example.json
fi

if [[ ! -f "$LOCK" ]]; then
  echo "$LOCK is missing" >&2
  exit 1
fi

version_for() {
  python3 - "$LOCK" "$1" <<'PY_VERSION'
import json
import sys
lock = json.load(open(sys.argv[1], encoding="utf-8"))
print(lock[sys.argv[2]]["version"])
PY_VERSION
}

refresh_one() {
  local distro=$1
  local version
  version=$(version_for "$distro")
  ./scripts/refresh-libvirt-source.py --distro "$distro" --version "$version" --config "$CONFIG"
}

if [[ $# -eq 0 ]]; then
  set -- ubuntu alma
fi

for distro in "$@"; do
  case "$distro" in
    ubuntu|alma) refresh_one "$distro" ;;
    *) echo "unsupported distro: $distro" >&2; exit 1 ;;
  esac
done
