#!/usr/bin/env bash
set -euo pipefail

CONFIG=${SUBVIRT_RELEASE_CONFIG:-/srv/subvirt/release/release.json}
BUILD_ID=${BUILD_ID:?BUILD_ID is required}
REF=${REF:-main}
REFRESH_SOURCES=${REFRESH_SOURCES:-false}
UBUNTU_VERSION=${UBUNTU_VERSION:-}
ALMA_VERSION=${ALMA_VERSION:-}
PROMOTE_STABLE=${PROMOTE_STABLE:-false}
BUILD_UBUNTU=${BUILD_UBUNTU:-true}
BUILD_ALMA=${BUILD_ALMA:-true}

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
summary "- Build Ubuntu: \`$BUILD_UBUNTU\`"
summary "- Build Alma: \`$BUILD_ALMA\`"

remote_quote() {
  python3 -c 'import shlex, sys; print(shlex.quote(sys.argv[1]))' "$1"
}

config_value() {
  python3 -c 'import json, sys; data=json.load(open(sys.argv[1])); cur=data;
for key in sys.argv[2].split("."):
    cur=cur[key]
print(cur)' "$CONFIG" "$1"
}

config_bool() {
  python3 -c 'import json, sys; data=json.load(open(sys.argv[1])); cur=data;
for key in sys.argv[2].split("."):
    if not isinstance(cur, dict) or key not in cur:
        cur=False
        break
    cur=cur[key]
print("true" if bool(cur) else "false")' "$CONFIG" "$1"
}

local_host_matches() {
  python3 -c 'import socket, sys; host=sys.argv[1]; print("yes" if host in {"localhost", "127.0.0.1", "::1", socket.gethostname(), socket.getfqdn()} else "no")' "$1"
}

if [[ "$BUILD_UBUNTU" != "true" && "$BUILD_ALMA" != "true" ]]; then
  echo "At least one of BUILD_UBUNTU or BUILD_ALMA must be true" >&2
  exit 1
fi

LAB_ENABLED=$(config_bool lab.enabled)
summary "- Ephemeral lab: \`$LAB_ENABLED\`"

if [[ "$REFRESH_SOURCES" == "true" ]]; then
  ./scripts/release.py checkout-build --config "$CONFIG" --ref "$REF" --build-id "$BUILD_ID" --execute
  BUILD_HOST=$(config_value hosts.build)
  BUILD_WORKDIR=$(config_value project.workdir)
  q_workdir=$(remote_quote "$BUILD_WORKDIR")
  q_config=$(remote_quote "$CONFIG")
  refresh_commands=()
  if [[ "$BUILD_UBUNTU" == "true" ]]; then
    test -n "$UBUNTU_VERSION"
    q_ubuntu_version=$(remote_quote "$UBUNTU_VERSION")
    refresh_commands+=("./scripts/refresh-libvirt-source.py --config $q_config --distro ubuntu --version $q_ubuntu_version")
  fi
  if [[ "$BUILD_ALMA" == "true" ]]; then
    test -n "$ALMA_VERSION"
    q_alma_version=$(remote_quote "$ALMA_VERSION")
    refresh_commands+=("./scripts/refresh-libvirt-source.py --config $q_config --distro alma --version $q_alma_version")
  fi
  refresh_command="cd $q_workdir && ${refresh_commands[0]}"
  for command in "${refresh_commands[@]:1}"; do
    refresh_command="$refresh_command && $command"
  done
  if [[ "$(local_host_matches "$BUILD_HOST")" == "yes" ]]; then
    bash -lc "$refresh_command"
  else
    ssh "$BUILD_HOST" "$refresh_command"
  fi
fi

if [[ "$BUILD_UBUNTU" == "true" && "$BUILD_ALMA" == "true" ]]; then
  ./scripts/release.py build --config "$CONFIG" --ref "$REF" --build-id "$BUILD_ID" --execute
  ./scripts/release.py collect --config "$CONFIG" --build-id "$BUILD_ID" --execute
  if [[ "$LAB_ENABLED" == "true" ]]; then
    ./scripts/release.py test-lab --config "$CONFIG" --ref "$REF" --build-id "$BUILD_ID" --execute
  else
    ./scripts/release.py test-artifacts --config "$CONFIG" --ref "$REF" --build-id "$BUILD_ID" --execute
    ./scripts/release.py publish-staging --config "$CONFIG" --build-id "$BUILD_ID" --execute
    ./scripts/release.py test-staging --config "$CONFIG" --ref "$REF" --build-id "$BUILD_ID" --execute
  fi
else
  if [[ "$PROMOTE_STABLE" == "true" ]]; then
    echo "Partial candidate builds cannot be promoted to stable" >&2
    exit 1
  fi
  if [[ "$BUILD_UBUNTU" == "true" ]]; then
    ./scripts/release.py build-ubuntu --config "$CONFIG" --ref "$REF" --build-id "$BUILD_ID" --execute
    ./scripts/release.py collect-ubuntu --config "$CONFIG" --build-id "$BUILD_ID" --execute
  fi
  if [[ "$BUILD_ALMA" == "true" ]]; then
    ./scripts/release.py build-alma --config "$CONFIG" --ref "$REF" --build-id "$BUILD_ID" --execute
    ./scripts/release.py collect-alma --config "$CONFIG" --build-id "$BUILD_ID" --execute
  fi
  if [[ "$LAB_ENABLED" == "true" ]]; then
    ./scripts/release.py test-lab --config "$CONFIG" --ref "$REF" --build-id "$BUILD_ID" --execute
  else
    if [[ "$BUILD_UBUNTU" == "true" ]]; then
      ./scripts/release.py test-ubuntu-artifacts --config "$CONFIG" --ref "$REF" --build-id "$BUILD_ID" --execute
    fi
    if [[ "$BUILD_ALMA" == "true" ]]; then
      ./scripts/release.py test-alma-artifacts --config "$CONFIG" --ref "$REF" --build-id "$BUILD_ID" --execute
    fi
    summary "Partial candidate completed without staging publish or storage migration gate."
  fi
fi

./scripts/write-release-evidence.py --build-id "$BUILD_ID"

if [[ "$PROMOTE_STABLE" == "true" ]]; then
  ./scripts/codex-release-gate.sh "$BUILD_ID"
  ./scripts/release.py promote --config "$CONFIG" --build-id "$BUILD_ID" --execute
fi

summary "Candidate workflow completed."
