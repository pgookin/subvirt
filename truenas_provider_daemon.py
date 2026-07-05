#!/usr/bin/env python3
"""Unix-socket JSON-RPC daemon for the TrueNAS libvirt storage provider."""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

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


class ProviderError(RuntimeError):
    def __init__(self, code: str, message: str, data: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.data = data or {}


def run_command(argv: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(argv, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
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


def transport_status() -> dict[str, Any]:
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
    }
    iscsi["ok"] = bool(iscsi["iscsiadm"] and iscsi["initiator_configured"] and iscsi["iscsid_active"])
    nvme["ok"] = bool(nvme["nvme"] and nvme["hostnqn_configured"])
    return {"iscsi": iscsi, "nvmeof": nvme}


class TrueNASLibvirtProvider:
    def __init__(self, config_path: str) -> None:
        self.config_path = config_path
        self.config = load_config(config_path)

    def _client(self) -> JsonRpcWebSocket:
        return open_client(self.config)

    def _target_ip(self, override: str | None = None) -> str:
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

    def _volume_from_dataset(self, pool: str, item: dict[str, Any]) -> dict[str, Any]:
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
    def _property_parsed(properties: Any, name: str) -> int | None:
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

    def _create_zvol(self, client: JsonRpcWebSocket, pool: str, name: str, capacity: Any, transport: str) -> dict[str, Any]:
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

    def _list_zvols(self, client: JsonRpcWebSocket, pool: str, transport: str | None = None) -> list[dict[str, Any]]:
        prefix = managed_dataset_name(pool, self.config) + "/"
        rows = client.call("pool.dataset.query", [[["type", "=", "VOLUME"], ["name", "^", prefix]], {"order_by": ["name"]}])
        assert isinstance(rows, list)
        volumes = [self._volume_from_dataset(pool, row) for row in rows if isinstance(row, dict)]
        if transport:
            volumes = [vol for vol in volumes if vol.get("transport") in (None, transport)]
        return volumes

    def _pool_space(self, client: JsonRpcWebSocket, pool: str) -> dict[str, int]:
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

    def _iscsi_export(self, client: JsonRpcWebSocket, pool: str, name: str, target_ip: str) -> dict[str, Any]:
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

    def _nvme_export(self, client: JsonRpcWebSocket, pool: str, name: str, target_ip: str, port_number: int = 4420) -> dict[str, Any]:
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

    def _connect_iscsi(self, export: dict[str, Any]) -> None:
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

    def _connect_nvme(self, export: dict[str, Any]) -> None:
        subnqn = str(export["subnqn"])
        result = run_command(["nvme", "connect", "-t", "tcp", "-a", str(export["traddr"]), "-s", str(export["trsvcid"]), "-n", subnqn], check=False)
        if result.returncode != 0 and "already connected" not in result.stderr.lower() and "duplicate connect" not in result.stderr.lower():
            raise ProviderError("nvme_connect_failed", "NVMe-oF connect failed", {"stderr": result.stderr, "stdout": result.stdout})

    def _find_by_id(self, prefixes: list[str], timeout: int = DEFAULT_TIMEOUT) -> str:
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

    def _iscsi_path(self, export: dict[str, Any]) -> str:
        naa = str(export.get("naa") or "")
        serial = str(export.get("serial") or "")
        prefixes = []
        if naa.startswith("0x"):
            prefixes.append(f"wwn-{naa}")
            prefixes.append(f"scsi-3{naa[2:]}")
        if serial:
            prefixes.append(f"scsi-STrueNAS_iSCSI_Disk_{serial}")
        return self._find_by_id(prefixes)

    def _nvme_path(self, export: dict[str, Any]) -> str:
        subnqn = str(export["subnqn"])
        deadline = time.time() + DEFAULT_TIMEOUT
        while time.time() < deadline:
            run_command(["udevadm", "settle"], check=False)
            result = run_command(["nvme", "list-subsys", "-o", "json"], check=False)
            if result.returncode == 0:
                try:
                    hosts = json.loads(result.stdout)
                except json.JSONDecodeError:
                    hosts = []
                for host in hosts if isinstance(hosts, list) else []:
                    for subsystem in host.get("Subsystems", []) if isinstance(host, dict) else []:
                        if not isinstance(subsystem, dict) or subsystem.get("NQN") != subnqn:
                            continue
                        for path in subsystem.get("Paths", []):
                            if not isinstance(path, dict) or not path.get("Name"):
                                continue
                            devname = f"{path['Name']}n1"
                            match = self._find_by_id_target(devname)
                            if match:
                                return match
            time.sleep(0.5)
        raise ProviderError("path_not_found", "stable NVMe /dev/disk/by-id path was not found", {"subnqn": subnqn})

    @staticmethod
    def _find_by_id_target(devname: str) -> str | None:
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

    def _export(self, client: JsonRpcWebSocket, pool: str, name: str, transport: str) -> dict[str, Any]:
        target_ip = self._target_ip()
        if transport == "iscsi":
            return self._iscsi_export(client, pool, name, target_ip)
        if transport == "nvmeof":
            return self._nvme_export(client, pool, name, target_ip)
        raise ProviderError("transport_invalid", f"unsupported transport: {transport}")

    def _connect_and_path(self, export: dict[str, Any], transport: str) -> str:
        if transport == "iscsi":
            self._connect_iscsi(export)
            return self._iscsi_path(export)
        if transport == "nvmeof":
            self._connect_nvme(export)
            return self._nvme_path(export)
        raise ProviderError("transport_invalid", f"unsupported transport: {transport}")

    def health_check(self, params: dict[str, Any]) -> dict[str, Any]:
        with self._client() as client:
            login(client, self.config)
            info = client.call("system.info", [])
        tools = {name: tool_exists(name) for name in ["iscsiadm", "nvme", "udevadm", "systemctl"]}
        transports = transport_status()
        ok = all(tools.values()) and bool(transports["iscsi"]["ok"] and transports["nvmeof"]["ok"])
        return {"ok": ok, "truenas": info, "tools": tools, "transports": transports}


    def pool_list(self, params: dict[str, Any]) -> dict[str, Any]:
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

    def vol_list(self, params: dict[str, Any]) -> dict[str, Any]:
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

    def _pool_refresh_once(self, pool: str, transport: str, connect: bool) -> dict[str, Any]:
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

    def pool_refresh(self, params: dict[str, Any]) -> dict[str, Any]:
        pool = str(params["pool"])
        transport = str(params["transport"])
        connect = bool(params.get("connect", True))
        self._require_transport_ready(transport)
        last_error: WebSocketError | None = None
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

    def vol_create(self, params: dict[str, Any]) -> dict[str, Any]:
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

    def vol_path(self, params: dict[str, Any]) -> dict[str, Any]:
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
        run_command(["iscsiadm", "-m", "node", "-T", target_iqn, "--logout"], check=False)

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

    def vol_delete(self, params: dict[str, Any]) -> dict[str, Any]:
        pool = str(params["pool"])
        name = str(params["name"])
        transport = str(params["transport"])
        if transport == "iscsi":
            self._disconnect_iscsi(name)
        elif transport == "nvmeof":
            self._disconnect_nvme(name)
        else:
            raise ProviderError("transport_invalid", f"unsupported transport: {transport}")
        with self._client() as client:
            login(client, self.config)
            if transport == "iscsi":
                self._cleanup_iscsi_export(client, name)
            else:
                self._cleanup_nvme_export(client, name)
            zvol = managed_zvol_name(pool, name, self.config)
            methods = client.call("core.get_methods", [])
            assert isinstance(methods, dict)
            if "pool.dataset.delete" not in methods:
                raise ProviderError("dataset_delete_unavailable", "TrueNAS API user cannot delete zvols with the currently exposed methods", {"zvol": zvol})
            client.call("pool.dataset.delete", [zvol, {"recursive": False, "force": False}])
        return {"deleted": True, "name": name, "transport": transport}

    def dispatch(self, method: str, params: dict[str, Any]) -> Any:
        handlers = {
            "health.check": self.health_check,
            "pool.list": self.pool_list,
            "pool.refresh": self.pool_refresh,
            "vol.list": self.vol_list,
            "vol.create": self.vol_create,
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

    def _dispatch_line(self, line: str) -> dict[str, Any]:
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


def rpc_call(socket_path: str, method: str, params: dict[str, Any]) -> dict[str, Any]:
    request = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.connect(socket_path)
        client.sendall((json.dumps(request, separators=(",", ":")) + "\n").encode("utf-8"))
        response = client.makefile("r", encoding="utf-8").readline()
    return json.loads(response)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=os.environ.get("TRUENAS_LIBVIRT_CONFIG", DEFAULT_CONFIG))
    parser.add_argument("--socket", default=os.environ.get("TRUENAS_LIBVIRT_SOCKET", DEFAULT_SOCKET))
    subparsers = parser.add_subparsers(required=True)

    daemon = subparsers.add_parser("daemon")
    daemon.set_defaults(command="daemon")

    call = subparsers.add_parser("call")
    call.add_argument("method")
    call.add_argument("params", nargs="?", default="{}")
    call.set_defaults(command="call")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.command == "daemon":
        UnixJsonRpcServer(TrueNASLibvirtProvider(args.config), args.socket).serve_forever()
        return 0
    if args.command == "call":
        print(json.dumps(rpc_call(args.socket, args.method, json.loads(args.params)), indent=2, sort_keys=True))
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
