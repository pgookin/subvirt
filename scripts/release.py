#!/usr/bin/env python3
"""Release orchestrator for patched libvirt and the TrueNAS provider.

The script intentionally defaults to dry-run mode. Pass --execute to run remote
commands that build, publish, test, or promote packages.
"""

from __future__ import annotations

import argparse
import json
import shlex
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True)
class Context:
    config: dict
    execute: bool
    ref: str
    build_id: str
    test_id_override: str | None = None


def q(value: str) -> str:
    return shlex.quote(value)


def load_config(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def run(argv: list[str], execute: bool) -> None:
    print("+ " + " ".join(q(part) for part in argv))
    if execute:
        subprocess.run(argv, check=True)


def is_local_host(host: str) -> bool:
    local_names = {"localhost", "127.0.0.1", "::1", socket.gethostname(), socket.getfqdn()}
    return host in local_names


def ssh_identity_args(config: dict) -> list[str]:
    args: list[str] = []
    seen: set[str] = set()
    for item in config.get("ssh", {}).get("identity_files", []):
        expanded = str(Path(item).expanduser())
        if expanded in seen or not Path(expanded).exists():
            continue
        seen.add(expanded)
        args.extend(["-i", expanded])
    return args


def ssh_args(config: dict) -> list[str]:
    return ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new", *ssh_identity_args(config)]


def remote(host: str, command: str, ctx_or_execute) -> None:
    if isinstance(ctx_or_execute, Context):
        execute = ctx_or_execute.execute
        config = ctx_or_execute.config
    else:
        execute = ctx_or_execute
        config = {}
    if is_local_host(host):
        run(["bash", "-lc", command], execute)
    else:
        run([*ssh_args(config), host, command], execute)


def rsync_to(src: str, host: str, dst: str, ctx_or_execute, excludes: list[str] | None = None) -> None:
    if isinstance(ctx_or_execute, Context):
        execute = ctx_or_execute.execute
        config = ctx_or_execute.config
    else:
        execute = ctx_or_execute
        config = {}
    argv = ["rsync", "-a", "--delete"]
    identities = ssh_identity_args(config)
    if identities:
        argv.extend(["-e", " ".join(q(part) for part in ssh_args(config))])
    for pattern in excludes or []:
        argv.extend(["--exclude", pattern])
    argv.extend([src, f"{host}:{dst}"])
    run(argv, execute)


def project(ctx: Context) -> dict:
    return ctx.config["project"]


def hosts(ctx: Context) -> dict:
    return ctx.config["hosts"]


def repos(ctx: Context) -> dict:
    return ctx.config["repos"]


def public_repo(ctx: Context) -> dict:
    return ctx.config.get("public_repo", {})


def tests(ctx: Context) -> dict:
    return ctx.config["tests"]


def artifact_dir(ctx: Context, distro: str) -> str:
    p = project(ctx)
    return f"{p['artifact_dir']}/{ctx.build_id}/{distro}"


def test_id(ctx: Context) -> str:
    if ctx.test_id_override:
        return ctx.test_id_override.replace("/", "-")
    return ctx.build_id.replace("/", "-")


def remote_checkout(ctx: Context, host: str) -> None:
    p = project(ctx)
    workdir = p["workdir"]
    source_mode = p.get("source_mode", "git")
    if source_mode == "rsync":
        source_path = p.get("source_path", ".").rstrip("/") + "/"
        excludes = p.get("rsync_excludes", [])
        remote(host, f"install -d -m 0755 {q(workdir)}", ctx)
        rsync_to(source_path, host, workdir + "/", ctx, excludes)
        return
    if source_mode != "git":
        raise ValueError(f"unsupported project.source_mode: {source_mode}")
    repo_url = p["repo_url"]
    origin_ref = f"origin/{ctx.ref}"
    q_workdir = q(workdir)
    q_repo = q(repo_url)
    q_origin_ref = q(origin_ref)
    q_ref = q(ctx.ref)
    checkout = " ".join([
        "if git rev-parse --verify --quiet",
        q_origin_ref,
        ">/dev/null; then git checkout --force --detach",
        q_origin_ref,
        "&& git reset --hard",
        q_origin_ref,
        "; else git checkout --force",
        q_ref,
        "&& git reset --hard",
        q_ref,
        "; fi",
    ])
    command = " && ".join([
        f"git config --global --add safe.directory {q_workdir} || true",
        f"if test -d {q_workdir}/.git; then true; else rm -rf {q_workdir} && git clone {q_repo} {q_workdir}; fi",
        f"cd {q_workdir}",
        "git fetch --tags --prune origin",
        checkout,
        "git clean -ffd",
        "(git submodule update --init --recursive || true)",
    ])
    remote(host, command, ctx)


def checkout_build(ctx: Context) -> None:
    remote_checkout(ctx, hosts(ctx)["build"])


def build_ubuntu(ctx: Context) -> None:
    host = hosts(ctx)["build"]
    p = project(ctx)
    workdir = p["workdir"]
    out_dir = artifact_dir(ctx, "ubuntu")
    remote_checkout(ctx, host)
    command = " && ".join([
        f"install -d -m 0755 {q(out_dir)}",
        f"cd {q(workdir)}",
        "./scripts/container-build-ubuntu.sh",
        f"find dist -maxdepth 1 -type f \\( -name '*.deb' -o -name '*.dsc' -o -name '*.changes' -o -name '*.buildinfo' -o -name '*.tar.*' \\) -exec cp -a {{}} {q(out_dir)}/ \\;",
    ])
    remote(host, command, ctx)


def build_alma(ctx: Context) -> None:
    host = hosts(ctx)["build"]
    p = project(ctx)
    workdir = p["workdir"]
    out_dir = artifact_dir(ctx, "alma")
    remote_checkout(ctx, host)
    command = " && ".join([
        f"install -d -m 0755 {q(out_dir)}",
        f"cd {q(workdir)}",
        "./scripts/container-build-alma.sh",
        f"find dist -maxdepth 1 -type f -name '*.rpm' -exec cp -a {{}} {q(out_dir)}/ \\;",
    ])
    remote(host, command, ctx)


def build_ubuntu_provider(ctx: Context) -> None:
    host = hosts(ctx)["build"]
    p = project(ctx)
    workdir = p["workdir"]
    out_dir = artifact_dir(ctx, "ubuntu")
    remote_checkout(ctx, host)
    command = " && ".join([
        f"install -d -m 0755 {q(out_dir)}",
        f"cd {q(workdir)}",
        "./scripts/container-build-provider-ubuntu.sh",
        f"find dist -maxdepth 1 -type f -name 'truenas-libvirt-provider_*.deb' -exec cp -a {{}} {q(out_dir)}/ \\;",
    ])
    remote(host, command, ctx)


def build_alma_provider(ctx: Context) -> None:
    host = hosts(ctx)["build"]
    p = project(ctx)
    workdir = p["workdir"]
    out_dir = artifact_dir(ctx, "alma")
    remote_checkout(ctx, host)
    command = " && ".join([
        f"install -d -m 0755 {q(out_dir)}",
        f"cd {q(workdir)}",
        "./scripts/container-build-provider-alma.sh",
        f"find dist -maxdepth 1 -type f -name 'truenas-libvirt-provider-*.rpm' -exec cp -a {{}} {q(out_dir)}/ \\;",
    ])
    remote(host, command, ctx)


def build_provider(ctx: Context) -> None:
    build_ubuntu_provider(ctx)
    build_alma_provider(ctx)


def collect_artifact(ctx: Context, distro: str) -> None:
    p = project(ctx)
    local = Path("artifacts") / ctx.build_id
    host = hosts(ctx)["build"]
    src = f"{p['artifact_dir']}/{ctx.build_id}/{distro}/"
    dst = str(local / distro) + "/"
    run(["mkdir", "-p", dst], ctx)
    if is_local_host(host):
        run(["rsync", "-a", src, dst], ctx)
    else:
        run(["rsync", "-a", f"{host}:{src}", dst], ctx)


def collect_artifacts(ctx: Context) -> None:
    for distro in ["ubuntu", "alma"]:
        collect_artifact(ctx, distro)


def publish_staging(ctx: Context) -> None:
    repo_host = hosts(ctx)["repo"]
    r = repos(ctx)
    remote_base = f"/srv/subvirt/incoming/{ctx.build_id}"
    remote(repo_host, f"install -d -m 0755 {q(remote_base)}", ctx)
    rsync_to(f"artifacts/{ctx.build_id}/", repo_host, remote_base + "/", ctx)
    command = " ".join([
        "/usr/local/libexec/subvirt/publish-repo.py",
        f"--incoming {q(remote_base)}",
        f"--web-root {q(r['web_root'])}",
        f"--suite {q(r['apt_distribution'])}",
        "--component staging",
        f"--yum-distro-path {q(r.get('yum_distro_path', 'almalinux/10'))}",
    ])
    remote(repo_host, command, ctx)


def public_repo_config(ctx: Context) -> dict:
    r = repos(ctx)
    public = public_repo(ctx)
    return {
        "host": public.get("host", hosts(ctx).get("public_repo")),
        "incoming_root": public.get("incoming_root", "/srv/subvirt/incoming"),
        "web_root": public.get("web_root", r["web_root"]),
        "apt_distribution": public.get("apt_distribution", r["apt_distribution"]),
        "yum_distro_path": public.get("yum_distro_path", r.get("yum_distro_path", "almalinux/10")),
        "base_url": public.get("base_url", "https://repo.subvirt.net").rstrip("/"),
        "gpg_name": public.get("gpg_name", "Subvirt Repository <repo@subvirt.net>"),
        "publish_script": public.get("publish_script", "/usr/local/libexec/subvirt/publish-repo.py"),
    }


def require_public_repo(ctx: Context) -> dict:
    public = public_repo_config(ctx)
    if not public.get("host"):
        raise SystemExit("public_repo.host or hosts.public_repo is required")
    return public


def artifact_files(ctx: Context, distro: str, suffix: str) -> list[Path]:
    root = Path("artifacts") / ctx.build_id / distro
    if not root.exists():
        return []
    return sorted(path for path in root.iterdir() if path.is_file() and path.name.endswith(suffix))


def publish_public_stable(ctx: Context) -> None:
    public = require_public_repo(ctx)
    remote_base = f"{public['incoming_root'].rstrip('/')}/{ctx.build_id}"
    remote(public["host"], f"install -d -m 0755 {q(remote_base)}", ctx)
    rsync_to(f"artifacts/{ctx.build_id}/", public["host"], remote_base + "/", ctx)
    command = " ".join([
        q(public["publish_script"]),
        f"--incoming {q(remote_base)}",
        f"--web-root {q(public['web_root'])}",
        f"--suite {q(public['apt_distribution'])}",
        "--component stable",
        f"--yum-distro-path {q(public['yum_distro_path'])}",
        f"--gpg-name {q(public['gpg_name'])}",
        "--skip-restorecon",
    ])
    remote(public["host"], command, ctx)


def check_url(url: str, execute: bool) -> None:
    print(f"+ check-url {url}")
    if not execute:
        return
    request = urllib.request.Request(url, method="HEAD")
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            if response.status >= 400:
                raise RuntimeError(f"{url} returned HTTP {response.status}")
            return
    except urllib.error.HTTPError as err:
        if err.code not in {405, 501}:
            raise
    request = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(request, timeout=20) as response:
        if response.status >= 400:
            raise RuntimeError(f"{url} returned HTTP {response.status}")


def verify_public_stable(ctx: Context) -> None:
    public = require_public_repo(ctx)
    base = public["base_url"]
    urls = [
        f"{base}/keys/subvirt.asc",
        f"{base}/keys/subvirt.gpg",
    ]
    debs = artifact_files(ctx, "ubuntu", ".deb")
    if debs:
        suite = public["apt_distribution"]
        urls.extend([
            f"{base}/apt/ubuntu/dists/{suite}/Release",
            f"{base}/apt/ubuntu/dists/{suite}/InRelease",
            f"{base}/apt/ubuntu/dists/{suite}/stable/binary-amd64/Packages.gz",
        ])
        urls.extend(f"{base}/apt/ubuntu/pool/stable/{path.name}" for path in debs)
    rpms = [
        path for path in artifact_files(ctx, "alma", ".rpm")
        if not path.name.endswith(".src.rpm") and "debuginfo" not in path.name and "debugsource" not in path.name
    ]
    if rpms:
        yum_path = public["yum_distro_path"].strip("/")
        urls.extend([
            f"{base}/yum/{yum_path}/stable/repodata/repomd.xml",
            f"{base}/yum/{yum_path}/stable/repodata/repomd.xml.asc",
        ])
        urls.extend(f"{base}/yum/{yum_path}/stable/{path.name}" for path in rpms)
    if not debs and not rpms:
        raise SystemExit(f"no public-verifiable packages found in artifacts/{ctx.build_id}")
    for url in urls:
        check_url(url, ctx.execute)

def storage_base_args(ctx: Context) -> str:
    t = tests(ctx)
    args = "--build-id {build_id} --iscsi-pool {iscsi_pool} --nvmeof-pool {nvmeof_pool} --iscsi-pool-xml {iscsi_xml} --nvmeof-pool-xml {nvmeof_xml} --migration-domain {domain}".format(
        build_id=q(test_id(ctx)),
        iscsi_pool=q(t["iscsi_pool"]),
        nvmeof_pool=q(t["nvmeof_pool"]),
        iscsi_xml=q(t["iscsi_pool_xml"]),
        nvmeof_xml=q(t["nvmeof_pool_xml"]),
        domain=q(t["migration_domain"]),
    )
    if "min_pool_capacity_gib" in t:
        args += f" --min-pool-capacity-gib {int(t['min_pool_capacity_gib'])}"
    return args


def sync_storage_pool_xml(ctx: Context, host: str) -> None:
    t = tests(ctx)
    for key in ("iscsi_pool_xml", "nvmeof_pool_xml"):
        path = t[key]
        remote(host, f"install -d -m 0755 {q(str(Path(path).parent))}", ctx)
        rsync_to(path, host, path, ctx)


def run_storage_gate(ctx: Context) -> None:
    p = project(ctx)
    ubuntu = hosts(ctx)["ubuntu_test"]
    alma = hosts(ctx)["alma_test"]
    for host in (ubuntu, alma):
        remote_checkout(ctx, host)
        sync_storage_pool_xml(ctx, host)
    base = storage_base_args(ctx)
    ubuntu_create = " && ".join([
        f"cd {q(p['workdir'])}",
        f"./scripts/test-storage.py --action create --role ubuntu --peer {q(alma)} {base}",
    ])
    alma_create = " && ".join([
        f"cd {q(p['workdir'])}",
        f"./scripts/test-storage.py --action create --role alma --peer {q(ubuntu)} {base}",
    ])
    ubuntu_check = " && ".join([
        f"cd {q(p['workdir'])}",
        f"./scripts/test-storage.py --action check-peer --role ubuntu --peer {q(alma)} {base}",
    ])
    alma_check = " && ".join([
        f"cd {q(p['workdir'])}",
        f"./scripts/test-storage.py --action check-peer --role alma --peer {q(ubuntu)} {base}",
    ])
    ubuntu_delete_check = " && ".join([
        f"cd {q(p['workdir'])}",
        f"./scripts/test-storage.py --action delete-check --role ubuntu --peer {q(alma)} {base}",
    ])
    alma_delete_check = " && ".join([
        f"cd {q(p['workdir'])}",
        f"./scripts/test-storage.py --action delete-check --role alma --peer {q(ubuntu)} {base}",
    ])
    migration = " && ".join([
        f"cd {q(p['workdir'])}",
        f"./scripts/test-storage.py --action migrate --role ubuntu --peer {q(alma)} {base}",
    ])
    remote(ubuntu, ubuntu_create, ctx)
    remote(alma, alma_create, ctx)
    remote(ubuntu, ubuntu_check, ctx)
    remote(alma, alma_check, ctx)
    remote(ubuntu, ubuntu_delete_check, ctx)
    remote(alma, alma_delete_check, ctx)
    if tests(ctx).get("run_migration", bool(tests(ctx).get("migration_domain"))):
        remote(ubuntu, migration, ctx)




def lab_config(ctx: Context) -> dict:
    return ctx.config.get("lab", {})


def test_ephemeral_lab(ctx: Context) -> None:
    lab = lab_config(ctx)
    host = lab.get("host", hosts(ctx)["build"])
    p = project(ctx)
    workdir = p["workdir"]
    config_path = lab.get("config", "/srv/subvirt/release/lab.json")
    artifacts = f"{p['artifact_dir']}/{ctx.build_id}"
    create_command = "create" if lab.get("full", False) else "create-linux"
    remote_checkout(ctx, host)
    command = " ; ".join([
        "set -e",
        f"cd {q(workdir)}",
        "set +e",
        f"./scripts/lab.py {create_command} --config {q(config_path)} --build-id {q(ctx.build_id)} --execute && "
        f"./scripts/lab.py publish-repo --config {q(config_path)} --build-id {q(ctx.build_id)} --artifacts {q(artifacts)} --execute && "
        f"./scripts/lab.py test-repo --config {q(config_path)} --build-id {q(ctx.build_id)} --execute",
        "rc=$?",
        f"if test $rc -eq 0; then ./scripts/lab.py destroy --config {q(config_path)} --build-id {q(ctx.build_id)} --execute; "
        f"else echo 'Ephemeral lab preserved for failed build {ctx.build_id}. Cleanup with: ./scripts/lab.py destroy --config {q(config_path)} --build-id {q(ctx.build_id)} --execute' >&2; fi",
        "exit $rc",
    ])
    remote(host, command, ctx)


def test_staging(ctx: Context) -> None:
    if lab_config(ctx).get("enabled"):
        test_ephemeral_lab(ctx)
    else:
        run_storage_gate(ctx)


def artifact_stage_dir(ctx: Context, distro: str) -> str:
    return f"/tmp/subvirt-artifacts/{test_id(ctx)}/{distro}"


def scp_args(config: dict) -> list[str]:
    return ["scp", *ssh_args(config)[1:]]


def copy_artifacts_to_test_host(ctx: Context, distro: str, target: str, pattern: str) -> None:
    build_host = hosts(ctx)["build"]
    stage_dir = artifact_stage_dir(ctx, distro)
    remote(target, f"install -d -m 0755 {q(stage_dir)}", ctx)
    if is_local_host(build_host):
        run([
            *scp_args(ctx.config),
            *sorted(str(path) for path in Path(artifact_dir(ctx, distro)).glob(pattern)),
            f"{target}:{stage_dir}/",
        ], ctx)
    else:
        run([
            *scp_args(ctx.config),
            "-3",
            f"{build_host}:{artifact_dir(ctx, distro)}/{pattern}",
            f"{target}:{stage_dir}/",
        ], ctx)


def virt_manager_validation_command() -> str:
    script = "; ".join([
        "from pathlib import Path",
        "text = Path('/usr/share/virt-manager/virtManager/object/storagepool.py').read_text(encoding='utf-8')",
        "assert '\"truenas\": _(' in text",
        "start = text.index('def supports_volume_creation')",
        "end = text.index('def ', start + 1)",
        "assert '\"truenas\"' in text[start:end]",
        "import sys",
        "sys.path.insert(0, '/usr/share/virt-manager')",
        "from virtinst import StoragePool",
        "assert StoragePool.TYPE_TRUENAS == 'truenas'",
        "assert hasattr(StoragePool, 'source_protocol')",
        "print('virt-manager truenas pool support OK')",
    ])
    return " && ".join([
        "test -d /usr/share/virt-manager",
        f"python3 -c {q(script)}",
    ])


def service_validation_command() -> str:
    return " && ".join([
        "systemctl daemon-reload",
        "systemctl enable --now truenas-libvirt-provider.service",
        "systemctl restart truenas-libvirt-provider.service",
        "for unit in virtqemud.socket virtstoraged.socket virtproxyd.socket virtlogd.socket virtlockd.socket; do systemctl list-unit-files \"$unit\" --no-legend | grep -q . && systemctl enable --now \"$unit\" || true; done",
        "for unit in virtstoraged.service libvirtd.service; do systemctl list-unit-files \"$unit\" --no-legend | grep -q . && { systemctl restart \"$unit\"; break; }; done",
        "systemctl list-unit-files virtqemud.service --no-legend | grep -q . && systemctl restart virtqemud.service || true",
        "systemctl is-active truenas-libvirt-provider.service",
        "for i in $(seq 1 20); do test -S /run/truenas-libvirt/provider.sock && break; sleep 0.25; done",
        "test -S /run/truenas-libvirt/provider.sock",
        "/usr/libexec/truenas-libvirt/truenas_provider_daemon.py doctor --json",
        "test -n \"$(find /usr/lib /usr/lib64 -name libvirt_storage_backend_truenas.so -print -quit 2>/dev/null)\"",
        "virsh pool-capabilities | grep \"type='truenas'\"",
    ])


def install_ubuntu_artifacts(ctx: Context) -> None:
    host = hosts(ctx)["ubuntu_test"]
    stage_dir = artifact_stage_dir(ctx, "ubuntu")
    copy_artifacts_to_test_host(ctx, "ubuntu", host, "*.deb")
    package_filter = "packages=$(find . -maxdepth 1 -type f -name '*.deb' ! -name 'libvirt-daemon-system-sysv_*' | sort)"
    command = " && ".join([
        f"cd {q(stage_dir)}",
        package_filter,
        "test -n \"$packages\"",
        "apt-get update",
        "DEBIAN_FRONTEND=noninteractive apt-get install -y --allow-downgrades -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold $packages",
        "DEBIAN_FRONTEND=noninteractive dpkg -i --force-confdef --force-confold $packages",
        service_validation_command(),
        virt_manager_validation_command(),
    ])
    remote(host, command, ctx)


def install_alma_artifacts(ctx: Context) -> None:
    host = hosts(ctx)["alma_test"]
    stage_dir = artifact_stage_dir(ctx, "alma")
    copy_artifacts_to_test_host(ctx, "alma", host, "*.rpm")
    package_filter = "packages=$(find . -maxdepth 1 -type f -name '*.rpm' ! -name '*.src.rpm' ! -name '*debuginfo*' ! -name '*debugsource*' | sort)"
    command = " && ".join([
        f"cd {q(stage_dir)}",
        package_filter,
        "test -n \"$packages\"",
        "dnf --disablerepo='subvirt-*' install -y $packages",
        "rpm -Uvh --replacepkgs $packages",
        service_validation_command(),
        virt_manager_validation_command(),
    ])
    remote(host, command, ctx)


def test_artifacts(ctx: Context) -> None:
    install_ubuntu_artifacts(ctx)
    install_alma_artifacts(ctx)
    run_storage_gate(ctx)


def promote(ctx: Context) -> None:
    repo_host = hosts(ctx)["repo"]
    r = repos(ctx)
    remote_base = f"/srv/subvirt/incoming/{ctx.build_id}"
    command = " ".join([
        "/usr/local/libexec/subvirt/publish-repo.py",
        f"--incoming {q(remote_base)}",
        f"--web-root {q(r['web_root'])}",
        f"--suite {q(r['apt_distribution'])}",
        "--component stable",
        f"--yum-distro-path {q(r.get('yum_distro_path', 'almalinux/10'))}",
    ])
    remote(repo_host, command, ctx)

def release(ctx: Context) -> None:
    build_ubuntu(ctx)
    build_alma(ctx)
    collect_artifacts(ctx)
    test_artifacts(ctx)
    publish_staging(ctx)
    test_staging(ctx)
    promote(ctx)


def parse_args(argv: Iterable[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=[
        "checkout-build",
        "build",
        "build-ubuntu",
        "build-alma",
        "build-provider",
        "build-ubuntu-provider",
        "build-alma-provider",
        "collect",
        "collect-ubuntu",
        "collect-alma",
        "test-artifacts",
        "test-ubuntu-artifacts",
        "test-alma-artifacts",
        "publish-staging",
        "publish-public-stable",
        "verify-public-stable",
        "test-staging",
        "test-lab",
        "promote",
        "release",
    ])
    parser.add_argument("--config", default="release/release.json")
    parser.add_argument("--ref", default="HEAD")
    parser.add_argument("--build-id", required=True)
    parser.add_argument("--test-id", help="override the storage test ID while using artifacts from --build-id")
    parser.add_argument("--execute", action="store_true", help="actually run commands; default is dry-run")
    return parser.parse_args(list(argv))


def main(argv: Iterable[str] = sys.argv[1:]) -> int:
    args = parse_args(argv)
    ctx = Context(load_config(Path(args.config)), args.execute, args.ref, args.build_id, args.test_id)
    actions = {
        "checkout-build": lambda: checkout_build(ctx),
        "build": lambda: (build_ubuntu(ctx), build_alma(ctx)),
        "build-ubuntu": lambda: build_ubuntu(ctx),
        "build-alma": lambda: build_alma(ctx),
        "build-provider": lambda: build_provider(ctx),
        "build-ubuntu-provider": lambda: build_ubuntu_provider(ctx),
        "build-alma-provider": lambda: build_alma_provider(ctx),
        "collect": lambda: collect_artifacts(ctx),
        "collect-ubuntu": lambda: collect_artifact(ctx, "ubuntu"),
        "collect-alma": lambda: collect_artifact(ctx, "alma"),
        "test-artifacts": lambda: test_artifacts(ctx),
        "test-ubuntu-artifacts": lambda: install_ubuntu_artifacts(ctx),
        "test-alma-artifacts": lambda: install_alma_artifacts(ctx),
        "publish-staging": lambda: publish_staging(ctx),
        "publish-public-stable": lambda: publish_public_stable(ctx),
        "verify-public-stable": lambda: verify_public_stable(ctx),
        "test-staging": lambda: test_staging(ctx),
        "test-lab": lambda: test_ephemeral_lab(ctx),
        "promote": lambda: promote(ctx),
        "release": lambda: release(ctx),
    }
    actions[args.command]()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
