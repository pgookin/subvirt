#!/usr/bin/env python3
"""Publish Subvirt apt and yum repository metadata from package directories."""

from __future__ import annotations

import argparse
import email.utils
import gzip
import hashlib
import io
import os
import shutil
import subprocess
import tarfile
from pathlib import Path


AR_MAGIC = b"!<arch>\n"


def run(argv: list[str]) -> None:
    print("+ " + " ".join(argv))
    subprocess.run(argv, check=True)


def ar_members(path: Path) -> dict[str, bytes]:
    data = path.read_bytes()
    if not data.startswith(AR_MAGIC):
        raise ValueError(f"{path} is not an ar archive")
    pos = len(AR_MAGIC)
    members: dict[str, bytes] = {}
    while pos + 60 <= len(data):
        header = data[pos:pos + 60]
        pos += 60
        name = header[:16].decode("utf-8").strip()
        size = int(header[48:58].decode("ascii").strip())
        payload = data[pos:pos + size]
        pos += size + (size % 2)
        members[name.rstrip("/")] = payload
    return members


def deb_control(path: Path) -> dict[str, str]:
    members = ar_members(path)
    control_name = next((name for name in members if name.startswith("control.tar")), None)
    if control_name is None:
        raise ValueError(f"{path} has no control.tar member")
    payload = members[control_name]
    if control_name.endswith(".zst"):
        result = subprocess.run(["zstd", "-d", "-c"], input=payload, stdout=subprocess.PIPE, check=True)
        payload = result.stdout
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:*") as tar:
        control = tar.extractfile("./control") or tar.extractfile("control")
        if control is None:
            raise ValueError(f"{path} control archive has no control file")
        text = control.read().decode("utf-8")
    fields: dict[str, str] = {}
    current: str | None = None
    for line in text.splitlines():
        if line.startswith((" ", "\t")) and current:
            fields[current] += "\n" + line
            continue
        key, _, value = line.partition(":")
        if not value:
            continue
        current = key
        fields[key] = value.strip()
    return fields


def checksum(path: Path, algorithm: str) -> str:
    h = hashlib.new(algorithm)
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def publish_apt(incoming: Path, web_root: Path, suite: str, component: str) -> None:
    debs = sorted(incoming.glob("*.deb"))
    if not debs:
        raise SystemExit(f"no .deb files in {incoming}")

    pool = web_root / "apt" / "ubuntu" / "pool" / component
    packages_dir = web_root / "apt" / "ubuntu" / "dists" / suite / component / "binary-amd64"
    pool.mkdir(parents=True, exist_ok=True)
    packages_dir.mkdir(parents=True, exist_ok=True)

    for deb in debs:
        shutil.copy2(deb, pool / deb.name)

    paragraphs: list[str] = []
    for deb in sorted(pool.glob("*.deb")):
        fields = deb_control(deb)
        relative = deb.relative_to(web_root / "apt" / "ubuntu")
        lines = []
        for key, value in fields.items():
            lines.append(f"{key}: {value}")
        lines.extend([
            f"Filename: {relative}",
            f"Size: {deb.stat().st_size}",
            f"MD5sum: {checksum(deb, 'md5')}",
            f"SHA1: {checksum(deb, 'sha1')}",
            f"SHA256: {checksum(deb, 'sha256')}",
        ])
        paragraphs.append("\n".join(lines))

    packages = packages_dir / "Packages"
    packages.write_text("\n\n".join(paragraphs) + "\n", encoding="utf-8")
    with packages.open("rb") as src, gzip.open(str(packages) + ".gz", "wb", compresslevel=9) as dst:
        shutil.copyfileobj(src, dst)

    suite_dir = web_root / "apt" / "ubuntu" / "dists" / suite
    components = sorted(path.name for path in suite_dir.iterdir() if path.is_dir())
    write_release(web_root / "apt" / "ubuntu", suite, components)


def release_entry(root: Path, path: Path) -> tuple[int, str, str, str]:
    rel = path.relative_to(root).as_posix()
    return path.stat().st_size, rel, checksum(path, "md5"), checksum(path, "sha256")


def write_release(apt_root: Path, suite: str, components: list[str]) -> None:
    dists = apt_root / "dists" / suite
    files = sorted(path for path in dists.rglob("*") if path.is_file() and path.name not in {"Release", "InRelease", "Release.gpg"})
    release = dists / "Release"
    lines = [
        "Origin: Subvirt",
        "Label: Subvirt",
        f"Suite: {suite}",
        f"Codename: {suite}",
        "Architectures: amd64 all",
        f"Components: {' '.join(components)}",
        "Description: Subvirt packages",
        f"Date: {email.utils.formatdate(usegmt=True)}",
        "MD5Sum:",
    ]
    for size, rel, md5, _sha256 in [release_entry(dists, path) for path in files]:
        lines.append(f" {md5} {size:16d} {rel}")
    lines.append("SHA256:")
    for size, rel, _md5, sha256 in [release_entry(dists, path) for path in files]:
        lines.append(f" {sha256} {size:16d} {rel}")
    release.write_text("\n".join(lines) + "\n", encoding="utf-8")
    run(["gpg", "--batch", "--yes", "--clearsign", "-o", str(dists / "InRelease"), str(release)])
    run(["gpg", "--batch", "--yes", "--detach-sign", "--armor", "-o", str(dists / "Release.gpg"), str(release)])


def publish_yum(incoming: Path, web_root: Path, distro_path: str, channel: str, gpg_name: str) -> None:
    rpms = [
        rpm for rpm in sorted(incoming.glob("*.rpm"))
        if not rpm.name.endswith(".src.rpm") and "debuginfo" not in rpm.name and "debugsource" not in rpm.name
    ]
    if not rpms:
        raise SystemExit(f"no binary .rpm files in {incoming}")
    target = web_root / "yum" / distro_path / channel
    target.mkdir(parents=True, exist_ok=True)
    for rpm in rpms:
        dst = target / rpm.name
        shutil.copy2(rpm, dst)
        run(["rpmsign", "--define", f"_gpg_name {gpg_name}", "--addsign", str(dst)])
    run(["createrepo_c", "--update", str(target)])
    repomd = target / "repodata" / "repomd.xml"
    run(["gpg", "--batch", "--yes", "--detach-sign", "--armor", str(repomd)])


def export_key(web_root: Path) -> None:
    key_dir = web_root / "keys"
    key_dir.mkdir(parents=True, exist_ok=True)
    run(["gpg", "--batch", "--yes", "--export", "--armor", "-o", str(key_dir / "subvirt.asc")])
    run(["gpg", "--batch", "--yes", "--export", "-o", str(key_dir / "subvirt.gpg")])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--incoming", required=True, type=Path)
    parser.add_argument("--web-root", default="/srv/repo/www", type=Path)
    parser.add_argument("--suite", default="noble")
    parser.add_argument("--component", choices=["staging", "stable"], required=True)
    parser.add_argument("--yum-distro-path", default="almalinux/10")
    parser.add_argument("--gpg-name", default="Subvirt Repository <repo@subvirt.local>")
    args = parser.parse_args()

    publish_apt(args.incoming / "ubuntu", args.web_root, args.suite, args.component)
    publish_yum(args.incoming / "alma", args.web_root, args.yum_distro_path, args.component, args.gpg_name)
    export_key(args.web_root)
    if shutil.which("restorecon"):
        run(["restorecon", "-RF", str(args.web_root)])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
