"""Context administration must not strand active or reserved sessions."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


def _get_handler(bp, name):
    for call in reversed(bp.route.return_value.call_args_list):
        fn = call.args[0]
        if getattr(fn, "__name__", None) == name:
            return fn
    raise LookupError(name)


def _context(**overrides):
    values = {
        "id": 4,
        "context_name": "runner-a",
        "hostname": "ssh://runner-a",
        "pub_hostname": "runner-a.example.edu",
        "weight": 1,
        "enabled": True,
        "max_containers": 4,
        "active_sessions": 0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _return_locked_context(contexts, context):
    fresh_query = contexts.query.filter_by.return_value.populate_existing.return_value
    fresh_query.with_for_update.return_value.first.return_value = context


def _return_no_live_work(rows, operations):
    rows.query.filter_by.return_value.first.return_value = None
    operations.query.filter_by.return_value.first.return_value = None


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("enabled", "false", "enabled must be a boolean"),
        ("weight", True, "weight must be an integer"),
        ("weight", 1.9, "weight must be an integer"),
        ("max_containers", False, "max_containers must be a non-negative integer or null"),
        ("max_containers", 1.9, "max_containers must be a non-negative integer or null"),
        ("max_containers", float("inf"), "max_containers must be a non-negative integer or null"),
    ],
)
def test_add_rejects_ambiguous_json_scalar_types(field, value, message):
    from routes import create_routes

    bp = create_routes(MagicMock(), MagicMock())
    handler = _get_handler(bp, "admin_add_context")
    payload = {
        "context_name": "runner-a",
        "pub_hostname": "runner-a.example.edu",
        "enabled": True,
        "weight": 1,
    }
    payload[field] = value

    with (
        patch("routes.request") as request,
        patch("routes.jsonify", side_effect=lambda payload: payload),
        patch("models.DesktopDockerContextModel") as contexts,
        patch("routes.db") as database,
    ):
        request.json = payload
        result = handler()

    assert result == ({"error": message}, 400)
    database.session.add.assert_not_called()
    contexts.query.filter_by.assert_not_called()


@pytest.mark.parametrize("hostname", [" runner-a", "runner a", "x" * 513, 42])
def test_add_rejects_invalid_or_oversized_hostname(hostname):
    from routes import create_routes

    bp = create_routes(MagicMock(), MagicMock())
    handler = _get_handler(bp, "admin_add_context")

    with (
        patch("routes.request") as request,
        patch("routes.jsonify", side_effect=lambda payload: payload),
        patch("models.DesktopDockerContextModel") as contexts,
    ):
        request.json = {
            "context_name": "runner-a",
            "hostname": hostname,
            "pub_hostname": "runner-a.example.edu",
        }
        result = handler()

    assert result == (
        {"error": "hostname must be a non-empty string of at most 512 characters without whitespace"},
        400,
    )
    contexts.query.filter_by.assert_not_called()


@pytest.mark.parametrize(
    "hostname",
    [
        "tcp://runner-a:2375",
        "root:secret@runner-a",
        "root@runner-a/path",
        "ssh://runner-a:",
        "ssh://runner-a:0",
        "ssh://@runner-a",
        "ssh://root@@runner-a",
    ],
)
def test_add_rejects_non_ssh_or_credential_bearing_endpoint(hostname):
    from routes import create_routes

    bp = create_routes(MagicMock(), MagicMock())
    handler = _get_handler(bp, "admin_add_context")

    with (
        patch("routes.request") as request,
        patch("routes.jsonify", side_effect=lambda payload: payload),
        patch("models.DesktopDockerContextModel") as contexts,
    ):
        request.json = {
            "context_name": "runner-a",
            "hostname": hostname,
            "pub_hostname": "runner-a.example.edu",
        }
        result = handler()

    assert result == (
        {"error": "hostname must be an SSH target such as root@runner.example or ssh://root@runner.example"},
        400,
    )
    contexts.query.filter_by.assert_not_called()


def test_update_rejects_string_false_instead_of_enabling_context():
    from routes import create_routes

    bp = create_routes(MagicMock(), MagicMock())
    handler = _get_handler(bp, "admin_update_context")

    with (
        patch("routes.request") as request,
        patch("routes.jsonify", side_effect=lambda payload: payload),
        patch("models.DesktopDockerContextModel") as contexts,
        patch("routes.db") as database,
    ):
        request.json = {"enabled": "false"}
        result = handler(4)

    assert result == ({"error": "enabled must be a boolean"}, 400)
    contexts.query.filter_by.assert_not_called()
    database.session.commit.assert_not_called()


def test_update_rejects_endpoint_change_while_counter_is_reserved():
    from routes import create_routes

    bp = create_routes(MagicMock(), MagicMock())
    handler = _get_handler(bp, "admin_update_context")

    with (
        patch("routes.request") as request,
        patch("routes.jsonify", side_effect=lambda payload: payload),
        patch("models.DesktopDockerContextModel") as contexts,
        patch("models.DesktopContainerInfoModel") as rows,
        patch("models.DesktopSessionOperationModel") as operations,
        patch("routes.db") as database,
    ):
        request.json = {"hostname": "ssh://replacement"}
        _return_locked_context(contexts, _context(active_sessions=1))
        result = handler(4)

    assert result == ({"error": "context has active or in-flight sessions; drain it before changing access"}, 409)
    rows.query.filter_by.assert_called_once_with(docker_context="runner-a")
    operations.query.filter_by.assert_called_once_with(docker_context="runner-a", capacity_reserved=True)
    database.session.commit.assert_not_called()
    assert database.session.rollback.call_count == 2
    contexts.query.filter_by.assert_called_once_with(id=4)
    contexts.query.filter_by.return_value.populate_existing.assert_called_once_with()
    contexts.query.filter_by.return_value.populate_existing.return_value.with_for_update.assert_called_once_with()


def test_delete_rejects_context_with_live_row_even_if_counter_is_stale():
    from routes import create_routes

    bp = create_routes(MagicMock(), MagicMock())
    handler = _get_handler(bp, "admin_delete_context")

    with (
        patch("routes.jsonify", side_effect=lambda payload: payload),
        patch("models.DesktopDockerContextModel") as contexts,
        patch("models.DesktopContainerInfoModel") as rows,
        patch("models.DesktopSessionOperationModel") as operations,
        patch("routes.db") as database,
    ):
        _return_locked_context(contexts, _context(active_sessions=0))
        rows.query.filter_by.return_value.first.return_value = object()
        result = handler(4)

    assert result == ({"error": "context has active or in-flight sessions; drain it before deletion"}, 409)
    operations.query.filter_by.assert_called_once_with(docker_context="runner-a", capacity_reserved=True)
    database.session.delete.assert_not_called()


def test_update_rejects_endpoint_change_without_drain_fence_even_when_empty():
    from routes import create_routes

    bp = create_routes(MagicMock(), MagicMock())
    handler = _get_handler(bp, "admin_update_context")

    with (
        patch("routes.request") as request,
        patch("routes.jsonify", side_effect=lambda payload: payload),
        patch("models.DesktopDockerContextModel") as contexts,
        patch("models.DesktopContainerInfoModel") as rows,
        patch("models.DesktopSessionOperationModel") as operations,
        patch("routes.db") as database,
    ):
        request.json = {"hostname": "ssh://replacement"}
        context = _context(active_sessions=0, enabled=True, max_containers=4)
        _return_locked_context(contexts, context)
        _return_no_live_work(rows, operations)
        result = handler(4)

    assert result == ({"error": "drain or disable the context before changing its endpoint"}, 409)
    rows.query.filter_by.assert_called_once_with(docker_context="runner-a")
    operations.query.filter_by.assert_called_once_with(docker_context="runner-a", capacity_reserved=True)
    database.session.commit.assert_not_called()
    assert context.hostname == "ssh://runner-a"


def test_update_treats_public_hostname_as_a_drained_endpoint_change():
    from routes import create_routes

    bp = create_routes(MagicMock(), MagicMock())
    handler = _get_handler(bp, "admin_update_context")

    with (
        patch("routes.request") as request,
        patch("routes.jsonify", side_effect=lambda payload: payload),
        patch("models.DesktopDockerContextModel") as contexts,
        patch("models.DesktopContainerInfoModel") as rows,
        patch("models.DesktopSessionOperationModel") as operations,
        patch("routes.db"),
    ):
        request.json = {"pub_hostname": "replacement.example.edu"}
        context = _context(active_sessions=0, enabled=True, max_containers=4)
        _return_locked_context(contexts, context)
        _return_no_live_work(rows, operations)
        result = handler(4)

    assert result == ({"error": "drain or disable the context before changing its endpoint"}, 409)
    assert context.pub_hostname == "runner-a.example.edu"


def test_update_cannot_change_endpoint_and_unfence_in_one_request():
    from routes import create_routes

    bp = create_routes(MagicMock(), MagicMock())
    handler = _get_handler(bp, "admin_update_context")

    with (
        patch("routes.request") as request,
        patch("routes.jsonify", side_effect=lambda payload: payload),
        patch("models.DesktopDockerContextModel") as contexts,
        patch("models.DesktopContainerInfoModel") as rows,
        patch("models.DesktopSessionOperationModel") as operations,
        patch("routes.db") as database,
    ):
        request.json = {"hostname": "ssh://replacement", "max_containers": 4}
        context = _context(active_sessions=0, enabled=True, max_containers=0)
        _return_locked_context(contexts, context)
        _return_no_live_work(rows, operations)
        result = handler(4)

    assert result == ({"error": "drain or disable the context before changing its endpoint"}, 409)
    assert context.hostname == "ssh://runner-a"
    assert context.max_containers == 0
    database.session.commit.assert_not_called()


def test_update_can_atomically_drain_and_change_endpoint_when_empty():
    from routes import create_routes

    orchestrator = MagicMock()
    bp = create_routes(MagicMock(), orchestrator)
    handler = _get_handler(bp, "admin_update_context")

    with (
        patch("routes.request") as request,
        patch("routes.jsonify", side_effect=lambda payload: payload),
        patch("models.DesktopDockerContextModel") as contexts,
        patch("models.DesktopContainerInfoModel") as rows,
        patch("models.DesktopSessionOperationModel") as operations,
        patch("routes.db") as database,
        patch("event_bus.publish") as publish,
    ):
        request.json = {"hostname": "ssh://replacement", "max_containers": 0}
        context = _context(active_sessions=0, enabled=True, max_containers=4)
        _return_locked_context(contexts, context)
        _return_no_live_work(rows, operations)
        result = handler(4)

    assert result == {"success": True}
    assert context.hostname == "ssh://replacement"
    assert context.max_containers == 0
    database.session.commit.assert_called_once_with()
    orchestrator.load_from_db.assert_called_once_with()
    publish.assert_called_once_with({"_control": "reload_contexts"})
    rows.query.filter_by.assert_called_once_with(docker_context="runner-a")
    operations.query.filter_by.assert_called_once_with(docker_context="runner-a", capacity_reserved=True)


def test_update_cannot_atomically_disable_and_change_endpoint_with_reservation():
    from routes import create_routes

    bp = create_routes(MagicMock(), MagicMock())
    handler = _get_handler(bp, "admin_update_context")

    with (
        patch("routes.request") as request,
        patch("routes.jsonify", side_effect=lambda payload: payload),
        patch("models.DesktopDockerContextModel") as contexts,
        patch("models.DesktopContainerInfoModel") as rows,
        patch("models.DesktopSessionOperationModel") as operations,
        patch("routes.db") as database,
    ):
        request.json = {"hostname": "ssh://replacement", "enabled": False}
        context = _context(active_sessions=0, enabled=True)
        _return_locked_context(contexts, context)
        rows.query.filter_by.return_value.first.return_value = None
        operations.query.filter_by.return_value.first.return_value = object()
        result = handler(4)

    assert result == ({"error": "context has active or in-flight sessions; drain it before changing access"}, 409)
    assert context.hostname == "ssh://runner-a"
    assert context.enabled is True
    database.session.commit.assert_not_called()


def test_delete_rejects_enabled_context_until_explicitly_drained():
    from routes import create_routes

    bp = create_routes(MagicMock(), MagicMock())
    handler = _get_handler(bp, "admin_delete_context")

    with (
        patch("routes.jsonify", side_effect=lambda payload: payload),
        patch("models.DesktopDockerContextModel") as contexts,
        patch("models.DesktopContainerInfoModel") as rows,
        patch("models.DesktopSessionOperationModel") as operations,
        patch("routes.db") as database,
    ):
        _return_locked_context(contexts, _context(enabled=True, max_containers=4))
        _return_no_live_work(rows, operations)
        result = handler(4)

    assert result == ({"error": "drain or disable the context before deletion"}, 409)
    database.session.delete.assert_not_called()


def test_delete_succeeds_after_zero_cap_drain_is_empty():
    from routes import create_routes

    orchestrator = MagicMock()
    bp = create_routes(MagicMock(), orchestrator)
    handler = _get_handler(bp, "admin_delete_context")
    context = _context(enabled=True, max_containers=0)

    with (
        patch("routes.jsonify", side_effect=lambda payload: payload),
        patch("models.DesktopDockerContextModel") as contexts,
        patch("models.DesktopContainerInfoModel") as rows,
        patch("models.DesktopSessionOperationModel") as operations,
        patch("routes.db") as database,
        patch("event_bus.publish") as publish,
    ):
        _return_locked_context(contexts, context)
        _return_no_live_work(rows, operations)
        result = handler(4)

    assert result == {"success": True}
    database.session.delete.assert_called_once_with(context)
    database.session.commit.assert_called_once_with()
    orchestrator.load_from_db.assert_called_once_with()
    publish.assert_called_once_with({"_control": "reload_contexts"})
