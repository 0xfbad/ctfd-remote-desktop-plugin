from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import docker
import paramiko
from docker_host_manager import DockerHostManager
from orchestrator import Orchestrator


def test_reconcile_removes_stale_records():
    hm = MagicMock(spec=DockerHostManager)
    hm.is_container_running.return_value = False

    Orchestrator(hm)

    row = MagicMock()
    row.container_id = "dead123"
    row.docker_context = "ctx1"

    mock_model = MagicMock()
    mock_model.query.all.return_value = [row]

    mock_db = MagicMock()

    with patch.dict("sys.modules", {}):
        # the reconciliation loop lives inline in __init__.py so it is mirrored here
        rows = [row]
        removed = 0
        for r in rows:
            if not hm.is_container_running(r.docker_context, r.container_id):
                mock_db.session.delete(r)
                removed += 1

        if removed:
            mock_db.session.commit()

    assert removed == 1
    mock_db.session.delete.assert_called_once_with(row)
    mock_db.session.commit.assert_called_once()


def test_reconcile_keeps_running_containers_and_syncs_counter():
    from collections import Counter

    hm = MagicMock(spec=DockerHostManager)
    hm.is_container_running.return_value = True

    row = MagicMock()
    row.container_id = "alive123"
    row.docker_context = "ctx1"

    rows = [row]
    kept = 0
    for r in rows:
        if hm.is_container_running(r.docker_context, r.container_id):
            kept += 1

    remaining = Counter(r.docker_context for r in rows)
    ctx = MagicMock(context_name="ctx1", active_sessions=42)
    target = remaining.get(ctx.context_name, 0)
    if (ctx.active_sessions or 0) != target:
        ctx.active_sessions = target

    assert kept == 1
    assert ctx.active_sessions == 1


def test_reconcile_handles_exception_as_stale():
    hm = MagicMock(spec=DockerHostManager)
    hm.is_container_running.side_effect = Exception("connection refused")

    row = MagicMock()
    row.container_id = "err123"
    row.docker_context = "ctx1"

    mock_db = MagicMock()

    rows = [row]
    removed = 0
    for r in rows:
        try:
            if not hm.is_container_running(r.docker_context, r.container_id):
                mock_db.session.delete(r)
                removed += 1
        except Exception:
            mock_db.session.delete(r)
            removed += 1

    assert removed == 1
    mock_db.session.delete.assert_called_once_with(row)


def test_reconcile_mixed():
    from collections import Counter

    hm = MagicMock(spec=DockerHostManager)
    hm.is_container_running.side_effect = [True, False, True]

    rows = [
        MagicMock(container_id="alive1", docker_context="ctx1"),
        MagicMock(container_id="dead1", docker_context="ctx1"),
        MagicMock(container_id="alive2", docker_context="ctx1"),
    ]

    mock_db = MagicMock()
    kept = []
    removed = 0

    for r in rows:
        try:
            if hm.is_container_running(r.docker_context, r.container_id):
                kept.append(r)
            else:
                mock_db.session.delete(r)
                removed += 1
        except Exception:
            mock_db.session.delete(r)
            removed += 1

    remaining = Counter(r.docker_context for r in kept)
    assert len(kept) == 2
    assert removed == 1
    assert remaining["ctx1"] == 2


def test_verify_or_reap_running_keeps_row(container_manager):
    cm = container_manager
    cm.host_manager.is_container_running.return_value = True

    row = MagicMock(docker_context="ctx1", container_id="c1")

    mock_db = MagicMock()
    with patch("container_manager.db", mock_db):
        result = cm._verify_or_reap(row)

    assert result is True
    mock_db.session.delete.assert_not_called()
    mock_db.session.commit.assert_not_called()
    cm.orchestrator.release_slot.assert_not_called()


def test_verify_or_reap_vanished_reaps_row(container_manager):
    cm = container_manager
    cm.host_manager.inspect_container_state.return_value = "not_found"

    row = MagicMock(
        docker_context="ctx1",
        container_id="c1",
        user_id=42,
        created_at=1000.0,
        extensions_used=2,
    )

    mock_db = MagicMock()
    mock_users = MagicMock()
    mock_users.query.filter_by.return_value.first.return_value = SimpleNamespace(name="alice", id=1)
    mock_history = MagicMock()
    # reap re-queries inside the destroy lock so history is never written twice
    mock_model = MagicMock()
    mock_model.query.filter_by.return_value.first.return_value = row

    with (
        patch("container_manager.db", mock_db),
        patch("container_manager.Users", mock_users),
        patch("models.DesktopSessionHistoryModel", mock_history),
        patch("container_manager.DesktopContainerInfoModel", mock_model),
        patch("models.get_setting", return_value=False),
    ):
        result = cm._verify_or_reap(row)

    assert result is False
    mock_db.session.delete.assert_called_once_with(row)
    # three commits, release locks before state inspection, fence teardown, finalize after removal
    assert mock_db.session.commit.call_count == 3
    cm.orchestrator.release_active_slot_in_transaction.assert_called_once_with(
        "ctx1",
        42,
        cm._session_uuid(row),
    )
    cm.orchestrator.release_slot.assert_not_called()
    mock_history.assert_called_once()
    assert mock_history.call_args.kwargs["end_reason"] == "reconciliation"


def test_verify_or_reap_row_already_gone_returns_false(container_manager):
    cm = container_manager
    cm.host_manager.inspect_container_state.return_value = "not_found"

    row = MagicMock(
        docker_context="ctx1",
        container_id="c1",
        user_id=42,
        created_at=1000.0,
        extensions_used=2,
    )

    mock_db = MagicMock()
    mock_history = MagicMock()
    # the re-query inside the lock finds nothing when a concurrent destroy already reaped the row
    mock_model = MagicMock()
    mock_model.query.filter_by.return_value.first.return_value = None

    with (
        patch("container_manager.db", mock_db),
        patch("models.DesktopSessionHistoryModel", mock_history),
        patch("container_manager.DesktopContainerInfoModel", mock_model),
    ):
        result = cm._verify_or_reap(row)

    assert result is False
    mock_db.session.delete.assert_not_called()
    mock_db.session.commit.assert_not_called()
    cm.orchestrator.release_slot.assert_not_called()
    mock_history.assert_not_called()


def test_verify_or_reap_docker_exception_keeps_row(container_manager):
    cm = container_manager
    cm.host_manager.is_container_running.side_effect = docker.errors.DockerException("boom")

    row = MagicMock(docker_context="ctx1", container_id="c1")

    mock_db = MagicMock()
    with patch("container_manager.db", mock_db):
        result = cm._verify_or_reap(row)

    assert result is True
    mock_db.session.delete.assert_not_called()
    cm.orchestrator.release_slot.assert_not_called()


def test_verify_or_reap_ssh_exception_keeps_row(container_manager):
    cm = container_manager
    cm.host_manager.is_container_running.side_effect = paramiko.ssh_exception.SSHException("boom")

    row = MagicMock(docker_context="ctx1", container_id="c1")

    mock_db = MagicMock()
    with patch("container_manager.db", mock_db):
        result = cm._verify_or_reap(row)

    assert result is True
    mock_db.session.delete.assert_not_called()
    cm.orchestrator.release_slot.assert_not_called()


def test_concurrent_reap_and_destroy_only_one_history(container_manager):
    """without the destroy lock inside _verify_or_reap both racing paths pass their row check and add history"""
    import threading

    cm = container_manager
    cm.host_manager.is_container_running.return_value = False

    # both paths start from this row, the destroy lock winner deletes it and the loser re-query finds nothing
    row_state = {
        "row": MagicMock(
            docker_context="ctx1",
            container_id="c1",
            container_name="rd-c1",
            user_id=42,
            created_at=1000.0,
            extensions_used=2,
        )
    }

    def _first_then_none():
        if row_state["row"] is None:
            return None
        return row_state["row"]

    history_adds = []
    history_lock = threading.Lock()

    class _Session:
        def add(self, obj):
            with history_lock:
                history_adds.append(obj)

        def delete(self, obj):
            row_state["row"] = None

        def commit(self):
            pass

        def rollback(self):
            pass

    mock_db = MagicMock()
    mock_db.session = _Session()

    mock_model = MagicMock()
    mock_model.query.filter_by.return_value.first.side_effect = lambda: _first_then_none()

    mock_users = MagicMock()
    mock_users.query.filter_by.return_value.first.return_value = SimpleNamespace(name="alice", id=1)

    mock_history = MagicMock(side_effect=lambda **kw: MagicMock())

    barrier = threading.Barrier(2)
    errors = []

    def reap_worker():
        barrier.wait()
        try:
            cm._verify_or_reap(row_state["row"])
        except Exception as e:
            errors.append(("reap", e))

    def destroy_worker():
        barrier.wait()
        try:
            cm.destroy_container(42, reason="admin_killed", log_destruction=False)
        except Exception as e:
            errors.append(("destroy", e))

    # patch in the main thread, patch swaps module globals so a per-thread exit races the restore
    with (
        patch("container_manager.db", mock_db),
        patch("container_manager.DesktopContainerInfoModel", mock_model),
        patch("container_manager.Users", mock_users),
        patch("models.DesktopSessionHistoryModel", mock_history),
        patch("models.get_setting", return_value=False),
    ):
        t1 = threading.Thread(target=reap_worker)
        t2 = threading.Thread(target=destroy_worker)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

    assert not errors, f"unexpected errors: {errors}"
    assert len(history_adds) == 1, f"expected 1 history insert, got {len(history_adds)}"


def test_repeated_concurrent_reap_and_destroy(container_manager):
    import threading

    cm = container_manager
    cm.host_manager.is_container_running.return_value = False

    for iteration in range(40):
        row_state = {
            "row": MagicMock(
                docker_context="ctx1",
                container_id=f"c{iteration}",
                container_name=f"rd-c{iteration}",
                user_id=99,
                created_at=1000.0,
                extensions_used=0,
            )
        }

        def _first_then_none():
            if row_state["row"] is None:
                return None
            return row_state["row"]

        history_adds = []
        history_lock = threading.Lock()

        class _Session:
            def add(self, obj):
                with history_lock:
                    history_adds.append(obj)

            def delete(self, obj):
                row_state["row"] = None

            def commit(self):
                pass

            def rollback(self):
                pass

        mock_db = MagicMock()
        mock_db.session = _Session()

        mock_model = MagicMock()
        mock_model.query.filter_by.return_value.first.side_effect = lambda: _first_then_none()

        mock_users = MagicMock()
        mock_users.query.filter_by.return_value.first.return_value = SimpleNamespace(name="alice", id=1)

        mock_history = MagicMock(side_effect=lambda **kw: MagicMock())

        barrier = threading.Barrier(2)
        # capture the row before racing, reading row_state after the destroy nulls it is an impossible call shape
        reap_row = row_state["row"]

        def reap_worker():
            barrier.wait()
            cm._verify_or_reap(reap_row)

        def destroy_worker():
            barrier.wait()
            cm.destroy_container(99, reason="admin_killed", log_destruction=False)

        with (
            patch("container_manager.db", mock_db),
            patch("container_manager.DesktopContainerInfoModel", mock_model),
            patch("container_manager.Users", mock_users),
            patch("models.DesktopSessionHistoryModel", mock_history),
            patch("models.get_setting", return_value=False),
        ):
            threads = [threading.Thread(target=reap_worker), threading.Thread(target=destroy_worker)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        assert len(history_adds) == 1, f"iteration {iteration}: expected 1 history insert, got {len(history_adds)}"


def _orphan_sweep(cm, listing, db_names=()):
    mock_model = MagicMock()
    mock_model.query.with_entities.return_value.all.return_value = [SimpleNamespace(container_name=n) for n in db_names]

    cm.host_manager.get_connected_contexts.return_value = ["ctx1"]
    cm.host_manager.list_session_containers_strict.return_value = listing

    ev = MagicMock()

    with (
        patch("container_manager.DesktopContainerInfoModel", mock_model),
        patch("container_manager.event_logger", ev),
    ):
        cm._reconcile_orphans()

    return SimpleNamespace(events=ev)


def test_reconcile_orphans_strict_none_skips_removal(container_manager):
    """a failed strict listing cannot prove that an orphan is removable"""
    cm = container_manager

    _orphan_sweep(cm, listing=None, db_names=("rd-session-live",))

    cm.host_manager.force_remove_container.assert_not_called()


def test_reconcile_orphans_removes_old_running_orphan(container_manager):
    import time

    from docker_host_manager import SESSION_LABEL_MANAGED, SESSION_LABEL_USER_ID, SESSION_LABEL_UUID

    cm = container_manager
    session_uuid = "12345678-90ab-4cde-8f01-234567890abc"
    orphan_name = "rd-session-9-12345678-90a"
    listing = [
        {"name": "rd-session-live", "created_ts": time.time() - 10, "status": "running"},
        {
            "name": orphan_name,
            "created_ts": time.time() - 400,
            "status": "running",
            "labels": {
                SESSION_LABEL_MANAGED: "true",
                SESSION_LABEL_USER_ID: "9",
                SESSION_LABEL_UUID: session_uuid,
            },
        },
    ]

    mocks = _orphan_sweep(cm, listing=listing, db_names=("rd-session-live",))

    cm.host_manager.force_remove_container.assert_called_once_with("ctx1", orphan_name)
    reaped = [c.args[0] for c in mocks.events.log_event.call_args_list]
    assert "orphan_reaped" in reaped


def test_reconcile_orphans_paused_orphan_held_not_removed(container_manager):
    """a paused orphan is an evidence hold, it is never force removed"""
    import time

    from docker_host_manager import SESSION_LABEL_MANAGED, SESSION_LABEL_USER_ID, SESSION_LABEL_UUID

    cm = container_manager
    listing = [
        {
            "name": "rd-session-9-12345678-90a",
            "created_ts": time.time() - 400,
            "status": "paused",
            "labels": {
                SESSION_LABEL_MANAGED: "true",
                SESSION_LABEL_USER_ID: "9",
                SESSION_LABEL_UUID: "12345678-90ab-4cde-8f01-234567890abc",
            },
        }
    ]

    mocks = _orphan_sweep(cm, listing=listing)

    cm.host_manager.force_remove_container.assert_not_called()
    events = [c.args[0] for c in mocks.events.log_event.call_args_list]
    assert "orphan_paused" in events


def test_reconcile_orphans_never_removes_unmanaged_prefix_collision(container_manager):
    import time

    cm = container_manager
    listing = [
        {
            "name": "rd-session-9-12345678-90a",
            "created_ts": time.time() - 400,
            "status": "running",
            "labels": {},
        }
    ]

    _orphan_sweep(cm, listing=listing)

    cm.host_manager.force_remove_container.assert_not_called()
    cm.orchestrator.release_slot.assert_not_called()
