#!/usr/bin/env python3
"""Deterministic release evidence gate for Subvirt candidates."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

from subvirt_versions import DEFAULT_MANIFEST, load_manifest, provider_version

PROVIDER_NAME = "truenas-libvirt-provider"
FULL_UBUNTU_PACKAGES = {
    "truenas-libvirt-provider",
    "libvirt0",
    "libvirt-daemon",
    "libvirt-daemon-driver-storage-truenas",
    "virt-manager",
    "virtinst",
}
FULL_ALMA_PACKAGES = {
    "truenas-libvirt-provider",
    "libvirt-libs",
    "libvirt-daemon-driver-storage-truenas",
    "virt-manager",
    "virt-manager-common",
    "virt-install",
}
REQUIRED_LOG_PATTERNS = {
    "pool_define": r"pool-define .*(iscsi|nvmeof)-pool\.xml",
    "pool_start": r"Pool truenas-(iscsi|nvmeof) started",
    "iscsi_create": r"Vol ci-.*-iscsi created",
    "nvmeof_create": r"Vol ci-.*-nvmeof created",
    "peer_check": r"test-storage\.py --action check-peer",
    "delete_check": r"test-storage\.py --action delete-check",
    "iscsi_delete": r"Vol ci-.*-iscsi deleted",
    "nvmeof_delete": r"Vol ci-.*-nvmeof deleted",
    "cleanup": r"persistent TrueNAS VM .*keeping lab networks|Candidate workflow completed",
}


def fail(message: str) -> None:
    raise SystemExit(f"release evidence gate failed: {message}")


def load_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        fail(f"missing {path}")
    except json.JSONDecodeError as exc:
        fail(f"invalid JSON in {path}: {exc}")


def package_lists(evidence: dict[str, Any]) -> tuple[list[dict[str, Any]], set[str], set[str]]:
    packages = evidence.get("packages")
    if not isinstance(packages, list) or not packages:
        fail("release-evidence.json contains no packages")
    deb_names = {str(pkg.get("name")) for pkg in packages if pkg.get("format") == "deb"}
    rpm_names = {str(pkg.get("name")) for pkg in packages if pkg.get("format") == "rpm" and str(pkg.get("architecture")) != "src"}
    return packages, deb_names, rpm_names


def require_file(path: Path, description: str) -> None:
    if not path.is_file():
        fail(f"missing {description}: {path}")
    if path.stat().st_size <= 0:
        fail(f"empty {description}: {path}")


def require_package_paths(root: Path, packages: list[dict[str, Any]]) -> None:
    for pkg in packages:
        rel = pkg.get("path")
        if not isinstance(rel, str) or not rel:
            fail("package entry missing path")
        path = Path(rel)
        if not path.is_absolute():
            path = root.parent.parent / path
        require_file(path, f"package artifact {rel}")
        if not isinstance(pkg.get("sha256"), str) or len(str(pkg.get("sha256"))) != 64:
            fail(f"package {rel} missing sha256")
        if not isinstance(pkg.get("size"), int) or int(pkg.get("size")) <= 0:
            fail(f"package {rel} has invalid size")


def infer_scope(packages: list[dict[str, Any]]) -> str:
    names = {str(pkg.get("name")) for pkg in packages}
    non_provider = {name for name in names if name and name != PROVIDER_NAME and name != "None"}
    return "full" if non_provider else "provider"


def require_provider_versions(packages: list[dict[str, Any]], manifest: dict[str, Any]) -> None:
    expected_deb_version = provider_version(manifest)
    expected_rpm_version = str(manifest["provider"]["version"])
    expected_rpm_release = str(manifest["provider"]["release"])
    provider_debs = [pkg for pkg in packages if pkg.get("format") == "deb" and pkg.get("name") == PROVIDER_NAME]
    provider_rpms = [pkg for pkg in packages if pkg.get("format") == "rpm" and pkg.get("name") == PROVIDER_NAME and pkg.get("architecture") != "src"]
    if not provider_debs:
        fail("missing Ubuntu provider deb")
    if not provider_rpms:
        fail("missing Alma provider rpm")
    for pkg in provider_debs:
        if pkg.get("version") != expected_deb_version:
            fail(f"provider deb version {pkg.get('version')} != {expected_deb_version}")
    for pkg in provider_rpms:
        if pkg.get("version") != expected_rpm_version:
            fail(f"provider rpm version {pkg.get('version')} != {expected_rpm_version}")
        release = str(pkg.get("release", ""))
        if not release.startswith(expected_rpm_release + ".") and release != expected_rpm_release:
            fail(f"provider rpm release {release} does not start with {expected_rpm_release}")


def require_scope_packages(scope: str, deb_names: set[str], rpm_names: set[str]) -> None:
    if scope == "provider":
        if deb_names != {PROVIDER_NAME}:
            fail(f"provider build has unexpected deb packages: {sorted(deb_names)}")
        if rpm_names != {PROVIDER_NAME}:
            fail(f"provider build has unexpected rpm packages: {sorted(rpm_names)}")
        return
    missing_deb = FULL_UBUNTU_PACKAGES - deb_names
    missing_rpm = FULL_ALMA_PACKAGES - rpm_names
    if missing_deb:
        fail(f"full build missing Ubuntu packages: {sorted(missing_deb)}")
    if missing_rpm:
        fail(f"full build missing Alma packages: {sorted(missing_rpm)}")


def require_log(root: Path) -> str:
    log_path = root / "candidate-release.log"
    require_file(log_path, "candidate release log")
    text = log_path.read_text(encoding="utf-8", errors="replace")
    for name, pattern in REQUIRED_LOG_PATTERNS.items():
        if not re.search(pattern, text):
            fail(f"candidate-release.log missing marker {name}")
    if "Ephemeral lab preserved for failed build" in text:
        fail("candidate-release.log shows failed lab was preserved")
    if "Traceback (most recent call last)" in text:
        fail("candidate-release.log contains a Python traceback")
    if "returned non-zero exit status" in text:
        fail("candidate-release.log contains a failed subprocess")
    return text


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--build-id", required=True)
    parser.add_argument("--artifact-root", default="artifacts")
    parser.add_argument("--version-manifest", default=DEFAULT_MANIFEST, type=Path)
    parser.add_argument("--scope", choices=["auto", "provider", "full"], default="auto")
    args = parser.parse_args()

    root = Path(args.artifact_root) / args.build_id
    evidence_path = root / "release-evidence.json"
    evidence = load_json(evidence_path)
    manifest = load_manifest(args.version_manifest)

    if evidence.get("build_id") != args.build_id:
        fail(f"evidence build_id {evidence.get('build_id')} != {args.build_id}")
    expected_provider = provider_version(manifest)
    if evidence.get("provider_version") != expected_provider:
        fail(f"evidence provider_version {evidence.get('provider_version')} != {expected_provider}")
    if evidence.get("subvirt_version") != manifest["subvirt_version"]:
        fail(f"evidence subvirt_version {evidence.get('subvirt_version')} != {manifest['subvirt_version']}")

    packages, deb_names, rpm_names = package_lists(evidence)
    require_package_paths(root, packages)
    scope = infer_scope(packages) if args.scope == "auto" else args.scope
    require_provider_versions(packages, manifest)
    require_scope_packages(scope, deb_names, rpm_names)
    require_log(root)

    print(f"release evidence OK: build_id={args.build_id} scope={scope} packages={len(packages)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
