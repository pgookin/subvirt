#!/usr/bin/env python3
"""Write a small release evidence manifest for CI artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--build-id', required=True)
    parser.add_argument('--artifact-root', default='artifacts')
    parser.add_argument('--output', default='')
    args = parser.parse_args()
    root = Path(args.artifact_root) / args.build_id
    packages = []
    for path in sorted(root.rglob('*')):
        if path.is_file() and path.suffix in {'.deb', '.rpm', '.dsc', '.changes', '.buildinfo'}:
            packages.append({'path': path.as_posix(), 'size': path.stat().st_size, 'sha256': sha256(path)})
    manifest = {'build_id': args.build_id, 'artifact_root': root.as_posix(), 'packages': packages}
    output = Path(args.output) if args.output else root / 'release-evidence.json'
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(manifest, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    print(output)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
