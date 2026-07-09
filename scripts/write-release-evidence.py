#!/usr/bin/env python3
"""Write a release evidence manifest for CI artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from subvirt_versions import DEFAULT_MANIFEST, load_manifest
from alma_targets import DEFAULT_TARGETS as ALMA_TARGETS
from ubuntu_targets import DEFAULT_TARGETS


PACKAGE_SUFFIXES = {".deb", ".rpm", ".dsc", ".changes", ".buildinfo"}


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def run_metadata(argv: list[str]) -> str | None:
    try:
        result = subprocess.run(argv, check=True, text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except (FileNotFoundError, subprocess.CalledProcessError):
        return None
    return result.stdout.strip()


def deb_metadata(path: Path) -> dict[str, str]:
    if shutil.which("dpkg-deb") is None:
        return {}
    output = run_metadata(["dpkg-deb", "-f", str(path), "Package", "Version", "Architecture"])
    if not output:
        return {}
    fields = {}
    for line in output.splitlines():
        if ": " in line:
            key, value = line.split(": ", 1)
            fields[key] = value
    return {
        "name": fields["Package"],
        "version": fields["Version"],
        "architecture": fields["Architecture"],
    }


def rpm_metadata(path: Path) -> dict[str, str]:
    if shutil.which("rpm") is None:
        return {}
    output = run_metadata([
        "rpm",
        "-qp",
        "--qf",
        "%{NAME}\n%{VERSION}\n%{RELEASE}\n%{ARCH}\n",
        str(path),
    ])
    if not output:
        return {}
    values = output.splitlines()
    keys = ["name", "version", "release", "architecture"]
    return {key: value for key, value in zip(keys, values) if value}


def package_metadata(path: Path) -> dict[str, str]:
    if path.suffix == ".deb":
        metadata = deb_metadata(path)
        if metadata:
            metadata["format"] = "deb"
        return metadata
    if path.suffix == ".rpm":
        metadata = rpm_metadata(path)
        if metadata:
            metadata["format"] = "rpm"
        return metadata
    return {"format": path.suffix.removeprefix(".")}


def package_entry(path: Path) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "path": path.as_posix(),
        "size": path.stat().st_size,
        "sha256": sha256(path),
    }
    entry.update(package_metadata(path))
    return entry


def runtime_targets(root: Path) -> dict[str, list[str]]:
    log = root / "candidate-release.log"
    if not log.is_file():
        return {"storage": [], "migration": []}
    text = log.read_text(encoding="utf-8", errors="replace")
    return {
        "storage": sorted(set(re.findall(r"Storage target ([A-Za-z0-9_.-]+) passed", text))),
        "migration": sorted(set(re.findall(r"Migration target ([A-Za-z0-9_.-]+) passed", text))),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--build-id", required=True)
    parser.add_argument("--artifact-root", default="artifacts")
    parser.add_argument("--output", default="")
    parser.add_argument("--version-manifest", default=DEFAULT_MANIFEST, type=Path)
    args = parser.parse_args()
    root = Path(args.artifact_root) / args.build_id
    version_manifest = load_manifest(args.version_manifest)
    packages = []
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.suffix in PACKAGE_SUFFIXES:
            packages.append(package_entry(path))
    manifest = {
        "build_id": args.build_id,
        "artifact_root": root.as_posix(),
        "subvirt_version": version_manifest["subvirt_version"],
        "provider_version": f"{version_manifest['provider']['version']}-{version_manifest['provider']['release']}",
        "ubuntu_targets": [target.__dict__ for target in DEFAULT_TARGETS],
        "alma_targets": [target.__dict__ for target in ALMA_TARGETS],
        "runtime_targets": runtime_targets(root),
        "packages": packages,
    }
    output = Path(args.output) if args.output else root / "release-evidence.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
