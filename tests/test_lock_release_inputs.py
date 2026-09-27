#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
spec = importlib.util.spec_from_file_location(
    "lock_release_inputs",
    ROOT / "scripts" / "lock-release-inputs.py",
)
lock_release_inputs = importlib.util.module_from_spec(spec)
sys.modules["lock_release_inputs"] = lock_release_inputs
assert spec.loader is not None
spec.loader.exec_module(lock_release_inputs)


class CandidateBuildIdTests(unittest.TestCase):
    def test_ubuntu_version_change_changes_build_id(self) -> None:
        alma = ["almalinux-10=11.10.0-12.4.el10_2.alma.1"]
        before = lock_release_inputs.candidate_build_id(
            ["ubuntu-24.04=10.0.0-2ubuntu8.16"],
            alma,
            "gh100-1",
        )
        after = lock_release_inputs.candidate_build_id(
            ["ubuntu-24.04=10.0.0-2ubuntu8.17"],
            alma,
            "gh100-1",
        )

        self.assertNotEqual(before, after)

    def test_alma_version_change_changes_build_id(self) -> None:
        ubuntu = ["ubuntu-24.04=10.0.0-2ubuntu8.17"]
        before = lock_release_inputs.candidate_build_id(
            ubuntu,
            ["almalinux-10=11.10.0-12.3.el10_2.alma.1"],
            "gh100-1",
        )
        after = lock_release_inputs.candidate_build_id(
            ubuntu,
            ["almalinux-10=11.10.0-12.4.el10_2.alma.1"],
            "gh100-1",
        )

        self.assertNotEqual(before, after)

    def test_run_attempt_makes_rebuild_unique(self) -> None:
        ubuntu = ["ubuntu-24.04=10.0.0-2ubuntu8.17"]
        alma = ["almalinux-10=11.10.0-12.4.el10_2.alma.1"]

        first = lock_release_inputs.candidate_build_id(ubuntu, alma, "gh100-1")
        retry = lock_release_inputs.candidate_build_id(ubuntu, alma, "gh100-2")

        self.assertNotEqual(first, retry)
        self.assertTrue(first.endswith("-gh100-1"))
        self.assertTrue(retry.endswith("-gh100-2"))

    def test_input_order_does_not_change_digest(self) -> None:
        ubuntu = [
            "ubuntu-22.04=8.0.0-1ubuntu7.20",
            "ubuntu-24.04=10.0.0-2ubuntu8.17",
        ]
        alma = [
            "almalinux-9=11.10.0-12.3.el9_8.alma.1",
            "almalinux-10=11.10.0-12.4.el10_2.alma.1",
        ]

        self.assertEqual(
            lock_release_inputs.release_input_digest(ubuntu, alma),
            lock_release_inputs.release_input_digest(
                list(reversed(ubuntu)),
                list(reversed(alma)),
            ),
        )


if __name__ == "__main__":
    unittest.main()
