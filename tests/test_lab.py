#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("lab", ROOT / "scripts" / "lab.py")
lab = importlib.util.module_from_spec(spec)
sys.modules["lab"] = lab
assert spec.loader is not None
spec.loader.exec_module(lab)


class UbuntuMirrorBootcmdTests(unittest.TestCase):
    def test_rewrites_legacy_and_deb822_sources_to_local_mirror(self) -> None:
        config = {
            "lab": {
                "workdir": "/tmp/subvirt-lab",
                "mirrors": {"ubuntu": "http://10.1.0.121/ubuntu"},
            }
        }
        bootcmd = lab.cloud_init_repo_bootcmd(lab.Lab(config=config, execute=False, build_id="test"), "ubuntu")

        self.assertIn("/etc/apt/sources.list", bootcmd)
        self.assertIn("/etc/apt/sources.list.d/*.sources", bootcmd)
        self.assertIn("URIs: $mirror", bootcmd)
        self.assertIn("http://10.1.0.121/ubuntu", bootcmd)


if __name__ == "__main__":
    unittest.main()
