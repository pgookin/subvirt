#!/usr/bin/env python3
from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LIBVIRT_PATCHES = sorted((ROOT / "patches" / "libvirt").glob("truenas-storage-backend-*.patch"))


class LibvirtPatchTests(unittest.TestCase):
    def test_provider_socket_copy_uses_negative_error_check(self) -> None:
        self.assertTrue(LIBVIRT_PATCHES)
        for path in LIBVIRT_PATCHES:
            text = path.read_text(encoding="utf-8")
            with self.subTest(path=path.relative_to(ROOT)):
                self.assertNotIn("if (!virStrcpyStatic(addr.sun_path, TRUENAS_PROVIDER_SOCKET))", text)
                self.assertIn("if (virStrcpyStatic(addr.sun_path, TRUENAS_PROVIDER_SOCKET) < 0)", text)

    def test_provider_socket_error_mentions_provider_service(self) -> None:
        for path in LIBVIRT_PATCHES:
            text = path.read_text(encoding="utf-8")
            with self.subTest(path=path.relative_to(ROOT)):
                self.assertIn("truenas-libvirt-provider.service", text)


if __name__ == "__main__":
    unittest.main()
