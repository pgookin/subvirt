# Getting Started

Subvirt adds a TrueNAS-backed storage pool type to libvirt. After setup, a
libvirt host can create VM disks as TrueNAS zvols and attach them through iSCSI
or NVMe-oF.

This guide covers Ubuntu 24.04 and AlmaLinux 10 hosts.

## Requirements

- TrueNAS 25.10.x with one or more storage pools.
- A dedicated TrueNAS API user and API key.
- Network access from each libvirt host to the TrueNAS API endpoint.
- Network access from each libvirt host to the TrueNAS storage IP for iSCSI,
  NVMe-oF, or both.
- Root access on the libvirt host.

Use the same TrueNAS API endpoint and storage IP on every host that should be
able to run or migrate guests backed by the same Subvirt storage pools.

## Add the Ubuntu 24.04 Repo

```sh
sudo install -d -m 0755 /usr/share/keyrings
curl -fsSL https://repo.subvirt.net/keys/subvirt.gpg | sudo tee /usr/share/keyrings/subvirt-archive-keyring.gpg >/dev/null

sudo tee /etc/apt/sources.list.d/subvirt.sources >/dev/null <<'EOF'
Types: deb
URIs: https://repo.subvirt.net/apt/ubuntu
Suites: noble
Components: stable
Signed-By: /usr/share/keyrings/subvirt-archive-keyring.gpg
EOF

sudo apt update
```

Install the host packages:

```sh
sudo DEBIAN_FRONTEND=noninteractive apt install -y \
  truenas-libvirt-provider \
  libvirt-daemon-system \
  libvirt-daemon-driver-qemu \
  libvirt-daemon-driver-storage-truenas \
  virt-manager \
  virtinst \
  open-iscsi \
  nvme-cli
```

## Add the AlmaLinux 10 Repo

```sh
sudo tee /etc/yum.repos.d/subvirt.repo >/dev/null <<'EOF'
[subvirt-stable]
name=Subvirt stable packages
baseurl=https://repo.subvirt.net/yum/almalinux/10/stable
enabled=1
gpgcheck=1
repo_gpgcheck=1
gpgkey=https://repo.subvirt.net/keys/subvirt.asc
EOF

sudo dnf makecache
```

Install the host packages:

```sh
sudo dnf install -y \
  truenas-libvirt-provider \
  libvirt-daemon-kvm \
  libvirt-daemon-driver-storage-truenas \
  virt-manager \
  virt-manager-common \
  virt-install \
  iscsi-initiator-utils \
  nvme-cli \
  kmod
```

## Prepare TrueNAS

Create a dedicated TrueNAS user for Subvirt and create an API key for that user.
The current provider authenticates with `auth.login_ex` using `API_KEY_PLAIN`.

The user must be able to:

- read system information and list pools
- query, create, update, and delete datasets and zvols under the managed dataset
- query, create, clone, and delete snapshots used for Subvirt clone workflows
- query, create, update, and delete iSCSI objects when using iSCSI
- query, create, update, and delete NVMe-oF objects when using NVMe-oF
- query, start, reload, and enable the TrueNAS iSCSI/NVMe-oF services

For initial testing, use a dedicated TrueNAS account with Full Admin access or a
custom role that exposes every method reported by `doctor`. A Sharing Admin role
can create some storage objects, but it may not expose `pool.dataset.delete`,
which means libvirt volume deletion will fail after creation succeeds.

## Configure the Provider

Store the API key in a root-readable file:

```sh
sudo install -d -m 0750 /etc/truenas-libvirt
sudo tee /etc/truenas-libvirt/api-key >/dev/null <<'EOF'
PASTE_API_KEY_HERE
EOF
sudo chmod 0600 /etc/truenas-libvirt/api-key
```

Create `/etc/truenas-libvirt/config.json`:

```sh
sudo tee /etc/truenas-libvirt/config.json >/dev/null <<'EOF'
{
  "truenas": {
    "url": "wss://TRUENAS_MANAGEMENT_IP/api/current",
    "username": "libvirt_user",
    "api_key_file": "/etc/truenas-libvirt/api-key",
    "tls_verify": false,
    "target_ip": "TRUENAS_STORAGE_IP"
  }
}
EOF
sudo chmod 0640 /etc/truenas-libvirt/config.json
```

Use `tls_verify: true` when the TrueNAS API presents a certificate trusted by
the host. Use `tls_verify: false` for a lab with a self-signed certificate.

## Enable Host Transport Services

iSCSI requires an initiator name and a running `iscsid.service`.

Ubuntu and AlmaLinux:

```sh
sudo systemctl enable --now iscsid
```

NVMe-oF over TCP requires a host NQN and the `nvme-tcp` kernel module:

```sh
sudo modprobe nvme-tcp
```

The provider checks these prerequisites before creating or refreshing volumes.

## Start the Provider

```sh
sudo systemctl enable --now truenas-libvirt-provider.service
sudo systemctl restart truenas-libvirt-provider.service
sudo systemctl status truenas-libvirt-provider.service
```

Run the diagnostic check:

```sh
sudo /usr/libexec/truenas-libvirt/truenas_provider_daemon.py doctor
```

For automation or detailed troubleshooting, use JSON output:

```sh
sudo /usr/libexec/truenas-libvirt/truenas_provider_daemon.py doctor --json
sudo /usr/libexec/truenas-libvirt/truenas_provider_daemon.py doctor --transport iscsi
sudo /usr/libexec/truenas-libvirt/truenas_provider_daemon.py doctor --transport nvmeof
```

The command exits successfully only when required config, TrueNAS API, API
permissions, and host transport checks pass. Storage-port reachability is
reported as a warning because TrueNAS services may start on demand when the first
export is created. If `truenas.permissions` fails, run `doctor --json`; the
`permissions.missing` list names the TrueNAS API methods the configured account
cannot use.

## Create a TrueNAS Storage Pool

Create one libvirt pool per TrueNAS pool and transport. Replace `tank` with the
TrueNAS pool name you want to use.

iSCSI:

```sh
cat >/tmp/truenas-tank-iscsi.xml <<'EOF'
<pool type='truenas'>
  <name>truenas-tank-iscsi</name>
  <source>
    <name>tank</name>
    <protocol type='iscsi'/>
  </source>
  <target>
    <path>/dev/disk/by-id</path>
  </target>
</pool>
EOF

sudo virsh pool-define /tmp/truenas-tank-iscsi.xml
sudo virsh pool-start truenas-tank-iscsi
sudo virsh pool-autostart truenas-tank-iscsi
```

NVMe-oF:

```sh
cat >/tmp/truenas-tank-nvmeof.xml <<'EOF'
<pool type='truenas'>
  <name>truenas-tank-nvmeof</name>
  <source>
    <name>tank</name>
    <protocol type='nvmeof'/>
  </source>
  <target>
    <path>/dev/disk/by-id</path>
  </target>
</pool>
EOF

sudo virsh pool-define /tmp/truenas-tank-nvmeof.xml
sudo virsh pool-start truenas-tank-nvmeof
sudo virsh pool-autostart truenas-tank-nvmeof
```

Refresh and inspect the pool:

```sh
sudo virsh pool-refresh truenas-tank-iscsi
sudo virsh pool-info truenas-tank-iscsi
sudo virsh vol-list truenas-tank-iscsi
```

## Create a Volume

Create a 64 GiB iSCSI-backed volume:

```sh
sudo virsh vol-create-as truenas-tank-iscsi vm01-root 64G
sudo virsh vol-list truenas-tank-iscsi
sudo virsh vol-path --pool truenas-tank-iscsi vm01-root
```

Create a 64 GiB NVMe-oF-backed volume:

```sh
sudo virsh vol-create-as truenas-tank-nvmeof vm01-fast 64G
sudo virsh vol-list truenas-tank-nvmeof
sudo virsh vol-path --pool truenas-tank-nvmeof vm01-fast
```

## Resize a Volume

Subvirt supports grow-only zvol resize through libvirt. This changes the backing
TrueNAS zvol size; it does not resize guest partitions or filesystems.

```sh
sudo virsh vol-resize --pool truenas-tank-iscsi vm01-root 128G
sudo virsh pool-refresh truenas-tank-iscsi
sudo virsh vol-info --pool truenas-tank-iscsi vm01-root
```

Shrinking and `--allocate` are intentionally unsupported.

## Clone a Volume

Subvirt supports same-pool CoW zvol clones through libvirt. Clones are fast and
space-efficient, but TrueNAS keeps a snapshot dependency between the source and
the clone until the dependency is removed.

```sh
sudo virsh vol-clone --pool truenas-tank-iscsi vm01-root vm01-root-copy
sudo virsh pool-refresh truenas-tank-iscsi
sudo virsh vol-info --pool truenas-tank-iscsi vm01-root-copy
```

Delete is strict by default. If a volume has Subvirt-managed clone snapshots,
retry with `--delete-snapshots`; unmanaged or dependent snapshots are refused.

```sh
sudo virsh vol-delete --pool truenas-tank-iscsi vm01-root-copy
sudo virsh vol-delete --pool truenas-tank-iscsi vm01-root --delete-snapshots
```

You can also create volumes from patched `virt-manager` after installing the
Subvirt `virt-manager` package on the workstation that runs the UI.

## Multiple Hosts and Migration

Configure every hypervisor with the same TrueNAS provider config, repo packages,
and pool XML. Each host must have its own local iSCSI IQN and NVMe host NQN.

For migration, define the same Subvirt pool name on each host. The same volume
name must resolve to the same TrueNAS zvol, and the disk source path used by the
guest must exist on both the source and destination hypervisor. Device discovery
names can differ between initiators, so production clusters should standardize
shared storage paths before relying on live migration.

## Troubleshooting

Run the provider diagnostic first:

```sh
sudo /usr/libexec/truenas-libvirt/truenas_provider_daemon.py doctor
```

If the provider service itself looks unhealthy, inspect systemd logs:

```sh
sudo systemctl status truenas-libvirt-provider.service
sudo journalctl -u truenas-libvirt-provider.service -n 100 --no-pager
```

Check the libvirt storage backend:

```sh
sudo virsh pool-capabilities | grep -A5 truenas
```

Check iSCSI:

```sh
cat /etc/iscsi/initiatorname.iscsi
systemctl is-active iscsid
iscsiadm -m session
```

Check NVMe-oF:

```sh
cat /etc/nvme/hostnqn
lsmod | grep nvme_tcp
nvme list-subsys
```

Common problems:

- `TrueNAS provider config file not found`: create
  `/etc/truenas-libvirt/config.json`.
- `TrueNAS API key file not found`: create the configured `api_key_file`.
- `truenas.permissions`: grant the configured TrueNAS user the missing API
  methods shown by `doctor --json`, or use a dedicated Full Admin account for
  initial testing.
- `iSCSI transport requires iscsid.service`: enable and start `iscsid`.
- `NVMe-oF transport requires the nvme-tcp kernel module`: run
  `modprobe nvme-tcp`.
- `Pool does not support volume creation` in virt-manager: install the Subvirt
  `virt-manager` package on the machine running virt-manager, not only on the
  remote hypervisor.
