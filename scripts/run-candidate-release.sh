#!/usr/bin/env bash
set -euo pipefail

CONFIG=${SUBVIRT_RELEASE_CONFIG:-/srv/subvirt/release/release.json}
BUILD_ID=${BUILD_ID:?BUILD_ID is required}
REF=${REF:-main}
REFRESH_SOURCES=${REFRESH_SOURCES:-false}
UBUNTU_VERSION=${UBUNTU_VERSION:-}
UBUNTU_VERSIONS=${UBUNTU_VERSIONS:-}
UBUNTU_TARGETS=${UBUNTU_TARGETS:-${SUBVIRT_UBUNTU_TARGETS:-ubuntu-24.04}}
export SUBVIRT_UBUNTU_TARGETS="$UBUNTU_TARGETS"
ALMA_VERSION=${ALMA_VERSION:-}
ALMA_VERSIONS=${ALMA_VERSIONS:-}
ALMA_TARGETS=${ALMA_TARGETS:-${SUBVIRT_ALMA_TARGETS:-almalinux-10}}
export SUBVIRT_ALMA_TARGETS="$ALMA_TARGETS"
PROMOTE_STABLE=${PROMOTE_STABLE:-false}
BUILD_UBUNTU=${BUILD_UBUNTU:-true}
BUILD_ALMA=${BUILD_ALMA:-true}
BUILD_SCOPE=${BUILD_SCOPE:-full}
REQUIRE_CODEX_GATE=${REQUIRE_CODEX_GATE:-false}

CANDIDATE_LOG_DIR=${CANDIDATE_LOG_DIR:-artifacts/$BUILD_ID}
CANDIDATE_LOG_FILE=${CANDIDATE_LOG_FILE:-$CANDIDATE_LOG_DIR/candidate-release.log}
if [[ "${SUBVIRT_CANDIDATE_LOGGING:-true}" == "true" ]]; then
  mkdir -p "$CANDIDATE_LOG_DIR"
  exec > >(tee -a "$CANDIDATE_LOG_FILE") 2>&1
  echo "Candidate release log: $CANDIDATE_LOG_FILE"
fi

collect_failure_diagnostics() {
  local rc=$?
  trap - ERR
  echo "Candidate release failed with exit code $rc; collecting diagnostics"
  ./scripts/collect-candidate-diagnostics.py \
    --build-id "$BUILD_ID" \
    --config "$CONFIG" \
    --candidate-log "$CANDIDATE_LOG_FILE" || echo "Candidate diagnostic collection failed; preserving original failure"
  exit "$rc"
}

trap collect_failure_diagnostics ERR

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
summary "- Build scope: \`$BUILD_SCOPE\`"
summary "- Require Codex gate: \`$REQUIRE_CODEX_GATE\`"

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

case "$BUILD_SCOPE" in
  full|provider) ;;
  *)
    echo "BUILD_SCOPE must be full or provider" >&2
    exit 1
    ;;
esac

if [[ "$BUILD_SCOPE" == "full" && "$BUILD_UBUNTU" != "true" && "$BUILD_ALMA" != "true" ]]; then
  echo "At least one of BUILD_UBUNTU or BUILD_ALMA must be true" >&2
  exit 1
fi

if [[ "$BUILD_SCOPE" == "provider" && "$REFRESH_SOURCES" == "true" ]]; then
  echo "Provider-only candidate builds cannot refresh libvirt sources" >&2
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
    if [[ -n "$UBUNTU_VERSIONS" ]]; then
      IFS=',' read -r -a ubuntu_pairs <<< "$UBUNTU_VERSIONS"
      for pair in "${ubuntu_pairs[@]}"; do
        target=${pair%%=*}
        version=${pair#*=}
        test -n "$target"
        test -n "$version"
        q_ubuntu_target=$(remote_quote "$target")
        q_ubuntu_version=$(remote_quote "$version")
        refresh_commands+=("./scripts/refresh-libvirt-source.py --config $q_config --distro ubuntu --ubuntu-target $q_ubuntu_target --version $q_ubuntu_version")
      done
    else
      test -n "$UBUNTU_VERSION"
      q_ubuntu_version=$(remote_quote "$UBUNTU_VERSION")
      refresh_commands+=("./scripts/refresh-libvirt-source.py --config $q_config --distro ubuntu --ubuntu-target ubuntu-24.04 --version $q_ubuntu_version")
    fi
  fi
  if [[ "$BUILD_ALMA" == "true" ]]; then
    if [[ -n "$ALMA_VERSIONS" ]]; then
      IFS=',' read -r -a alma_pairs <<< "$ALMA_VERSIONS"
      for pair in "${alma_pairs[@]}"; do
        target=${pair%%=*}
        version=${pair#*=}
        test -n "$target"
        test -n "$version"
        q_alma_target=$(remote_quote "$target")
        q_alma_version=$(remote_quote "$version")
        refresh_commands+=("./scripts/refresh-libvirt-source.py --config $q_config --distro alma --alma-target $q_alma_target --version $q_alma_version")
      done
    else
      test -n "$ALMA_VERSION"
      q_alma_version=$(remote_quote "$ALMA_VERSION")
      refresh_commands+=("./scripts/refresh-libvirt-source.py --config $q_config --distro alma --alma-target almalinux-10 --version $q_alma_version")
    fi
  fi
  if [[ ${#refresh_commands[@]} -eq 0 ]]; then
    echo "REFRESH_SOURCES=true but no refresh commands were derived" >&2
    exit 1
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

if [[ "$BUILD_SCOPE" == "provider" ]]; then
  ./scripts/release.py build-provider --config "$CONFIG" --ref "$REF" --build-id "$BUILD_ID" --execute
  ./scripts/release.py collect --config "$CONFIG" --build-id "$BUILD_ID" --execute
  if [[ "$LAB_ENABLED" != "true" ]]; then
    echo "Provider-only candidates require lab.enabled=true so fresh VMs can test the staged provider against stable libvirt packages" >&2
    exit 1
  fi
  ./scripts/release.py test-lab --config "$CONFIG" --ref "$REF" --build-id "$BUILD_ID" --lab-mode provider --execute
  summary "Provider-only candidate tested in ephemeral lab with stable libvirt packages and staged provider packages."
elif [[ "$BUILD_UBUNTU" == "true" && "$BUILD_ALMA" == "true" ]]; then
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
./scripts/verify-release-evidence.py --build-id "$BUILD_ID" --scope "$BUILD_SCOPE"

if [[ "$PROMOTE_STABLE" == "true" ]]; then
  if [[ "$REQUIRE_CODEX_GATE" == "true" ]]; then
    ./scripts/codex-release-gate.sh "$BUILD_ID"
  else
    ./scripts/codex-release-gate.sh "$BUILD_ID" || echo "Codex promotion review did not pass; continuing because REQUIRE_CODEX_GATE=false"
  fi
  ./scripts/release.py publish-public-stable --config "$CONFIG" --build-id "$BUILD_ID" --execute
  ./scripts/release.py verify-public-stable --config "$CONFIG" --build-id "$BUILD_ID" --execute
fi

summary "Candidate workflow completed."
