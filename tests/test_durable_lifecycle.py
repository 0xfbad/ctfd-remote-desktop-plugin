from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from exceptions import HostsUnavailableException
from models import LIFECYCLE_ACTIVE, LIFECYCLE_CLEANUP_PENDING, OP_CLEANUP_PENDING, OP_HELD, OP_STOPPING


def _active_row():
    return SimpleNamespace(
        user_id=7,
        container_id="container-id",
        container_name="rd-session-7-0123456789ab",
        docker_context="runner-a",
        cookie_sid=None,
        paused_at=None,
        session_uuid="01234567-89ab-4def-8123-456789abcdef",
        lifecycle_state=LIFECYCLE_ACTIVE,
        lifecycle_reason=None,
        created_at=100.0,
        extensions_used=1,
    )


def _operation(row):
    return SimpleNamespace(
        user_id=row.user_id,
        session_uuid=row.session_uuid,
        operation_uuid="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        worker_lease_uuid=None,
        state="active",
        cancel_requested=False,
        docker_context=row.docker_context,
        container_name=row.container_name,
        capacity_reserved=True,
        requested_reason=None,
        updated_at=100.0,
        error=None,
    )


def test_destroy_unknown_remote_outcome_retains_authoritative_row(container_manager):
    cm = container_manager
    row = _active_row()
    operation = _operation(row)
    mock_db = MagicMock()
    cm.host_manager.stop_container.side_effect = HostsUnavailableException("runner unreachable")

    with (
        patch.object(cm, "_locked_operation", return_value=operation),
        patch.object(cm, "_locked_active_row", return_value=row),
        patch("container_manager.db", mock_db),
        patch("container_manager.history_from_row") as make_history,
        patch("container_manager.Users") as users,
    ):
        users.query.filter_by.return_value.first.return_value = SimpleNamespace(id=7, name="alice")
        result = cm.destroy_container(7)

    assert not result["success"]
    assert row.lifecycle_state == LIFECYCLE_CLEANUP_PENDING
    assert operation.state == OP_CLEANUP_PENDING
    assert operation.capacity_reserved is True
    make_history.assert_not_called()
    mock_db.session.delete.assert_not_called()
    cm.orchestrator.release_slot.assert_not_called()


def test_destroy_orders_remote_confirmation_before_history_and_release(container_manager):
    cm = container_manager
    row = _active_row()
    operation = _operation(row)
    mock_db = MagicMock()
    order = []
    history = SimpleNamespace(started_at=row.created_at, extensions_used=row.extensions_used)

    cm.host_manager.stop_container.side_effect = lambda *_args: order.append("docker-stop")
    mock_db.session.delete.side_effect = lambda _row: order.append("active-delete")

    def _history(*_args, **_kwargs):
        order.append("history")
        return history

    cm.orchestrator.release_active_slot_in_transaction.side_effect = lambda *_args: order.append("capacity-release")

    with (
        patch.object(cm, "_locked_operation", return_value=operation),
        patch.object(cm, "_locked_active_row", return_value=row),
        patch("container_manager.db", mock_db),
        patch("container_manager.history_from_row", side_effect=_history),
        patch("container_manager.Users") as users,
        patch("container_manager.event_logger"),
        patch("models.get_setting", return_value=False),
    ):
        users.query.filter_by.return_value.first.return_value = SimpleNamespace(id=7, name="alice")
        result = cm.destroy_container(7)

    assert result["success"]
    assert order == ["docker-stop", "capacity-release", "history", "active-delete"]
    cm.orchestrator.release_slot.assert_not_called()


def test_stale_worker_cannot_advance_replaced_creation_lease(container_manager):
    cm = container_manager
    operation = SimpleNamespace(
        session_uuid="current-session",
        worker_lease_uuid="current-worker",
        cancel_requested=False,
        state=OP_STOPPING,
    )
    mock_db = MagicMock()

    with patch.object(cm, "_locked_operation", return_value=operation), patch("container_manager.db", mock_db):
        updated = cm._update_operation(7, "old-session", "old-worker", "active")

    assert not updated
    assert operation.state == OP_STOPPING
    mock_db.session.commit.assert_not_called()


def test_stale_paused_creation_becomes_evidence_hold(container_manager):
    cm = container_manager
    operation = SimpleNamespace(
        user_id=7,
        session_uuid="01234567-89ab-4def-8123-456789abcdef",
        worker_lease_uuid="old-worker",
        state=OP_CLEANUP_PENDING,
        cancel_requested=True,
        updated_at=0.0,
        docker_context="runner-a",
        container_name="rd-session-7-0123456789ab",
        capacity_reserved=True,
        error=None,
    )
    operation_model = MagicMock()
    operation_model.updated_at.__le__.return_value = MagicMock()
    operation_model.query.filter.return_value.all.return_value = [operation]
    mock_db = MagicMock()
    cm.host_manager.list_session_containers_strict.return_value = [
        {"name": operation.container_name, "status": "paused", "created_ts": 1.0}
    ]

    with (
        patch("container_manager.DesktopSessionOperationModel", operation_model),
        patch.object(cm, "_locked_operation", return_value=operation),
        patch.object(cm, "_locked_active_row", return_value=None),
        patch("container_manager.db", mock_db),
        patch("container_manager.time") as mock_time,
    ):
        mock_time.time.return_value = 1000.0
        cm._recover_stale_operations()

    assert operation.state == OP_HELD
    assert operation.capacity_reserved is True
    cm.host_manager.force_remove_container.assert_not_called()
    cm.orchestrator.release_slot.assert_not_called()
