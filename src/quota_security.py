from __future__ import annotations

import hashlib
import json
from pathlib import Path

import docker

WORKSPACE_RUNTIME = "/usr/local/libexec/workspace-runtime"
WORKSPACE_CAPABILITIES = frozenset(
    "AUDIT_WRITE CHOWN DAC_OVERRIDE FOWNER KILL NET_BIND_SERVICE NET_RAW SETGID SETUID SYS_CHROOT".split()
)

QUOTA_SECCOMP_PATH = Path(__file__).with_name("seccomp") / "workspace-quota.json"
QUOTA_SECCOMP_SHA256 = "63405c81b33f692155cd10d177e48239d891ed2ffb4f9f065f984dde768e3616"


def workspace_quota_seccomp() -> str:
    try:
        content = QUOTA_SECCOMP_PATH.read_bytes()
    except OSError as exc:
        raise ValueError("workspace quota seccomp profile is unavailable") from exc
    if hashlib.sha256(content).hexdigest() != QUOTA_SECCOMP_SHA256:
        raise ValueError("workspace quota seccomp profile does not match the pinned checksum")
    profile = json.loads(content)
    profile["syscalls"].append({"names": ["pivot_root"], "action": "SCMP_ACT_ALLOW"})
    return "seccomp=" + json.dumps(profile, separators=(",", ":"))


def workspace_quota_options(generation: str, capabilities: list[str], *, apparmor: bool) -> dict:
    if (
        not generation
        or len(generation) > 127
        or any(not (char.isascii() and (char.isalnum() or char in "-_")) for char in generation)
    ):
        raise ValueError("workspace isolation requires a valid session UUID")
    if not set(capabilities) <= WORKSPACE_CAPABILITIES:
        raise ValueError("workspace capabilities exceed the supported guest policy")
    security = [workspace_quota_seccomp()]
    if apparmor:
        security.append("apparmor=unconfined")
    return {
        "read_only": True,
        "cap_add": sorted(set(capabilities) | {"SYS_ADMIN", "MKNOD", "SETPCAP"}),
        "security_opt": security,
        "devices": ["/dev/loop-control:/dev/loop-control:rwm"],
        "device_cgroup_rules": ["b 7:* rwm"],
        "mounts": [
            docker.types.Mount("/state", None, "tmpfs", tmpfs_size=65536, tmpfs_mode=0o700),
            docker.types.Mount("/quota-runtime", None, "tmpfs", tmpfs_size=1048576, tmpfs_mode=0o700),
            docker.types.Mount("/quota-store", None, "volume", no_copy=True),
            docker.types.Mount(
                "/var/lib/remote-desktop",
                None,
                "volume",
                no_copy=True,
                driver_config=docker.types.DriverConfig(
                    "local", {"type": "tmpfs", "device": "tmpfs", "o": "size=64m,nosuid,nodev,noexec"}
                ),
            ),
        ],
        "entrypoint": [WORKSPACE_RUNTIME, "init", generation, "/startup.sh"],
        "healthcheck": {
            "test": ["CMD", WORKSPACE_RUNTIME, "enter", generation, "/usr/local/bin/remote-desktop-healthcheck"],
            "interval": 30000000000,
            "timeout": 15000000000,
            "start_period": 180000000000,
            "start_interval": 5000000000,
            "retries": 3,
        },
    }


def run_quota_container(
    client,
    image,
    *,
    name,
    hostname,
    environment,
    ports,
    network,
    labels,
    detach,
    entrypoint,
    healthcheck,
    **host_config_options,
):
    host_config = client.api.create_host_config(port_bindings=ports, network_mode=network, **host_config_options)
    host_config["ReadonlyPaths"] = []
    host_config["MaskedPaths"] = []
    exposed_ports = [tuple(port.split("/", 1)) for port in sorted(host_config.get("PortBindings") or {})]
    config = client.api.create_container_config(
        image=image,
        hostname=hostname,
        environment=environment,
        entrypoint=entrypoint,
        command=[],
        healthcheck=healthcheck,
        ports=exposed_ports,
        labels=labels,
        detach=detach,
        host_config=host_config,
    )
    config["Healthcheck"]["StartInterval"] = healthcheck["start_interval"]
    created = client.api.create_container_from_config(config, name=name)
    container = client.containers.get(created["Id"])
    try:
        container.start()
    except Exception:
        try:
            container.remove(force=True, v=True)
        except docker.errors.NotFound:
            pass
        raise
    return container
