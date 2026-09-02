import pytest
from unittest.mock import patch, MagicMock

import docker


def _make_manager():
    from _rd_plugin.docker_host_manager import DockerHostManager

    mgr = DockerHostManager()
    mgr._context_configs = {"alpha": "unix:///fake.sock"}
    mgr._config_generation = 1
    return mgr


# every key run_container reads, a blanket return_value would feed cgroup_parent a pids count and trip validation
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
    resolved_image = MagicMock()
    resolved_image.id = "sha256:immutable-desktop-image"
    resolved_image.attrs = {
        "Config": {"Labels": {"edu.ucsc.ctfd-remote-desktop.contract": "3"}},
    }
    mock_client.images.get.return_value = resolved_image

    settings = _effective_settings(settings_overrides)

    with (
        patch.object(mgr, "_get_client", return_value=mock_client),
        patch("models.get_all_settings", return_value=settings),
    ):
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
    """the daemon refuses storage_opt on ext4 data roots so the kwarg must be absent, not empty"""
    mgr = _make_manager()
    _client, kwargs = _run_with_settings(mgr, {"storage_limit": ""})
    assert "storage_opt" not in kwargs


def test_storage_limit_set_passes_storage_opt():
    mgr = _make_manager()
    _client, kwargs = _run_with_settings(mgr, {"storage_limit": "20g"})
    assert kwargs["storage_opt"] == {"size": "20g"}


def test_storage_limit_garbage_raises_before_docker_call():
    mgr = _make_manager()
    mock_client = MagicMock()

    settings = _effective_settings({"storage_limit": "garbage"})

    with (
        patch.object(mgr, "_get_client", return_value=mock_client),
        patch("models.get_all_settings", return_value=settings),
        pytest.raises(ValueError),
    ):
        mgr.run_container(
            context_name="alpha",
            image="img:latest",
            name="rd-session-1-1700000000",
            env={},
            ports=["5900/tcp", "6080/tcp"],
        )

    mock_client.containers.run.assert_not_called()


def test_default_log_caps_serialized():
    """max-file must be a string, daemon log opts are string keyed and an int fails to unmarshal"""
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
    mgr = _make_manager()
    _client, kwargs = _run_with_settings(mgr, {"storage_limit": "20g"})
    assert kwargs.get("volumes") is None
    assert kwargs.get("mounts") is None


def test_apierror_non_port_message_raises_without_retry():
    mgr = _make_manager()
    mock_client = MagicMock()
    mock_client.containers.run.side_effect = docker.errors.APIError(
        "500 Server Error: --storage-opt is supported only for overlay over xfs"
    )
    resolved_image = MagicMock()
    resolved_image.id = "sha256:immutable-desktop-image"
    resolved_image.attrs = {
        "Config": {"Labels": {"edu.ucsc.ctfd-remote-desktop.contract": "3"}},
    }
    mock_client.images.get.return_value = resolved_image

    settings = _effective_settings({"storage_limit": "20g"})

    with (
        patch.object(mgr, "_get_client", return_value=mock_client),
        patch("models.get_all_settings", return_value=settings),
        pytest.raises(docker.errors.APIError),
    ):
        mgr.run_container(
            context_name="alpha",
            image="img:latest",
            name="rd-session-1-1700000000",
            env={},
            ports=["5900/tcp", "6080/tcp"],
        )

    assert mock_client.containers.run.call_count == 1
