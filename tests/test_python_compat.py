#!/usr/bin/env python3
from __future__ import annotations

import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEPLOYED_PYTHON36_SCRIPTS = [
    ROOT / "truenas_provider.py",
    ROOT / "truenas_provider_daemon.py",
    ROOT / "scripts" / "test-storage.py",
]


class Python36CompatibilityTests(unittest.TestCase):
    def test_deployed_guest_scripts_parse_on_python36(self) -> None:
        for path in DEPLOYED_PYTHON36_SCRIPTS:
            source = path.read_text(encoding="utf-8")
            with self.subTest(path=path.relative_to(ROOT)):
                ast.parse(source, filename=str(path), feature_version=(3, 6))

    def test_deployed_guest_scripts_do_not_use_builtin_generic_annotations(self) -> None:
        for path in DEPLOYED_PYTHON36_SCRIPTS:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(path))
            offenders = []
            for node in ast.walk(tree):
                if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name) and node.value.id in {"dict", "list", "set", "tuple"}:
                    offenders.append(f"{node.value.id}[] at line {node.lineno}")
            with self.subTest(path=path.relative_to(ROOT)):
                self.assertEqual(offenders, [])

    def test_deployed_guest_scripts_do_not_require_argparse_subparsers_keyword(self) -> None:
        for path in DEPLOYED_PYTHON36_SCRIPTS:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(path))
            offenders = []
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "add_subparsers":
                    for keyword in node.keywords:
                        if keyword.arg == "required":
                            offenders.append(f"add_subparsers(required=...) at line {node.lineno}")
            with self.subTest(path=path.relative_to(ROOT)):
                self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
