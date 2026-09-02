import sys
from unittest.mock import MagicMock, patch

import pytest

from routes import create_routes


def _get_handler(bp, name):
    # Blueprint is a shared MagicMock in the lightweight Flask test shim, so
    # select the registration from this fixture's most recent create_routes call.
    for c in reversed(bp.route.return_value.call_args_list):
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
        "_cm": cm,
        "kill": _get_handler(bp, "admin_kill_container"),
        "kill_all": _get_handler(bp, "admin_kill_all"),
        "peek": _get_handler(bp, "admin_peek_session"),
        "extend": _get_handler(bp, "admin_extend_session"),
        "clear_history": _get_handler(bp, "admin_clear_history"),
        "clear_reports": _get_handler(bp, "admin_clear_reports"),
        "get_paused_orphans": _get_handler(bp, "admin_get_paused_orphans"),
        "remove_paused_orphan": _get_handler(bp, "admin_remove_paused_orphan"),
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


@pytest.mark.parametrize("name", ["kill", "extend"])
def test_admin_action_returns_404_when_target_missing(handlers, name):
    (result, logger) = _invoke_missing_user(handlers[name])
    payload, status = result
    assert status == 404
    assert payload == {"error": "User not found"}
    logger.log_event.assert_not_called()


def test_admin_monitoring_endpoint_is_disabled_without_target_lookup_or_logging(handlers):
    with (
        patch("routes.Users") as users,
        patch("routes.jsonify", side_effect=lambda payload: payload),
        patch("routes.event_logger") as logger,
    ):
        payload, status = handlers["peek"]()

    assert status == 403
    assert payload == {"error": "Cross-user session monitoring is disabled"}
    users.query.filter_by.assert_not_called()
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
    _install_models_stub(DesktopSessionHistoryModel=sess_q)

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

        assert result == {"success": True, "sessions": 5}
        sess_q.delete.assert_called_once()
        db.session.commit.assert_called_once()
        logger.log_event.assert_called_once()


def test_get_paused_orphans_returns_manager_snapshot(handlers):
    orphan = {
        "context": "runner-a&b",
        "container_id": "a" * 64,
        "container_name": "rd-session-7-12345678-90a",
        "user_id": 7,
        "session_uuid": "12345678-90ab-4cde-8f01-234567890abc",
        "created_at": 1.0,
        "age_seconds": 2,
    }
    cm = handlers["_cm"]
    cm.list_paused_orphans.return_value = [orphan]
    with patch("routes.jsonify", side_effect=lambda payload: payload):
        assert handlers["get_paused_orphans"]() == {"orphans": [orphan]}


def test_remove_paused_orphan_rejects_invalid_identity_fields(handlers):
    with patch("routes.request") as req, patch("routes.jsonify", side_effect=lambda payload: payload):
        req.get_json.return_value = {
            "confirm": "DELETE",
            "context": "runner-a",
            "container_id": "",
            "container_name": "rd-session-7-12345678-90a",
        }
        payload, status = handlers["remove_paused_orphan"]()
    assert status == 400
    assert "non-empty strings" in payload["error"]
    handlers["_cm"].remove_paused_orphan_admin.assert_not_called()


def test_remove_paused_orphan_requires_confirmation(handlers):
    with patch("routes.request") as req, patch("routes.jsonify", side_effect=lambda payload: payload):
        req.get_json.return_value = {}
        payload, status = handlers["remove_paused_orphan"]()
    assert status == 400
    assert "confirmation" in payload["error"]


def test_remove_paused_orphan_forwards_exact_identity(handlers):
    payload = {
        "confirm": "DELETE",
        "context": "runner-a",
        "container_id": "a" * 64,
        "container_name": "rd-session-7-12345678-90a",
    }
    cm = handlers["_cm"]
    cm.remove_paused_orphan_admin.return_value = {"success": True}
    admin = MagicMock(id=1, name="admin")
    with (
        patch("routes.request") as req,
        patch("routes.jsonify", side_effect=lambda value: value),
        patch("routes.get_current_user", return_value=admin),
    ):
        req.get_json.return_value = payload
        assert handlers["remove_paused_orphan"]() == {"success": True}
    cm.remove_paused_orphan_admin.assert_called_once_with(
        admin,
        payload["context"],
        payload["container_id"],
        payload["container_name"],
    )


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


# -- settings validation (validate-then-apply) --------------------------------


def _get_latest_handler(bp, name):
    # the flask Blueprint stub is a shared MagicMock, so bp.route accumulates
    # registrations from every create_routes call in the session; the LAST
    # match is the one bound to the container_manager we just passed in
    for c in reversed(bp.route.return_value.call_args_list):
        fn = c.args[0]
        if getattr(fn, "__name__", None) == name:
            return fn
    raise LookupError(name)


@pytest.fixture()
def cm_handlers():
    """like handlers, but keeps the container_manager mock for call assertions"""
    cm = MagicMock()
    bp = create_routes(cm, MagicMock())
    return cm, {
        "settings_put": _get_latest_handler(bp, "admin_update_settings"),
        "pause": _get_latest_handler(bp, "admin_pause_session"),
        "unpause": _get_latest_handler(bp, "admin_unpause_session"),
    }


def _put_settings(handler, payload):
    with (
        patch("routes.request") as req,
        patch("routes.jsonify", side_effect=lambda p: p),
        patch("models.set_settings") as set_settings,
        patch("models.get_all_settings", return_value=dict(__import__("models").SETTING_DEFAULTS)),
    ):
        req.json = payload
        return handler(), set_settings


def test_settings_put_storage_limit_below_floor_rejected(cm_handlers):
    _cm, handlers = cm_handlers
    result, set_setting = _put_settings(handlers["settings_put"], {"storage_limit": "20m"})
    payload, status = result
    assert status == 400
    assert "storage_limit" in payload["error"]
    set_setting.assert_not_called()


def test_settings_put_storage_limit_20g_accepted(cm_handlers):
    _cm, handlers = cm_handlers
    result, set_setting = _put_settings(handlers["settings_put"], {"storage_limit": "20g"})
    assert result == {"success": True}
    set_setting.assert_called_once_with({"storage_limit": "20g"})


def test_settings_put_log_max_file_zero_rejected(cm_handlers):
    _cm, handlers = cm_handlers
    result, set_setting = _put_settings(handlers["settings_put"], {"log_max_file": 0})
    payload, status = result
    assert status == 400
    assert "log_max_file" in payload["error"]
    set_setting.assert_not_called()


def test_settings_put_multi_key_batch_is_all_or_nothing(cm_handlers):
    # one bad key must reject the WHOLE batch before any set_setting commit,
    # otherwise the per-key commits partially apply the update
    _cm, handlers = cm_handlers
    result, set_setting = _put_settings(handlers["settings_put"], {"storage_limit": "20m", "cpu_limit": 4})
    payload, status = result
    assert status == 400
    set_setting.assert_not_called()


def test_settings_put_cgroup_parent_must_be_slice(cm_handlers):
    _cm, handlers = cm_handlers
    result, set_setting = _put_settings(handlers["settings_put"], {"cgroup_parent": "notaslice"})
    payload, status = result
    assert status == 400
    assert "cgroup_parent" in payload["error"]
    set_setting.assert_not_called()


def test_settings_update_reloads_locally_and_publishes_cross_worker():
    orchestrator = MagicMock()
    bp = create_routes(MagicMock(), orchestrator)
    handler = _get_latest_handler(bp, "admin_update_settings")
    from models import SETTING_DEFAULTS

    with (
        patch("routes.request") as req,
        patch("routes.jsonify", side_effect=lambda payload: payload),
        patch("models.get_all_settings", return_value=dict(SETTING_DEFAULTS)),
        patch("models.set_settings"),
        patch("event_bus.publish") as publish,
    ):
        req.json = {"rd_network_name": "bridge"}
        assert handler() == {"success": True}
    orchestrator.load_from_db.assert_called_once()
    publish.assert_called_once_with({"_control": "reload_contexts"})


def test_manual_context_reload_publishes_cross_worker():
    orchestrator = MagicMock()
    bp = create_routes(MagicMock(), orchestrator)
    handler = _get_latest_handler(bp, "admin_reload_contexts")

    with (
        patch("routes.jsonify", side_effect=lambda payload: payload),
        patch("event_bus.publish") as publish,
    ):
        assert handler() == {"success": True}

    orchestrator.load_from_db.assert_called_once()
    publish.assert_called_once_with({"_control": "reload_contexts"})


def test_context_test_checks_configured_image_after_ping():
    cm = MagicMock()
    cm.host_manager.ping.return_value = True
    cm.host_manager.get_image_info.return_value = None
    bp = create_routes(cm, MagicMock())
    handler = _get_latest_handler(bp, "admin_test_context")
    context = MagicMock(context_name="alpha")
    model = MagicMock()
    model.query.get.return_value = context
    with (
        patch("routes.jsonify", side_effect=lambda payload: payload),
        patch("models.DesktopDockerContextModel", model),
        patch("models.get_setting", return_value="img:latest"),
    ):
        payload, status = handler(1)
    assert status == 503
    assert "not found" in payload["error"]
    cm.host_manager.get_image_info.assert_called_once_with("alpha", "img:latest")
    cm.host_manager.check_image.assert_not_called()


# -- pause / unpause ----------------------------------------------------------


def _invoke_pause(handler):
    with (
        patch("routes.request") as req,
        patch("routes.Users") as users,
        patch("routes.jsonify", side_effect=lambda p: p),
        patch("routes.event_logger") as logger,
        patch("routes.get_current_user") as gcu,
    ):
        req.form.get.return_value = 7
        users.query.filter_by.return_value.first.return_value = MagicMock()
        gcu.return_value = MagicMock(id=1, name="admin")
        return handler(), logger


def test_admin_pause_success_logs_audit_trail(cm_handlers):
    cm, handlers = cm_handlers
    cm.pause_session.return_value = {"success": True}

    result, logger = _invoke_pause(handlers["pause"])
    assert result == {"success": True}
    cm.pause_session.assert_called_once_with(7)

    types = [c.args[0] for c in logger.log_event.call_args_list]
    assert types == ["admin_action", "session_paused"]
    admin_call = logger.log_event.call_args_list[0]
    assert admin_call.kwargs["metadata"]["action"] == "pause"
    assert admin_call.kwargs["metadata"]["target_id"] == 7
    assert admin_call.kwargs["level"] == "warning"
    paused_call = logger.log_event.call_args_list[1]
    assert paused_call.kwargs["user_id"] == 7
    assert paused_call.kwargs["metadata"] == {"source": "admin"}


def test_admin_unpause_success_logs_audit_trail(cm_handlers):
    cm, handlers = cm_handlers
    cm.unpause_session.return_value = {"success": True}

    result, logger = _invoke_pause(handlers["unpause"])
    assert result == {"success": True}
    cm.unpause_session.assert_called_once_with(7)

    types = [c.args[0] for c in logger.log_event.call_args_list]
    assert types == ["admin_action", "session_unpaused"]
    assert logger.log_event.call_args_list[0].kwargs["metadata"]["action"] == "unpause"


def test_admin_pause_failure_returns_400_and_skips_audit_log(cm_handlers):
    cm, handlers = cm_handlers
    cm.pause_session.return_value = {"success": False, "error": "Session already paused"}

    result, logger = _invoke_pause(handlers["pause"])
    payload, status = result
    assert status == 400
    assert payload == {"error": "Session already paused"}
    # nothing was paused, so no admin_action / session_paused rows
    logger.log_event.assert_not_called()


# -- infra status classification ----------------------------------------------


def test_infra_status_capacity_message_is_503():
    from routes import _infra_status

    assert _infra_status("All servers are at capacity right now. Please try again in a few minutes.") == 503


def test_infra_status_generic_error_is_500():
    from routes import _infra_status

    assert _infra_status("boom") == 500
