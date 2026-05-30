import json
import sys
import types
from unittest.mock import MagicMock, patch

import pytest

import event_bus


@pytest.fixture(autouse=True)
def _reset_bus_state():
    event_bus._app = None
    event_bus._pub_client = None
    event_bus._subscriber_started = False
    yield
    event_bus._app = None
    event_bus._pub_client = None
    event_bus._subscriber_started = False


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
    assert len(parts[1]) == 8
