#!/usr/bin/env python3
"""Storage smoke tests for staging packages.

This test intentionally creates uniquely named sparse zvols. Successful full
storage gates clean up their volumes, while failed runs leave volumes behind
for inspection.
"""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.request
from typing import Iterable


def q(argv: Iterable[str]) -> str:
    import shlex
    return " ".join(shlex.quote(str(item)) for item in argv)


def run(argv: list[str]) -> str:
    print("+ " + q(argv), flush=True)
    result = subprocess.run(argv, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)
    if result.stdout:
        print(result.stdout, end="")
    result.check_returncode()
    return result.stdout


def run_expect_failure(argv: list[str], expected: str | None = None) -> str:
    print("+ " + q(argv), flush=True)
    result = subprocess.run(argv, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)
    if result.stdout:
        print(result.stdout, end="")
    if result.returncode == 0:
        raise RuntimeError(f"command unexpectedly succeeded: {q(argv)}")
    if expected is not None and expected not in result.stdout:
        raise RuntimeError(f"command failed without expected text {expected!r}: {q(argv)}")
    return result.stdout


def virsh(*args: str) -> str:
    return run(["virsh", "-c", "qemu:///system", *args])


def virsh_expect_failure(expected: str | None, *args: str) -> str:
    return run_expect_failure(["virsh", "-c", "qemu:///system", *args], expected)


def ssh_args(peer: str) -> list[str]:
    argv = ["ssh", "-o", "BatchMode=yes"]
    identity = os.environ.get("SUBVIRT_TEST_SSH_IDENTITY_FILE", "")
    known_hosts = os.environ.get("SUBVIRT_TEST_SSH_KNOWN_HOSTS_FILE", "")
    if identity:
        argv.extend(["-i", identity])
    if known_hosts:
        argv.extend(["-o", f"UserKnownHostsFile={known_hosts}", "-o", "StrictHostKeyChecking=yes"])
    else:
        argv.extend(["-o", "StrictHostKeyChecking=accept-new"])
    argv.append(peer)
    return argv


def remote(peer: str, *args: str) -> str:
    return run([*ssh_args(peer), *args])


def remote_virsh(peer: str, *args: str) -> str:
    return remote(peer, "virsh", "-c", "qemu:///system", *args)


def ensure_pool(name: str, xml: str) -> None:
    pools = virsh("pool-list", "--all")
    if name not in pools:
        virsh("pool-define", xml)
    try:
        virsh("pool-start", name)
    except subprocess.CalledProcessError:
        pass
    virsh("pool-refresh", name)


def remote_ensure_pool(peer: str, name: str, xml: str) -> None:
    pools = remote_virsh(peer, "pool-list", "--all")
    if name not in pools:
        remote_virsh(peer, "pool-define", xml)
    try:
        remote_virsh(peer, "pool-start", name)
    except subprocess.CalledProcessError:
        pass
    remote_virsh(peer, "pool-refresh", name)


UNIT_BYTES = {
    "B": 1,
    "KiB": 1024,
    "MiB": 1024**2,
    "GiB": 1024**3,
    "TiB": 1024**4,
}


def parse_capacity(output: str) -> int:
    for line in output.splitlines():
        if line.strip().startswith("Capacity:"):
            parts = line.split()
            if len(parts) >= 3:
                return int(float(parts[1]) * UNIT_BYTES[parts[2]])
    raise RuntimeError("output did not contain Capacity")


def assert_pool_capacity(pool: str, min_gib: int) -> None:
    out = virsh("pool-info", pool)
    capacity = parse_capacity(out)
    if capacity < min_gib * 1024**3:
        raise RuntimeError(f"pool {pool} capacity {capacity} bytes is below {min_gib} GiB")


def assert_volume(pool: str, name: str) -> None:
    out = virsh("vol-info", "--pool", pool, name)
    if name not in out:
        raise RuntimeError(f"volume {name} was not visible in pool {pool}")


def assert_volume_missing(pool: str, name: str) -> None:
    virsh_expect_failure(None, "vol-info", "--pool", pool, name)


def create_volume(pool: str, name: str, size: str = "64M") -> None:
    virsh("vol-create-as", pool, name, size)
    assert_volume(pool, name)


def resize_volume(pool: str, name: str, size: str, min_bytes: int) -> None:
    virsh("vol-resize", "--pool", pool, name, size)
    virsh("pool-refresh", pool)
    out = virsh("vol-info", "--pool", pool, name)
    capacity = parse_capacity(out)
    if capacity < min_bytes:
        raise RuntimeError(f"volume {name} capacity {capacity} bytes is below expected {min_bytes} bytes")


def clone_volume(pool: str, source: str, clone: str) -> None:
    virsh("vol-clone", "--pool", pool, source, clone)
    virsh("pool-refresh", pool)
    assert_volume(pool, clone)


def delete_clone_and_source(pool: str, source: str, clone: str) -> None:
    virsh("vol-delete", "--pool", pool, clone)
    virsh("pool-refresh", pool)
    assert_volume_missing(pool, clone)

    virsh_expect_failure("delete-snapshots", "vol-delete", "--pool", pool, source)
    virsh("vol-delete", "--pool", pool, "--delete-snapshots", source)
    virsh("pool-refresh", pool)
    assert_volume_missing(pool, source)


DEFAULT_MIGRATION_IMAGE_URL = "https://download.cirros-cloud.net/0.6.2/cirros-0.6.2-x86_64-disk.img"


def volume_path(pool: str, name: str) -> str:
    return virsh("vol-path", "--pool", pool, name).strip()


def wait_for_domain_state(domain: str, state: str, timeout: int = 60, peer: str | None = None) -> None:
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        try:
            last = remote_virsh(peer, "domstate", domain) if peer else virsh("domstate", domain)
            if state in last.lower():
                return
        except subprocess.CalledProcessError as exc:
            last = str(exc)
        time.sleep(1)
    location = peer or "local"
    raise RuntimeError(f"domain {domain!r} did not reach state {state!r} on {location}: {last}")


def wait_for_domain_absent(domain: str, timeout: int = 30) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        doms = virsh("list", "--all")
        if domain not in doms:
            return
        time.sleep(1)
    raise RuntimeError(f"domain {domain!r} still exists on source after migration")


def download_migration_image(url: str, sha256: str) -> Path:
    cache = Path(os.environ.get("SUBVIRT_TEST_CACHE", "/var/cache/subvirt-tests"))
    cache.mkdir(parents=True, exist_ok=True)
    name = url.rsplit("/", 1)[-1] or "migration-image.img"
    path = cache / name
    if not path.exists():
        print(f"+ download {url} {path}", flush=True)
        with urllib.request.urlopen(url, timeout=60) as response, path.open("wb") as handle:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                handle.write(chunk)
    if sha256:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest.lower() != sha256.lower():
            raise RuntimeError(f"migration image checksum mismatch for {path}: expected {sha256}, got {digest}")
    return path


QEMU_EMULATOR_CANDIDATES = [
    "/usr/bin/qemu-system-x86_64",
    "/usr/libexec/qemu-kvm",
]


def executable_exists(path: str, peer: str | None = None) -> bool:
    try:
        if peer:
            remote(peer, "test", "-x", path)
        else:
            run(["test", "-x", path])
        return True
    except subprocess.CalledProcessError:
        return False


def first_existing_emulator(peer: str | None = None) -> str:
    for candidate in QEMU_EMULATOR_CANDIDATES:
        if executable_exists(candidate, peer=peer):
            return candidate
    location = peer or "local"
    raise RuntimeError(f"no known QEMU emulator path found on {location}")


def ensure_peer_emulator(peer: str, emulator: str) -> bool:
    if executable_exists(emulator, peer=peer):
        return False
    peer_emulator = first_existing_emulator(peer=peer)
    remote(peer, "ln", "-s", peer_emulator, emulator)
    return True


UNSAFE_MACHINE_ALIASES = {"pc", "q35", "ubuntu", "ubuntu-q35", "none"}


def machine_types(emulator: str, peer: str | None = None) -> tuple[list[str], set[str]]:
    output = remote(peer, emulator, "-machine", "help") if peer else run([emulator, "-machine", "help"])
    names: list[str] = []
    deprecated: set[str] = set()
    for line in output.splitlines():
        parts = line.split()
        if not parts or parts[0] == "Supported":
            continue
        name = parts[0]
        names.append(name)
        if "deprecated" in line:
            deprecated.add(name)
    return names, deprecated


def select_migration_machine(local_emulator: str, peer: str, requested: str) -> str:
    if requested and requested != "auto":
        return requested
    remote_emulator = first_existing_emulator(peer=peer)
    local_names, local_deprecated = machine_types(local_emulator)
    remote_names, remote_deprecated = machine_types(remote_emulator, peer=peer)
    common = [name for name in local_names if name in set(remote_names) and name not in UNSAFE_MACHINE_ALIASES]
    if not common:
        raise RuntimeError(
            "no common concrete QEMU machine type found; this hypervisor pair is not a valid live-migration baseline"
        )
    for name in common:
        if name not in local_deprecated and name not in remote_deprecated:
            return name
    return common[0]


def domain_xml(domain: str, disk_path: str, emulator: str, machine: str) -> str:
    return f"""<domain type='kvm'>
  <name>{domain}</name>
  <memory unit='MiB'>256</memory>
  <currentMemory unit='MiB'>256</currentMemory>
  <vcpu placement='static'>1</vcpu>
  <os>
    <type arch='x86_64' machine='{machine}'>hvm</type>
    <boot dev='hd'/>
  </os>
  <features>
    <acpi/>
  </features>
  <cpu mode='host-model'/>
  <clock offset='utc'/>
  <on_poweroff>destroy</on_poweroff>
  <on_reboot>restart</on_reboot>
  <on_crash>destroy</on_crash>
  <devices>
    <emulator>{emulator}</emulator>
    <disk type='block' device='disk'>
      <driver name='qemu' type='raw' cache='none' io='native'/>
      <source dev='{disk_path}'/>
      <target dev='vda' bus='virtio'/>
    </disk>
    <serial type='pty'>
      <target type='isa-serial' port='0'/>
    </serial>
    <console type='pty'>
      <target type='serial' port='0'/>
    </console>
    <memballoon model='virtio'/>
  </devices>
</domain>
"""


def define_domain(domain: str, disk_path: str, emulator: str, machine: str) -> Path:
    xml_path = Path(f"/tmp/{domain}.xml")
    xml_path.write_text(domain_xml(domain, disk_path, emulator, machine), encoding="utf-8")
    virsh("define", str(xml_path))
    return xml_path


def local_domain_exists(domain: str) -> bool:
    return domain in virsh("list", "--all")


def remote_domain_exists(peer: str, domain: str) -> bool:
    return domain in remote_virsh(peer, "list", "--all")


def cleanup_migration(domain: str, peer: str, pool: str, volume: str, peer_emulator_alias: str | None = None) -> None:
    if remote_domain_exists(peer, domain):
        remote_virsh(peer, "destroy", domain)
        remote_virsh(peer, "undefine", domain)
    if local_domain_exists(domain):
        virsh("destroy", domain)
        virsh("undefine", domain)
    virsh("pool-refresh", pool)
    if volume in virsh("vol-list", pool):
        virsh("vol-delete", "--pool", pool, volume)
        virsh("pool-refresh", pool)
    if peer_emulator_alias:
        remote(peer, "rm", "-f", peer_emulator_alias)


def migration_smoke(args: argparse.Namespace) -> None:
    domain = args.migration_domain
    volume = f"ci-{args.build_id}-migration"
    image = download_migration_image(args.migration_image_url, args.migration_image_sha256)

    emulator = first_existing_emulator()
    machine = select_migration_machine(emulator, args.peer, args.migration_machine)

    create_volume(args.iscsi_pool, volume, args.migration_volume_size)
    peer_alias = None
    try:
        disk_path = volume_path(args.iscsi_pool, volume)
        run(["qemu-img", "convert", "-O", "raw", str(image), disk_path])
        virsh("pool-refresh", args.iscsi_pool)
        remote_ensure_pool(args.peer, args.iscsi_pool, args.iscsi_pool_xml)

        peer_alias = emulator if ensure_peer_emulator(args.peer, emulator) else None
        define_domain(domain, disk_path, emulator, machine)
        virsh("start", domain)
        wait_for_domain_state(domain, "running")
        migration_uri = f"qemu+ssh://{args.peer}/system"
        identity = os.environ.get("SUBVIRT_TEST_SSH_IDENTITY_FILE", "")
        if identity:
            migration_uri += f"?keyfile={identity}&no_verify=1"
        run([
            "timeout",
            "180",
            "virsh",
            "-c",
            "qemu:///system",
            "migrate",
            "--live",
            "--persistent",
            "--undefinesource",
            domain,
            migration_uri,
        ])
        wait_for_domain_absent(domain)
        wait_for_domain_state(domain, "running", peer=args.peer)
        remote_xml = remote_virsh(args.peer, "dumpxml", domain)
        if disk_path not in remote_xml:
            raise RuntimeError(f"migrated domain XML does not reference expected shared disk path {disk_path}")
    finally:
        cleanup_migration(domain, args.peer, args.iscsi_pool, volume, peer_alias)


def parse_args(argv: Iterable[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--action", choices=["create", "check-peer", "delete-check", "migrate"], required=True)
    parser.add_argument("--role", choices=["ubuntu", "alma"], required=True)
    parser.add_argument("--peer", required=True)
    parser.add_argument("--build-id", required=True)
    parser.add_argument("--iscsi-pool", required=True)
    parser.add_argument("--nvmeof-pool", required=True)
    parser.add_argument("--iscsi-pool-xml", required=True)
    parser.add_argument("--nvmeof-pool-xml", required=True)
    parser.add_argument("--migration-domain", required=True)
    parser.add_argument("--migration-image-url", default=DEFAULT_MIGRATION_IMAGE_URL)
    parser.add_argument("--migration-image-sha256", default="")
    parser.add_argument("--migration-volume-size", default="512M")
    parser.add_argument("--migration-machine", default="auto")
    parser.add_argument("--ssh-identity-file", default="")
    parser.add_argument("--ssh-known-hosts-file", default="")
    parser.add_argument("--min-pool-capacity-gib", type=int, default=100)
    parser.add_argument("--test-resize", action="store_true", help="exercise virsh vol-resize when the backend advertises resize support")
    parser.add_argument("--test-clone", action="store_true", help="exercise virsh vol-clone when the backend advertises clone support")
    return parser.parse_args(list(argv))


def main(argv: Iterable[str] = sys.argv[1:]) -> int:
    args = parse_args(argv)
    if args.ssh_identity_file:
        os.environ["SUBVIRT_TEST_SSH_IDENTITY_FILE"] = args.ssh_identity_file
    if args.ssh_known_hosts_file:
        os.environ["SUBVIRT_TEST_SSH_KNOWN_HOSTS_FILE"] = args.ssh_known_hosts_file
    ensure_pool(args.iscsi_pool, args.iscsi_pool_xml)
    ensure_pool(args.nvmeof_pool, args.nvmeof_pool_xml)
    assert_pool_capacity(args.iscsi_pool, args.min_pool_capacity_gib)
    assert_pool_capacity(args.nvmeof_pool, args.min_pool_capacity_gib)

    iscsi_name = f"ci-{args.build_id}-iscsi"
    nvmeof_name = f"ci-{args.build_id}-nvmeof"
    iscsi_clone = f"ci-{args.build_id}-iscsi-clone"
    nvmeof_clone = f"ci-{args.build_id}-nvmeof-clone"

    if args.action == "create":
        if args.role == "ubuntu":
            create_volume(args.iscsi_pool, iscsi_name)
            if args.test_resize:
                resize_volume(args.iscsi_pool, iscsi_name, "96M", 96 * 1024**2)
            if args.test_clone:
                clone_volume(args.iscsi_pool, iscsi_name, iscsi_clone)
        else:
            create_volume(args.nvmeof_pool, nvmeof_name)
            if args.test_resize:
                resize_volume(args.nvmeof_pool, nvmeof_name, "96M", 96 * 1024**2)
            if args.test_clone:
                clone_volume(args.nvmeof_pool, nvmeof_name, nvmeof_clone)
    elif args.action == "check-peer":
        virsh("pool-refresh", args.iscsi_pool)
        virsh("pool-refresh", args.nvmeof_pool)
        if args.role == "ubuntu":
            assert_volume(args.nvmeof_pool, nvmeof_name)
            if args.test_clone:
                assert_volume(args.nvmeof_pool, nvmeof_clone)
        else:
            assert_volume(args.iscsi_pool, iscsi_name)
            if args.test_clone:
                assert_volume(args.iscsi_pool, iscsi_clone)
    elif args.action == "delete-check":
        if args.role == "ubuntu":
            if args.test_clone:
                delete_clone_and_source(args.iscsi_pool, iscsi_name, iscsi_clone)
            else:
                virsh("vol-delete", "--pool", args.iscsi_pool, iscsi_name)
                virsh("pool-refresh", args.iscsi_pool)
                assert_volume_missing(args.iscsi_pool, iscsi_name)
        else:
            if args.test_clone:
                delete_clone_and_source(args.nvmeof_pool, nvmeof_name, nvmeof_clone)
            else:
                virsh("vol-delete", "--pool", args.nvmeof_pool, nvmeof_name)
                virsh("pool-refresh", args.nvmeof_pool)
                assert_volume_missing(args.nvmeof_pool, nvmeof_name)
    elif args.action == "migrate":
        migration_smoke(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
