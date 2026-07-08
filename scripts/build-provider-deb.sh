#!/usr/bin/env bash
set -euo pipefail
rm -rf provider-build/deb
mkdir -p provider-build/deb/DEBIAN provider-build/deb/usr/libexec/truenas-libvirt provider-build/deb/etc/truenas-libvirt provider-build/deb/usr/lib/systemd/system provider-build/deb/usr/lib/tmpfiles.d dist
rm -f dist/truenas-libvirt-provider_*_all.deb
cp packaging/debian-provider/control provider-build/deb/DEBIAN/control
PROVIDER_VERSION=$(python3 - <<'PYVERSION'
import json
with open("release/subvirt-version.json", encoding="utf-8") as handle:
    data = json.load(handle)
provider = data["provider"]
print("{}-{}".format(provider["version"], provider["release"]))
PYVERSION
)
python3 - "$PROVIDER_VERSION" provider-build/deb/DEBIAN/control <<'PYCONTROL'
from pathlib import Path
import re
import sys
version = sys.argv[1]
control = Path(sys.argv[2])
text = control.read_text(encoding="utf-8")
text = re.sub(r"^Version:\s*.*$", f"Version: {version}", text, flags=re.M)
control.write_text(text, encoding="utf-8")
PYCONTROL
cp packaging/debian-provider/conffiles provider-build/deb/DEBIAN/conffiles
install -m 0755 packaging/debian-provider/postinst provider-build/deb/DEBIAN/postinst
install -m 0755 packaging/debian-provider/postrm provider-build/deb/DEBIAN/postrm
install -m 0755 truenas_provider.py provider-build/deb/usr/libexec/truenas-libvirt/truenas_provider.py
install -m 0755 truenas_provider_daemon.py provider-build/deb/usr/libexec/truenas-libvirt/truenas_provider_daemon.py
install -m 0640 config.example.json provider-build/deb/etc/truenas-libvirt/config.json
install -m 0644 packaging/systemd/truenas-libvirt-provider.service provider-build/deb/usr/lib/systemd/system/truenas-libvirt-provider.service
install -m 0644 packaging/tmpfiles/truenas-libvirt-provider.conf provider-build/deb/usr/lib/tmpfiles.d/truenas-libvirt-provider.conf
dpkg-deb --build provider-build/deb "dist/truenas-libvirt-provider_${PROVIDER_VERSION}_all.deb"
