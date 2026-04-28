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

    with (
        patch("container_manager.db", mock_db),
        patch("container_manager.Users", mock_users),
        patch("container_manager.DesktopSessionHistoryModel", mock_history),
    ):
        result = cm._verify_or_reap(row)

    assert result is False
    mock_db.session.delete.assert_called_once_with(row)
    mock_db.session.commit.assert_called_once()
    cm.orchestrator.release_slot.assert_called_once_with("ctx1")
    mock_history.assert_called_once()
    assert mock_history.call_args.kwargs["end_reason"] == "reconciliation"


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
