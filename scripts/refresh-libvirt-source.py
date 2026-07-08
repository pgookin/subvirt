#!/usr/bin/env python3
"""Refresh generated libvirt build sources from distro source packages.

This script intentionally writes only generated workspace files under build/ and
sources/. Those paths are ignored and must not be committed.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import lzma
import os
import re
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Any

from alma_targets import AlmaTarget, alma_target
from subvirt_versions import alma_libvirt_release, load_manifest, ubuntu_libvirt_version
from ubuntu_targets import UbuntuTarget, ubuntu_target

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



def has_tool(name: str) -> bool:
    return shutil.which(name) is not None


def shell_join(argv: list[str]) -> str:
    return " ".join(shlex_quote(part) for part in argv)


def shlex_quote(value: str) -> str:
    import shlex
    return shlex.quote(value)



def load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def upstream_config(config_path: Path) -> dict[str, Any]:
    return load_config(config_path).get("upstream", {})


def fetch_url(url: str) -> bytes:
    print(f"+ fetch {url}", flush=True)
    with urllib.request.urlopen(url, timeout=60) as response:
        return response.read()


def decode_metadata(url: str, data: bytes) -> str:
    if url.endswith(".xz"):
        data = lzma.decompress(data)
    elif url.endswith(".gz"):
        data = gzip.decompress(data)
    return data.decode("utf-8")


def parse_deb822(text: str) -> list[dict[str, str]]:
    paragraphs: list[dict[str, str]] = []
    fields: dict[str, str] = {}
    current_key: str | None = None
    for line in text.splitlines():
        if not line:
            if fields:
                paragraphs.append(fields)
                fields = {}
            current_key = None
            continue
        if line.startswith((" ", "\t")):
            if current_key is not None:
                fields[current_key] += "\n" + line
            continue
        key, sep, value = line.partition(":")
        if sep:
            current_key = key
            fields[key] = value.lstrip()
    if fields:
        paragraphs.append(fields)
    return paragraphs


def ubuntu_dist_names(upstream: dict[str, Any], target: UbuntuTarget) -> list[str]:
    suite = target.suite
    pockets = upstream.get("ubuntu_pockets", ["updates", "security"])
    dist_names: list[str] = []
    for pocket in pockets:
        pocket = str(pocket)
        if pocket in ("", "release", suite):
            dist = suite
        elif pocket.startswith(f"{suite}-"):
            dist = pocket
        else:
            dist = f"{suite}-{pocket}"
        if dist not in dist_names:
            dist_names.append(dist)
    if suite not in dist_names:
        dist_names.append(suite)
    return dist_names


def find_ubuntu_source(version: str, config_path: Path, target: UbuntuTarget) -> tuple[str, dict[str, str]]:
    upstream = upstream_config(config_path)
    mirrors = upstream.get("mirrors", {})
    mirror = str(mirrors.get(target.id, mirrors.get(target.suite, mirrors.get("ubuntu", "http://archive.ubuntu.com/ubuntu")))).rstrip("/")
    component = str(upstream.get("ubuntu_component", "main"))
    errors: list[str] = []
    for dist in ubuntu_dist_names(upstream, target):
        base = f"{mirror}/dists/{dist}/{component}/source/Sources"
        for suffix in (".xz", ".gz", ""):
            url = base + suffix
            try:
                paragraphs = parse_deb822(decode_metadata(url, fetch_url(url)))
            except Exception as exc:
                errors.append(f"{url}: {exc}")
                continue
            for paragraph in paragraphs:
                if paragraph.get("Package") == "libvirt" and paragraph.get("Version") == version:
                    return mirror, paragraph
    raise SystemExit("could not find Ubuntu libvirt source package "
                     f"{version} in configured mirror metadata:\n" + "\n".join(errors))


def source_files_from_paragraph(paragraph: dict[str, str]) -> list[dict[str, str]]:
    checksums = paragraph.get("Checksums-Sha256", "")
    files: list[dict[str, str]] = []
    for line in checksums.splitlines():
        parts = line.split()
        if len(parts) == 3:
            checksum, size, name = parts
            files.append({"sha256": checksum, "size": size, "name": name})
    if not files:
        raise SystemExit("Ubuntu source paragraph did not include Checksums-Sha256")
    return files


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download_ubuntu_source_files(version: str, config_path: Path, out: Path, target: UbuntuTarget) -> Path:
    mirror, paragraph = find_ubuntu_source(version, config_path, target)
    directory = paragraph.get("Directory")
    if not directory:
        raise SystemExit("Ubuntu source paragraph did not include Directory")
    dsc: Path | None = None
    for item in source_files_from_paragraph(paragraph):
        name = item["name"]
        dst = out / name
        if dst.exists() and sha256(dst) == item["sha256"]:
            print(f"+ keep {dst}", flush=True)
        else:
            data = fetch_url(f"{mirror}/{directory}/{name}")
            dst.write_bytes(data)
            actual = hashlib.sha256(data).hexdigest()
            if actual != item["sha256"]:
                dst.unlink(missing_ok=True)
                raise SystemExit(f"sha256 mismatch for {name}: expected {item['sha256']}, got {actual}")
        if name.endswith(".dsc"):
            dsc = dst
    if dsc is None:
        raise SystemExit("Ubuntu source metadata did not include a .dsc")
    return dsc


def prepend_debian_changelog(src: Path, base_version: str, target: UbuntuTarget) -> None:
    changelog = src / "debian" / "changelog"
    old = changelog.read_text(encoding="utf-8")
    version = ubuntu_libvirt_version(base_version, load_manifest(), target_id=target.id)
    entry = f"""libvirt ({version}) {target.suite}; urgency=medium

  * Local build: add TrueNAS provider-backed storage pool backend.
  * Add libvirt-daemon-driver-storage-truenas binary package.

 -- subvirt automation <release@subvirt.local>  Tue, 30 Jun 2026 00:00:00 -0400

"""
    if old.startswith(f"libvirt ({version})"):
        return
    changelog.write_text(entry + old, encoding="utf-8")



def add_ubuntu_truenas_package(src: Path, target: UbuntuTarget) -> None:
    debian = src / "debian"
    install_file = debian / "libvirt-daemon-driver-storage-truenas.install"
    install_path = "usr/lib/libvirt/storage-backend/libvirt_storage_backend_truenas.so"
    if target.id != "ubuntu-18.04":
        install_path = "usr/lib/*/libvirt/storage-backend/libvirt_storage_backend_truenas.so"
    install_file.write_text(f"{install_path}\n", encoding="utf-8")

    control = debian / "control"
    text = control.read_text(encoding="utf-8")
    if "Package: libvirt-daemon-driver-storage-truenas\n" in text:
        return
    stanza = """Package: libvirt-daemon-driver-storage-truenas
Section: admin
Architecture: linux-any
Multi-Arch: no
Depends:
 libvirt-daemon (= ${binary:Version}),
 libvirt0 (= ${binary:Version}),
 truenas-libvirt-provider,
 ${misc:Depends},
 ${shlibs:Depends},
Description: Virtualization daemon TrueNAS storage driver
 Libvirt is a C toolkit to interact with the virtualization capabilities
 of recent versions of Linux (and other OSes). The library aims at providing
 a long term stable C API for different virtualization mechanisms. It currently
 supports QEMU, KVM, XEN, OpenVZ, LXC, and VirtualBox.
 .
 This package contains the libvirtd storage driver for TrueNAS-backed storage.

"""
    marker = "Package: libvirt-daemon-system\n"
    if marker not in text:
        raise SystemExit("could not find libvirt-daemon-system stanza in debian/control")
    control.write_text(text.replace(marker, stanza + marker, 1), encoding="utf-8")


def refresh_ubuntu(version: str, config_path: Path, target_id: str | None = None) -> None:
    ensure_tool("dpkg-source")
    ensure_tool("patch")
    target = ubuntu_target(load_config(config_path), target_id=target_id)
    out = SOURCES / target.source_dir
    out.mkdir(parents=True, exist_ok=True)
    dsc = download_ubuntu_source_files(version, config_path, out, target)
    generated = BUILD / f"{target.build_dir_prefix}-{version.split('-')[0]}"
    if generated.exists():
        shutil.rmtree(generated)
    BUILD.mkdir(exist_ok=True)
    run(["dpkg-source", "-x", str(dsc), str(generated)])
    patch_path = PATCHES / target.patch
    if not patch_path.exists():
        raise SystemExit(f"missing Ubuntu patch for {target.id}: {patch_path}")
    run(["patch", "-p1", "-i", str(patch_path)], cwd=generated)
    add_ubuntu_truenas_package(generated, target)
    prepend_debian_changelog(generated, version, target)
    print(f"Ubuntu source ready for {target.id}: {generated}")


def rpm_source_path(version: str, target: AlmaTarget) -> Path:
    candidates = sorted((SOURCES / target.source_dir).glob(f"libvirt-{version}.src.rpm"))
    candidates += sorted((SOURCES / target.source_dir).glob("libvirt-*.src.rpm"))
    if candidates:
        return candidates[-1]
    ensure_tool("dnf")
    out = SOURCES / target.source_dir
    out.mkdir(parents=True, exist_ok=True)
    run(["dnf", "download", "--source", "--destdir", str(out), f"libvirt-{version}"])
    candidates = sorted(out.glob(f"libvirt-{version}.src.rpm")) or sorted(out.glob("libvirt-*.src.rpm"))
    if not candidates:
        raise SystemExit(f"no libvirt source RPM found in {out}")
    return candidates[-1]


def spec_set_truenas_release(spec: Path, version: str, target: AlmaTarget) -> None:
    text = spec.read_text(encoding="utf-8")
    wanted = alma_libvirt_release(version, load_manifest(), target_id=target.id)
    text = re.sub(r"^Release:\s*.*$", f"Release: {wanted}", text, count=1, flags=re.M)
    spec.write_text(text, encoding="utf-8")



def spec_add_patch(spec: Path, patch_name: str) -> None:
    text = spec.read_text(encoding="utf-8")
    if re.search(rf"^Patch\d+:\s+{re.escape(patch_name)}$", text, flags=re.M):
        return
    matches = list(re.finditer(r"^Patch(\d+):\s+.*$", text, flags=re.M))
    if not matches:
        raise SystemExit("libvirt.spec did not contain any Patch entries")
    last = matches[-1]
    patch_num = int(last.group(1)) + 1
    insertion = f"Patch{patch_num}: {patch_name}\n"
    text = text[:last.end() + 1] + insertion + text[last.end() + 1:]
    spec.write_text(text, encoding="utf-8")



def spec_add_truenas_storage_package(spec: Path) -> None:
    text = spec.read_text(encoding="utf-8")
    if "%package daemon-driver-storage-truenas\n" not in text:
        package_stanza = """
%package daemon-driver-storage-truenas
Summary: Storage driver plugin for TrueNAS
Requires: libvirt-daemon-driver-storage-core = %{version}-%{release}
Requires: libvirt-libs = %{version}-%{release}
Requires: truenas-libvirt-provider

%description daemon-driver-storage-truenas
The storage driver backend adding implementation of the storage APIs for
TrueNAS-backed storage.
"""
        marker = "%package daemon-driver-storage\n"
        if marker not in text:
            raise SystemExit("could not find daemon-driver-storage package marker in libvirt.spec")
        text = text.replace(marker, package_stanza + "\n" + marker, 1)
    require_line = "Requires: libvirt-daemon-driver-storage-truenas = %{version}-%{release}\n"
    if require_line not in text:
        marker = "Requires: libvirt-daemon-driver-storage-mpath = %{version}-%{release}\n"
        if marker not in text:
            raise SystemExit("could not find storage aggregate dependency marker in libvirt.spec")
        text = text.replace(marker, marker + require_line, 1)
    if "%files daemon-driver-storage-truenas\n" not in text:
        files_stanza = """
%files daemon-driver-storage-truenas
%{_libdir}/libvirt/storage-backend/libvirt_storage_backend_truenas.so
"""
        marker = "%files daemon-driver-storage-mpath\n"
        if marker not in text:
            raise SystemExit("could not find daemon-driver-storage-mpath files marker in libvirt.spec")
        text = text.replace(marker, files_stanza + "\n" + marker, 1)
    spec.write_text(text, encoding="utf-8")



def write_git_am_patch(src: Path, dst: Path, subject: str) -> None:
    diff = src.read_text(encoding="utf-8")
    if diff.startswith("From "):
        dst.write_text(diff, encoding="utf-8")
        return
    header = f"""From 0000000000000000000000000000000000000000 Mon Sep 17 00:00:00 2001
From: subvirt automation <release@subvirt.local>
Date: Tue, 30 Jun 2026 00:00:00 -0400
Subject: [PATCH] {subject}

---
"""
    dst.write_text(header + diff, encoding="utf-8")



def refresh_alma_in_container(version: str, target: AlmaTarget) -> None:
    runtime = os.environ.get("SUBVIRT_CONTAINER_RUNTIME", "podman")
    image = os.environ.get("SUBVIRT_ALMA_BUILD_IMAGE", target.image)
    containerfile = os.environ.get("SUBVIRT_ALMA_CONTAINERFILE", target.containerfile)
    ensure_tool(runtime)
    probe = subprocess.run([runtime, "image", "exists", image], check=False)
    if probe.returncode != 0:
        run([runtime, "build", "-t", image, "-f", containerfile, "."], cwd=ROOT)
    command = shell_join([
        "env",
        "SUBVIRT_REFRESH_IN_CONTAINER=1",
        "./scripts/refresh-libvirt-source.py",
        "--distro",
        "alma",
        "--version",
        version,
        "--alma-target",
        target.id,
    ])
    run([
        runtime,
        "run",
        "--rm",
        "--security-opt",
        "label=disable",
        "-v",
        f"{ROOT}:/work",
        "-w",
        "/work",
        image,
        "bash",
        "-lc",
        command,
    ], cwd=ROOT)


def refresh_alma(version: str, config_path: Path, target_id: str | None = None) -> None:
    target = alma_target(load_config(config_path), target_id=target_id)
    required = ["rpm2cpio", "cpio", "patch", "dnf"]
    if os.environ.get("SUBVIRT_REFRESH_IN_CONTAINER") != "1" and any(not has_tool(tool) for tool in required):
        refresh_alma_in_container(version, target)
        return
    ensure_tool("rpm2cpio")
    ensure_tool("cpio")
    ensure_tool("patch")
    src_rpm = rpm_source_path(version, target)
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
    patch_name = target.patch
    write_git_am_patch(PATCHES / patch_name, BUILD / patch_name, "Add TrueNAS storage backend")
    spec_add_patch(spec, patch_name)
    spec_add_truenas_storage_package(spec)
    spec_set_truenas_release(spec, version, target)
    print(f"Alma source ready for {target.id} from {src_rpm}: {BUILD}")


def main(argv: list[str] = sys.argv[1:]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--distro", choices=["ubuntu", "alma"], required=True)
    parser.add_argument("--ubuntu-target", help="Ubuntu target id, for example ubuntu-24.04")
    parser.add_argument("--alma-target", help="AlmaLinux target id, for example almalinux-10")
    parser.add_argument("--version", required=True, help="distro package EVR without local +truenas/.truenas suffix")
    parser.add_argument("--config", default="release/release.example.json", type=Path)
    args = parser.parse_args(argv)
    if args.distro == "ubuntu":
        refresh_ubuntu(args.version, args.config, args.ubuntu_target)
    else:
        refresh_alma(args.version, args.config, args.alma_target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
