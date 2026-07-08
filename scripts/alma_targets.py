#!/usr/bin/env python3
"""Shared AlmaLinux target definitions for Subvirt release automation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class AlmaTarget:
    id: str
    version: str
    source_dir: str
    repo_path: str
    yum_distro_path: str
    patch: str
    containerfile: str
    image: str
    mock_config: str


DEFAULT_TARGETS = [
    AlmaTarget(
        id="almalinux-9",
        version="9",
        source_dir="al9",
        repo_path="9/AppStream/x86_64/os",
        yum_distro_path="almalinux/9",
        patch="truenas-storage-backend-al10.patch",
        containerfile="containers/almalinux-9-build/Containerfile",
        image="localhost/subvirt-almalinux-9-build:latest",
        mock_config="almalinux-9-x86_64",
    ),
    AlmaTarget(
        id="almalinux-10",
        version="10",
        source_dir="al10",
        repo_path="10/AppStream/x86_64_v2/os",
        yum_distro_path="almalinux/10",
        patch="truenas-storage-backend-al10.patch",
        containerfile="containers/almalinux-10-build/Containerfile",
        image="localhost/subvirt-almalinux-10-build:latest",
        mock_config="almalinux-10-x86_64",
    ),
]


def _merge_target(base: AlmaTarget, override: dict[str, Any]) -> AlmaTarget:
    data = base.__dict__.copy()
    data.update({key: value for key, value in override.items() if key in data})
    return AlmaTarget(**data)


def alma_targets(config: dict[str, Any] | None = None) -> list[AlmaTarget]:
    upstream = (config or {}).get("upstream", {})
    configured = upstream.get("alma_targets")
    defaults = {target.id: target for target in DEFAULT_TARGETS}
    if not configured:
        return list(DEFAULT_TARGETS)
    targets: list[AlmaTarget] = []
    for item in configured:
        target_id = str(item["id"])
        base = defaults.get(target_id)
        if base is None:
            version = str(item["version"])
            base = AlmaTarget(
                id=target_id,
                version=version,
                source_dir=str(item.get("source_dir", f"al{version}")),
                repo_path=str(item.get("repo_path", f"{version}/AppStream/x86_64/os")),
                yum_distro_path=str(item.get("yum_distro_path", f"almalinux/{version}")),
                patch=str(item.get("patch", "truenas-storage-backend-al10.patch")),
                containerfile=str(item.get("containerfile", f"containers/{target_id}-build/Containerfile")),
                image=str(item.get("image", f"localhost/subvirt-{target_id}-build:latest")),
                mock_config=str(item.get("mock_config", f"{target_id}-x86_64")),
            )
        targets.append(_merge_target(base, item))
    return targets


def alma_target(config: dict[str, Any] | None, target_id: str | None = None, version: str | None = None) -> AlmaTarget:
    targets = alma_targets(config)
    if target_id:
        for target in targets:
            if target.id == target_id:
                return target
        raise SystemExit(f"unknown AlmaLinux target: {target_id}")
    if version:
        for target in targets:
            if target.version == version:
                return target
        raise SystemExit(f"unknown AlmaLinux version: {version}")
    for target in targets:
        if target.id == "almalinux-10":
            return target
    return targets[-1]


def alma_lock_key(target: AlmaTarget) -> str:
    return target.id.replace("-", "_").replace(".", "_")


def parse_target_list(value: str, config: dict[str, Any] | None = None) -> list[AlmaTarget]:
    targets = alma_targets(config)
    by_id = {target.id: target for target in targets}
    by_version = {target.version: target for target in targets}
    if not value or value == "almalinux-10":
        return [alma_target(config, target_id="almalinux-10")]
    if value == "all":
        return targets
    selected: list[AlmaTarget] = []
    for raw in value.split(","):
        item = raw.strip()
        if not item:
            continue
        target = by_id.get(item) or by_version.get(item)
        if target is None:
            raise SystemExit(f"unknown AlmaLinux target in list: {item}")
        if target not in selected:
            selected.append(target)
    return selected
