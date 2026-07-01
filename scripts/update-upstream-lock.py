#!/usr/bin/env python3
"""Update release/upstream-lock.json from check-upstream.py JSON output."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def load(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding='utf-8'))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--report', required=True, type=Path)
    parser.add_argument('--lock', default='release/upstream-lock.json', type=Path)
    args = parser.parse_args()

    report = load(args.report)
    lock = load(args.lock)
    for row in report.get('packages', []):
        distro = row['distro']
        lock[distro] = {
            'package': row['package'],
            'version': row['current_version'],
        }
    args.lock.write_text(json.dumps(lock, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
