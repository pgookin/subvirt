#!/usr/bin/env python3
"""Write deterministic upstream source refresh manifests."""

from __future__ import annotations

import argparse
import hashlib
import json
import urllib.parse
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_entry(path: Path) -> dict[str, Any]:
    rel = path.relative_to(ROOT).as_posix()
    return {
        "path": rel,
        "sha256": sha256(path),
        "size": path.stat().st_size,
    }


def ubuntu_source_files(version: str) -> list[dict[str, Any]]:
    dsc = ROOT / "sources" / "u24" / f"libvirt_{version}.dsc"
    if not dsc.exists():
        raise SystemExit(f"missing refreshed Ubuntu source descriptor: {dsc}")
    names = {dsc.name}
    for line in dsc.read_text(encoding="utf-8", errors="replace").splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[2].startswith("libvirt_"):
            names.add(parts[2])
    source_dir = dsc.parent
    files = []
    for name in sorted(names):
        path = source_dir / name
        if not path.exists():
            raise SystemExit(f"Ubuntu source descriptor references missing file: {path}")
        files.append(file_entry(path))
    return files


def alma_source_files(version: str) -> list[dict[str, Any]]:
    src_rpm = ROOT / "sources" / "al10" / f"libvirt-{version}.src.rpm"
    if not src_rpm.exists():
        raise SystemExit(f"missing refreshed AlmaLinux source RPM: {src_rpm}")
    return [file_entry(src_rpm)]


def patch_files(distro: str) -> list[dict[str, Any]]:
    patch_name = {
        "ubuntu": "truenas-storage-backend-u24.patch",
        "alma": "truenas-storage-backend-al10.patch",
    }[distro]
    path = ROOT / "patches" / "libvirt" / patch_name
    if not path.exists():
        raise SystemExit(f"missing tracked patch overlay: {path}")
    return [file_entry(path)]


def generated_outputs(distro: str, version: str) -> list[dict[str, Any]]:
    if distro == "ubuntu":
        src = ROOT / "build" / f"libvirt-u24-{version.split('-', 1)[0]}"
        expected = src / "debian" / "changelog"
    else:
        expected = ROOT / "build" / "libvirt.spec"
    if not expected.exists():
        raise SystemExit(f"refresh did not produce expected generated file: {expected}")
    return [file_entry(expected)]


def sanitize_source_metadata(value: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme and parsed.netloc:
        path = parsed.path.lstrip("/")
        if parsed.query:
            path = f"{path}?{parsed.query}"
        return f"configured-mirror:{path}"
    return value


def local_version(distro: str, version: str) -> str:
    if distro == "ubuntu":
        return version if version.endswith("+truenas1") else f"{version}+truenas1"
    release = version.split("-", 1)[1]
    return version if release.endswith(".truenas1") else f"{version}.truenas1"


def changed_rows(report: dict[str, Any], changed_only: bool) -> list[dict[str, Any]]:
    rows = []
    for row in report.get("packages", []):
        if row.get("distro") not in {"ubuntu", "alma"}:
            continue
        if changed_only and not row.get("update_available"):
            continue
        rows.append(row)
    return rows


def write_manifest(row: dict[str, Any], output_dir: Path) -> Path:
    distro = str(row["distro"])
    version = str(row["current_version"])
    package = str(row["package"])
    if distro == "ubuntu":
        sources = ubuntu_source_files(version)
    else:
        sources = alma_source_files(version)
    manifest = {
        "schema_version": 1,
        "distro": distro,
        "package": package,
        "upstream_version": version,
        "local_version": local_version(distro, version),
        "source_metadata": sanitize_source_metadata(str(row.get("source", ""))),
        "source_files": sources,
        "patches": patch_files(distro),
        "generated_checks": generated_outputs(distro, version),
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{distro}.json"
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--output-dir", default="release/upstream-manifests", type=Path)
    parser.add_argument("--changed-only", action="store_true")
    args = parser.parse_args()

    report = load_json(args.report)
    rows = changed_rows(report, args.changed_only)
    if not rows:
        raise SystemExit("report has no matching package rows for manifest generation")
    for row in rows:
        path = write_manifest(row, args.output_dir)
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
