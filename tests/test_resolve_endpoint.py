import json
from unittest.mock import MagicMock, mock_open, patch
from docker_host_manager import DockerHostManager


def make_manager():
    # bypass __init__ which starts the async bridge
    mgr = object.__new__(DockerHostManager)
    mgr._bridge = MagicMock()
    mgr._clients = {}
    mgr._pub_hostnames = {}
    return mgr


def test_meta_file_endpoint():
    mgr = make_manager()
    meta = {"Endpoints": {"docker": {"Host": "ssh://user@host:22"}}}

    with patch("docker_host_manager.os.path.expanduser", return_value="/fake/meta.json"):
        with patch("docker_host_manager.os.path.exists", return_value=True):
            with patch("builtins.open", mock_open(read_data=json.dumps(meta))):
                result = mgr._resolve_endpoint("test-ctx", hostname=None)

    assert result == "ssh://user@host:22"


def test_hostname_with_user():
    mgr = make_manager()

    with patch("docker_host_manager.os.path.exists", return_value=False):
        result = mgr._resolve_endpoint("ctx", hostname="admin@host.example.com")
    assert result == "ssh://admin@host.example.com"


def test_hostname_without_user():
    mgr = make_manager()

    with patch("docker_host_manager.os.path.exists", return_value=False):
        result = mgr._resolve_endpoint("ctx", hostname="host.example.com")
    assert result == "ssh://root@host.example.com"


def test_no_meta_no_hostname():
    mgr = make_manager()

    with patch("docker_host_manager.os.path.exists", return_value=False):
        result = mgr._resolve_endpoint("ctx", hostname=None)
    assert result is None


def test_meta_file_takes_priority_over_hostname():
    mgr = make_manager()
    meta = {"Endpoints": {"docker": {"Host": "tcp://localhost:2375"}}}

    with patch("docker_host_manager.os.path.expanduser", return_value="/fake/meta.json"):
        with patch("docker_host_manager.os.path.exists", return_value=True):
            with patch("builtins.open", mock_open(read_data=json.dumps(meta))):
                result = mgr._resolve_endpoint("ctx", hostname="other-host")

    assert result == "tcp://localhost:2375"


def test_corrupt_meta_falls_through_to_hostname():
    mgr = make_manager()

    with patch("docker_host_manager.os.path.expanduser", return_value="/fake/meta.json"):
        with patch("docker_host_manager.os.path.exists", return_value=True):
            with patch("builtins.open", mock_open(read_data="not json")):
                result = mgr._resolve_endpoint("ctx", hostname="fallback-host")

    assert result == "ssh://root@fallback-host"
