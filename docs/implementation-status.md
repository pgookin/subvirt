# Implementation Status

## Implemented

- TrueNAS provider daemon with Unix-socket JSON-RPC protocol.
- Provider methods: `health.check`, `pool.list`, `pool.refresh`, `vol.list`, `vol.create`, `vol.path`, and `vol.delete`.
- Libvirt `truenas` storage pool backend for Ubuntu 24.04 and AlmaLinux 10 package builds.
- Virt-manager support for TrueNAS pool creation and volume creation.
- iSCSI and NVMe-oF transports through host-native tools.
- Public apt and yum repositories at `repo.subvirt.net`.
- Automated upstream package polling, candidate builds, lab package install tests, and public stable publishing.
- Tracked static website deployment for `subvirt.net`.

## Validated Behavior

The current package path has validated these workflows in lab environments:

- provider service startup and health checks
- `virsh pool-capabilities` advertising `type='truenas'`
- defining, starting, refreshing, and listing TrueNAS pools
- creating iSCSI-backed and NVMe-oF-backed volumes with `virsh vol-create-as`
- resolving volumes to stable `/dev/disk/by-id` paths
- reporting managed dataset capacity instead of summing zvol sizes
- package-manager installation from apt and yum repositories
- patched virt-manager detecting `truenas` pools as volume-creation capable

## Known Limitations

- `vol.delete` currently cleans export records, but complete zvol deletion depends on confirming the required TrueNAS API permission/method exposure for the configured user.
- Full ephemeral CI can create fresh Linux test VMs, but fully unattended TrueNAS VM setup is still pending.
- Automated live-migration validation is planned but not yet enabled in the default release gate.
- Snapshot create/list/delete/clone support is planned but not implemented.

## Next Work

- Finish the full ephemeral lab with TrueNAS included.
- Add the automated migration gate using a disposable guest.
- Implement provider-level zvol snapshots.
- Define and enforce package retention for public repositories.
- Add a concise health/doctor command for operator troubleshooting.
