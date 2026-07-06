# Architecture

## Goal

Expose TrueNAS zvols as libvirt storage volumes so KVM/libvirt hosts can create,
discover, attach, and migrate guests using shared TrueNAS-backed block storage.

## Components

- `truenas-libvirt-provider`: local provider daemon that talks to the TrueNAS API and host transport tools.
- Libvirt storage backend patch: adds `type='truenas'` storage pools and delegates TrueNAS-specific operations to the provider over a local Unix socket.
- Virt-manager patch: exposes TrueNAS pool creation fields and enables volume creation for `truenas` pools.
- Distro packages: patched libvirt, patched virt-manager, and the provider for Ubuntu 24.04 and AlmaLinux 10.

## TrueNAS API

The provider authenticates to the TrueNAS JSON-RPC WebSocket API with
`auth.login_ex` and `API_KEY_PLAIN`. Runtime configuration lives in
`/etc/truenas-libvirt/config.json` and identifies:

- management API URL
- dedicated TrueNAS username
- root-readable API key file
- TLS verification mode
- storage target IP used for iSCSI and NVMe-oF exports

Secrets are intentionally stored outside the repository. The provider `doctor`
command verifies that the configured API account exposes the TrueNAS methods
Subvirt needs for the selected transport, including dataset delete permission for
libvirt volume removal.

## Storage Model

Subvirt exposes one libvirt pool per TrueNAS pool and transport. A libvirt volume
maps to one provider-managed TrueNAS zvol under a managed dataset namespace.

Example:

```text
libvirt pool:   truenas-tank-iscsi
libvirt volume: vm01-data
TrueNAS zvol:   tank/libvirt/vm01-data
transport:      iscsi
```

NVMe-oF uses the same model with `<protocol type='nvmeof'/>`.

## Cluster Model

The first cluster model is shared-discovery, single-writer:

- every approved hypervisor can discover managed exports
- every host uses the same libvirt pool definitions
- each host keeps its own local iSCSI IQN and NVMe host NQN
- libvirt/QEMU locking remains responsible for preventing unsafe concurrent VM disk use
- live migration should not require storage ACL changes in the migration path

Dynamic per-host grant/revoke can be added later after the basic migration path
is stable.

## Pool XML

Example pool definitions live in `examples/`:

- `storage-pool-tank-iscsi.xml`
- `storage-pool-tank-nvmeof.xml`

The source `<name>` is the TrueNAS pool name. The source `<protocol>` is either
`iscsi` or `nvmeof`. The target path should be `/dev/disk/by-id` so libvirt uses
stable block-device paths.

## Milestones

Implemented core path:

- TrueNAS API authentication and pool discovery
- managed dataset creation
- zvol-backed volume create/list/path/delete workflow
- iSCSI export/discovery/login
- NVMe-oF export/discovery/connect
- libvirt `truenas` pool type and volume creation
- virt-manager pool creation and volume creation support
- public apt/yum repositories and automated package publishing

Planned next areas:

- fully automated ephemeral TrueNAS lab
- automated migration gate
- provider-level zvol snapshots
- richer provider-level zvol snapshot management
