#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from unittest import mock

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

    def test_configure_linux_repos_rewrites_ubuntu_base_sources_before_update(self) -> None:
        config = {
            "lab": {
                "workdir": "/tmp/subvirt-lab",
                "http_listen": "192.168.150.1:8080",
                "mirrors": {"ubuntu": "http://10.1.0.121/ubuntu"},
            },
            "repo": {"apt_suite": "noble", "component": "staging"},
            "vms": {
                "u24": {
                    "distro": "ubuntu",
                    "suite": "noble",
                    "management_ip": "192.168.150.24",
                }
            },
        }
        calls: list[str] = []

        def fake_ssh(_target: str, command: str, _lab: object) -> str:
            calls.append(command)
            return ""

        with mock.patch.object(lab, "ssh", side_effect=fake_ssh), \
             mock.patch.object(lab, "wait_for_ssh"), \
             mock.patch.object(lab, "verify_provider_package"):
            lab.configure_linux_repos(lab.Lab(config=config, execute=False, build_id="test"), ["u24"])

        command = calls[0]
        self.assertLess(command.index("mirror=http://10.1.0.121/ubuntu"), command.index("apt-get update"))
        self.assertLess(command.index("/etc/apt/sources.list"), command.index("apt-get update"))
        self.assertLess(command.index("/etc/apt/sources.list.d/*.sources"), command.index("apt-get update"))
        self.assertIn("linux-modules-extra-$(uname -r)", command)


if __name__ == "__main__":
    unittest.main()
