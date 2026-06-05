from unittest.mock import patch, MagicMock

import docker
import paramiko
from docker_host_manager import DockerHostManager
from orchestrator import Orchestrator


def test_reconcile_removes_stale_records():
    """Stale DB records (container no longer running) should be deleted."""
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
        # simulate the reconciliation logic from __init__.py
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


def test_reconcile_keeps_running_containers():
    """Running containers should be kept and their slots reserved in the orchestrator."""
    hm = MagicMock(spec=DockerHostManager)
    hm.is_container_running.return_value = True

    orchestrator = Orchestrator(hm)
    orchestrator.health = {"ctx1": True}
    orchestrator.weights = {"ctx1": 1}
    orchestrator.container_counts["ctx1"] = 0

    row = MagicMock()
    row.container_id = "alive123"
    row.docker_context = "ctx1"

    rows = [row]
    kept = 0
    for r in rows:
        if hm.is_container_running(r.docker_context, r.container_id):
            orchestrator.reserve_slot(r.docker_context)
            kept += 1

    assert kept == 1
    assert orchestrator.container_counts["ctx1"] == 1


def test_reconcile_handles_exception_as_stale():
    """If is_container_running raises, treat the record as stale."""
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
    """Mix of running and stale containers."""
    hm = MagicMock(spec=DockerHostManager)
    hm.is_container_running.side_effect = [True, False, True]

    orchestrator = Orchestrator(hm)
    orchestrator.health = {"ctx1": True}
    orchestrator.weights = {"ctx1": 1}
    orchestrator.container_counts["ctx1"] = 0

    rows = [
        MagicMock(container_id="alive1", docker_context="ctx1"),
        MagicMock(container_id="dead1", docker_context="ctx1"),
        MagicMock(container_id="alive2", docker_context="ctx1"),
    ]

    mock_db = MagicMock()
    kept = 0
    removed = 0

    for r in rows:
        try:
            if hm.is_container_running(r.docker_context, r.container_id):
                orchestrator.reserve_slot(r.docker_context)
                kept += 1
            else:
                mock_db.session.delete(r)
                removed += 1
        except Exception:
            mock_db.session.delete(r)
            removed += 1

    assert kept == 2
    assert removed == 1
    assert orchestrator.container_counts["ctx1"] == 2


def test_verify_or_reap_running_keeps_row(container_manager):
    """live container, helper returns True without touching the DB"""
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
    """missing container, helper writes history, releases slot, deletes row"""
    cm = container_manager
    cm.host_manager.is_container_running.return_value = False

    row = MagicMock(
        docker_context="ctx1",
        container_id="c1",
        user_id=42,
        created_at=1000.0,
        extensions_used=2,
    )

    mock_db = MagicMock()
    mock_users = MagicMock()
    mock_users.query.filter_by.return_value.first.return_value = MagicMock(name="alice")
    mock_history = MagicMock()
    # T10: reap now re-queries inside the destroy lock to avoid double-history
    mock_model = MagicMock()
    mock_model.query.filter_by.return_value.first.return_value = row

    with (
        patch("container_manager.db", mock_db),
        patch("container_manager.Users", mock_users),
        patch("container_manager.DesktopSessionHistoryModel", mock_history),
        patch("container_manager.DesktopContainerInfoModel", mock_model),
    ):
        result = cm._verify_or_reap(row)

    assert result is False
    mock_db.session.delete.assert_called_once_with(row)
    mock_db.session.commit.assert_called_once()
    cm.orchestrator.release_slot.assert_called_once_with("ctx1")
    mock_history.assert_called_once()
    assert mock_history.call_args.kwargs["end_reason"] == "reconciliation"


def test_verify_or_reap_row_already_gone_returns_false(container_manager):
    """T10: if a concurrent destroy already reaped the row, re-query inside
    the lock returns None and we bail out without double-inserting history"""
    cm = container_manager
    cm.host_manager.is_container_running.return_value = False

    row = MagicMock(
        docker_context="ctx1",
        container_id="c1",
        user_id=42,
        created_at=1000.0,
        extensions_used=2,
    )

    mock_db = MagicMock()
    mock_history = MagicMock()
    # the re-query inside the lock returns None (someone else already reaped)
    mock_model = MagicMock()
    mock_model.query.filter_by.return_value.first.return_value = None

    with (
        patch("container_manager.db", mock_db),
        patch("container_manager.DesktopSessionHistoryModel", mock_history),
        patch("container_manager.DesktopContainerInfoModel", mock_model),
    ):
        result = cm._verify_or_reap(row)

    assert result is False
    mock_db.session.delete.assert_not_called()
    mock_db.session.commit.assert_not_called()
    cm.orchestrator.release_slot.assert_not_called()
    mock_history.assert_not_called()


def test_verify_or_reap_docker_exception_keeps_row(container_manager):
    """transient docker error, helper returns True and leaves the row alone"""
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
    """transient ssh error, helper returns True and leaves the row alone"""
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
    """T10 regression: a concurrent admin destroy + verify_or_reap on the
    same vanished container must produce exactly one history insert, never
    two. without the destroy lock inside _verify_or_reap, both code paths
    can pass their respective row checks and both call session.add(history)."""
    import threading

    cm = container_manager
    cm.host_manager.is_container_running.return_value = False

    # shared row object both call paths see at the start. the destroy lock
    # winner deletes it, the loser's re-query returns None and bails out
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
        # mimic mariadb: the first query inside the lock sees the row, after
        # the winner's delete+commit subsequent queries return None
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
    mock_users.query.filter_by.return_value.first.return_value = MagicMock(name="alice")

    mock_history = MagicMock(side_effect=lambda **kw: MagicMock())

    barrier = threading.Barrier(2)
    errors = []

    def reap_worker():
        barrier.wait()
        try:
            with (
                patch("container_manager.db", mock_db),
                patch("container_manager.DesktopContainerInfoModel", mock_model),
                patch("container_manager.Users", mock_users),
                patch("container_manager.DesktopSessionHistoryModel", mock_history),
                patch("models.get_setting", return_value=False),
            ):
                cm._verify_or_reap(row_state["row"])
        except Exception as e:
            errors.append(("reap", e))

    def destroy_worker():
        barrier.wait()
        try:
            with (
                patch("container_manager.db", mock_db),
                patch("container_manager.DesktopContainerInfoModel", mock_model),
                patch("container_manager.Users", mock_users),
                patch("container_manager.DesktopSessionHistoryModel", mock_history),
                patch("models.get_setting", return_value=False),
            ):
                cm.destroy_container(42, reason="admin_killed", log_destruction=False)
        except Exception as e:
            errors.append(("destroy", e))

    t1 = threading.Thread(target=reap_worker)
    t2 = threading.Thread(target=destroy_worker)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert not errors, f"unexpected errors: {errors}"
    # exactly one history row must have been added across both paths
    assert len(history_adds) == 1, (
        f"expected 1 history insert, got {len(history_adds)}"
    )


def test_repeated_concurrent_reap_and_destroy(container_manager):
    """T10: run the race many times under aggressive thread switching.
    every iteration must produce exactly one history row, never two."""
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
        mock_users.query.filter_by.return_value.first.return_value = MagicMock(name="alice")

        mock_history = MagicMock(side_effect=lambda **kw: MagicMock())

        barrier = threading.Barrier(2)

        def reap_worker():
            barrier.wait()
            with (
                patch("container_manager.db", mock_db),
                patch("container_manager.DesktopContainerInfoModel", mock_model),
                patch("container_manager.Users", mock_users),
                patch("container_manager.DesktopSessionHistoryModel", mock_history),
                patch("models.get_setting", return_value=False),
            ):
                cm._verify_or_reap(row_state["row"])

        def destroy_worker():
            barrier.wait()
            with (
                patch("container_manager.db", mock_db),
                patch("container_manager.DesktopContainerInfoModel", mock_model),
                patch("container_manager.Users", mock_users),
                patch("container_manager.DesktopSessionHistoryModel", mock_history),
                patch("models.get_setting", return_value=False),
            ):
                cm.destroy_container(99, reason="admin_killed", log_destruction=False)

        threads = [threading.Thread(target=reap_worker), threading.Thread(target=destroy_worker)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(history_adds) == 1, (
            f"iteration {iteration}: expected 1 history insert, got {len(history_adds)}"
        )
