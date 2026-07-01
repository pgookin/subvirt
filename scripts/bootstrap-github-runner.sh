#!/usr/bin/env bash
set -euo pipefail

REPO_URL=${GITHUB_REPO_URL:-https://github.com/pgookin/subvirt}
RUNNER_USER=${SUBVIRT_RUNNER_USER:-pgookin}
RUNNER_DIR=${SUBVIRT_RUNNER_DIR:-/srv/subvirt/actions-runner}
RUNNER_LABELS=${SUBVIRT_RUNNER_LABELS:-subvirt-build}
RUNNER_NAME=${SUBVIRT_RUNNER_NAME:-subvirt-build}
TOKEN=${GITHUB_RUNNER_TOKEN:-${1:-}}

if [[ "$(id -u)" != "0" ]]; then
  echo "run as root" >&2
  exit 1
fi
if [[ -z "$TOKEN" ]]; then
  echo "usage: GITHUB_RUNNER_TOKEN=<token> $0" >&2
  echo "Create the token in GitHub: Settings -> Actions -> Runners -> New self-hosted runner." >&2
  exit 2
fi
if ! id "$RUNNER_USER" >/dev/null 2>&1; then
  echo "runner user $RUNNER_USER does not exist" >&2
  exit 1
fi

apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y ca-certificates curl git python3 sudo tar

install -d -m 0755 -o "$RUNNER_USER" -g "$RUNNER_USER" "$RUNNER_DIR"
version=$(python3 - <<'PYINNER'
import json, urllib.request
with urllib.request.urlopen('https://api.github.com/repos/actions/runner/releases/latest', timeout=30) as r:
    print(json.load(r)['tag_name'].lstrip('v'))
PYINNER
)
archive="actions-runner-linux-x64-${version}.tar.gz"
url="https://github.com/actions/runner/releases/download/v${version}/${archive}"
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
curl -fsSL "$url" -o "$tmp/$archive"
tar -xzf "$tmp/$archive" -C "$RUNNER_DIR"
chown -R "$RUNNER_USER:$RUNNER_USER" "$RUNNER_DIR"

if [[ -f "$RUNNER_DIR/.runner" ]]; then
  sudo -u "$RUNNER_USER" bash -lc "cd '$RUNNER_DIR' && ./config.sh remove --unattended --token '$TOKEN'" || true
fi
sudo -u "$RUNNER_USER" bash -lc "cd '$RUNNER_DIR' && ./config.sh --url '$REPO_URL' --token '$TOKEN' --name '$RUNNER_NAME' --labels '$RUNNER_LABELS' --unattended --replace"
cd "$RUNNER_DIR"
./svc.sh install "$RUNNER_USER"
./svc.sh start
./svc.sh status
