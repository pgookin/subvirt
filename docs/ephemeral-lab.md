# Ephemeral Test Lab

Subvirt can run release-candidate tests against disposable VMs created on the build host with libvirt. The lab path is intended to replace long-lived Ubuntu/Alma test hosts once the TrueNAS appliance install flow is stable.

## Roles

- Build host: VM factory, per-run repo publisher, and release runner.
- Ubuntu VM: fresh Ubuntu 24.04 cloud image test host.
- AlmaLinux VM: fresh AlmaLinux 10 cloud image test host.
- TrueNAS VM: ISO-installed or golden-image appliance with separate management and storage NICs.

The lab creates two libvirt networks. Bridge names must stay within Linux's 15-character interface-name limit:

- `subvirt-lab-mgmt`: NAT network, default `192.168.150.0/24`, also serves the per-run apt/dnf repo from `192.168.150.1:8080`.
- `subvirt-lab-storage`: isolated network, default `192.168.151.0/24`, used by iSCSI and NVMe-oF traffic.

The lab nginx service listens on `lab.http_listen`; use `0.0.0.0:8080` when bootstrapping before the lab network exists. Guests consume the repo through `lab.http_url`, normally `192.168.150.1:8080`.

## Configuration

Copy `release/lab.example.json` to an untracked local config such as `/srv/subvirt/release/lab.json` on the build host and fill in local values:

- SSH public key file or literal authorized key.
- TrueNAS ISO URL and checksum, or `install_mode=golden` with `golden_image` after a stable install image exists.
- TrueNAS post-install script and generated API key.
- The iSCSI and NVMe-oF TrueNAS pool names used by storage tests.

Do not commit API keys, local ISO checksums if they point to private mirrors, or golden image paths.

## Commands

Bootstrap the VM factory host:

```sh
sudo ./scripts/lab.py bootstrap-host --config /srv/subvirt/release/lab.json --build-id bootstrap --execute
```

Create only the Ubuntu and AlmaLinux VMs while debugging cloud-init, networking, or package-manager behavior:

```sh
./scripts/lab.py create-linux --config /srv/subvirt/release/lab.json --build-id <build-id> --execute
./scripts/lab.py wait-linux --config /srv/subvirt/release/lab.json --build-id <build-id> --execute
```

Create the full lab for an existing build ID, including TrueNAS:

```sh
./scripts/lab.py create --config /srv/subvirt/release/lab.json --build-id <build-id> --execute
```

Publish the per-run repo from build artifacts. The publisher accepts full or one-distro artifact directories, which lets Ubuntu-only or Alma-only candidate builds test the package-manager path without requiring unrelated packages:

```sh
./scripts/lab.py publish-repo --config /srv/subvirt/release/lab.json --build-id <build-id> --artifacts /srv/subvirt/artifacts/<build-id> --execute
```

Run repo-based package installation and storage tests. Full Ubuntu+Alma artifact sets run the storage gate; one-distro artifact sets install and configure only that distro and skip the two-host storage gate:

```sh
./scripts/lab.py test-repo --config /srv/subvirt/release/lab.json --build-id <build-id> --execute
```

Destroy a preserved lab:

```sh
./scripts/lab.py destroy --config /srv/subvirt/release/lab.json --build-id <build-id> --execute
```

To make release validation use the ephemeral lab, set this in the local release config:

```json
"lab": {
  "enabled": true,
  "full": false,
  "host": "build-host",
  "config": "/srv/subvirt/release/lab.json"
}
```

With `full: false`, candidate workflows create fresh Ubuntu and AlmaLinux VMs, publish a per-run repo, install the candidate packages through apt/dnf, and validate the provider service plus virt-manager integration. Set `full: true` after the TrueNAS golden-image/API setup is ready; that path also creates the TrueNAS VM and runs the storage gate.

On success the lab is destroyed automatically. On failure the VMs, disks, and per-run repo are preserved and the cleanup command is printed.

## TrueNAS install flow

The first implementation supports both ISO and golden-image paths. ISO mode creates the VM and boots the configured ISO. Because TrueNAS installer automation can vary by release, the appliance setup is finished by a configured `truenas.post_install_script`. That script is responsible for setting credentials, configuring management/storage addresses, creating ZFS pools, enabling iSCSI/NVMe-oF services, and creating the API key used by `truenas-libvirt-provider`.

Once ISO automation is stable for a TrueNAS release, create a golden image and set `vms.truenas.install_mode` to `golden` for faster, fully unattended CI runs while still deriving the image from the tested ISO.
