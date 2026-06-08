from __future__ import annotations

import json
import time
import logging
from typing import Callable, Any
from threading import Lock
from collections import deque
from datetime import UTC, datetime

from markupsafe import escape as _markup_escape

logger = logging.getLogger(__name__)


def _esc_passthrough(val: Any) -> Any:
    """html-escape strings, pass through everything else.

    distinct from models._esc, which coerces to str and returns "" for falsy.
    that differing falsy/passthrough behavior is load-bearing, do not merge them
    """
    if isinstance(val, str):
        return str(_markup_escape(val))
    return val


def _esc_deep(obj: Any) -> Any:
    """recursively html-escape all string values (and dict keys) in a structure"""
    if isinstance(obj, dict):
        return {_esc_passthrough(k): _esc_deep(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return type(obj)(_esc_deep(v) for v in obj)
    return _esc_passthrough(obj)


EventDict = dict[str, int | float | str | bool | None | dict[str, int | float | str | bool | None]]
EventListener = Callable[[EventDict], None]


# bounded so non-leader workers (which don't drain) can't grow without bound
_PERSIST_QUEUE_MAXSIZE = 10000
_persist_queue: Any = None
_persist_queue_lock = Lock()
_drainer_stop = False


def _get_persist_queue() -> Any:
    """lazy-init the persistence queue, prefers gevent.queue.Queue when available"""
    global _persist_queue
    if _persist_queue is not None:
        return _persist_queue
    with _persist_queue_lock:
        if _persist_queue is not None:
            return _persist_queue
        try:
            import gevent.queue

            _persist_queue = gevent.queue.Queue(maxsize=_PERSIST_QUEUE_MAXSIZE)
        except Exception:
            # fall back to a plain deque in environments without gevent (unit tests)
            _persist_queue = _DequeQueue(maxsize=_PERSIST_QUEUE_MAXSIZE)
    return _persist_queue


class _DequeQueue:
    """minimal queue shim with put_nowait/get_nowait/qsize, used when gevent is unavailable"""

    def __init__(self, maxsize: int) -> None:
        self._dq: deque = deque(maxlen=maxsize)
        self._lock = Lock()

    def put_nowait(self, item: Any) -> None:
        with self._lock:
            self._dq.append(item)

    def get_nowait(self) -> Any:
        with self._lock:
            if not self._dq:
                raise IndexError("empty")
            return self._dq.popleft()

    def qsize(self) -> int:
        with self._lock:
            return len(self._dq)

    def empty(self) -> bool:
        with self._lock:
            return not self._dq


def _event_to_row(event: EventDict) -> dict[str, Any]:
    """flatten an EventDict to the columns of DesktopEventLogModel"""
    metadata = event.get("metadata") or {}
    try:
        meta_json = json.dumps(metadata, default=str) if metadata else None
    except Exception:
        meta_json = None
    # timestamp is always a float at the source; narrow the EventDict union for mypy
    ts = event.get("timestamp") or time.time()
    timestamp = float(ts) if isinstance(ts, (int, float, str)) else time.time()
    return {
        "timestamp": timestamp,
        "event_type": str(event.get("type") or "")[:128],
        "level": str(event.get("level") or "info")[:16],
        "user_id": event.get("user_id"),
        "username": event.get("username"),
        "message": str(event.get("message") or ""),
        "metadata_json": meta_json,
    }


class EventLogger:
    def __init__(self, max_events: int = 2000) -> None:
        self.events: deque[EventDict] = deque(maxlen=max_events)
        self.lock = Lock()
        self.listeners: list[EventListener] = []
        self._next_id: int = 1

    def log_event(
        self,
        event_type: str,
        message: str,
        user_id: int | None = None,
        username: str | None = None,
        level: str = "info",
        metadata: dict[str, int | float | str | bool | None] | None = None,
        user_flags: dict[str, bool] | None = None,
    ) -> EventDict:
        from . import event_bus
        from .models import DISPLAY_DATETIME_FORMAT

        with self.lock:
            event_id = f"{event_bus.WORKER_ID}:{self._next_id}"
            self._next_id += 1

        if user_flags is None:
            user_flags = {}
            if user_id:
                from CTFd.models import Users
                from .models import user_flags as extract_user_flags

                user = Users.query.filter_by(id=user_id).first()
                if user:
                    user_flags = extract_user_flags(user)

        event: EventDict = {
            "id": event_id,
            "timestamp": time.time(),
            "datetime": datetime.now(UTC).strftime(DISPLAY_DATETIME_FORMAT),
            "type": event_type,
            "level": level,
            "message": _esc_passthrough(message),
            "user_id": user_id,
            "username": _esc_passthrough(username),
            **user_flags,
            "metadata": _esc_deep(metadata) if metadata else {},
        }

        self._deliver_local(event)

        try:
            from . import event_bus

            event_bus.publish(event)
        except Exception:
            logger.warning("event bus publish failed", exc_info=True)

        log_msg = f"[{event_type}] {message}"
        if username:
            log_msg = f"[{event_type}] User {username} (ID: {user_id}): {message}"

        if level == "error":
            logger.error(log_msg)
        elif level == "warning":
            logger.warning(log_msg)
        else:
            logger.info(log_msg)

        return event

    def _deliver_local(self, event: EventDict) -> None:
        with self.lock:
            self.events.append(event)
            listeners = self.listeners[:]

        # enqueue for persistence on every delivery (local and cross-worker bus).
        # the drainer only runs on the leader worker, so bounded queue prevents
        # unbounded growth on non-leader workers where nothing is consuming.
        try:
            q = _get_persist_queue()
            q.put_nowait(_event_to_row(event))
        except Exception:
            # queue full or transient error, drop the row rather than block log_event
            pass

        failed: list[EventListener] = []
        for listener in listeners:
            try:
                listener(event)
            except Exception as e:
                logger.warning(f"event listener failed and was removed: {str(e)}")
                failed.append(listener)

        if failed:
            with self.lock:
                for listener in failed:
                    if listener in self.listeners:
                        self.listeners.remove(listener)

    def get_recent_events(self, limit: int = 100) -> list[EventDict]:
        with self.lock:
            events_list = list(self.events)
            return events_list[-limit:] if limit else events_list

    def add_listener(self, callback: EventListener) -> None:
        with self.lock:
            self.listeners.append(callback)

    def remove_listener(self, callback: EventListener) -> None:
        with self.lock:
            if callback in self.listeners:
                self.listeners.remove(callback)


event_logger = EventLogger()


def _drain_batch(q: Any, max_batch: int = 100) -> list[dict[str, Any]]:
    batch: list[dict[str, Any]] = []
    for _ in range(max_batch):
        try:
            batch.append(q.get_nowait())
        except Exception:
            break
    return batch


def start_persistence_drainer(app: Any, interval: float = 1.0, batch_size: int = 100) -> Any:
    """spawn a greenlet that bulk-inserts queued events every `interval` seconds.

    only the leader worker should call this. returns the greenlet handle for tests.
    """
    global _drainer_stop
    _drainer_stop = False

    def _loop() -> None:
        import gevent

        while not _drainer_stop:
            try:
                q = _get_persist_queue()
                batch = _drain_batch(q, batch_size)
                if batch:
                    # Flask-SQLAlchemy's session teardown only fires reliably on REQUEST contexts.
                    # manually-opened app contexts don't auto-remove the scoped session, so each
                    # iteration leaked a connection to the pool. explicit remove() releases it.
                    try:
                        with app.app_context():
                            from CTFd.models import db
                            from .models import DesktopEventLogModel

                            try:
                                db.session.bulk_insert_mappings(DesktopEventLogModel, batch)
                                db.session.commit()
                            except Exception:
                                logger.warning("event log persistence batch failed", exc_info=True)
                                db.session.rollback()
                            finally:
                                db.session.remove()
                    except Exception:
                        logger.warning("event log drainer iteration crashed", exc_info=True)
            except Exception:
                logger.warning("event log drainer iteration crashed", exc_info=True)
            gevent.sleep(interval)

    try:
        import gevent

        return gevent.spawn(_loop)
    except Exception:
        logger.exception("event log drainer: failed to spawn greenlet")
        return None


def stop_persistence_drainer() -> None:
    """signal the drainer to exit on its next iteration, mainly used by tests"""
    global _drainer_stop
    _drainer_stop = True


def prune_event_log(retention_days: int) -> int:
    """delete event log rows older than `retention_days`, returns number deleted"""
    from CTFd.models import db
    from .models import DesktopEventLogModel

    cutoff = time.time() - (retention_days * 86400)
    try:
        deleted = DesktopEventLogModel.query.filter(DesktopEventLogModel.timestamp < cutoff).delete()
        db.session.commit()
        return deleted
    finally:
        # explicit remove so scheduled job doesn't leak its connection
        db.session.remove()
