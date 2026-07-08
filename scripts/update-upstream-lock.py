#!/usr/bin/env python3
"""Update release/upstream-lock.json from check-upstream.py JSON output."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from subvirt_versions import validate_manifest


def load(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def reset_libvirt_revision(version_manifest: Path, changed_distros: set[str]) -> None:
    if not changed_distros:
        return
    data = load(version_manifest)
    if not data:
        raise SystemExit(f"version manifest not found: {version_manifest}")
    for distro in sorted(changed_distros):
        key = "almalinux_10" if distro == "alma" else distro
        if key in data:
            data[key]["libvirt"]["local_revision"] = 1
    validate_manifest(data)
    version_manifest.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--lock", default="release/upstream-lock.json", type=Path)
    parser.add_argument("--version-manifest", default="release/subvirt-version.json", type=Path)
    args = parser.parse_args()

    report = load(args.report)
    lock = load(args.lock)
    changed_distros: set[str] = set()
    for row in report.get("packages", []):
        distro = row["distro"]
        current_version = row["current_version"]
        previous_version = str(lock.get(distro, {}).get("version", ""))
        if previous_version != current_version:
            changed_distros.add(distro)
        lock[distro] = {
            "package": row["package"],
            "version": current_version,
        }
    args.lock.write_text(json.dumps(lock, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    reset_libvirt_revision(args.version_manifest, changed_distros)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
