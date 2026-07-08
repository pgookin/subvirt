#!/bin/sh
set -eu

PUBLISH_USER=${PUBLISH_USER:-subvirt-publish}
GPG_NAME=${GPG_NAME:-Subvirt Repository <repo@subvirt.net>}
AUTHORIZED_KEY_FILE=${1:-}

dnf install -y createrepo_c rpm-sign rsync gnupg2 zstd policycoreutils-python-utils

if ! id "$PUBLISH_USER" >/dev/null 2>&1; then
    useradd --system --create-home --home-dir "/var/lib/$PUBLISH_USER" --shell /bin/bash "$PUBLISH_USER"
fi

install -d -m 0755 -o "$PUBLISH_USER" -g "$PUBLISH_USER" /srv/repo/www /srv/www /srv/subvirt/incoming /srv/subvirt/tools
install -m 0755 -o "$PUBLISH_USER" -g "$PUBLISH_USER" scripts/publish-repo.py /srv/subvirt/tools/publish-repo.py

if [ -n "$AUTHORIZED_KEY_FILE" ]; then
    install -d -m 0700 -o "$PUBLISH_USER" -g "$PUBLISH_USER" "/var/lib/$PUBLISH_USER/.ssh"
    install -m 0600 -o "$PUBLISH_USER" -g "$PUBLISH_USER" "$AUTHORIZED_KEY_FILE" "/var/lib/$PUBLISH_USER/.ssh/authorized_keys"
fi

if ! runuser -u "$PUBLISH_USER" -- gpg --batch --list-secret-keys "$GPG_NAME" >/dev/null 2>&1; then
    runuser -u "$PUBLISH_USER" -- gpg --batch --pinentry-mode loopback --passphrase '' --quick-generate-key "$GPG_NAME" rsa4096 sign 0
fi

semanage fcontext -a -t httpd_sys_content_t '/srv/repo/www(/.*)?' 2>/dev/null || semanage fcontext -m -t httpd_sys_content_t '/srv/repo/www(/.*)?'
semanage fcontext -a -t httpd_sys_content_t '/srv/www(/.*)?' 2>/dev/null || semanage fcontext -m -t httpd_sys_content_t '/srv/www(/.*)?'
restorecon -RF /srv/repo/www /srv/www /srv/subvirt/incoming /srv/subvirt/tools || true

nginx -t
