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
package_update: {package_update}
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
      macaddress: {mgmt_mac}
    set-name: mgmt0
    addresses:
      - {mgmt_ip}/{mgmt['prefix']}
    gateway4: {mgmt['gateway']}
    nameservers:
      addresses:
{chr(10).join('        - ' + item for item in dns)}
  storage0:
    match:
      macaddress: {storage_mac}
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
    storage_mac = mac_for(lab.build_id, offset + 100)
    seed = write_cloud_init(lab, name, vm_distro(lab, vm_key), mgmt_mac, storage_mac, vm["management_ip"], vm["storage_ip"])
    run([
        "virt-install", "--connect", "qemu:///system", "--name", name,
        "--memory", str(vm["memory_mib"]), "--vcpus", str(vm["vcpus"]),
        "--import", "--os-variant", vm["os_variant"],
        "--disk", f"path={disk},format=qcow2,bus=virtio",
        "--disk", f"path={seed},device=cdrom",
        "--network", f"network={lab.config['networks']['management']['name']},model=virtio,mac={mgmt_mac}",
        "--network", f"network={lab.config['networks']['storage']['name']},model=virtio,mac={storage_mac}",
        "--graphics", "none", "--noautoconsole",
    ], lab.execute)


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


def create_linux_lab(lab: Lab) -> None:
    ensure_dirs(lab)
    cleanup_stale_ephemeral_domains(lab)
    ensure_networks(lab)
    create_cloud_vm(lab, "ubuntu", 10)
    create_cloud_vm(lab, "alma", 20)
    if lab.config["tests"].get("run_migration", False) and "ubuntu_migration_peer" in lab.config["vms"]:
        create_cloud_vm(lab, "ubuntu_migration_peer", 40)
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


def published_distros(lab: Lab) -> list[str]:
    marker = published_marker(lab)
    if marker.exists():
        return json.loads(marker.read_text(encoding="utf-8"))["distros"]
    return ["ubuntu", "alma"]


def vm_distro(lab: Lab, key: str) -> str:
    return str(lab.config["vms"][key].get("distro", key))


def linux_vm_keys(lab: Lab, package_distros: Iterable[str]) -> list[str]:
    available = set(package_distros)
    keys = [key for key in ("ubuntu", "alma") if key in lab.config["vms"] and vm_distro(lab, key) in available]
    if lab.config["tests"].get("run_migration", False) and "ubuntu_migration_peer" in lab.config["vms"] and "ubuntu" in available:
        keys.append("ubuntu_migration_peer")
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
        published_marker(lab).write_text(json.dumps({"distros": distros}, indent=2) + "\n", encoding="utf-8")
    else:
        print(f"+ ln -sfn {lab.web_root} {current}")
        print(f"+ write {published_marker(lab)} with distros={distros}")


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


def configure_linux_repos(lab: Lab, vm_keys: Iterable[str] | None = None, mode: str = "full") -> None:
    vm_keys = list(vm_keys or linux_vm_keys(lab, published_distros(lab)))
    url = repo_url(lab)
    suite = lab.config["repo"].get("apt_suite", "noble")
    component = lab.config["repo"].get("component", "staging")
    yum_path = lab.config["repo"].get("yum_distro_path", "almalinux/10")
    stable_base_url = lab.config["repo"].get("stable_base_url", "https://repo.subvirt.net")
    if mode == "provider":
        ubuntu_cmd = f"""
set -euo pipefail
install -d -m 0755 /usr/share/keyrings /etc/apt/sources.list.d /etc/apt/preferences.d
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
apt-get clean
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get --fix-broken install -y -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold
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
        ubuntu_cmd = f"""
set -euo pipefail
install -d -m 0755 /usr/share/keyrings /etc/apt/sources.list.d
curl -fsSL {q(url + '/keys/subvirt.gpg')} -o /usr/share/keyrings/subvirt.gpg
cat >/etc/apt/sources.list.d/subvirt.sources <<'EOF'
Types: deb
URIs: {url}/apt/ubuntu
Suites: {suite}
Components: {component}
Architectures: amd64
Signed-By: /usr/share/keyrings/subvirt.gpg
EOF
apt-get clean
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get --fix-broken install -y -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold
DEBIAN_FRONTEND=noninteractive apt-get full-upgrade -y --allow-downgrades -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold
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
            ssh(f"root@{host}", ubuntu_cmd, lab)
        elif distro == "alma":
            ssh(f"root@{host}", alma_cmd, lab)
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
    if {"ubuntu", "alma"}.issubset(set(distros)):
        run([str(ROOT / "scripts" / "release.py"), "test-staging", "--config", str(lab.run_dir / "release.json"), "--build-id", lab.build_id, "--execute"], lab.execute)
    else:
        print(f"partial lab repo test completed for distros={distros}; storage gate requires ubuntu and alma artifacts")


def configure_truenas(lab: Lab) -> None:
    script = lab.config.get("truenas", {}).get("post_install_script")
    if not script:
        raise SystemExit("set truenas.post_install_script in local lab config; it must configure pools/services and print or store an API key")
    run([script, "--config", str(lab.run_dir / "lab.json"), "--build-id", lab.build_id], lab.execute)


def write_run_release_config(lab: Lab) -> None:
    tests = lab.config["tests"]
    release = load_config(Path(lab.config["lab"].get("release_template", ROOT / "release" / "release.example.json")))
    release["hosts"]["build"] = socket.gethostname()
    release.setdefault("ssh", {})["identity_files"] = lab.config["lab"].get("ssh_identity_files", [])
    release["ssh"]["known_hosts_file"] = str(lab.run_dir / "known_hosts")
    release["project"]["source_mode"] = "rsync"
    release["project"]["source_path"] = str(ROOT)
    release["project"].setdefault("rsync_excludes", [".git", "build", "dist", "provider-build", ".venv-vnc"])
    release["hosts"]["ubuntu_test"] = f"root@{lab.config['vms']['ubuntu']['management_ip']}"
    release["hosts"]["alma_test"] = f"root@{lab.config['vms']['alma']['management_ip']}"
    release["hosts"]["migration_source"] = release["hosts"]["ubuntu_test"]
    if "ubuntu_migration_peer" in lab.config["vms"]:
        release["hosts"]["migration_target"] = f"root@{lab.config['vms']['ubuntu_migration_peer']['management_ip']}"
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
    for key in ("ubuntu", "alma", "ubuntu_migration_peer", "truenas"):
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
    parser.add_argument("command", choices=["bootstrap-host", "create-linux", "create", "ensure-truenas", "wait-linux", "wait-truenas", "doctor-truenas", "publish-repo", "test-repo", "configure-truenas", "destroy"])
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
    elif args.command == "configure-truenas":
        configure_truenas(lab)
    elif args.command == "destroy":
        destroy_lab(lab)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
