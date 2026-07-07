#!/usr/bin/env bash
set -euo pipefail

BUILD_ID=${1:?usage: codex-release-gate.sh BUILD_ID}
EVIDENCE_DIR=${2:-artifacts/$BUILD_ID}
REPORT=${3:-artifacts/$BUILD_ID/codex-release-review.md}

if ! command -v codex >/dev/null 2>&1; then
  echo "codex CLI is required for promotion review" >&2
  exit 1
fi

mkdir -p "$(dirname "$REPORT")"
cat >"$REPORT.prompt" <<PROMPT
You are reviewing a Subvirt release candidate before stable repository promotion.

Decision contract:
- Reply with PROMOTE_OK on the first line only if deterministic release gates passed and no high-risk issue is visible.
- Reply with HOLD on the first line if logs are missing, tests failed, package versions look wrong, repo metadata is suspicious, or manual review is needed.
- After the first line, briefly explain the reason.

Evidence directory: $EVIDENCE_DIR
Review the available files recursively, especially package manifests, build logs, install logs, storage test logs, staging repo tests, and upstream version reports. Do not modify files.
PROMPT

codex exec --cd . --sandbox read-only "$(cat "$REPORT.prompt")" >"$REPORT"
head -n 1 "$REPORT" | grep -qx 'PROMOTE_OK'
