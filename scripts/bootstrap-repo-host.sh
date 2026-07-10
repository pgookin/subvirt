#!/bin/sh
set -eu

dnf install -y nginx createrepo_c rpm-sign rsync gnupg2 zstd policycoreutils-python-utils firewalld

install -d -m 0755 /srv/repo/www /srv/repo/www/archive /srv/subvirt/incoming /usr/local/libexec/subvirt
install -m 0755 scripts/publish-repo.py /usr/local/libexec/subvirt/publish-repo.py

if ! gpg --batch --list-secret-keys "Subvirt Repository" >/dev/null 2>&1; then
    gpg --batch --pinentry-mode loopback --passphrase '' --quick-generate-key \
        "Subvirt Repository <repo@subvirt.local>" rsa4096 sign 0
fi

cat >/etc/nginx/nginx.conf <<'EOF'
user nginx;
worker_processes auto;
error_log /var/log/nginx/error.log notice;
pid /run/nginx.pid;

include /usr/share/nginx/modules/*.conf;

events {
    worker_connections 1024;
}

http {
    access_log /var/log/nginx/access.log;
    sendfile on;
    tcp_nopush on;
    keepalive_timeout 65;
    include /etc/nginx/mime.types;
    default_type application/octet-stream;

    server {
        listen 80 default_server;
        listen [::]:80 default_server;
        server_name _;
        root /srv/repo/www;

        location / {
            autoindex on;
            try_files $uri $uri/ =404;
        }
    }
}
EOF
rm -f /etc/nginx/conf.d/subvirt-repo.conf

semanage fcontext -a -t httpd_sys_content_t '/srv/repo/www(/.*)?' 2>/dev/null || semanage fcontext -m -t httpd_sys_content_t '/srv/repo/www(/.*)?'
restorecon -RF /srv/repo/www /srv/subvirt/incoming /usr/local/libexec/subvirt /etc/nginx/nginx.conf || true
firewall-cmd --permanent --add-service=http
firewall-cmd --reload
systemctl enable --now nginx
systemctl restart nginx
nginx -t
