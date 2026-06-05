"""Verify the rd-isolated network plumbing.

run_container must pass network=<name> to client.containers.run so the
docker daemon attaches the new container to the named bridge instead of
docker0. load_contexts must emit a warning when a connected docker host
is missing the configured network, so an operator who deploys the plugin
before running the T08 runbook sees the failure mode clearly in logs.
"""

import logging
from unittest.mock import patch, MagicMock


def _make_manager():
    from _rd_plugin.docker_host_manager import DockerHostManager

    mgr = DockerHostManager()
    mgr._context_configs = {"alpha": "unix:///fake.sock"}
    mgr._config_generation = 1
    mgr._client_generation = 1
    return mgr


def _patched_run_container(mgr, **overrides):
    """invoke run_container with a mocked docker client; return the kwargs
    that client.containers.run was called with"""
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

    with patch.object(mgr, "_get_client", return_value=mock_client):
        with patch("models.get_setting", return_value=4096):
            kwargs = dict(
                context_name="alpha",
                image="img:latest",
                name="rd-session-1-1700000000",
                env={"VNC_PASSWORD": "secret"},
                ports=["5900/tcp", "6080/tcp"],
            )
            kwargs.update(overrides)
            mgr.run_container(**kwargs)

    assert mock_client.containers.run.called
    return mock_client.containers.run.call_args.kwargs


def test_run_container_passes_network_kwarg_to_docker():
    mgr = _make_manager()
    call_kwargs = _patched_run_container(mgr, network="rd-isolated")
    assert call_kwargs["network"] == "rd-isolated"


def test_run_container_passes_override_network_name():
    """when an operator overrides the setting (e.g. to 'bridge' as an
    emergency rollback), that value must reach containers.run"""
    mgr = _make_manager()
    call_kwargs = _patched_run_container(mgr, network="bridge")
    assert call_kwargs["network"] == "bridge"


def test_run_container_default_network_is_none_for_backcompat():
    """callers that don't pass network get None, which docker-py treats as
    'default bridge'. preserves behavior for any out-of-band caller (cli,
    future tooling) that hasn't been updated"""
    mgr = _make_manager()
    call_kwargs = _patched_run_container(mgr)
    assert call_kwargs.get("network") is None


def test_container_manager_reads_setting_and_passes_through():
    """create_container reads rd_network_name and forwards to host_manager"""
    from _rd_plugin.container_manager import ContainerManager

    cm = ContainerManager(MagicMock(), MagicMock(), MagicMock())

    settings = {
        "docker_image": "img:latest",
        "resolution": "1920x1080",
        "shm_size": "256m",
        "memory_limit": "2g",
        "cpu_limit": 1,
        "initial_duration": 3600,
        "extension_duration": 1800,
        "max_extensions": 3,
        "vnc_ready_attempts": 1,
        "http_request_timeout": 1,
        "command_logging_enabled": False,
        "username_source": "name",
        "rd_network_name": "rd-isolated",
    }

    cm.host_manager.run_container.return_value = {
        "container_id": "abc",
        "container_name": "rd-session-1-1",
        "ports": {"22/tcp": 1, "5900/tcp": 2, "6080/tcp": 3, "7682/tcp": 4},
    }
    cm.orchestrator.select_and_reserve.return_value = "alpha"
    cm.host_manager.get_pub_hostname.return_value = "alpha.example.com"
    cm.host_manager.get_check_hostname.return_value = "alpha.example.com"

    user = MagicMock()
    user.id = 1
    user.name = "alice"
    user.email = "alice@example.com"

    with (
        patch("container_manager._mint_session_cookie", return_value=None),
        patch.object(cm, "_get_setting", side_effect=lambda k: settings.get(k)),
        patch.object(cm, "wait_for_vnc_ready", return_value=True),
        patch("container_manager._display_name", return_value=(user, "alice")),
        patch("container_manager.DesktopContainerInfoModel"),
        patch("container_manager.db"),
        patch("container_manager.event_logger"),
    ):
        cm._create_container_background(user_id=1, container_url="http://ctfd", extra_hosts=None)

    cm.host_manager.run_container.assert_called_once()
    call_kwargs = cm.host_manager.run_container.call_args.kwargs
    assert call_kwargs["network"] == "rd-isolated"


def test_container_manager_passes_overridden_network():
    """setting overridden to 'bridge' flows all the way to host_manager"""
    from _rd_plugin.container_manager import ContainerManager

    cm = ContainerManager(MagicMock(), MagicMock(), MagicMock())

    settings = {
        "docker_image": "img:latest",
        "resolution": "1920x1080",
        "shm_size": "256m",
        "memory_limit": "2g",
        "cpu_limit": 1,
        "initial_duration": 3600,
        "extension_duration": 1800,
        "max_extensions": 3,
        "vnc_ready_attempts": 1,
        "http_request_timeout": 1,
        "command_logging_enabled": False,
        "username_source": "name",
        "rd_network_name": "bridge",
    }

    cm.host_manager.run_container.return_value = {
        "container_id": "abc",
        "container_name": "rd-session-1-1",
        "ports": {"22/tcp": 1, "5900/tcp": 2, "6080/tcp": 3, "7682/tcp": 4},
    }
    cm.orchestrator.select_and_reserve.return_value = "alpha"
    cm.host_manager.get_pub_hostname.return_value = "alpha.example.com"
    cm.host_manager.get_check_hostname.return_value = "alpha.example.com"

    user = MagicMock()
    user.id = 1
    user.name = "alice"
    user.email = "alice@example.com"

    with (
        patch("container_manager._mint_session_cookie", return_value=None),
        patch.object(cm, "_get_setting", side_effect=lambda k: settings.get(k)),
        patch.object(cm, "wait_for_vnc_ready", return_value=True),
        patch("container_manager._display_name", return_value=(user, "alice")),
        patch("container_manager.DesktopContainerInfoModel"),
        patch("container_manager.db"),
        patch("container_manager.event_logger"),
    ):
        cm._create_container_background(user_id=1, container_url="http://ctfd", extra_hosts=None)

    call_kwargs = cm.host_manager.run_container.call_args.kwargs
    assert call_kwargs["network"] == "bridge"


def _stub_context_row(name="alpha", host="alpha.example.com"):
    row = MagicMock()
    row.context_name = name
    row.hostname = host
    row.pub_hostname = host
    return row


def _settings_dispatch(rd_network):
    """dispatch get_setting by key so _init_semaphores still gets an int for
    max_concurrent_creates while load_contexts gets the network name string"""

    def _impl(key, default=None):
        if key == "rd_network_name":
            return rd_network
        if key == "max_concurrent_creates":
            return 4
        return default

    return _impl


def test_load_contexts_warns_when_network_missing(caplog):
    """startup probe: connected context that lacks rd-isolated must log a
    WARNING so an operator who skipped the runbook notices before any
    container create attempt fails"""
    from _rd_plugin.docker_host_manager import DockerHostManager

    mgr = DockerHostManager()
    mock_client = MagicMock()
    mock_client.networks.list.return_value = []

    with (
        patch("_rd_plugin.docker_host_manager._resolve_endpoint", return_value="unix:///fake.sock"),
        patch("_rd_plugin.docker_host_manager.docker.DockerClient", return_value=mock_client),
        patch("models.get_setting", side_effect=_settings_dispatch("rd-isolated")),
        caplog.at_level(logging.WARNING, logger="_rd_plugin.docker_host_manager"),
    ):
        mgr.load_contexts([_stub_context_row("alpha")])

    mock_client.networks.list.assert_called_with(names=["rd-isolated"])

    warned = [r for r in caplog.records if "rd-isolated" in r.message and "missing" in r.message]
    assert warned, f"no missing-network warning logged: {[r.message for r in caplog.records]}"


def test_load_contexts_no_warning_when_network_present(caplog):
    from _rd_plugin.docker_host_manager import DockerHostManager

    mgr = DockerHostManager()
    mock_network = MagicMock()
    mock_network.name = "rd-isolated"
    mock_client = MagicMock()
    mock_client.networks.list.return_value = [mock_network]

    with (
        patch("_rd_plugin.docker_host_manager._resolve_endpoint", return_value="unix:///fake.sock"),
        patch("_rd_plugin.docker_host_manager.docker.DockerClient", return_value=mock_client),
        patch("models.get_setting", side_effect=_settings_dispatch("rd-isolated")),
        caplog.at_level(logging.WARNING, logger="_rd_plugin.docker_host_manager"),
    ):
        mgr.load_contexts([_stub_context_row("alpha")])

    warned = [r for r in caplog.records if "missing" in r.message]
    assert not warned, f"unexpected warning when network is present: {[r.message for r in warned]}"


def test_load_contexts_skips_probe_when_network_is_bridge(caplog):
    """if the operator overrode the setting to 'bridge' (emergency rollback),
    we shouldn't probe - bridge always exists and the whole point is to
    suppress the warning channel"""
    from _rd_plugin.docker_host_manager import DockerHostManager

    mgr = DockerHostManager()
    mock_client = MagicMock()

    with (
        patch("_rd_plugin.docker_host_manager._resolve_endpoint", return_value="unix:///fake.sock"),
        patch("_rd_plugin.docker_host_manager.docker.DockerClient", return_value=mock_client),
        patch("models.get_setting", side_effect=_settings_dispatch("bridge")),
        caplog.at_level(logging.WARNING, logger="_rd_plugin.docker_host_manager"),
    ):
        mgr.load_contexts([_stub_context_row("alpha")])

    mock_client.networks.list.assert_not_called()
    warned = [r for r in caplog.records if "missing" in r.message or "network check failed" in r.message]
    assert not warned


def test_load_contexts_warns_when_network_probe_throws(caplog):
    """if the docker call itself errors (e.g. SSH flake mid-check), we must
    still log a warning rather than silently treating the context as healthy
    on the network front"""
    from _rd_plugin.docker_host_manager import DockerHostManager

    mgr = DockerHostManager()
    mock_client = MagicMock()
    mock_client.networks.list.side_effect = RuntimeError("ssh blew up")

    with (
        patch("_rd_plugin.docker_host_manager._resolve_endpoint", return_value="unix:///fake.sock"),
        patch("_rd_plugin.docker_host_manager.docker.DockerClient", return_value=mock_client),
        patch("models.get_setting", side_effect=_settings_dispatch("rd-isolated")),
        caplog.at_level(logging.WARNING, logger="_rd_plugin.docker_host_manager"),
    ):
        mgr.load_contexts([_stub_context_row("alpha")])

    warned = [r for r in caplog.records if "network check failed" in r.message]
    assert warned, f"expected probe-error warning, got: {[r.message for r in caplog.records]}"
