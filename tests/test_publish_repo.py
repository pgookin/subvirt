#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("publish_repo", ROOT / "scripts" / "publish-repo.py")
publish_repo = importlib.util.module_from_spec(spec)
sys.modules["publish_repo"] = publish_repo
assert spec.loader is not None
spec.loader.exec_module(publish_repo)


class ArchivePackageTests(unittest.TestCase):
    def test_archive_is_idempotent_for_identical_build(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            incoming = root / "incoming"
            archive = root / "archive"
            package = incoming / "ubuntu" / "noble" / "pkg_1_amd64.deb"
            package.parent.mkdir(parents=True)
            package.write_bytes(b"package-v1")

            publish_repo.archive_packages(incoming, archive, "build-1")
            publish_repo.archive_packages(incoming, archive, "build-1")

            archived = archive / "builds" / "build-1" / "artifacts" / "ubuntu" / "noble" / package.name
            manifest = json.loads((archive / "builds" / "build-1" / "manifest.json").read_text())
            self.assertEqual(archived.read_bytes(), b"package-v1")
            self.assertEqual(manifest["build_id"], "build-1")
            self.assertEqual(manifest["packages"][0]["path"], "artifacts/ubuntu/noble/pkg_1_amd64.deb")

    def test_archive_rejects_conflicting_republish(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            incoming = root / "incoming"
            archive = root / "archive"
            package = incoming / "alma" / "9" / "pkg-1-1.el9.x86_64.rpm"
            package.parent.mkdir(parents=True)
            package.write_bytes(b"package-v1")
            publish_repo.archive_packages(incoming, archive, "build-1")

            package.write_bytes(b"package-v2")
            with self.assertRaisesRegex(RuntimeError, "archive conflict"):
                publish_repo.archive_packages(incoming, archive, "build-1")


class AptPruneTests(unittest.TestCase):
    def test_prunes_wrong_suite_and_superseded_same_package_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pool = Path(tmp)
            files = {
                "libvirt0_10.0.0-2ubuntu8.13+truenas1_amd64.deb": {"Package": "libvirt0", "Version": "10.0.0-2ubuntu8.13+truenas1"},
                "libvirt0_10.0.0-2ubuntu8.14+truenas1_amd64.deb": {"Package": "libvirt0", "Version": "10.0.0-2ubuntu8.14+truenas1"},
                "libvirt0_8.0.0-1ubuntu7.17+truenas1_amd64.deb": {"Package": "libvirt0", "Version": "8.0.0-1ubuntu7.17+truenas1"},
                "truenas-libvirt-provider_0.2.0-3_all.deb": {"Package": "truenas-libvirt-provider", "Version": "0.2.0-3"},
                "truenas-libvirt-provider_0.2.0-4_all.deb": {"Package": "truenas-libvirt-provider", "Version": "0.2.0-4"},
                "virt-manager_5.1.0-1_all.deb": {"Package": "virt-manager", "Version": "5.1.0-1"},
            }
            for name in files:
                (pool / name).write_bytes(b"x")

            def fake_control(path: Path) -> dict[str, str]:
                return files[path.name]

            incoming = [pool / "libvirt0_10.0.0-2ubuntu8.14+truenas1_amd64.deb", pool / "truenas-libvirt-provider_0.2.0-4_all.deb"]
            with mock.patch.object(publish_repo, "deb_control", side_effect=fake_control):
                publish_repo.prune_apt_pool(pool, incoming, "noble")

            self.assertEqual(
                sorted(path.name for path in pool.glob("*.deb")),
                [
                    "libvirt0_10.0.0-2ubuntu8.14+truenas1_amd64.deb",
                    "truenas-libvirt-provider_0.2.0-4_all.deb",
                    "virt-manager_5.1.0-1_all.deb",
                ],
            )


class YumPruneTests(unittest.TestCase):
    def test_prunes_wrong_el_and_superseded_same_package_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            names = [
                "libvirt-daemon-driver-storage-truenas-11.10.0-12.1.el9_8.alma.1.x86_64.rpm",
                "libvirt-daemon-driver-storage-truenas-11.10.0-12.3.el9_8.alma.1.x86_64.rpm",
                "libvirt-daemon-driver-storage-truenas-11.10.0-12.el10_2.alma.1.x86_64.rpm",
                "truenas-libvirt-provider-0.2.0-3.el9.noarch.rpm",
                "truenas-libvirt-provider-0.2.0-4.el9.noarch.rpm",
                "virt-manager-5.1.0-1.truenas1.el9.noarch.rpm",
            ]
            for name in names:
                (target / name).write_bytes(b"x")

            incoming = [
                target / "libvirt-daemon-driver-storage-truenas-11.10.0-12.3.el9_8.alma.1.x86_64.rpm",
                target / "truenas-libvirt-provider-0.2.0-4.el9.noarch.rpm",
            ]
            publish_repo.prune_yum_rpms(target, incoming, "almalinux/9")

            self.assertEqual(
                sorted(path.name for path in target.glob("*.rpm")),
                [
                    "libvirt-daemon-driver-storage-truenas-11.10.0-12.3.el9_8.alma.1.x86_64.rpm",
                    "truenas-libvirt-provider-0.2.0-4.el9.noarch.rpm",
                    "virt-manager-5.1.0-1.truenas1.el9.noarch.rpm",
                ],
            )


if __name__ == "__main__":
    unittest.main()
