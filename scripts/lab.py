#!/usr/bin/env python3
"""Ephemeral Subvirt libvirt lab orchestration.

This script is intended to run on the VM factory host, normally subvirt-build.
It creates disposable Ubuntu, AlmaLinux, and TrueNAS VMs, publishes a per-run
repository with the same public layout as the stable repo, and drives package
manager based test installation.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import textwrap
import time
from urllib.parse import urlparse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Lab:
    config: dict[str, Any]
    execute: bool
    build_id: str

    @property
    def workdir(self) -> Path:
        return Path(self.config["lab"]["workdir"])

    @property
    def run_dir(self) -> Path:
        return self.workdir / "runs" / self.build_id

    @property
    def image_dir(self) -> Path:
        return self.workdir / "images"

    @property
    def cache_dir(self) -> Path:
        return self.workdir / "cache"

    @property
    def web_root(self) -> Path:
        return self.run_dir / "www"

    @property
    def gpg_home(self) -> Path:
        return self.workdir / "gnupg"


def q(value: object) -> str:
    import shlex
    return shlex.quote(str(value))


def run(argv: list[str], execute: bool = True, env: dict[str, str] | None = None, input_text: str | None = None) -> str:
    print("+ " + " ".join(q(part) for part in argv), flush=True)
    if not execute:
        return ""
    result = subprocess.run(argv, check=False, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            env=env, input=input_text)
    if result.stdout:
        print(result.stdout, end="")
    if result.returncode:
        raise subprocess.CalledProcessError(result.returncode, argv, output=result.stdout)
    return result.stdout


def run_shell(command: str, execute: bool = True, env: dict[str, str] | None = None) -> str:
    return run(["bash", "-lc", command], execute=execute, env=env)


def load_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def ensure_dirs(lab: Lab) -> None:
    for path in (lab.workdir, lab.run_dir, lab.image_dir, lab.cache_dir, lab.web_root, lab.gpg_home):
        if lab.execute:
            path.mkdir(parents=True, exist_ok=True)
        else:
            print(f"+ mkdir -p {q(path)}")


def bootstrap_host(lab: Lab) -> None:
    packages = [
        "qemu-kvm", "libvirt-daemon-system", "libvirt-clients", "virtinst", "qemu-utils",
        "cloud-image-utils", "genisoimage", "nginx", "gnupg", "createrepo-c", "rpm",
        "python3", "python3-libvirt", "curl", "xorriso", "whois",
    ]
    run(["apt-get", "update"], lab.execute)
    run(["apt-get", "install", "-y", *packages], lab.execute)
    run(["systemctl", "enable", "--now", "libvirtd", "nginx"], lab.execute)
    ensure_dirs(lab)
    configure_nginx(lab)


def configure_nginx(lab: Lab) -> None:
    listen = lab.config["lab"].get("http_listen", "192.168.150.1:8080")
    root = lab.workdir / "current-www"
    conf = f"""server {{
    listen {listen};
    server_name _;
    root {root};
    autoindex on;
}}
"""
    path = Path("/etc/nginx/sites-available/subvirt-lab")
    if lab.execute:
        path.write_text(conf, encoding="utf-8")
        enabled = Path("/etc/nginx/sites-enabled/subvirt-lab")
        if not enabled.exists():
            enabled.symlink_to(path)
        run(["nginx", "-t"], True)
        run(["systemctl", "reload", "nginx"], True)
    else:
        print(f"+ write {path}\n{conf}")


def virsh(*args: str, lab: Lab) -> str:
    return run(["virsh", "-c", "qemu:///system", *args], lab.execute)


def download(url: str, dst: Path, sha256: str | None, execute: bool) -> None:
    if execute and dst.exists():
        return
    print(f"+ download {url} -> {dst}")
    if not execute:
        return
    tmp = dst.with_suffix(dst.suffix + ".tmp")
    tmp.unlink(missing_ok=True)
    with urllib.request.urlopen(url, timeout=120) as response, tmp.open("wb") as out:
        shutil.copyfileobj(response, out, length=1024 * 1024)
    tmp.replace(dst)
    if sha256:
        import hashlib
        actual = hashlib.sha256(dst.read_bytes()).hexdigest()
        if actual.lower() != sha256.lower():
            dst.unlink(missing_ok=True)
            raise SystemExit(f"sha256 mismatch for {dst}: expected {sha256}, got {actual}")


def ensure_network(lab: Lab, key: str) -> None:
    net = lab.config["networks"][key]
    name = net["name"]
    mode = net["mode"]
    bridge = net["bridge"]
    if len(bridge) > 15:
        raise SystemExit(f"network {name!r} bridge {bridge!r} is too long; Linux interface names must be 15 characters or fewer")
    ip = net["gateway"]
    prefix = net["prefix"]
    if mode == "nat":
        forward = "<forward mode='nat'/>"
    elif mode == "isolated":
        forward = ""
    else:
        raise SystemExit(f"unsupported network mode {mode!r}")
    xml = f"""<network>
  <name>{name}</name>
  {forward}
  <bridge name='{bridge}' stp='on' delay='0'/>
  <ip address='{ip}' prefix='{prefix}'/>
</network>
"""
    xml_path = lab.run_dir / f"network-{name}.xml"
    if lab.execute:
        xml_path.write_text(xml, encoding="utf-8")
    existing = virsh("net-list", "--all", lab=lab)
    active = False
    for line in existing.splitlines():
        parts = line.split()
        if parts and parts[0] == name:
            active = len(parts) > 1 and parts[1] == "active"
            break
    if name not in existing:
        virsh("net-define", str(xml_path), lab=lab)
    if not active:
        virsh("net-start", name, lab=lab)
    virsh("net-autostart", name, lab=lab)


def ensure_networks(lab: Lab) -> None:
    ensure_network(lab, "management")
    ensure_network(lab, "storage")


def mac_for(build_id: str, offset: int) -> str:
    import hashlib
    digest = hashlib.sha256(f"{build_id}:{offset}".encode()).digest()
    return "52:54:%02x:%02x:%02x:%02x" % (digest[0], digest[1], digest[2], digest[3])


def ssh_keys(config: dict[str, Any]) -> list[str]:
    keys = config["lab"].get("ssh_authorized_keys", [])
    key_files = config["lab"].get("ssh_authorized_key_files", ["~/.ssh/virt.pub", "~/.ssh/id_rsa.pub"])
    for item in key_files:
        path = Path(item).expanduser()
        if path.exists():
            keys.append(path.read_text(encoding="utf-8").strip())
    keys = [key for key in keys if key]
    if not keys:
        raise SystemExit("no SSH public key configured; set lab.ssh_authorized_keys or lab.ssh_authorized_key_files")
    return keys


def ssh_identity_args(config: dict[str, Any]) -> list[str]:
    identities = list(config["lab"].get("ssh_identity_files", []))
    for item in config["lab"].get("ssh_authorized_key_files", []):
        if item.endswith(".pub"):
            identities.append(item[:-4])
    args: list[str] = []
    seen: set[str] = set()
    for item in identities:
        expanded = str(Path(item).expanduser())
        if expanded in seen or not Path(expanded).exists():
            continue
        seen.add(expanded)
        args.extend(["-i", expanded])
    return args


def ssh_base_args(lab: Lab) -> list[str]:
    known_hosts = lab.run_dir / "known_hosts"
    return [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", f"UserKnownHostsFile={known_hosts}",
        *ssh_identity_args(lab.config),
    ]


def clear_known_host(lab: Lab, host: str) -> None:
    known_hosts = lab.run_dir / "known_hosts"
    if lab.execute:
        known_hosts.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["ssh-keygen", "-R", host, "-f", str(known_hosts)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    else:
        print(f"+ ssh-keygen -R {host} -f {known_hosts}")


def os_mirror_url(lab: Lab, distro: str) -> str:
    mirrors = lab.config.get("lab", {}).get("mirrors", {})
    defaults = {
        "ubuntu": "http://10.1.0.121/ubuntu",
        "alma": "http://10.1.0.121/almalinux",
    }
    return str(mirrors.get(distro, defaults[distro])).rstrip("/")


def ubuntu_mirror_rewrite_command(lab: Lab) -> str:
    mirror = q(os_mirror_url(lab, "ubuntu"))
    return f"""mirror={mirror}
if [ -f /etc/apt/sources.list ]; then
  sed -i -E "s|https?://(archive|security|cloud\\.archive|ports)\\.ubuntu\\.com/ubuntu|$mirror|g" /etc/apt/sources.list
fi
if ls /etc/apt/sources.list.d/*.sources >/dev/null 2>&1; then
  sed -i -E "s|^URIs: .*$|URIs: $mirror|g" /etc/apt/sources.list.d/*.sources
fi"""


def cloud_init_repo_bootcmd(lab: Lab, distro: str) -> str:
    if distro == "ubuntu":
        return f"""bootcmd:
  - |
    set -eu
{textwrap.indent(ubuntu_mirror_rewrite_command(lab), '    ')}
"""
    if distro == "alma":
        mirror = q(os_mirror_url(lab, "alma"))
        return f"""bootcmd:
  - |
    set -eu
    mirror={mirror}
    if ls /etc/yum.repos.d/*.repo >/dev/null 2>&1; then
      sed -i -E 's|^mirrorlist=|#mirrorlist=|; s|^metalink=|#metalink=|' /etc/yum.repos.d/*.repo
      sed -i -E "s|^# ?baseurl=https?://repo\\.almalinux\\.org/almalinux|baseurl=$mirror|; s|^baseurl=https?://repo\\.almalinux\\.org/almalinux|baseurl=$mirror|" /etc/yum.repos.d/*.repo
    fi
"""
    return ""


def ubuntu_refresh_command() -> str:
    return """DEBIAN_FRONTEND=noninteractive apt-get full-upgrade -y --allow-downgrades -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold
if command -v snap >/dev/null 2>&1; then
  snap refresh
fi"""


def write_cloud_init(lab: Lab, name: str, distro: str, mgmt_mac: str, storage_mac: str, mgmt_ip: str, storage_ip: str) -> Path:
    keys = "\n".join(f"      - {key}" for key in ssh_keys(lab.config))
    package_update = "true" if distro == "ubuntu" else "false"
    packages = ["qemu-guest-agent", "openssh-server", "curl", "rsync"]
    if distro == "ubuntu":
        packages += ["open-iscsi", "nvme-cli", "udev"]
    else:
        packages += ["iscsi-initiator-utils", "nvme-cli", "device-mapper", "lvm2"]
    user_data = f"""#cloud-config
hostname: {name}
manage_etc_hosts: true
disable_root: false
ssh_pwauth: false
{cloud_init_repo_bootcmd(lab, distro)}package_update: {package_update}
packages:
{chr(10).join('  - ' + p for p in packages)}
users:
  - name: root
    lock_passwd: false
    ssh_authorized_keys:
{keys}
runcmd:
  - systemctl enable --now qemu-guest-agent || true
  - systemctl enable --now ssh || systemctl enable --now sshd || true
"""
    mgmt = lab.config["networks"]["management"]
    dns = lab.config["lab"].get("dns", ["1.1.1.1", "8.8.8.8"])
    network = {
        "version": 2,
        "ethernets": {
            "mgmt0": {
                "match": {"macaddress": mgmt_mac},
                "set-name": "mgmt0",
                "addresses": [f"{mgmt_ip}/{mgmt['prefix']}"],
                "gateway4": mgmt["gateway"],
                "nameservers": {"addresses": dns},
            },
            "storage0": {
                "match": {"macaddress": storage_mac},
                "set-name": "storage0",
                "addresses": [f"{storage_ip}/{lab.config['networks']['storage']['prefix']}"],
            },
        },
    }
    seed = lab.run_dir / f"{name}-seed.iso"
    user = lab.run_dir / f"{name}-user-data.yaml"
    net = lab.run_dir / f"{name}-network-config.yaml"
    meta = lab.run_dir / f"{name}-meta-data.yaml"
    network_yaml = f"""version: 2
ethernets:
  mgmt0:
    match:
      macaddress: "{mgmt_mac}"
    set-name: mgmt0
    addresses:
      - {mgmt_ip}/{mgmt['prefix']}
    gateway4: {mgmt['gateway']}
    nameservers:
      addresses:
{chr(10).join('        - ' + item for item in dns)}
  storage0:
    match:
      macaddress: "{storage_mac}"
    set-name: storage0
    addresses:
      - {storage_ip}/{lab.config['networks']['storage']['prefix']}
"""
    if lab.execute:
        user.write_text(user_data, encoding="utf-8")
        net.write_text(network_yaml, encoding="utf-8")
        meta.write_text(f"instance-id: {name}-{lab.build_id}\nlocal-hostname: {name}\n", encoding="utf-8")
        run(["cloud-localds", "--network-config", str(net), str(seed), str(user), str(meta)], True)
    else:
        print(f"+ write cloud-init seed {seed}")
    return seed


def image_path(lab: Lab, vm_key: str) -> Path:
    vm = lab.config["vms"][vm_key]
    return lab.image_dir / f"{lab.build_id}-{vm['name']}.qcow2"


def ensure_base_image(lab: Lab, vm_key: str) -> Path:
    vm = lab.config["vms"][vm_key]
    url = vm["image_url"]
    base = lab.cache_dir / Path(url).name
    download(url, base, vm.get("image_sha256"), lab.execute)
    return base


def create_cloud_vm(lab: Lab, vm_key: str, offset: int) -> None:
    vm = lab.config["vms"][vm_key]
    name = f"{lab.config['lab']['name_prefix']}-{lab.build_id}-{vm['name']}"
    if name in virsh("list", "--all", lab=lab):
        return
    base = ensure_base_image(lab, vm_key)
    disk = image_path(lab, vm_key)
    if not disk.exists() or not lab.execute:
        run(["qemu-img", "create", "-f", "qcow2", "-F", "qcow2", "-b", str(base), str(disk), vm["disk_size"]], lab.execute)
    mgmt_mac = mac_for(lab.build_id, offset)
    storage_mac = mac_for(lab.build_id, offset + 1000)
    seed = write_cloud_init(lab, name, vm_distro(lab, vm_key), mgmt_mac, storage_mac, vm["management_ip"], vm["storage_ip"])
    cmd = [
        "virt-install", "--connect", "qemu:///system", "--name", name,
        "--memory", str(vm["memory_mib"]), "--vcpus", str(vm["vcpus"]),
        "--import", "--os-variant", vm["os_variant"],
        "--disk", f"path={disk},format=qcow2,bus=virtio",
        "--disk", f"path={seed},device=cdrom",
        "--network", f"network={lab.config['networks']['management']['name']},model=virtio,mac={mgmt_mac}",
        "--network", f"network={lab.config['networks']['storage']['name']},model=virtio,mac={storage_mac}",
        "--graphics", "none", "--noautoconsole",
    ]
    if vm.get("firmware") == "efi":
        cmd += ["--boot", "uefi"]
    run(cmd, lab.execute)


def create_truenas_vm(lab: Lab) -> None:
    vm = lab.config["vms"]["truenas"]
    name = f"{lab.config['lab']['name_prefix']}-{lab.build_id}-{vm['name']}"
    disk = image_path(lab, "truenas")
    mode = vm.get("install_mode", "iso")
    if mode == "golden" and vm.get("golden_image"):
        golden = Path(vm["golden_image"])
        if not disk.exists() or not lab.execute:
            run(["qemu-img", "create", "-f", "qcow2", "-F", "qcow2", "-b", str(golden), str(disk), vm["boot_disk_size"]], lab.execute)
    else:
        iso_url = vm["iso_url"]
        iso = lab.cache_dir / Path(iso_url).name
        download(iso_url, iso, vm.get("iso_sha256"), lab.execute)
        if not disk.exists() or not lab.execute:
            run(["qemu-img", "create", "-f", "qcow2", str(disk), vm["boot_disk_size"]], lab.execute)
    data_disks = []
    for index, size in enumerate(vm.get("data_disks", []), start=1):
        path = lab.image_dir / f"{lab.build_id}-{vm['name']}-data{index}.qcow2"
        data_disks.append(path)
        if not path.exists() or not lab.execute:
            run(["qemu-img", "create", "-f", "qcow2", str(path), size], lab.execute)
    mgmt_mac = mac_for(lab.build_id, 30)
    storage_mac = mac_for(lab.build_id, 130)
    if name in virsh("list", "--all", lab=lab):
        return
    cmd = [
        "virt-install", "--connect", "qemu:///system", "--name", name,
        "--memory", str(vm["memory_mib"]), "--vcpus", str(vm["vcpus"]),
        "--os-variant", vm.get("os_variant", "freebsd13.0"),
        "--disk", f"path={disk},format=qcow2,bus=sata",
    ]
    for data in data_disks:
        cmd += ["--disk", f"path={data},format=qcow2,bus=virtio"]
    if mode == "golden" and vm.get("golden_image"):
        cmd += ["--import"]
    else:
        cmd += ["--cdrom", str(lab.cache_dir / Path(vm["iso_url"]).name)]
    cmd += [
        "--network", f"network={lab.config['networks']['management']['name']},model=virtio,mac={mgmt_mac}",
        "--network", f"network={lab.config['networks']['storage']['name']},model=virtio,mac={storage_mac}",
        "--graphics", "vnc,listen=127.0.0.1", "--noautoconsole",
    ]
    run(cmd, lab.execute)
    if mode == "iso" and vm.get("installer_automation") != "external":
        marker = lab.run_dir / "TRUENAS_INSTALL_REQUIRED.txt"
        message = textwrap.dedent(f"""
        TrueNAS VM {name} was created from ISO.

        Complete installer automation for this TrueNAS release, then run:
          ./scripts/lab.py configure-truenas --config <config> --build-id {lab.build_id} --execute

        To make subsequent runs fully unattended, create a golden image from the installed boot disk
        and set vms.truenas.install_mode='golden' with vms.truenas.golden_image.
        """).strip() + "\n"
        if lab.execute:
            marker.write_text(message, encoding="utf-8")
        print(message)


def persistent_truenas_name(lab: Lab) -> str:
    return str(lab.config["vms"]["truenas"].get("persistent_name", "subvirt-lab-truenas"))


def persistent_truenas_boot_disk(lab: Lab) -> Path:
    vm = lab.config["vms"]["truenas"]
    return Path(vm.get("persistent_boot_disk", lab.image_dir / "truenas-lab-boot.qcow2"))


def persistent_truenas_data_disks(lab: Lab) -> list[Path]:
    vm = lab.config["vms"]["truenas"]
    configured = vm.get("persistent_data_disks")
    if configured:
        return [Path(item) for item in configured]
    return [lab.image_dir / f"truenas-lab-data{index}.qcow2" for index, _ in enumerate(vm.get("data_disks", []), start=1)]


def domain_state(lab: Lab, name: str) -> str | None:
    output = virsh("list", "--all", lab=lab)
    for line in output.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[1] == name:
            return " ".join(parts[2:])
        if parts and parts[0] == name:
            return " ".join(parts[1:])
    return None


def domain_names(lab: Lab) -> list[str]:
    return [line.strip() for line in virsh("list", "--all", "--name", lab=lab).splitlines() if line.strip()]


def destroy_domain(lab: Lab, name: str) -> None:
    run_shell(f"virsh -c qemu:///system destroy {q(name)} || true", lab.execute)
    run_shell(f"virsh -c qemu:///system undefine {q(name)} --nvram || true", lab.execute)


def cleanup_stale_ephemeral_domains(lab: Lab) -> None:
    if not lab.config["lab"].get("cleanup_stale_ephemeral_domains", True):
        return
    prefix = f"{lab.config['lab']['name_prefix']}-"
    current_prefix = f"{prefix}{lab.build_id}-"
    persistent_truenas = persistent_truenas_name(lab)
    for name in domain_names(lab):
        if not name.startswith(prefix) or name.startswith(current_prefix) or name == persistent_truenas:
            continue
        print(f"removing stale ephemeral lab domain {name}")
        destroy_domain(lab, name)


def cleanup_current_ephemeral_domains(lab: Lab) -> None:
    prefix = f"{lab.config['lab']['name_prefix']}-{lab.build_id}-"
    persistent_truenas = persistent_truenas_name(lab)
    for name in domain_names(lab):
        if name.startswith(prefix) and name != persistent_truenas:
            print(f"removing current ephemeral lab domain {name}")
            destroy_domain(lab, name)


def remove_run_dir(lab: Lab) -> None:
    if not lab.run_dir.exists() or not lab.execute:
        print(f"+ rm -rf {lab.run_dir}")
        return
    try:
        shutil.rmtree(lab.run_dir)
    except OSError as exc:
        print(f"failed to remove {lab.run_dir} directly: {exc}; retrying with sudo")
        run_shell(f"sudo rm -rf -- {q(lab.run_dir)}", True)
        if lab.run_dir.exists():
            raise SystemExit(f"failed to remove stale lab run directory {lab.run_dir}")


def prepare_run_dir(lab: Lab) -> None:
    cleanup_stale_ephemeral_domains(lab)
    cleanup_current_ephemeral_domains(lab)
    remove_run_dir(lab)
    if lab.execute:
        lab.run_dir.mkdir(parents=True, exist_ok=True)
        lab.web_root.mkdir(parents=True, exist_ok=True)
    else:
        print(f"+ mkdir -p {lab.run_dir}")
        print(f"+ mkdir -p {lab.web_root}")


def ensure_persistent_truenas(lab: Lab) -> None:
    ensure_dirs(lab)
    ensure_networks(lab)
    vm = lab.config["vms"]["truenas"]
    name = persistent_truenas_name(lab)
    state = domain_state(lab, name)
    if state:
        if "running" not in state:
            virsh("start", name, lab=lab)
        print(f"persistent TrueNAS VM {name} already exists")
        return

    iso_url = vm["iso_url"]
    iso = lab.cache_dir / Path(iso_url).name
    download(iso_url, iso, vm.get("iso_sha256"), lab.execute)

    boot_disk = persistent_truenas_boot_disk(lab)
    if not boot_disk.exists() or not lab.execute:
        run(["qemu-img", "create", "-f", "qcow2", str(boot_disk), vm["boot_disk_size"]], lab.execute)

    data_disks = persistent_truenas_data_disks(lab)
    for path, size in zip(data_disks, vm.get("data_disks", [])):
        if not path.exists() or not lab.execute:
            run(["qemu-img", "create", "-f", "qcow2", str(path), size], lab.execute)

    mgmt_mac = str(vm.get("persistent_management_mac", mac_for("persistent-truenas", 30)))
    storage_mac = str(vm.get("persistent_storage_mac", mac_for("persistent-truenas", 130)))
    cmd = [
        "virt-install", "--connect", "qemu:///system", "--name", name,
        "--memory", str(vm["memory_mib"]), "--vcpus", str(vm["vcpus"]),
        "--os-variant", vm.get("os_variant", "generic"),
        "--disk", f"path={boot_disk},format=qcow2,bus=sata",
    ]
    for data in data_disks:
        cmd += ["--disk", f"path={data},format=qcow2,bus=virtio"]
    cmd += [
        "--cdrom", str(iso),
        "--boot", "cdrom,hd",
        "--network", f"network={lab.config['networks']['management']['name']},model=virtio,mac={mgmt_mac}",
        "--network", f"network={lab.config['networks']['storage']['name']},model=virtio,mac={storage_mac}",
        "--graphics", "vnc,listen=127.0.0.1", "--noautoconsole",
    ]
    run(cmd, lab.execute)
    display = ""
    if lab.execute:
        display = virsh("domdisplay", name, lab=lab).strip()
    print(textwrap.dedent(f"""
    Persistent TrueNAS VM {name} has been created.

    Complete the TrueNAS installer once through the VM console, then configure:
      management IP: {lab.config['truenas']['management_ip']}
      storage IP:    {lab.config['truenas']['storage_ip']}
      test pools:    {lab.config['tests']['iscsi_truenas_pool']}, {lab.config['tests']['nvmeof_truenas_pool']}
      API user/key:  store the key only in the local lab config

    Console display: {display or 'run virsh -c qemu:///system domdisplay ' + name}
    """).strip())


def wait_for_tcp(host: str, port: int, label: str, execute: bool, attempts: int = 60) -> None:
    for attempt in range(1, attempts + 1):
        try:
            run_shell(f"timeout 5 bash -lc '</dev/tcp/{q(host)}/{int(port)}'", execute)
            return
        except subprocess.CalledProcessError:
            if attempt == attempts:
                raise SystemExit(f"timeout waiting for {label} at {host}:{port}")
            time.sleep(5)


def wait_truenas(lab: Lab) -> None:
    ensure_networks(lab)
    state = domain_state(lab, persistent_truenas_name(lab))
    if state and "running" not in state:
        virsh("start", persistent_truenas_name(lab), lab=lab)
    wait_for_tcp(str(lab.config["truenas"]["management_ip"]), 443, "TrueNAS API", lab.execute)


def truenas_api_config(lab: Lab) -> Path:
    t = lab.config["truenas"]
    api_key = t.get("api_key")
    if not api_key:
        raise SystemExit("truenas.api_key is required in the local lab config")
    if lab.execute:
        lab.run_dir.mkdir(parents=True, exist_ok=True)
    else:
        print(f"+ mkdir -p {lab.run_dir}")
    api_key_path = lab.run_dir / "truenas-api-key"
    config_path = lab.run_dir / "truenas-provider-config.json"
    if lab.execute:
        api_key_path.write_text(str(api_key).strip() + "\n", encoding="utf-8")
        os.chmod(api_key_path, 0o600)
        config_path.write_text(json.dumps({
            "truenas": {
                "url": f"wss://{t['management_ip']}/api/current",
                "username": t.get("username", "root"),
                "api_key_file": str(api_key_path),
                "tls_verify": False,
                "target_ip": t["storage_ip"],
            },
            "namespace": {"dataset": t.get("dataset", "libvirt")},
        }, indent=2) + "\n", encoding="utf-8")
    else:
        print(f"+ write {config_path}")
    return config_path


def doctor_truenas(lab: Lab) -> None:
    wait_truenas(lab)
    config_path = truenas_api_config(lab)
    output = run([sys.executable, str(ROOT / "truenas_provider.py"), "--config", str(config_path), "pool-list"], lab.execute)
    if not lab.execute:
        return
    pools = {item.get("name") for item in json.loads(output)}
    required = {lab.config["tests"]["iscsi_truenas_pool"], lab.config["tests"]["nvmeof_truenas_pool"]}
    missing = sorted(required - pools)
    if missing:
        raise SystemExit(f"TrueNAS is missing required test pools: {', '.join(missing)}")
    print(f"TrueNAS doctor OK: pools {', '.join(sorted(required))} are present")


def prepare_linux_lab(lab: Lab) -> None:
    ensure_dirs(lab)
    prepare_run_dir(lab)
    ensure_networks(lab)


def linux_vm_offset_map(lab: Lab) -> dict[str, int]:
    return {key: index * 10 for index, key in enumerate(linux_vm_candidate_keys(lab), start=1)}


def create_linux_vms(lab: Lab, vm_keys: Iterable[str]) -> None:
    ensure_dirs(lab)
    ensure_networks(lab)
    offsets = linux_vm_offset_map(lab)
    for key in vm_keys:
        create_cloud_vm(lab, key, offsets[key])


def linux_domain_name(lab: Lab, key: str) -> str:
    return f"{lab.config['lab']['name_prefix']}-{lab.build_id}-{lab.config['vms'][key]['name']}"


def destroy_linux_vms(lab: Lab, vm_keys: Iterable[str]) -> None:
    for key in vm_keys:
        destroy_domain(lab, linux_domain_name(lab, key))
        image_path(lab, key).unlink(missing_ok=True) if lab.execute else print(f"+ rm -f {image_path(lab, key)}")
        seed = seed_iso_path(lab, linux_domain_name(lab, key))
        seed.unlink(missing_ok=True) if lab.execute else print(f"+ rm -f {seed}")


def create_linux_lab(lab: Lab) -> None:
    prepare_linux_lab(lab)
    create_linux_vms(lab, linux_vm_keys(lab, ["ubuntu", "alma"]))
    write_run_release_config(lab)


def create_lab(lab: Lab) -> None:
    create_linux_lab(lab)
    create_truenas_vm(lab)
    write_run_release_config(lab)


def ensure_lab_gpg(lab: Lab) -> dict[str, str]:
    env = os.environ.copy()
    env["GNUPGHOME"] = str(lab.gpg_home)
    if lab.execute:
        lab.gpg_home.mkdir(parents=True, exist_ok=True)
        os.chmod(lab.gpg_home, 0o700)
    gpg_name = lab.config["repo"].get("gpg_name", "Subvirt Lab Repository <lab@subvirt.local>")
    if lab.execute:
        result = subprocess.run(["gpg", "--batch", "--list-secret-keys", gpg_name], text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
        existing = result.stdout
        if existing:
            print(existing, end="")
    else:
        existing = run(["gpg", "--batch", "--list-secret-keys", gpg_name], False, env=env)
    if lab.execute and gpg_name not in existing:
        batch = f"""Key-Type: RSA
Key-Length: 3072
Name-Real: Subvirt Lab Repository
Name-Email: lab@subvirt.local
Expire-Date: 0
%no-protection
%commit
"""
        run(["gpg", "--batch", "--generate-key"], True, env=env, input_text=batch)
    return env


def published_marker(lab: Lab) -> Path:
    return lab.run_dir / "published-distros.json"


def artifact_distros(path: Path) -> list[str]:
    distros = []
    ubuntu = path / "ubuntu"
    if ubuntu.exists() and any(ubuntu.rglob("*.deb")):
        distros.append("ubuntu")
    alma = path / "alma"
    if alma.exists() and any(alma.rglob("*.rpm")):
        distros.append("alma")
    return distros


def artifact_target_ids(path: Path) -> list[str]:
    targets: list[str] = []
    ubuntu = path / "ubuntu"
    if ubuntu.exists():
        for suite_dir in sorted(item for item in ubuntu.iterdir() if item.is_dir()):
            if any(suite_dir.glob("*.deb")):
                target = SUITE_TO_UBUNTU_ID.get(suite_dir.name)
                if target and target not in targets:
                    targets.append(target)
        if any(ubuntu.glob("*.deb")) and "ubuntu-24.04" not in targets:
            targets.append("ubuntu-24.04")
    alma = path / "alma"
    if alma.exists():
        for version_dir in sorted(item for item in alma.iterdir() if item.is_dir()):
            if any(version_dir.glob("*.rpm")):
                target = VERSION_TO_ALMA_ID.get(version_dir.name)
                if target and target not in targets:
                    targets.append(target)
        if any(alma.glob("*.rpm")) and "almalinux-10" not in targets:
            targets.append("almalinux-10")
    return targets


def published_distros(lab: Lab) -> list[str]:
    marker = published_marker(lab)
    if marker.exists():
        return json.loads(marker.read_text(encoding="utf-8"))["distros"]
    return ["ubuntu", "alma"]


def published_target_ids(lab: Lab) -> list[str]:
    marker = published_marker(lab)
    if marker.exists():
        return json.loads(marker.read_text(encoding="utf-8")).get("target_ids", [])
    return []


def vm_distro(lab: Lab, key: str) -> str:
    return str(lab.config["vms"][key].get("distro", key))


UBUNTU_ALIASES = {
    "ubuntu-18.04": {"ubuntu-18.04", "18.04", "bionic"},
    "ubuntu-20.04": {"ubuntu-20.04", "20.04", "focal"},
    "ubuntu-22.04": {"ubuntu-22.04", "22.04", "jammy"},
    "ubuntu-24.04": {"ubuntu-24.04", "24.04", "noble"},
    "ubuntu-26.04": {"ubuntu-26.04", "26.04", "resolute"},
}
ALMA_ALIASES = {
    "almalinux-9": {"almalinux-9", "9"},
    "almalinux-10": {"almalinux-10", "10"},
}
SUITE_TO_UBUNTU_ID = {alias: target for target, aliases in UBUNTU_ALIASES.items() for alias in aliases}
VERSION_TO_ALMA_ID = {alias: target for target, aliases in ALMA_ALIASES.items() for alias in aliases}


def vm_target_id(lab: Lab, key: str) -> str:
    vm = lab.config["vms"][key]
    if vm.get("target_id"):
        return str(vm["target_id"])
    distro = vm_distro(lab, key)
    if distro == "ubuntu":
        suite = str(vm.get("suite", lab.config["repo"].get("apt_suite", "noble")))
        return SUITE_TO_UBUNTU_ID.get(suite, "ubuntu-24.04")
    if distro == "alma":
        version = str(vm.get("version", lab.config["repo"].get("yum_distro_path", "almalinux/10").strip("/").split("/")[-1]))
        return VERSION_TO_ALMA_ID.get(version, "almalinux-10")
    return key


def vm_apt_suite(lab: Lab, key: str) -> str:
    vm = lab.config["vms"][key]
    if vm.get("suite"):
        return str(vm["suite"])
    target = vm_target_id(lab, key)
    for alias, target_id in SUITE_TO_UBUNTU_ID.items():
        if target_id == target and not alias.startswith(("ubuntu-", "1", "2")):
            return alias
    return lab.config["repo"].get("apt_suite", "noble")


def vm_yum_path(lab: Lab, key: str) -> str:
    vm = lab.config["vms"][key]
    if vm.get("yum_distro_path"):
        return str(vm["yum_distro_path"])
    target = vm_target_id(lab, key)
    version = target.rsplit("-", 1)[-1] if target.startswith("almalinux-") else "10"
    return f"almalinux/{version}"


def is_linux_vm(lab: Lab, key: str) -> bool:
    return vm_distro(lab, key) in {"ubuntu", "alma"}


def is_migration_peer(lab: Lab, key: str) -> bool:
    vm = lab.config["vms"][key]
    return bool(vm.get("migration_peer_for")) or key.endswith("_migration_peer")


def linux_vm_candidate_keys(lab: Lab) -> list[str]:
    return [key for key in lab.config["vms"] if key != "truenas" and is_linux_vm(lab, key)]


def primary_linux_vm_keys(lab: Lab, keys: Iterable[str]) -> list[str]:
    return [key for key in keys if not is_migration_peer(lab, key)]


def selected_tokens_for_family(family: str) -> set[str]:
    env_name = "SUBVIRT_UBUNTU_TARGETS" if family == "ubuntu" else "SUBVIRT_ALMA_TARGETS"
    value = os.environ.get(env_name, "").strip()
    if not value:
        return set()
    tokens = {item.strip() for item in value.split(",") if item.strip()}
    if "all" in tokens:
        aliases = UBUNTU_ALIASES if family == "ubuntu" else ALMA_ALIASES
        return set(aliases)
    if family == "ubuntu" and "standard" in tokens:
        tokens.remove("standard")
        tokens.update({"ubuntu-22.04", "ubuntu-24.04", "ubuntu-26.04"})
    if family == "ubuntu" and "esm" in tokens:
        tokens.remove("esm")
        tokens.update({"ubuntu-18.04", "ubuntu-20.04"})
    return tokens


def target_selected(lab: Lab, key: str, tokens: set[str]) -> bool:
    target = vm_target_id(lab, key)
    aliases = UBUNTU_ALIASES.get(target) or ALMA_ALIASES.get(target) or {target}
    return bool(tokens.intersection(aliases | {target}))


def baseline_linux_vm_keys(lab: Lab) -> list[str]:
    return [key for key in ("ubuntu", "alma") if key in lab.config["vms"] and is_linux_vm(lab, key)]


def selected_primary_linux_vm_keys(lab: Lab, package_distros: Iterable[str] | None = None) -> list[str]:
    available = set(package_distros or {"ubuntu", "alma"})
    candidates = [key for key in linux_vm_candidate_keys(lab) if not is_migration_peer(lab, key) and vm_distro(lab, key) in available]
    mode = os.environ.get("SUBVIRT_LAB_TARGETS", "selected").strip() or "selected"
    if mode == "all":
        return candidates
    if mode == "baseline":
        return [key for key in baseline_linux_vm_keys(lab) if key in candidates]
    tokens = selected_tokens_for_family("ubuntu") | selected_tokens_for_family("alma")
    selected = [key for key in candidates if target_selected(lab, key, tokens)]
    if selected:
        return selected
    return [key for key in baseline_linux_vm_keys(lab) if key in candidates]


def migration_peer_key_for(lab: Lab, primary_key: str) -> str | None:
    primary_target = vm_target_id(lab, primary_key)
    for key in linux_vm_candidate_keys(lab):
        if key == primary_key:
            continue
        vm = lab.config["vms"][key]
        peer_for = vm.get("migration_peer_for")
        if peer_for in {primary_key, primary_target} or (is_migration_peer(lab, key) and vm_target_id(lab, key) == primary_target):
            return key
    return None


def linux_vm_keys(lab: Lab, package_distros: Iterable[str]) -> list[str]:
    keys = selected_primary_linux_vm_keys(lab, package_distros)
    for key in list(keys):
        peer = migration_peer_key_for(lab, key)
        if peer and peer not in keys:
            keys.append(peer)
    return keys


def publish_repo(lab: Lab, artifacts: Path) -> None:
    ensure_dirs(lab)
    write_run_release_config(lab)
    distros = artifact_distros(artifacts)
    if not distros:
        raise SystemExit(f"no publishable package artifacts found in {artifacts}")
    incoming = lab.run_dir / "incoming"
    if lab.execute:
        if incoming.exists():
            shutil.rmtree(incoming)
        shutil.copytree(artifacts, incoming)
    else:
        print(f"+ copy {artifacts} -> {incoming}")
    env = ensure_lab_gpg(lab)
    run([
        str(ROOT / "scripts" / "publish-repo.py"),
        "--incoming", str(incoming),
        "--web-root", str(lab.web_root),
        "--suite", lab.config["repo"].get("apt_suite", "noble"),
        "--component", lab.config["repo"].get("component", "staging"),
        "--yum-distro-path", lab.config["repo"].get("yum_distro_path", "almalinux/10"),
        "--gpg-name", lab.config["repo"].get("gpg_name", "Subvirt Lab Repository <lab@subvirt.local>"),
    ], lab.execute, env=env)
    current = lab.workdir / "current-www"
    if lab.execute:
        current.unlink(missing_ok=True)
        current.symlink_to(lab.web_root)
        published_marker(lab).write_text(json.dumps({"distros": distros, "target_ids": artifact_target_ids(artifacts)}, indent=2) + "\n", encoding="utf-8")
    else:
        print(f"+ ln -sfn {lab.web_root} {current}")
        print(f"+ write {published_marker(lab)} with distros={distros} target_ids={artifact_target_ids(artifacts)}")


def ssh_known_host_name(host: str) -> str:
    return host.rsplit("@", 1)[-1]


def ssh(host: str, command: str, lab: Lab) -> str:
    argv = [*ssh_base_args(lab), host, command]
    try:
        return run(argv, lab.execute)
    except subprocess.CalledProcessError as exc:
        output = exc.output or ""
        if "REMOTE HOST IDENTIFICATION HAS CHANGED" not in output and "Host key verification failed" not in output:
            raise
        clear_known_host(lab, ssh_known_host_name(host))
        return run(argv, lab.execute)


def repo_url(lab: Lab) -> str:
    url = lab.config["lab"].get("http_url")
    if url:
        return url if str(url).startswith(("http://", "https://")) else f"http://{url}"
    listen = lab.config["lab"].get("http_listen", "192.168.150.1:8080")
    return f"http://{listen}"


def dns_wait_command(*urls: str) -> str:
    hosts: list[str] = []
    for url in urls:
        host = urlparse(url).hostname
        if host and any(ch.isalpha() for ch in host) and host not in hosts:
            hosts.append(host)
    if not hosts:
        return ""
    lines = ["for name in " + " ".join(q(host) for host in hosts) + "; do"]
    lines.extend([
        "  for attempt in $(seq 1 30); do",
        '    getent hosts "$name" >/dev/null && break',
        "    sleep 2",
        "  done",
        '  getent hosts "$name" >/dev/null',
        "done",
    ])
    return "\n".join(lines) + "\n"


def wait_for_ssh(lab: Lab, host: str, label: str, attempts: int = 60) -> None:
    clear_known_host(lab, host)
    target = f"root@{host}"
    for attempt in range(1, attempts + 1):
        try:
            run([*ssh_base_args(lab), "-o", "ConnectTimeout=5", target, "true"], lab.execute)
            return
        except subprocess.CalledProcessError:
            if attempt == attempts:
                raise SystemExit(f"SSH timeout waiting for {label} at {host}")
            time.sleep(5)


def wait_for_linux_vms(lab: Lab, vm_keys: Iterable[str]) -> None:
    for key in vm_keys:
        host = lab.config["vms"][key]["management_ip"]
        wait_for_ssh(lab, host, key)
        ssh(f"root@{host}", "cloud-init status --wait || true", lab)


def ensure_bionic_hwe_kernel(lab: Lab, key: str) -> None:
    host = lab.config["vms"][key]["management_ip"]
    command = f"""
set -euo pipefail
if uname -r | grep -q '^5\\.4\\.' && modprobe nvme-tcp; then
  exit 0
fi
{ubuntu_mirror_rewrite_command(lab)}
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold linux-generic-hwe-18.04 linux-modules-extra-virtual-hwe-18.04
nohup systemctl reboot >/dev/null 2>&1 &
"""
    try:
        ssh(f"root@{host}", command, lab)
    except subprocess.CalledProcessError:
        pass
    time.sleep(10)
    wait_for_ssh(lab, host, key, attempts=90)
    ssh(f"root@{host}", "uname -r && modprobe nvme-tcp", lab)


def configure_linux_repos(lab: Lab, vm_keys: Iterable[str] | None = None, mode: str = "full") -> None:
    vm_keys = list(vm_keys or linux_vm_keys(lab, published_distros(lab)))
    url = repo_url(lab)
    suite = lab.config["repo"].get("apt_suite", "noble")
    component = lab.config["repo"].get("component", "staging")
    yum_path = lab.config["repo"].get("yum_distro_path", "almalinux/10")
    stable_base_url = lab.config["repo"].get("stable_base_url", "https://repo.subvirt.net")
    if mode == "provider":
        dns_wait = dns_wait_command(stable_base_url, url)
        ubuntu_cmd = f"""
set -euo pipefail
install -d -m 0755 /usr/share/keyrings /etc/apt/sources.list.d /etc/apt/preferences.d
{dns_wait}\
curl -fsSL {q(stable_base_url + '/keys/subvirt.gpg')} -o /usr/share/keyrings/subvirt-stable.gpg
curl -fsSL {q(url + '/keys/subvirt.gpg')} -o /usr/share/keyrings/subvirt-staging.gpg
cat >/etc/apt/sources.list.d/subvirt-stable.sources <<'EOF'
Types: deb
URIs: {stable_base_url}/apt/ubuntu
Suites: {suite}
Components: stable
Architectures: amd64
Signed-By: /usr/share/keyrings/subvirt-stable.gpg
EOF
cat >/etc/apt/sources.list.d/subvirt-staging.sources <<'EOF'
Types: deb
URIs: {url}/apt/ubuntu
Suites: {suite}
Components: {component}
Architectures: amd64
Signed-By: /usr/share/keyrings/subvirt-staging.gpg
EOF
cat >/etc/apt/preferences.d/subvirt-provider-staging <<'EOF'
Package: truenas-libvirt-provider
Pin: release c=staging
Pin-Priority: 1001
EOF
{ubuntu_mirror_rewrite_command(lab)}
apt-get clean
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get --fix-broken install -y -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold
{ubuntu_refresh_command()}
DEBIAN_FRONTEND=noninteractive apt-get install -y --allow-downgrades -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold truenas-libvirt-provider libvirt-daemon-system libvirt-daemon-driver-qemu libvirt-daemon-driver-storage-truenas virt-manager virtinst open-iscsi nvme-cli qemu-utils
if ! modprobe nvme-tcp 2>/dev/null; then
  DEBIAN_FRONTEND=noninteractive apt-get install -y "linux-modules-extra-$(uname -r)"
  modprobe nvme-tcp
fi

systemctl daemon-reload
for unit in virtqemud.socket virtstoraged.socket virtproxyd.socket virtlogd.socket virtlockd.socket; do
  systemctl list-unit-files "$unit" --no-legend | grep -q . && systemctl enable --now "$unit" || true
done
for unit in virtstoraged.service libvirtd.service; do
  systemctl list-unit-files "$unit" --no-legend | grep -q . && {{ systemctl restart "$unit"; break; }}
done
systemctl list-unit-files virtqemud.service --no-legend | grep -q . && systemctl restart virtqemud.service || true
"""
        alma_cmd = f"""
set -euo pipefail
{dns_wait}\
curl -fsSL {q(stable_base_url + '/keys/subvirt.asc')} -o /etc/pki/rpm-gpg/RPM-GPG-KEY-subvirt-stable
curl -fsSL {q(url + '/keys/subvirt.asc')} -o /etc/pki/rpm-gpg/RPM-GPG-KEY-subvirt-staging
cat >/etc/yum.repos.d/subvirt-stable.repo <<'EOF'
[subvirt-stable]
name=Subvirt stable packages
baseurl={stable_base_url}/yum/{yum_path}/stable
enabled=1
gpgcheck=1
gpgkey=file:///etc/pki/rpm-gpg/RPM-GPG-KEY-subvirt-stable
EOF
cat >/etc/yum.repos.d/subvirt-lab.repo <<'EOF'
[subvirt-lab]
name=Subvirt Lab
baseurl={url}/yum/{yum_path}/{component}
enabled=1
gpgcheck=1
gpgkey=file:///etc/pki/rpm-gpg/RPM-GPG-KEY-subvirt-staging
EOF
dnf upgrade -y
dnf install -y --disablerepo=subvirt-lab libvirt-daemon-kvm libvirt-daemon-driver-storage-truenas virt-manager virt-manager-common virt-install iscsi-initiator-utils nvme-cli qemu-img kmod
dnf install -y --disablerepo=subvirt-stable truenas-libvirt-provider
modprobe nvme-tcp

systemctl daemon-reload
for unit in virtqemud.socket virtstoraged.socket virtproxyd.socket virtlogd.socket virtlockd.socket; do
  systemctl list-unit-files "$unit" --no-legend | grep -q . && systemctl enable --now "$unit" || true
done
for unit in virtstoraged.service libvirtd.service; do
  systemctl list-unit-files "$unit" --no-legend | grep -q . && {{ systemctl restart "$unit"; break; }}
done
systemctl list-unit-files virtqemud.service --no-legend | grep -q . && systemctl restart virtqemud.service || true
"""
    else:
        dns_wait = dns_wait_command(url)
        ubuntu_cmd = f"""
set -euo pipefail
install -d -m 0755 /usr/share/keyrings /etc/apt/sources.list.d
{dns_wait}\
curl -fsSL {q(url + '/keys/subvirt.gpg')} -o /usr/share/keyrings/subvirt.gpg
cat >/etc/apt/sources.list.d/subvirt.sources <<'EOF'
Types: deb
URIs: {url}/apt/ubuntu
Suites: {suite}
Components: {component}
Architectures: amd64
Signed-By: /usr/share/keyrings/subvirt.gpg
EOF
{ubuntu_mirror_rewrite_command(lab)}
apt-get clean
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get --fix-broken install -y -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold
{ubuntu_refresh_command()}
DEBIAN_FRONTEND=noninteractive apt-get install -y --allow-downgrades truenas-libvirt-provider libvirt-daemon-system libvirt-daemon-driver-qemu libvirt-daemon-driver-storage-truenas virt-manager virtinst open-iscsi nvme-cli qemu-utils
if ! modprobe nvme-tcp 2>/dev/null; then
  DEBIAN_FRONTEND=noninteractive apt-get install -y "linux-modules-extra-$(uname -r)"
  modprobe nvme-tcp
fi

systemctl daemon-reload
for unit in virtqemud.socket virtstoraged.socket virtproxyd.socket virtlogd.socket virtlockd.socket; do
  systemctl list-unit-files "$unit" --no-legend | grep -q . && systemctl enable --now "$unit" || true
done
for unit in virtstoraged.service libvirtd.service; do
  systemctl list-unit-files "$unit" --no-legend | grep -q . && {{ systemctl restart "$unit"; break; }}
done
systemctl list-unit-files virtqemud.service --no-legend | grep -q . && systemctl restart virtqemud.service || true
"""
        alma_cmd = f"""
set -euo pipefail
{dns_wait}\
curl -fsSL {q(url + '/keys/subvirt.asc')} -o /etc/pki/rpm-gpg/RPM-GPG-KEY-subvirt
cat >/etc/yum.repos.d/subvirt-lab.repo <<'EOF'
[subvirt-lab]
name=Subvirt Lab
baseurl={url}/yum/{yum_path}/{component}
enabled=1
gpgcheck=1
gpgkey=file:///etc/pki/rpm-gpg/RPM-GPG-KEY-subvirt
EOF
dnf upgrade -y
dnf install -y truenas-libvirt-provider libvirt-daemon-kvm libvirt-daemon-driver-storage-truenas virt-manager virt-manager-common virt-install iscsi-initiator-utils nvme-cli qemu-img kmod
modprobe nvme-tcp

systemctl daemon-reload
for unit in virtqemud.socket virtstoraged.socket virtproxyd.socket virtlogd.socket virtlockd.socket; do
  systemctl list-unit-files "$unit" --no-legend | grep -q . && systemctl enable --now "$unit" || true
done
for unit in virtstoraged.service libvirtd.service; do
  systemctl list-unit-files "$unit" --no-legend | grep -q . && {{ systemctl restart "$unit"; break; }}
done
systemctl list-unit-files virtqemud.service --no-legend | grep -q . && systemctl restart virtqemud.service || true
"""
    for key in vm_keys:
        host = lab.config["vms"][key]["management_ip"]
        distro = vm_distro(lab, key)
        if distro == "ubuntu":
            vm_suite = vm_apt_suite(lab, key)
            command = ubuntu_cmd.replace(f"Suites: {suite}", f"Suites: {vm_suite}")
            command = command.replace(f"dists/{suite}", f"dists/{vm_suite}")
            if vm_suite == "bionic":
                ensure_bionic_hwe_kernel(lab, key)
                command = command.replace(" libvirt-daemon-driver-qemu", "")
                command = command.replace(" virt-manager virtinst", "")
                command = command.replace('if ! modprobe nvme-tcp 2>/dev/null; then\n  DEBIAN_FRONTEND=noninteractive apt-get install -y "linux-modules-extra-$(uname -r)"\n  modprobe nvme-tcp\nfi', "modprobe nvme-tcp")
            ssh(f"root@{host}", command, lab)
        elif distro == "alma":
            vm_yum = vm_yum_path(lab, key)
            command = alma_cmd.replace(f"/yum/{yum_path}/", f"/yum/{vm_yum}/")
            ssh(f"root@{host}", command, lab)
        else:
            raise SystemExit(f"unsupported Linux VM distro {distro!r} for {key}")
        wait_for_ssh(lab, host, key)
        verify_provider_package(lab, key)


def verify_provider_package(lab: Lab, vm_key: str) -> None:
    host = lab.config["vms"][vm_key]["management_ip"]
    distro = vm_distro(lab, vm_key)
    if distro == "ubuntu":
        command = """
set -euo pipefail
dpkg-query -W -f='${db:Status-Abbrev} ${Package} ${Version}\n' truenas-libvirt-provider libvirt-daemon-driver-storage-truenas
dpkg-query -W -f='${db:Status-Abbrev}' truenas-libvirt-provider | grep -q '^ii '
if ! test -e /lib/systemd/system/truenas-libvirt-provider.service && ! test -e /usr/lib/systemd/system/truenas-libvirt-provider.service; then
  echo "truenas-libvirt-provider.service is missing after package install" >&2
  exit 1
fi
"""
    elif distro == "alma":
        command = """
set -euo pipefail
rpm -q truenas-libvirt-provider libvirt-daemon-driver-storage-truenas
test -e /usr/lib/systemd/system/truenas-libvirt-provider.service
"""
    else:
        raise SystemExit(f"unsupported Linux VM distro {distro!r} for {vm_key}")
    ssh(f"root@{host}", command, lab)


def configure_provider_configs(lab: Lab, vm_keys: Iterable[str] | None = None) -> None:
    api_key = lab.config.get("truenas", {}).get("api_key")
    if not api_key:
        raise SystemExit("truenas.api_key is required after TrueNAS install/configuration; run configure-truenas or set it in local lab config")
    vm_keys = list(vm_keys or linux_vm_keys(lab, published_distros(lab)))
    t = lab.config["truenas"]
    config = {
        "truenas": {
            "url": f"wss://{t['management_ip']}/api/current",
            "username": t.get("username", "root"),
            "api_key_file": "/etc/truenas-libvirt/api-key",
            "tls_verify": False,
            "target_ip": t["storage_ip"],
        },
        "namespace": {"dataset": t.get("dataset", "libvirt")},
    }
    payload = json.dumps(config, indent=2)
    for key in vm_keys:
        host = lab.config["vms"][key]["management_ip"]
        command = f"""
set -euo pipefail
install -d -m 0750 /etc/truenas-libvirt
cat >/etc/truenas-libvirt/api-key <<'EOF'
{api_key}
EOF
chmod 0600 /etc/truenas-libvirt/api-key
cat >/etc/truenas-libvirt/config.json <<'EOF'
{payload}
EOF
chmod 0640 /etc/truenas-libvirt/config.json
for attempt in $(seq 1 30); do
  systemctl daemon-reload
  if systemctl list-unit-files truenas-libvirt-provider.service --no-legend | grep -q '^truenas-libvirt-provider.service'; then
    break
  fi
  if [ "$attempt" = 30 ]; then
    echo "truenas-libvirt-provider.service unit file did not appear" >&2
    exit 1
  fi
  sleep 2
done
systemctl enable --now truenas-libvirt-provider.service
systemctl restart truenas-libvirt-provider.service
"""
        ssh(f"root@{host}", command, lab)


def test_repo(lab: Lab, mode: str) -> None:
    distros = published_distros(lab)
    vm_keys = linux_vm_keys(lab, distros)
    wait_for_linux_vms(lab, vm_keys)
    configure_linux_repos(lab, vm_keys, mode)
    api_key = lab.config.get("truenas", {}).get("api_key")
    if not api_key:
        raise SystemExit("truenas.api_key is required for lab candidate validation; set it in the local lab config")
    doctor_truenas(lab)
    configure_provider_configs(lab, vm_keys)
    release_config = load_config(lab.run_dir / "release.json") if lab.execute else {"tests": {"storage_targets": []}}
    if lab.execute and release_config.get("tests", {}).get("storage_targets"):
        run([str(ROOT / "scripts" / "release.py"), "test-staging", "--config", str(lab.run_dir / "release.json"), "--build-id", lab.build_id, "--execute"], lab.execute)
    elif not lab.execute:
        run([str(ROOT / "scripts" / "release.py"), "test-staging", "--config", str(lab.run_dir / "release.json"), "--build-id", lab.build_id, "--execute"], lab.execute)
    else:
        print(f"partial lab repo test completed for distros={distros}; no storage target pairs were configured")


def test_repo_sequential(lab: Lab, mode: str) -> None:
    distros = published_distros(lab)
    primary_keys = selected_primary_linux_vm_keys(lab, distros)
    if not primary_keys:
        raise SystemExit(f"no Linux lab targets selected for distros={distros}")
    api_key = lab.config.get("truenas", {}).get("api_key")
    if not api_key:
        raise SystemExit("truenas.api_key is required for lab candidate validation; set it in the local lab config")
    doctor_truenas(lab)
    for primary in primary_keys:
        vm_keys = [primary]
        peer = migration_peer_key_for(lab, primary)
        if peer and peer not in vm_keys:
            vm_keys.append(peer)
        print(f"Sequential lab target {vm_target_id(lab, primary)} start: {', '.join(vm_keys)}")
        create_linux_vms(lab, vm_keys)
        write_run_release_config(lab, [primary])
        wait_for_linux_vms(lab, vm_keys)
        configure_linux_repos(lab, vm_keys, mode)
        configure_provider_configs(lab, vm_keys)
        release_config = load_config(lab.run_dir / "release.json") if lab.execute else {"tests": {"storage_targets": []}}
        if lab.execute and release_config.get("tests", {}).get("storage_targets"):
            run([str(ROOT / "scripts" / "release.py"), "test-staging", "--config", str(lab.run_dir / "release.json"), "--build-id", lab.build_id, "--execute"], lab.execute)
        elif not lab.execute:
            run([str(ROOT / "scripts" / "release.py"), "test-staging", "--config", str(lab.run_dir / "release.json"), "--build-id", lab.build_id, "--execute"], lab.execute)
        else:
            print(f"partial lab repo test completed for target={vm_target_id(lab, primary)}; no storage target pair was configured")
        destroy_linux_vms(lab, vm_keys)
        print(f"Sequential lab target {vm_target_id(lab, primary)} passed")


def configure_truenas(lab: Lab) -> None:
    script = lab.config.get("truenas", {}).get("post_install_script")
    if not script:
        raise SystemExit("set truenas.post_install_script in local lab config; it must configure pools/services and print or store an API key")
    run([script, "--config", str(lab.run_dir / "lab.json"), "--build-id", lab.build_id], lab.execute)


def write_run_release_config(lab: Lab, selected_keys: Iterable[str] | None = None) -> None:
    tests = lab.config["tests"]
    release = load_config(Path(lab.config["lab"].get("release_template", ROOT / "release" / "release.example.json")))
    release["hosts"]["build"] = socket.gethostname()
    release.setdefault("ssh", {})["identity_files"] = lab.config["lab"].get("ssh_identity_files", [])
    release["ssh"]["known_hosts_file"] = str(lab.run_dir / "known_hosts")
    release["project"]["source_mode"] = "rsync"
    release["project"]["source_path"] = str(ROOT)
    release["project"].setdefault("rsync_excludes", [".git", "build", "dist", "provider-build", ".venv-vnc"])
    selected_keys = list(selected_keys or selected_primary_linux_vm_keys(lab, published_distros(lab)))
    ubuntu_keys = [key for key in selected_keys if vm_distro(lab, key) == "ubuntu"]
    alma_keys = [key for key in selected_keys if vm_distro(lab, key) == "alma"]
    if ubuntu_keys:
        release["hosts"]["ubuntu_test"] = f"root@{lab.config['vms'][ubuntu_keys[0]]['management_ip']}"
    if alma_keys:
        release["hosts"]["alma_test"] = f"root@{lab.config['vms'][alma_keys[0]]['management_ip']}"
    if ubuntu_keys:
        release["hosts"]["migration_source"] = release["hosts"]["ubuntu_test"]
    first_peer = migration_peer_key_for(lab, ubuntu_keys[0]) if ubuntu_keys else None
    if first_peer:
        release["hosts"]["migration_target"] = f"root@{lab.config['vms'][first_peer]['management_ip']}"
    elif alma_keys:
        release["hosts"]["migration_target"] = f"root@{lab.config['vms'][alma_keys[0]]['management_ip']}"

    storage_targets = []
    for key in selected_keys:
        peer = migration_peer_key_for(lab, key)
        if peer is None:
            alternatives = [candidate for candidate in selected_keys if candidate != key]
            peer = alternatives[0] if alternatives else None
        if peer is None:
            continue
        storage_targets.append({
            "id": vm_target_id(lab, key),
            "name": key,
            "host": f"root@{lab.config['vms'][key]['management_ip']}",
            "peer": f"root@{lab.config['vms'][peer]['management_ip']}",
            "migration": bool(tests.get("run_migration", False)) and vm_target_id(lab, peer) == vm_target_id(lab, key),
        })
    release["tests"]["storage_targets"] = storage_targets
    release["tests"]["iscsi_pool"] = tests["iscsi_pool_name"]
    release["tests"]["nvmeof_pool"] = tests["nvmeof_pool_name"]
    release["tests"]["iscsi_pool_xml"] = str(lab.run_dir / "iscsi-pool.xml")
    release["tests"]["nvmeof_pool_xml"] = str(lab.run_dir / "nvmeof-pool.xml")
    release["tests"]["run_migration"] = bool(tests.get("run_migration", False))
    for key in ("migration_domain", "migration_image_url", "migration_image_sha256", "migration_volume_size", "migration_machine"):
        if key in tests:
            release["tests"][key] = tests[key]
    if "min_pool_capacity_gib" in tests:
        release["tests"]["min_pool_capacity_gib"] = int(tests["min_pool_capacity_gib"])
    iscsi_pool = tests["iscsi_truenas_pool"]
    nvme_pool = tests["nvmeof_truenas_pool"]
    target_path = tests.get("target_path", "/dev/disk/by-id")
    iscsi_xml = f"""<pool type='truenas'>
  <name>{tests['iscsi_pool_name']}</name>
  <source>
    <name>{iscsi_pool}</name>
    <protocol type='iscsi'/>
  </source>
  <target><path>{target_path}</path></target>
</pool>
"""
    nvme_xml = f"""<pool type='truenas'>
  <name>{tests['nvmeof_pool_name']}</name>
  <source>
    <name>{nvme_pool}</name>
    <protocol type='nvmeof'/>
  </source>
  <target><path>{target_path}</path></target>
</pool>
"""
    if lab.execute:
        (lab.run_dir / "release.json").write_text(json.dumps(release, indent=2) + "\n", encoding="utf-8")
        (lab.run_dir / "lab.json").write_text(json.dumps(lab.config, indent=2) + "\n", encoding="utf-8")
        (lab.run_dir / "iscsi-pool.xml").write_text(iscsi_xml, encoding="utf-8")
        (lab.run_dir / "nvmeof-pool.xml").write_text(nvme_xml, encoding="utf-8")
    else:
        print(f"+ write {lab.run_dir / 'release.json'}")


def destroy_lab(lab: Lab) -> None:
    prefix = f"{lab.config['lab']['name_prefix']}-{lab.build_id}-"
    for key in [*linux_vm_candidate_keys(lab), "truenas"]:
        if key not in lab.config["vms"]:
            continue
        name = prefix + lab.config["vms"][key]["name"]
        existing = virsh("list", "--all", lab=lab)
        if name in existing:
            destroy_domain(lab, name)
    for path in sorted(lab.image_dir.glob(f"{lab.build_id}-*.qcow2")):
        if lab.execute:
            path.unlink(missing_ok=True)
        else:
            print(f"+ rm -f {path}")
    keep_networks = domain_state(lab, persistent_truenas_name(lab)) is not None
    if keep_networks:
        print(f"persistent TrueNAS VM {persistent_truenas_name(lab)} exists; keeping lab networks")
    else:
        for key in ("management", "storage"):
            name = lab.config["networks"][key]["name"]
            run_shell(f"virsh -c qemu:///system net-destroy {q(name)} || true", lab.execute)
            run_shell(f"virsh -c qemu:///system net-undefine {q(name)} || true", lab.execute)
    if lab.execute and lab.run_dir.exists():
        shutil.rmtree(lab.run_dir)
    else:
        print(f"+ rm -rf {lab.run_dir}")


def parse_args(argv: Iterable[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["bootstrap-host", "prepare-linux", "create-linux", "create", "ensure-truenas", "wait-linux", "wait-truenas", "doctor-truenas", "publish-repo", "test-repo", "test-repo-sequential", "configure-truenas", "destroy"])
    parser.add_argument("--config", default="release/lab.example.json", type=Path)
    parser.add_argument("--build-id", default="manual")
    parser.add_argument("--artifacts", type=Path, help="artifact directory containing ubuntu/ and alma/ subdirectories")
    parser.add_argument("--mode", choices=["full", "provider"], default="full", help="lab repo test mode")
    parser.add_argument("--execute", action="store_true")
    return parser.parse_args(list(argv))


def main(argv: Iterable[str] = sys.argv[1:]) -> int:
    args = parse_args(argv)
    lab = Lab(load_config(args.config), args.execute, args.build_id)
    if args.command == "bootstrap-host":
        bootstrap_host(lab)
    elif args.command == "prepare-linux":
        prepare_linux_lab(lab)
    elif args.command == "create-linux":
        create_linux_lab(lab)
    elif args.command == "create":
        create_lab(lab)
    elif args.command == "ensure-truenas":
        ensure_persistent_truenas(lab)
    elif args.command == "wait-linux":
        wait_for_linux_vms(lab, linux_vm_keys(lab, ["ubuntu", "alma"]))
    elif args.command == "wait-truenas":
        wait_truenas(lab)
    elif args.command == "doctor-truenas":
        doctor_truenas(lab)
    elif args.command == "publish-repo":
        artifacts = args.artifacts or Path(lab.config["lab"].get("artifact_root", "/srv/subvirt/artifacts")) / args.build_id
        publish_repo(lab, artifacts)
    elif args.command == "test-repo":
        test_repo(lab, args.mode)
    elif args.command == "test-repo-sequential":
        test_repo_sequential(lab, args.mode)
    elif args.command == "configure-truenas":
        configure_truenas(lab)
    elif args.command == "destroy":
        destroy_lab(lab)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
