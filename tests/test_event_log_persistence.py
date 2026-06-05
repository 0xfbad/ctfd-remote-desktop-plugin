import ast
from pathlib import Path
from unittest.mock import MagicMock, patch

import event_logger
import models


_MODELS_SRC = (Path(__file__).resolve().parent.parent / "src" / "models.py").read_text()
_MODELS_AST = ast.parse(_MODELS_SRC)


def _find_class(name):
    for node in ast.walk(_MODELS_AST):
        if isinstance(node, ast.ClassDef) and node.name == name:
            return node
    raise AssertionError(f"class {name} not found in models.py")


def _column_calls(class_node):
    """returns {column_name: ast.Call node for db.Column(...)}"""
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
        "timestamp",
        "event_type",
        "level",
        "user_id",
        "username",
        "message",
        "metadata_json",
    }.issubset(cols.keys())
    # __tablename__ assignment present and correct
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
    # walk args looking for db.ForeignKey calls
    for arg in user_id.args:
        if isinstance(arg, ast.Call):
            fn = arg.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
            assert name != "ForeignKey", "user_id should not be a FK"


def test_retention_days_in_setting_defaults():
    assert models.SETTING_DEFAULTS.get("retention_days") == 60


def test_persist_queue_receives_event_when_log_event_called():
    # reset module-level queue so the test starts clean
    event_logger._persist_queue = None
    el = event_logger.EventLogger()
    el.log_event("test_type", "hello world", user_id=42, username="alice")
    q = event_logger._get_persist_queue()
    # at least one row enqueued
    assert q.qsize() >= 1
    row = q.get_nowait()
    assert row["event_type"] == "test_type"
    assert row["user_id"] == 42
    assert row["username"] == "alice"
    assert "hello world" in row["message"]
    assert row["level"] == "info"
    assert isinstance(row["timestamp"], float)


def test_event_to_row_includes_metadata_json():
    event = {
        "type": "create",
        "timestamp": 123.0,
        "level": "warning",
        "user_id": 7,
        "username": "bob",
        "message": "did a thing",
        "metadata": {"foo": "bar", "n": 3},
    }
    row = event_logger._event_to_row(event)
    assert row["event_type"] == "create"
    assert row["level"] == "warning"
    assert row["metadata_json"] is not None
    assert "foo" in row["metadata_json"]


def test_drainer_exits_cleanly_on_stop():
    # don't actually spawn a real greenlet, just verify the stop flag flips
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


def test_prune_event_log_builds_delete_query():
    import sys

    fake_db = MagicMock()
    fake_model = MagicMock()
    fake_filter_result = MagicMock()
    fake_model.query.filter.return_value = fake_filter_result
    fake_filter_result.delete.return_value = 5
    # comparing MagicMock < float would raise, so make timestamp a real-valued attr
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
    # remaining queue should still hold the leftover items
    assert q.qsize() == 50


def test_persist_queue_is_bounded():
    event_logger._persist_queue = None
    q = event_logger._get_persist_queue()
    # deque shim caps at _PERSIST_QUEUE_MAXSIZE, so overflow gets dropped, not raised
    for i in range(event_logger._PERSIST_QUEUE_MAXSIZE + 100):
        try:
            q.put_nowait({"event_type": "t", "timestamp": float(i), "level": "info", "message": "m"})
        except Exception:
            pass
    assert q.qsize() <= event_logger._PERSIST_QUEUE_MAXSIZE
