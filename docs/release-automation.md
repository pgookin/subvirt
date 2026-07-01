# Release Automation

## Design

The active release flow uses one build VM, two distro test VMs, and one repository VPS.

- Build VM: `subvirt-build`, an Ubuntu 24.04 host that runs Podman.
- Ubuntu build container: builds Ubuntu 24.04 packages inside a pinned Ubuntu image.
- AlmaLinux build container: builds AlmaLinux 10 packages inside a pinned AlmaLinux image.
- Ubuntu test VM: installs only from the staging apt repo and runs smoke tests.
- AlmaLinux test VM: installs only from the staging dnf repo and runs smoke tests.
- Repo VPS: owns the GPG key, publishes staging/stable repos, and signs metadata.

`release/release.example.json` is the template for hostnames, paths, repo names, and test pool names. Copy it to `release/release.json` and edit local values. Do not commit secrets or private keys.

The public workflow uses `project.source_mode = "git"` with `project.repo_url = "https://github.com/pgookin/subvirt.git"` so build and test hosts can clone without GitHub SSH keys. For pre-publication or emergency lab-only testing, `source_mode = "rsync"` can still be used in the ignored local `release/release.json`.


## Public GitHub Repository

The canonical public source repository is `https://github.com/pgookin/subvirt`. Developer pushes can use `git@github.com:pgookin/subvirt.git`, but automation clones over HTTPS. Commit code, scripts, docs, examples, package metadata, and patch overlays only. Do not commit generated libvirt source trees, downloaded source packages, binary packages, live release configs, API keys, SSH keys, or GPG private keys.

The durable source inputs are:

- provider source and packaging in the repository root and `packaging/`
- libvirt patch overlays in `patches/libvirt/`
- virt-manager patch overlays in `patches/` and `packaging/virt-manager/`
- public-safe templates in `release/`

Generated paths such as `build/`, `sources/`, `dist/`, `provider-build/`, `work-clean/`, and `work-patchgen/` are intentionally ignored.

## Upstream Tracking

`scripts/check-upstream.py` compares the current mirror metadata against `release/upstream-lock.json`. GitHub Actions runs this check every 8 hours, matching the local mirror sync cadence. The script exits with code `10` when a newer libvirt package is available and writes a JSON report suitable for workflow artifacts.

`scripts/refresh-libvirt-source.py` creates generated libvirt build inputs from distro source packages and applies the tracked Subvirt overlays. It writes only ignored workspace paths. If patches do not apply cleanly, the candidate stops for Codex or human rebase work instead of guessing.

When the upstream check workflow opens a PR that changes `release/upstream-lock.json`, `upstream-candidate.yml` automatically derives the target Ubuntu and Alma versions, refreshes generated sources, builds packages, runs direct artifact tests, publishes staging, and runs staging tests. It intentionally does not promote to stable; stable promotion remains a separate Codex-gated workflow until a full upstream refresh succeeds reliably.

## Commands

Dry-run a full release from a tag or commit:

```sh
./scripts/release.py release --config release/release.json --ref v0.1.0 --build-id 0.1.0-1
```

Run it for real:

```sh
./scripts/release.py release --config release/release.json --ref v0.1.0 --build-id 0.1.0-1 --execute
```

Run individual phases:

```sh
./scripts/release.py build --config release/release.json --ref v0.1.0 --build-id 0.1.0-1 --execute
./scripts/release.py collect --config release/release.json --build-id 0.1.0-1 --execute
./scripts/release.py test-artifacts --config release/release.json --ref v0.1.0 --build-id 0.1.0-1 --execute
./scripts/release.py publish-staging --config release/release.json --build-id 0.1.0-1 --execute
./scripts/release.py test-staging --config release/release.json --ref v0.1.0 --build-id 0.1.0-1 --execute
./scripts/release.py promote --config release/release.json --build-id 0.1.0-1 --execute
```

Use `--test-id` to rerun storage tests against the same artifact directory without reusing volume names:

```sh
./scripts/release.py test-artifacts --config release/release.json --build-id 0.1.0-1 --test-id 0.1.0-1-rerun1 --execute
```

## Build Helpers

The orchestrator calls these project-local wrapper scripts on `subvirt-build`:

- `scripts/container-build-ubuntu.sh`
- `scripts/container-build-alma.sh`

The wrappers build repo-local images from:

- `containers/ubuntu-24.04-build/Containerfile`
- `containers/almalinux-10-build/Containerfile`

Both libvirt package helpers run `scripts/validate-unified-diff.py` before building so malformed generated patch hunk counts fail early. The virt-manager helpers run a static source check after patching so the client-side TrueNAS storage-pool capability is validated before package build.

Then they run the existing package helpers inside the matching distro container:

- Ubuntu: `scripts/build-provider-deb.sh`, `scripts/build-libvirt-deb.sh`, and `scripts/build-virt-manager-deb.sh`
- AlmaLinux: `scripts/build-provider-rpm.sh`, `scripts/build-libvirt-rpm.sh`, and `scripts/build-virt-manager-rpm.sh`

The current container mode uses native package builds inside the pinned distro images. This is intentional for the prototype release pipeline. If stricter distro-policy isolation becomes necessary, the wrapper scripts can later grow `sbuild` or `mock` modes without changing the outer release flow.

Bootstrap the build host:

```sh
ssh subvirt-build
cd /srv/subvirt/build
./scripts/bootstrap-build-host.sh
```

The bootstrap installs Podman and baseline host tools. Build dependencies belong in the container images, not on the build host.

## Direct Artifact Test

`test-artifacts` installs packages directly from the build VM artifact directory onto the two test VMs before anything is published. It copies packages from `/srv/subvirt/artifacts/<build-id>/ubuntu` and `/srv/subvirt/artifacts/<build-id>/alma` into `/tmp/subvirt-artifacts/<test-id>/...` on each test host, installs them with apt/dnf, restarts the provider and libvirt storage daemon, checks `health.check`, verifies the `truenas` storage backend is visible in `virsh pool-capabilities`, verifies patched virt-manager reports `truenas` pools as volume-creation capable, then runs the storage gate.

This is the fast confidence gate for the lab workflow. Staging repo tests still matter later because they verify package-manager repo behavior, signing, and dependency resolution from published metadata.

## Repo Host Bootstrap

The lab repo host is `subvirt-repo`, currently an AlmaLinux 10.2 VM at `10.6.0.118`.
Bootstrap it from a synced project checkout:

```sh
ssh subvirt-repo
cd /srv/subvirt/build
./scripts/bootstrap-repo-host.sh
```

The bootstrap installs:

- `nginx` for static HTTP serving.
- `createrepo_c` for AlmaLinux/YUM metadata.
- `rpm-sign` for RPM package signing.
- `zstd` for reading Ubuntu `.deb` control archives.
- `rsync`, `gnupg2`, `policycoreutils-python-utils`, and `firewalld`.

It creates these paths:

- `/srv/repo/www`: nginx document root.
- `/srv/subvirt/incoming`: uploaded release artifacts.
- `/usr/local/libexec/subvirt/publish-repo.py`: repo metadata publisher.

It also creates a local lab GPG key named `Subvirt Repository <repo@subvirt.local>` if one does not already exist, exports public keys under `/srv/repo/www/keys/`, opens HTTP in firewalld, and enables nginx.

For a future public VPS, repeat this bootstrap on the VPS, then replace the lab GPG key with a production key and update the client templates to use the public hostname and HTTPS.

## Publishing

The VPS publishes two channels per distro:

- `staging`: every candidate build lands here first.
- `stable`: promoted only after staging tests pass.

Apt metadata is generated by `scripts/publish-repo.py`. Yum/DNF metadata is managed by `createrepo_c`. The GPG signing key lives only on the repo host.

The published layout is:

- `http://<repo-host>/apt/ubuntu/dists/noble/staging/...`
- `http://<repo-host>/apt/ubuntu/dists/noble/stable/...`
- `http://<repo-host>/yum/almalinux/10/staging/...`
- `http://<repo-host>/yum/almalinux/10/stable/...`
- `http://<repo-host>/keys/subvirt.gpg` for apt `Signed-By` use.
- `http://<repo-host>/keys/subvirt.asc` for DNF/RPM `gpgkey` use.

Client templates are in `release/repo-templates/`:

- `subvirt.sources`
- `subvirt-staging.sources`
- `subvirt.repo`

Replace `repo.example.com` with the production repo hostname before publishing installation instructions.

## Future GitHub Workflow

GitHub is not required for the current lab workflow. In the lab, `scripts/release.py` synchronizes this workspace over SSH with `rsync`. Once the project is published, GitHub Actions should become the control plane:

- A scheduled workflow polls Ubuntu and AlmaLinux package metadata every 8 hours for new libvirt source versions.
- A manual `workflow_dispatch` path starts release candidates on demand.
- A self-hosted runner on `subvirt-build` runs the same Podman wrapper scripts used locally.
- Candidate artifacts are uploaded, published to staging, tested on the two test VMs, reviewed by a Codex veto gate, then promoted to stable only after deterministic tests pass and Codex does not hold the release.

There is no expected webhook-style event from Ubuntu or AlmaLinux for new libvirt packages. Polling distro package metadata is the pragmatic path.

## Test Gate

`scripts/test-storage.py` is run on both test VMs. It verifies:

- `truenas` pool support appears in libvirt.
- iSCSI and NVMe-oF pools define/start/refresh.
- Pool capacity reports managed-dataset space, not summed zvol sizes.
- Ubuntu creates an iSCSI test volume.
- AlmaLinux creates an NVMe-oF test volume.
- Each host refreshes and sees the other host's volume.
- A configured throwaway VM live-migrates to the peer host when `tests.run_migration` is true.

The test script creates unique volume names containing the test ID. By default the test ID is the build ID, but `--test-id` can override it when rerunning tests against the same artifact directory. Cleanup is intentionally not required until TrueNAS dataset deletion is available to the API user.

Set `tests.run_migration` to `true` once `tests.migration_domain` names a throwaway VM that exists on the Ubuntu test host and can live-migrate to the AlmaLinux test host.

## Current Implementation Notes

The next package rebuild must include the runtime fixes already validated on the hosts:

- Provider service `ReadWritePaths=/etc/iscsi /var/lib/iscsi`.
- Provider iSCSI node creation and NVMe-oF subnqn-specific path lookup.
- Libvirt backend `read()` socket framing fix.
- Libvirt backend `createVol` callback.
- Managed-dataset pool capacity reporting.
- Virt-manager `truenas` pool label and volume-creation enablement.
