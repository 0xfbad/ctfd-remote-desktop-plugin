"""Connection toggles (ssh_enabled / web_terminal_enabled).

Ports published and ENABLE_* env must be built from the same one-shot settings
read so a mid-create toggle flip can never make them disagree; disabled ports
must land as NULL ssh_port/ttyd_port on the row; the settings, routes and
template surfaces must all know about both keys.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from container_manager import ContainerManager, _connection_ports
from docker_host_manager import SESSION_LABEL_MANAGED, SESSION_LABEL_USER_ID, SESSION_LABEL_UUID

TEMPLATES = Path(__file__).resolve().parent.parent / "src" / "templates"


# -- 1. pure helper matrix ---------------------------------------------------


@pytest.mark.parametrize(
    "ssh,ttyd,expected",
    [
        (True, True, ["6080/tcp", "22/tcp", "7682/tcp"]),
        (False, True, ["6080/tcp", "7682/tcp"]),
        (True, False, ["6080/tcp", "22/tcp"]),
        (False, False, ["6080/tcp"]),
    ],
)
def test_connection_ports_matrix(ssh, ttyd, expected):
    assert _connection_ports(ssh, ttyd) == expected


# -- 2. create-call serialization through _create_container_background -------


def _base_settings(**overrides):
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
    settings.update(overrides)
    return settings


def _run_background_create(settings, ports_return):
    """drive the real _create_container_background with a mocked host_manager;
    returns (run_container kwargs, patched DesktopContainerInfoModel mock)"""
    cm = ContainerManager(MagicMock(), MagicMock(), MagicMock())

    cm.host_manager.run_container.return_value = {
        "container_id": "abc",
        "container_name": "rd-session-1-1",
        "ports": ports_return,
    }
    cm.host_manager.exec_in_container.return_value = (0, "")
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
        patch("container_manager.DesktopContainerInfoModel") as model,
        patch("container_manager.db"),
        patch("container_manager.event_logger"),
    ):
        cm._create_container_background(user_id=1, container_url="http://ctfd", extra_hosts=None)

    cm.host_manager.run_container.assert_called_once()
    return cm.host_manager.run_container.call_args.kwargs, model


@pytest.mark.parametrize("ssh,ttyd", [(True, True), (False, True), (True, False), (False, False)])
def test_create_call_ports_env_and_row_agree(ssh, ttyd):
    expected_ports = _connection_ports(ssh, ttyd)
    # the daemon only maps the ports we asked to publish
    port_numbers = {"5900/tcp": 40001, "6080/tcp": 40002, "22/tcp": 40003, "7682/tcp": 40004}
    ports_return = {p: port_numbers[p] for p in expected_ports}

    settings = _base_settings(ssh_enabled=ssh, web_terminal_enabled=ttyd)
    kwargs, model = _run_background_create(settings, ports_return)

    assert kwargs["ports"] == expected_ports
    assert kwargs["env"]["ENABLE_SSH"] == ("1" if ssh else "0")
    assert kwargs["env"]["ENABLE_TTYD"] == ("1" if ttyd else "0")

    row_kwargs = model.call_args.kwargs
    assert row_kwargs["ssh_port"] == (port_numbers["22/tcp"] if ssh else None)
    assert row_kwargs["ttyd_port"] == (port_numbers["7682/tcp"] if ttyd else None)


def test_create_call_applies_immutable_orphan_ownership_labels():
    kwargs, model = _run_background_create(
        _base_settings(),
        {"5900/tcp": 40001, "6080/tcp": 40002, "22/tcp": 40003, "7682/tcp": 40004},
    )
    session_uuid = model.call_args.kwargs["session_uuid"]
    assert kwargs["labels"] == {
        SESSION_LABEL_MANAGED: "true",
        SESSION_LABEL_USER_ID: "1",
        SESSION_LABEL_UUID: session_uuid,
    }


# -- 3. settings -------------------------------------------------------------


def test_setting_defaults_have_both_toggles_on():
    from models import SETTING_DEFAULTS

    assert SETTING_DEFAULTS["ssh_enabled"] is True
    assert SETTING_DEFAULTS["web_terminal_enabled"] is True


# -- 4. routes ---------------------------------------------------------------


def _get_handler(bp, name):
    # the flask Blueprint stub is a shared MagicMock: bp.route accumulates
    # registrations from every create_routes call in the whole session, so take
    # the LAST match - that is the closure bound to the mocks we just passed in
    for c in reversed(bp.route.return_value.call_args_list):
        fn = c.args[0]
        if getattr(fn, "__name__", None) == name:
            return fn
    raise LookupError(name)


def test_admin_update_settings_persists_ssh_toggle():
    from routes import create_routes

    bp = create_routes(MagicMock(), MagicMock())
    handler = _get_handler(bp, "admin_update_settings")

    with (
        patch("routes.request") as req,
        patch("routes.jsonify", side_effect=lambda payload: payload),
        patch("models.set_settings") as set_settings,
        patch("models.get_all_settings", return_value=dict(__import__("models").SETTING_DEFAULTS)),
    ):
        req.json = {"ssh_enabled": False}
        result = handler()

    assert result == {"success": True}
    set_settings.assert_called_once_with({"ssh_enabled": False})


def test_terminal_auth_404_when_ttyd_port_null():
    from routes import create_routes

    cm = MagicMock()
    bp = create_routes(cm, MagicMock())
    handler = _get_handler(bp, "terminal_auth")

    row = MagicMock()
    row.ttyd_port = None

    with (
        patch("routes.request") as req,
        patch("routes.get_current_user") as gcu,
        patch("models.DesktopContainerInfoModel") as model,
    ):
        req.headers.get.return_value = "1"
        gcu.return_value = MagicMock(id=1)
        model.query.filter_by.return_value.first.return_value = row
        result = handler()

    assert result == ("", 404)
    # the 404 must fire before any host lookup: db-only auth path
    cm.host_manager.get_check_hostname.assert_not_called()


def test_terminal_auth_uses_internal_context_host_and_injects_upstream_basic_auth():
    from types import SimpleNamespace

    from routes import create_routes

    class FakeResponse:
        def __init__(self, _body, _status):
            self.headers = {}

    cm = MagicMock()
    cm.host_manager.get_check_hostname.return_value = "172.18.0.1"
    bp = create_routes(cm, MagicMock())
    handler = _get_handler(bp, "terminal_auth")
    row = SimpleNamespace(
        ttyd_port=40003,
        lifecycle_state="active",
        docker_context="alpha",
        pub_hostname="RUNNER.Example.EDU",
        container_username="student_root",
        vnc_password="ttydpw1",
    )

    with (
        patch("routes.request") as req,
        patch("routes.get_current_user", return_value=MagicMock(id=7)),
        patch("routes.Response", FakeResponse),
        patch("models.DesktopContainerInfoModel") as model,
    ):
        req.headers.get.return_value = "7"
        model.query.filter_by.return_value.first.return_value = row
        result = handler()

    assert result.headers == {
        "X-Terminal-Host": "172.18.0.1",
        "X-Terminal-Port": "40003",
        "X-Terminal-Authorization": "Basic c3R1ZGVudF9yb290OnR0eWRwdzE=",
    }
    cm.host_manager.get_check_hostname.assert_called_once_with("alpha")


@pytest.mark.parametrize("handler_name", ["vnc_auth", "terminal_auth"])
def test_proxy_auth_denies_cross_user_admin_before_backend_lookup(handler_name):
    from routes import create_routes

    cm = MagicMock()
    handler = _get_handler(create_routes(cm, MagicMock()), handler_name)

    with (
        patch("routes.request") as req,
        patch("routes.get_current_user", return_value=MagicMock(id=1)),
        patch("routes.is_admin", return_value=True),
        patch("models.DesktopContainerInfoModel") as model,
    ):
        req.headers.get.return_value = "7"
        assert handler() == ("", 403)

    model.query.filter_by.assert_not_called()
    cm.host_manager.get_check_hostname.assert_not_called()


def test_vnc_proxy_auth_preserves_owner_access():
    from types import SimpleNamespace

    from routes import create_routes

    class FakeResponse:
        def __init__(self, _body, _status):
            self.headers = {}

    cm = MagicMock()
    cm.host_manager.get_check_hostname.return_value = "172.18.0.1"
    handler = _get_handler(create_routes(cm, MagicMock()), "vnc_auth")
    row = SimpleNamespace(
        novnc_port=40002,
        lifecycle_state="active",
        docker_context="alpha",
    )

    with (
        patch("routes.request") as req,
        patch("routes.get_current_user", return_value=MagicMock(id=7)),
        patch("routes.Response", FakeResponse),
        patch("models.DesktopContainerInfoModel") as model,
    ):
        req.headers.get.return_value = "7"
        model.query.filter_by.return_value.first.return_value = row
        result = handler()

    assert result.headers == {"X-VNC-Host": "172.18.0.1", "X-VNC-Port": "40002"}


def test_remote_desktop_page_passes_toggle_kwargs_to_template():
    from routes import create_routes

    cm = MagicMock()
    cm.get_container_info.return_value = None
    cm.get_creation_status.return_value = None
    bp = create_routes(cm, MagicMock())
    handler = _get_handler(bp, "remote_desktop_page")

    settings = {
        "remote_desktop_enabled": True,
        "require_verified": False,
        "max_extensions": 3,
        "ssh_enabled": True,
        "web_terminal_enabled": False,
    }

    with (
        patch("routes.render_template") as render,
        patch("routes.get_current_user") as gcu,
        patch("models.get_setting", side_effect=lambda k, default=None: settings.get(k, default)),
    ):
        gcu.return_value = MagicMock(id=1)
        handler()

    render.assert_called_once()
    kwargs = render.call_args.kwargs
    assert kwargs["ssh_enabled"] is True
    assert kwargs["web_terminal_enabled"] is False


def test_page_uses_proxy_only_urls_without_forwarded_headers():
    from routes import create_routes

    cm = MagicMock()
    cm.get_creation_status.return_value = None
    cm.get_container_info.return_value = {
        "container_id": "cid",
        "container_name": "rd-session-7",
        "vnc_port": 40001,
        "novnc_port": 40002,
        "ttyd_port": 40003,
        "ssh_port": None,
        "docker_context": "alpha",
        "created_at": 1.0,
        "vnc_password": "secret",
        "vnc_url": "/remote-desktop/vnc/7/vnc.html#password=secret",
    }
    bp = create_routes(cm, MagicMock())
    handler = _get_handler(bp, "remote_desktop_page")
    settings = {
        "remote_desktop_enabled": True,
        "require_verified": False,
        "max_extensions": 3,
        "ssh_enabled": False,
        "web_terminal_enabled": True,
    }
    with (
        patch("routes.get_current_user", return_value=MagicMock(id=7)),
        patch("routes.render_template") as render,
        patch("models.get_setting", side_effect=lambda key, default=None: settings.get(key, default)),
    ):
        handler()
    kwargs = render.call_args.kwargs
    assert kwargs["vnc_url"].startswith("/remote-desktop/vnc/7/")
    assert kwargs["terminal_url"] == "/remote-desktop/terminal/7/"
    assert "http://" not in kwargs["vnc_url"]


def test_page_formats_bracketed_ipv6_for_openssh():
    from routes import create_routes

    cm = MagicMock()
    cm.get_creation_status.return_value = None
    cm.get_container_info.return_value = {
        "container_id": "cid",
        "container_name": "rd-session-7",
        "vnc_port": 40001,
        "novnc_port": 40002,
        "ttyd_port": None,
        "ssh_port": 40022,
        "docker_context": "alpha",
        "pub_hostname": "[2001:db8::1]",
        "container_username": "student_root",
        "vnc_password": "secret",
        "created_at": 1.0,
        "vnc_url": "/remote-desktop/vnc/7/vnc.html#password=secret",
    }
    bp = create_routes(cm, MagicMock())
    handler = _get_handler(bp, "remote_desktop_page")
    settings = {
        "remote_desktop_enabled": True,
        "require_verified": False,
        "max_extensions": 3,
        "ssh_enabled": True,
        "web_terminal_enabled": False,
    }
    with (
        patch("routes.get_current_user", return_value=MagicMock(id=7)),
        patch("routes.render_template") as render,
        patch("models.get_setting", side_effect=lambda key, default=None: settings.get(key, default)),
    ):
        handler()

    assert render.call_args.kwargs["ssh_info"]["host"] == "2001:db8::1"


# -- 5. template plain-text guards -------------------------------------------


def test_session_page_hardcoded_modes_line_removed():
    text = (TEMPLATES / "remote_desktop.html").read_text()
    assert "Desktop, Terminal, and SSH modes available" not in text


def test_config_page_has_toggle_checkboxes_and_setting_keys():
    text = (TEMPLATES / "remote_desktop_config.html").read_text()
    assert "rd-setting-ssh_enabled" in text
    assert "rd-setting-web_terminal_enabled" in text

    # the keys must also be in rdSettingKeys or the checkboxes render but never persist
    start = text.index("rdSettingKeys = [")
    end = text.index("]", start)
    keys_src = text[start:end]
    assert "'ssh_enabled'" in keys_src
    assert "'web_terminal_enabled'" in keys_src


def test_admin_dashboard_has_no_cross_user_monitor_action():
    text = (TEMPLATES / "remote_desktop_dashboard.html").read_text()
    assert "peekVNC" not in text
    assert "/remote-desktop/dashboard/api/peek" not in text
