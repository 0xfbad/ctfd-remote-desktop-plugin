"""without revocation a minted cookie stays replayable in the session cache for PERMANENT_SESSION_LIFETIME"""

from types import SimpleNamespace
import sys
import types
from unittest.mock import patch, MagicMock

import pytest


def test_mint_session_cookie_returns_3_tuple():
    """the raw sid comes back with the signed cookie or destroy_container cannot revoke the cache entry"""
    from container_manager import _mint_session_cookie

    # login_user is imported at call time and runs against flask.session, stub both so no real app context is needed
    fake_session = MagicMock()
    fake_session.sid = "test-sid-1234"

    flask_stub = sys.modules["flask"]
    flask_stub.session = fake_session

    ctfd_security = types.ModuleType("CTFd.utils.security")
    ctfd_security_auth = types.ModuleType("CTFd.utils.security.auth")
    ctfd_security_auth.login_user = MagicMock()
    sys.modules["CTFd.utils.security"] = ctfd_security
    sys.modules["CTFd.utils.security.auth"] = ctfd_security_auth

    werkzeug_wrappers = types.ModuleType("werkzeug.wrappers")

    class FakeResponse:
        def __init__(self):
            self.headers = MagicMock()
            self.headers.getlist.return_value = ["session=signed-cookie-blob; HttpOnly"]

    werkzeug_wrappers.Response = FakeResponse
    sys.modules["werkzeug"] = types.ModuleType("werkzeug")
    sys.modules["werkzeug.wrappers"] = werkzeug_wrappers

    mock_app = MagicMock()
    mock_app.session_cookie_name = "session"

    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=ctx)
    ctx.__exit__ = MagicMock(return_value=False)
    mock_app.test_request_context.return_value = ctx

    mock_user = MagicMock()
    mock_user.id = 1

    result = _mint_session_cookie(mock_app, mock_user)

    assert result is not None
    assert len(result) == 3
    cookie_name, cookie_value, sid = result
    assert cookie_name == "session"
    assert cookie_value == "signed-cookie-blob"
    assert sid == "test-sid-1234"


def test_mint_session_cookie_revokes_sid_when_save_raises():
    from container_manager import _mint_session_cookie

    fake_session = MagicMock()
    fake_session.sid = "partial-save-sid"
    flask_stub = sys.modules["flask"]

    auth_module = types.ModuleType("CTFd.utils.security.auth")
    auth_module.login_user = MagicMock()
    response_module = types.ModuleType("werkzeug.wrappers")
    response_module.Response = MagicMock

    app = MagicMock()
    app.session_cookie_name = "session"
    app.test_request_context.return_value.__enter__.return_value = MagicMock()
    app.session_interface.save_session.side_effect = RuntimeError("redis write outcome unknown")

    with (
        patch.object(flask_stub, "session", fake_session, create=True),
        patch.dict(
            sys.modules,
            {
                "CTFd.utils.security.auth": auth_module,
                "werkzeug.wrappers": response_module,
            },
        ),
        patch("container_manager._revoke_session_cookie", return_value=True) as revoke,
        pytest.raises(RuntimeError, match="outcome unknown"),
    ):
        _mint_session_cookie(app, MagicMock(id=1))

    revoke.assert_called_once_with(app, "partial-save-sid")


def _make_row_with_sid(user_id=1, cookie_sid="abc-sid"):
    row = MagicMock()
    row.user_id = user_id
    row.docker_context = "ctx1"
    row.container_name = f"rd-session-{user_id}-1234"
    row.container_id = "cid-xyz"
    row.created_at = 1000.0
    row.extensions_used = 0
    row.cookie_sid = cookie_sid
    row.paused_at = None
    return row


def test_destroy_revokes_cookie_via_cache_delete(container_manager):
    cm = container_manager
    row = _make_row_with_sid(user_id=42, cookie_sid="raw-sid-42")

    mock_model = MagicMock()
    mock_model.query.filter_by.return_value.first.return_value = row

    mock_db = MagicMock()
    mock_users = MagicMock()
    mock_users.query.filter_by.return_value.first.return_value = SimpleNamespace(name="alice", id=1)
    mock_history_cls = MagicMock()

    mock_cache = MagicMock()
    ctfd_cache_mod = types.ModuleType("CTFd.cache")
    ctfd_cache_mod.cache = mock_cache

    mock_current_app = MagicMock()
    mock_current_app.session_interface.key_prefix = "session"

    flask_stub = sys.modules["flask"]
    original_current_app = getattr(flask_stub, "current_app", None)
    flask_stub.current_app = mock_current_app

    with (
        patch("container_manager.DesktopContainerInfoModel", mock_model),
        patch("models.DesktopSessionHistoryModel", mock_history_cls),
        patch("container_manager.db", mock_db),
        patch("container_manager.Users", mock_users),
        patch.dict(sys.modules, {"CTFd.cache": ctfd_cache_mod}),
        patch("models.get_setting", return_value=False),
    ):
        try:
            result = cm.destroy_container(42, reason="user_destroyed")
        finally:
            if original_current_app is not None:
                flask_stub.current_app = original_current_app

    assert result["success"]
    mock_cache.delete.assert_called_once_with("session" + "raw-sid-42")


def test_destroy_skips_revoke_when_cookie_sid_missing(container_manager):
    """a failed mint leaves cookie_sid null, teardown must still succeed"""
    cm = container_manager
    row = _make_row_with_sid(user_id=7, cookie_sid=None)

    mock_model = MagicMock()
    mock_model.query.filter_by.return_value.first.return_value = row

    mock_db = MagicMock()
    mock_users = MagicMock()
    mock_users.query.filter_by.return_value.first.return_value = MagicMock(name="bob")
    mock_history_cls = MagicMock()

    mock_cache = MagicMock()
    ctfd_cache_mod = types.ModuleType("CTFd.cache")
    ctfd_cache_mod.cache = mock_cache

    with (
        patch("container_manager.DesktopContainerInfoModel", mock_model),
        patch("models.DesktopSessionHistoryModel", mock_history_cls),
        patch("container_manager.db", mock_db),
        patch("container_manager.Users", mock_users),
        patch.dict(sys.modules, {"CTFd.cache": ctfd_cache_mod}),
        patch("models.get_setting", return_value=False),
    ):
        result = cm.destroy_container(7, reason="user_destroyed")

    assert result["success"]
    mock_cache.delete.assert_not_called()


def test_destroy_retains_revocation_handle_when_cache_errors(container_manager):
    cm = container_manager
    row = _make_row_with_sid(user_id=99, cookie_sid="sid-99")

    mock_model = MagicMock()
    mock_model.query.filter_by.return_value.first.return_value = row

    mock_db = MagicMock()
    mock_users = MagicMock()
    mock_users.query.filter_by.return_value.first.return_value = MagicMock(name="carol")
    mock_history_cls = MagicMock()

    mock_cache = MagicMock()
    mock_cache.delete.side_effect = RuntimeError("redis offline")
    ctfd_cache_mod = types.ModuleType("CTFd.cache")
    ctfd_cache_mod.cache = mock_cache

    mock_current_app = MagicMock()
    mock_current_app.session_interface.key_prefix = "session"

    flask_stub = sys.modules["flask"]
    original_current_app = getattr(flask_stub, "current_app", None)
    flask_stub.current_app = mock_current_app

    with (
        patch("container_manager.DesktopContainerInfoModel", mock_model),
        patch("models.DesktopSessionHistoryModel", mock_history_cls),
        patch("container_manager.db", mock_db),
        patch("container_manager.Users", mock_users),
        patch.dict(sys.modules, {"CTFd.cache": ctfd_cache_mod}),
        patch("models.get_setting", return_value=False),
    ):
        try:
            result = cm.destroy_container(99, reason="user_destroyed")
        finally:
            if original_current_app is not None:
                flask_stub.current_app = original_current_app

    # the active row and cookie_sid stay behind for a later cleanup retry once the cache backend recovers
    assert not result["success"]
    assert "revocation" in result["error"]
    assert row.lifecycle_state == "cleanup_pending"
    mock_history_cls.assert_not_called()
    mock_db.session.delete.assert_not_called()
