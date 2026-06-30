# TrueNAS libvirt storage integration

Prototype for a cluster-aware TrueNAS-backed libvirt storage provider.

Initial targets:

- TrueNAS 25.10.x JSON-RPC WebSocket API
- Ubuntu 24.04 libvirt hosts
- AlmaLinux 10 libvirt hosts
- iSCSI and NVMe-oF transports
- shared cluster access suitable for live migration

The first implementation lives outside libvirt as a provider helper. Once the
storage workflow is proven, libvirt can call the helper from a new storage
backend.

Do not commit API keys. Put secrets in a root-readable file referenced by the
provider config.



## Prototype Commands

Use `TRUENAS_API_KEY` for lab runs, or place the key in the configured
`api_key_file`.

```sh
./truenas_provider.py --config config.json pool-list
./truenas_provider.py --config config.json namespace-ensure hot1
./truenas_provider.py --config config.json zvol-create hot1 lab-iscsi-001 1g --transport iscsi
./truenas_provider.py --config config.json iscsi-export hot1 lab-iscsi-001
./truenas_provider.py --config config.json zvol-create hot1 lab-nvmeof-001 1g --transport nvmeof
./truenas_provider.py --config config.json nvmeof-export hot1 lab-nvmeof-001
./truenas_provider.py --config config.json zvol-list hot1
```

The first live iSCSI export in the lab is:

```text
portal:     10.6.0.119:3260
target IQN: iqn.2005-10.org.freenas.ctl:libvirt-lab-iscsi-001
zvol:       hot1/libvirt/lab-iscsi-001
size:       1 GiB
WWN:        0x6589cfc000000770268f1b524b437447
serial:     645e601fe4690a8
```

The first live NVMe-oF export in the lab is:

```text
portal:     10.6.0.119:4420
subnqn:     nqn.2011-06.com.truenas:uuid:9065a4ed-e3aa-467f-bf13-8ab58bdb51f2:libvirt-lab-nvmeof-001
zvol:       hot1/libvirt/lab-nvmeof-001
size:       1 GiB
serial:     94330d09d6bfa5f07022
namespace:  uuid.f41c7a2c-c987-47b1-b899-d10bac36c089
```


## Libvirt Backend Status

The libvirt backend is carried as patch overlays instead of committed generated source trees. Example pool XML is in `examples/`, provider packaging is in `packaging/`, and generated distro source trees are recreated under ignored `build/` paths by `scripts/refresh-libvirt-source.py`.


## Release Automation

Release automation is in `scripts/`, `release/`, and `.github/workflows/`. The intended flow is GitHub Actions on a self-hosted build runner, Podman-based Ubuntu and AlmaLinux package builds, signed staging repos on the repo host, storage and migration tests on separate test VMs, a Codex veto gate, then promotion to stable. See `docs/release-automation.md`.
