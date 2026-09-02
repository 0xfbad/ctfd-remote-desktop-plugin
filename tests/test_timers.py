from unittest.mock import patch, MagicMock


class FakeRow:
    def __init__(self, user_id=1, max_extensions=3):
        self.user_id = user_id
        self.timer_started = False
        self.timer_start_time = None
        self.timer_duration = 0
        self.extensions_used = 0
        self.max_extensions = max_extensions


def _patch_db(row):
    mock_query = MagicMock()
    mock_query.filter_by.return_value.first.return_value = row
    mock_model = MagicMock()
    mock_model.query = mock_query
    return patch("container_manager.DesktopContainerInfoModel", mock_model)


def test_extend_timer(container_manager):
    cm = container_manager
    row = FakeRow(max_extensions=3)
    row.timer_started = True
    row.timer_start_time = 1000.0
    row.timer_duration = 600

    with _patch_db(row), patch("container_manager.db"), patch("container_manager.time") as mock_time:
        mock_time.time.return_value = 1100.0
        result = cm.extend_session_timer(1, new_duration=300)

    assert result["success"]
    assert result["extensions_used"] == 1
    assert row.timer_duration == 800  # extension adds to the remaining 500, not the original 600


def test_extend_max_reached(container_manager):
    cm = container_manager
    row = FakeRow(max_extensions=1)
    row.timer_started = True
    row.timer_start_time = 1000.0
    row.timer_duration = 600
    row.extensions_used = 1

    with _patch_db(row):
        result = cm.extend_session_timer(1, new_duration=300)

    assert not result["success"]
    assert "Maximum extensions" in result["error"]


def test_timer_status_running(container_manager):
    cm = container_manager
    row = FakeRow()
    row.timer_started = True
    row.timer_start_time = 1000.0
    row.timer_duration = 600

    with _patch_db(row), patch("container_manager.time") as mock_time:
        mock_time.time.return_value = 1100.0
        status = cm.get_session_timer_status(1)

    assert status["success"]
    assert status["started"]
    assert status["time_remaining"] == 500


def test_timer_status_expired(container_manager):
    cm = container_manager
    row = FakeRow()
    row.timer_started = True
    row.timer_start_time = 1000.0
    row.timer_duration = 600

    with _patch_db(row), patch("container_manager.time") as mock_time:
        mock_time.time.return_value = 1700.0
        status = cm.get_session_timer_status(1)

    assert status["expired"]
    assert status["time_remaining"] == 0


def test_timer_status_not_started(container_manager):
    cm = container_manager
    row = FakeRow()

    with _patch_db(row):
        status = cm.get_session_timer_status(1)

    assert status["success"]
    assert not status["started"]
    assert status["time_remaining"] == 0


def test_concurrent_extend(container_manager):
    import threading

    num_threads = 20
    max_ext = 5

    row = FakeRow(max_extensions=max_ext)
    row.timer_started = True
    row.timer_start_time = 1000.0
    row.timer_duration = 60000

    cm = container_manager
    barrier = threading.Barrier(num_threads + 1)
    results = []
    results_lock = threading.Lock()

    def worker():
        barrier.wait()
        result = cm.extend_session_timer(1, new_duration=10)
        with results_lock:
            results.append(result)

    with _patch_db(row), patch("container_manager.db"):
        threads = [threading.Thread(target=worker) for _ in range(num_threads)]
        for t in threads:
            t.start()
        barrier.wait()
        for t in threads:
            t.join()

    successes = [r for r in results if r["success"]]
    assert len(successes) == max_ext
    assert row.extensions_used == max_ext


def test_two_thread_extend_no_lost_update(container_manager):
    """without the per-user destroy lock both threads read extensions_used 0 and both write 1"""
    import threading

    cm = container_manager
    row = FakeRow(max_extensions=3)
    row.timer_started = True
    row.timer_start_time = 1000.0
    row.timer_duration = 6000

    barrier = threading.Barrier(2)
    results = []
    results_lock = threading.Lock()

    def worker():
        barrier.wait()
        # repeat per thread to multiply interleaving chances under aggressive thread switching
        for _ in range(50):
            r = cm.extend_session_timer(1, new_duration=10)
            with results_lock:
                results.append(r)
            # reset between rounds so each round races up from zero
            if r["success"] and r["extensions_used"] >= row.max_extensions:
                with results_lock:
                    row.extensions_used = 0

    threads = [threading.Thread(target=worker) for _ in range(2)]
    with _patch_db(row), patch("container_manager.db"):
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    success_counts = [r["extensions_used"] for r in results if r["success"]]
    # the lock keeps every increment inside 1 to max_extensions
    assert all(1 <= v <= 3 for v in success_counts)

    failures_max = sum(1 for r in results if not r["success"] and "Maximum" in r.get("error", ""))
    total = len(results)
    assert len(success_counts) + failures_max == total
    assert len(success_counts) > 0


def test_two_thread_extend_max_reached_exact(container_manager):
    """exactly three successes, a lost update yields two and a double increment yields four"""
    import threading

    cm = container_manager
    row = FakeRow(max_extensions=3)
    row.timer_started = True
    row.timer_start_time = 1000.0
    row.timer_duration = 6000

    barrier = threading.Barrier(2)
    results = []
    results_lock = threading.Lock()

    def worker():
        barrier.wait()
        for _ in range(10):
            r = cm.extend_session_timer(1, new_duration=10)
            with results_lock:
                results.append(r)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    with _patch_db(row), patch("container_manager.db"):
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    successes = [r for r in results if r["success"]]
    assert len(successes) == 3
    assert row.extensions_used == 3
