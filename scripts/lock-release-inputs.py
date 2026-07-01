#!/usr/bin/env python3
"""Derive release workflow inputs from release/upstream-lock.json."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any


def sanitize(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lock", default="release/upstream-lock.json", type=Path)
    parser.add_argument("--github-output", default="")
    args = parser.parse_args()

    data: dict[str, Any] = json.loads(args.lock.read_text(encoding="utf-8"))
    ubuntu = str(data["ubuntu"]["version"])
    alma = str(data["alma"]["version"])
    build_id = f"upstream-u24-{sanitize(ubuntu)}-al10-{sanitize(alma)}"
    outputs = {
        "ubuntu_version": ubuntu,
        "alma_version": alma,
        "build_id": build_id,
    }
    for key, value in outputs.items():
        print(f"{key}={value}")
    if args.github_output:
        with Path(args.github_output).open("a", encoding="utf-8") as handle:
            for key, value in outputs.items():
                handle.write(f"{key}={value}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
