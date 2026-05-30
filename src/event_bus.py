from __future__ import annotations

import json
import logging
import os
import secrets
import threading
import time
from collections.abc import Callable

logger = logging.getLogger(__name__)

CHANNEL = "ctfd_remote_desktop:events"

# unique per-process token, used by subscribers to skip messages they themselves published
WORKER_ID = f"{os.getpid()}-{secrets.token_hex(4)}"

_app = None
_pub_client = None
_pub_lock = threading.Lock()
_subscriber_started = False
_subscriber_lock = threading.Lock()


def init(app, on_message: Callable[[dict], None] | None = None) -> None:
    """call from plugin load. captures app for greenlet context, optionally starts subscriber"""
    global _app
    _app = app
    if on_message is not None:
        start_subscriber(on_message)


def _get_redis_url():
    if _app is None:
        return None
    return _app.config.get("CACHE_REDIS_URL") or _app.config.get("REDIS_URL")


def _get_publish_client():
    """publish client has a short socket_timeout so a hung redis can't park the request greenlet"""
    global _pub_client
    if _pub_client is not None:
        return _pub_client
    with _pub_lock:
        if _pub_client is not None:
            return _pub_client
        url = _get_redis_url()
        if not url:
            return None
        try:
            import redis

            client = redis.from_url(url, decode_responses=True, socket_timeout=2, socket_connect_timeout=2)
            client.ping()
            _pub_client = client
        except Exception:
            logger.warning("event bus: publish client init failed, falling back to local-only delivery", exc_info=True)
            _pub_client = None
    return _pub_client


def _new_subscribe_client():
    """subscribe client uses no socket_timeout so pubsub.listen() can block forever waiting for messages"""
    url = _get_redis_url()
    if not url:
        return None
    try:
        import redis

        client = redis.from_url(url, decode_responses=True, socket_connect_timeout=2, socket_keepalive=True)
        client.ping()
        return client
    except Exception:
        logger.warning("event bus: subscribe client init failed", exc_info=True)
        return None


def publish(event: dict) -> bool:
    """fire-and-forget cross-worker publish. returns True if message was sent, False otherwise"""
    client = _get_publish_client()
    if client is None:
        return False
    payload = dict(event)
    payload["_origin"] = WORKER_ID
    try:
        client.publish(CHANNEL, json.dumps(payload, default=str))
        return True
    except Exception:
        logger.warning("event bus publish failed", exc_info=True)
        return False


def start_subscriber(on_message: Callable[[dict], None]) -> None:
    """idempotently spawn a greenlet that subscribes and delivers each remote event to on_message"""
    global _subscriber_started
    if _subscriber_started:
        return
    with _subscriber_lock:
        if _subscriber_started:
            return
        if _app is None:
            return
        try:
            import gevent

            gevent.spawn(_subscriber_loop, on_message)
            _subscriber_started = True
        except Exception:
            logger.exception("event bus: failed to spawn subscriber greenlet")


def _subscriber_loop(on_message: Callable[[dict], None]) -> None:
    backoff = 1.0
    while True:
        pubsub = None
        client = None
        try:
            client = _new_subscribe_client()
            if client is None:
                time.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
                continue
            pubsub = client.pubsub(ignore_subscribe_messages=True)
            pubsub.subscribe(CHANNEL)
            backoff = 1.0
            assert _app is not None  # start_subscriber guarantees this
            with _app.app_context():
                for msg in pubsub.listen():
                    if not msg or msg.get("type") != "message":
                        continue
                    try:
                        event = json.loads(msg["data"])
                    except Exception:
                        logger.warning("event bus: malformed message, dropping")
                        continue
                    if event.get("_origin") == WORKER_ID:
                        continue
                    event.pop("_origin", None)
                    try:
                        on_message(event)
                    except Exception:
                        logger.exception("event bus: subscriber callback failed")
        except Exception:
            logger.warning("event bus: subscriber loop crashed, reconnecting", exc_info=True)
            time.sleep(backoff)
            backoff = min(backoff * 2, 30.0)
        finally:
            if pubsub is not None:
                try:
                    pubsub.close()
                except Exception:
                    pass
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass
