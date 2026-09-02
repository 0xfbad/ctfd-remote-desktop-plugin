"""Feature 3: fair-share governance kwargs in run_container.

Five knobs, all validated before any docker call: memswap_limit is
unconditional whenever memory is set (memory.swap.max=0), the rest are
settings-gated where ""/0 must OMIT the kwarg entirely (not pass None -
docker-py serializes an explicit None differently from absent for some
HostConfig fields, and absence is the documented rollback contract).
"""

import pytest
from unittest.mock import patch, MagicMock

import docker

from _rd_plugin.docker_host_manager import parse_size

GIB = 1024**3


def _make_manager():
    from _rd_plugin.docker_host_manager import DockerHostManager

    mgr = DockerHostManager()
    mgr._context_configs = {"alpha": "unix:///fake.sock"}
    mgr._config_generation = 1
    return mgr


# full plugin defaults for the governance keys (mirrors SETTING_DEFAULTS)
DEFAULT_SETTINGS = {
    "pids_limit": 4096,
    "cap_drop": "ALL",
    "cap_add": "",
    "storage_limit": "",
    "log_max_size": "",
    "log_max_file": 3,
    "memory_reservation": "1g",
    "swap_limit": "",
    "oom_score_adj": 500,
    "nofile_soft": 1024,
    "nofile_hard": 1048576,
    "cgroup_parent": "rd.slice",
}


def _run_with_settings(mgr, settings_overrides=None, **call_overrides):
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

    from settings import SETTING_DEFAULTS

    settings = dict(SETTING_DEFAULTS)
    settings.update(DEFAULT_SETTINGS)
    settings.update(settings_overrides or {})

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
    return mock_client.containers.run.call_args.kwargs


def _run_expect_valueerror(mgr, settings_overrides, **call_overrides):
    """run_container must raise ValueError before touching the client"""
    mock_client = MagicMock()
    from settings import SETTING_DEFAULTS

    settings = dict(SETTING_DEFAULTS)
    settings.update(DEFAULT_SETTINGS)
    settings.update(settings_overrides)

    with patch.object(mgr, "_get_client", return_value=mock_client):
        with patch("models.get_all_settings", return_value=settings):
            kwargs = dict(
                context_name="alpha",
                image="img:latest",
                name="rd-session-1-1700000000",
                env={},
                ports=["5900/tcp", "6080/tcp"],
            )
            kwargs.update(call_overrides)
            with pytest.raises(ValueError):
                mgr.run_container(**kwargs)

    mock_client.containers.run.assert_not_called()


def test_full_defaults_serialize_all_governance_kwargs():
    mgr = _make_manager()
    memory = parse_size("4g")
    kwargs = _run_with_settings(mgr, memory=memory)

    # default swap cushion equal to the RAM limit: memswap_limit = 2 * memory
    assert kwargs["memswap_limit"] == 2 * memory
    assert kwargs["mem_reservation"] == 1073741824  # 1g
    assert kwargs["oom_score_adj"] == 500
    assert kwargs["cgroup_parent"] == "rd.slice"
    assert kwargs["ulimits"] == [docker.types.Ulimit(name="nofile", soft=1024, hard=1048576)]


def test_cgroup_parent_empty_omits_kwarg():
    mgr = _make_manager()
    kwargs = _run_with_settings(mgr, {"cgroup_parent": ""}, memory=parse_size("4g"))
    assert "cgroup_parent" not in kwargs


def test_oom_score_adj_zero_omits_kwarg():
    mgr = _make_manager()
    kwargs = _run_with_settings(mgr, {"oom_score_adj": 0}, memory=parse_size("4g"))
    assert "oom_score_adj" not in kwargs


@pytest.mark.parametrize("raw", ["", "0"])
def test_memory_reservation_empty_or_zero_omits_kwarg(raw):
    mgr = _make_manager()
    kwargs = _run_with_settings(mgr, {"memory_reservation": raw}, memory=parse_size("4g"))
    assert "mem_reservation" not in kwargs


def test_nofile_soft_zero_omits_ulimits():
    mgr = _make_manager()
    kwargs = _run_with_settings(mgr, {"nofile_soft": 0}, memory=parse_size("4g"))
    assert "ulimits" not in kwargs


def test_cgroup_parent_without_slice_suffix_raises_before_client():
    """systemd cgroup driver rejects non-.slice parents at create; validate
    plugin-side so the error is loud and precedes any docker call"""
    mgr = _make_manager()
    _run_expect_valueerror(mgr, {"cgroup_parent": "rd"}, memory=parse_size("4g"))


def test_nofile_hard_below_soft_raises():
    mgr = _make_manager()
    _run_expect_valueerror(mgr, {"nofile_soft": 1024, "nofile_hard": 512}, memory=parse_size("4g"))


def test_memory_reservation_above_memory_raises():
    mgr = _make_manager()
    _run_expect_valueerror(mgr, {"memory_reservation": "8g"}, memory=parse_size("4g"))


def test_oom_score_adj_clamps_to_1000():
    mgr = _make_manager()
    kwargs = _run_with_settings(mgr, {"oom_score_adj": 5000}, memory=parse_size("4g"))
    assert kwargs["oom_score_adj"] == 1000


def test_swap_limit_zero_hard_disables_swap():
    mgr = _make_manager()
    memory = parse_size("4g")
    kwargs = _run_with_settings(mgr, {"swap_limit": "0"}, memory=memory)
    assert kwargs["memswap_limit"] == memory  # memory.swap.max = 0


def test_swap_limit_unlimited():
    mgr = _make_manager()
    kwargs = _run_with_settings(mgr, {"swap_limit": "-1"}, memory=parse_size("4g"))
    assert kwargs["memswap_limit"] == -1


def test_swap_limit_explicit_size_adds_to_memory():
    mgr = _make_manager()
    memory = parse_size("4g")
    kwargs = _run_with_settings(mgr, {"swap_limit": "2g"}, memory=memory)
    assert kwargs["memswap_limit"] == memory + parse_size("2g")


def test_memory_none_omits_memswap_limit():
    # memory_reservation cleared too: the reservation-vs-memory guard is
    # skipped when memory is None, keep this test about memswap only
    mgr = _make_manager()
    kwargs = _run_with_settings(mgr, {"memory_reservation": ""}, memory=None)
    assert "memswap_limit" not in kwargs
