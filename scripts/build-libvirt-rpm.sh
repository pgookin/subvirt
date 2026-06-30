#!/usr/bin/env bash
set -euo pipefail
export RPM_ARCH=${RPM_ARCH:-$(rpm --eval "%{_arch}")}
MOCK_CONFIG=${SUBVIRT_MOCK_CONFIG:-almalinux-10-x86_64}
RPM_BUILD_JOBS=${SUBVIRT_RPM_BUILD_JOBS:-2}
TOPDIR=$(pwd)/provider-build/libvirt-rpmbuild
rm -rf "$TOPDIR"
mkdir -p "$TOPDIR"/{BUILD,RPMS,SOURCES,SPECS,SRPMS} dist
if [[ ! -f build/libvirt.spec ]]; then
  echo "build/libvirt.spec is missing" >&2
  exit 1
fi
if compgen -G "build/*.patch" >/dev/null; then
  ./scripts/validate-unified-diff.py build/*.patch
fi
cp build/libvirt.spec "$TOPDIR/SPECS/libvirt.spec"
export RPM_PACKAGE_NAME=${RPM_PACKAGE_NAME:-$(rpm --specfile "$TOPDIR/SPECS/libvirt.spec" --qf '%{NAME}\n' | head -1)}
export RPM_PACKAGE_VERSION=${RPM_PACKAGE_VERSION:-$(rpm --specfile "$TOPDIR/SPECS/libvirt.spec" --qf '%{VERSION}\n' | head -1)}
export RPM_PACKAGE_RELEASE=${RPM_PACKAGE_RELEASE:-$(rpm --specfile "$TOPDIR/SPECS/libvirt.spec" --qf '%{RELEASE}\n' | head -1)}
find build -maxdepth 1 -type f \( -name '*.patch' -o -name '*.tar.xz' -o -name '*.tar.gz' \) -exec cp -a {} "$TOPDIR/SOURCES/" \;
if [[ "${SUBVIRT_NATIVE_BUILD:-0}" == "1" ]]; then
  rpmbuild --define "_topdir $TOPDIR" --define "_smp_mflags -j$RPM_BUILD_JOBS" --define "_smp_build_ncpus $RPM_BUILD_JOBS" -ba "$TOPDIR/SPECS/libvirt.spec"
else
  rpmbuild --define "_topdir $TOPDIR" --define "_smp_mflags -j$RPM_BUILD_JOBS" --define "_smp_build_ncpus $RPM_BUILD_JOBS" -bs "$TOPDIR/SPECS/libvirt.spec"
  SRPM=$(find "$TOPDIR/SRPMS" -type f -name '*.src.rpm' | sort | tail -1)
  if [[ -z "$SRPM" ]]; then
    echo "failed to build source RPM" >&2
    exit 1
  fi
  mock -r "$MOCK_CONFIG" --resultdir "$TOPDIR/mock-results" --rebuild "$SRPM"
  find "$TOPDIR/mock-results" -type f -name '*.rpm' -exec cp -a {} dist/ \;
  exit 0
fi
find "$TOPDIR/RPMS" "$TOPDIR/SRPMS" -type f -name '*.rpm' -exec cp -a {} dist/ \;
