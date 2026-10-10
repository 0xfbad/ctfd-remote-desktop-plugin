from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
PROFILE_DIR = ROOT / "src" / "seccomp"
BLOCKED_IOCTL_WORDS = {0x5820, 0x6602}


def allowed_ioctl_prefixes(candidates, bit=15, mask=0, value=0):
    if not candidates:
        return [(mask, value)]
    if bit < 0:
        return []
    flag = 1 << bit
    result = []
    for branch in (0, flag):
        matches = {word for word in candidates if word & flag == branch}
        result.extend(allowed_ioctl_prefixes(matches, bit - 1, mask | flag, value | branch))
    return result


def quota_profile(default_profile):
    profile = copy.deepcopy(default_profile)
    for rule in profile["syscalls"]:
        rule["names"] = [name for name in rule["names"] if name != "ioctl"]
    profile["syscalls"] = [rule for rule in profile["syscalls"] if rule["names"]]
    for mask, value in allowed_ioctl_prefixes(BLOCKED_IOCTL_WORDS):
        profile["syscalls"].append(
            {
                "names": ["ioctl"],
                "action": "SCMP_ACT_ALLOW",
                "args": [{"index": 1, "op": "SCMP_CMP_MASKED_EQ", "value": mask, "valueTwo": value}],
            }
        )
    return profile


def check_container_requests(loader, source):
    default_paths = ["/proc/bus", "/proc/fs", "/proc/irq", "/proc/sys", "/proc/sysrq-trigger"]
    readonly_paths = default_paths + ["/etc/hosts", "/etc/hostname", "/etc/resolv.conf"]
    assert source["readonly_paths"]["defaults"] == default_paths
    assert list(loader.QUOTA_READONLY_PATHS) == readonly_paths
    for network in (None, "", "bridge", "host", "none", "desktop-network"):
        client = Mock()
        host_config = {
            "PortBindings": {"5000/sctp": [{"HostPort": "45000"}], "53/udp": [], "22/tcp": []},
            "StorageOpt": {"size": "20g"},
            "Tmpfs": {"/run": "rw,exec,nosuid,nodev,size=64m,mode=0755"},
        }
        if network == "desktop-network":
            host_config.update(MaskedPaths=["/proc/kcore"], ReadonlyRootfs=False)
        original = copy.deepcopy(host_config)
        client.api.create_host_config.return_value = host_config
        client.api.create_container.return_value = {"Id": "created-container"}
        ports = {22: 40022, "53/udp": 40053, "5000/sctp": 45000}
        options = {
            "auto_remove": True,
            "init": True,
            "storage_opt": {"size": "20g"},
            "tmpfs": {"/run": "rw,exec,nosuid,nodev,size=64m,mode=0755"},
        }
        container = loader.run_quota_container(
            client,
            "sha256:desktop",
            name="desktop",
            hostname="desktop-host",
            environment={"VNC_PASSWORD": "secret"},
            ports=ports,
            network=network,
            labels={"managed": "true"},
            detach=True,
            **options,
        )
        client.api.create_host_config.assert_called_once_with(port_bindings=ports, network_mode=network, **options)
        original["ReadonlyPaths"] = readonly_paths
        assert host_config == original
        if network != "desktop-network":
            assert "MaskedPaths" not in host_config
            assert "ReadonlyRootfs" not in host_config
        client.api.create_container.assert_called_once_with(
            image="sha256:desktop",
            name="desktop",
            hostname="desktop-host",
            environment={"VNC_PASSWORD": "secret"},
            ports=[("22", "tcp"), ("5000", "sctp"), ("53", "udp")],
            networking_config={network: None} if network else None,
            labels={"managed": "true"},
            detach=True,
            host_config=original,
        )
        client.containers.get.assert_called_once_with("created-container")
        assert container is client.containers.get.return_value
        container.start.assert_called_once_with()

    error = RuntimeError("container start failed")
    container.start.side_effect = error
    try:
        loader.run_quota_container(
            client,
            "sha256:desktop",
            name="desktop",
            hostname="desktop-host",
            environment={},
            ports=ports,
            network=None,
            labels={},
            detach=True,
        )
    except RuntimeError as exc:
        assert exc is error
    else:
        raise AssertionError("container start failure must propagate to the owner")
    container.remove.assert_not_called()


def main():
    source = json.loads((PROFILE_DIR / "source.json").read_bytes())
    content = (PROFILE_DIR / "workspace-quota.json").read_bytes()
    profile = json.loads(content)
    assert hashlib.sha256(content).hexdigest() == source["profile"]["sha256"]
    assert hashlib.sha256((PROFILE_DIR / "LICENSE").read_bytes()).hexdigest() == source["license"]["sha256"]
    ioctl_rules = [rule for rule in profile["syscalls"] if "ioctl" in rule["names"]]
    assert len(ioctl_rules) == 28
    prefixes = allowed_ioctl_prefixes(BLOCKED_IOCTL_WORDS)
    for rule, (mask, value) in zip(ioctl_rules, prefixes, strict=True):
        assert rule == {
            "names": ["ioctl"],
            "action": "SCMP_ACT_ALLOW",
            "args": [{"index": 1, "op": "SCMP_CMP_MASKED_EQ", "value": mask, "valueTwo": value}],
        }
    for word in range(1 << 16):
        assert any(word & mask == value for mask, value in prefixes) == (word not in BLOCKED_IOCTL_WORDS)
    for high_bits in (0, 0x40000000, 0x80000000, 0xFFFFFFFF00000000):
        for word in (0x5820, 0x6602, 0x581F, 0x5821, 0x6601, 0x6603, 0x5413):
            request = high_bits | word
            assert any(request & mask == value for mask, value in prefixes) == (word not in BLOCKED_IOCTL_WORDS)
    assert not any("file_setattr" in rule["names"] for rule in profile["syscalls"])
    restored = copy.deepcopy(profile)
    restored["syscalls"] = [rule for rule in restored["syscalls"] if "ioctl" not in rule["names"]]
    metadata = source["profile"]
    restored["syscalls"][metadata["upstream_ioctl_rule_index"]]["names"].insert(
        metadata["upstream_ioctl_name_index"], "ioctl"
    )
    canonical = json.dumps(restored, sort_keys=True, separators=(",", ":")).encode()
    assert hashlib.sha256(canonical).hexdigest() == source["upstream"]["canonical_sha256"]
    assert json.dumps(quota_profile(restored), indent=2).encode() + b"\n" == content
    loader_source = ROOT / "src" / "quota_security.py"
    spec = importlib.util.spec_from_file_location("quota_security", loader_source)
    assert spec is not None and spec.loader is not None
    loader = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loader)
    assert loader.QUOTA_SECCOMP_SHA256 == source["profile"]["sha256"]
    assert loader.workspace_quota_seccomp() == "seccomp=" + content.decode()
    check_container_requests(loader, source)
    with tempfile.TemporaryDirectory(prefix="workspace-quota-check-") as directory:
        invalid_path = Path(directory) / "profile.json"
        setattr(loader, "QUOTA_SECCOMP_PATH", invalid_path)
        for invalid in (None, b"{", b"{}", content + b" ", b'{"defaultAction":"SCMP_ACT_ALLOW"}'):
            if invalid is not None:
                invalid_path.write_bytes(invalid)
            try:
                loader.workspace_quota_seccomp()
            except ValueError:
                pass
            else:
                raise AssertionError("missing or altered quota profile must fail closed")
    if len(sys.argv) == 2:
        upstream = Path(sys.argv[1]).read_bytes()
        assert hashlib.sha256(upstream).hexdigest() == source["upstream"]["sha256"]
        assert json.dumps(quota_profile(json.loads(upstream)), indent=2).encode() + b"\n" == content
    else:
        assert len(sys.argv) == 1, "usage: python checks/workspace_quota.py [pinned-upstream-default.json]"
    print(
        json.dumps(
            {
                "pass": True,
                "checks": [
                    "all 65536 ioctl identifiers and high bits preserve exact setter exclusions",
                    "remaining default rules equal the pinned upstream profile",
                    "regeneration matches the vendored profile bytes",
                    "profile and license checksums match provenance",
                    "loader sends inline JSON and rejects missing or altered profiles",
                    "capped create preserves Docker defaults, protocol ports, and network options",
                    "container start failures propagate to the existing owner",
                ],
            }
        )
    )


if __name__ == "__main__":
    main()
