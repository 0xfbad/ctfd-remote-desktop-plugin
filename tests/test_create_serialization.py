"""two concurrent create_container calls must produce one claimer and one in progress refusal
without one lock around the check and claim both callers spawn greenlets and leak a host slot"""

import threading
from types import SimpleNamespace
from unittest.mock import patch, MagicMock


def _patch_create_path(no_existing_row=True):
    mock_model = MagicMock()
    mock_model.query.filter_by.return_value.first.return_value = None if no_existing_row else MagicMock()

    mock_users = MagicMock()
    # a plain namespace value, not a mock, magic method materialization is not thread safe
    mock_users.query.filter_by.return_value.first.return_value = SimpleNamespace(name="alice", id=1)

    mock_app = MagicMock()
    mock_current_app = MagicMock()
    mock_current_app._get_current_object.return_value = mock_app

    mock_flask = MagicMock()
    mock_flask.current_app = mock_current_app

    # materialize mock children before concurrent use, first touch from two threads is not thread safe
    bool(mock_users.query.filter_by(id=1).first())
    mock_model.query.filter_by(user_id=1).first()
    mock_current_app._get_current_object()

    return mock_model, mock_users, mock_flask


def test_two_thread_create_only_one_winner(container_manager):
    cm = container_manager
    cm.orchestrator = MagicMock()
    cm.orchestrator.has_healthy_context.return_value = True
    cm.orchestrator.get_status.return_value = []

    mock_model, mock_users, mock_flask = _patch_create_path()

    barrier = threading.Barrier(2)
    results = []
    results_lock = threading.Lock()

    def worker():
        barrier.wait()
        try:
            r = cm.create_container(1, "http://test/", None)
        except Exception as e:
            r = {"success": False, "error": f"raised: {e}"}
        with results_lock:
            results.append(r)

    # patch once in the main thread, per-thread enter and exit races the delattr on the patched attribute
    with (
        patch("container_manager.DesktopContainerInfoModel", mock_model),
        patch("container_manager.Users", mock_users),
        patch("container_manager.current_app", mock_flask.current_app, create=True),
        patch("models.get_setting", return_value=True),
    ):
        threads = [threading.Thread(target=worker) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    creating = [r for r in results if r.get("status") == "creating"]
    losers = [r for r in results if not r.get("success") and "Creation already in progress" in r.get("error", "")]

    assert len(creating) == 1, f"expected 1 creating, got {len(creating)}: {results}"
    assert len(losers) == 1, f"expected 1 loser, got {len(losers)}: {results}"

    assert cm.creation_status[1]["status"] == "queued"


def test_many_thread_create_only_one_winner(container_manager):
    """higher fan out catches lock gaps that the two thread test can miss by luck"""
    cm = container_manager
    cm.orchestrator = MagicMock()
    cm.orchestrator.has_healthy_context.return_value = True
    cm.orchestrator.get_status.return_value = []

    mock_model, mock_users, mock_flask = _patch_create_path()

    num_threads = 30
    barrier = threading.Barrier(num_threads)
    results = []
    results_lock = threading.Lock()

    def worker():
        barrier.wait()
        try:
            r = cm.create_container(1, "http://test/", None)
        except Exception as e:
            r = {"success": False, "error": f"raised: {e}"}
        with results_lock:
            results.append(r)

    with (
        patch("container_manager.DesktopContainerInfoModel", mock_model),
        patch("container_manager.Users", mock_users),
        patch("container_manager.current_app", mock_flask.current_app, create=True),
        patch("models.get_setting", return_value=True),
    ):
        threads = [threading.Thread(target=worker) for _ in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    creating = [r for r in results if r.get("status") == "creating"]
    losers = [r for r in results if not r.get("success") and "Creation already in progress" in r.get("error", "")]

    assert len(creating) == 1, f"expected exactly 1 winner, got {len(creating)}: {results}"
    assert len(creating) + len(losers) == num_threads


def test_existing_session_rejected_under_lock(container_manager):
    """when the row already exists the early return inside the lock must not claim the queued slot"""
    cm = container_manager
    cm.orchestrator = MagicMock()
    cm.orchestrator.has_healthy_context.return_value = True
    cm.orchestrator.get_status.return_value = []

    mock_model, mock_users, mock_flask = _patch_create_path(no_existing_row=False)

    with (
        patch("container_manager.DesktopContainerInfoModel", mock_model),
        patch("container_manager.Users", mock_users),
        patch("container_manager.current_app", mock_flask.current_app, create=True),
    ):
        result = cm.create_container(1, "http://test/", None)

    assert not result["success"]
    assert "Session already exists" in result["error"]
    assert 1 not in cm.creation_status


def test_orchestrator_unhealthy_rolls_back_claim(container_manager):
    """when admission fails after the lock claimed the slot the rollback must clear creation_status"""
    from _rd_plugin.exceptions import HostsUnavailableException

    cm = container_manager
    cm.orchestrator = MagicMock()
    cm.orchestrator.admission_check.side_effect = HostsUnavailableException("no healthy docker contexts available")

    mock_model, mock_users, mock_flask = _patch_create_path()

    with (
        patch("container_manager.DesktopContainerInfoModel", mock_model),
        patch("container_manager.Users", mock_users),
        patch("container_manager.current_app", mock_flask.current_app, create=True),
    ):
        raised = False
        try:
            cm.create_container(1, "http://test/", None)
        except HostsUnavailableException:
            raised = True

    assert raised
    assert 1 not in cm.creation_status
