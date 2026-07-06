# Implementation Status

## Implemented

- TrueNAS provider daemon with Unix-socket JSON-RPC protocol and `doctor` diagnostics.
- Provider methods: `health.check`, `pool.list`, `pool.refresh`, `vol.list`, `vol.create`, `vol.clone`, `vol.resize`, `vol.path`, and `vol.delete`.
- Libvirt `truenas` storage pool backend for Ubuntu 24.04 and AlmaLinux 10 package builds.
- Grow-only resize support through `virsh vol-resize`; shrink and `--allocate` are rejected.
- Same-pool CoW clone support through `virsh vol-clone`.
- Strict delete behavior with `--delete-snapshots` support for safe managed snapshot cleanup.
- Virt-manager support for TrueNAS pool creation and volume creation.
- iSCSI and NVMe-oF transports through host-native tools.
- Public apt and yum repositories at `repo.subvirt.net`.
- Automated upstream package polling, candidate builds, lab package install tests, and public stable publishing.
- Tracked static website deployment for `subvirt.net`.

## Validated Behavior

The current package path has validated these workflows in lab environments:

- provider service startup, health checks, and `doctor` diagnostics
- `virsh pool-capabilities` advertising `type='truenas'`
- defining, starting, refreshing, and listing TrueNAS pools
- creating iSCSI-backed and NVMe-oF-backed volumes with `virsh vol-create-as`
- resolving volumes to stable `/dev/disk/by-id` paths
- reporting managed dataset capacity instead of summing zvol sizes
- package-manager installation from apt and yum repositories
- patched virt-manager detecting `truenas` pools as volume-creation capable

## Known Limitations

- Full ephemeral CI can create fresh Linux test VMs, but fully unattended TrueNAS VM setup is still pending.
- Automated live-migration validation exists as an opt-in iSCSI-backed smoke gate, checks QEMU machine-type compatibility, and is not enabled in the default release gate.
- General provider-level snapshot create/list/delete support is planned but not implemented.
- Volume shrinking and resize `--allocate` are intentionally unsupported.

## Next Work

- Finish the full ephemeral lab with TrueNAS included.
- Stabilize and then enable the disposable-guest migration gate by default.
- Implement general provider-level zvol snapshot management.
- Define and enforce package retention for public repositories.
- Add stronger automated delete dependency tests after package rebuild.
