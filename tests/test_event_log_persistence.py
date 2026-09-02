import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import event_logger
import models
import pytest


_MODELS_SRC = (Path(__file__).resolve().parent.parent / "src" / "models.py").read_text()
_MODELS_AST = ast.parse(_MODELS_SRC)


@pytest.fixture(autouse=True)
def _isolate_persistence_state():
    original_queue = event_logger._persist_queue
    original_stats = event_logger.get_persistence_stats()
    original_stats.pop("queue_depth", None)
    yield
    event_logger._persist_queue = original_queue
    with event_logger._persistence_stats_lock:
        event_logger._persistence_stats.clear()
        event_logger._persistence_stats.update(original_stats)


def _find_class(name):
    for node in ast.walk(_MODELS_AST):
        if isinstance(node, ast.ClassDef) and node.name == name:
            return node
    raise AssertionError(f"class {name} not found in models.py")


def _column_calls(class_node):
    out = {}
    for item in class_node.body:
        if not isinstance(item, ast.Assign):
            continue
        if len(item.targets) != 1 or not isinstance(item.targets[0], ast.Name):
            continue
        if not isinstance(item.value, ast.Call):
            continue
        out[item.targets[0].id] = item.value
    return out


def _kwarg(call, key):
    for kw in call.keywords:
        if kw.arg == key:
            return kw.value
    return None


def test_desktop_event_log_model_has_expected_columns():
    cls = _find_class("DesktopEventLogModel")
    cols = _column_calls(cls)
    assert {
        "id",
        "event_id",
        "timestamp",
        "event_type",
        "level",
        "user_id",
        "username",
        "message",
        "metadata_json",
    }.issubset(cols.keys())
    for item in cls.body:
        if (
            isinstance(item, ast.Assign)
            and isinstance(item.targets[0], ast.Name)
            and item.targets[0].id == "__tablename__"
            and isinstance(item.value, ast.Constant)
        ):
            assert item.value.value == "desktop_event_log"
            break
    else:
        raise AssertionError("__tablename__ not set on DesktopEventLogModel")


def test_timestamp_and_event_type_are_indexed():
    cls = _find_class("DesktopEventLogModel")
    cols = _column_calls(cls)
    for col_name in ("timestamp", "event_type"):
        idx = _kwarg(cols[col_name], "index")
        assert isinstance(idx, ast.Constant) and idx.value is True, f"{col_name} not indexed"


def test_user_id_is_not_a_foreign_key():
    """audit history must survive user deletion, no FK CASCADE"""
    cls = _find_class("DesktopEventLogModel")
    cols = _column_calls(cls)
    user_id = cols["user_id"]
    for arg in user_id.args:
        if isinstance(arg, ast.Call):
            fn = arg.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
            assert name != "ForeignKey", "user_id should not be a FK"


def test_retention_days_in_setting_defaults():
    assert models.SETTING_DEFAULTS.get("retention_days") == 60


def test_persist_queue_receives_event_when_log_event_called():
    event_logger._persist_queue = None
    el = event_logger.EventLogger()
    el.log_event("test_type", "hello world", user_id=42, username="alice")
    q = event_logger._get_persist_queue()
    assert q.qsize() >= 1
    row = q.get_nowait()
    assert row["event_id"] == el.get_recent_events()[0]["id"]
    assert row["event_type"] == "test_type"
    assert row["user_id"] == 42
    assert row["username"] == "alice"
    assert "hello world" in row["message"]
    assert row["level"] == "info"
    assert isinstance(row["timestamp"], float)


def test_sync_event_persistence_commits_without_queueing():
    from CTFd.models import db

    event_logger._persist_queue = event_logger._DequeQueue(maxsize=10)
    db.reset_mock()
    with patch("models.DesktopEventLogModel", side_effect=lambda **values: SimpleNamespace(**values)):
        event = event_logger.EventLogger().log_event_sync(
            "admin_action",
            "requested evidence deletion",
            user_id=1,
            username="admin",
            metadata={"outcome": "requested"},
        )

    db.session.add.assert_called_once()
    db.session.commit.assert_called_once()
    db.session.rollback.assert_not_called()
    assert event_logger._get_persist_queue().qsize() == 0
    assert event["metadata"]["outcome"] == "requested"


def test_redis_outage_does_not_prevent_origin_persistence_enqueue():
    event_logger._persist_queue = event_logger._DequeQueue(maxsize=10)
    event_logger._reset_persistence_stats()

    with patch("event_bus.publish", return_value=False):
        event_logger.EventLogger().log_event("redis_down", "still local")

    assert event_logger._get_persist_queue().qsize() == 1
    assert event_logger.get_persistence_stats()["enqueued"] == 1


def test_event_to_row_includes_metadata_json():
    event = {
        "id": "worker-1:42",
        "type": "create",
        "timestamp": 123.0,
        "level": "warning",
        "user_id": 7,
        "username": "bob",
        "message": "did a thing",
        "metadata": {"foo": "bar", "n": 3},
    }
    row = event_logger._event_to_row(event)
    assert row["event_id"] == "worker-1:42"
    assert row["event_type"] == "create"
    assert row["level"] == "warning"
    assert row["metadata_json"] is not None
    assert "foo" in row["metadata_json"]


def test_event_to_row_bounds_event_id_to_model_width():
    row = event_logger._event_to_row({"id": "x" * 200, "type": "create", "timestamp": 123.0})

    assert row["event_id"] == "x" * 128


def test_retry_mapping_preserves_event_id_and_strips_queue_metadata():
    row = {
        "event_id": "worker-1:42",
        "event_type": "create",
        event_logger._PERSIST_RETRY_FIELD: 2,
    }

    insert_row = event_logger._row_for_insert(row)

    assert insert_row["event_id"] == "worker-1:42"
    assert event_logger._PERSIST_RETRY_FIELD not in insert_row
    assert row[event_logger._PERSIST_RETRY_FIELD] == 2


def test_drainer_exits_cleanly_on_stop():
    event_logger._drainer_stop = False
    event_logger.stop_persistence_drainer()
    assert event_logger._drainer_stop is True


def test_start_persistence_drainer_spawns_greenlet():
    fake_app = MagicMock()
    with patch("gevent.spawn") as spawn:
        spawn.return_value = MagicMock()
        result = event_logger.start_persistence_drainer(fake_app)
        spawn.assert_called_once()
        assert result is spawn.return_value


def test_stop_persistence_drainer_flushes_queued_rows():
    fake_app = MagicMock()
    event_logger._persist_queue = event_logger._DequeQueue(maxsize=10)
    event_logger._drainer_app = fake_app
    event_logger._drainer_handle = None
    event_logger._persist_queue.put_nowait({"event_id": "worker:1", "event_type": "test"})

    with patch.object(event_logger, "_process_persist_batch", return_value=True) as persist:
        remaining = event_logger.stop_persistence_drainer(deadline_seconds=1)

    assert remaining == 0
    persist.assert_called_once()
    assert persist.call_args.args[0] is fake_app


def test_stop_persistence_drainer_reports_remaining_rows_after_deadline():
    event_logger._persist_queue = event_logger._DequeQueue(maxsize=10)
    event_logger._drainer_app = MagicMock()
    event_logger._drainer_handle = None
    event_logger._persist_queue.put_nowait({"event_id": "worker:1"})

    remaining = event_logger.stop_persistence_drainer(deadline_seconds=0)

    assert remaining == 1


def test_prune_event_log_builds_delete_query():
    import sys

    fake_db = MagicMock()
    fake_model = MagicMock()
    fake_filter_result = MagicMock()
    fake_model.query.filter.return_value = fake_filter_result
    fake_filter_result.delete.return_value = 5
    # comparing a mock against a float would raise, so timestamp needs a real comparison
    fake_model.timestamp = type("FakeCol", (), {"__lt__": lambda self, other: True})()

    models_mod = sys.modules["models"]
    orig_model = models_mod.DesktopEventLogModel
    orig_db = sys.modules["CTFd.models"].db
    sys.modules["CTFd.models"].db = fake_db
    models_mod.DesktopEventLogModel = fake_model
    try:
        deleted = event_logger.prune_event_log(30)
    finally:
        models_mod.DesktopEventLogModel = orig_model
        sys.modules["CTFd.models"].db = orig_db

    assert deleted == 5
    fake_model.query.filter.assert_called_once()
    fake_filter_result.delete.assert_called_once()
    fake_db.session.commit.assert_called_once()


def test_drain_batch_pulls_up_to_max():
    event_logger._persist_queue = None
    q = event_logger._get_persist_queue()
    for i in range(150):
        q.put_nowait({"event_type": f"t{i}", "timestamp": float(i), "level": "info", "message": "m"})
    batch = event_logger._drain_batch(q, max_batch=100)
    assert len(batch) == 100
    assert q.qsize() == 50


def test_persist_queue_is_bounded():
    event_logger._persist_queue = None
    q = event_logger._get_persist_queue()
    # deque shim raises at _PERSIST_QUEUE_MAXSIZE, the swallow below models callers dropping overflow
    for i in range(event_logger._PERSIST_QUEUE_MAXSIZE + 100):
        try:
            q.put_nowait({"event_type": "t", "timestamp": float(i), "level": "info", "message": "m"})
        except Exception:
            pass
    assert q.qsize() <= event_logger._PERSIST_QUEUE_MAXSIZE


def test_queue_overflow_is_observable():
    event_logger._persist_queue = event_logger._DequeQueue(maxsize=1)
    event_logger._reset_persistence_stats()
    row = {"event_type": "t", "timestamp": 1.0, "level": "info", "message": "m"}

    assert event_logger._enqueue_persist_row(row) is True
    assert event_logger._enqueue_persist_row(dict(row)) is False

    stats = event_logger.get_persistence_stats()
    assert stats["queue_depth"] == 1
    assert stats["enqueued"] == 1
    assert stats["dropped_overflow"] == 1


def test_database_failure_rolls_back_and_removes_session():
    fake_app = MagicMock()
    fake_db = MagicMock()
    fake_db.session.commit.side_effect = RuntimeError("database unavailable")

    with patch("CTFd.models.db", fake_db):
        assert event_logger._write_persist_batch(fake_app, [{"event_type": "t"}]) is False

    fake_db.session.rollback.assert_called_once()
    fake_db.session.remove.assert_called_once()


def test_failed_batch_retries_are_bounded_and_counted():
    q = event_logger._DequeQueue(maxsize=10)
    event_logger._reset_persistence_stats()
    batch = [
        {
            "event_id": "worker-1:99",
            "event_type": "t",
            "timestamp": 1.0,
            "level": "info",
            "message": "m",
        }
    ]
    retry_event_ids = []

    with patch.object(event_logger, "_write_persist_batch", return_value=False):
        for _ in range(event_logger._PERSIST_MAX_RETRIES + 1):
            assert event_logger._process_persist_batch(MagicMock(), q, batch) is False
            batch = event_logger._drain_batch(q)
            retry_event_ids.extend(row["event_id"] for row in batch)

    assert batch == []
    stats = event_logger.get_persistence_stats()
    assert stats["write_failures"] == event_logger._PERSIST_MAX_RETRIES + 1
    assert stats["retried"] == event_logger._PERSIST_MAX_RETRIES
    assert stats["dropped_retry_exhausted"] == 1
    assert retry_event_ids == ["worker-1:99"] * event_logger._PERSIST_MAX_RETRIES


def test_retry_backoff_is_bounded():
    assert event_logger._retry_backoff(1.0, 1) == 1.0
    assert event_logger._retry_backoff(1.0, 2) == 2.0
    assert event_logger._retry_backoff(1.0, 100) == event_logger._PERSIST_BACKOFF_MAX_SECONDS


def test_persisted_event_tail_reconstructs_chronological_cross_worker_events():
    newest = SimpleNamespace(
        event_id="worker-b:2",
        timestamp=200.0,
        event_type="destroy",
        level="warning",
        user_id=8,
        username="bob",
        message="destroyed",
        metadata_json='{"context":"runner-b"}',
    )
    oldest = SimpleNamespace(
        event_id="worker-a:1",
        timestamp=100.0,
        event_type="create",
        level="info",
        user_id=7,
        username="alice",
        message="created",
        metadata_json=None,
    )
    model = MagicMock()
    model.query.order_by.return_value.limit.return_value.all.return_value = [newest, oldest]

    with patch("models.DesktopEventLogModel", model):
        events = event_logger.get_persisted_events(limit=2)

    assert [event["id"] for event in events] == ["worker-a:1", "worker-b:2"]
    assert events[1]["metadata"] == {"context": "runner-b"}
    model.query.order_by.return_value.limit.assert_called_once_with(2)
