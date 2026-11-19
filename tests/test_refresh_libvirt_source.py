#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
spec = importlib.util.spec_from_file_location("refresh_libvirt_source", ROOT / "scripts" / "refresh-libvirt-source.py")
refresh = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(refresh)


class RpmSourceTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.sources = Path(temporary.name)
        self.target = refresh.alma_target(None, target_id="almalinux-10")
        self.version = "12.5.0-4.el10_3.alma.1"
        self.out = self.sources / self.target.source_dir
        self.out.mkdir()
        self.expected = self.out / f"libvirt-{self.version}.src.rpm"
        self.patch("SOURCES", self.sources)
        self.patch("ensure_tool")
        self.download = self.patch("run")
        query_patcher = mock.patch.object(refresh.subprocess, "run")
        self.addCleanup(query_patcher.stop)
        self.query = query_patcher.start()
        self.query.return_value.stdout = f"libvirt-{self.version}\n"

    def patch(self, name: str, *args):
        patcher = mock.patch.object(refresh, name, *args)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def test_reuses_exact_cached_version_among_other_versions(self) -> None:
        self.expected.touch()
        (self.out / "libvirt-99.0.0-1.el10.src.rpm").touch()

        self.assertEqual(refresh.rpm_source_path(self.version, self.target), self.expected)
        self.download.assert_not_called()
        self.query.assert_called_once_with(
            ["rpm", "-qp", "--queryformat", "%{NAME}-%{VERSION}-%{RELEASE}\n", str(self.expected)],
            check=True, capture_output=True, text=True,
        )

    def test_downloads_requested_version_despite_stale_cache(self) -> None:
        stale = self.out / "libvirt-11.10.0-12.el10_2.alma.1.src.rpm"
        stale.touch()
        self.download.side_effect = lambda argv: self.expected.touch()

        self.assertEqual(refresh.rpm_source_path(self.version, self.target), self.expected)
        self.download.assert_called_once_with(
            ["dnf", "download", "--source", "--destdir", str(self.out), f"libvirt-{self.version}"]
        )
        self.assertTrue(stale.exists())

    def test_downloads_into_missing_target_directory(self) -> None:
        self.out.rmdir()

        def download(argv):
            self.assertTrue(self.out.is_dir())
            self.expected.touch()

        self.download.side_effect = download
        self.assertEqual(refresh.rpm_source_path(self.version, self.target), self.expected)

    def test_rejects_download_without_requested_version(self) -> None:
        stale = self.out / "libvirt-11.10.0-12.el10_2.alma.1.src.rpm"
        stale.touch()
        self.download.side_effect = lambda argv: (self.out / "libvirt-99.0.0-1.el10.src.rpm").touch()

        with self.assertRaisesRegex(SystemExit, f"requested libvirt source RPM.*{self.version}"):
            refresh.rpm_source_path(self.version, self.target)
        self.query.assert_not_called()

    def test_download_failure_does_not_fall_back_to_cache(self) -> None:
        (self.out / "libvirt-11.10.0-12.el10_2.alma.1.src.rpm").touch()
        self.download.side_effect = subprocess.CalledProcessError(1, ["dnf", "download"])

        with self.assertRaises(subprocess.CalledProcessError):
            refresh.rpm_source_path(self.version, self.target)
        self.query.assert_not_called()

    def test_rejects_mislabeled_cached_rpm(self) -> None:
        self.expected.touch()
        for identity in (
            "libvirt-11.10.0-12.el10_2.alma.1",
            "libvirt-12.5.0-3.el10_3.alma.1",
            f"not-libvirt-{self.version}",
        ):
            with self.subTest(identity=identity):
                self.query.return_value.stdout = identity + "\n"
                with self.assertRaisesRegex(SystemExit, "source RPM mismatch"):
                    refresh.rpm_source_path(self.version, self.target)

    def test_rejects_unreadable_rpm(self) -> None:
        self.expected.touch()
        self.query.side_effect = subprocess.CalledProcessError(1, ["rpm", "-qp"])

        with self.assertRaises(subprocess.CalledProcessError):
            refresh.rpm_source_path(self.version, self.target)


if __name__ == "__main__":
    unittest.main()
