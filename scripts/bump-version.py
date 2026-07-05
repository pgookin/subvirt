#!/usr/bin/env python3
"""Update release/subvirt-version.json explicitly."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from subvirt_versions import DEFAULT_MANIFEST, validate_manifest


def set_if(value, target: dict, key: str) -> None:
    if value is not None:
        target[key] = value


def set_rev(value: int | None, data: dict, distro: str, package: str) -> None:
    if value is not None:
        data[distro][package]["local_revision"] = value


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", default=DEFAULT_MANIFEST, type=Path)
    parser.add_argument("--subvirt-version")
    parser.add_argument("--provider-version")
    parser.add_argument("--provider-release", type=int)
    parser.add_argument("--ubuntu-libvirt-revision", type=int)
    parser.add_argument("--alma-libvirt-revision", type=int)
    parser.add_argument("--ubuntu-virt-manager-revision", type=int)
    parser.add_argument("--alma-virt-manager-revision", type=int)
    args = parser.parse_args()

    data = json.loads(args.manifest.read_text(encoding="utf-8"))
    set_if(args.subvirt_version, data, "subvirt_version")
    set_if(args.provider_version, data["provider"], "version")
    set_if(args.provider_release, data["provider"], "release")
    set_rev(args.ubuntu_libvirt_revision, data, "ubuntu_24_04", "libvirt")
    set_rev(args.alma_libvirt_revision, data, "almalinux_10", "libvirt")
    set_rev(args.ubuntu_virt_manager_revision, data, "ubuntu_24_04", "virt_manager")
    set_rev(args.alma_virt_manager_revision, data, "almalinux_10", "virt_manager")
    validate_manifest(data)
    args.manifest.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
