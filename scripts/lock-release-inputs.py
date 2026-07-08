#!/usr/bin/env python3
"""Derive release workflow inputs from release/upstream-lock.json."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

from alma_targets import alma_lock_key, alma_targets
from ubuntu_targets import ubuntu_lock_key, ubuntu_targets


def sanitize(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-")


def changed_targets(report_path: Path | None, config: dict[str, Any], lock: dict[str, Any] | None = None) -> tuple[list[str], list[str]]:
    targets = [target.id for target in ubuntu_targets(config)]
    if report_path is None:
        locked = lock or {}
        selected = [target.id for target in ubuntu_targets(config) if str(locked.get(ubuntu_lock_key(target), {}).get("version", ""))]
        alma_selected = [target.id for target in alma_targets(config) if str(locked.get(alma_lock_key(target), {}).get("version", ""))]
        return selected, alma_selected
    report: dict[str, Any] = json.loads(report_path.read_text(encoding="utf-8"))
    ubuntu: list[str] = []
    alma: list[str] = []
    for package in report.get("packages", []):
        if not package.get("update_available"):
            continue
        distro = str(package.get("distro"))
        if distro.startswith("ubuntu_"):
            key = distro
            for target in ubuntu_targets(config):
                if ubuntu_lock_key(target) == key:
                    ubuntu.append(target.id)
                    break
        elif distro.startswith("almalinux_"):
            key = distro
            for target in alma_targets(config):
                if alma_lock_key(target) == key:
                    alma.append(target.id)
                    break
        elif distro == "alma":
            alma.append("almalinux-10")
    return ubuntu, alma


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lock", default="release/upstream-lock.json", type=Path)
    parser.add_argument("--report", type=Path, help="upstream check report used to gate distro builds")
    parser.add_argument("--config", default="release/release.example.json", type=Path)
    parser.add_argument("--github-output", default="")
    args = parser.parse_args()

    lock: dict[str, Any] = json.loads(args.lock.read_text(encoding="utf-8"))
    config: dict[str, Any] = json.loads(args.config.read_text(encoding="utf-8")) if args.config.exists() else {}
    ubuntu_target_ids, alma_target_ids = changed_targets(args.report, config, lock)
    build_ubuntu = bool(ubuntu_target_ids)
    build_alma = bool(alma_target_ids)
    ubuntu_versions = []
    for target in ubuntu_targets(config):
        key = ubuntu_lock_key(target)
        version = str(lock.get(key, {}).get("version", ""))
        if target.id in ubuntu_target_ids and version:
            ubuntu_versions.append(f"{target.id}={version}")
    alma_versions = []
    for target in alma_targets(config):
        key = alma_lock_key(target)
        version = str(lock.get(key, {}).get("version", ""))
        if target.id in alma_target_ids and version:
            alma_versions.append(f"{target.id}={version}")
    alma = str(lock.get("almalinux_10", lock.get("alma", {})).get("version", ""))
    changed_label = "-".join(sanitize(item.split("=", 1)[0].replace("ubuntu-", "u")) for item in ubuntu_versions) or "no-ubuntu"
    alma_label = "-".join(sanitize(item.split("=", 1)[0].replace("almalinux-", "al")) for item in alma_versions) or "no-alma"
    alma_version_label = "-".join(sanitize(item.split("=", 1)[1]) for item in alma_versions) or sanitize(alma)
    build_id = f"upstream-{changed_label}-{alma_label}-{alma_version_label}"
    outputs = {
        "ubuntu_versions": ",".join(ubuntu_versions),
        "ubuntu_targets": ",".join(ubuntu_target_ids),
        "alma_version": alma_versions[0].split("=", 1)[1] if len(alma_versions) == 1 else alma,
        "alma_versions": ",".join(alma_versions),
        "alma_targets": ",".join(alma_target_ids),
        "build_ubuntu": str(build_ubuntu).lower(),
        "build_alma": str(build_alma).lower(),
        "build_id": build_id,
        "ubuntu_version": ubuntu_versions[0].split("=", 1)[1] if len(ubuntu_versions) == 1 else "",
    }
    for key, value in outputs.items():
        print(f"{key}={value}")
    if args.github_output:
        with Path(args.github_output).open("a", encoding="utf-8") as handle:
            for key, value in outputs.items():
                handle.write(f"{key}={value}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
