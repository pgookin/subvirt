#!/usr/bin/env bash
set -euo pipefail

if [[ "$(id -u)" != "0" ]]; then
  echo "run as root" >&2
  exit 1
fi

apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y ca-certificates nodejs npm
npm install -g @openai/codex
codex --version

echo "Codex CLI installed. Run 'codex login' as the GitHub runner user before enabling the promotion gate."
