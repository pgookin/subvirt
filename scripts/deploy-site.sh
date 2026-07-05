#!/usr/bin/env bash
set -euo pipefail

DRY_RUN=false
if [[ "${1:-}" == "--dry-run" ]]; then
  DRY_RUN=true
  shift
fi

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
SITE_DIR=${SUBVIRT_SITE_DIR:-"$ROOT/site"}
SITE_HOST=${SUBVIRT_SITE_HOST:-subvirt-publish@repo.subvirt.net}
SITE_ROOT=${SUBVIRT_SITE_ROOT:-/srv/www}
SITE_IDENTITY=${SUBVIRT_SITE_IDENTITY:-/srv/subvirt/release/public_repo_ed25519}

if [[ ! -f "$SITE_DIR/index.html" ]]; then
  echo "site/index.html is required" >&2
  exit 1
fi

ssh_cmd=(ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new)
if [[ -n "$SITE_IDENTITY" && -f "$SITE_IDENTITY" ]]; then
  ssh_cmd+=(-i "$SITE_IDENTITY")
fi

rsync_args=(-a --delete)
if [[ "$DRY_RUN" == "true" ]]; then
  rsync_args+=(--dry-run --itemize-changes)
fi

printf '+ rsync %s/ %s:%s/\n' "$SITE_DIR" "$SITE_HOST" "$SITE_ROOT"
rsync "${rsync_args[@]}" -e "${ssh_cmd[*]}" "$SITE_DIR/" "$SITE_HOST:$SITE_ROOT/"

if [[ "$DRY_RUN" == "true" ]]; then
  exit 0
fi

for url in https://subvirt.net/ https://www.subvirt.net/; do
  printf '+ check-url %s\n' "$url"
  curl -fsSIL --max-time 20 "$url" >/dev/null
done
