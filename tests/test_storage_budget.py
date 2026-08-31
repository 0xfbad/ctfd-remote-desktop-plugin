"""Feature 1: per-session storage budget + log caps in run_container.

storage_limit "" must omit storage_opt entirely (the daemon refuses the
kwarg on non-xfs data-roots, so presence-with-empty would brick every dev
create); a bad value must fail loudly BEFORE any docker call. log caps
default on, escape hatch log_max_size="" omits the kwarg. The section-6
invariant pins run_container to zero volume/mount kwargs.
"""

import pytest
from unittest.mock import patch, MagicMock

import docker


def _make_manager():
    from _rd_plugin.docker_host_manager import DockerHostManager

    mgr = DockerHostManager()
    mgr._context_configs = {"alpha": "unix:///fake.sock"}
    mgr._config_generation = 1
    mgr._client_generation = 1
    return mgr


# every settings key run_container reads, keyed dispatch (a blanket
# return_value would feed e.g. cgroup_parent="4096" into the validators)
BASE_SETTINGS = {
    "pids_limit": 4096,
    "cap_drop": "ALL",
    "cap_add": "",
    "storage_limit": "",
    "log_max_size": "50m",
    "log_max_file": 3,
    "memory_reservation": "",
    "oom_score_adj": 0,
    "nofile_soft": 0,
    "nofile_hard": 0,
    "cgroup_parent": "",
}


def _effective_settings(overrides=None):
    from settings import SETTING_DEFAULTS

    settings = dict(SETTING_DEFAULTS)
    settings.update(BASE_SETTINGS)
    settings.update(overrides or {})
    return settings


def _run_with_settings(mgr, settings_overrides=None, **call_overrides):
    """invoke run_container with a mocked docker client; return
    (mock_client, kwargs that client.containers.run was called with)"""
    mock_client = MagicMock()
    mock_container = MagicMock()
    mock_container.id = "container-id-xyz"
    mock_container.attrs = {
        "NetworkSettings": {
            "Ports": {
                "5900/tcp": [{"HostPort": "40001"}],
                "6080/tcp": [{"HostPort": "40002"}],
            }
        }
    }
    mock_client.containers.run.return_value = mock_container

    settings = _effective_settings(settings_overrides)

    with patch.object(mgr, "_get_client", return_value=mock_client):
        with patch("models.get_all_settings", return_value=settings):
            kwargs = dict(
                context_name="alpha",
                image="img:latest",
                name="rd-session-1-1700000000",
                env={"VNC_PASSWORD": "secret"},
                ports=["5900/tcp", "6080/tcp"],
            )
            kwargs.update(call_overrides)
            mgr.run_container(**kwargs)

    assert mock_client.containers.run.called
    return mock_client, mock_client.containers.run.call_args.kwargs


def test_storage_limit_empty_omits_storage_opt():
    """default "" must not put storage_opt in the create call at all -
    presence of the kwarg is refused by the daemon on ext4 data-roots"""
    mgr = _make_manager()
    _client, kwargs = _run_with_settings(mgr, {"storage_limit": ""})
    assert "storage_opt" not in kwargs


def test_storage_limit_set_passes_storage_opt():
    mgr = _make_manager()
    _client, kwargs = _run_with_settings(mgr, {"storage_limit": "20g"})
    assert kwargs["storage_opt"] == {"size": "20g"}


def test_storage_limit_garbage_raises_before_docker_call():
    """parse_size("garbage") hits int("garbage") -> ValueError; must fire
    before the client is touched"""
    mgr = _make_manager()
    mock_client = MagicMock()

    settings = _effective_settings({"storage_limit": "garbage"})

    with patch.object(mgr, "_get_client", return_value=mock_client):
        with patch("models.get_all_settings", return_value=settings):
            with pytest.raises(ValueError):
                mgr.run_container(
                    context_name="alpha",
                    image="img:latest",
                    name="rd-session-1-1700000000",
                    env={},
                    ports=["5900/tcp", "6080/tcp"],
                )

    mock_client.containers.run.assert_not_called()


def test_default_log_caps_serialized():
    """defaults log_max_size=50m/log_max_file=3 -> json-file caps; max-file
    MUST be a str (daemon log-opts are map[string]string, an int fails
    JSON unmarshal)"""
    mgr = _make_manager()
    _client, kwargs = _run_with_settings(mgr)
    assert kwargs["log_config"] == {
        "type": "json-file",
        "config": {"max-size": "50m", "max-file": "3"},
    }
    assert isinstance(kwargs["log_config"]["config"]["max-file"], str)


def test_log_max_size_empty_omits_log_config():
    mgr = _make_manager()
    _client, kwargs = _run_with_settings(mgr, {"log_max_size": ""})
    assert "log_config" not in kwargs


def test_no_writable_or_persistent_mounts_invariant():
    """Ordinary sessions have no volume or mount paths outside storage_opt."""
    mgr = _make_manager()
    _client, kwargs = _run_with_settings(mgr, {"storage_limit": "20g"})
    assert kwargs.get("volumes") is None
    assert kwargs.get("mounts") is None


def test_apierror_non_port_message_raises_without_retry():
    """a daemon storage-opt refusal is an APIError without the port-collision
    strings; it must propagate out and NOT be retried as a port clash"""
    mgr = _make_manager()
    mock_client = MagicMock()
    mock_client.containers.run.side_effect = docker.errors.APIError(
        "500 Server Error: --storage-opt is supported only for overlay over xfs"
    )

    settings = _effective_settings({"storage_limit": "20g"})

    with patch.object(mgr, "_get_client", return_value=mock_client):
        with patch("models.get_all_settings", return_value=settings):
            with pytest.raises(docker.errors.APIError):
                mgr.run_container(
                    context_name="alpha",
                    image="img:latest",
                    name="rd-session-1-1700000000",
                    env={},
                    ports=["5900/tcp", "6080/tcp"],
                )

    assert mock_client.containers.run.call_count == 1
