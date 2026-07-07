#!/usr/bin/env python3
"""Collect best-effort diagnostics for a failed Subvirt candidate run."""

from __future__ import annotations

import argparse
import json
import shlex
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any


def q(value: object) -> str:
    return shlex.quote(str(value))


def load_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def is_local_host(host: str) -> bool:
    return host in {"localhost", "127.0.0.1", "::1", socket.gethostname(), socket.getfqdn()}


def ssh_identity_args(config: dict[str, Any]) -> list[str]:
    args: list[str] = []
    seen: set[str] = set()
    for item in config.get("ssh", {}).get("identity_files", []):
        expanded = str(Path(item).expanduser())
        if expanded in seen or not Path(expanded).exists():
            continue
        seen.add(expanded)
        args.extend(["-i", expanded])
    return args


def ssh_args(config: dict[str, Any]) -> list[str]:
    args = [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=8",
        "-o", "StrictHostKeyChecking=accept-new",
    ]
    known_hosts = config.get("ssh", {}).get("known_hosts_file")
    if known_hosts:
        args.extend(["-o", f"UserKnownHostsFile={Path(known_hosts).expanduser()}"])
    args.extend(ssh_identity_args(config))
    return args


def run(argv: list[str], timeout: int = 45) -> tuple[int, str]:
    try:
        result = subprocess.run(argv, check=False, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
        return result.returncode, result.stdout
    except FileNotFoundError as exc:
        return 127, str(exc) + "\n"
    except subprocess.TimeoutExpired as exc:
        output = exc.stdout or ""
        if isinstance(output, bytes):
            output = output.decode(errors="replace")
        return 124, output + f"\nTIMEOUT after {timeout}s\n"


def run_shell(command: str, timeout: int = 45) -> tuple[int, str]:
    return run(["bash", "-lc", command], timeout=timeout)


def remote(host: str, command: str, config: dict[str, Any], timeout: int = 45) -> tuple[int, str]:
    if is_local_host(host):
        return run_shell(command, timeout=timeout)
    return run([*ssh_args(config), host, command], timeout=timeout)


def write_command(path: Path, title: str, rc: int, output: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"$ {title}\n# exit={rc}\n{output}", encoding="utf-8", errors="replace")


def capture_local(out: Path, name: str, command: str, timeout: int = 45) -> None:
    rc, output = run_shell(command, timeout=timeout)
    write_command(out / name, command, rc, output)


def capture_remote(out: Path, host: str, name: str, command: str, config: dict[str, Any], timeout: int = 45) -> None:
    rc, output = remote(host, command, config, timeout=timeout)
    write_command(out / host_label(host) / name, f"{host}: {command}", rc, output)


def host_label(host: str) -> str:
    return host.replace("@", "_").replace("/", "_").replace(":", "_")


def maybe_copy(src: Path, dst: Path) -> None:
    try:
        if src.is_file():
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(src.read_bytes())
    except OSError as exc:
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(f"failed to copy {src}: {exc}\n", encoding="utf-8")


def collect_lab_host(out: Path, build_id: str, config: dict[str, Any]) -> None:
    lab = config.get("lab", {})
    host = lab.get("host") or config.get("hosts", {}).get("build")
    if not host:
        return
    lab_config = lab.get("config", "/srv/subvirt/release/lab.json")
    artifact_root = config.get("project", {}).get("artifact_dir", "/srv/subvirt/artifacts")
    commands = {
        "virsh-list.txt": "virsh -c qemu:///system list --all",
        "runner-processes.txt": "ps -eo pid,ppid,stat,etime,cmd | grep -E 'run-candidate-release|lab.py|virt-install|dnf|apt-get|virsh|qemu-img' | grep -v grep || true",
        "lab-run-files.txt": f"find /srv/subvirt/lab/runs/{q(build_id)} -maxdepth 3 -printf '%M %u %g %s %p\\n' 2>/dev/null | sort || true",
        "artifact-files.txt": f"find {q(str(Path(artifact_root) / build_id))} -maxdepth 3 -printf '%M %u %g %s %p\\n' 2>/dev/null | sort || true",
        "journal-libvirt.txt": "journalctl -u libvirtd -u virtqemud -u virtstoraged --no-pager -n 250 2>/dev/null || true",
    }
    for filename, command in commands.items():
        capture_remote(out, host, filename, command, config, timeout=60)
    lab_summary = """python3 - <<'PY'
import json
p={lab_config!r}
data=json.load(open(p))
secret_words = ('api_key', 'password', 'secret', 'token')
def redact(value):
    if isinstance(value, dict):
        return {{k: ('<redacted>' if any(word in str(k).lower() for word in secret_words) else redact(v)) for k, v in value.items()}}
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value
for key in ('lab','truenas','tests','vms'):
    print('[' + key + ']')
    print(json.dumps(redact(data.get(key, {{}})), indent=2, sort_keys=True))
PY""".format(lab_config=lab_config)
    capture_remote(out, host, "lab-config-summary.txt", lab_summary, config, timeout=30)


def collect_test_hosts(out: Path, config: dict[str, Any]) -> None:
    hosts = config.get("hosts", {})
    candidates = {
        "ubuntu_test": hosts.get("ubuntu_test"),
        "alma_test": hosts.get("alma_test"),
        "migration_source": hosts.get("migration_source"),
        "migration_target": hosts.get("migration_target"),
    }
    seen: set[str] = set()
    for role, host in candidates.items():
        if not host or host in seen:
            continue
        seen.add(host)
        base = out / f"{role}-{host_label(host)}"
        commands = {
            "identity.txt": "hostname; uname -a; cat /etc/os-release 2>/dev/null || true",
            "subvirt-packages.txt": "command -v dpkg-query >/dev/null && dpkg-query -W 'truenas-libvirt-provider' 'libvirt*' 'virt-manager' 'virtinst' 2>/dev/null || true; command -v rpm >/dev/null && rpm -qa | grep -E '^(truenas-libvirt-provider|libvirt|virt-manager|virt-install)' | sort || true",
            "provider-service.txt": "systemctl status truenas-libvirt-provider.service --no-pager 2>&1 || true; systemctl cat truenas-libvirt-provider.service 2>&1 || true",
            "provider-doctor.txt": "/usr/libexec/truenas-libvirt/truenas_provider_daemon.py doctor --json 2>&1 || true",
            "provider-journal.txt": "journalctl -u truenas-libvirt-provider.service --no-pager -n 250 2>/dev/null || true",
            "libvirt-status.txt": "systemctl status libvirtd virtqemud virtstoraged --no-pager 2>&1 || true",
            "virsh-storage.txt": "virsh pool-list --all --details 2>&1 || true; virsh pool-capabilities 2>&1 | sed -n '1,220p' || true",
            "apt-history.txt": "cat /var/log/apt/history.log 2>/dev/null || true; tail -300 /var/log/dpkg.log 2>/dev/null || true",
            "dnf-history.txt": "tail -300 /var/log/dnf.log 2>/dev/null || true; tail -300 /var/log/dnf.rpm.log 2>/dev/null || true",
        }
        role_dir = out / f"{role}-{host_label(host)}"
        for filename, command in commands.items():
            rc, output = remote(host, command, config, timeout=60)
            write_command(role_dir / filename, f"{host}: {command}", rc, output)


def collect_summary(out: Path, build_id: str, config_path: Path, config: dict[str, Any]) -> None:
    data = {
        "build_id": build_id,
        "config": str(config_path),
        "build_host": config.get("hosts", {}).get("build"),
        "lab_enabled": bool(config.get("lab", {}).get("enabled")),
        "lab_host": config.get("lab", {}).get("host"),
        "artifact_dir": config.get("project", {}).get("artifact_dir"),
    }
    (out / "summary.json").write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main(argv: list[str] = sys.argv[1:]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--build-id", required=True)
    parser.add_argument("--config", default="/srv/subvirt/release/release.json", type=Path)
    parser.add_argument("--artifact-root", default="artifacts", type=Path)
    parser.add_argument("--candidate-log", default="")
    args = parser.parse_args(argv)

    out = args.artifact_root / args.build_id / "diagnostics"
    out.mkdir(parents=True, exist_ok=True)
    config = load_config(args.config)
    collect_summary(out, args.build_id, args.config, config)
    if args.candidate_log:
        maybe_copy(Path(args.candidate_log), out / "candidate-release.log")
    capture_local(out, "local-processes.txt", "ps -eo pid,ppid,stat,etime,cmd | grep -E 'run-candidate-release|lab.py|gh run|ssh ' | grep -v grep || true")
    collect_lab_host(out / "lab-host", args.build_id, config)
    collect_test_hosts(out / "test-hosts", config)
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
