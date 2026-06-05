import sys
from unittest.mock import MagicMock, patch

import pytest

from routes import create_routes


def _get_handler(bp, name):
    for c in bp.route.return_value.call_args_list:
        fn = c.args[0]
        if getattr(fn, "__name__", None) == name:
            return fn
    raise LookupError(name)


@pytest.fixture()
def handlers():
    cm = MagicMock()
    # extend short-circuits on falsy get_container_info, force truthy so the user lookup runs
    cm.get_container_info.return_value = {"some": "session"}
    bp = create_routes(cm, MagicMock())
    return {
        "kill": _get_handler(bp, "admin_kill_container"),
        "peek": _get_handler(bp, "admin_peek_session"),
        "extend": _get_handler(bp, "admin_extend_session"),
        "clear_history": _get_handler(bp, "admin_clear_history"),
        "clear_reports": _get_handler(bp, "admin_clear_reports"),
    }


def _invoke_missing_user(handler):
    with (
        patch("routes.request") as req,
        patch("routes.Users") as users,
        patch("routes.jsonify") as jsonify,
        patch("routes.event_logger") as logger,
        patch("routes.get_current_user") as gcu,
    ):
        req.form.get.return_value = 999
        users.query.filter_by.return_value.first.return_value = None
        jsonify.side_effect = lambda payload: payload
        gcu.return_value = MagicMock(id=1, name="admin")
        return handler(), logger


@pytest.mark.parametrize("name", ["kill", "peek", "extend"])
def test_admin_action_returns_404_when_target_missing(handlers, name):
    (result, logger) = _invoke_missing_user(handlers[name])
    payload, status = result
    assert status == 404
    assert payload == {"error": "User not found"}
    logger.log_event.assert_not_called()


@pytest.mark.parametrize("name", ["clear_history", "clear_reports"])
def test_clear_endpoint_missing_confirm_returns_400(handlers, name):
    with patch("routes.request") as req, patch("routes.jsonify") as jsonify:
        req.get_json.return_value = None
        jsonify.side_effect = lambda payload: payload
        payload, status = handlers[name]()
        assert status == 400
        assert "confirmation required" in payload["error"]


@pytest.mark.parametrize("name", ["clear_history", "clear_reports"])
def test_clear_endpoint_wrong_confirm_returns_400(handlers, name):
    with patch("routes.request") as req, patch("routes.jsonify") as jsonify:
        # case-sensitive: lowercase "delete" must be rejected
        req.get_json.return_value = {"confirm": "delete"}
        jsonify.side_effect = lambda payload: payload
        payload, status = handlers[name]()
        assert status == 400
        assert "confirmation required" in payload["error"]


def _install_models_stub(**queries):
    # the handlers do `from .models import ...` at call time, which under the test
    # package layout resolves to the `models` module already loaded by conftest
    mod = sys.modules["models"]
    for attr, q in queries.items():
        setattr(mod, attr, MagicMock(query=q))
    return mod


def test_clear_history_correct_confirm_proceeds(handlers):
    sess_q = MagicMock()
    sess_q.count.return_value = 5
    cmd_q = MagicMock()
    cmd_q.count.return_value = 7
    _install_models_stub(DesktopSessionHistoryModel=sess_q, CommandLogModel=cmd_q)

    with (
        patch("routes.request") as req,
        patch("routes.jsonify") as jsonify,
        patch("routes.event_logger") as logger,
        patch("routes.get_current_user") as gcu,
        patch("routes.db") as db,
    ):
        req.get_json.return_value = {"confirm": "DELETE"}
        jsonify.side_effect = lambda payload: payload
        gcu.return_value = MagicMock(id=1, name="admin")

        result = handlers["clear_history"]()

        assert result == {"success": True, "sessions": 5, "commands": 7}
        sess_q.delete.assert_called_once()
        cmd_q.delete.assert_called_once()
        db.session.commit.assert_called_once()
        logger.log_event.assert_called_once()


def test_clear_reports_correct_confirm_proceeds(handlers):
    rep_q = MagicMock()
    rep_q.count.return_value = 3
    _install_models_stub(DesktopReportModel=rep_q)

    with (
        patch("routes.request") as req,
        patch("routes.jsonify") as jsonify,
        patch("routes.event_logger") as logger,
        patch("routes.get_current_user") as gcu,
        patch("routes.db") as db,
    ):
        req.get_json.return_value = {"confirm": "DELETE"}
        jsonify.side_effect = lambda payload: payload
        gcu.return_value = MagicMock(id=1, name="admin")

        result = handlers["clear_reports"]()

        assert result == {"success": True, "reports": 3}
        rep_q.delete.assert_called_once()
        db.session.commit.assert_called_once()
        logger.log_event.assert_called_once()
