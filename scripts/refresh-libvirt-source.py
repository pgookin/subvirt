#!/usr/bin/env python3
"""Refresh generated libvirt build sources from distro source packages.

This script intentionally writes only generated workspace files under build/ and
sources/. Those paths are ignored and must not be committed.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "build"
SOURCES = ROOT / "sources"
PATCHES = ROOT / "patches" / "libvirt"


def run(argv: list[str], cwd: Path | None = None) -> None:
    print("+ " + " ".join(argv), flush=True)
    subprocess.run(argv, cwd=cwd, check=True)


def ensure_tool(name: str) -> None:
    if shutil.which(name) is None:
        raise SystemExit(f"{name} is required")


def prepend_debian_changelog(src: Path, base_version: str) -> None:
    changelog = src / "debian" / "changelog"
    old = changelog.read_text(encoding="utf-8")
    version = base_version if base_version.endswith("+truenas1") else f"{base_version}+truenas1"
    entry = f"""libvirt ({version}) noble; urgency=medium

  * Local build: add TrueNAS provider-backed storage pool backend.
  * Add libvirt-daemon-driver-storage-truenas binary package.

 -- subvirt automation <release@subvirt.local>  Tue, 30 Jun 2026 00:00:00 -0400

"""
    if old.startswith(f"libvirt ({version})"):
        return
    changelog.write_text(entry + old, encoding="utf-8")


def refresh_ubuntu(version: str) -> None:
    ensure_tool("apt-get")
    ensure_tool("dpkg-source")
    ensure_tool("patch")
    out = SOURCES / "u24"
    out.mkdir(parents=True, exist_ok=True)
    run(["apt-get", "source", "--download-only", f"libvirt={version}"], cwd=out)
    dscs = sorted(out.glob(f"libvirt_{version}.dsc")) or sorted(out.glob("libvirt_*.dsc"))
    if not dscs:
        raise SystemExit(f"no libvirt .dsc found in {out}")
    dsc = dscs[-1]
    generated = BUILD / f"libvirt-u24-{version.split('-')[0]}"
    if generated.exists():
        shutil.rmtree(generated)
    BUILD.mkdir(exist_ok=True)
    run(["dpkg-source", "-x", str(dsc), str(generated)])
    run(["patch", "-p1", "-i", str(PATCHES / "truenas-storage-backend-u24.patch")], cwd=generated)
    prepend_debian_changelog(generated, version)
    print(f"Ubuntu source ready: {generated}")


def rpm_source_path(version: str) -> Path:
    candidates = sorted((SOURCES / "al10").glob(f"libvirt-{version}.src.rpm"))
    candidates += sorted((SOURCES / "al10").glob("libvirt-*.src.rpm"))
    if candidates:
        return candidates[-1]
    ensure_tool("dnf")
    out = SOURCES / "al10"
    out.mkdir(parents=True, exist_ok=True)
    run(["dnf", "download", "--source", "--destdir", str(out), f"libvirt-{version}"])
    candidates = sorted(out.glob(f"libvirt-{version}.src.rpm")) or sorted(out.glob("libvirt-*.src.rpm"))
    if not candidates:
        raise SystemExit(f"no libvirt source RPM found in {out}")
    return candidates[-1]


def spec_set_truenas_release(spec: Path, version: str) -> None:
    text = spec.read_text(encoding="utf-8")
    wanted = version.split("-", 1)[1]
    wanted = wanted if wanted.endswith(".truenas1") else f"{wanted}.truenas1"
    text = re.sub(r"^Release:\s*.*$", f"Release: {wanted}", text, count=1, flags=re.M)
    spec.write_text(text, encoding="utf-8")


def refresh_alma(version: str) -> None:
    ensure_tool("rpm2cpio")
    ensure_tool("cpio")
    ensure_tool("patch")
    src_rpm = rpm_source_path(version)
    BUILD.mkdir(exist_ok=True)
    for old in BUILD.glob("libvirt*.patch"):
        old.unlink()
    for old in BUILD.glob("libvirt*.tar.*"):
        old.unlink()
    if (BUILD / "libvirt.spec").exists():
        (BUILD / "libvirt.spec").unlink()
    run(["bash", "-lc", f"rpm2cpio {src_rpm} | cpio -idmv"], cwd=BUILD)
    spec = BUILD / "libvirt.spec"
    if not spec.exists():
        raise SystemExit("source RPM did not contain libvirt.spec")
    run(["patch", "-p1", "-i", str(PATCHES / "truenas-storage-backend-al10.patch")], cwd=BUILD)
    spec_set_truenas_release(spec, version)
    print(f"Alma source ready from {src_rpm}: {BUILD}")


def main(argv: list[str] = sys.argv[1:]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--distro", choices=["ubuntu", "alma"], required=True)
    parser.add_argument("--version", required=True, help="distro package EVR without local +truenas/.truenas suffix")
    args = parser.parse_args(argv)
    if args.distro == "ubuntu":
        refresh_ubuntu(args.version)
    else:
        refresh_alma(args.version)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
