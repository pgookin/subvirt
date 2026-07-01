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

remote_quote() {
  python3 -c 'import shlex, sys; print(shlex.quote(sys.argv[1]))' "$1"
}

config_value() {
  python3 -c 'import json, sys; data=json.load(open(sys.argv[1])); cur=data;
for key in sys.argv[2].split("."):
    cur=cur[key]
print(cur)' "$CONFIG" "$1"
}

local_host_matches() {
  python3 -c 'import socket, sys; host=sys.argv[1]; print("yes" if host in {"localhost", "127.0.0.1", "::1", socket.gethostname(), socket.getfqdn()} else "no")' "$1"
}

if [[ "$REFRESH_SOURCES" == "true" ]]; then
  test -n "$UBUNTU_VERSION"
  test -n "$ALMA_VERSION"
  ./scripts/release.py checkout-build --config "$CONFIG" --ref "$REF" --build-id "$BUILD_ID" --execute
  BUILD_HOST=$(config_value hosts.build)
  BUILD_WORKDIR=$(config_value project.workdir)
  q_workdir=$(remote_quote "$BUILD_WORKDIR")
  q_config=$(remote_quote "$CONFIG")
  q_ubuntu_version=$(remote_quote "$UBUNTU_VERSION")
  q_alma_version=$(remote_quote "$ALMA_VERSION")
  refresh_command="cd $q_workdir && ./scripts/refresh-libvirt-source.py --config $q_config --distro ubuntu --version $q_ubuntu_version && ./scripts/refresh-libvirt-source.py --config $q_config --distro alma --version $q_alma_version"
  if [[ "$(local_host_matches "$BUILD_HOST")" == "yes" ]]; then
    bash -lc "$refresh_command"
  else
    ssh "$BUILD_HOST" "$refresh_command"
  fi
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
