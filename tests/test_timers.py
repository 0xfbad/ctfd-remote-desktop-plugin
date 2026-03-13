from unittest.mock import patch, MagicMock
from container_manager import ContainerManager


def make_manager():
    cm = ContainerManager(MagicMock(), MagicMock())
    return cm


def setup_timer(cm, user_id=1, max_extensions=3):
    cm.session_timers[user_id] = {
        "started": False,
        "start_time": None,
        "duration": 0,
        "extensions_used": 0,
        "max_extensions": max_extensions,
    }


def test_start_timer():
    cm = make_manager()
    setup_timer(cm)
    result = cm.start_session_timer(1, duration=600)
    assert result["success"]
    assert result["duration"] == 600
    assert cm.session_timers[1]["started"]


def test_start_timer_no_session():
    cm = make_manager()
    result = cm.start_session_timer(1, duration=600)
    assert not result["success"]


def test_start_timer_already_started():
    cm = make_manager()
    setup_timer(cm)
    cm.start_session_timer(1, duration=600)
    result = cm.start_session_timer(1, duration=600)
    assert not result["success"]
    assert "already started" in result["error"]


def test_stop_timer():
    cm = make_manager()
    setup_timer(cm)
    cm.start_session_timer(1, duration=600)
    result = cm.stop_session_timer(1)
    assert result["success"]
    assert not cm.session_timers[1]["started"]


def test_stop_timer_not_started():
    cm = make_manager()
    setup_timer(cm)
    result = cm.stop_session_timer(1)
    assert not result["success"]


def test_extend_timer():
    cm = make_manager()
    setup_timer(cm, max_extensions=3)
    cm.start_session_timer(1, duration=600)

    with patch("container_manager.time") as mock_time:
        # 100 seconds have elapsed
        mock_time.time.return_value = cm.session_timers[1]["start_time"] + 100
        result = cm.extend_session_timer(1, new_duration=300)

    assert result["success"]
    assert result["extensions_used"] == 1
    # remaining was 500, plus 300 extension
    assert cm.session_timers[1]["duration"] == 800


def test_extend_max_reached():
    cm = make_manager()
    setup_timer(cm, max_extensions=1)
    cm.start_session_timer(1, duration=600)

    with patch("container_manager.time") as mock_time:
        mock_time.time.return_value = cm.session_timers[1]["start_time"]
        cm.extend_session_timer(1, new_duration=300)
        result = cm.extend_session_timer(1, new_duration=300)

    assert not result["success"]
    assert "Maximum extensions" in result["error"]


def test_timer_status_running():
    cm = make_manager()
    setup_timer(cm)
    cm.start_session_timer(1, duration=600)

    with patch("container_manager.time") as mock_time:
        mock_time.time.return_value = cm.session_timers[1]["start_time"] + 100
        status = cm.get_session_timer_status(1)

    assert status["success"]
    assert status["started"]
    assert status["time_remaining"] == 500


def test_timer_status_expired():
    cm = make_manager()
    setup_timer(cm)
    cm.start_session_timer(1, duration=600)

    with patch("container_manager.time") as mock_time:
        mock_time.time.return_value = cm.session_timers[1]["start_time"] + 700
        status = cm.get_session_timer_status(1)

    assert status["expired"]
    assert status["time_remaining"] == 0


def test_timer_status_not_started():
    cm = make_manager()
    setup_timer(cm)
    status = cm.get_session_timer_status(1)
    assert status["success"]
    assert not status["started"]
    assert status["time_remaining"] == 0


def test_concurrent_extend():
    import threading

    num_threads = 20
    max_ext = 5
    cm = make_manager()
    setup_timer(cm, user_id=1, max_extensions=max_ext)
    cm.start_session_timer(1, duration=60000)
    barrier = threading.Barrier(num_threads + 1)
    results = []
    results_lock = threading.Lock()

    def worker():
        barrier.wait()
        result = cm.extend_session_timer(1, new_duration=10)
        with results_lock:
            results.append(result)

    threads = [threading.Thread(target=worker) for _ in range(num_threads)]
    for t in threads:
        t.start()
    barrier.wait()
    for t in threads:
        t.join()

    successes = [r for r in results if r["success"]]
    assert len(successes) == max_ext
    assert cm.session_timers[1]["extensions_used"] == max_ext
