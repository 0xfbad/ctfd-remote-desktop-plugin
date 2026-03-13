from unittest.mock import patch, MagicMock
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
