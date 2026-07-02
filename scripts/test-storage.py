#!/usr/bin/env python3
"""Storage smoke tests for staging packages.

This test intentionally creates uniquely named sparse zvols. Deletion is not
required because the current TrueNAS API user may not expose dataset deletion.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from typing import Iterable


def run(argv: list[str]) -> str:
    print("+ " + " ".join(argv), flush=True)
    result = subprocess.run(argv, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)
    if result.stdout:
        print(result.stdout, end="")
    result.check_returncode()
    return result.stdout


def virsh(*args: str) -> str:
    return run(["virsh", *args])


def ensure_pool(name: str, xml: str) -> None:
    pools = virsh("pool-list", "--all")
    if name not in pools:
        virsh("pool-define", xml)
    try:
        virsh("pool-start", name)
    except subprocess.CalledProcessError:
        pass
    virsh("pool-refresh", name)


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
    raise RuntimeError("pool-info output did not contain Capacity")


def assert_pool_capacity(pool: str, min_gib: int) -> None:
    out = virsh("pool-info", pool)
    capacity = parse_capacity(out)
    if capacity < min_gib * 1024**3:
        raise RuntimeError(f"pool {pool} capacity {capacity} bytes is below {min_gib} GiB")


def assert_volume(pool: str, name: str) -> None:
    out = virsh("vol-info", "--pool", pool, name)
    if name not in out:
        raise RuntimeError(f"volume {name} was not visible in pool {pool}")


def create_volume(pool: str, name: str, size: str = "64M") -> None:
    virsh("vol-create-as", pool, name, size)
    assert_volume(pool, name)


def migration_smoke(domain: str, peer: str) -> None:
    doms = virsh("list", "--all")
    if domain not in doms:
        raise RuntimeError(f"migration test domain {domain!r} is not defined")
    virsh("start", domain)
    virsh("migrate", "--live", "--persistent", "--undefinesource", domain, f"qemu+ssh://{peer}/system")


def parse_args(argv: Iterable[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--action", choices=["create", "check-peer", "migrate"], required=True)
    parser.add_argument("--role", choices=["ubuntu", "alma"], required=True)
    parser.add_argument("--peer", required=True)
    parser.add_argument("--build-id", required=True)
    parser.add_argument("--iscsi-pool", required=True)
    parser.add_argument("--nvmeof-pool", required=True)
    parser.add_argument("--iscsi-pool-xml", required=True)
    parser.add_argument("--nvmeof-pool-xml", required=True)
    parser.add_argument("--migration-domain", required=True)
    parser.add_argument("--min-pool-capacity-gib", type=int, default=100)
    return parser.parse_args(list(argv))


def main(argv: Iterable[str] = sys.argv[1:]) -> int:
    args = parse_args(argv)
    ensure_pool(args.iscsi_pool, args.iscsi_pool_xml)
    ensure_pool(args.nvmeof_pool, args.nvmeof_pool_xml)
    assert_pool_capacity(args.iscsi_pool, args.min_pool_capacity_gib)
    assert_pool_capacity(args.nvmeof_pool, args.min_pool_capacity_gib)

    iscsi_name = f"ci-{args.build_id}-iscsi"
    nvmeof_name = f"ci-{args.build_id}-nvmeof"

    if args.action == "create":
        if args.role == "ubuntu":
            create_volume(args.iscsi_pool, iscsi_name)
        else:
            create_volume(args.nvmeof_pool, nvmeof_name)
    elif args.action == "check-peer":
        virsh("pool-refresh", args.iscsi_pool)
        virsh("pool-refresh", args.nvmeof_pool)
        if args.role == "ubuntu":
            assert_volume(args.nvmeof_pool, nvmeof_name)
        else:
            assert_volume(args.iscsi_pool, iscsi_name)
    elif args.action == "migrate":
        migration_smoke(args.migration_domain, args.peer)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
