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

ubuntu_targets() {
  PYTHONPATH=scripts python3 - "$CONFIG" <<'PY_TARGETS'
import json
import sys
from pathlib import Path
from ubuntu_targets import ubuntu_targets
config = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
for target in ubuntu_targets(config):
    print(target.id)
PY_TARGETS
}

lock_key_for_target() {
  PYTHONPATH=scripts python3 - "$1" <<'PY_KEY'
import sys
from ubuntu_targets import ubuntu_lock_key, ubuntu_target
print(ubuntu_lock_key(ubuntu_target(None, target_id=sys.argv[1])))
PY_KEY
}

alma_targets() {
  PYTHONPATH=scripts python3 - "$CONFIG" <<'PY_TARGETS'
import json
import sys
from pathlib import Path
from alma_targets import alma_targets
config = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
for target in alma_targets(config):
    print(target.id)
PY_TARGETS
}

alma_lock_key_for_target() {
  PYTHONPATH=scripts python3 - "$1" <<'PY_KEY'
import sys
from alma_targets import alma_lock_key, alma_target
print(alma_lock_key(alma_target(None, target_id=sys.argv[1])))
PY_KEY
}

refresh_alma_target() {
  local target=$1
  local key version
  key=$(alma_lock_key_for_target "$target")
  version=$(version_for "$key")
  if [[ -z "$version" ]]; then
    echo "No locked AlmaLinux version for $target; skipping" >&2
    return
  fi
  ./scripts/refresh-libvirt-source.py --distro alma --alma-target "$target" --version "$version" --config "$CONFIG"
}

refresh_ubuntu_target() {
  local target=$1
  local key version
  key=$(lock_key_for_target "$target")
  version=$(version_for "$key")
  if [[ -z "$version" ]]; then
    echo "No locked Ubuntu version for $target; skipping" >&2
    return
  fi
  ./scripts/refresh-libvirt-source.py --distro ubuntu --ubuntu-target "$target" --version "$version" --config "$CONFIG"
}

refresh_one() {
  local distro=$1
  local version
  case "$distro" in
    ubuntu)
      while read -r target; do
        refresh_ubuntu_target "$target"
      done < <(ubuntu_targets)
      ;;
    ubuntu-*)
      refresh_ubuntu_target "$distro"
      ;;
    alma)
      while read -r target; do
        refresh_alma_target "$target"
      done < <(alma_targets)
      ;;
    almalinux-*)
      refresh_alma_target "$distro"
      ;;
    *) echo "unsupported distro: $distro" >&2; exit 1 ;;
  esac
}

if [[ $# -eq 0 ]]; then
  set -- ubuntu alma
fi

for distro in "$@"; do
  refresh_one "$distro"
done
