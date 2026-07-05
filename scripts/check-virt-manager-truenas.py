#!/usr/bin/env python3
"""Check that virt-manager has complete TrueNAS storage pool support."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--source-root",
        default="/usr/share/virt-manager",
        help="directory containing virtManager/ and virtinst/ packages",
    )
    parser.add_argument(
        "--static",
        action="store_true",
        help="inspect source text instead of importing virt-manager",
    )
    return parser.parse_args()


def static_checks(source_root: Path) -> list[tuple[bool, str]]:
    storagepool = (source_root / "virtManager" / "object" / "storagepool.py").read_text(encoding="utf-8")
    storage = (source_root / "virtinst" / "storage.py").read_text(encoding="utf-8")
    createpool = (source_root / "virtManager" / "createpool.py").read_text(encoding="utf-8")
    ui = (source_root / "ui" / "createpool.ui").read_text(encoding="utf-8")
    return [
        ('"truenas": _("TrueNAS managed storage")' in storagepool,
         "truenas pools must have a friendly label"),
        ('"truenas"' in storagepool and 'supports_volume_creation' in storagepool,
         "truenas pools must be in virt-manager volume creation support"),
        ('TYPE_TRUENAS = "truenas"' in storage,
         "virtinst must define StoragePool.TYPE_TRUENAS"),
        ('source_protocol = XMLProperty("./source/protocol/@type")' in storage,
         "virtinst must emit source/protocol type XML"),
        ('_DEFAULT_TRUENAS_TARGET = "/dev/disk/by-id"' in storage,
         "truenas pools must default to /dev/disk/by-id"),
        ('TrueNAS source pool name is required.' in storage,
         "truenas validation must require a TrueNAS source pool"),
        ('TrueNAS transport must be iSCSI or NVMe-oF.' in storage,
         "truenas validation must require an iSCSI or NVMe-oF transport"),
        ('pool-truenas-transport' in createpool,
         "create pool wizard must populate a TrueNAS transport control"),
        ('pool.source_protocol = transport' in createpool,
         "create pool wizard must write the selected TrueNAS transport to XML"),
        ('TrueNAS _Pool:' in createpool,
         "create pool wizard must label source name as TrueNAS pool"),
        ('pooltype == StoragePool.TYPE_TRUENAS' in createpool and '_list_pool_sources(pooltype)' in createpool,
         "create pool wizard must discover TrueNAS source pools"),
        ('id="pool-truenas-transport"' in ui,
         "create pool UI must contain the TrueNAS transport combo box"),
    ]


def dynamic_checks(source_root: Path) -> list[tuple[bool, str]]:
    sys.path.insert(0, str(source_root))
    from virtManager.object.storagepool import vmmStoragePool
    from virtinst import StoragePool

    pool = StoragePool(None)
    pool.type = StoragePool.TYPE_TRUENAS
    pool.name = "subvirt-test"
    pool.source_name = "tank"
    pool.source_protocol = "nvmeof"
    pool.validate_name = lambda *_args, **_kwargs: None
    pool.validate()
    xml = pool.get_xml()

    return [
        (vmmStoragePool.supports_volume_creation("truenas") is True,
         "truenas pools must support volume creation"),
        (vmmStoragePool.supports_volume_creation("truenas", clone=True) is False,
         "truenas pools must not advertise clone support"),
        (vmmStoragePool.pretty_type("truenas") == "TrueNAS managed storage",
         "truenas pools must have a friendly label"),
        (pool.supports_source_name() is True,
         "truenas pools must expose source/name"),
        (pool.supports_target_path() is True,
         "truenas pools must expose target/path"),
        (pool.default_target_path() == "/dev/disk/by-id",
         "truenas pools must default to /dev/disk/by-id"),
        ("<protocol type=\"nvmeof\"/>" in xml or "<protocol type='nvmeof'/>" in xml,
         "truenas pool XML must include source/protocol type"),
    ]


def main() -> int:
    args = parse_args()
    source_root = Path(args.source_root)
    checks = static_checks(source_root) if args.static else dynamic_checks(source_root)
    failed = [message for ok, message in checks if not ok]
    if failed:
        for message in failed:
            print(message, file=sys.stderr)
        return 1

    print("virt-manager truenas pool support OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
