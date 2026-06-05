"""T09 regression: two concurrent create_container calls for the same user
must result in exactly one claimer (status=creating) and the loser must see
"Creation already in progress". Without serializing the check-and-claim under
a single lock, both callers can pass the in-progress guard and both spawn
background greenlets, leaking a host slot."""

import threading
from unittest.mock import patch, MagicMock


def _patch_create_path(no_existing_row=True):
    """Patches needed to drive create_container all the way to gevent.spawn.

    Returns a list of context managers. Callers do `with ExitStack` style
    composition via a helper below."""

    mock_model = MagicMock()
    mock_model.query.filter_by.return_value.first.return_value = (
        None if no_existing_row else MagicMock()
    )

    mock_users = MagicMock()
    mock_users.query.filter_by.return_value.first.return_value = MagicMock(name="alice")

    mock_app = MagicMock()
    mock_current_app = MagicMock()
    mock_current_app._get_current_object.return_value = mock_app

    mock_flask = MagicMock()
    mock_flask.current_app = mock_current_app

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
        # patches must be entered per-thread since MagicMock attribute lookups
        # via patch() are not thread-shared
        with (
            patch("container_manager.DesktopContainerInfoModel", mock_model),
            patch("container_manager.Users", mock_users),
            patch("container_manager.current_app", mock_flask.current_app, create=True),
        ):
            try:
                r = cm.create_container(1, "http://test/", None)
            except Exception as e:
                r = {"success": False, "error": f"raised: {e}"}
        with results_lock:
            results.append(r)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    creating = [r for r in results if r.get("status") == "creating"]
    losers = [
        r for r in results
        if not r.get("success") and "Creation already in progress" in r.get("error", "")
    ]

    # exactly one winner claimed the slot, one loser got the in-progress error
    assert len(creating) == 1, f"expected 1 creating, got {len(creating)}: {results}"
    assert len(losers) == 1, f"expected 1 loser, got {len(losers)}: {results}"

    # and the in-memory claim records the winner
    assert cm.creation_status[1]["status"] == "queued"


def test_many_thread_create_only_one_winner(container_manager):
    """Higher fan-out under aggressive thread switching catches lock gaps
    that a 2-thread test might miss by luck."""
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
        with (
            patch("container_manager.DesktopContainerInfoModel", mock_model),
            patch("container_manager.Users", mock_users),
            patch("container_manager.current_app", mock_flask.current_app, create=True),
        ):
            try:
                r = cm.create_container(1, "http://test/", None)
            except Exception as e:
                r = {"success": False, "error": f"raised: {e}"}
        with results_lock:
            results.append(r)

    threads = [threading.Thread(target=worker) for _ in range(num_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    creating = [r for r in results if r.get("status") == "creating"]
    losers = [
        r for r in results
        if not r.get("success") and "Creation already in progress" in r.get("error", "")
    ]

    assert len(creating) == 1, f"expected exactly 1 winner, got {len(creating)}"
    # every other thread either lost as "in progress" or saw the row check
    # under the lock (mock returns None so it wouldn't be that, must be loser)
    assert len(creating) + len(losers) == num_threads


def test_existing_session_rejected_under_lock(container_manager):
    """Sanity: when the DB row exists, the early-return path inside the lock
    must NOT claim the queued slot. A subsequent create call after the row
    is removed should still see a clean status dict."""
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
    # critical: the lock window must not have left a stale "queued" entry
    assert 1 not in cm.creation_status


def test_orchestrator_unhealthy_rolls_back_claim(container_manager):
    """When orchestrator.has_healthy_context returns False after the lock
    has claimed the slot, the rollback path must clear creation_status so the
    user can retry."""
    from _rd_plugin.exceptions import HostsUnavailableException

    cm = container_manager
    cm.orchestrator = MagicMock()
    cm.orchestrator.has_healthy_context.return_value = False

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
    # the queued claim must have been rolled back
    assert 1 not in cm.creation_status
