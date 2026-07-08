#!/usr/bin/env python3
"""Check configured distro mirrors for newer libvirt package versions."""

from __future__ import annotations

import argparse
import gzip
import json
import lzma
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from alma_targets import AlmaTarget, alma_lock_key, alma_targets
from ubuntu_targets import UbuntuTarget, ubuntu_lock_key, ubuntu_targets

UPDATE_EXIT = 10


@dataclass(frozen=True)
class VersionInfo:
    distro: str
    package: str
    version: str
    source: str
    suite: str = ""
    support_tier: str = ""


def fetch(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=60) as response:
        return response.read()


def load_json(path: Path) -> dict[str, Any]:
    with path.open('r', encoding='utf-8') as handle:
        return json.load(handle)


def deb_version_key(version: str) -> list[Any]:
    # Enough for Ubuntu's libvirt EVR shape; dpkg remains authoritative in builds.
    parts = re.split(r'([0-9]+)', version.replace('~', '-'))
    key: list[Any] = []
    for part in parts:
        if not part:
            continue
        key.append(int(part) if part.isdigit() else part)
    return key


def rpm_version_key(version: str) -> list[Any]:
    parts = re.split(r'([0-9]+)', version)
    key: list[Any] = []
    for part in parts:
        if not part:
            continue
        key.append(int(part) if part.isdigit() else part)
    return key


def newer(distro: str, current: str, locked: str) -> bool:
    if not locked:
        return True
    if distro.startswith('ubuntu_') or distro == 'ubuntu':
        return deb_version_key(current) > deb_version_key(locked)
    return rpm_version_key(current) > rpm_version_key(locked)


def ubuntu_candidate(config: dict[str, Any], target: UbuntuTarget) -> VersionInfo:
    mirrors = config['mirrors']
    mirror = str(mirrors.get(target.id, mirrors.get(target.suite, mirrors.get('ubuntu')))).rstrip('/')
    suite = target.suite
    pockets = config.get('ubuntu_pockets', ['updates', 'security'])
    component = config.get('ubuntu_component', 'main')
    arch = config.get('ubuntu_arch', 'amd64')
    candidates: list[tuple[list[Any], str, str]] = []
    for pocket in pockets:
        dist = suite if pocket in {'release', suite} else f'{suite}-{pocket}'
        url = f'{mirror}/dists/{dist}/{component}/binary-{arch}/Packages.xz'
        text = lzma.decompress(fetch(url)).decode('utf-8', errors='replace')
        for paragraph in text.split('\n\n'):
            fields: dict[str, str] = {}
            for line in paragraph.splitlines():
                if ': ' in line:
                    key, value = line.split(': ', 1)
                    fields[key] = value
            if fields.get('Package') == 'libvirt0' and 'Version' in fields:
                version = fields['Version']
                candidates.append((deb_version_key(version), version, url))
                break
    if not candidates:
        raise RuntimeError('libvirt0 was not found in configured Ubuntu mirrors')
    _, version, source = sorted(candidates)[-1]
    return VersionInfo(ubuntu_lock_key(target), 'libvirt0', version, source, suite=target.suite, support_tier=target.support_tier)


def alma_candidate(config: dict[str, Any], target: AlmaTarget) -> VersionInfo:
    mirrors = config['mirrors']
    mirror = str(mirrors.get(target.id, mirrors.get(target.version, mirrors.get('alma')))).rstrip('/')
    repo_path = target.repo_path
    base = f'{mirror}/{repo_path.strip("/")}'
    repomd = ET.fromstring(fetch(f'{base}/repodata/repomd.xml'))
    repo_ns = {'r': 'http://linux.duke.edu/metadata/repo'}
    primary_href = None
    for data in repomd.findall('r:data', repo_ns):
        if data.get('type') == 'primary':
            primary_href = data.find('r:location', repo_ns).get('href')
            break
    if primary_href is None:
        raise RuntimeError('Alma primary metadata was not found')
    primary = gzip.decompress(fetch(f'{base}/{primary_href}'))
    root = ET.fromstring(primary)
    common_ns = {'m': 'http://linux.duke.edu/metadata/common'}
    for package in root.findall('m:package', common_ns):
        if package.findtext('m:name', namespaces=common_ns) != 'libvirt':
            continue
        version = package.find('m:version', common_ns).attrib
        evr = f"{version['ver']}-{version['rel']}"
        return VersionInfo(alma_lock_key(target), 'libvirt', evr, f'{base}/{primary_href}', suite=target.version)
    raise RuntimeError('libvirt was not found in configured Alma mirror metadata')


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', default='release/release.example.json')
    parser.add_argument('--lock', default='release/upstream-lock.json')
    parser.add_argument('--json-output', default='')
    args = parser.parse_args()

    config = load_json(Path(args.config)).get('upstream', {})
    lock_path = Path(args.lock)
    lock = load_json(lock_path) if lock_path.exists() else {}

    found = [ubuntu_candidate(config, target) for target in ubuntu_targets({'upstream': config})]
    found.extend(alma_candidate(config, target) for target in alma_targets({'upstream': config}))
    updates: list[dict[str, str | bool]] = []
    for item in found:
        locked = str(lock.get(item.distro, {}).get('version', ''))
        updates.append({
            'distro': item.distro,
            'package': item.package,
            'locked_version': locked,
            'current_version': item.version,
            'update_available': newer(item.distro, item.version, locked),
            'source': item.source,
            'suite': item.suite,
            'support_tier': item.support_tier,
        })

    result = {'updates_available': any(bool(row['update_available']) for row in updates), 'packages': updates}
    payload = json.dumps(result, indent=2, sort_keys=True)
    print(payload)
    if args.json_output:
        Path(args.json_output).write_text(payload + '\n', encoding='utf-8')
    return UPDATE_EXIT if result['updates_available'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
