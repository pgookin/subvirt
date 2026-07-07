#!/usr/bin/env bash
set -euo pipefail

ROOT=$(pwd)
WORKDIR=${SUBVIRT_VIRT_MANAGER_DEB_WORKDIR:-provider-build/virt-manager-deb}
DIST=${SUBVIRT_UBUNTU_DIST:-noble}
export DEBFULLNAME=${DEBFULLNAME:-SubVirt Builder}
export DEBEMAIL=${DEBEMAIL:-builder@subvirt.invalid}

ensure_deb_src() {
  if apt-cache showsrc virt-manager >/dev/null 2>&1; then
    return
  fi
  cat >/etc/apt/sources.list.d/subvirt-src.sources <<SRC
Types: deb-src
URIs: http://archive.ubuntu.com/ubuntu
Suites: ${DIST} ${DIST}-updates
Components: main restricted universe multiverse
Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg

Types: deb-src
URIs: http://security.ubuntu.com/ubuntu
Suites: ${DIST}-security
Components: main restricted universe multiverse
Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg
SRC
  apt-get update
}

rm -rf "$WORKDIR"
mkdir -p "$WORKDIR" dist
ensure_deb_src
(
  cd "$WORKDIR"
  apt-get source virt-manager
)
SRC_DIR=$(find "$WORKDIR" -maxdepth 1 -type d -name 'virt-manager-*' | sort | tail -1)
if [[ -z "$SRC_DIR" ]]; then
  echo "failed to locate extracted virt-manager source" >&2
  exit 1
fi
mk-build-deps -i -r -t "apt-get -y --no-install-recommends" "$SRC_DIR/debian/control"
(
  cd "$SRC_DIR"
  "$ROOT/scripts/patch-virt-manager-truenas.py" .
  BASE_VERSION=$(dpkg-parsechangelog -S Version)
  LOCAL_REVISION=$("$ROOT/scripts/subvirt_versions.py" ubuntu-virt-manager-revision)
  dch --newversion "${BASE_VERSION}+truenas${LOCAL_REVISION}" --distribution "$DIST" --force-distribution \
    "Enable TrueNAS storage pool volume creation in virt-manager."
  "$ROOT/scripts/check-virt-manager-truenas.py" --static --source-root .
  dpkg-buildpackage -us -uc -b
)
find "$WORKDIR" -maxdepth 1 -type f \( -name '*.deb' -o -name '*.changes' -o -name '*.buildinfo' \) -exec cp -a {} dist/ \;
