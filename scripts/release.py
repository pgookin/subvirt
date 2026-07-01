#!/usr/bin/env python3
"""Release orchestrator for patched libvirt and the TrueNAS provider.

The script intentionally defaults to dry-run mode. Pass --execute to run remote
commands that build, publish, test, or promote packages.
"""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
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


def remote(host: str, command: str, execute: bool) -> None:
    run(["ssh", host, command], execute)


def rsync_to(src: str, host: str, dst: str, execute: bool, excludes: list[str] | None = None) -> None:
    argv = ["rsync", "-a", "--delete"]
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
        remote(host, f"install -d -m 0755 {q(workdir)}", ctx.execute)
        rsync_to(source_path, host, workdir + "/", ctx.execute, excludes)
        return
    if source_mode != "git":
        raise ValueError(f"unsupported project.source_mode: {source_mode}")
    repo_url = p["repo_url"]
    origin_ref = f"origin/{ctx.ref}"
    checkout = " ".join([
        "if git rev-parse --verify --quiet",
        q(origin_ref),
        ">/dev/null; then git checkout --detach",
        q(origin_ref),
        "; else git checkout",
        q(ctx.ref),
        "; fi",
    ])
    command = " && ".join([
        f"if test -d {q(workdir)}/.git; then true; else rm -rf {q(workdir)} && git clone {q(repo_url)} {q(workdir)}; fi",
        f"cd {q(workdir)}",
        "git fetch --tags --prune origin",
        checkout,
        "git submodule update --init --recursive || true",
    ])
    remote(host, command, ctx.execute)


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
    remote(host, command, ctx.execute)


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
    remote(host, command, ctx.execute)


def collect_artifacts(ctx: Context) -> None:
    p = project(ctx)
    local = Path("artifacts") / ctx.build_id
    host = hosts(ctx)["build"]
    for distro in ["ubuntu", "alma"]:
        src = f"{p['artifact_dir']}/{ctx.build_id}/{distro}/"
        dst = str(local / distro) + "/"
        run(["mkdir", "-p", dst], ctx.execute)
        run(["rsync", "-a", f"{host}:{src}", dst], ctx.execute)


def publish_staging(ctx: Context) -> None:
    repo_host = hosts(ctx)["repo"]
    r = repos(ctx)
    remote_base = f"/srv/subvirt/incoming/{ctx.build_id}"
    remote(repo_host, f"install -d -m 0755 {q(remote_base)}", ctx.execute)
    rsync_to(f"artifacts/{ctx.build_id}/", repo_host, remote_base + "/", ctx.execute)
    command = " ".join([
        "/usr/local/libexec/subvirt/publish-repo.py",
        f"--incoming {q(remote_base)}",
        f"--web-root {q(r['web_root'])}",
        f"--suite {q(r['apt_distribution'])}",
        "--component staging",
        f"--yum-distro-path {q(r.get('yum_distro_path', 'almalinux/10'))}",
    ])
    remote(repo_host, command, ctx.execute)

def storage_base_args(ctx: Context) -> str:
    t = tests(ctx)
    return "--build-id {build_id} --iscsi-pool {iscsi_pool} --nvmeof-pool {nvmeof_pool} --iscsi-pool-xml {iscsi_xml} --nvmeof-pool-xml {nvmeof_xml} --migration-domain {domain}".format(
        build_id=q(test_id(ctx)),
        iscsi_pool=q(t["iscsi_pool"]),
        nvmeof_pool=q(t["nvmeof_pool"]),
        iscsi_xml=q(t["iscsi_pool_xml"]),
        nvmeof_xml=q(t["nvmeof_pool_xml"]),
        domain=q(t["migration_domain"]),
    )


def run_storage_gate(ctx: Context) -> None:
    p = project(ctx)
    ubuntu = hosts(ctx)["ubuntu_test"]
    alma = hosts(ctx)["alma_test"]
    for host in (ubuntu, alma):
        remote_checkout(ctx, host)
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
    migration = " && ".join([
        f"cd {q(p['workdir'])}",
        f"./scripts/test-storage.py --action migrate --role ubuntu --peer {q(alma)} {base}",
    ])
    remote(ubuntu, ubuntu_create, ctx.execute)
    remote(alma, alma_create, ctx.execute)
    remote(ubuntu, ubuntu_check, ctx.execute)
    remote(alma, alma_check, ctx.execute)
    if tests(ctx).get("run_migration", bool(tests(ctx).get("migration_domain"))):
        remote(ubuntu, migration, ctx.execute)


def test_staging(ctx: Context) -> None:
    run_storage_gate(ctx)


def artifact_stage_dir(ctx: Context, distro: str) -> str:
    return f"/tmp/subvirt-artifacts/{test_id(ctx)}/{distro}"


def copy_artifacts_to_test_host(ctx: Context, distro: str, target: str, pattern: str) -> None:
    build_host = hosts(ctx)["build"]
    stage_dir = artifact_stage_dir(ctx, distro)
    remote(target, f"install -d -m 0755 {q(stage_dir)}", ctx.execute)
    run([
        "scp",
        "-3",
        f"{build_host}:{artifact_dir(ctx, distro)}/{pattern}",
        f"{target}:{stage_dir}/",
    ], ctx.execute)


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
        "for unit in virtstoraged.service libvirtd.service; do systemctl list-unit-files \"$unit\" --no-legend | grep -q . && { systemctl restart \"$unit\"; break; }; done",
        "systemctl list-unit-files virtqemud.service --no-legend | grep -q . && systemctl restart virtqemud.service || true",
        "systemctl is-active truenas-libvirt-provider.service",
        "for i in $(seq 1 20); do test -S /run/truenas-libvirt/provider.sock && break; sleep 0.25; done",
        "test -S /run/truenas-libvirt/provider.sock",
        "/usr/libexec/truenas-libvirt/truenas_provider_daemon.py call health.check '{}'",
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
        service_validation_command(),
        virt_manager_validation_command(),
    ])
    remote(host, command, ctx.execute)


def install_alma_artifacts(ctx: Context) -> None:
    host = hosts(ctx)["alma_test"]
    stage_dir = artifact_stage_dir(ctx, "alma")
    copy_artifacts_to_test_host(ctx, "alma", host, "*.rpm")
    package_filter = "packages=$(find . -maxdepth 1 -type f -name '*.rpm' ! -name '*.src.rpm' ! -name '*debuginfo*' ! -name '*debugsource*' | sort)"
    command = " && ".join([
        f"cd {q(stage_dir)}",
        package_filter,
        "test -n \"$packages\"",
        "dnf install -y $packages",
        service_validation_command(),
        virt_manager_validation_command(),
    ])
    remote(host, command, ctx.execute)


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
    remote(repo_host, command, ctx.execute)

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
    parser.add_argument("command", choices=["checkout-build", "build", "collect", "test-artifacts", "publish-staging", "test-staging", "promote", "release"])
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
        "collect": lambda: collect_artifacts(ctx),
        "test-artifacts": lambda: test_artifacts(ctx),
        "publish-staging": lambda: publish_staging(ctx),
        "test-staging": lambda: test_staging(ctx),
        "promote": lambda: promote(ctx),
        "release": lambda: release(ctx),
    }
    actions[args.command]()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
