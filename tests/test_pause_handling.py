"""Feature 1/4: pause = evidence hold, enforced plugin-side.

docker stop/kill/rm -f all SUCCEED against paused containers (verified on
29.7.2), so nothing daemon-side protects a held writable layer. The plugin
must therefore: count paused as alive, refuse user destroy on paused rows,
    skip expiry-destroy at every sweep site, preserve holds across restarts,
    and credit frozen time back to the timer on explicit admin unpause (expiry
is skipped while paused - without the credit the next sweep re-creates the
exact evidence loss the hold exists to prevent).
"""

from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import pytest
import docker

from models import (
    END_REASON_ADMIN_KILLED,
    END_REASON_EXPIRED,
    END_REASON_RECONCILIATION,
    END_REASON_USER_DESTROYED,
    LIFECYCLE_ACTIVE,
    LIFECYCLE_CLEANUP_PENDING,
    LIFECYCLE_HELD,
    LIFECYCLE_UNPAUSING,
    OP_UNPAUSING,
)


def _row(**overrides):
    row = MagicMock()
    row.user_id = 7
    row.docker_context = "ctx1"
    row.container_name = "rd-session-7-1700000000"
    row.container_id = "cid-7"
    row.created_at = 1000.0
    row.extensions_used = 0
    row.cookie_sid = None
    row.paused_at = None
    for k, v in overrides.items():
        setattr(row, k, v)
    return row


def _make_host_manager():
    from docker_host_manager import DockerHostManager

    mgr = DockerHostManager()
    mgr._context_configs = {"ctx1": "unix:///fake.sock"}
    mgr._config_generation = 1
    return mgr


# ---------------------------------------------------------------------------
# docker_host_manager.is_container_running: paused counts as alive
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status,expected",
    [("paused", True), ("running", True), ("exited", False)],
)
def test_is_container_running_treats_paused_as_alive(status, expected):
    mgr = _make_host_manager()
    mock_client = MagicMock()
    mock_client.containers.get.return_value = MagicMock(status=status)

    with patch.object(mgr, "_get_client", return_value=mock_client):
        assert mgr.is_container_running("ctx1", "cid-7") is expected


@pytest.mark.parametrize(
    "status,expected",
    [
        ("running", "running"),
        ("paused", "paused"),
        ("created", "created"),
        ("exited", "exited"),
        ("dead", "exited"),
        ("restarting", "unknown"),
        (None, "unknown"),
    ],
)
def test_inspect_container_state_is_closed_and_typed(status, expected):
    mgr = _make_host_manager()
    mock_client = MagicMock()
    mock_client.containers.get.return_value = MagicMock(status=status)

    with patch.object(mgr, "_get_client", return_value=mock_client):
        assert mgr.inspect_container_state("ctx1", "cid-7") == expected


def test_inspect_container_state_distinguishes_not_found_from_unknown():
    mgr = _make_host_manager()
    mock_client = MagicMock()

    with patch.object(mgr, "_get_client", return_value=mock_client):
        mock_client.containers.get.side_effect = docker.errors.NotFound("gone")
        assert mgr.inspect_container_state("ctx1", "cid-7") == "not_found"

        mock_client.containers.get.side_effect = OSError("ssh transport failed")
        assert mgr.inspect_container_state("ctx1", "cid-7") == "unknown"


@pytest.mark.parametrize("method_name", ["pause_container", "unpause_container"])
def test_pause_controls_propagate_not_found(method_name):
    mgr = _make_host_manager()
    mock_client = MagicMock()
    mock_client.containers.get.side_effect = docker.errors.NotFound("gone")

    with patch.object(mgr, "_get_client", return_value=mock_client):
        with pytest.raises(docker.errors.NotFound):
            getattr(mgr, method_name)("ctx1", "rd-session-7")


# ---------------------------------------------------------------------------
# destroy_container: refusal for the user, force_remove for the admin
# ---------------------------------------------------------------------------


def test_destroy_paused_row_refused_for_user(container_manager):
    cm = container_manager
    row = _row(paused_at=123.0)

    mock_model = MagicMock()
    mock_model.query.filter_by.return_value.first.return_value = row

    mock_db = MagicMock()
    mock_users = MagicMock()
    mock_users.query.filter_by.return_value.first.return_value = SimpleNamespace(name="alice", id=7)

    with (
        patch("container_manager.DesktopContainerInfoModel", mock_model),
        patch("container_manager.db", mock_db),
        patch("container_manager.Users", mock_users),
    ):
        result = cm.destroy_container(7)  # default reason = END_REASON_USER_DESTROYED

    assert result["success"] is False
    assert "suspended" in result["error"]
    cm.host_manager.stop_container.assert_not_called()
    cm.host_manager.force_remove_container.assert_not_called()
    mock_db.session.delete.assert_not_called()


def test_destroy_paused_row_admin_kill_uses_force_remove(container_manager):
    cm = container_manager
    row = _row(paused_at=123.0)
    # Explicit admin override is allowed even when the fresh state probe fails.
    cm.host_manager.inspect_container_state.return_value = "unknown"

    mock_model = MagicMock()
    mock_model.query.filter_by.return_value.first.return_value = row

    mock_db = MagicMock()
    mock_users = MagicMock()
    mock_users.query.filter_by.return_value.first.return_value = SimpleNamespace(name="alice", id=7)

    with (
        patch("container_manager.DesktopContainerInfoModel", mock_model),
        patch("models.DesktopSessionHistoryModel", MagicMock()),
        patch("container_manager.db", mock_db),
        patch("container_manager.Users", mock_users),
        patch("container_manager.event_logger"),
        patch("models.get_setting", return_value=False),
    ):
        result = cm.destroy_container(7, reason=END_REASON_ADMIN_KILLED)

    assert result["success"] is True
    # frozen container: stop would block the full timeout before SIGKILL,
    # force_remove is immediate
    cm.host_manager.force_remove_container.assert_called_once_with("ctx1", str(row.container_id))
    cm.host_manager.stop_container.assert_not_called()
    mock_db.session.delete.assert_called_once_with(row)


@pytest.mark.parametrize("reason", [END_REASON_USER_DESTROYED, END_REASON_EXPIRED, END_REASON_RECONCILIATION])
def test_destroy_detects_out_of_band_pause_and_mirrors_hold(container_manager, reason):
    cm = container_manager
    row = _row(paused_at=None, lifecycle_state=LIFECYCLE_ACTIVE)
    cm.host_manager.inspect_container_state.return_value = "paused"

    mock_model = MagicMock()
    mock_model.query.filter_by.return_value.first.return_value = row
    mock_users = MagicMock()
    mock_users.query.filter_by.return_value.first.return_value = SimpleNamespace(name="alice", id=7)

    with (
        patch("container_manager.DesktopContainerInfoModel", mock_model),
        patch("container_manager.Users", mock_users),
        patch("container_manager.db") as mock_db,
        patch.object(cm, "_locked_operation", return_value=None),
        patch("container_manager.time") as mock_time,
    ):
        mock_time.time.return_value = 2000.0
        result = cm.destroy_container(7, reason=reason)

    assert result["success"] is False
    assert row.paused_at == 2000.0
    assert row.lifecycle_state == LIFECYCLE_HELD
    mock_db.session.commit.assert_called()
    cm.host_manager.stop_container.assert_not_called()
    cm.host_manager.force_remove_container.assert_not_called()


@pytest.mark.parametrize("reason", [END_REASON_USER_DESTROYED, END_REASON_EXPIRED, END_REASON_RECONCILIATION])
@pytest.mark.parametrize("lifecycle_state", [LIFECYCLE_ACTIVE, LIFECYCLE_CLEANUP_PENDING])
def test_destroy_unknown_state_fails_closed_before_logs_or_stop(container_manager, reason, lifecycle_state):
    cm = container_manager
    row = _row(paused_at=None, lifecycle_state=lifecycle_state)
    cm.host_manager.inspect_container_state.return_value = "unknown"

    mock_model = MagicMock()
    mock_model.query.filter_by.return_value.first.return_value = row
    mock_users = MagicMock()
    mock_users.query.filter_by.return_value.first.return_value = SimpleNamespace(name="alice", id=7)

    with (
        patch("container_manager.DesktopContainerInfoModel", mock_model),
        patch("container_manager.Users", mock_users),
        patch("container_manager.db") as mock_db,
        patch.object(cm, "_locked_operation", return_value=None),
    ):
        result = cm.destroy_container(7, reason=reason)

    assert result == {"success": False, "error": "Container state is unknown; refusing destructive cleanup"}
    assert row.lifecycle_state == lifecycle_state
    mock_db.session.delete.assert_not_called()
    cm.host_manager.stop_container.assert_not_called()
    cm.host_manager.force_remove_container.assert_not_called()


# ---------------------------------------------------------------------------
# expiry sweeps must skip paused rows (all three sites)
# ---------------------------------------------------------------------------


def test_get_container_info_expired_paused_row_not_destroyed(container_manager):
    cm = container_manager
    row = _row(
        paused_at=500.0,
        timer_started=True,
        timer_start_time=1000.0,
        timer_duration=600,
        vnc_port=5900,
        novnc_port=6080,
        ssh_port=2222,
        ttyd_port=7682,
        pub_hostname="host1",
        container_username="alice",
        vnc_password="secret",
        vnc_url="/vnc",
    )

    mock_model = MagicMock()
    mock_model.query.filter_by.return_value.first.return_value = row

    with (
        patch("container_manager.DesktopContainerInfoModel", mock_model),
        patch("container_manager.time") as mock_time,
        patch.object(cm, "destroy_container") as mock_destroy,
        patch.object(cm, "_verify_or_reap", return_value=True),
    ):
        mock_time.time.return_value = 1700.0  # 700s elapsed > 600s duration
        info = cm.get_container_info(7)

    mock_destroy.assert_not_called()
    # Evidence holds remain retained but are not exposed through user read or
    # proxy paths until an administrator explicitly resumes them.
    assert info is None


def test_periodic_cleanup_skips_expired_paused_row(container_manager):
    cm = container_manager
    row = _row(
        paused_at=555.0,
        timer_started=True,
        timer_start_time=1000.0,
        timer_duration=600,
    )

    mock_model = MagicMock()
    mock_model.query.with_entities.return_value.all.return_value = []
    mock_model.query.filter_by.return_value.all.return_value = [row]

    with (
        patch("container_manager.DesktopContainerInfoModel", mock_model),
        patch("container_manager.time") as mock_time,
        patch.object(cm, "destroy_container") as mock_destroy,
        patch.object(cm, "_reconcile_orphans") as mock_reconcile,
    ):
        mock_time.time.return_value = 1700.0  # expired if it weren't paused
        cm.periodic_cleanup()

    mock_destroy.assert_not_called()
    cm.orchestrator.audit_counts.assert_called_once()
    mock_reconcile.assert_called_once()


def test_get_all_containers_expired_paused_row_kept_and_flagged(container_manager):
    cm = container_manager
    row = _row(
        paused_at=555.0,
        timer_started=True,
        timer_start_time=1000.0,
        timer_duration=600,
        vnc_port=5900,
        novnc_port=6080,
        vnc_password="secret",
        vnc_url="/vnc",
        max_extensions=3,
    )

    mock_model = MagicMock()
    mock_model.query.all.return_value = [row]

    mock_users = MagicMock()
    mock_users.query.filter.return_value.all.return_value = []

    with (
        patch("container_manager.DesktopContainerInfoModel", mock_model),
        patch("container_manager.Users", mock_users),
        patch("container_manager.time") as mock_time,
        patch.object(cm, "destroy_container") as mock_destroy,
    ):
        mock_time.time.return_value = 1700.0
        containers = cm.get_all_containers()

    mock_destroy.assert_not_called()
    assert len(containers) == 1
    assert containers[0]["paused"] is True
    assert "vnc_password" not in containers[0]
    assert "vnc_url" not in containers[0]
    assert "vnc_port" not in containers[0]


# ---------------------------------------------------------------------------
# pause_watch: mirror out-of-band pause/unpause into paused_at + events
# ---------------------------------------------------------------------------


def test_pause_watch_detects_out_of_band_pause(container_manager):
    cm = container_manager
    row = _row(paused_at=None)

    mock_model = MagicMock()
    mock_model.query.all.return_value = [row]

    cm.host_manager.get_connected_contexts.return_value = ["ctx1"]
    cm.host_manager.list_session_containers_strict.return_value = [
        {"name": row.container_name, "status": "paused", "created_ts": 100.0}
    ]

    mock_users = MagicMock()
    mock_users.query.filter_by.return_value.first.return_value = SimpleNamespace(name="alice", id=7)

    with (
        patch("container_manager.DesktopContainerInfoModel", mock_model),
        patch("container_manager.Users", mock_users),
        patch("container_manager.db") as mock_db,
        patch("container_manager.event_logger") as mock_events,
        patch("container_manager.time") as mock_time,
    ):
        mock_time.time.return_value = 2000.0
        cm.pause_watch()

    assert row.paused_at == 2000.0
    mock_db.session.commit.assert_called_once()
    mock_events.log_event.assert_called_once()
    args, kwargs = mock_events.log_event.call_args
    assert args[0] == "session_paused"
    assert kwargs["level"] == "error"
    assert kwargs["metadata"]["source"] == "detected"


def test_pause_watch_repairs_out_of_band_unpause_without_releasing_hold(container_manager):
    cm = container_manager
    row = _row(
        paused_at=1000.0,
        lifecycle_state=LIFECYCLE_HELD,
        timer_started=True,
        timer_start_time=500.0,
        timer_duration=600,
    )

    mock_model = MagicMock()
    mock_model.query.all.return_value = [row]

    cm.host_manager.get_connected_contexts.return_value = ["ctx1"]
    cm.host_manager.list_session_containers_strict.return_value = [
        {"name": row.container_name, "status": "running", "created_ts": 100.0}
    ]

    mock_users = MagicMock()
    mock_users.query.filter_by.return_value.first.return_value = SimpleNamespace(name="alice", id=7)

    with (
        patch("container_manager.DesktopContainerInfoModel", mock_model),
        patch("container_manager.Users", mock_users),
        patch("container_manager.db"),
        patch("container_manager.event_logger") as mock_events,
        patch("container_manager.time") as mock_time,
    ):
        mock_time.time.return_value = 1600.0  # paused for 600s
        cm.pause_watch()

    assert row.paused_at == 1000.0
    assert row.timer_start_time == 500.0
    assert row.lifecycle_state == LIFECYCLE_HELD
    cm.host_manager.pause_container.assert_called_once_with("ctx1", row.container_name)
    args, kwargs = mock_events.log_event.call_args
    assert args[0] == "session_paused"
    assert kwargs["level"] == "warning"
    assert kwargs["metadata"]["source"] == "drift_repaired"


# ---------------------------------------------------------------------------
# pause_session / unpause_session (admin endpoints)
# ---------------------------------------------------------------------------


def test_pause_session_sets_paused_at_and_pauses_container(container_manager):
    cm = container_manager
    row = _row(paused_at=None)

    mock_model = MagicMock()
    mock_model.query.filter_by.return_value.first.return_value = row

    with (
        patch("container_manager.DesktopContainerInfoModel", mock_model),
        patch("container_manager.db") as mock_db,
        patch("container_manager.time") as mock_time,
    ):
        mock_time.time.return_value = 2000.0
        result = cm.pause_session(7)

    assert result["success"] is True
    cm.host_manager.pause_container.assert_called_once_with("ctx1", row.container_name)
    assert row.paused_at == 2000.0
    mock_db.session.commit.assert_called_once()


def test_pause_session_already_paused_errors(container_manager):
    cm = container_manager
    row = _row(paused_at=1500.0)

    mock_model = MagicMock()
    mock_model.query.filter_by.return_value.first.return_value = row

    with patch("container_manager.DesktopContainerInfoModel", mock_model):
        result = cm.pause_session(7)

    assert result["success"] is False
    assert "already paused" in result["error"]
    cm.host_manager.pause_container.assert_not_called()


def test_unpause_session_credits_timer_and_clears(container_manager):
    cm = container_manager
    row = _row(
        paused_at=1000.0,
        timer_started=True,
        timer_start_time=500.0,
    )

    mock_model = MagicMock()
    mock_model.query.filter_by.return_value.first.return_value = row

    with (
        patch("container_manager.DesktopContainerInfoModel", mock_model),
        patch("container_manager.db"),
        patch("container_manager.time") as mock_time,
    ):
        mock_time.time.return_value = 1600.0
        result = cm.unpause_session(7)

    assert result["success"] is True
    cm.host_manager.unpause_container.assert_called_once_with("ctx1", row.container_name)
    assert row.timer_start_time == 1100.0  # increased by the 600s pause
    assert row.paused_at is None


def test_unpause_session_failure_restores_sticky_hold(container_manager):
    cm = container_manager
    row = _row(paused_at=1000.0, lifecycle_state=LIFECYCLE_HELD)
    cm.host_manager.unpause_container.side_effect = docker.errors.NotFound("gone")

    mock_model = MagicMock()
    mock_model.query.filter_by.return_value.first.return_value = row

    with (
        patch("container_manager.DesktopContainerInfoModel", mock_model),
        patch("container_manager.db"),
        patch.object(cm, "_locked_operation", return_value=None),
    ):
        result = cm.unpause_session(7)

    assert result["success"] is False
    assert row.lifecycle_state == LIFECYCLE_HELD
    assert row.paused_at == 1000.0


def test_pause_watch_ignores_explicit_unpause_transition(container_manager):
    cm = container_manager
    row = _row(paused_at=1000.0, lifecycle_state=LIFECYCLE_UNPAUSING)
    mock_model = MagicMock()
    mock_model.query.all.return_value = [row]
    cm.host_manager.get_connected_contexts.return_value = ["ctx1"]
    cm.host_manager.list_session_containers_strict.return_value = [
        {"name": row.container_name, "status": "running", "created_ts": 100.0}
    ]
    operation = SimpleNamespace(
        session_uuid=cm._session_uuid(row),
        state=OP_UNPAUSING,
        updated_at=1990.0,
    )

    with (
        patch("container_manager.DesktopContainerInfoModel", mock_model),
        patch("container_manager.Users"),
        patch("container_manager.db"),
        patch.object(cm, "_locked_operation", return_value=operation),
        patch("container_manager.time") as mock_time,
    ):
        mock_time.time.return_value = 2000.0
        cm.pause_watch()

    cm.host_manager.pause_container.assert_not_called()


def test_pause_watch_recovers_stale_unpause_to_hold(container_manager):
    cm = container_manager
    row = _row(paused_at=1000.0, lifecycle_state=LIFECYCLE_UNPAUSING)
    operation = SimpleNamespace(
        session_uuid=cm._session_uuid(row),
        state=OP_UNPAUSING,
        updated_at=1000.0,
    )
    mock_model = MagicMock()
    mock_model.query.all.return_value = [row]
    cm.host_manager.get_connected_contexts.return_value = ["ctx1"]
    cm.host_manager.list_session_containers_strict.return_value = [
        {"name": row.container_name, "status": "running", "created_ts": 100.0}
    ]

    with (
        patch("container_manager.DesktopContainerInfoModel", mock_model),
        patch("container_manager.Users"),
        patch("container_manager.db"),
        patch.object(cm, "_locked_operation", return_value=operation),
        patch("container_manager.time") as mock_time,
    ):
        mock_time.time.return_value = 2000.0
        cm.pause_watch()

    assert row.lifecycle_state == LIFECYCLE_HELD
    assert operation.state == LIFECYCLE_HELD
    cm.host_manager.pause_container.assert_called_once_with("ctx1", row.container_name)


def test_unpause_session_not_paused_errors(container_manager):
    cm = container_manager
    row = _row(paused_at=None)

    mock_model = MagicMock()
    mock_model.query.filter_by.return_value.first.return_value = row

    with patch("container_manager.DesktopContainerInfoModel", mock_model):
        result = cm.unpause_session(7)

    assert result["success"] is False
    assert "not paused" in result["error"]
    cm.host_manager.unpause_container.assert_not_called()


# ---------------------------------------------------------------------------
# _credit_pause_and_clear
# ---------------------------------------------------------------------------


def test_credit_pause_and_clear_shifts_start_time(container_manager):
    cm = container_manager
    row = _row(
        paused_at=1000.0,
        timer_started=True,
        timer_start_time=500.0,
    )

    with (
        patch("container_manager.db") as mock_db,
        patch("container_manager.time") as mock_time,
    ):
        mock_time.time.return_value = 1600.0
        cm._credit_pause_and_clear(row)

    assert row.timer_start_time == 1100.0  # +600s of frozen time
    assert row.paused_at is None
    mock_db.session.commit.assert_called_once()


def test_admin_kill_all_includes_creates_and_reports_every_outcome(container_manager):
    cm = container_manager
    rows = [SimpleNamespace(user_id=user_id) for user_id in range(1, 5)]
    model = MagicMock()
    model.query.all.return_value = rows
    operation_model = MagicMock()
    operation_model.state.in_.return_value = object()
    operation_model.query.filter.return_value.all.return_value = [SimpleNamespace(user_id=5)]
    cm.destroy_container = MagicMock(
        side_effect=[
            {"success": True},
            {"success": True, "status": "cancelling"},
            {"success": True, "status": "stopping"},
            {"success": False, "error": "unknown"},
            {"success": True, "status": "cancelling"},
        ]
    )
    admin = SimpleNamespace(id=99, name="admin")

    with (
        patch("container_manager.DesktopContainerInfoModel", model),
        patch("container_manager.DesktopSessionOperationModel", operation_model),
        patch("container_manager.event_logger") as events,
    ):
        summary = cm.destroy_all_containers_admin(admin)

    assert summary == {"requested": 5, "completed": 1, "cancelling": 2, "stopping": 1, "failed": 1}
    assert [call.args[0] for call in cm.destroy_container.call_args_list] == [1, 2, 3, 4, 5]
    assert events.log_event.call_args.kwargs["metadata"] == {"killed_count": 1, **summary}
