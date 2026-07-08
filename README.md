# Subvirt

Subvirt adds a TrueNAS-backed storage pool type to libvirt. It lets libvirt and
virt-manager create VM disks as TrueNAS zvols and attach them through iSCSI or
NVMe-oF.

Current supported targets:

- TrueNAS 25.10.x JSON-RPC WebSocket API
- Ubuntu LTS libvirt hosts: 18.04/20.04 through ESM-era package access, plus 22.04, 24.04, and 26.04 standard LTS
- AlmaLinux 9 and 10 libvirt hosts
- iSCSI and NVMe-oF transports
- shared cluster access suitable for live migration

## Install

Start with the end-user guide:

- [Getting Started](docs/getting-started.md)

The public package repository is:

- `https://repo.subvirt.net/apt/ubuntu` for Ubuntu LTS suites bionic, focal, jammy, noble, and resolute
- `https://repo.subvirt.net/yum/almalinux/$releasever/stable` for AlmaLinux 9 and 10

The guide covers adding the repo, installing packages, configuring the TrueNAS
provider, creating libvirt pools, and validating iSCSI or NVMe-oF volumes.

## Documentation

- [Architecture](docs/architecture.md)
- [Implementation Status](docs/implementation-status.md)
- [Release Automation](docs/release-automation.md)
- [Ephemeral Lab](docs/ephemeral-lab.md)

## Security

Do not commit API keys. Put secrets in a root-readable file referenced by
`/etc/truenas-libvirt/config.json`.

## Project Layout

- `truenas_provider.py` and `truenas_provider_daemon.py`: TrueNAS provider
  helper and daemon.
- `patches/libvirt/`: libvirt storage backend patches.
- `patches/` and `packaging/virt-manager/`: virt-manager support patches.
- `packaging/`: provider package metadata and systemd unit.
- `scripts/`: build, test, release, and repo publishing automation.
- `release/`: public-safe release templates and upstream version locks.
- `examples/`: example libvirt TrueNAS storage pool XML.

Generated distro source trees and binary package artifacts are intentionally not
committed.

## Development

The libvirt backend is carried as patch overlays instead of committed generated
source trees. Generated distro source trees are recreated under ignored `build/`
paths by `scripts/refresh-libvirt-source.py`.

Release automation is in `scripts/`, `release/`, and `.github/workflows/`. The
intended flow is GitHub Actions on a self-hosted build runner, Podman-based
Ubuntu and AlmaLinux 9/10 package builds, signed staging repos, storage and migration
tests, then promotion to stable. See [Release Automation](docs/release-automation.md).
