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
        self.assertIn("for source_file in /etc/apt/sources.list.d/*.sources", bootcmd)
        self.assertIn("grep -Eq '^URIs: https?://(archive|security|cloud\\.archive|ports)\\.ubuntu\\.com/ubuntu/?$'", bootcmd)
        self.assertIn("URIs: $mirror", bootcmd)
        self.assertIn("s@https?://(archive|security|cloud\\.archive|ports)\\.ubuntu\\.com/ubuntu@$mirror@g", bootcmd)
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
        self.assertLess(command.index("for source_file in /etc/apt/sources.list.d/*.sources"), command.index("apt-get update"))
        self.assertIn("s@https?://(archive|security|cloud\\.archive|ports)\\.ubuntu\\.com/ubuntu@$mirror@g", command)
        self.assertIn("grep -Eq '^URIs: https?://(archive|security|cloud\\.archive|ports)\\.ubuntu\\.com/ubuntu/?$'", command)
        self.assertIn("linux-modules-extra-$(uname -r)", command)


class SequentialLabTests(unittest.TestCase):
    def test_sequential_repo_test_creates_and_destroys_one_target_pair_at_a_time(self) -> None:
        config = {
            "lab": {
                "workdir": "/tmp/subvirt-lab",
                "name_prefix": "subvirt",
                "http_listen": "192.168.150.1:8080",
                "mirrors": {"ubuntu": "http://10.1.0.121/ubuntu"},
            },
            "repo": {"apt_suite": "noble", "component": "staging"},
            "truenas": {
                "api_key": "test-key",
                "management_ip": "192.168.150.50",
                "storage_ip": "192.168.151.50",
            },
            "tests": {
                "iscsi_truenas_pool": "cold",
                "nvmeof_truenas_pool": "hot1",
                "iscsi_pool_name": "cold",
                "nvmeof_pool_name": "hot1",
            },
            "vms": {
                "u24": {"name": "u24", "distro": "ubuntu", "suite": "noble", "management_ip": "192.168.150.24", "storage_ip": "192.168.151.24"},
                "u24_peer": {"name": "u24-peer", "distro": "ubuntu", "suite": "noble", "target_id": "ubuntu-24.04", "migration_peer_for": "u24", "management_ip": "192.168.150.25", "storage_ip": "192.168.151.25"},
                "alma9": {"name": "alma9", "distro": "alma", "version": "9", "management_ip": "192.168.150.19", "storage_ip": "192.168.151.19"},
            },
        }
        events: list[tuple[str, tuple[str, ...]]] = []

        def record(name: str):
            def inner(_lab: object, vm_keys=None, *_args):
                keys = tuple(vm_keys or ())
                events.append((name, keys))
            return inner

        with mock.patch.dict(lab.os.environ, {"SUBVIRT_UBUNTU_TARGETS": "ubuntu-24.04", "SUBVIRT_ALMA_TARGETS": "almalinux-9", "SUBVIRT_LAB_TARGETS": "selected"}, clear=False), \
             mock.patch.object(lab, "doctor_truenas"), \
             mock.patch.object(lab, "create_linux_vms", side_effect=record("create")), \
             mock.patch.object(lab, "wait_for_linux_vms", side_effect=record("wait")), \
             mock.patch.object(lab, "configure_linux_repos", side_effect=record("repos")), \
             mock.patch.object(lab, "configure_provider_configs", side_effect=record("provider")), \
             mock.patch.object(lab, "write_run_release_config", side_effect=record("write")), \
             mock.patch.object(lab, "destroy_linux_vms", side_effect=record("destroy")), \
             mock.patch.object(lab, "run"):
            lab.test_repo_sequential(lab.Lab(config=config, execute=False, build_id="test"), "full")

        self.assertEqual(events, [
            ("create", ("u24", "u24_peer")),
            ("write", ("u24",)),
            ("wait", ("u24", "u24_peer")),
            ("repos", ("u24", "u24_peer")),
            ("provider", ("u24", "u24_peer")),
            ("destroy", ("u24", "u24_peer")),
            ("create", ("alma9",)),
            ("write", ("alma9",)),
            ("wait", ("alma9",)),
            ("repos", ("alma9",)),
            ("provider", ("alma9",)),
            ("destroy", ("alma9",)),
        ])

    def test_destroy_linux_vms_removes_matching_seed_iso_path(self) -> None:
        config = {
            "lab": {"workdir": "/tmp/subvirt-lab", "name_prefix": "subvirt"},
            "vms": {"u24": {"name": "u24"}},
        }
        test_lab = lab.Lab(config=config, execute=False, build_id="test")
        events: list[str] = []

        def fake_print(message: object) -> None:
            events.append(str(message))

        with mock.patch.object(lab, "destroy_domain"), \
             mock.patch.object(lab, "print", side_effect=fake_print):
            lab.destroy_linux_vms(test_lab, ["u24"])

        domain_name = "subvirt-test-u24"
        self.assertEqual(lab.seed_iso_path(test_lab, domain_name), Path("/tmp/subvirt-lab/runs/test/subvirt-test-u24-seed.iso"))
        self.assertIn("+ rm -f /tmp/subvirt-lab/images/test-u24.qcow2", events)
        self.assertIn("+ rm -f /tmp/subvirt-lab/runs/test/subvirt-test-u24-seed.iso", events)


if __name__ == "__main__":
    unittest.main()
