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
    }


def _invoke_missing_user(handler):
    with patch("routes.request") as req, patch("routes.Users") as users, patch(
        "routes.jsonify"
    ) as jsonify, patch("routes.event_logger") as logger, patch(
        "routes.get_current_user"
    ) as gcu:
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
