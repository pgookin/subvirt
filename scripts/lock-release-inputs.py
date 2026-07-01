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


def changed_distros(report_path: Path | None) -> tuple[bool, bool]:
    if report_path is None:
        return True, True
    report: dict[str, Any] = json.loads(report_path.read_text(encoding="utf-8"))
    ubuntu = False
    alma = False
    for package in report.get("packages", []):
        if not package.get("update_available"):
            continue
        distro = package.get("distro")
        if distro == "ubuntu":
            ubuntu = True
        elif distro == "alma":
            alma = True
    return ubuntu, alma


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lock", default="release/upstream-lock.json", type=Path)
    parser.add_argument("--report", type=Path, help="upstream check report used to gate distro builds")
    parser.add_argument("--github-output", default="")
    args = parser.parse_args()

    data: dict[str, Any] = json.loads(args.lock.read_text(encoding="utf-8"))
    ubuntu = str(data["ubuntu"]["version"])
    alma = str(data["alma"]["version"])
    build_ubuntu, build_alma = changed_distros(args.report)
    build_id = f"upstream-u24-{sanitize(ubuntu)}-al10-{sanitize(alma)}"
    outputs = {
        "ubuntu_version": ubuntu,
        "alma_version": alma,
        "build_ubuntu": str(build_ubuntu).lower(),
        "build_alma": str(build_alma).lower(),
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
