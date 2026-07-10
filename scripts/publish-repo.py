#!/usr/bin/env python3
"""Publish Subvirt apt and yum repository metadata from package directories."""

from __future__ import annotations

import argparse
import email.utils
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import tarfile
from pathlib import Path


AR_MAGIC = b"!<arch>\n"

UBUNTU_SUITE_VERSION_PREFIXES = {
    "bionic": "4.0.0-",
    "focal": "6.0.0-",
    "jammy": "8.0.0-",
    "noble": "10.0.0-",
    "resolute": "12.0.0-",
}


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


def archive_packages(incoming: Path, archive_root: Path, build_id: str) -> None:
    files = [
        path for path in sorted(incoming.rglob("*"))
        if path.is_file()
        and (
            path.name.endswith(".deb")
            or (
                path.name.endswith(".rpm")
                and not path.name.endswith(".src.rpm")
                and "debuginfo" not in path.name
                and "debugsource" not in path.name
            )
        )
    ]
    if not files:
        return

    target = archive_root / "builds" / build_id / "artifacts"
    manifest_path = archive_root / "builds" / build_id / "manifest.json"
    entries = []
    for src in files:
        rel = src.relative_to(incoming)
        dst = target / rel
        entries.append({
            "path": f"artifacts/{rel.as_posix()}",
            "size": src.stat().st_size,
            "sha256": checksum(src, "sha256"),
        })
        if dst.exists() and checksum(dst, "sha256") != checksum(src, "sha256"):
            raise RuntimeError(f"archive conflict: {dst} already exists with different content")

    manifest = {
        "build_id": build_id,
        "packages": sorted(entries, key=lambda item: str(item["path"])),
    }
    if manifest_path.exists():
        existing = json.loads(manifest_path.read_text(encoding="utf-8"))
        if existing != manifest:
            raise RuntimeError(f"archive conflict: {manifest_path} already exists with different content")
        return

    target.mkdir(parents=True, exist_ok=True)
    for src in files:
        rel = src.relative_to(incoming)
        dst = target / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if not dst.exists():
            shutil.copyfile(src, dst)
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def publish_apt(incoming: Path, web_root: Path, suite: str, component: str) -> bool:
    if not incoming.exists():
        return False
    debs = sorted(incoming.glob("*.deb"))
    if not debs:
        return False

    pool = web_root / "apt" / "ubuntu" / "pool" / component / suite
    packages_dir = web_root / "apt" / "ubuntu" / "dists" / suite / component / "binary-amd64"
    pool.mkdir(parents=True, exist_ok=True)
    packages_dir.mkdir(parents=True, exist_ok=True)

    for deb in debs:
        shutil.copyfile(deb, pool / deb.name)
    prune_apt_pool(pool, debs, suite)

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
    return True



def prune_apt_pool(pool: Path, incoming_debs: list[Path], suite: str) -> None:
    incoming_controls = [deb_control(deb) for deb in incoming_debs]
    incoming_names = {
        fields.get("Package", "")
        for fields in incoming_controls
        if fields.get("Package")
    }
    incoming_versions_by_name = {
        (fields.get("Package", ""), fields.get("Version", ""))
        for fields in incoming_controls
        if fields.get("Package") and fields.get("Version")
    }
    expected_prefix = UBUNTU_SUITE_VERSION_PREFIXES.get(suite)
    if expected_prefix:
        for deb in pool.glob("*.deb"):
            fields = deb_control(deb)
            package = fields.get("Package", "")
            version = fields.get("Version", "")
            if package.startswith("libvirt") and not version.startswith(expected_prefix):
                deb.unlink()
                continue
            if package in incoming_names and (package, version) not in incoming_versions_by_name:
                deb.unlink()
        return

    for deb in pool.glob("*.deb"):
        fields = deb_control(deb)
        package = fields.get("Package", "")
        version = fields.get("Version", "")
        if package in incoming_names and (package, version) not in incoming_versions_by_name:
            deb.unlink()

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



def publish_apt_all(incoming: Path, web_root: Path, default_suite: str, component: str) -> bool:
    ubuntu = incoming / "ubuntu"
    if not ubuntu.exists():
        return False
    published = False
    direct_debs = sorted(ubuntu.glob("*.deb"))
    if direct_debs:
        published = publish_apt(ubuntu, web_root, default_suite, component) or published
    for suite_dir in sorted(path for path in ubuntu.iterdir() if path.is_dir()):
        if sorted(suite_dir.glob("*.deb")):
            published = publish_apt(suite_dir, web_root, suite_dir.name, component) or published
    return published

def publish_yum(incoming: Path, web_root: Path, distro_path: str, channel: str, gpg_name: str) -> bool:
    if not incoming.exists():
        return False
    rpms = [
        rpm for rpm in sorted(incoming.glob("*.rpm"))
        if not rpm.name.endswith(".src.rpm") and "debuginfo" not in rpm.name and "debugsource" not in rpm.name
    ]
    if not rpms:
        return False
    target = web_root / "yum" / distro_path / channel
    target.mkdir(parents=True, exist_ok=True)
    for rpm in rpms:
        dst = target / rpm.name
        shutil.copyfile(rpm, dst)
        sign_cmd = ["rpmsign"]
        if shutil.which("gpg"):
            sign_cmd += ["--define", f"__gpg {shutil.which('gpg')}"]
        if os.environ.get("GNUPGHOME"):
            sign_cmd += ["--define", f"_gpg_path {os.environ['GNUPGHOME']}"]
        sign_cmd += ["--define", f"_gpg_name {gpg_name}", "--addsign", str(dst)]
        run(sign_cmd)
    prune_yum_rpms(target, rpms, distro_path)
    run(["createrepo_c", "--update", str(target)])
    repomd = target / "repodata" / "repomd.xml"
    run(["gpg", "--batch", "--yes", "--detach-sign", "--armor", str(repomd)])
    return True



def rpm_identity(path: Path) -> tuple[str, str, str] | None:
    if not path.name.endswith(".rpm") or path.name.endswith(".src.rpm"):
        return None
    stem = path.name[:-4]
    arch_sep = stem.rfind(".")
    if arch_sep < 0:
        return None
    arch = stem[arch_sep + 1:]
    nevra = stem[:arch_sep]
    match = re.search(r"-(?=\d)", nevra)
    if not match:
        return None
    name = nevra[:match.start()]
    version_release = nevra[match.start() + 1:]
    return name, version_release, arch


def prune_yum_rpms(target: Path, incoming_rpms: list[Path], distro_path: str) -> None:
    """Remove incompatible and superseded RPMs from a repository.

    Stable repos are append-style so a provider-only release can update just the
    provider package without removing the matching libvirt package set. Keep
    only RPMs that match the major version implied by the repo path, and keep
    only the incoming version for package/arch pairs present in this publish.
    """
    version = distro_path.strip("/").split("/")[-1]
    incoming_identities = {identity for rpm in incoming_rpms if (identity := rpm_identity(rpm))}
    incoming_keys = {(name, arch) for name, _version_release, arch in incoming_identities}
    if not version.isdigit():
        return
    el_version = re.compile(r"\.el(\d+)(?:[_\.-]|$)")
    for rpm in target.glob("*.rpm"):
        match = el_version.search(rpm.name)
        if match and match.group(1) != version:
            rpm.unlink()
            continue
        identity = rpm_identity(rpm)
        if identity is None:
            continue
        name, _version_release, arch = identity
        if (name, arch) in incoming_keys and identity not in incoming_identities:
            rpm.unlink()


def publish_yum_all(incoming: Path, web_root: Path, default_distro_path: str, channel: str, gpg_name: str) -> bool:
    alma = incoming / "alma"
    if not alma.exists():
        return False
    published = False
    direct_rpms = sorted(alma.glob("*.rpm"))
    if direct_rpms:
        published = publish_yum(alma, web_root, default_distro_path, channel, gpg_name) or published
    for version_dir in sorted(path for path in alma.iterdir() if path.is_dir()):
        if sorted(version_dir.glob("*.rpm")):
            published = publish_yum(version_dir, web_root, f"almalinux/{version_dir.name}", channel, gpg_name) or published
    return published

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
    parser.add_argument("--build-id", help="stable build ID used for immutable archive paths")
    parser.add_argument("--archive-root", type=Path, help="archive root; defaults to <web-root>/archive")
    parser.add_argument("--skip-restorecon", action="store_true", help="do not run restorecon after publishing")
    args = parser.parse_args()

    if args.component == "stable":
        if not args.build_id:
            raise SystemExit("--build-id is required when publishing stable")
        archive_packages(args.incoming, args.archive_root or args.web_root / "archive", args.build_id)

    published = [
        publish_apt_all(args.incoming, args.web_root, args.suite, args.component),
        publish_yum_all(args.incoming, args.web_root, args.yum_distro_path, args.component, args.gpg_name),
    ]
    if not any(published):
        raise SystemExit(f"no publishable packages found in {args.incoming}")
    export_key(args.web_root)
    if not args.skip_restorecon and shutil.which("restorecon") and os.geteuid() == 0:
        run(["restorecon", "-RF", str(args.web_root)])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
