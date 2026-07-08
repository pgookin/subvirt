#!/usr/bin/env python3
"""Validate unified-diff hunk line counts.

This catches malformed generated patches where a hunk header says one line
count but the body contains a different number of old/new lines. Patch tools
can otherwise apply only part of a hunk and leave missing source in packages.
"""

import re
import sys
from pathlib import Path
from typing import List, Optional

HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def declared_count(value: Optional[str]) -> int:
    return 1 if value is None else int(value)


def validate(path: Path) -> List[str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    errors = []  # type: List[str]
    idx = 0
    while idx < len(lines):
        match = HUNK_RE.match(lines[idx])
        if not match:
            idx += 1
            continue

        old_expected = declared_count(match.group(2))
        new_expected = declared_count(match.group(4))
        header_line = idx + 1
        idx += 1
        old_actual = 0
        new_actual = 0

        while idx < len(lines):
            line = lines[idx]
            if line == "-- " or HUNK_RE.match(line) or line.startswith("diff --git "):
                break
            if line.startswith("\\"):
                idx += 1
                continue
            if not line:
                errors.append(f"{path}:{idx + 1}: empty hunk body line must start with a space")
            elif line[0] == " ":
                old_actual += 1
                new_actual += 1
            elif line[0] == "-":
                old_actual += 1
            elif line[0] == "+":
                new_actual += 1
            else:
                errors.append(f"{path}:{idx + 1}: invalid hunk body prefix {line[0]!r}")
            idx += 1

        if old_actual != old_expected or new_actual != new_expected:
            errors.append(
                f"{path}:{header_line}: hunk count mismatch "
                f"old {old_actual}/{old_expected}, new {new_actual}/{new_expected}"
            )
    return errors


def main(argv: List[str]) -> int:
    if len(argv) < 2:
        print("usage: validate-unified-diff.py PATCH...", file=sys.stderr)
        return 2

    all_errors = []  # type: List[str]
    for item in argv[1:]:
        all_errors.extend(validate(Path(item)))

    if all_errors:
        for error in all_errors:
            print(error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
