from __future__ import annotations

import hashlib
from pathlib import Path

QUOTA_SECCOMP_PATH = Path(__file__).with_name("seccomp") / "workspace-quota.json"
QUOTA_SECCOMP_SHA256 = "63405c81b33f692155cd10d177e48239d891ed2ffb4f9f065f984dde768e3616"
QUOTA_READONLY_PATHS = (
    "/proc/bus",
    "/proc/fs",
    "/proc/irq",
    "/proc/sys",
    "/proc/sysrq-trigger",
    "/etc/hosts",
    "/etc/hostname",
    "/etc/resolv.conf",
)


def workspace_quota_seccomp() -> str:
    try:
        content = QUOTA_SECCOMP_PATH.read_bytes()
    except OSError as exc:
        raise ValueError("workspace quota seccomp profile is unavailable") from exc
    if hashlib.sha256(content).hexdigest() != QUOTA_SECCOMP_SHA256:
        raise ValueError("workspace quota seccomp profile does not match the pinned checksum")
    return "seccomp=" + content.decode("utf-8")


def run_quota_container(
    client, image, *, name, hostname, environment, ports, network, labels, detach, **host_config_options
):
    host_config = client.api.create_host_config(port_bindings=ports, network_mode=network, **host_config_options)
    host_config["ReadonlyPaths"] = list(QUOTA_READONLY_PATHS)
    exposed_ports = [tuple(port.split("/", 1)) for port in sorted(host_config.get("PortBindings") or {})]
    created = client.api.create_container(
        image=image,
        name=name,
        hostname=hostname,
        environment=environment,
        ports=exposed_ports,
        networking_config={network: None} if network else None,
        labels=labels,
        detach=detach,
        host_config=host_config,
    )
    container = client.containers.get(created["Id"])
    container.start()
    return container
