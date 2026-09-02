"""Verify configured Docker network plumbing.

run_container must pass network=<name> to client.containers.run so the
docker daemon attaches the new container to the named bridge instead of
docker0. load_contexts must emit a warning when a connected Docker host
is missing the configured network so the failure is visible before create.
"""

import logging
from unittest.mock import patch, MagicMock


def _make_manager():
    from _rd_plugin.docker_host_manager import DockerHostManager

    mgr = DockerHostManager()
    mgr._context_configs = {"alpha": "unix:///fake.sock"}
    mgr._config_generation = 1
    return mgr


def _settings_profile(**overrides):
    from settings import SETTING_DEFAULTS

    profile = dict(SETTING_DEFAULTS)
    profile.update(overrides)
    return profile


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
    resolved_image = MagicMock()
    resolved_image.id = "sha256:immutable-desktop-image"
    resolved_image.attrs = {
        "Config": {"Labels": {"edu.ucsc.ctfd-remote-desktop.contract": "3"}},
    }
    mock_client.images.get.return_value = resolved_image

    # keyed dispatch: run_container reads many settings now; a blanket
    # return_value would feed e.g. cgroup_parent="4096" into the validators
    base_settings = _settings_profile(
        pids_limit=4096,
        cap_drop="ALL",
        cap_add="",
        storage_limit="",
        log_max_size="",
        log_max_file=3,
        memory_reservation="",
        oom_score_adj=0,
        nofile_soft=0,
        nofile_hard=0,
        cgroup_parent="",
    )

    with patch.object(mgr, "_get_client", return_value=mock_client):
        with patch("models.get_all_settings", return_value=base_settings):
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
    call_kwargs = _patched_run_container(mgr, network="ctfd-desktops")
    assert call_kwargs["network"] == "ctfd-desktops"
    assert call_kwargs["init"] is True


def test_run_container_passes_override_network_name():
    """The configured network name must reach containers.run."""
    mgr = _make_manager()
    call_kwargs = _patched_run_container(mgr, network="bridge")
    assert call_kwargs["network"] == "bridge"


def test_run_container_default_network_is_none():
    """Omitting the optional network delegates selection to Docker."""
    mgr = _make_manager()
    call_kwargs = _patched_run_container(mgr)
    assert call_kwargs.get("network") is None


def test_unix_context_keeps_public_name_but_checks_bridge_gateway():
    mgr = _make_manager()
    mgr._pub_hostnames = {"alpha": "runner.public.example"}

    with patch("_rd_plugin.docker_host_manager._get_host_gateway", return_value="172.17.0.1") as gateway:
        assert mgr.get_connection_hostnames("alpha") == (
            "runner.public.example",
            "172.17.0.1",
        )
        assert mgr.get_pub_hostname("alpha") == "runner.public.example"
        assert mgr.get_check_hostname("alpha") == "172.17.0.1"
    assert gateway.call_count == 2


def test_check_hostname_uses_gateway_only_without_configured_address():
    mgr = _make_manager()
    mgr._pub_hostnames = {}

    with patch("_rd_plugin.docker_host_manager._get_host_gateway", return_value="172.17.0.1") as gateway:
        assert mgr.get_connection_hostnames("alpha") == (None, "172.17.0.1")
        assert mgr.get_check_hostname("alpha") == "172.17.0.1"
    assert gateway.call_count == 2


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
        "username_source": "name",
        "rd_network_name": "ctfd-desktops",
        "ssh_enabled": True,
        "web_terminal_enabled": True,
    }

    cm.host_manager.run_container.return_value = {
        "container_id": "abc",
        "container_name": "rd-session-1-1",
        "ports": {"22/tcp": 1, "5900/tcp": 2, "6080/tcp": 3, "7682/tcp": 4},
    }
    cm.orchestrator.select_and_reserve.return_value = "alpha"
    cm.host_manager.get_connection_hostnames.return_value = (
        "alpha.example.com",
        "alpha.example.com",
    )

    user = MagicMock()
    user.id = 1
    user.name = "alice"
    user.email = "alice@example.com"

    with (
        patch("container_manager._mint_session_cookie", return_value=None),
        patch.object(cm, "_get_setting", side_effect=lambda k: settings.get(k)),
        patch.object(cm, "wait_for_vnc_ready", return_value=True),
        patch.object(cm, "_read_resolved_username", return_value="alice"),
        patch("container_manager._display_name", return_value=(user, "alice")),
        patch("container_manager.DesktopContainerInfoModel"),
        patch("container_manager.db"),
        patch("container_manager.event_logger"),
    ):
        cm._create_container_background(user_id=1, container_url="http://ctfd", extra_hosts=None)

    cm.host_manager.run_container.assert_called_once()
    cm.host_manager.get_connection_hostnames.assert_called_once_with("alpha")
    cm.host_manager.get_pub_hostname.assert_not_called()
    cm.host_manager.get_check_hostname.assert_not_called()
    call_kwargs = cm.host_manager.run_container.call_args.kwargs
    assert call_kwargs["network"] == "ctfd-desktops"


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
        "username_source": "name",
        "rd_network_name": "bridge",
        "ssh_enabled": True,
        "web_terminal_enabled": True,
    }

    cm.host_manager.run_container.return_value = {
        "container_id": "abc",
        "container_name": "rd-session-1-1",
        "ports": {"22/tcp": 1, "5900/tcp": 2, "6080/tcp": 3, "7682/tcp": 4},
    }
    cm.orchestrator.select_and_reserve.return_value = "alpha"
    cm.host_manager.get_connection_hostnames.return_value = (
        "alpha.example.com",
        "alpha.example.com",
    )

    user = MagicMock()
    user.id = 1
    user.name = "alice"
    user.email = "alice@example.com"

    with (
        patch("container_manager._mint_session_cookie", return_value=None),
        patch.object(cm, "_get_setting", side_effect=lambda k: settings.get(k)),
        patch.object(cm, "wait_for_vnc_ready", return_value=True),
        patch.object(cm, "_read_resolved_username", return_value="alice"),
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
    return _settings_profile(
        rd_network_name=rd_network,
        max_concurrent_creates=4,
    )


def test_load_contexts_warns_when_network_missing(caplog):
    """The startup probe warns before a create attempts a missing network."""
    from _rd_plugin.docker_host_manager import DockerHostManager

    mgr = DockerHostManager()
    mock_client = MagicMock()
    mock_client.networks.list.return_value = []

    with (
        patch("_rd_plugin.docker_host_manager._resolve_endpoint", return_value="unix:///fake.sock"),
        patch("_rd_plugin.docker_host_manager.docker.DockerClient", return_value=mock_client),
        patch("models.get_all_settings", return_value=_settings_dispatch("ctfd-desktops")),
        caplog.at_level(logging.WARNING, logger="_rd_plugin.docker_host_manager"),
    ):
        mgr.load_contexts([_stub_context_row("alpha")])

    mock_client.networks.list.assert_called_with(names=["ctfd-desktops"])

    warned = [r for r in caplog.records if "ctfd-desktops" in r.message and "missing" in r.message]
    assert warned, f"no missing-network warning logged: {[r.message for r in caplog.records]}"


def test_load_contexts_no_warning_when_network_present(caplog):
    from _rd_plugin.docker_host_manager import DockerHostManager

    mgr = DockerHostManager()
    mock_network = MagicMock()
    mock_network.name = "ctfd-desktops"
    mock_client = MagicMock()
    mock_client.networks.list.return_value = [mock_network]

    with (
        patch("_rd_plugin.docker_host_manager._resolve_endpoint", return_value="unix:///fake.sock"),
        patch("_rd_plugin.docker_host_manager.docker.DockerClient", return_value=mock_client),
        patch("models.get_all_settings", return_value=_settings_dispatch("ctfd-desktops")),
        caplog.at_level(logging.WARNING, logger="_rd_plugin.docker_host_manager"),
    ):
        mgr.load_contexts([_stub_context_row("alpha")])

    assert mgr.get_connected_contexts() == ["alpha"]
    warned = [r for r in caplog.records if "missing" in r.message]
    assert not warned, f"unexpected warning when network is present: {[r.message for r in warned]}"


def test_load_contexts_skips_probe_when_network_is_bridge(caplog):
    """The built-in bridge always exists, so it does not need a probe."""
    from _rd_plugin.docker_host_manager import DockerHostManager

    mgr = DockerHostManager()
    mock_client = MagicMock()

    with (
        patch("_rd_plugin.docker_host_manager._resolve_endpoint", return_value="unix:///fake.sock"),
        patch("_rd_plugin.docker_host_manager.docker.DockerClient", return_value=mock_client),
        patch("models.get_all_settings", return_value=_settings_dispatch("bridge")),
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
        patch("models.get_all_settings", return_value=_settings_dispatch("ctfd-desktops")),
        caplog.at_level(logging.WARNING, logger="_rd_plugin.docker_host_manager"),
    ):
        mgr.load_contexts([_stub_context_row("alpha")])

    warned = [r for r in caplog.records if "network check failed" in r.message]
    assert warned, f"expected probe-error warning, got: {[r.message for r in caplog.records]}"


def test_failed_startup_probe_retains_config_and_recovers_on_ping():
    """A transient load failure remains probeable by the periodic health job."""
    from _rd_plugin.docker_host_manager import DockerHostManager
    from _rd_plugin.docker_host_manager import docker

    mgr = DockerHostManager()
    startup_client = MagicMock()
    startup_client.ping.side_effect = docker.errors.DockerException("runner booting")

    with (
        patch("_rd_plugin.docker_host_manager._resolve_endpoint", return_value="ssh://root@alpha"),
        patch("_rd_plugin.docker_host_manager.docker.DockerClient", return_value=startup_client),
        patch("models.get_all_settings", return_value=_settings_dispatch("bridge")),
    ):
        mgr.load_contexts([_stub_context_row("alpha")])

    assert mgr._context_configs == {"alpha": "ssh://root@alpha"}
    assert mgr.get_connected_contexts() == []
    assert mgr.get_connection_hostnames("alpha") == ("alpha.example.com", "alpha.example.com")
    startup_client.close.assert_called_once_with()

    with patch("_rd_plugin.docker_host_manager.ping_endpoint", return_value=True) as probe:
        assert mgr.ping("alpha") is True

    probe.assert_called_once_with("ssh://root@alpha", timeout=3)
    assert mgr.get_connected_contexts() == ["alpha"]


def test_failed_ping_removes_only_matching_endpoint_from_connected_list():
    from _rd_plugin.docker_host_manager import DockerHostManager

    mgr = DockerHostManager()
    mgr._context_configs = {
        "alpha": "ssh://root@alpha",
        "beta": "ssh://root@beta",
    }
    mgr._connected_contexts = {"alpha", "beta"}

    with patch("_rd_plugin.docker_host_manager.ping_endpoint", return_value=False):
        assert mgr.ping("alpha") is False

    assert mgr.get_connected_contexts() == ["beta"]


def test_stale_ping_result_cannot_overwrite_reloaded_endpoint_state():
    from _rd_plugin.docker_host_manager import DockerHostManager

    mgr = DockerHostManager()
    mgr._context_configs = {"alpha": "ssh://root@old-alpha"}

    def probe_then_reload(_endpoint, timeout):
        assert timeout == 3
        with mgr._lock:
            mgr._context_configs["alpha"] = "ssh://root@new-alpha"
            mgr._connected_contexts.add("alpha")
        return False

    with patch("_rd_plugin.docker_host_manager.ping_endpoint", side_effect=probe_then_reload):
        assert mgr.ping("alpha") is False

    assert mgr.get_connected_contexts() == ["alpha"]
    assert mgr._context_client_epochs.get("alpha", 0) == 0


def test_ping_endpoint_closes_ephemeral_client_after_failure():
    from _rd_plugin.docker_host_manager import docker, ping_endpoint

    client = MagicMock()
    client.ping.side_effect = docker.errors.DockerException("connection lost")

    with patch("_rd_plugin.docker_host_manager.docker.DockerClient", return_value=client):
        assert ping_endpoint("ssh://root@alpha") is False

    client.close.assert_called_once_with()
