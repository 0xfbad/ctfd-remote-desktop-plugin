import json
from unittest.mock import mock_open, patch
from docker_host_manager import _resolve_endpoint


def test_meta_file_endpoint():
    meta = {"Endpoints": {"docker": {"Host": "ssh://user@host:22"}}}

    with patch("docker_host_manager.os.path.expanduser", return_value="/fake/meta.json"):
        with patch("docker_host_manager.os.path.exists", return_value=True):
            with patch("builtins.open", mock_open(read_data=json.dumps(meta))):
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


def test_no_meta_no_hostname_no_socket():
    with patch("docker_host_manager.os.path.exists", return_value=False):
        result = _resolve_endpoint("ctx", hostname=None)
    assert result is None


def test_local_socket_fallback():
    def exists_side_effect(path):
        # meta file doesn't exist, but the docker socket does
        return path == "/var/run/docker.sock"

    with patch("docker_host_manager.os.path.exists", side_effect=exists_side_effect):
        result = _resolve_endpoint("ctx", hostname=None)
    assert result == "unix:///var/run/docker.sock"


def test_meta_file_takes_priority_over_hostname():
    meta = {"Endpoints": {"docker": {"Host": "tcp://localhost:2375"}}}

    with patch("docker_host_manager.os.path.expanduser", return_value="/fake/meta.json"):
        with patch("docker_host_manager.os.path.exists", return_value=True):
            with patch("builtins.open", mock_open(read_data=json.dumps(meta))):
                result = _resolve_endpoint("ctx", hostname="other-host")

    assert result == "tcp://localhost:2375"


def test_corrupt_meta_falls_through_to_hostname():
    with patch("docker_host_manager.os.path.expanduser", return_value="/fake/meta.json"):
        with patch("docker_host_manager.os.path.exists", return_value=True):
            with patch("builtins.open", mock_open(read_data="not json")):
                result = _resolve_endpoint("ctx", hostname="fallback-host")

    assert result == "ssh://root@fallback-host"
