#!/usr/bin/env python3
"""Unix-socket JSON-RPC daemon for the TrueNAS libvirt storage provider."""

import argparse
import hashlib
import json
import os
import re
import socket
import ssl
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from truenas_provider import (
    ConfigError,
    LOCAL_ISCSI_INITIATOR_FILE,
    LOCAL_NVME_HOSTNQN_FILE,
    JsonRpcWebSocket,
    managed_export_name,
    object_ref_id,
    WebSocketError,
    dataset_exists,
    ensure_iscsi_extent,
    ensure_iscsi_initiator_group,
    ensure_iscsi_mapping,
    ensure_iscsi_portal,
    ensure_iscsi_target,
    ensure_nvmet_host,
    ensure_nvmet_host_subsys,
    ensure_nvmet_namespace,
    ensure_nvmet_port,
    get_nvmet_subsys,
    ensure_nvmet_port_subsys,
    ensure_nvmet_subsys,
    ensure_service_enabled_and_running,
    load_config,
    local_iscsi_iqn,
    local_nvme_nqn,
    login,
    managed_dataset_name,
    managed_zvol_name,
    open_client,
    parse_size,
    query_all,
)

DEFAULT_CONFIG = "/etc/truenas-libvirt/config.json"
DEFAULT_SOCKET = "/run/truenas-libvirt/provider.sock"
DEFAULT_TIMEOUT = 30
TRANSPORTS = ("iscsi", "nvmeof")
BASE_TRUENAS_API_METHODS = (
    "system.info",
    "pool.query",
    "pool.dataset.query",
    "pool.dataset.create",
    "pool.dataset.update",
    "pool.dataset.delete",
    "pool.snapshot.query",
    "pool.snapshot.create",
    "pool.snapshot.clone",
    "pool.snapshot.delete",
    "service.query",
    "service.update",
    "service.start",
    "service.reload",
)
ISCSI_TRUENAS_API_METHODS = (
    "iscsi.portal.query",
    "iscsi.portal.create",
    "iscsi.initiator.query",
    "iscsi.initiator.create",
    "iscsi.initiator.update",
    "iscsi.target.query",
    "iscsi.target.create",
    "iscsi.target.update",
    "iscsi.target.delete",
    "iscsi.extent.query",
    "iscsi.extent.create",
    "iscsi.extent.delete",
    "iscsi.targetextent.query",
    "iscsi.targetextent.create",
    "iscsi.targetextent.delete",
)
NVMEOF_TRUENAS_API_METHODS = (
    "nvmet.host.query",
    "nvmet.host.create",
    "nvmet.host_subsys.query",
    "nvmet.host_subsys.create",
    "nvmet.host_subsys.delete",
    "nvmet.namespace.query",
    "nvmet.namespace.create",
    "nvmet.namespace.delete",
    "nvmet.port.query",
    "nvmet.port.create",
    "nvmet.port_subsys.query",
    "nvmet.port_subsys.create",
    "nvmet.port_subsys.delete",
    "nvmet.subsys.query",
    "nvmet.subsys.create",
    "nvmet.subsys.delete",
)


class ProviderError(RuntimeError):
    def __init__(self, code: str, message: str, data: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(message)
        self.code = code
        self.data = data or {}


def run_command(argv: List[str], check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(argv, universal_newlines=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if check and result.returncode != 0:
        raise ProviderError(
            "command_failed",
            f"command failed: {' '.join(argv)}",
            {"returncode": result.returncode, "stdout": result.stdout, "stderr": result.stderr},
        )
    return result


def tool_exists(name: str) -> bool:
    return run_command(["/usr/bin/env", "sh", "-c", f"command -v {name}"], check=False).returncode == 0


def systemd_active(*units: str) -> bool:
    for unit in units:
        result = run_command(["systemctl", "is-active", "--quiet", unit], check=False)
        if result.returncode == 0:
            return True
    return False


def readable_nonempty(path: str) -> bool:
    try:
        return bool(Path(path).read_text(encoding="utf-8").strip())
    except OSError:
        return False


def tcp_reachable(host: str, port: int, timeout: float = 3.0) -> Dict[str, Any]:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return {"ok": True, "host": host, "port": port}
    except OSError as exc:
        return {"ok": False, "host": host, "port": port, "error": str(exc)}


def kernel_module_available(name: str) -> bool:
    if Path(f"/sys/module/{name.replace('-', '_')}").exists():
        return True
    modinfo = "/usr/sbin/modinfo" if Path("/usr/sbin/modinfo").exists() else "modinfo"
    return run_command([modinfo, name], check=False).returncode == 0


def transport_status() -> Dict[str, Any]:
    nvme_tcp_available = kernel_module_available("nvme-tcp")
    iscsi = {
        "iscsiadm": tool_exists("iscsiadm"),
        "initiator_file": LOCAL_ISCSI_INITIATOR_FILE,
        "initiator_configured": readable_nonempty(LOCAL_ISCSI_INITIATOR_FILE),
        "iscsid_active": systemd_active("iscsid.service"),
    }
    nvme = {
        "nvme": tool_exists("nvme"),
        "hostnqn_file": LOCAL_NVME_HOSTNQN_FILE,
        "hostnqn_configured": readable_nonempty(LOCAL_NVME_HOSTNQN_FILE),
        "nvme_tcp_loaded": Path("/sys/module/nvme_tcp").exists(),
        "nvme_tcp_available": nvme_tcp_available,
    }
    iscsi["ok"] = bool(iscsi["iscsiadm"] and iscsi["initiator_configured"] and iscsi["iscsid_active"])
    nvme["ok"] = bool(nvme["nvme"] and nvme["hostnqn_configured"] and nvme["nvme_tcp_available"])
    return {"iscsi": iscsi, "nvmeof": nvme}


def doctor_check(
    name: str,
    ok: bool,
    message: str,
    data: Optional[Dict[str, Any]] = None,
    required: bool = True,
) -> Dict[str, Any]:
    item = {"name": name, "ok": bool(ok), "required": required, "message": message}
    if data:
        item["data"] = data
    return item


def required_truenas_api_methods(transport: str) -> Tuple[str, ...]:
    methods = list(BASE_TRUENAS_API_METHODS)
    if transport in ("all", "iscsi"):
        methods.extend(ISCSI_TRUENAS_API_METHODS)
    if transport in ("all", "nvmeof"):
        methods.extend(NVMEOF_TRUENAS_API_METHODS)
    return tuple(dict.fromkeys(methods))


class TrueNASLibvirtProvider:
    def __init__(self, config_path: str) -> None:
        self.config_path = config_path
        self.config = load_config(config_path)

    def _client(self) -> JsonRpcWebSocket:
        return open_client(self.config)

    def _target_ip(self, override: Optional[str] = None) -> str:
        truenas = self.config["truenas"]
        assert isinstance(truenas, dict)
        target_ip = override or truenas.get("target_ip")
        if not isinstance(target_ip, str) or not target_ip:
            raise ProviderError("config_invalid", "TrueNAS storage target_ip is required in provider config")
        return target_ip

    def _target_name(self, volume: str) -> str:
        return managed_export_name(volume)

    def _require_transport_ready(self, transport: str) -> None:
        status = transport_status()
        if transport == "iscsi":
            iscsi = status["iscsi"]
            if not iscsi["iscsiadm"]:
                raise ProviderError("iscsi_unavailable", "iSCSI transport requires iscsiadm; install open-iscsi or iscsi-initiator-utils", iscsi)
            if not iscsi["initiator_configured"]:
                raise ProviderError("iscsi_unavailable", f"iSCSI transport requires InitiatorName in {LOCAL_ISCSI_INITIATOR_FILE}", iscsi)
            if not iscsi["iscsid_active"]:
                raise ProviderError("iscsi_unavailable", "iSCSI transport requires iscsid.service to be enabled and running", iscsi)
            return
        if transport == "nvmeof":
            nvme = status["nvmeof"]
            if not nvme["nvme"]:
                raise ProviderError("nvmeof_unavailable", "NVMe-oF transport requires nvme-cli", nvme)
            if not nvme["hostnqn_configured"]:
                raise ProviderError("nvmeof_unavailable", f"NVMe-oF transport requires a host NQN in {LOCAL_NVME_HOSTNQN_FILE}", nvme)
            if not nvme["nvme_tcp_loaded"]:
                modprobe = "/usr/sbin/modprobe" if Path("/usr/sbin/modprobe").exists() else "modprobe"
                result = run_command([modprobe, "nvme-tcp"], check=False)
                if result.returncode != 0 or not Path("/sys/module/nvme_tcp").exists():
                    nvme["modprobe_stderr"] = result.stderr
                    nvme["modprobe_stdout"] = result.stdout
                    raise ProviderError("nvmeof_unavailable", "NVMe-oF transport requires the nvme-tcp kernel module", nvme)
            return
        raise ProviderError("transport_invalid", f"unsupported transport: {transport}")

    def _pool_name_from_zvol(self, pool: str, zvol: str) -> str:
        prefix = managed_dataset_name(pool, self.config) + "/"
        if not zvol.startswith(prefix):
            return zvol
        return zvol[len(prefix):]

    def _volume_from_dataset(self, pool: str, item: Dict[str, Any]) -> Dict[str, Any]:
        properties = item.get("properties") or item
        user_properties = item.get("user_properties", {})
        transport = None
        if isinstance(user_properties, dict):
            value = user_properties.get("org.libvirt:transport")
            if isinstance(value, dict):
                transport = value.get("value") or value.get("rawvalue")
        return {
            "name": self._pool_name_from_zvol(pool, str(item["name"])),
            "zvol": item["name"],
            "type": item.get("type"),
            "transport": transport,
            "capacity": self._property_parsed(properties, "volsize"),
            "allocation": self._property_parsed(properties, "used"),
        }

    @staticmethod
    def _property_parsed(properties: Any, name: str) -> Optional[int]:
        if not isinstance(properties, dict):
            return None
        value = properties.get(name)
        if isinstance(value, dict):
            parsed = value.get("parsed")
            if isinstance(parsed, int):
                return parsed
        return None

    def _ensure_namespace(self, client: JsonRpcWebSocket, pool: str) -> None:
        dataset = managed_dataset_name(pool, self.config)
        if dataset_exists(client, dataset):
            return
        client.call(
            "pool.dataset.create",
            [{
                "name": dataset,
                "type": "FILESYSTEM",
                "share_type": "GENERIC",
                "create_ancestors": True,
                "managedby": "truenas-libvirt-provider",
                "user_properties": [{"key": "org.libvirt:managed", "value": "true"}],
            }],
        )

    def _create_zvol(self, client: JsonRpcWebSocket, pool: str, name: str, capacity: Any, transport: str) -> Dict[str, Any]:
        self._ensure_namespace(client, pool)
        zvol = managed_zvol_name(pool, name, self.config)
        if dataset_exists(client, zvol):
            raise ProviderError("volume_exists", f"zvol already exists: {zvol}")
        result = client.call(
            "pool.dataset.create",
            [{
                "name": zvol,
                "type": "VOLUME",
                "volsize": parse_size(str(capacity)),
                "volblocksize": "16K",
                "sparse": True,
                "managedby": "truenas-libvirt-provider",
                "user_properties": [
                    {"key": "org.libvirt:managed", "value": "true"},
                    {"key": "org.libvirt:transport", "value": transport},
                ],
            }],
        )
        assert isinstance(result, dict)
        return result

    def _get_zvol(self, client: JsonRpcWebSocket, pool: str, name: str) -> Dict[str, Any]:
        zvol = managed_zvol_name(pool, name, self.config)
        rows = client.call("pool.dataset.query", [[["name", "=", zvol]], {"extra": {"properties": ["volsize", "used"]}}])
        if not isinstance(rows, list) or not rows:
            raise ProviderError("volume_missing", f"zvol does not exist: {zvol}")
        row = rows[0]
        if not isinstance(row, dict):
            raise ProviderError("volume_invalid", f"invalid zvol response for: {zvol}")
        return row

    def _resize_zvol(self, client: JsonRpcWebSocket, pool: str, name: str, capacity: Any) -> Dict[str, Any]:
        zvol = managed_zvol_name(pool, name, self.config)
        requested = int(capacity)
        row = self._get_zvol(client, pool, name)
        current = self._property_parsed(row.get("properties") or row, "volsize")
        if current is not None and requested < current:
            raise ProviderError("resize_shrink_unsupported", "TrueNAS zvol resize is grow-only; shrinking is not supported", {"zvol": zvol, "current": current, "requested": requested})
        client.call("pool.dataset.update", [zvol, {"volsize": requested}])
        return self._get_zvol(client, pool, name)

    def _wait_for_zvol(self, client: JsonRpcWebSocket, pool: str, name: str, timeout: float = 10.0) -> Dict[str, Any]:
        deadline = time.monotonic() + timeout
        last_error: Optional[ProviderError] = None
        while time.monotonic() < deadline:
            try:
                return self._get_zvol(client, pool, name)
            except ProviderError as exc:
                last_error = exc
                time.sleep(0.25)
        if last_error is not None:
            raise last_error
        return self._get_zvol(client, pool, name)

    def _clone_snapshot_name(self, source: str, target: str) -> str:
        stamp = str(int(time.time()))
        digest = hashlib.sha256(f"{source}\0{target}\0{stamp}".encode("utf-8")).hexdigest()[:16]
        return f"subvirt-clone-{digest}"

    @staticmethod
    def _snapshot_name(snapshot: Dict[str, Any]) -> str:
        value = snapshot.get("name") or snapshot.get("id")
        return str(value or "")

    @staticmethod
    def _snapshot_short_name(snapshot: Dict[str, Any]) -> str:
        name = TrueNASLibvirtProvider._snapshot_name(snapshot)
        return name.rsplit("@", 1)[-1] if "@" in name else name

    def _list_zvol_snapshots(self, client: JsonRpcWebSocket, zvol: str) -> List[Dict[str, Any]]:
        rows = client.call("pool.snapshot.query", [[["name", "^", f"{zvol}@"]], {"order_by": ["name"]}])
        if not isinstance(rows, list):
            raise ProviderError("snapshot_query_invalid", f"invalid snapshot response for: {zvol}")
        return [row for row in rows if isinstance(row, dict)]

    def _safe_delete_dataset(self, client: JsonRpcWebSocket, dataset: str) -> None:
        methods = client.call("core.get_methods", [])
        assert isinstance(methods, dict)
        if "pool.dataset.delete" not in methods:
            raise ProviderError("dataset_delete_unavailable", "TrueNAS API user cannot delete zvols with the currently exposed methods", {"zvol": dataset})
        client.call("pool.dataset.delete", [dataset, {"recursive": False, "force": False}])

    def _delete_managed_snapshots(self, client: JsonRpcWebSocket, zvol: str, snapshots: List[Dict[str, Any]]) -> None:
        blocking = []
        for snapshot in snapshots:
            name = self._snapshot_name(snapshot)
            short_name = self._snapshot_short_name(snapshot)
            if not short_name.startswith("subvirt-clone-"):
                blocking.append(name)
                continue
            try:
                client.call("pool.snapshot.delete", [name, {"recursive": False, "defer": False}])
            except WebSocketError as exc:
                raise ProviderError("snapshot_delete_failed", f"failed to delete managed snapshot {name}: {exc}", {"zvol": zvol, "snapshot": name}) from None
        if blocking:
            raise ProviderError("delete_blocked_by_snapshots", "volume has unmanaged snapshots; refusing delete", {"zvol": zvol, "snapshots": blocking})

    def _clone_zvol(self, client: JsonRpcWebSocket, pool: str, source: str, target: str, transport: str, replace_existing: bool) -> Dict[str, Any]:
        source_zvol = managed_zvol_name(pool, source, self.config)
        target_zvol = managed_zvol_name(pool, target, self.config)
        if not dataset_exists(client, source_zvol):
            raise ProviderError("volume_missing", f"source zvol does not exist: {source_zvol}")
        target_exists = dataset_exists(client, target_zvol)
        if target_exists and not replace_existing:
            raise ProviderError("volume_exists", f"target zvol already exists: {target_zvol}")
        if target_exists:
            if transport == "iscsi":
                self._disconnect_iscsi(target)
                self._cleanup_iscsi_export(client, target)
            elif transport == "nvmeof":
                self._disconnect_nvme(target)
                self._cleanup_nvme_export(client, target)
            else:
                raise ProviderError("transport_invalid", f"unsupported transport: {transport}")
            self._safe_delete_dataset(client, target_zvol)

        snapshot_name = self._clone_snapshot_name(source, target)
        snapshot_id = f"{source_zvol}@{snapshot_name}"
        client.call("pool.snapshot.create", [{"dataset": source_zvol, "name": snapshot_name, "recursive": False}])
        try:
            client.call("pool.snapshot.clone", [{"snapshot": snapshot_id, "dataset_dst": target_zvol}])
        except WebSocketError as first_exc:
            try:
                client.call("pool.snapshot.clone", [snapshot_id, {"dataset_dst": target_zvol}])
            except WebSocketError:
                try:
                    client.call("pool.snapshot.clone", [snapshot_id, target_zvol])
                except WebSocketError:
                    try:
                        client.call("pool.snapshot.delete", [snapshot_id, {"recursive": False, "defer": False}])
                    except WebSocketError:
                        pass
                    raise ProviderError("clone_failed", f"failed to clone {snapshot_id} to {target_zvol}: {first_exc}", {"source": source_zvol, "target": target_zvol, "snapshot": snapshot_id}) from None
        self._wait_for_zvol(client, pool, target)
        try:
            client.call(
                "pool.dataset.update",
                [
                    target_zvol,
                    {
                        "managedby": "truenas-libvirt-provider",
                        "user_properties_update": [
                            {"key": "org.libvirt:managed", "value": "true"},
                            {"key": "org.libvirt:transport", "value": transport},
                            {"key": "org.libvirt:clone-source", "value": source_zvol},
                            {"key": "org.libvirt:clone-snapshot", "value": snapshot_id},
                        ],
                    },
                ],
            )
        except WebSocketError as exc:
            raise ProviderError("clone_metadata_failed", f"failed to tag cloned zvol {target_zvol}: {exc}", {"target": target_zvol, "transport": transport}) from None
        return self._get_zvol(client, pool, target)

    def _list_zvols(self, client: JsonRpcWebSocket, pool: str, transport: Optional[str] = None) -> List[Dict[str, Any]]:
        prefix = managed_dataset_name(pool, self.config) + "/"
        rows = client.call("pool.dataset.query", [[["type", "=", "VOLUME"], ["name", "^", prefix]], {"order_by": ["name"]}])
        assert isinstance(rows, list)
        volumes = [self._volume_from_dataset(pool, row) for row in rows if isinstance(row, dict)]
        if transport:
            volumes = [vol for vol in volumes if vol.get("transport") in (None, transport)]
        return volumes

    def _pool_space(self, client: JsonRpcWebSocket, pool: str) -> Dict[str, int]:
        dataset = managed_dataset_name(pool, self.config)
        rows = client.call(
            "pool.dataset.query",
            [[["name", "=", dataset]], {"select": ["name", "available", "used"]}],
        )
        if not isinstance(rows, list) or not rows:
            raise ProviderError("dataset_missing", f"managed dataset does not exist: {dataset}")
        row = rows[0]
        if not isinstance(row, dict):
            raise ProviderError("dataset_invalid", f"invalid dataset response for: {dataset}")
        available = self._property_parsed(row, "available")
        allocation = self._property_parsed(row, "used")
        if available is None or allocation is None:
            raise ProviderError("dataset_invalid", f"dataset space properties are missing: {dataset}")
        return {
            "name": dataset,
            "capacity": available + allocation,
            "allocation": allocation,
            "available": available,
        }

    def _iscsi_export(self, client: JsonRpcWebSocket, pool: str, name: str, target_ip: str) -> Dict[str, Any]:
        zvol = managed_zvol_name(pool, name, self.config)
        if not dataset_exists(client, zvol):
            raise ProviderError("volume_missing", f"zvol does not exist: {zvol}")
        target_name = self._target_name(name)
        portal, _ = ensure_iscsi_portal(client, target_ip)
        initiator, _ = ensure_iscsi_initiator_group(client, [local_iscsi_iqn()])
        target, _ = ensure_iscsi_target(client, target_name, zvol, int(portal["id"]), int(initiator["id"]), [local_iscsi_iqn()])
        extent, _ = ensure_iscsi_extent(client, target_name, zvol)
        mapping, _ = ensure_iscsi_mapping(client, int(target["id"]), int(extent["id"]))
        service = ensure_service_enabled_and_running(client, "iscsitarget")
        return {
            "service": service,
            "portal": f"{target_ip}:3260,1",
            "target_iqn": f"iqn.2005-10.org.freenas.ctl:{target.get('name', target_name)}",
            "lun": mapping.get("lunid", 0),
            "naa": extent.get("naa"),
            "serial": extent.get("serial"),
        }

    def _nvme_export(self, client: JsonRpcWebSocket, pool: str, name: str, target_ip: str, port_number: int = 4420) -> Dict[str, Any]:
        zvol = managed_zvol_name(pool, name, self.config)
        if not dataset_exists(client, zvol):
            raise ProviderError("volume_missing", f"zvol does not exist: {zvol}")
        subsys, _ = ensure_nvmet_subsys(client, self._target_name(name))
        namespace, _ = ensure_nvmet_namespace(client, int(subsys["id"]), zvol)
        namespace_subsys_id = object_ref_id(namespace.get("subsys"))
        if namespace_subsys_id is not None and namespace_subsys_id != int(subsys["id"]):
            subsys = get_nvmet_subsys(client, namespace_subsys_id)
        port, _ = ensure_nvmet_port(client, target_ip, port_number)
        ensure_nvmet_port_subsys(client, int(port["id"]), int(subsys["id"]))
        host, _ = ensure_nvmet_host(client, local_nvme_nqn())
        ensure_nvmet_host_subsys(client, int(host["id"]), int(subsys["id"]))
        service = ensure_service_enabled_and_running(client, "nvmet")
        return {
            "service": service,
            "target": f"{target_ip}:{port_number}",
            "traddr": target_ip,
            "trsvcid": port_number,
            "subnqn": subsys.get("subnqn"),
            "namespace_id": namespace.get("id"),
        }

    def _connect_iscsi(self, export: Dict[str, Any]) -> None:
        portal = str(export["portal"])
        target_iqn = str(export["target_iqn"])
        result = None
        for _attempt in range(30):
            run_command(["iscsiadm", "-m", "discovery", "-t", "sendtargets", "-p", portal], check=False)
            run_command(["iscsiadm", "-m", "node", "-T", target_iqn, "-p", portal, "--op", "new"], check=False)
            result = run_command(["iscsiadm", "-m", "node", "-T", target_iqn, "-p", portal, "--login"], check=False)
            if result.returncode == 0 or "already present" in result.stderr or "already exists" in result.stderr:
                return
            time.sleep(1)
        assert result is not None
        raise ProviderError("iscsi_login_failed", "iSCSI login failed", {"stderr": result.stderr, "stdout": result.stdout})

    def _connect_nvme(self, export: Dict[str, Any]) -> None:
        subnqn = str(export["subnqn"])
        result = run_command(["nvme", "connect", "-t", "tcp", "-a", str(export["traddr"]), "-s", str(export["trsvcid"]), "-n", subnqn], check=False)
        if result.returncode == 0 or self._nvme_connect_already_active(result.stderr):
            return
        if self._nvme_subsystem_connected(subnqn):
            return
        raise ProviderError("nvme_connect_failed", "NVMe-oF connect failed", {"returncode": result.returncode, "stderr": result.stderr, "stdout": result.stdout, "subnqn": subnqn})

    @staticmethod
    def _nvme_connect_already_active(stderr: str) -> bool:
        normalized = stderr.lower()
        return (
            "already connected" in normalized
            or "duplicate connect" in normalized
            or "operation already in progress" in normalized
        )

    def _nvme_subsystem_connected(self, subnqn: str) -> bool:
        result = run_command(["nvme", "list-subsys", "-o", "json"], check=False)
        return result.returncode == 0 and bool(self._nvme_subsystem_devnames(result.stdout, subnqn))

    def _find_by_id(self, prefixes: List[str], timeout: int = DEFAULT_TIMEOUT) -> str:
        deadline = time.time() + timeout
        by_id = Path("/dev/disk/by-id")
        while time.time() < deadline:
            if by_id.exists():
                for entry in sorted(by_id.iterdir()):
                    name = entry.name
                    if any(name.startswith(prefix) or name == prefix for prefix in prefixes):
                        return str(entry)
            time.sleep(0.5)
        raise ProviderError("path_not_found", "stable /dev/disk/by-id path was not found", {"prefixes": prefixes})

    def _iscsi_path(self, export: Dict[str, Any]) -> str:
        naa = str(export.get("naa") or "")
        serial = str(export.get("serial") or "")
        prefixes = []
        if naa.startswith("0x"):
            prefixes.append(f"wwn-{naa}")
            prefixes.append(f"scsi-3{naa[2:]}")
        if serial:
            prefixes.append(f"scsi-STrueNAS_iSCSI_Disk_{serial}")
        return self._find_by_id(prefixes)

    def _nvme_path(self, export: Dict[str, Any]) -> str:
        subnqn = str(export["subnqn"])
        deadline = time.time() + DEFAULT_TIMEOUT
        discovered_paths = []
        while time.time() < deadline:
            run_command(["udevadm", "settle"], check=False)
            result = run_command(["nvme", "list-subsys", "-o", "json"], check=False)
            if result.returncode == 0:
                discovered_paths = self._nvme_subsystem_devnames(result.stdout, subnqn)
                for devname in discovered_paths:
                    match = self._find_by_id_target(devname)
                    if match:
                        return match
            time.sleep(0.5)
        raise ProviderError("path_not_found", "stable NVMe /dev/disk/by-id path was not found", {"subnqn": subnqn, "devnames": discovered_paths})

    @staticmethod
    def _nvme_subsystem_devnames(output: str, subnqn: str) -> List[str]:
        try:
            parsed = json.loads(output)
        except json.JSONDecodeError:
            return []

        subsystem_groups = []
        if isinstance(parsed, dict):
            subsystem_groups.append(parsed.get("Subsystems", []))
        elif isinstance(parsed, list):
            for host in parsed:
                if isinstance(host, dict):
                    subsystem_groups.append(host.get("Subsystems", []))

        devnames = []
        for subsystems in subsystem_groups:
            if not isinstance(subsystems, list):
                continue
            current_matches = False
            for subsystem in subsystems:
                if not isinstance(subsystem, dict):
                    continue
                if "NQN" in subsystem:
                    current_matches = subsystem.get("NQN") == subnqn
                if not current_matches:
                    continue
                paths = subsystem.get("Paths", [])
                if not isinstance(paths, list):
                    continue
                for path in paths:
                    if not isinstance(path, dict) or not path.get("Name"):
                        continue
                    devname = Path(str(path["Name"])).name
                    if re.match(r"^nvme[0-9]+$", devname):
                        devname = f"{devname}n1"
                    if devname not in devnames:
                        devnames.append(devname)
        return devnames

    @staticmethod
    def _find_by_id_target(devname: str) -> Optional[str]:
        devname = Path(devname).name
        by_id = Path("/dev/disk/by-id")
        if not by_id.exists():
            return None
        for entry in sorted(by_id.iterdir()):
            name = entry.name
            if not (name.startswith("nvme-uuid.") or name.startswith("nvme-TrueNAS_")):
                continue
            try:
                if entry.resolve().name == devname:
                    return str(entry)
            except FileNotFoundError:
                continue
        return None

    def _export(self, client: JsonRpcWebSocket, pool: str, name: str, transport: str) -> Dict[str, Any]:
        target_ip = self._target_ip()
        if transport == "iscsi":
            return self._iscsi_export(client, pool, name, target_ip)
        if transport == "nvmeof":
            return self._nvme_export(client, pool, name, target_ip)
        raise ProviderError("transport_invalid", f"unsupported transport: {transport}")

    def _connect_and_path(self, export: Dict[str, Any], transport: str) -> str:
        if transport == "iscsi":
            self._connect_iscsi(export)
            return self._iscsi_path(export)
        if transport == "nvmeof":
            self._connect_nvme(export)
            return self._nvme_path(export)
        raise ProviderError("transport_invalid", f"unsupported transport: {transport}")

    def _config_diagnostics(self) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        checks = []
        details: Dict[str, Any] = {"config_path": self.config_path}
        config_path = Path(self.config_path)
        checks.append(doctor_check("config.file", config_path.exists(), f"provider config exists: {self.config_path}"))

        truenas = self.config.get("truenas")
        if not isinstance(truenas, dict):
            checks.append(doctor_check("config.truenas", False, "provider config must contain a truenas object"))
            return checks, details

        url = truenas.get("url")
        username = truenas.get("username")
        api_key_file = truenas.get("api_key_file")
        tls_verify = truenas.get("tls_verify", True)
        target_ip = truenas.get("target_ip")
        details["url"] = url
        details["username"] = username
        details["tls_verify"] = tls_verify
        details["target_ip"] = target_ip
        details["api_key_file"] = api_key_file

        checks.append(doctor_check("config.url", isinstance(url, str) and bool(url), "truenas.url is configured"))
        checks.append(doctor_check("config.username", isinstance(username, str) and bool(username), "truenas.username is configured"))
        checks.append(doctor_check("config.tls_verify", isinstance(tls_verify, bool), "truenas.tls_verify is a boolean"))
        checks.append(doctor_check("config.target_ip", isinstance(target_ip, str) and bool(target_ip), "truenas.target_ip is configured"))
        if isinstance(api_key_file, str) and api_key_file:
            key_path = Path(api_key_file)
            key_ok = readable_nonempty(api_key_file)
            checks.append(doctor_check("config.api_key_file", key_ok, f"TrueNAS API key file is readable and non-empty: {api_key_file}"))
            details["api_key_file_exists"] = key_path.exists()
        else:
            checks.append(doctor_check("config.api_key_file", bool(os.environ.get("TRUENAS_API_KEY")), "truenas.api_key_file is configured or TRUENAS_API_KEY is set"))
        return checks, details

    def _tool_diagnostics(self) -> Tuple[List[Dict[str, Any]], Dict[str, bool]]:
        tools = {name: tool_exists(name) for name in ["iscsiadm", "nvme", "udevadm", "systemctl"]}
        checks = [doctor_check(f"tool.{name}", ok, f"{name} is available") for name, ok in tools.items()]
        return checks, tools

    def _service_diagnostics(self) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        service = {
            "provider_service_active": systemd_active("truenas-libvirt-provider.service"),
            "provider_socket": DEFAULT_SOCKET,
            "provider_socket_exists": Path(DEFAULT_SOCKET).exists(),
        }
        checks = [
            doctor_check("service.provider", bool(service["provider_service_active"]), "truenas-libvirt-provider.service is active"),
            doctor_check("service.socket", bool(service["provider_socket_exists"]), f"provider socket exists at {DEFAULT_SOCKET}"),
        ]
        return checks, service

    def _transport_diagnostics(self, transport: str) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        transports = transport_status()
        selected = list(TRANSPORTS) if transport == "all" else [transport]
        checks = []
        for name in selected:
            state = transports[name]
            if name == "iscsi":
                checks.extend([
                    doctor_check("transport.iscsi.tool", bool(state["iscsiadm"]), "iSCSI tool iscsiadm is available; install open-iscsi or iscsi-initiator-utils if missing"),
                    doctor_check("transport.iscsi.initiator", bool(state["initiator_configured"]), f"iSCSI InitiatorName is configured in {LOCAL_ISCSI_INITIATOR_FILE}"),
                    doctor_check("transport.iscsi.service", bool(state["iscsid_active"]), "iscsid.service is active; run systemctl enable --now iscsid if this fails"),
                ])
            elif name == "nvmeof":
                checks.extend([
                    doctor_check("transport.nvmeof.tool", bool(state["nvme"]), "NVMe tool nvme is available; install nvme-cli if missing"),
                    doctor_check("transport.nvmeof.hostnqn", bool(state["hostnqn_configured"]), f"NVMe host NQN is configured in {LOCAL_NVME_HOSTNQN_FILE}"),
                    doctor_check("transport.nvmeof.tcp", bool(state["nvme_tcp_available"]), "nvme-tcp kernel module is loaded or available before NVMe-oF use"),
                ])
        return checks, transports

    def _network_diagnostics(self, transport: str) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        truenas = self.config.get("truenas", {})
        if not isinstance(truenas, dict):
            return [], {}
        checks = []
        details: Dict[str, Any] = {}
        url = truenas.get("url")
        if isinstance(url, str) and url:
            parsed = urllib.parse.urlparse(url)
            api_host = parsed.hostname
            api_port = parsed.port or (443 if parsed.scheme == "wss" else 80)
            if api_host:
                api_result = tcp_reachable(api_host, api_port)
                details["api"] = api_result
                checks.append(doctor_check("network.api", bool(api_result["ok"]), f"TrueNAS API endpoint is reachable at {api_host}:{api_port}", api_result))
        target_ip = truenas.get("target_ip")
        if isinstance(target_ip, str) and target_ip:
            ports = []
            if transport in ("all", "iscsi"):
                ports.append(("iscsi", 3260))
            if transport in ("all", "nvmeof"):
                ports.append(("nvmeof", 4420))
            storage_results = []
            for name, port in ports:
                result = tcp_reachable(target_ip, port)
                result["transport"] = name
                storage_results.append(result)
                checks.append(doctor_check(f"network.{name}", bool(result["ok"]), f"TrueNAS {name} storage endpoint is reachable at {target_ip}:{port}", result, required=False))
            details["storage"] = storage_results
        return checks, details

    def _truenas_diagnostics(self) -> Tuple[List[Dict[str, Any]], Optional[Dict[str, Any]]]:
        try:
            with self._client() as client:
                login(client, self.config)
                info = client.call("system.info", [])
                pools = client.call("pool.query", [[], {"order_by": ["name"]}])
            checks = [
                doctor_check("truenas.login", True, "TrueNAS API authentication succeeded"),
                doctor_check("truenas.system_info", isinstance(info, dict), "TrueNAS system information is readable"),
                doctor_check("truenas.pool_query", isinstance(pools, list), "TrueNAS pools are discoverable"),
            ]
            return checks, {"system": info, "pools": pools}
        except (ConfigError, WebSocketError, OSError, ssl.SSLError) as exc:
            message = str(exc)
        except Exception as exc:
            message = str(exc)
        return [doctor_check("truenas.login", False, f"TrueNAS API login/query failed: {message}", {"error": message})], None

    def _truenas_permission_diagnostics(self, transport: str) -> Tuple[List[Dict[str, Any]], Optional[Dict[str, Any]]]:
        try:
            with self._client() as client:
                login(client, self.config)
                methods = client.call("core.get_methods", [])
            if not isinstance(methods, dict):
                return [doctor_check("truenas.permissions", False, "TrueNAS API method list returned an unexpected response")], None
            required = required_truenas_api_methods(transport)
            missing = [method for method in required if method not in methods]
            ok = not missing
            message = "TrueNAS API user exposes required Subvirt methods"
            if missing:
                message = "TrueNAS API user is missing methods required by Subvirt"
            details = {"required": list(required), "missing": missing}
            return [doctor_check("truenas.permissions", ok, message, details)], details
        except (ConfigError, WebSocketError, OSError, ssl.SSLError) as exc:
            message = str(exc)
        except Exception as exc:
            message = str(exc)
        return [doctor_check("truenas.permissions", False, f"TrueNAS API permission check failed: {message}", {"error": message})], None

    def health_check(self, params: Dict[str, Any]) -> Dict[str, Any]:
        transport = str(params.get("transport", "all"))
        if transport not in (*TRANSPORTS, "all"):
            raise ProviderError("transport_invalid", f"unsupported transport: {transport}")

        checks = []
        config_checks, config = self._config_diagnostics()
        tool_checks, tools = self._tool_diagnostics()
        service_checks, service = self._service_diagnostics()
        transport_checks, transports = self._transport_diagnostics(transport)
        network_checks, network = self._network_diagnostics(transport)
        truenas_checks, truenas = self._truenas_diagnostics()
        permission_checks, permissions = self._truenas_permission_diagnostics(transport)
        checks.extend(config_checks)
        checks.extend(tool_checks)
        checks.extend(service_checks)
        checks.extend(transport_checks)
        checks.extend(network_checks)
        checks.extend(truenas_checks)
        checks.extend(permission_checks)
        ok = all(bool(item["ok"]) for item in checks if item.get("required", True))
        return {
            "ok": ok,
            "transport": transport,
            "checks": checks,
            "config": config,
            "tools": tools,
            "service": service,
            "transports": transports,
            "network": network,
            "truenas": truenas,
            "permissions": permissions,
        }


    def pool_list(self, params: Dict[str, Any]) -> Dict[str, Any]:
        with self._client() as client:
            login(client, self.config)
            rows = client.call("pool.query", [[], {"order_by": ["name"]}])
        if not isinstance(rows, list):
            raise ProviderError("pool_list_invalid", "invalid TrueNAS pool list response")
        pools = []
        unusable = {"OFFLINE", "UNAVAIL", "UNAVAILABLE", "REMOVED", "FAULTED"}
        for row in rows:
            if not isinstance(row, dict):
                continue
            name = row.get("name")
            if not isinstance(name, str) or not name:
                continue
            status_value = row.get("status") or row.get("state") or ""
            status = str(status_value).upper()
            if status in unusable:
                continue
            item = {"name": name}
            if status_value:
                item["status"] = str(status_value)
            pools.append(item)
        return {"pools": pools}

    def vol_list(self, params: Dict[str, Any]) -> Dict[str, Any]:
        pool = str(params["pool"])
        transport = params.get("transport")
        with self._client() as client:
            login(client, self.config)
            volumes = self._list_zvols(client, pool, str(transport) if transport else None)
        return {"volumes": volumes}

    @staticmethod
    def _is_transient_truenas_error(exc: WebSocketError) -> bool:
        message = str(exc).lower()
        return any(
            item in message
            for item in (
                "unexpected eof from websocket",
                "websocket closed by server",
                "truenas api connection failed",
            )
        )

    def _pool_refresh_once(self, pool: str, transport: str, connect: bool) -> Dict[str, Any]:
        refreshed = []
        with self._client() as client:
            login(client, self.config)
            self._ensure_namespace(client, pool)
            pool_space = self._pool_space(client, pool)
            for volume in self._list_zvols(client, pool, transport):
                export = self._export(client, pool, str(volume["name"]), transport)
                path = None
                if connect:
                    try:
                        path = self._connect_and_path(export, transport)
                    except ProviderError as exc:
                        if exc.code != "path_not_found":
                            raise
                volume.update({"export": export})
                if path is not None:
                    volume["path"] = path
                refreshed.append(volume)
        return {"pool": pool_space, "volumes": refreshed}

    def pool_refresh(self, params: Dict[str, Any]) -> Dict[str, Any]:
        pool = str(params["pool"])
        transport = str(params["transport"])
        connect = bool(params.get("connect", True))
        self._require_transport_ready(transport)
        last_error: Optional[WebSocketError] = None
        for attempt in range(2):
            try:
                return self._pool_refresh_once(pool, transport, connect)
            except WebSocketError as exc:
                if not self._is_transient_truenas_error(exc) or attempt == 1:
                    raise
                last_error = exc
                time.sleep(1)
        assert last_error is not None
        raise last_error

    def vol_create(self, params: Dict[str, Any]) -> Dict[str, Any]:
        pool = str(params["pool"])
        name = str(params["name"])
        transport = str(params["transport"])
        capacity = params["capacity"]
        self._require_transport_ready(transport)
        with self._client() as client:
            login(client, self.config)
            row = self._create_zvol(client, pool, name, capacity, transport)
            export = self._export(client, pool, name, transport)
        path = self._connect_and_path(export, transport)
        volume = self._volume_from_dataset(pool, row)
        volume.update({"export": export, "path": path})
        return volume

    def vol_clone(self, params: Dict[str, Any]) -> Dict[str, Any]:
        pool = str(params["pool"])
        source = str(params["source"])
        target = str(params["target"])
        transport = str(params["transport"])
        replace_existing = bool(params.get("replace_existing", False))
        if transport not in TRANSPORTS:
            raise ProviderError("transport_invalid", f"unsupported transport: {transport}")
        self._require_transport_ready(transport)
        with self._client() as client:
            login(client, self.config)
            row = self._clone_zvol(client, pool, source, target, transport, replace_existing)
            export = self._export(client, pool, target, transport)
        path = self._connect_and_path(export, transport)
        volume = self._volume_from_dataset(pool, row)
        volume.update({"export": export, "path": path})
        return volume

    def vol_resize(self, params: Dict[str, Any]) -> Dict[str, Any]:
        pool = str(params["pool"])
        name = str(params["name"])
        transport = str(params["transport"])
        capacity = params["capacity"]
        if transport not in TRANSPORTS:
            raise ProviderError("transport_invalid", f"unsupported transport: {transport}")
        with self._client() as client:
            login(client, self.config)
            row = self._resize_zvol(client, pool, name, capacity)
        return self._volume_from_dataset(pool, row)

    def vol_path(self, params: Dict[str, Any]) -> Dict[str, Any]:
        pool = str(params["pool"])
        name = str(params["name"])
        transport = str(params["transport"])
        self._require_transport_ready(transport)
        with self._client() as client:
            login(client, self.config)
            export = self._export(client, pool, name, transport)
        path = self._connect_and_path(export, transport)
        return {"name": name, "transport": transport, "path": path, "export": export}

    def _disconnect_iscsi(self, name: str) -> None:
        target_iqn = f"iqn.2005-10.org.freenas.ctl:{self._target_name(name)}"
        # A deleted/recreated TrueNAS extent can keep the same target IQN while
        # exposing a new LUN identity.  Clear both live sessions and node records
        # so the next pool refresh performs a fresh login and device discovery.
        run_command(["iscsiadm", "-m", "node", "-T", target_iqn, "--logout"], check=False)
        run_command(["iscsiadm", "-m", "node", "-T", target_iqn, "--op", "delete"], check=False)
        if tool_exists("udevadm"):
            run_command(["udevadm", "settle", "--timeout=10"], check=False)

    def _disconnect_nvme(self, name: str) -> None:
        with self._client() as client:
            login(client, self.config)
            subsystems = query_all(client, "nvmet.subsys.query", [["name", "=", self._target_name(name)]])
        for subsys in subsystems:
            if isinstance(subsys, dict) and subsys.get("subnqn"):
                run_command(["nvme", "disconnect", "-n", str(subsys["subnqn"])], check=False)

    def _cleanup_iscsi_export(self, client: JsonRpcWebSocket, name: str) -> None:
        target_name = self._target_name(name)
        targets = query_all(client, "iscsi.target.query", [["name", "=", target_name]])
        extents = query_all(client, "iscsi.extent.query", [["name", "=", target_name]])
        for mapping in query_all(client, "iscsi.targetextent.query"):
            if not isinstance(mapping, dict):
                continue
            if any(isinstance(target, dict) and mapping.get("target") == target.get("id") for target in targets):
                client.call("iscsi.targetextent.delete", [mapping["id"], True])
        for target in targets:
            if isinstance(target, dict):
                client.call("iscsi.target.delete", [target["id"], True, False])
        for extent in extents:
            if isinstance(extent, dict):
                client.call("iscsi.extent.delete", [extent["id"], False, True])
        ensure_service_enabled_and_running(client, "iscsitarget")

    def _cleanup_nvme_export(self, client: JsonRpcWebSocket, name: str) -> None:
        subsystems = query_all(client, "nvmet.subsys.query", [["name", "=", self._target_name(name)]])
        for subsys in subsystems:
            if not isinstance(subsys, dict):
                continue
            subsys_id = int(subsys["id"])
            for assoc in query_all(client, "nvmet.host_subsys.query"):
                if isinstance(assoc, dict) and (assoc.get("subsys") == subsys_id or (isinstance(assoc.get("subsys"), dict) and assoc["subsys"].get("id") == subsys_id)):
                    client.call("nvmet.host_subsys.delete", [assoc["id"]])
            for assoc in query_all(client, "nvmet.port_subsys.query"):
                if isinstance(assoc, dict) and (assoc.get("subsys") == subsys_id or (isinstance(assoc.get("subsys"), dict) and assoc["subsys"].get("id") == subsys_id)):
                    client.call("nvmet.port_subsys.delete", [assoc["id"]])
            for namespace in query_all(client, "nvmet.namespace.query"):
                if isinstance(namespace, dict):
                    ns_subsys = namespace.get("subsys")
                    if ns_subsys == subsys_id or (isinstance(ns_subsys, dict) and ns_subsys.get("id") == subsys_id):
                        client.call("nvmet.namespace.delete", [namespace["id"], {"remove": False}])
            client.call("nvmet.subsys.delete", [subsys_id, {"force": True}])
        ensure_service_enabled_and_running(client, "nvmet")

    def vol_delete(self, params: Dict[str, Any]) -> Dict[str, Any]:
        pool = str(params["pool"])
        name = str(params["name"])
        transport = str(params["transport"])
        delete_snapshots = bool(params.get("delete_snapshots", False))
        zvol = managed_zvol_name(pool, name, self.config)
        if transport not in TRANSPORTS:
            raise ProviderError("transport_invalid", f"unsupported transport: {transport}")
        with self._client() as client:
            login(client, self.config)
            if not dataset_exists(client, zvol):
                raise ProviderError("volume_missing", f"zvol does not exist: {zvol}")
            snapshots = self._list_zvol_snapshots(client, zvol)
            if snapshots and not delete_snapshots:
                raise ProviderError(
                    "delete_blocked_by_snapshots",
                    "volume has snapshots; retry with virsh vol-delete --delete-snapshots to remove safe Subvirt-managed snapshots",
                    {"zvol": zvol, "snapshots": [self._snapshot_name(snapshot) for snapshot in snapshots]},
                )
            if snapshots:
                self._delete_managed_snapshots(client, zvol, snapshots)
            if transport == "iscsi":
                self._disconnect_iscsi(name)
                self._cleanup_iscsi_export(client, name)
            else:
                self._disconnect_nvme(name)
                self._cleanup_nvme_export(client, name)
            self._safe_delete_dataset(client, zvol)
        return {"deleted": True, "name": name, "transport": transport}

    def dispatch(self, method: str, params: Dict[str, Any]) -> Any:
        handlers = {
            "health.check": self.health_check,
            "pool.list": self.pool_list,
            "pool.refresh": self.pool_refresh,
            "vol.list": self.vol_list,
            "vol.create": self.vol_create,
            "vol.clone": self.vol_clone,
            "vol.resize": self.vol_resize,
            "vol.path": self.vol_path,
            "vol.delete": self.vol_delete,
        }
        if method not in handlers:
            raise ProviderError("method_not_found", f"unknown method: {method}")
        return handlers[method](params)


class UnixJsonRpcServer:
    def __init__(self, provider: TrueNASLibvirtProvider, socket_path: str) -> None:
        self.provider = provider
        self.socket_path = Path(socket_path)

    def serve_forever(self) -> None:
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        if self.socket_path.exists():
            self.socket_path.unlink()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(str(self.socket_path))
            os.chmod(self.socket_path, 0o600)
            server.listen(16)
            while True:
                conn, _addr = server.accept()
                with conn:
                    self._handle(conn)

    def _handle(self, conn: socket.socket) -> None:
        reader = conn.makefile("r", encoding="utf-8")
        writer = conn.makefile("w", encoding="utf-8")
        for line in reader:
            response = self._dispatch_line(line)
            writer.write(json.dumps(response, separators=(",", ":")) + "\n")
            writer.flush()

    def _dispatch_line(self, line: str) -> Dict[str, Any]:
        request_id = None
        try:
            request = json.loads(line)
            request_id = request.get("id")
            method = request["method"]
            params = request.get("params") or {}
            if not isinstance(params, dict):
                raise ProviderError("params_invalid", "params must be an object")
            print(f"request method={method} params={json.dumps(params, sort_keys=True)}", file=sys.stderr, flush=True)
            result = self.provider.dispatch(str(method), params)
            print(f"response method={method} ok", file=sys.stderr, flush=True)
            return {"jsonrpc": "2.0", "id": request_id, "result": result}
        except ProviderError as exc:
            print(f"response error={exc.code} message={exc}", file=sys.stderr, flush=True)
            return {"jsonrpc": "2.0", "id": request_id, "error": {"code": exc.code, "message": str(exc), "data": exc.data}}
        except (ConfigError, KeyError, TypeError, ValueError, WebSocketError) as exc:
            print(f"response error=request_failed message={exc}", file=sys.stderr, flush=True)
            return {"jsonrpc": "2.0", "id": request_id, "error": {"code": "request_failed", "message": str(exc)}}
        except Exception as exc:
            print(f"response error=internal_error message={exc}", file=sys.stderr, flush=True)
            return {"jsonrpc": "2.0", "id": request_id, "error": {"code": "internal_error", "message": str(exc)}}


def rpc_call(socket_path: str, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
    request = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.connect(socket_path)
        client.sendall((json.dumps(request, separators=(",", ":")) + "\n").encode("utf-8"))
        response = client.makefile("r", encoding="utf-8").readline()
    return json.loads(response)


def rpc_result(response: Dict[str, Any]) -> Any:
    if "error" in response:
        error = response["error"]
        if isinstance(error, dict):
            raise ProviderError(str(error.get("code", "request_failed")), str(error.get("message", "provider request failed")), error.get("data") if isinstance(error.get("data"), dict) else None)
        raise ProviderError("request_failed", str(error))
    return response.get("result")


def error_report(config_path: str, transport: str, check_name: str, message: str) -> Dict[str, Any]:
    check = doctor_check(check_name, False, message, {"config_path": config_path})
    return {"ok": False, "transport": transport, "checks": [check], "config": {"config_path": config_path}}


def doctor_report(args: argparse.Namespace) -> Dict[str, Any]:
    params = {"transport": args.transport}
    socket_path = Path(args.socket)
    if socket_path.exists():
        try:
            return rpc_result(rpc_call(args.socket, "health.check", params))
        except (OSError, json.JSONDecodeError, ProviderError) as exc:
            return error_report(args.config, args.transport, "service.socket", f"provider socket request failed: {exc}")
    try:
        return TrueNASLibvirtProvider(args.config).health_check(params)
    except (ConfigError, WebSocketError, ProviderError, OSError, ValueError) as exc:
        return error_report(args.config, args.transport, "config.load", str(exc))


def print_doctor_text(report: Dict[str, Any]) -> None:
    status = "OK" if report.get("ok") else "FAILED"
    transport = report.get("transport", "all")
    print(f"Subvirt doctor: {status} (transport={transport})")
    for item in report.get("checks", []):
        if not isinstance(item, dict):
            continue
        prefix = "OK" if item.get("ok") else ("FAIL" if item.get("required", True) else "WARN")
        name = item.get("name", "check")
        message = item.get("message", "")
        print(f"[{prefix}] {name}: {message}")


def run_doctor(args: argparse.Namespace) -> int:
    report = doctor_report(args)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print_doctor_text(report)
    return 0 if report.get("ok") else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=os.environ.get("TRUENAS_LIBVIRT_CONFIG", DEFAULT_CONFIG))
    parser.add_argument("--socket", default=os.environ.get("TRUENAS_LIBVIRT_SOCKET", DEFAULT_SOCKET))
    subparsers = parser.add_subparsers(dest="command")

    daemon = subparsers.add_parser("daemon")
    daemon.set_defaults(command="daemon")

    call = subparsers.add_parser("call")
    call.add_argument("method")
    call.add_argument("params", nargs="?", default="{}")
    call.set_defaults(command="call")

    doctor = subparsers.add_parser("doctor")
    doctor.add_argument("--transport", choices=["all", *TRANSPORTS], default="all")
    doctor.add_argument("--json", action="store_true")
    doctor.set_defaults(command="doctor")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if not args.command:
        parser.error("missing command")
    if args.command == "daemon":
        UnixJsonRpcServer(TrueNASLibvirtProvider(args.config), args.socket).serve_forever()
        return 0
    if args.command == "call":
        print(json.dumps(rpc_call(args.socket, args.method, json.loads(args.params)), indent=2, sort_keys=True))
        return 0
    if args.command == "doctor":
        return run_doctor(args)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
