#!/usr/bin/env python3
from __future__ import annotations

import gzip
import importlib.util
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
spec = importlib.util.spec_from_file_location("check_upstream", ROOT / "scripts" / "check-upstream.py")
check_upstream = importlib.util.module_from_spec(spec)
sys.modules["check_upstream"] = check_upstream
assert spec.loader is not None
spec.loader.exec_module(check_upstream)
from alma_targets import alma_target


def repomd(primary_href: str = "repodata/primary.xml.gz") -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<repomd xmlns="http://linux.duke.edu/metadata/repo">\n'
        '  <data type="primary">\n'
        f'    <location href="{primary_href}"/>\n'
        '  </data>\n'
        '</repomd>\n'
    ).encode()


def primary(*evrs: str) -> bytes:
    packages = []
    for evr in evrs:
        version, release = evr.split("-", 1)
        packages.append(
            '\n  <package type="rpm">\n'
            '    <name>libvirt</name>\n'
            '    <arch>x86_64</arch>\n'
            f'    <version epoch="0" ver="{version}" rel="{release}"/>\n'
            '  </package>'
        )
    packages.append(
        '\n  <package type="rpm">\n'
        '    <name>not-libvirt</name>\n'
        '    <arch>x86_64</arch>\n'
        '    <version epoch="0" ver="99" rel="1"/>\n'
        '  </package>'
    )
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<metadata xmlns="http://linux.duke.edu/metadata/common">'
        + ''.join(packages)
        + '\n</metadata>\n'
    ).encode()
    return gzip.compress(xml)


class AlmaCandidateTests(unittest.TestCase):
    def test_selects_newest_libvirt_evr_from_unordered_metadata(self) -> None:
        payloads = {
            "http://mirror/almalinux/9/AppStream/x86_64/os/repodata/repomd.xml": repomd(),
            "http://mirror/almalinux/9/AppStream/x86_64/os/repodata/primary.xml.gz": primary(
                "11.10.0-12.1.el9_8.alma.1",
                "11.10.0-12.3.el9_8.alma.1",
                "11.10.0-12.el9_8.alma.1",
            ),
        }

        def fake_fetch(url: str) -> bytes:
            return payloads[url]

        config = {"mirrors": {"alma": "http://mirror/almalinux"}}
        target = alma_target(None, target_id="almalinux-9")
        with mock.patch.object(check_upstream, "fetch", side_effect=fake_fetch):
            result = check_upstream.alma_candidate(config, target)

        self.assertEqual(result.version, "11.10.0-12.3.el9_8.alma.1")
        self.assertEqual(result.distro, "almalinux_9")

    def test_rpm_version_key_orders_release_segments(self) -> None:
        versions = [
            "11.10.0-12.1.el9_8.alma.1",
            "11.10.0-12.3.el9_8.alma.1",
            "11.10.0-12.el9_8.alma.1",
        ]

        self.assertEqual(
            sorted(versions, key=check_upstream.rpm_version_key),
            [
                "11.10.0-12.el9_8.alma.1",
                "11.10.0-12.1.el9_8.alma.1",
                "11.10.0-12.3.el9_8.alma.1",
            ],
        )


if __name__ == "__main__":
    unittest.main()
