#!/usr/bin/env python3
"""Storage smoke tests for staging packages.

This test intentionally creates uniquely named sparse zvols. Successful full
storage gates clean up their volumes, while failed runs leave volumes behind
for inspection.
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


def run_expect_failure(argv: list[str], expected: str | None = None) -> str:
    print("+ " + " ".join(argv), flush=True)
    result = subprocess.run(argv, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)
    if result.stdout:
        print(result.stdout, end="")
    if result.returncode == 0:
        raise RuntimeError(f"command unexpectedly succeeded: {' '.join(argv)}")
    if expected is not None and expected not in result.stdout:
        raise RuntimeError(f"command failed without expected text {expected!r}: {' '.join(argv)}")
    return result.stdout


def virsh(*args: str) -> str:
    return run(["virsh", *args])


def virsh_expect_failure(expected: str | None, *args: str) -> str:
    return run_expect_failure(["virsh", *args], expected)


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


def migration_smoke(domain: str, peer: str) -> None:
    doms = virsh("list", "--all")
    if domain not in doms:
        raise RuntimeError(f"migration test domain {domain!r} is not defined")
    virsh("start", domain)
    virsh("migrate", "--live", "--persistent", "--undefinesource", domain, f"qemu+ssh://{peer}/system")


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
    parser.add_argument("--min-pool-capacity-gib", type=int, default=100)
    parser.add_argument("--test-resize", action="store_true", help="exercise virsh vol-resize when the backend advertises resize support")
    return parser.parse_args(list(argv))


def main(argv: Iterable[str] = sys.argv[1:]) -> int:
    args = parse_args(argv)
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
            clone_volume(args.iscsi_pool, iscsi_name, iscsi_clone)
        else:
            create_volume(args.nvmeof_pool, nvmeof_name)
            if args.test_resize:
                resize_volume(args.nvmeof_pool, nvmeof_name, "96M", 96 * 1024**2)
            clone_volume(args.nvmeof_pool, nvmeof_name, nvmeof_clone)
    elif args.action == "check-peer":
        virsh("pool-refresh", args.iscsi_pool)
        virsh("pool-refresh", args.nvmeof_pool)
        if args.role == "ubuntu":
            assert_volume(args.nvmeof_pool, nvmeof_name)
            assert_volume(args.nvmeof_pool, nvmeof_clone)
        else:
            assert_volume(args.iscsi_pool, iscsi_name)
            assert_volume(args.iscsi_pool, iscsi_clone)
    elif args.action == "delete-check":
        if args.role == "ubuntu":
            delete_clone_and_source(args.iscsi_pool, iscsi_name, iscsi_clone)
        else:
            delete_clone_and_source(args.nvmeof_pool, nvmeof_name, nvmeof_clone)
    elif args.action == "migrate":
        migration_smoke(args.migration_domain, args.peer)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
