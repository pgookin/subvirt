# Release Automation

## Design

The active release flow uses one build VM, two distro test VMs, and one repository VPS.

- Build VM: an Ubuntu 24.04 self-hosted runner that runs Podman.
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


## Version Manifest

`release/subvirt-version.json` is the committed version source of truth. It tracks the Subvirt project version, the independent `truenas-libvirt-provider` package version/release, and the local `truenasN` package revision used for each distro libvirt and virt-manager rebuild.

Use `scripts/bump-version.py` for intentional feature/version changes. Typical examples:

```sh
./scripts/bump-version.py --subvirt-version 0.3.0 --provider-version 0.3.0 --provider-release 1
./scripts/bump-version.py --provider-release 2
./scripts/bump-version.py --ubuntu-virt-manager-revision 2 --alma-virt-manager-revision 2
```

When the upstream libvirt checker detects a new parent package, `scripts/update-upstream-lock.py` resets only the affected distro libvirt local revision to `1`. Provider and virt-manager versions are not changed by upstream libvirt polling. If a Subvirt patch needs another rebuild against the same parent package, bump that distro's libvirt local revision explicitly.

## Upstream Tracking

`scripts/check-upstream.py` compares the current mirror metadata against `release/upstream-lock.json`. GitHub Actions runs this check every 8 hours, matching the local mirror sync cadence. The script exits with code `10` when a newer libvirt package is available and writes a JSON report suitable for workflow artifacts.

`scripts/refresh-libvirt-source.py` creates generated libvirt build inputs from distro source packages and applies the tracked Subvirt overlays. It writes only ignored workspace paths. If patches do not apply cleanly, the candidate stops for Codex or human rebase work instead of guessing.

`scripts/write-upstream-manifest.py` records the durable refresh proof in `release/upstream-manifests/`. These manifests are committed with the lock update and include the upstream source file checksums, tracked patch checksums, generated local version, and a small generated-output check. Generated source trees, downloaded source packages, binary packages, and repo metadata are still not committed.

When the upstream check workflow detects a newer parent package, it refreshes generated source inputs, writes manifests, and opens or updates `automation/upstream-libvirt-refresh`. The candidate build runs from that PR branch. If the candidate build and ephemeral lab test pass, the workflow comments with the evidence URL. Scheduled runs finalize automatically by running the deterministic release-evidence gate, publishing the tested build to the public stable repository, verifying the public HTTPS metadata and package URLs, and then merging the PR. Manual `workflow_dispatch` runs default to `finalize=false`, which exercises the production path but skips public publishing and merging; set `finalize=true` only when you want the manual run to publish stable. If refresh, build, evidence validation, test, public publish, or verification fails, the PR is not merged.

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
./scripts/release.py publish-public-stable --config release/release.json --build-id 0.1.0-1 --execute
./scripts/release.py verify-public-stable --config release/release.json --build-id 0.1.0-1 --execute
```

Validate release evidence before public promotion:

```sh
./scripts/verify-release-evidence.py --build-id 0.1.0-1 --scope auto
```

Use `--test-id` to rerun storage tests against the same artifact directory without reusing volume names:

```sh
./scripts/release.py test-artifacts --config release/release.json --build-id 0.1.0-1 --test-id 0.1.0-1-rerun1 --execute
```


Run a provider-only release candidate when only `truenas-libvirt-provider` changed:

```sh
BUILD_ID=0.2.1-provider-1 BUILD_SCOPE=provider PROMOTE_STABLE=false ./scripts/run-candidate-release.sh
```

Provider-only candidates build the Ubuntu and Alma provider packages, create fresh ephemeral lab VMs, install patched libvirt and virt-manager from the current stable repo, install the staged provider package from the per-run lab repo, and run the same TrueNAS storage gate, including the Ubuntu-to-Ubuntu live-migration smoke gate when enabled in the lab config. Provider-only candidates require `lab.enabled=true` and `truenas.api_key` in the local lab config. Each candidate writes `artifacts/<build-id>/candidate-release.log` and must pass `scripts/verify-release-evidence.py` before stable publishing. Set `PROMOTE_STABLE=true` only when that provider build should be published to the public stable repository; Codex review is advisory by default and becomes blocking only with `REQUIRE_CODEX_GATE=true`.

## Build Helpers

The orchestrator calls these project-local wrapper scripts on the build host:

- `scripts/container-build-ubuntu.sh`
- `scripts/container-build-alma.sh`

The wrappers build repo-local images from:

- `containers/ubuntu-24.04-build/Containerfile`
- `containers/almalinux-10-build/Containerfile`

Both libvirt package helpers run `scripts/validate-unified-diff.py` before building so malformed generated patch hunk counts fail early. The virt-manager helpers run a static source check after patching so the client-side TrueNAS storage-pool capability is validated before package build.

Then they run the existing package helpers inside the matching distro container:

- Ubuntu: `scripts/build-provider-deb.sh`, `scripts/build-libvirt-deb.sh`, and `scripts/build-virt-manager-deb.sh`
- AlmaLinux: `scripts/build-provider-rpm.sh`, `scripts/build-libvirt-rpm.sh`, and `scripts/build-virt-manager-rpm.sh`

Provider-only candidates use `scripts/container-build-provider-ubuntu.sh` and `scripts/container-build-provider-alma.sh`, which run only the provider package helpers inside the same distro containers.

The current container mode uses native package builds inside the pinned distro images. This is intentional for the prototype release pipeline. If stricter distro-policy isolation becomes necessary, the wrapper scripts can later grow `sbuild` or `mock` modes without changing the outer release flow.

Bootstrap the build host:

```sh
ssh <build-host>
cd /srv/subvirt/build
./scripts/bootstrap-build-host.sh
```

The bootstrap installs Podman and baseline host tools. Build dependencies belong in the container images, not on the build host.

## Direct Artifact Test

`test-artifacts` installs packages directly from the build VM artifact directory onto the two test VMs before anything is published. It copies packages from `/srv/subvirt/artifacts/<build-id>/ubuntu` and `/srv/subvirt/artifacts/<build-id>/alma` into `/tmp/subvirt-artifacts/<test-id>/...` on each test host, installs them with apt/dnf, restarts the provider and libvirt storage daemon, runs `doctor --json`, verifies the `truenas` storage backend is visible in `virsh pool-capabilities`, verifies patched virt-manager reports `truenas` pools as volume-creation capable, then runs the storage gate.

This is the fast confidence gate for the lab workflow. Staging repo tests still matter later because they verify package-manager repo behavior, signing, and dependency resolution from published metadata.

## Repo Host Bootstrap

The internal lab repo host is configured in the local release config. The public stable repo host is `repo.subvirt.net`.

Use `scripts/bootstrap-repo-host.sh` only for lab hosts where Subvirt owns nginx completely. For the public VPS, use the safer bootstrap so the existing landing page, nginx server blocks, and Certbot-managed TLS config are preserved:

```sh
ssh subvirtDO
cd /srv/subvirt/build
./scripts/bootstrap-public-repo-host.sh /tmp/subvirt-build-publish.pub
```

The public bootstrap installs repo publishing dependencies, creates a dedicated `subvirt-publish` user, installs `/usr/local/libexec/subvirt/publish-repo.py`, prepares `/srv/repo/www` and `/srv/subvirt/incoming`, and generates a production signing key named `Subvirt Repository <repo@subvirt.net>` as the deploy user. The private signing key stays on the public VPS.

The build runner publishes to the public VPS over SSH as `subvirt-publish@repo.subvirt.net`. Public publishing writes only the `stable` channel; candidate and staging validation stay private.

## Publishing

The public VPS publishes the `stable` channel only. Candidate and staging repositories remain private to the lab workflow.

Apt metadata is generated by `scripts/publish-repo.py`. Yum/DNF metadata is managed by `createrepo_c`. The production GPG signing key lives only on the public repo host under the `subvirt-publish` account.

The published layout is:

- `https://repo.subvirt.net/apt/ubuntu/dists/noble/stable/...`
- `https://repo.subvirt.net/apt/ubuntu/pool/stable/...`
- `https://repo.subvirt.net/yum/almalinux/10/stable/...`
- `https://repo.subvirt.net/keys/subvirt.gpg` for apt `Signed-By` use.
- `https://repo.subvirt.net/keys/subvirt.asc` for DNF/RPM `gpgkey` use.

Stable client templates are in `release/repo-templates/`:

- `subvirt.sources`
- `subvirt.repo`

They already target `repo.subvirt.net`. The staging template remains an internal/example template and is not part of public publishing.

## Website Deployment

The public landing page for `https://subvirt.net/` is tracked as a plain static site in `site/`. Pushes to `main` that change `site/**`, `scripts/deploy-site.sh`, or the website workflow run `.github/workflows/deploy-site.yml` on the self-hosted build runner. The workflow syncs `site/` to `/srv/www/` on the public VPS as `subvirt-publish@repo.subvirt.net` and verifies both `https://subvirt.net/` and `https://www.subvirt.net/` after deploy.

Website deployment is separate from package publishing. Package repository content remains under `/srv/repo/www/` and is served only by `repo.subvirt.net`.

## Future GitHub Workflow

GitHub is not required for the current lab workflow. In the lab, `scripts/release.py` synchronizes this workspace over SSH with `rsync`. Once the project is published, GitHub Actions should become the control plane:

- A scheduled workflow polls Ubuntu and AlmaLinux package metadata every 8 hours for new libvirt source versions.
- A manual `workflow_dispatch` path starts release candidates on demand.
- A self-hosted build runner runs the same Podman wrapper scripts used locally.
- Candidate artifacts are tested privately. Finalization publishes tested artifacts to public stable, verifies public HTTPS metadata and package URLs, then merges the upstream refresh PR.

There is no expected webhook-style event from Ubuntu or AlmaLinux for new libvirt packages. Polling distro package metadata is the pragmatic path.

## Test Gate

`scripts/test-storage.py` is run on both test VMs. It verifies:

- `truenas` pool support appears in libvirt.
- iSCSI and NVMe-oF pools define/start/refresh.
- Pool capacity reports managed-dataset space, not summed zvol sizes.
- Ubuntu creates, resizes, clones, and delete-validates an iSCSI test volume.
- AlmaLinux creates, resizes, clones, and delete-validates an NVMe-oF test volume.
- Each host refreshes and sees the other host's source and clone volumes before cleanup.
- Source volume deletion fails without `--delete-snapshots` and succeeds with it after the clone is removed.
- A disposable iSCSI-backed guest is created on the configured migration source and live-migrated to the configured migration target when `tests.run_migration` is true. The ephemeral lab default is Ubuntu-to-Ubuntu.

The test script creates unique volume names containing the test ID. By default the test ID is the build ID, but `--test-id` can override it when rerunning tests against the same artifact directory. Successful full storage gates clean up their test volumes; failed runs leave volumes behind for inspection.

Set `tests.run_migration` to `true` to enable the live-migration smoke gate. The gate downloads the configured `tests.migration_image_url`, writes it into a temporary Subvirt iSCSI volume sized by `tests.migration_volume_size`, defines `tests.migration_domain` on `hosts.migration_source`, live-migrates it to `hosts.migration_target` over `qemu+ssh`, verifies it is running on the destination, and then removes the guest and volume. Migration requires `ssh.identity_files`; the release harness copies the first configured identity and run-local known_hosts file into each storage or migration host. If migration hosts are not configured, the release harness falls back to the storage test pair.

The smoke VM uses `tests.migration_machine`, which defaults to `auto`. Auto mode selects a concrete QEMU machine type that both hypervisors advertise and rejects unsafe distro aliases such as `pc` and `q35`. If no common concrete machine type exists, the gate fails before provisioning the temporary volume. This is expected for mixed Ubuntu/AlmaLinux live migration because their QEMU packages expose different machine-type families; production migration clusters should use a compatible hypervisor OS/QEMU baseline. The lab config includes `vms.ubuntu_migration_peer` so the default migration proof uses a compatible Ubuntu-to-Ubuntu pair while the regular storage matrix still covers Ubuntu and AlmaLinux.

## Current Implementation Notes

The next package rebuild must include the runtime fixes already validated on the hosts:

- Provider service `ReadWritePaths=/etc/iscsi /var/lib/iscsi`.
- Provider iSCSI node creation and NVMe-oF subnqn-specific path lookup.
- Libvirt backend `read()` socket framing fix.
- Libvirt backend `createVol` callback.
- Managed-dataset pool capacity reporting.
- Virt-manager `truenas` pool label and volume-creation enablement.
