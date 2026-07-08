#!/usr/bin/env python3
"""Write deterministic upstream source refresh manifests."""

from __future__ import annotations

import argparse
import hashlib
import json
import urllib.parse
from pathlib import Path
from typing import Any

from alma_targets import AlmaTarget, alma_target
from subvirt_versions import alma_libvirt_release, load_manifest, ubuntu_libvirt_version
from ubuntu_targets import UbuntuTarget, ubuntu_target


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


def ubuntu_source_files(version: str, target: UbuntuTarget) -> list[dict[str, Any]]:
    dsc = ROOT / "sources" / target.source_dir / f"libvirt_{version}.dsc"
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


def alma_source_files(version: str, target: AlmaTarget) -> list[dict[str, Any]]:
    src_rpm = ROOT / "sources" / target.source_dir / f"libvirt-{version}.src.rpm"
    if not src_rpm.exists():
        raise SystemExit(f"missing refreshed AlmaLinux source RPM: {src_rpm}")
    return [file_entry(src_rpm)]


def patch_files(distro: str, target: UbuntuTarget | AlmaTarget | None = None) -> list[dict[str, Any]]:
    patch_name = target.patch if target is not None else {
        "alma": "truenas-storage-backend-al10.patch",
    }[distro]
    path = ROOT / "patches" / "libvirt" / patch_name
    if not path.exists():
        raise SystemExit(f"missing tracked patch overlay: {path}")
    return [file_entry(path)]


def generated_outputs(distro: str, version: str, target: UbuntuTarget | AlmaTarget | None = None) -> list[dict[str, Any]]:
    if isinstance(target, UbuntuTarget):
        src = ROOT / "build" / f"{target.build_dir_prefix}-{version.split('-', 1)[0]}"
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


def local_version(distro: str, version: str, target: UbuntuTarget | AlmaTarget | None = None) -> str:
    manifest = load_manifest()
    if isinstance(target, UbuntuTarget):
        return ubuntu_libvirt_version(version, manifest, target_id=target.id)
    parent_version = version.split("-", 1)[0]
    if isinstance(target, AlmaTarget):
        return f"{parent_version}-{alma_libvirt_release(version, manifest, target_id=target.id)}"
    return f"{parent_version}-{alma_libvirt_release(version, manifest)}"


def changed_rows(report: dict[str, Any], changed_only: bool) -> list[dict[str, Any]]:
    rows = []
    for row in report.get("packages", []):
        distro = str(row.get("distro"))
        if distro != "alma" and not distro.startswith("ubuntu_") and not distro.startswith("almalinux_"):
            continue
        if changed_only and not row.get("update_available"):
            continue
        rows.append(row)
    return rows


def write_manifest(row: dict[str, Any], output_dir: Path) -> Path:
    distro = str(row["distro"])
    version = str(row["current_version"])
    package = str(row["package"])
    target = None
    if distro.startswith("ubuntu_"):
        target = ubuntu_target(None, target_id=distro.replace("_", "-", 1).replace("_", "."))
        sources = ubuntu_source_files(version, target)
    elif distro.startswith("almalinux_"):
        target = alma_target(None, target_id=distro.replace("_", "-", 1))
        sources = alma_source_files(version, target)
    else:
        target = alma_target(None, target_id="almalinux-10")
        sources = alma_source_files(version, target)
    manifest = {
        "schema_version": 1,
        "distro": distro,
        "suite": target.suite if isinstance(target, UbuntuTarget) else getattr(target, "version", ""),
        "support_tier": target.support_tier if isinstance(target, UbuntuTarget) else "standard",
        "package": package,
        "upstream_version": version,
        "local_version": local_version(distro, version, target),
        "source_metadata": sanitize_source_metadata(str(row.get("source", ""))),
        "source_files": sources,
        "patches": patch_files(distro, target),
        "generated_checks": generated_outputs(distro, version, target),
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
