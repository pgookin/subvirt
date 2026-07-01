#!/usr/bin/env bash
set -euo pipefail

CONFIG=${SUBVIRT_RELEASE_CONFIG:-/srv/subvirt/release/release.json}
BUILD_ID=${BUILD_ID:?BUILD_ID is required}
REF=${REF:-main}
REFRESH_SOURCES=${REFRESH_SOURCES:-false}
UBUNTU_VERSION=${UBUNTU_VERSION:-}
ALMA_VERSION=${ALMA_VERSION:-}
PROMOTE_STABLE=${PROMOTE_STABLE:-false}

summary() {
  if [[ -n "${GITHUB_STEP_SUMMARY:-}" ]]; then
    printf '%s\n' "$*" >>"$GITHUB_STEP_SUMMARY"
  fi
}

summary "## Subvirt candidate release"
summary "- Build ID: \`$BUILD_ID\`"
summary "- Ref: \`$REF\`"
summary "- Refresh sources: \`$REFRESH_SOURCES\`"
summary "- Ubuntu version: \`${UBUNTU_VERSION:-not set}\`"
summary "- Alma version: \`${ALMA_VERSION:-not set}\`"
summary "- Promote stable: \`$PROMOTE_STABLE\`"

if [[ "$REFRESH_SOURCES" == "true" ]]; then
  test -n "$UBUNTU_VERSION"
  test -n "$ALMA_VERSION"
  ./scripts/refresh-libvirt-source.py --distro ubuntu --version "$UBUNTU_VERSION"
  ./scripts/refresh-libvirt-source.py --distro alma --version "$ALMA_VERSION"
fi

./scripts/release.py build --config "$CONFIG" --ref "$REF" --build-id "$BUILD_ID" --execute
./scripts/release.py collect --config "$CONFIG" --build-id "$BUILD_ID" --execute
./scripts/release.py test-artifacts --config "$CONFIG" --ref "$REF" --build-id "$BUILD_ID" --execute
./scripts/release.py publish-staging --config "$CONFIG" --build-id "$BUILD_ID" --execute
./scripts/release.py test-staging --config "$CONFIG" --ref "$REF" --build-id "$BUILD_ID" --execute
./scripts/write-release-evidence.py --build-id "$BUILD_ID"

if [[ "$PROMOTE_STABLE" == "true" ]]; then
  ./scripts/codex-release-gate.sh "$BUILD_ID"
  ./scripts/release.py promote --config "$CONFIG" --build-id "$BUILD_ID" --execute
fi

summary "Candidate workflow completed."
