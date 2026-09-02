from unittest.mock import mock_open, patch

import pytest

from docker_host_manager import _apply_ssh_connect_timeouts, _resolve_endpoint, discover_contexts


def test_paramiko_connect_phases_receive_explicit_bounds():
    params: dict[str, object] = {"hostname": "runner.example"}
    _apply_ssh_connect_timeouts(params, 3)
    assert params == {
        "hostname": "runner.example",
        "timeout": 3.0,
        "banner_timeout": 3.0,
        "auth_timeout": 3.0,
    }


def test_meta_file_endpoint():
    meta = {"Endpoints": {"docker": {"Host": "ssh://user@host:22"}}}

    with patch("docker_host_manager._scan_context_meta", return_value=meta):
        result = _resolve_endpoint("test-ctx", hostname=None)

    assert result == "ssh://user@host:22"


def test_hostname_with_user():
    with patch("docker_host_manager.os.path.exists", return_value=False):
        result = _resolve_endpoint("ctx", hostname="admin@host.example.com")
    assert result == "ssh://admin@host.example.com"


def test_hostname_without_user():
    with patch("docker_host_manager.os.path.exists", return_value=False):
        result = _resolve_endpoint("ctx", hostname="host.example.com")
    assert result == "ssh://root@host.example.com"


def test_explicit_endpoint_scheme_is_not_double_wrapped():
    with patch("docker_host_manager.os.path.exists", return_value=False):
        result = _resolve_endpoint("ctx", hostname="ssh://admin@host.example.com:22")
    assert result == "ssh://admin@host.example.com:22"


def test_explicit_unsupported_endpoint_scheme_is_rejected():
    with patch("docker_host_manager.os.path.exists", return_value=False):
        assert _resolve_endpoint("ctx", hostname="tcp://host.example.com:2375") is None


@pytest.mark.parametrize(
    "hostname",
    [
        "ssh://",
        "ssh://host.example.com:",
        "ssh://host.example.com:0",
        "ssh://host.example.com:65536",
        "ssh://host.example.com:not-a-port",
        "ssh://@host.example.com",
        "ssh://user@@host.example.com",
        "ssh://user:secret@host.example.com",
        "ssh://host.example.com/path",
        "ssh://host.example.com?option=value",
        "ssh://host.example.com#fragment",
        "root:secret@host.example.com",
        "root@host.example.com/path",
        "root@@host.example.com",
        "host.example.com:",
    ],
)
def test_malformed_explicit_ssh_endpoint_is_rejected(hostname):
    with patch("docker_host_manager.os.path.exists", return_value=False):
        assert _resolve_endpoint("ctx", hostname=hostname) is None


def test_no_meta_no_hostname_no_socket():
    with patch("docker_host_manager.os.path.exists", return_value=False):
        result = _resolve_endpoint("ctx", hostname=None)
    assert result is None


def test_local_socket_fallback():
    def exists_side_effect(path):
        return path == "/var/run/docker.sock"

    with patch("docker_host_manager.os.path.exists", side_effect=exists_side_effect):
        result = _resolve_endpoint("local", hostname=None)
    assert result == "unix:///var/run/docker.sock"


def test_nonlocal_context_never_falls_back_to_local_socket():
    with (
        patch("docker_host_manager._scan_context_meta", return_value=None),
        patch("docker_host_manager.os.path.exists", return_value=True),
    ):
        assert _resolve_endpoint("missing-remote", hostname=None) is None


def test_unsupported_meta_file_falls_through_to_valid_hostname():
    meta = {"Endpoints": {"docker": {"Host": "tcp://localhost:2375"}}}

    with patch("docker_host_manager._scan_context_meta", return_value=meta):
        result = _resolve_endpoint("ctx", hostname="other-host")

    assert result == "ssh://root@other-host"


@pytest.mark.parametrize("endpoint", ["tcp://localhost:2375", "http://daemon:2375", "npipe:////./pipe/docker_engine"])
def test_unsupported_meta_endpoint_is_rejected(endpoint):
    meta = {"Endpoints": {"docker": {"Host": endpoint}}}

    with (
        patch("docker_host_manager._scan_context_meta", return_value=meta),
        patch("docker_host_manager.os.path.exists", return_value=False),
    ):
        assert _resolve_endpoint("ctx", hostname=None) is None


def test_only_canonical_local_context_may_use_local_socket_metadata():
    meta = {"Endpoints": {"docker": {"Host": "unix:///var/run/docker.sock"}}}

    with patch("docker_host_manager._scan_context_meta", return_value=meta):
        assert _resolve_endpoint("local", hostname=None) == "unix:///var/run/docker.sock"
        assert _resolve_endpoint("remote", hostname=None) is None


def test_discovery_omits_unsupported_metadata_endpoints():
    metadata = [
        {"Name": "good", "Endpoints": {"docker": {"Host": "ssh://runner.example"}}},
        {"Name": "tcp", "Endpoints": {"docker": {"Host": "tcp://runner.example:2375"}}},
        {"Name": "socket", "Endpoints": {"docker": {"Host": "unix:///tmp/docker.sock"}}},
    ]

    with (
        patch("docker_host_manager._scan_context_meta", return_value=metadata),
        patch("docker_host_manager.os.path.exists", return_value=False),
    ):
        assert discover_contexts() == [{"name": "good", "endpoint": "ssh://runner.example"}]


def test_discovery_ignores_malformed_metadata_shapes():
    metadata = [
        [],
        {"Name": "missing-endpoints"},
        {"Name": "bad-endpoints", "Endpoints": "ssh://runner.example"},
        {"Name": "bad-docker", "Endpoints": {"docker": []}},
        {"Name": "bad-host", "Endpoints": {"docker": {"Host": 2375}}},
    ]

    with (
        patch("docker_host_manager._scan_context_meta", return_value=metadata),
        patch("docker_host_manager.os.path.exists", return_value=False),
    ):
        assert discover_contexts() == []


def test_corrupt_meta_falls_through_to_hostname():
    with (
        patch("docker_host_manager.os.path.expanduser", return_value="/fake/meta.json"),
        patch("docker_host_manager.os.path.exists", return_value=True),
        patch("builtins.open", mock_open(read_data="not json")),
    ):
        result = _resolve_endpoint("ctx", hostname="fallback-host")

    assert result == "ssh://root@fallback-host"
