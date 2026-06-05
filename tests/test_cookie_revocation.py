"""T12 regression: minted CTFd session cookies must be revoked when the
container is destroyed. Without revocation the cookie stays live in the
server-side session cache until PERMANENT_SESSION_LIFETIME (~31 days), so a
leaked cookie is replayable far beyond the container's lifetime.

Coverage:
- _mint_session_cookie returns (cookie_name, signed_value, raw_sid)
- destroy_container revokes the cache entry using the stored sid
- destroy_container with no stored sid (legacy row) silently skips the delete
"""

import sys
import types
from unittest.mock import patch, MagicMock


def test_mint_session_cookie_returns_3_tuple():
    """_mint_session_cookie must return the raw sid alongside the signed cookie
    so destroy_container can revoke the corresponding cache entry."""
    from container_manager import _mint_session_cookie

    # the function imports CTFd.utils.security.auth.login_user at call time and
    # works against flask.session. We pre-stub both so the test doesn't need
    # the real CTFd app context
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

    # test_request_context returns a context manager
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


def _make_row_with_sid(user_id=1, cookie_sid="abc-sid"):
    row = MagicMock()
    row.user_id = user_id
    row.docker_context = "ctx1"
    row.container_name = f"rd-session-{user_id}-1234"
    row.container_id = "cid-xyz"
    row.created_at = 1000.0
    row.extensions_used = 0
    row.cookie_sid = cookie_sid
    return row


def test_destroy_revokes_cookie_via_cache_delete(container_manager):
    """destroy_container must call cache.delete with `key_prefix + sid` so
    the server-side session entry is purged."""
    cm = container_manager
    row = _make_row_with_sid(user_id=42, cookie_sid="raw-sid-42")

    mock_model = MagicMock()
    mock_model.query.filter_by.return_value.first.return_value = row

    mock_db = MagicMock()
    mock_users = MagicMock()
    mock_users.query.filter_by.return_value.first.return_value = MagicMock(name="alice")
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
        patch("container_manager.DesktopSessionHistoryModel", mock_history_cls),
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
    """Legacy rows from before the cookie_sid column existed have
    cookie_sid=None. destroy_container must NOT call cache.delete in that
    case (cache backend may not even be importable in some test harnesses)."""
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
        patch("container_manager.DesktopSessionHistoryModel", mock_history_cls),
        patch("container_manager.db", mock_db),
        patch("container_manager.Users", mock_users),
        patch.dict(sys.modules, {"CTFd.cache": ctfd_cache_mod}),
        patch("models.get_setting", return_value=False),
    ):
        result = cm.destroy_container(7, reason="user_destroyed")

    assert result["success"]
    mock_cache.delete.assert_not_called()


def test_destroy_swallows_cache_errors(container_manager):
    """If the cache backend itself errors (redis down, connection lost), the
    destroy path must still complete - the row delete and history insert are
    higher-priority than the cache revocation. This keeps container teardown
    resilient to redis outages."""
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
        patch("container_manager.DesktopSessionHistoryModel", mock_history_cls),
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

    # destroy still succeeds, history still recorded, row still deleted
    assert result["success"]
    mock_history_cls.assert_called_once()
    mock_db.session.delete.assert_called_once_with(row)
