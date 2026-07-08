#!/usr/bin/env python3
"""Shared Ubuntu LTS target definitions for Subvirt release automation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class UbuntuTarget:
    id: str
    version: str
    suite: str
    support_tier: str
    source_dir: str
    build_dir_prefix: str
    patch: str
    containerfile: str
    image: str


DEFAULT_TARGETS = [
    UbuntuTarget(
        id="ubuntu-18.04",
        version="18.04",
        suite="bionic",
        support_tier="esm",
        source_dir="u18",
        build_dir_prefix="libvirt-u18",
        patch="truenas-storage-backend-u24.patch",
        containerfile="containers/ubuntu-18.04-build/Containerfile",
        image="localhost/subvirt-ubuntu-18.04-build:latest",
    ),
    UbuntuTarget(
        id="ubuntu-20.04",
        version="20.04",
        suite="focal",
        support_tier="esm",
        source_dir="u20",
        build_dir_prefix="libvirt-u20",
        patch="truenas-storage-backend-u24.patch",
        containerfile="containers/ubuntu-20.04-build/Containerfile",
        image="localhost/subvirt-ubuntu-20.04-build:latest",
    ),
    UbuntuTarget(
        id="ubuntu-22.04",
        version="22.04",
        suite="jammy",
        support_tier="standard",
        source_dir="u22",
        build_dir_prefix="libvirt-u22",
        patch="truenas-storage-backend-u24.patch",
        containerfile="containers/ubuntu-22.04-build/Containerfile",
        image="localhost/subvirt-ubuntu-22.04-build:latest",
    ),
    UbuntuTarget(
        id="ubuntu-24.04",
        version="24.04",
        suite="noble",
        support_tier="standard",
        source_dir="u24",
        build_dir_prefix="libvirt-u24",
        patch="truenas-storage-backend-u24.patch",
        containerfile="containers/ubuntu-24.04-build/Containerfile",
        image="localhost/subvirt-ubuntu-24.04-build:latest",
    ),
    UbuntuTarget(
        id="ubuntu-26.04",
        version="26.04",
        suite="resolute",
        support_tier="standard",
        source_dir="u26",
        build_dir_prefix="libvirt-u26",
        patch="truenas-storage-backend-u24.patch",
        containerfile="containers/ubuntu-26.04-build/Containerfile",
        image="localhost/subvirt-ubuntu-26.04-build:latest",
    ),
]


def _merge_target(base: UbuntuTarget, override: dict[str, Any]) -> UbuntuTarget:
    data = base.__dict__.copy()
    data.update({key: value for key, value in override.items() if key in data})
    return UbuntuTarget(**data)


def ubuntu_targets(config: dict[str, Any] | None = None) -> list[UbuntuTarget]:
    upstream = (config or {}).get("upstream", {})
    configured = upstream.get("ubuntu_targets")
    defaults = {target.id: target for target in DEFAULT_TARGETS}
    if not configured:
        return list(DEFAULT_TARGETS)
    targets: list[UbuntuTarget] = []
    for item in configured:
        target_id = str(item["id"])
        base = defaults.get(target_id)
        if base is None:
            base = UbuntuTarget(
                id=target_id,
                version=str(item["version"]),
                suite=str(item["suite"]),
                support_tier=str(item.get("support_tier", "standard")),
                source_dir=str(item.get("source_dir", target_id.replace("ubuntu-", "u").replace(".", ""))),
                build_dir_prefix=str(item.get("build_dir_prefix", "libvirt-" + target_id.replace("ubuntu-", "u").replace(".", ""))),
                patch=str(item.get("patch", "truenas-storage-backend-u24.patch")),
                containerfile=str(item.get("containerfile", f"containers/{target_id}-build/Containerfile")),
                image=str(item.get("image", f"localhost/subvirt-{target_id}-build:latest")),
            )
        targets.append(_merge_target(base, item))
    return targets


def ubuntu_target(config: dict[str, Any] | None, target_id: str | None = None, suite: str | None = None) -> UbuntuTarget:
    targets = ubuntu_targets(config)
    if target_id:
        for target in targets:
            if target.id == target_id:
                return target
        raise SystemExit(f"unknown Ubuntu target: {target_id}")
    if suite:
        for target in targets:
            if target.suite == suite:
                return target
        raise SystemExit(f"unknown Ubuntu suite: {suite}")
    for target in targets:
        if target.id == "ubuntu-24.04":
            return target
    return targets[0]


def ubuntu_lock_key(target: UbuntuTarget) -> str:
    return target.id.replace("-", "_").replace(".", "_")


def parse_target_list(value: str, config: dict[str, Any] | None = None) -> list[UbuntuTarget]:
    targets = ubuntu_targets(config)
    by_id = {target.id: target for target in targets}
    by_suite = {target.suite: target for target in targets}
    if not value or value == "all":
        return targets
    if value == "standard":
        return [target for target in targets if target.support_tier == "standard"]
    if value == "esm":
        return [target for target in targets if target.support_tier == "esm"]
    selected: list[UbuntuTarget] = []
    for raw in value.split(","):
        item = raw.strip()
        if not item:
            continue
        target = by_id.get(item) or by_suite.get(item)
        if target is None:
            raise SystemExit(f"unknown Ubuntu target in list: {item}")
        if target not in selected:
            selected.append(target)
    return selected
