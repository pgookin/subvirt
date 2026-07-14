#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
spec = importlib.util.spec_from_file_location("release", ROOT / "scripts" / "release.py")
release = importlib.util.module_from_spec(spec)
sys.modules["release"] = release
assert spec.loader is not None
spec.loader.exec_module(release)


class PublicStableUrlTests(unittest.TestCase):
    def context(self) -> release.Context:
        return release.Context(
            config={
                "hosts": {"public_repo": "repo.example"},
                "repos": {
                    "apt_distribution": "noble",
                    "apt_distributions": ["noble"],
                    "yum_distro_path": "almalinux/10",
                    "web_root": "/srv/repo/www",
                },
                "public_repo": {"base_url": "https://repo.example"},
            },
            execute=False,
            ref="main",
            build_id="build-1",
        )

    def test_ubuntu_package_urls_use_artifact_suite_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "artifacts/build-1/ubuntu/bionic").mkdir(parents=True)
            (root / "artifacts/build-1/ubuntu/noble").mkdir(parents=True)
            (root / "artifacts/build-1/ubuntu/bionic/libnss-libvirt_4.0.0-1ubuntu8.21+truenas1_amd64.deb").write_bytes(b"deb")
            (root / "artifacts/build-1/ubuntu/noble/libnss-libvirt_10.0.0-2ubuntu8.14+truenas1_amd64.deb").write_bytes(b"deb")

            old_cwd = Path.cwd()
            try:
                import os
                os.chdir(root)
                urls = release.public_stable_urls(self.context())
            finally:
                os.chdir(old_cwd)

        self.assertIn(
            "https://repo.example/apt/ubuntu/pool/stable/bionic/libnss-libvirt_4.0.0-1ubuntu8.21+truenas1_amd64.deb",
            urls,
        )
        self.assertNotIn(
            "https://repo.example/apt/ubuntu/pool/stable/noble/libnss-libvirt_4.0.0-1ubuntu8.21+truenas1_amd64.deb",
            urls,
        )

    def test_alma_package_urls_use_artifact_version_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "artifacts/build-1/alma/9").mkdir(parents=True)
            (root / "artifacts/build-1/alma/10").mkdir(parents=True)
            (root / "artifacts/build-1/alma/9/libvirt-11.10.0-12.3.el9_8.alma.1.truenas1.x86_64.rpm").write_bytes(b"rpm")
            (root / "artifacts/build-1/alma/10/libvirt-11.10.0-12.el10_2.alma.1.truenas1.x86_64.rpm").write_bytes(b"rpm")

            old_cwd = Path.cwd()
            try:
                import os
                os.chdir(root)
                urls = release.public_stable_urls(self.context())
            finally:
                os.chdir(old_cwd)

        self.assertIn(
            "https://repo.example/yum/almalinux/9/stable/libvirt-11.10.0-12.3.el9_8.alma.1.truenas1.x86_64.rpm",
            urls,
        )
        self.assertIn(
            "https://repo.example/yum/almalinux/10/stable/libvirt-11.10.0-12.el10_2.alma.1.truenas1.x86_64.rpm",
            urls,
        )


if __name__ == "__main__":
    unittest.main()
