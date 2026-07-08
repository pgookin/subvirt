#!/usr/bin/env bash
set -euo pipefail
DIST=${SUBVIRT_UBUNTU_DIST:-noble}
ARCH=${SUBVIRT_UBUNTU_ARCH:-amd64}
SRC_DIR=${SUBVIRT_UBUNTU_LIBVIRT_SRC:-build/libvirt-u24-10.0.0}
mkdir -p dist
rm -f build/*.deb build/*.buildinfo build/*.changes
if [[ ! -d "$SRC_DIR/debian" ]]; then
  echo "$SRC_DIR/debian is missing" >&2
  exit 1
fi
if compgen -G "build/*.patch" >/dev/null; then
  ./scripts/validate-unified-diff.py build/*.patch
fi
if [[ "${SUBVIRT_NATIVE_BUILD:-0}" == "1" ]]; then
  (cd "$SRC_DIR" && dpkg-buildpackage -us -uc -b)
  find build -maxdepth 1 -type f \( -name '*.deb' -o -name '*.buildinfo' -o -name '*.changes' \) -exec cp -a {} dist/ \;
  exit 0
fi
(
  cd "$SRC_DIR"
  dpkg-buildpackage -S -d -us -uc
)
DSC=$(find build -maxdepth 1 -type f \( -name 'libvirt_*+truenas*.dsc' -o -name 'libvirt_*.dsc' \) | sort | tail -1)
if [[ -z "$DSC" ]]; then
  echo "failed to find generated libvirt .dsc" >&2
  exit 1
fi
sbuild --dist="$DIST" --arch="$ARCH" --no-run-lintian --build-dir=build "$DSC"
find build -maxdepth 1 -type f \( -name '*.deb' -o -name '*.buildinfo' -o -name '*.changes' \) -exec cp -a {} dist/ \;
