#!/usr/bin/env bash
set -euo pipefail
rm -rf provider-build/deb
mkdir -p provider-build/deb/DEBIAN provider-build/deb/usr/libexec/truenas-libvirt provider-build/deb/etc/truenas-libvirt provider-build/deb/usr/lib/systemd/system provider-build/deb/usr/lib/tmpfiles.d dist
cp packaging/debian-provider/control provider-build/deb/DEBIAN/control
cp packaging/debian-provider/conffiles provider-build/deb/DEBIAN/conffiles
install -m 0755 truenas_provider.py provider-build/deb/usr/libexec/truenas-libvirt/truenas_provider.py
install -m 0755 truenas_provider_daemon.py provider-build/deb/usr/libexec/truenas-libvirt/truenas_provider_daemon.py
install -m 0640 config.example.json provider-build/deb/etc/truenas-libvirt/config.json
install -m 0644 packaging/systemd/truenas-libvirt-provider.service provider-build/deb/usr/lib/systemd/system/truenas-libvirt-provider.service
install -m 0644 packaging/tmpfiles/truenas-libvirt-provider.conf provider-build/deb/usr/lib/tmpfiles.d/truenas-libvirt-provider.conf
dpkg-deb --build provider-build/deb dist/truenas-libvirt-provider_0.1.0-8_all.deb
