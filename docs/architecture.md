# Architecture

## Goal

Expose TrueNAS zvols as libvirt storage volumes with enough cluster awareness
to support guest migration between KVM/libvirt hosts.

## Components

- `truenas-libvirt-provider`: helper binary/script that talks to TrueNAS.
- libvirt storage backend patch: delegates TrueNAS-specific operations to the
  provider over JSON.
- distro packages: patched libvirt plus the provider helper for Ubuntu 24.04
  and AlmaLinux 10.

## TrueNAS API

Use `auth.login_ex` with `API_KEY_PLAIN` against the TrueNAS 25.10 JSON-RPC
WebSocket API.

Authentication config should identify:

- management endpoint, currently `wss://10.6.0.119/api/current`
- dedicated username, currently `libvirt_user`
- path to a root-readable API key file
- TLS verification mode
- storage target IP, currently `10.6.0.119`

## Pool Mapping

TrueNAS pools:

- `cold`
- `warm`
- `hot1`
- `hot3`

Recommended libvirt exposure:

- one libvirt pool per TrueNAS pool and transport
- one libvirt volume per TrueNAS zvol

Example:

```text
libvirt pool:   truenas-hot1-iscsi
libvirt volume: vm01-data
TrueNAS zvol:   hot1/libvirt/vm01-data
```

## Cluster Model

The first cluster implementation should be shared-discovery, single-writer:

- all approved hypervisors are authorized on TrueNAS for every managed volume
- libvirt/QEMU locking prevents two hosts from running the same VM with the
  same non-clustered disk
- live migration works without a late storage ACL update in the migration path

Dynamic grant/revoke can be added later after the basic migration path is
stable.

## Host Inventory

Known lab hosts:

```text
subvirt-al10  10.6.0.120  AlmaLinux 10.2
subvirt-u24   10.6.0.121  Ubuntu 24.04.4
```

Known initiator data:

```text
subvirt-al10 iSCSI IQN: iqn.2026-06.dev.pgookin:subvirt-al10
subvirt-al10 NVMe NQN:  nqn.2014-08.org.nvmexpress:uuid:5dd508d1-4f97-497d-a6d9-6f3e5e2ea9b2
subvirt-u24 iSCSI IQN: iqn.2004-10.com.ubuntu:01:8ca920cc4166
subvirt-u24 NVMe NQN:  nqn.2014-08.org.nvmexpress:uuid:d499300d-00d9-4b48-b964-5ad25cdaa0ac
```


## Lab Status

Completed on 2026-06-14:

- installed libvirt/QEMU, iSCSI, and NVMe CLI packages on both hosts
- generated AlmaLinux iSCSI IQN and confirmed Ubuntu NVMe host NQN
- confirmed TrueNAS JSON-RPC endpoint at `wss://10.6.0.119/api/current`
- created provider-managed namespace `hot1/libvirt`
- created sparse zvol `hot1/libvirt/lab-iscsi-001` with 1 GiB size and 16K block size
- exported that zvol over iSCSI to both configured host IQNs
- confirmed both hosts discover `iqn.2005-10.org.freenas.ctl:libvirt-lab-iscsi-001`
- logged both hosts into the LUN and confirmed matching serial `645e601fe4690a8` and WWN `0x6589cfc000000770268f1b524b437447`
- created sparse zvol `hot1/libvirt/lab-nvmeof-001` with 1 GiB size and 16K block size
- exported that zvol over NVMe-oF/TCP to both configured host NQNs
- confirmed both hosts discover `nqn.2011-06.com.truenas:uuid:9065a4ed-e3aa-467f-bf13-8ab58bdb51f2:libvirt-lab-nvmeof-001`
- connected both hosts to the namespace and confirmed matching serial `94330d09d6bfa5f07022` and namespace UUID `uuid.f41c7a2c-c987-47b1-b899-d10bac36c089`

Current TrueNAS iSCSI objects for the first test volume:

```text
portal id:    1
initiator id: 1
target id:    1
extent id:    1
mapping id:   1
```

Current TrueNAS NVMe-oF objects for the first test volume:

```text
host ids:        1, 2
subsystem id:    1
namespace id:    1
port id:         1
port mapping id: 1
host mapping ids: 1, 2
```

## First Milestone

1. Query TrueNAS pools through the provider.
2. Create a zvol under one test namespace, for example `hot1/libvirt`.
3. Export the zvol over iSCSI to both hosts.
4. Export the same model over NVMe-oF to both hosts.
5. Connect from both Ubuntu 24.04 and AlmaLinux 10.
6. Attach a managed volume to a VM.
7. Live migrate the VM and confirm the block device identity is stable.
8. Delete the managed volume cleanly.

## Snapshot Scope

Provider-level zvol snapshots should be supported early:

- create snapshot
- list snapshots
- delete snapshot
- clone from snapshot

Libvirt-facing snapshot semantics should come later, after create/delete/resize
and migration are working.

