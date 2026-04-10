from unittest.mock import patch, MagicMock
from container_manager import ContainerManager


def make_manager():
    cm = ContainerManager(MagicMock(), MagicMock())
    return cm


class FakeRow:
    """Mutable stand-in for DesktopContainerInfoModel row."""

    def __init__(self, user_id=1, max_extensions=3):
        self.user_id = user_id
        self.timer_started = False
        self.timer_start_time = None
        self.timer_duration = 0
        self.extensions_used = 0
        self.max_extensions = max_extensions


def _patch_db(row):
    """Returns a patch context that makes the DB query return `row`."""
    mock_query = MagicMock()
    mock_query.filter_by.return_value.first.return_value = row
    mock_model = MagicMock()
    mock_model.query = mock_query
    return patch("container_manager.DesktopContainerInfoModel", mock_model)


def test_extend_timer():
    cm = make_manager()
    row = FakeRow(max_extensions=3)
    row.timer_started = True
    row.timer_start_time = 1000.0
    row.timer_duration = 600

    with _patch_db(row), patch("container_manager.db"), patch("container_manager.time") as mock_time:
        # 100 seconds have elapsed
        mock_time.time.return_value = 1100.0
        result = cm.extend_session_timer(1, new_duration=300)

    assert result["success"]
    assert result["extensions_used"] == 1
    # remaining was 500, plus 300 extension
    assert row.timer_duration == 800


def test_extend_max_reached():
    cm = make_manager()
    row = FakeRow(max_extensions=1)
    row.timer_started = True
    row.timer_start_time = 1000.0
    row.timer_duration = 600
    row.extensions_used = 1

    with _patch_db(row):
        result = cm.extend_session_timer(1, new_duration=300)

    assert not result["success"]
    assert "Maximum extensions" in result["error"]


def test_timer_status_running():
    cm = make_manager()
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


def test_timer_status_expired():
    cm = make_manager()
    row = FakeRow()
    row.timer_started = True
    row.timer_start_time = 1000.0
    row.timer_duration = 600

    with _patch_db(row), patch("container_manager.time") as mock_time:
        mock_time.time.return_value = 1700.0
        status = cm.get_session_timer_status(1)

    assert status["expired"]
    assert status["time_remaining"] == 0


def test_timer_status_not_started():
    cm = make_manager()
    row = FakeRow()

    with _patch_db(row):
        status = cm.get_session_timer_status(1)

    assert status["success"]
    assert not status["started"]
    assert status["time_remaining"] == 0


def test_concurrent_extend():
    import threading

    num_threads = 20
    max_ext = 5

    # shared row visible to all threads, simulates single DB row
    row = FakeRow(max_extensions=max_ext)
    row.timer_started = True
    row.timer_start_time = 1000.0
    row.timer_duration = 60000

    cm = make_manager()
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
