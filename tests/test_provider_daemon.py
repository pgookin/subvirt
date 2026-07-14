#!/usr/bin/env python3
from __future__ import annotations

import json
import unittest

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


if __name__ == "__main__":
    unittest.main()
