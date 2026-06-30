# Implementation Status

## Verified provider behavior

- TrueNAS API endpoint: `wss://10.6.0.119/api/current`
- Unix JSON-RPC provider daemon: `truenas_provider_daemon.py`
- Provider socket protocol verified on both hypervisors.
- Provider methods implemented: `health.check`, `pool.refresh`, `vol.list`, `vol.create`, `vol.path`, `vol.delete`.
- Provider service hardening keeps `ProtectSystem=full` and grants the required iSCSI state write access with `ReadWritePaths=/etc/iscsi /var/lib/iscsi`.
- `/etc/iscsi` and `/var/lib/iscsi` exist on both test hypervisors.
- `pool.refresh` reports managed dataset space from `hot1/libvirt` as `pool.capacity`, `pool.allocation`, and `pool.available`.

## Distro libvirt sources

- AlmaLinux source RPM: `sources/al10/libvirt-11.10.0-12.el10_2.alma.1.src.rpm`
- Alma source tree: `build/libvirt-al10-11.10.0`
- Ubuntu source package: `sources/u24/libvirt_10.0.0-2ubuntu8.13.dsc`
- Ubuntu source tree: `build/libvirt-u24-10.0.0`

## Package artifacts

- Provider DEB: `dist/truenas-libvirt-provider_0.1.0-1_all.deb`
- Provider RPM: `dist/truenas-libvirt-provider-0.1.0-1.el10.noarch.rpm`
- Ubuntu libvirt packages: `dist/*_10.0.0-2ubuntu8.13+truenas1_*.deb`
- AlmaLinux libvirt packages: `dist/*-11.10.0-12.el10.alma.1.truenas1*.rpm`

Note: the installed test hosts also include post-package validation fixes applied directly to the provider script, provider service unit, and TrueNAS storage backend module. Rebuild final binary packages before publishing a repository.

## Libvirt patch points

- `src/conf/storage_conf.h`: add `VIR_STORAGE_POOL_TRUENAS` and source `protocolType`.
- `src/conf/storage_conf.c`: add string enum `truenas`, pool options, protocol parse/format.
- `src/conf/schemas/storagepool.rng`: allow `type='truenas'` with `<protocol type='iscsi|nvmeof'/>`.
- `src/storage/storage_backend.c`: load `truenas` backend module.
- `src/storage/meson.build`: add backend source and module.
- `src/storage/storage_backend_truenas.[ch]`: new backend implementation.
- `src/conf/domain_conf.c`: resolve TrueNAS pool volumes to block paths for domain disks.
- `src/conf/virstorageobj.c`: detect duplicate TrueNAS pool definitions by pool name and protocol.
- `src/test/test_driver.c`: classify TrueNAS test pool volumes as block volumes.
- `tools/virsh-pool.c` and test fixtures: recognize the new pool type in strict switch/test paths.

## Runtime validation

Validated on Ubuntu 24.04 and AlmaLinux 10:

- Provider service starts and exposes `/run/truenas-libvirt/provider.sock`.
- `health.check` authenticates to TrueNAS and confirms local `iscsiadm`, `nvme`, and `udevadm` tools.
- `virsh pool-capabilities` includes `type='truenas'`.
- `virsh pool-define`, `pool-start`, `pool-refresh`, and `vol-list` work for:
  - `examples/storage-pool-hot1-iscsi.xml`
  - `examples/storage-pool-hot1-nvmeof.xml`
- iSCSI `virsh vol-create-as` works from Ubuntu:
  - `codex-iscsi-final` -> `/dev/disk/by-id/scsi-36589cfc0000007a2b10e3b5498fc4c4a`
- `virsh pool-info truenas-hot1-iscsi` now reports managed dataset space correctly on both hosts:
  - Capacity: `1.76 TiB`
  - Allocation: `768.00 KiB`
  - Available: `1.76 TiB`
- NVMe-oF `virsh vol-create-as` works from AlmaLinux:
  - `codex-nvmeof-smoke-2` -> `/dev/disk/by-id/nvme-TrueNAS_X570_AORUS_ELITE_b5cd687e2bcd50706575`
- Cross-host visibility works:
  - AlmaLinux sees the iSCSI volume created on Ubuntu after pool refresh.
  - Ubuntu sees the NVMe-oF volume created on AlmaLinux after pool refresh.

## Fixes made during runtime validation

- Replaced `saferead()` with `read()` in the libvirt backend socket client. `saferead()` waits for the full requested byte count and caused `pool-start` to hang after the provider had already returned a newline-framed JSON response.
- Changed the backend from `buildVol` to `createVol` so libvirt advertises and calls volume creation for TrueNAS pools.
- Made NVMe-oF path lookup subnqn-specific using `nvme list-subsys -o json`, avoiding duplicate paths when multiple TrueNAS namespaces are connected.
- Made iSCSI export/login use the full portal tuple and explicitly create local `iscsiadm` node records.
- Added the provider service `ReadWritePaths` exception needed for `iscsiadm` node database writes under systemd sandboxing.

## Current limitation

The configured TrueNAS API user exposes zvol create/query/update but no dataset delete method. Provider `vol.delete` cleans export records and then returns a structured `dataset_delete_unavailable` error unless a dataset deletion method becomes visible to the API user.

## Remaining work

- Copy `release/release.example.json` to `release/release.json` and fill in build/test/repo hostnames.
- Rebuild final Ubuntu and AlmaLinux binary packages after the latest runtime fixes.
- Rebuild provider DEB/RPM after the systemd unit and provider attach fixes.
- Set up the throwaway migration VM named in release config and run the automated migration gate.
- Decide whether to add snapshot support in this first storage backend iteration or as the next milestone.
