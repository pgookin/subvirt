#!/usr/bin/env python3
"""Read Subvirt package version metadata from release/subvirt-version.json."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

from alma_targets import alma_lock_key, alma_target
from ubuntu_targets import ubuntu_lock_key, ubuntu_target

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "release" / "subvirt-version.json"
SEMVER_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?$")


class VersionError(ValueError):
    pass


def load_manifest(path: Path = DEFAULT_MANIFEST) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    validate_manifest(data)
    return data


def _positive_int(value: Any, name: str) -> int:
    if not isinstance(value, int) or value < 1:
        raise VersionError(f"{name} must be a positive integer")
    return value


def _semver(value: Any, name: str) -> str:
    if not isinstance(value, str) or not SEMVER_RE.match(value):
        raise VersionError(f"{name} must be a SemVer string")
    return value


def validate_manifest(data: dict[str, Any]) -> None:
    if data.get("schema_version") != 1:
        raise VersionError("schema_version must be 1")
    _semver(data.get("subvirt_version"), "subvirt_version")
    provider = data.get("provider")
    if not isinstance(provider, dict):
        raise VersionError("provider must be an object")
    _semver(provider.get("version"), "provider.version")
    _positive_int(provider.get("release"), "provider.release")
    for distro in ("ubuntu_18_04", "ubuntu_20_04", "ubuntu_22_04", "ubuntu_24_04", "ubuntu_26_04", "almalinux_9", "almalinux_10"):
        section = data.get(distro)
        if not isinstance(section, dict):
            raise VersionError(f"{distro} must be an object")
        for package in ("libvirt", "virt_manager"):
            item = section.get(package)
            if not isinstance(item, dict):
                raise VersionError(f"{distro}.{package} must be an object")
            _positive_int(item.get("local_revision"), f"{distro}.{package}.local_revision")


def provider_version(data: dict[str, Any]) -> str:
    provider = data["provider"]
    return f"{provider['version']}-{provider['release']}"


def provider_rpm_version(data: dict[str, Any]) -> str:
    return str(data["provider"]["version"])


def provider_rpm_release(data: dict[str, Any]) -> str:
    return str(data["provider"]["release"])


def ubuntu_manifest_key(target_id: str | None = None, suite: str | None = None) -> str:
    return ubuntu_lock_key(ubuntu_target(None, target_id=target_id, suite=suite))


def ubuntu_libvirt_version(base_version: str, data: dict[str, Any], target_id: str | None = None, suite: str | None = None) -> str:
    key = ubuntu_manifest_key(target_id, suite)
    suffix = f"+truenas{data[key]['libvirt']['local_revision']}"
    return base_version if base_version.endswith(suffix) else f"{base_version}{suffix}"


def alma_manifest_key(target_id: str | None = None, version: str | None = None) -> str:
    return alma_lock_key(alma_target(None, target_id=target_id, version=version))


def alma_libvirt_release(parent_evr: str, data: dict[str, Any], target_id: str | None = None, version: str | None = None) -> str:
    if "-" not in parent_evr:
        raise VersionError("Alma libvirt parent version must include release")
    release = parent_evr.split("-", 1)[1]
    key = alma_manifest_key(target_id, version)
    suffix = f".truenas{data[key]['libvirt']['local_revision']}"
    return release if release.endswith(suffix) else f"{release}{suffix}"


def ubuntu_virt_manager_revision(data: dict[str, Any], target_id: str | None = None, suite: str | None = None) -> int:
    return int(data[ubuntu_manifest_key(target_id, suite)]["virt_manager"]["local_revision"])


def alma_virt_manager_revision(data: dict[str, Any], target_id: str | None = None, version: str | None = None) -> int:
    return int(data[alma_manifest_key(target_id, version)]["virt_manager"]["local_revision"])


def append_rpm_truenas_release(release: str, revision: int) -> str:
    suffix = f".truenas{revision}"
    if suffix in release:
        return release
    if "%{?dist}" in release:
        return release.replace("%{?dist}", f"{suffix}%{{?dist}}", 1)
    return release + suffix


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=[
        "validate",
        "subvirt-version",
        "provider-deb-version",
        "provider-rpm-version",
        "provider-rpm-release",
        "ubuntu-libvirt-version",
        "ubuntu-libvirt-version-for",
        "alma-libvirt-release",
        "alma-libvirt-release-for",
        "ubuntu-virt-manager-revision",
        "ubuntu-virt-manager-revision-for",
        "alma-virt-manager-revision",
        "alma-virt-manager-revision-for",
        "alma-virt-manager-release",
    ])
    parser.add_argument("value", nargs="?", help="parent package version/release for commands that need one")
    parser.add_argument("target", nargs="?", help="Ubuntu target id or suite for target-specific commands")
    parser.add_argument("--manifest", default=DEFAULT_MANIFEST, type=Path)
    args = parser.parse_args()
    data = load_manifest(args.manifest)
    if args.command == "validate":
        return 0
    if args.command == "subvirt-version":
        print(data["subvirt_version"])
    elif args.command == "provider-deb-version":
        print(provider_version(data))
    elif args.command == "provider-rpm-version":
        print(provider_rpm_version(data))
    elif args.command == "provider-rpm-release":
        print(provider_rpm_release(data))
    elif args.command == "ubuntu-libvirt-version":
        if not args.value:
            raise SystemExit("ubuntu-libvirt-version requires a parent version")
        print(ubuntu_libvirt_version(args.value, data))
    elif args.command == "ubuntu-libvirt-version-for":
        if not args.value or not args.target:
            raise SystemExit("ubuntu-libvirt-version-for requires a parent version and target")
        target_id = args.target if args.target.startswith("ubuntu-") else None
        suite = None if args.target.startswith("ubuntu-") else args.target
        print(ubuntu_libvirt_version(args.value, data, target_id=target_id, suite=suite))
    elif args.command == "alma-libvirt-release":
        if not args.value:
            raise SystemExit("alma-libvirt-release requires a parent EVR")
        print(alma_libvirt_release(args.value, data))
    elif args.command == "alma-libvirt-release-for":
        if not args.value or not args.target:
            raise SystemExit("alma-libvirt-release-for requires a parent EVR and target")
        target_id = args.target if args.target.startswith("almalinux-") else None
        version = None if args.target.startswith("almalinux-") else args.target
        print(alma_libvirt_release(args.value, data, target_id=target_id, version=version))
    elif args.command == "ubuntu-virt-manager-revision":
        print(ubuntu_virt_manager_revision(data))
    elif args.command == "ubuntu-virt-manager-revision-for":
        if not args.value:
            raise SystemExit("ubuntu-virt-manager-revision-for requires a target")
        target_id = args.value if args.value.startswith("ubuntu-") else None
        suite = None if args.value.startswith("ubuntu-") else args.value
        print(ubuntu_virt_manager_revision(data, target_id=target_id, suite=suite))
    elif args.command == "alma-virt-manager-revision":
        print(alma_virt_manager_revision(data))
    elif args.command == "alma-virt-manager-revision-for":
        if not args.value:
            raise SystemExit("alma-virt-manager-revision-for requires a target")
        target_id = args.value if args.value.startswith("almalinux-") else None
        version = None if args.value.startswith("almalinux-") else args.value
        print(alma_virt_manager_revision(data, target_id=target_id, version=version))
    elif args.command == "alma-virt-manager-release":
        if not args.value:
            raise SystemExit("alma-virt-manager-release requires a parent Release value")
        print(append_rpm_truenas_release(args.value, alma_virt_manager_revision(data)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
