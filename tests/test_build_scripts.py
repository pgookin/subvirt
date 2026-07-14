#!/usr/bin/env python3
from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class BuildScriptTests(unittest.TestCase):
    def test_ubuntu_container_build_refreshes_locked_source_before_selecting_tree(self) -> None:
        script = (ROOT / "scripts" / "container-build-ubuntu.sh").read_text(encoding="utf-8")
        refresh = './scripts/refresh-locked-libvirt-sources.sh "$TARGET_ID"'
        select = 'SRC_DIR=$(find build -maxdepth 1 -type d -name "${SRC_GLOB#build/}" | sort | tail -1)'

        self.assertIn(refresh, script)
        self.assertIn(select, script)
        self.assertLess(script.index(refresh), script.index(select))


if __name__ == "__main__":
    unittest.main()
