#!/usr/bin/env bash
set -euo pipefail
export RPM_ARCH=${RPM_ARCH:-$(rpm --eval "%{_arch}")}
mkdir -p provider-build/rpmbuild/{BUILD,RPMS,SOURCES,SPECS,SRPMS} dist
cp truenas_provider.py truenas_provider_daemon.py config.example.json provider-build/rpmbuild/SOURCES/
cp packaging/systemd/truenas-libvirt-provider.service provider-build/rpmbuild/SOURCES/
cp packaging/tmpfiles/truenas-libvirt-provider.conf provider-build/rpmbuild/SOURCES/
cp packaging/rpm-provider/truenas-libvirt-provider.spec provider-build/rpmbuild/SPECS/
export RPM_PACKAGE_NAME=${RPM_PACKAGE_NAME:-$(rpm --specfile provider-build/rpmbuild/SPECS/truenas-libvirt-provider.spec --qf '%{NAME}\n' | head -1)}
export RPM_PACKAGE_VERSION=${RPM_PACKAGE_VERSION:-$(rpm --specfile provider-build/rpmbuild/SPECS/truenas-libvirt-provider.spec --qf '%{VERSION}\n' | head -1)}
export RPM_PACKAGE_RELEASE=${RPM_PACKAGE_RELEASE:-$(rpm --specfile provider-build/rpmbuild/SPECS/truenas-libvirt-provider.spec --qf '%{RELEASE}\n' | head -1)}
rpmbuild --define "_topdir $(pwd)/provider-build/rpmbuild" -ba provider-build/rpmbuild/SPECS/truenas-libvirt-provider.spec
find provider-build/rpmbuild/RPMS provider-build/rpmbuild/SRPMS -type f -name '*.rpm' -exec cp -a {} dist/ \;
