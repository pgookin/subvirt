#!/usr/bin/env python3
"""Small TrueNAS JSON-RPC client for the libvirt storage prototype."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import socket
import ssl
import struct
import sys
import urllib.parse


class WebSocketError(RuntimeError):
    pass


class ConfigError(RuntimeError):
    pass


def managed_export_name(volume: str) -> str:
    raw = f"libvirt-{volume}"
    safe = re.sub(r"[^a-z0-9.:-]+", "-", raw.lower()).strip(".-:")
    if safe == raw and len(raw) <= 64:
        return raw
    if not safe:
        safe = "libvirt-volume"
    digest = hashlib.sha256(volume.encode("utf-8")).hexdigest()[:12]
    max_prefix = 64 - len(digest) - 1
    return f"{safe[:max_prefix].rstrip('.-:')}-{digest}"


class JsonRpcWebSocket:
    def __init__(self, url: str, tls_verify: bool = True) -> None:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in {"ws", "wss"}:
            raise ValueError("url must use ws:// or wss://")
        self.parsed = parsed
        self.tls_verify = tls_verify
        self.sock: socket.socket | ssl.SSLSocket | None = None
        self.next_id = 1

    def __enter__(self) -> "JsonRpcWebSocket":
        self.connect()
        return self

    def __exit__(self, *_exc: object) -> None:
        if self.sock is not None:
            self.sock.close()

    def connect(self) -> None:
        host = self.parsed.hostname
        if host is None:
            raise ValueError("url is missing hostname")
        port = self.parsed.port or (443 if self.parsed.scheme == "wss" else 80)
        raw: socket.socket | None = None
        try:
            raw = socket.create_connection((host, port), timeout=15)
            if self.parsed.scheme == "wss":
                context = ssl.create_default_context()
                if not self.tls_verify:
                    context.check_hostname = False
                    context.verify_mode = ssl.CERT_NONE
                self.sock = context.wrap_socket(raw, server_hostname=host)
                raw = None
            else:
                self.sock = raw
                raw = None

            key = base64.b64encode(os.urandom(16)).decode("ascii")
            path = self.parsed.path or "/"
            if self.parsed.query:
                path += "?" + self.parsed.query
            request = (
                f"GET {path} HTTP/1.1\r\n"
                f"Host: {host}:{port}\r\n"
                "Upgrade: WebSocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\n"
                "Sec-WebSocket-Version: 13\r\n"
                "\r\n"
            )
            self.sock.sendall(request.encode("ascii"))
            response = self._read_http_response()
            if b" 101 " not in response.split(b"\r\n", 1)[0]:
                raise WebSocketError(response.decode("utf-8", errors="replace"))
        except (OSError, ssl.SSLError, WebSocketError) as exc:
            if raw is not None:
                raw.close()
            if self.sock is not None:
                self.sock.close()
                self.sock = None
            raise WebSocketError(f"TrueNAS API connection failed for {self.parsed.geturl()}: {exc}") from None

    def call(self, method: str, params: list[object] | None = None) -> object:
        request_id = self.next_id
        self.next_id += 1
        self._send_json(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params or [],
            }
        )
        while True:
            message = self._recv_json()
            if message.get("id") != request_id:
                continue
            if "error" in message:
                raise WebSocketError(json.dumps(message["error"], indent=2))
            return message.get("result")

    def _read_http_response(self) -> bytes:
        assert self.sock is not None
        chunks = []
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = self.sock.recv(4096)
            if not chunk:
                break
            chunks.append(chunk)
            data = b"".join(chunks)
        return data

    def _send_json(self, data: object) -> None:
        payload = json.dumps(data, separators=(",", ":")).encode("utf-8")
        self._send_frame(payload)

    def _recv_json(self) -> dict[str, object]:
        payload = self._recv_frame()
        return json.loads(payload.decode("utf-8"))

    def _send_frame(self, payload: bytes) -> None:
        assert self.sock is not None
        header = bytearray([0x81])
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", length))
        mask = os.urandom(4)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        self.sock.sendall(bytes(header) + mask + masked)

    def _recv_frame(self) -> bytes:
        assert self.sock is not None
        header = self._recv_exact(2)
        opcode = header[0] & 0x0F
        masked = bool(header[1] & 0x80)
        length = header[1] & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._recv_exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._recv_exact(8))[0]
        mask = self._recv_exact(4) if masked else b""
        payload = self._recv_exact(length)
        if masked:
            payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        if opcode == 0x8:
            raise WebSocketError("websocket closed by server")
        if opcode != 0x1:
            return self._recv_frame()
        return payload

    def _recv_exact(self, length: int) -> bytes:
        assert self.sock is not None
        data = b""
        while len(data) < length:
            chunk = self.sock.recv(length - len(data))
            if not chunk:
                raise WebSocketError("unexpected EOF from websocket")
            data += chunk
        return data


def load_config(path: str) -> dict[str, object]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            config = json.load(handle)
    except FileNotFoundError as exc:
        raise ConfigError(f"TrueNAS provider config file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"TrueNAS provider config file is invalid JSON: {path}: {exc}") from exc
    if not isinstance(config, dict):
        raise ConfigError(f"TrueNAS provider config must be a JSON object: {path}")
    return config


def load_api_key(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            api_key = handle.read().strip()
    except FileNotFoundError as exc:
        raise ConfigError(f"TrueNAS API key file not found: {path}") from exc
    if not api_key:
        raise ConfigError(f"TrueNAS API key file is empty: {path}")
    return api_key


def get_truenas_config(config: dict[str, object]) -> dict[str, object]:
    truenas = config.get("truenas")
    if not isinstance(truenas, dict):
        raise ConfigError("TrueNAS provider config must contain a 'truenas' object")
    return truenas


def login(client: JsonRpcWebSocket, config: dict[str, object]) -> None:
    truenas = get_truenas_config(config)
    api_key = os.environ.get("TRUENAS_API_KEY")
    api_key_file = truenas.get("api_key_file")
    username = truenas.get("username")
    if not isinstance(username, str) or not username:
        raise ConfigError("TrueNAS provider config requires truenas.username")
    if api_key is None:
        if not isinstance(api_key_file, str) or not api_key_file:
            raise ConfigError("TrueNAS provider config requires truenas.api_key_file or TRUENAS_API_KEY")
        api_key = load_api_key(api_key_file)
    try:
        result = client.call(
            "auth.login_ex",
            [
                {
                    "mechanism": "API_KEY_PLAIN",
                    "username": username,
                    "api_key": api_key,
                }
            ],
        )
    except WebSocketError as exc:
        raise WebSocketError(f"TrueNAS API authentication request failed for user {username!r}: {exc}") from None
    if not result:
        raise WebSocketError(f"TrueNAS API authentication failed for user {username!r}")


def open_client(config: dict[str, object]) -> JsonRpcWebSocket:
    truenas = get_truenas_config(config)
    url = truenas.get("url")
    tls_verify = truenas.get("tls_verify", True)
    if not isinstance(url, str) or not url:
        raise ConfigError("TrueNAS provider config requires truenas.url")
    if not isinstance(tls_verify, bool):
        raise ConfigError("TrueNAS provider config value truenas.tls_verify must be true or false")
    return JsonRpcWebSocket(url, tls_verify=tls_verify)


def cmd_call(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    params = json.loads(args.params) if args.params else []
    with open_client(config) as client:
        login(client, config)
        print(json.dumps(client.call(args.method, params), indent=2, sort_keys=True))


def cmd_pool_list(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    with open_client(config) as client:
        login(client, config)
        result = client.call("pool.query", [])
        print(json.dumps(result, indent=2, sort_keys=True))



LOCAL_ISCSI_INITIATOR_FILE = "/etc/iscsi/initiatorname.iscsi"
LOCAL_NVME_HOSTNQN_FILE = "/etc/nvme/hostnqn"


def _read_key_value_file(path: str, key: str) -> str | None:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                item_key, item_value = line.split("=", 1)
                if item_key.strip() == key:
                    value = item_value.strip()
                    return value or None
    except FileNotFoundError:
        return None
    return None


def local_iscsi_iqn() -> str:
    iqn = _read_key_value_file(LOCAL_ISCSI_INITIATOR_FILE, "InitiatorName")
    if not iqn:
        raise ConfigError(f"local iSCSI initiator IQN not found in {LOCAL_ISCSI_INITIATOR_FILE}")
    return iqn


def local_nvme_nqn() -> str:
    try:
        with open(LOCAL_NVME_HOSTNQN_FILE, "r", encoding="utf-8") as handle:
            nqn = handle.read().strip()
    except FileNotFoundError as exc:
        raise ConfigError(f"local NVMe host NQN file not found: {LOCAL_NVME_HOSTNQN_FILE}") from exc
    if not nqn:
        raise ConfigError(f"local NVMe host NQN file is empty: {LOCAL_NVME_HOSTNQN_FILE}")
    return nqn


SIZE_SUFFIXES = {
    "k": 1024,
    "m": 1024**2,
    "g": 1024**3,
    "t": 1024**4,
}


def parse_size(value: str) -> int:
    normalized = value.strip().lower()
    if not normalized:
        raise ValueError("size cannot be empty")
    suffix = normalized[-1]
    if suffix in SIZE_SUFFIXES:
        number = normalized[:-1]
        return int(float(number) * SIZE_SUFFIXES[suffix])
    return int(normalized)


def managed_dataset_name(pool: str, config: dict[str, object]) -> str:
    namespace = config.get("namespace", {})
    assert isinstance(namespace, dict)
    dataset = namespace.get("dataset", "libvirt")
    assert isinstance(dataset, str)
    return f"{pool}/{dataset}"


def managed_zvol_name(pool: str, volume: str, config: dict[str, object]) -> str:
    return f"{managed_dataset_name(pool, config)}/{volume}"


def call_authenticated(config: dict[str, object], method: str, params: list[object] | None = None) -> object:
    with open_client(config) as client:
        login(client, config)
        return client.call(method, params or [])


def dataset_exists(client: JsonRpcWebSocket, name: str) -> bool:
    result = client.call("pool.dataset.query", [[["name", "=", name]], {"select": ["name"]}])
    return bool(result)


def cmd_method_list(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    methods = call_authenticated(config, "core.get_methods", [])
    assert isinstance(methods, dict)
    for name in sorted(methods):
        if not args.prefix or name.startswith(tuple(args.prefix)):
            print(name)


def cmd_method_info(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    methods = call_authenticated(config, "core.get_methods", [])
    assert isinstance(methods, dict)
    result = {name: methods[name] for name in args.method if name in methods}
    print(json.dumps(result, indent=2, sort_keys=True))


def cmd_namespace_ensure(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    dataset = managed_dataset_name(args.pool, config)
    with open_client(config) as client:
        login(client, config)
        if dataset_exists(client, dataset):
            print(json.dumps({"name": dataset, "changed": False}, indent=2))
            return
        result = client.call(
            "pool.dataset.create",
            [
                {
                    "name": dataset,
                    "type": "FILESYSTEM",
                    "share_type": "GENERIC",
                    "create_ancestors": True,
                    "managedby": "truenas-libvirt-provider",
                    "user_properties": [
                        {"key": "org.libvirt:managed", "value": "true"},
                    ],
                }
            ],
        )
        print(json.dumps({"name": dataset, "changed": True, "result": result}, indent=2, sort_keys=True))


def cmd_zvol_list(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    prefix = managed_dataset_name(args.pool, config) + "/"
    result = call_authenticated(
        config,
        "pool.dataset.query",
        [[["type", "=", "VOLUME"], ["name", "^", prefix]], {"order_by": ["name"]}],
    )
    print(json.dumps(result, indent=2, sort_keys=True))


def cmd_zvol_create(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    dataset = managed_dataset_name(args.pool, config)
    zvol = managed_zvol_name(args.pool, args.name, config)
    with open_client(config) as client:
        login(client, config)
        if not dataset_exists(client, dataset):
            client.call(
                "pool.dataset.create",
                [
                    {
                        "name": dataset,
                        "type": "FILESYSTEM",
                        "share_type": "GENERIC",
                        "create_ancestors": True,
                        "managedby": "truenas-libvirt-provider",
                        "user_properties": [
                            {"key": "org.libvirt:managed", "value": "true"},
                        ],
                    }
                ],
            )
        if dataset_exists(client, zvol):
            raise ValueError(f"zvol already exists: {zvol}")
        payload = {
            "name": zvol,
            "type": "VOLUME",
            "volsize": parse_size(args.size),
            "volblocksize": args.volblocksize,
            "sparse": args.sparse,
            "managedby": "truenas-libvirt-provider",
            "user_properties": [
                {"key": "org.libvirt:managed", "value": "true"},
                {"key": "org.libvirt:transport", "value": args.transport},
            ],
        }
        result = client.call("pool.dataset.create", [payload])
        print(json.dumps({"name": zvol, "result": result}, indent=2, sort_keys=True))




def query_all(client: JsonRpcWebSocket, method: str, filters: list[object] | None = None) -> list[object]:
    result = client.call(method, [filters or []])
    assert isinstance(result, list)
    return result


def ensure_service_enabled_and_running(client: JsonRpcWebSocket, service: str) -> dict[str, object]:
    services = query_all(client, "service.query", [["service", "=", service]])
    if not services:
        raise WebSocketError(f"TrueNAS service not found: {service}")
    current = services[0]
    assert isinstance(current, dict)
    changed = False
    if current.get("enable") is not True:
        client.call("service.update", [service, {"enable": True}])
        changed = True
    if current.get("state") != "RUNNING":
        started = client.call("service.start", [service, {"silent": False, "timeout": 120}])
        if started is not True:
            raise WebSocketError(f"TrueNAS service did not start: {service}")
        changed = True
    else:
        reloaded = client.call("service.reload", [service, {"silent": False, "timeout": 120}])
        if reloaded is not True:
            raise WebSocketError(f"TrueNAS service did not reload: {service}")
    refreshed = query_all(client, "service.query", [["service", "=", service]])
    status = refreshed[0] if refreshed else current
    assert isinstance(status, dict)
    return {
        "service": service,
        "changed": changed,
        "enable": status.get("enable"),
        "state": status.get("state"),
    }


def ensure_iscsi_portal(client: JsonRpcWebSocket, ip: str) -> tuple[dict[str, object], bool]:
    for portal in query_all(client, "iscsi.portal.query"):
        assert isinstance(portal, dict)
        listen = portal.get("listen", [])
        if isinstance(listen, list) and any(isinstance(item, dict) and item.get("ip") == ip for item in listen):
            return portal, False
    portal = client.call(
        "iscsi.portal.create",
        [{"listen": [{"ip": ip}], "comment": "truenas-libvirt-provider"}],
    )
    assert isinstance(portal, dict)
    return portal, True


def initiator_group_has_iqns(group: dict[str, object], iqns: list[str]) -> bool:
    initiators = group.get("initiators", [])
    if not isinstance(initiators, list):
        return False
    current = {str(item) for item in initiators}
    return all(iqn in current for iqn in iqns)


def ensure_iscsi_initiator_group(client: JsonRpcWebSocket, iqns: list[str]) -> tuple[dict[str, object], bool]:
    for group in query_all(client, "iscsi.initiator.query"):
        assert isinstance(group, dict)
        if initiator_group_has_iqns(group, iqns):
            return group, False
    group = client.call(
        "iscsi.initiator.create",
        [{"initiators": iqns, "comment": "truenas-libvirt-provider"}],
    )
    assert isinstance(group, dict)
    return group, True


def ensure_iscsi_initiator_group_members(client: JsonRpcWebSocket, group_id: int, iqns: list[str]) -> tuple[dict[str, object], bool]:
    groups = query_all(client, "iscsi.initiator.query", [["id", "=", group_id]])
    if not groups:
        raise WebSocketError(f"iSCSI initiator group not found: {group_id}")
    group = groups[0]
    assert isinstance(group, dict)
    if initiator_group_has_iqns(group, iqns):
        return group, False
    initiators = group.get("initiators", [])
    current = [str(item) for item in initiators] if isinstance(initiators, list) else []
    merged = current + [iqn for iqn in iqns if iqn not in current]
    updated = client.call("iscsi.initiator.update", [group_id, {"initiators": merged}])
    assert isinstance(updated, dict)
    return updated, True


def ensure_iscsi_target(
    client: JsonRpcWebSocket,
    name: str,
    alias: str,
    portal_id: int,
    initiator_id: int,
    iqns: list[str],
) -> tuple[dict[str, object], bool]:
    desired_group = {"portal": portal_id, "initiator": initiator_id, "authmethod": "NONE"}
    for target in query_all(client, "iscsi.target.query"):
        assert isinstance(target, dict)
        if target.get("name") == name or target.get("alias") == alias:
            groups = target.get("groups")
            if not isinstance(groups, list):
                groups = []
            for group in groups:
                if not isinstance(group, dict):
                    continue
                if group.get("portal") != portal_id:
                    continue
                existing_initiator_id = group.get("initiator")
                if existing_initiator_id == initiator_id:
                    return target, False
                if isinstance(existing_initiator_id, int):
                    _, changed = ensure_iscsi_initiator_group_members(client, existing_initiator_id, iqns)
                    return target, changed
            updated_groups = [group for group in groups if isinstance(group, dict)] + [desired_group]
            updated = client.call("iscsi.target.update", [target["id"], {"groups": updated_groups}])
            assert isinstance(updated, dict)
            return updated, True
    target = client.call(
        "iscsi.target.create",
        [
            {
                "name": name,
                "alias": alias,
                "groups": [
                    {
                        "portal": portal_id,
                        "initiator": initiator_id,
                        "authmethod": "NONE",
                    }
                ],
            }
        ],
    )
    assert isinstance(target, dict)
    return target, True


def ensure_iscsi_extent(client: JsonRpcWebSocket, name: str, zvol: str) -> tuple[dict[str, object], bool]:
    disk = f"zvol/{zvol}"
    for extent in query_all(client, "iscsi.extent.query", [["name", "=", name]]):
        assert isinstance(extent, dict)
        return extent, False
    extent = client.call(
        "iscsi.extent.create",
        [
            {
                "name": name,
                "type": "DISK",
                "disk": disk,
                "blocksize": 512,
                "pblocksize": True,
                "comment": zvol,
            }
        ],
    )
    assert isinstance(extent, dict)
    return extent, True


def ensure_iscsi_mapping(
    client: JsonRpcWebSocket,
    target_id: int,
    extent_id: int,
) -> tuple[dict[str, object], bool]:
    for mapping in query_all(client, "iscsi.targetextent.query"):
        assert isinstance(mapping, dict)
        if mapping.get("target") == target_id and mapping.get("extent") == extent_id:
            return mapping, False
    mapping = client.call(
        "iscsi.targetextent.create",
        [{"target": target_id, "extent": extent_id, "lunid": 0}],
    )
    assert isinstance(mapping, dict)
    return mapping, True


def cmd_iscsi_export(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    zvol = managed_zvol_name(args.pool, args.name, config)
    truenas = config["truenas"]
    assert isinstance(truenas, dict)
    target_ip = args.target_ip or truenas.get("target_ip") or "10.6.0.119"
    assert isinstance(target_ip, str)
    iqns = [local_iscsi_iqn()]
    target_name = args.target_name or managed_export_name(args.name)
    with open_client(config) as client:
        login(client, config)
        if not dataset_exists(client, zvol):
            raise ValueError(f"zvol does not exist: {zvol}")
        portal, portal_changed = ensure_iscsi_portal(client, target_ip)
        initiator, initiator_changed = ensure_iscsi_initiator_group(client, iqns)
        target, target_changed = ensure_iscsi_target(
            client,
            target_name,
            zvol,
            int(portal["id"]),
            int(initiator["id"]),
            iqns,
        )
        extent, extent_changed = ensure_iscsi_extent(client, target_name, zvol)
        mapping, mapping_changed = ensure_iscsi_mapping(client, int(target["id"]), int(extent["id"]))
        service = ensure_service_enabled_and_running(client, "iscsitarget")
    print(
        json.dumps(
            {
                "target_iqn": f"iqn.2005-10.org.freenas.ctl:{target.get('name', target_name)}",
                "service": service,
                "portal": {"id": portal["id"], "changed": portal_changed},
                "initiator": {"id": initiator["id"], "changed": initiator_changed},
                "target": {"id": target["id"], "changed": target_changed},
                "extent": {"id": extent["id"], "changed": extent_changed},
                "mapping": {"id": mapping["id"], "changed": mapping_changed},
            },
            indent=2,
            sort_keys=True,
        )
    )



def ensure_nvmet_host(client: JsonRpcWebSocket, hostnqn: str) -> tuple[dict[str, object], bool]:
    for host in query_all(client, "nvmet.host.query", [["hostnqn", "=", hostnqn]]):
        assert isinstance(host, dict)
        return host, False
    host = client.call("nvmet.host.create", [{"hostnqn": hostnqn}])
    assert isinstance(host, dict)
    return host, True


def ensure_nvmet_subsys(client: JsonRpcWebSocket, name: str) -> tuple[dict[str, object], bool]:
    for subsys in query_all(client, "nvmet.subsys.query", [["name", "=", name]]):
        assert isinstance(subsys, dict)
        return subsys, False
    subsys = client.call("nvmet.subsys.create", [{"name": name, "allow_any_host": False}])
    assert isinstance(subsys, dict)
    return subsys, True


def object_ref_id(value: object) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, dict) and isinstance(value.get("id"), int):
        return int(value["id"])
    return None


def get_nvmet_subsys(client: JsonRpcWebSocket, subsys_id: int) -> dict[str, object]:
    for subsys in query_all(client, "nvmet.subsys.query", [["id", "=", subsys_id]]):
        assert isinstance(subsys, dict)
        return subsys
    raise WebSocketError(f"NVMe-oF subsystem not found: {subsys_id}")


def ensure_nvmet_namespace(
    client: JsonRpcWebSocket,
    subsys_id: int,
    zvol: str,
) -> tuple[dict[str, object], bool]:
    device_path = f"zvol/{zvol}"
    for namespace in query_all(client, "nvmet.namespace.query"):
        assert isinstance(namespace, dict)
        if namespace.get("device_path") == device_path:
            return namespace, False
    namespace = client.call(
        "nvmet.namespace.create",
        [
            {
                "device_type": "ZVOL",
                "device_path": device_path,
                "subsys_id": subsys_id,
                "enabled": True,
            }
        ],
    )
    assert isinstance(namespace, dict)
    return namespace, True


def ensure_nvmet_port(client: JsonRpcWebSocket, target_ip: str, port_number: int) -> tuple[dict[str, object], bool]:
    for port in query_all(client, "nvmet.port.query"):
        assert isinstance(port, dict)
        if (
            port.get("addr_trtype") == "TCP"
            and port.get("addr_traddr") == target_ip
            and int(port.get("addr_trsvcid", 0)) == port_number
        ):
            return port, False
    port = client.call(
        "nvmet.port.create",
        [
            {
                "addr_trtype": "TCP",
                "addr_traddr": target_ip,
                "addr_trsvcid": port_number,
                "enabled": True,
            }
        ],
    )
    assert isinstance(port, dict)
    return port, True


def association_matches(value: object, expected_id: int) -> bool:
    return value == expected_id or (isinstance(value, dict) and value.get("id") == expected_id)


def ensure_nvmet_host_subsys(
    client: JsonRpcWebSocket,
    host_id: int,
    subsys_id: int,
) -> tuple[dict[str, object], bool]:
    for association in query_all(client, "nvmet.host_subsys.query"):
        assert isinstance(association, dict)
        if association_matches(association.get("host"), host_id) and association_matches(association.get("subsys"), subsys_id):
            return association, False
    association = client.call("nvmet.host_subsys.create", [{"host_id": host_id, "subsys_id": subsys_id}])
    assert isinstance(association, dict)
    return association, True


def ensure_nvmet_port_subsys(
    client: JsonRpcWebSocket,
    port_id: int,
    subsys_id: int,
) -> tuple[dict[str, object], bool]:
    for association in query_all(client, "nvmet.port_subsys.query"):
        assert isinstance(association, dict)
        if association_matches(association.get("port"), port_id) and association_matches(association.get("subsys"), subsys_id):
            return association, False
    association = client.call("nvmet.port_subsys.create", [{"port_id": port_id, "subsys_id": subsys_id}])
    assert isinstance(association, dict)
    return association, True


def cmd_nvmeof_export(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    zvol = managed_zvol_name(args.pool, args.name, config)
    truenas = config["truenas"]
    assert isinstance(truenas, dict)
    target_ip = args.target_ip or truenas.get("target_ip") or "10.6.0.119"
    assert isinstance(target_ip, str)
    hostnqns = [local_nvme_nqn()]
    subsys_name = args.subsys_name or managed_export_name(args.name)
    with open_client(config) as client:
        login(client, config)
        if not dataset_exists(client, zvol):
            raise ValueError(f"zvol does not exist: {zvol}")
        subsys, subsys_changed = ensure_nvmet_subsys(client, subsys_name)
        namespace, namespace_changed = ensure_nvmet_namespace(client, int(subsys["id"]), zvol)
        namespace_subsys_id = object_ref_id(namespace.get("subsys"))
        if namespace_subsys_id is not None and namespace_subsys_id != int(subsys["id"]):
            subsys = get_nvmet_subsys(client, namespace_subsys_id)
            subsys_changed = False
        port, port_changed = ensure_nvmet_port(client, target_ip, args.port)
        port_assoc, port_assoc_changed = ensure_nvmet_port_subsys(client, int(port["id"]), int(subsys["id"]))
        host_results = []
        for hostnqn in hostnqns:
            host, host_changed = ensure_nvmet_host(client, hostnqn)
            host_assoc, host_assoc_changed = ensure_nvmet_host_subsys(client, int(host["id"]), int(subsys["id"]))
            host_results.append(
                {
                    "hostnqn": hostnqn,
                    "host_id": host["id"],
                    "host_changed": host_changed,
                    "association_id": host_assoc["id"],
                    "association_changed": host_assoc_changed,
                }
            )
        service = ensure_service_enabled_and_running(client, "nvmet")
    print(
        json.dumps(
            {
                "target": f"{target_ip}:{args.port}",
                "subnqn": subsys.get("subnqn"),
                "service": service,
                "subsys": {"id": subsys["id"], "changed": subsys_changed},
                "namespace": {"id": namespace["id"], "changed": namespace_changed},
                "port": {"id": port["id"], "changed": port_changed},
                "port_association": {"id": port_assoc["id"], "changed": port_assoc_changed},
                "hosts": host_results,
            },
            indent=2,
            sort_keys=True,
        )
    )

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.json")
    subparsers = parser.add_subparsers(required=True)

    call_parser = subparsers.add_parser("call")
    call_parser.add_argument("method")
    call_parser.add_argument("params", nargs="?")
    call_parser.set_defaults(func=cmd_call)

    pool_parser = subparsers.add_parser("pool-list")
    pool_parser.set_defaults(func=cmd_pool_list)

    method_list_parser = subparsers.add_parser("method-list")
    method_list_parser.add_argument("prefix", nargs="*")
    method_list_parser.set_defaults(func=cmd_method_list)

    method_info_parser = subparsers.add_parser("method-info")
    method_info_parser.add_argument("method", nargs="+")
    method_info_parser.set_defaults(func=cmd_method_info)

    namespace_parser = subparsers.add_parser("namespace-ensure")
    namespace_parser.add_argument("pool")
    namespace_parser.set_defaults(func=cmd_namespace_ensure)

    zvol_list_parser = subparsers.add_parser("zvol-list")
    zvol_list_parser.add_argument("pool")
    zvol_list_parser.set_defaults(func=cmd_zvol_list)

    zvol_create_parser = subparsers.add_parser("zvol-create")
    zvol_create_parser.add_argument("pool")
    zvol_create_parser.add_argument("name")
    zvol_create_parser.add_argument("size")
    zvol_create_parser.add_argument("--volblocksize", default="16K")
    zvol_create_parser.add_argument("--transport", choices=["iscsi", "nvmeof"], default="iscsi")
    zvol_create_parser.add_argument("--thick", action="store_false", dest="sparse")
    zvol_create_parser.set_defaults(func=cmd_zvol_create, sparse=True)

    iscsi_export_parser = subparsers.add_parser("iscsi-export")
    iscsi_export_parser.add_argument("pool")
    iscsi_export_parser.add_argument("name")
    iscsi_export_parser.add_argument("--target-ip")
    iscsi_export_parser.add_argument("--target-name")
    iscsi_export_parser.set_defaults(func=cmd_iscsi_export)

    nvmeof_export_parser = subparsers.add_parser("nvmeof-export")
    nvmeof_export_parser.add_argument("pool")
    nvmeof_export_parser.add_argument("name")
    nvmeof_export_parser.add_argument("--target-ip")
    nvmeof_export_parser.add_argument("--port", type=int, default=4420)
    nvmeof_export_parser.add_argument("--subsys-name")
    nvmeof_export_parser.set_defaults(func=cmd_nvmeof_export)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        args.func(args)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

