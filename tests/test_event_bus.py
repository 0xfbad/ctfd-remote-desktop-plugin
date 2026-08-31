import json
import sys
import types
from unittest.mock import MagicMock, patch

import pytest

import event_bus
import event_logger


@pytest.fixture(autouse=True)
def _reset_bus_state():
    original_worker_id = event_bus.WORKER_ID
    original_persist_queue = event_logger._persist_queue
    original_persistence_stats = event_logger.get_persistence_stats()
    original_persistence_stats.pop("queue_depth", None)
    event_bus._app = None
    event_bus._pub_client = None
    event_bus._subscriber_started = False
    yield
    event_bus._app = None
    event_bus._pub_client = None
    event_bus._subscriber_started = False
    event_bus.WORKER_ID = original_worker_id
    event_logger._persist_queue = original_persist_queue
    with event_logger._persistence_stats_lock:
        event_logger._persistence_stats.clear()
        event_logger._persistence_stats.update(original_persistence_stats)


def test_publish_without_init_is_noop():
    assert event_bus.publish({"type": "session_created"}) is False


def test_publish_without_redis_url_returns_false():
    app = MagicMock()
    app.config.get.return_value = None
    event_bus.init(app)
    assert event_bus.publish({"type": "x"}) is False


def test_publish_serializes_event_and_tags_origin():
    app = MagicMock()
    app.config.get.side_effect = lambda k: "redis://localhost:6379/0" if k == "CACHE_REDIS_URL" else None
    event_bus.init(app)

    fake_client = MagicMock()
    fake_redis = types.ModuleType("redis")
    fake_redis.from_url = MagicMock(return_value=fake_client)
    with patch.dict(sys.modules, {"redis": fake_redis}):
        assert event_bus.publish({"type": "session_created", "id": 1}) is True

    fake_client.publish.assert_called_once()
    channel, payload = fake_client.publish.call_args.args
    assert channel == event_bus.CHANNEL
    decoded = json.loads(payload)
    assert decoded["type"] == "session_created"
    assert decoded["_origin"] == event_bus.WORKER_ID


def test_publish_swallows_redis_errors():
    app = MagicMock()
    app.config.get.side_effect = lambda k: "redis://localhost:6379/0" if k == "CACHE_REDIS_URL" else None
    event_bus.init(app)

    fake_client = MagicMock()
    fake_client.publish.side_effect = RuntimeError("boom")
    fake_redis = types.ModuleType("redis")
    fake_redis.from_url = MagicMock(return_value=fake_client)
    with patch.dict(sys.modules, {"redis": fake_redis}):
        assert event_bus.publish({"type": "x"}) is False


def test_worker_id_format():
    parts = event_bus.WORKER_ID.split("-")
    assert len(parts) == 2
    assert parts[0].isdigit()
    assert len(parts[1]) == 32


def test_worker_id_is_stable_for_worker_lifetime():
    assert event_bus.get_worker_id() == event_bus.WORKER_ID
    assert event_bus.get_worker_id() == event_bus.WORKER_ID


def test_bus_delivery_is_live_only_and_not_reenqueued():
    event_logger._persist_queue = event_logger._DequeQueue(maxsize=10)
    event_logger._reset_persistence_stats()
    receiver = event_logger.EventLogger()
    event = {
        "_origin": "remote-worker",
        "id": "remote-worker:1",
        "type": "session_created",
        "message": "created",
        "timestamp": 123.0,
        "level": "info",
    }

    assert event_bus._deliver_bus_event(receiver._deliver_local, event) is True

    assert receiver.get_recent_events()[0]["type"] == "session_created"
    assert event_logger._get_persist_queue().qsize() == 0
    assert event_logger.get_persistence_stats()["enqueued"] == 0
    assert event_bus._BUS_DELIVERY_MARKER not in receiver.get_recent_events()[0]
