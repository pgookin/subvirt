#!/usr/bin/env python3
from __future__ import annotations

import json
import unittest
from unittest import mock

from truenas_provider_daemon import TrueNASLibvirtProvider


SUBNQN = "nqn.2011-06.com.truenas:test-subsystem"


class NvmeSubsystemDevnameTests(unittest.TestCase):
    def test_modern_nested_list_subsys_json(self) -> None:
        output = json.dumps([
            {
                "HostNQN": "nqn.host",
                "Subsystems": [
                    {
                        "Name": "nvme-subsys0",
                        "NQN": SUBNQN,
                        "Paths": [
                            {"Name": "nvme0", "Transport": "tcp", "Address": "traddr=192.0.2.10 trsvcid=4420"}
                        ],
                    }
                ],
            }
        ])

        self.assertEqual(TrueNASLibvirtProvider._nvme_subsystem_devnames(output, SUBNQN), ["nvme0n1"])

    def test_ubuntu_18_split_list_subsys_json(self) -> None:
        output = json.dumps({
            "Subsystems": [
                {
                    "Name": "nvme-subsys0",
                    "NQN": SUBNQN,
                },
                {
                    "Paths": [
                        {"Name": "nvme0", "Transport": "tcp", "Address": "traddr=192.0.2.10 trsvcid=4420"}
                    ]
                },
            ]
        })

        self.assertEqual(TrueNASLibvirtProvider._nvme_subsystem_devnames(output, SUBNQN), ["nvme0n1"])

    def test_namespace_name_is_preserved(self) -> None:
        output = json.dumps({
            "Subsystems": [
                {
                    "Name": "nvme-subsys0",
                    "NQN": SUBNQN,
                    "Paths": [{"Name": "/dev/nvme0n2"}],
                }
            ]
        })

        self.assertEqual(TrueNASLibvirtProvider._nvme_subsystem_devnames(output, SUBNQN), ["nvme0n2"])

    def test_nonmatching_subsystem_is_ignored(self) -> None:
        output = json.dumps({
            "Subsystems": [
                {"Name": "nvme-subsys0", "NQN": "nqn.other"},
                {"Paths": [{"Name": "nvme0"}]},
            ]
        })

        self.assertEqual(TrueNASLibvirtProvider._nvme_subsystem_devnames(output, SUBNQN), [])


class NvmeConnectAlreadyActiveTests(unittest.TestCase):
    def test_duplicate_connect_messages_are_idempotent(self) -> None:
        messages = [
            "already connected",
            "Duplicate connect",
            "Failed to write to /dev/nvme-fabrics: Operation already in progress",
        ]

        for message in messages:
            with self.subTest(message=message):
                self.assertTrue(TrueNASLibvirtProvider._nvme_connect_already_active(message))

    def test_unrelated_connect_failure_is_not_idempotent(self) -> None:
        self.assertFalse(TrueNASLibvirtProvider._nvme_connect_already_active("connection timed out"))


class NvmeConnectTests(unittest.TestCase):
    def test_failed_connect_is_ok_when_subsystem_is_already_connected(self) -> None:
        provider = TrueNASLibvirtProvider.__new__(TrueNASLibvirtProvider)
        list_subsys = json.dumps({
            "Subsystems": [
                {"Name": "nvme-subsys0", "NQN": SUBNQN},
                {"Paths": [{"Name": "nvme0"}]},
            ]
        })
        results = [
            mock.Mock(returncode=114, stderr="", stdout=""),
            mock.Mock(returncode=0, stderr="", stdout=list_subsys),
        ]

        with mock.patch("truenas_provider_daemon.run_command", side_effect=results):
            provider._connect_nvme({"subnqn": SUBNQN, "traddr": "192.0.2.10", "trsvcid": "4420"})

    def test_failed_connect_raises_when_subsystem_is_not_connected(self) -> None:
        provider = TrueNASLibvirtProvider.__new__(TrueNASLibvirtProvider)
        results = [
            mock.Mock(returncode=1, stderr="connection failed", stdout=""),
            mock.Mock(returncode=0, stderr="", stdout=json.dumps({"Subsystems": []})),
        ]

        with mock.patch("truenas_provider_daemon.run_command", side_effect=results):
            with self.assertRaises(Exception):
                provider._connect_nvme({"subnqn": SUBNQN, "traddr": "192.0.2.10", "trsvcid": "4420"})


if __name__ == "__main__":
    unittest.main()
