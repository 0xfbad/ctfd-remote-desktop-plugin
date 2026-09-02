from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from docker_host_manager import (
    DockerHostManager,
    SESSION_LABEL_MANAGED,
    SESSION_LABEL_USER_ID,
    SESSION_LABEL_UUID,
)


SESSION_UUID = "12345678-90ab-4cde-8f01-234567890abc"
CONTAINER_NAME = "rd-session-7-12345678-90a"
CONTAINER_ID = "a" * 64
LABELS = {
    SESSION_LABEL_MANAGED: "true",
    SESSION_LABEL_USER_ID: "7",
    SESSION_LABEL_UUID: SESSION_UUID,
}


def _entry(**overrides):
    entry = {
        "id": CONTAINER_ID,
        "name": CONTAINER_NAME,
        "status": "paused",
        "created_ts": 100.0,
        "labels": dict(LABELS),
    }
    entry.update(overrides)
    return entry


def _host_manager():
    manager = DockerHostManager()
    manager._context_configs = {"runner-a": "unix:///fake.sock"}
    manager._config_generation = 1
    return manager


def test_strict_listing_includes_full_id_and_labels():
    manager = _host_manager()
    container = MagicMock()
    container.id = CONTAINER_ID
    container.name = CONTAINER_NAME
    container.status = "paused"
    container.attrs = {"Created": "2026-08-30T12:00:00Z", "Config": {"Labels": LABELS}}
    client = MagicMock()
    client.containers.list.return_value = [container]
    with patch.object(manager, "_get_client", return_value=client):
        rows = manager.list_session_containers_strict("runner-a", "rd-session-")
    assert rows is not None
    assert rows[0]["id"] == CONTAINER_ID
    assert rows[0]["labels"] == LABELS


def test_strict_listing_enforces_prefix_after_docker_partial_name_filter():
    manager = _host_manager()
    container = MagicMock()
    container.id = CONTAINER_ID
    container.name = f"unrelated-{CONTAINER_NAME}"
    container.status = "paused"
    container.attrs = {"Created": "2026-08-30T12:00:00Z", "Config": {"Labels": LABELS}}
    client = MagicMock()
    client.containers.list.return_value = [container]

    with patch.object(manager, "_get_client", return_value=client):
        assert manager.list_session_containers_strict("runner-a", "rd-session-") == []


def test_host_removal_rechecks_exact_managed_paused_identity():
    manager = _host_manager()
    container = MagicMock()
    container.id = CONTAINER_ID
    container.name = CONTAINER_NAME
    container.status = "paused"
    container.attrs = {"Config": {"Labels": LABELS}}
    client = MagicMock()
    client.containers.get.return_value = container
    with patch.object(manager, "_get_client", return_value=client):
        assert manager.remove_paused_managed_orphan("runner-a", CONTAINER_ID, CONTAINER_NAME, LABELS) == LABELS
    container.reload.assert_called_once()
    container.remove.assert_called_once_with(force=True)


@pytest.mark.parametrize(
    "id_value,name_value,status,labels,error",
    [
        ("b" * 64, CONTAINER_NAME, "paused", LABELS, "identity changed"),
        (CONTAINER_ID, "rd-session-8-12345678-90a", "paused", LABELS, "identity changed"),
        (CONTAINER_ID, CONTAINER_NAME, "running", LABELS, "no longer paused"),
        (CONTAINER_ID, CONTAINER_NAME, "paused", {}, "ownership labels changed"),
        (
            CONTAINER_ID,
            CONTAINER_NAME,
            "paused",
            {**LABELS, SESSION_LABEL_USER_ID: "8"},
            "ownership labels changed",
        ),
        (
            CONTAINER_ID,
            CONTAINER_NAME,
            "paused",
            {**LABELS, SESSION_LABEL_UUID: "abcdefab-cdef-4abc-8def-abcdefabcdef"},
            "ownership labels changed",
        ),
    ],
)
def test_host_removal_refuses_changed_or_unmanaged_target(id_value, name_value, status, labels, error):
    manager = _host_manager()
    container = MagicMock()
    container.id = id_value
    container.name = name_value
    container.status = status
    container.attrs = {"Config": {"Labels": labels}}
    client = MagicMock()
    client.containers.get.return_value = container
    with patch.object(manager, "_get_client", return_value=client):
        with pytest.raises(ValueError, match=error):
            manager.remove_paused_managed_orphan("runner-a", CONTAINER_ID, CONTAINER_NAME, LABELS)
    container.remove.assert_not_called()


def test_manager_lists_only_unreferenced_paused_managed_containers(container_manager):
    cm = container_manager
    cm.host_manager.get_connected_contexts.return_value = ["runner-a"]
    cm.host_manager.list_session_containers_strict.return_value = [
        _entry(),
        _entry(
            id="b" * 64,
            name="rd-session-8-abcdefab-cde",
            labels={
                SESSION_LABEL_MANAGED: "true",
                SESSION_LABEL_USER_ID: "8",
                SESSION_LABEL_UUID: "abcdefab-cdef-4abc-8def-abcdefabcdef",
            },
        ),
        _entry(id="c" * 64, name="rd-session-9-fedcbafe-dcb", labels={}),
        _entry(id="d" * 64, name="rd-session-10-aaaaaaaa-aaa", status="running"),
    ]
    with patch.object(cm, "_session_reference_sets", return_value=({"rd-session-8-abcdefab-cde"}, set())):
        rows = cm.list_paused_orphans()
    assert [row["container_id"] for row in rows] == [CONTAINER_ID]
    assert rows[0]["user_id"] == 7
    assert rows[0]["session_uuid"] == SESSION_UUID


def test_manager_removes_exact_orphan_then_audits_capacity(container_manager):
    cm = container_manager
    cm.host_manager.list_session_containers_strict.return_value = [_entry()]
    admin = SimpleNamespace(id=1, name="admin")
    users = MagicMock()
    users.query.filter_by.return_value.first.return_value = SimpleNamespace(id=7, name="alice")
    order: list[str] = []
    with (
        patch.object(cm, "_session_reference_sets", return_value=(set(), set())),
        patch("container_manager.Users", users),
        patch("container_manager.event_logger") as events,
    ):
        events.log_event_sync.side_effect = lambda *_args, **kwargs: order.append(str(kwargs["metadata"]["outcome"]))
        cm.host_manager.remove_paused_managed_orphan.side_effect = lambda *_args: order.append("remove") or LABELS
        result = cm.remove_paused_orphan_admin(admin, "runner-a", CONTAINER_ID, CONTAINER_NAME)
    assert result == {"success": True}
    cm.host_manager.remove_paused_managed_orphan.assert_called_once_with(
        "runner-a", CONTAINER_ID, CONTAINER_NAME, LABELS
    )
    cm.orchestrator.release_slot.assert_not_called()
    cm.orchestrator.audit_counts.assert_called_once()
    assert [call.kwargs["metadata"]["outcome"] for call in events.log_event_sync.call_args_list] == [
        "requested",
        "completed",
    ]
    assert order == ["requested", "remove", "completed"]
    metadata = events.log_event_sync.call_args.kwargs["metadata"]
    assert metadata["outcome"] == "completed"
    assert metadata["target_id"] == 7
    assert metadata["session_uuid"] == SESSION_UUID


def test_manager_refuses_any_active_or_operation_reference_and_audits_failure(container_manager):
    cm = container_manager
    cm.host_manager.list_session_containers_strict.return_value = [_entry()]
    admin = SimpleNamespace(id=1, name="admin")
    with (
        patch.object(cm, "_session_reference_sets", return_value=({CONTAINER_NAME}, {SESSION_UUID})),
        patch("container_manager.Users") as users,
        patch("container_manager.event_logger") as events,
    ):
        users.query.filter_by.return_value.first.return_value = None
        result = cm.remove_paused_orphan_admin(admin, "runner-a", CONTAINER_ID, CONTAINER_NAME)
    assert result["success"] is False
    assert "referenced" in str(result["error"])
    cm.host_manager.remove_paused_managed_orphan.assert_not_called()
    assert events.log_event_sync.call_args.kwargs["metadata"]["outcome"] == "failed"


def test_manager_fails_closed_when_requested_audit_cannot_commit(container_manager):
    cm = container_manager
    cm.host_manager.list_session_containers_strict.return_value = [_entry()]
    admin = SimpleNamespace(id=1, name="admin")
    with (
        patch.object(cm, "_session_reference_sets", return_value=(set(), set())),
        patch("container_manager.Users") as users,
        patch("container_manager.event_logger") as events,
    ):
        users.query.filter_by.return_value.first.return_value = None
        events.log_event_sync.side_effect = RuntimeError("audit database down")
        result = cm.remove_paused_orphan_admin(admin, "runner-a", CONTAINER_ID, CONTAINER_NAME)

    assert result["success"] is False
    assert "audit database down" in str(result["error"])
    cm.host_manager.remove_paused_managed_orphan.assert_not_called()


def test_dashboard_exposes_small_paused_orphan_flow():
    source = (
        __import__("pathlib").Path(__file__).resolve().parents[1]
        / "src"
        / "templates"
        / "remote_desktop_dashboard.html"
    ).read_text()
    assert 'id="paused-orphans-section"' in source
    assert "/dashboard/api/paused-orphans" in source
    assert "confirm: 'DELETE'" in source
    assert "removePausedOrphan(${index})" in source
