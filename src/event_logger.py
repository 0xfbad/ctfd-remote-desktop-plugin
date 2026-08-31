from __future__ import annotations

import json
import logging
import os
import time
from typing import Callable, Any
from threading import Lock
from collections import deque
from datetime import UTC, datetime
from queue import Full

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


# Process-local and bounded: each serving worker must run its own drainer.
_PERSIST_QUEUE_MAXSIZE = 10000
_PERSIST_MAX_RETRIES = 3
_PERSIST_BACKOFF_MAX_SECONDS = 30.0
_PERSIST_RETRY_FIELD = "_persist_retry_count"
_BUS_DELIVERY_MARKER = "_rd_bus_delivery"
_persist_queue: Any = None
_persist_queue_lock = Lock()
_drainer_stop = False
_drainer_app: Any = None
_drainer_handle: Any = None
_persistence_stats_lock = Lock()
_persistence_stats: dict[str, int] = {
    "enqueued": 0,
    "persisted": 0,
    "retried": 0,
    "write_failures": 0,
    "dropped_overflow": 0,
    "dropped_retry_exhausted": 0,
}


def _reset_after_fork() -> None:
    """discard inherited in-memory queue state and replace inherited locks"""
    global _persist_queue, _persist_queue_lock, _drainer_stop, _drainer_app, _drainer_handle
    global _persistence_stats_lock, _persistence_stats
    _persist_queue = None
    _persist_queue_lock = Lock()
    _persistence_stats_lock = Lock()
    _persistence_stats = {name: 0 for name in _persistence_stats}
    _drainer_stop = False
    _drainer_app = None
    _drainer_handle = None


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_after_fork)


def _increment_persistence_stat(name: str, amount: int = 1) -> int:
    with _persistence_stats_lock:
        total = _persistence_stats.get(name, 0) + amount
        _persistence_stats[name] = total
        return total


def get_persistence_stats() -> dict[str, int]:
    """return process-local best-effort persistence counters for monitoring"""
    with _persistence_stats_lock:
        stats = dict(_persistence_stats)
    try:
        stats["queue_depth"] = int(_get_persist_queue().qsize())
    except Exception:
        stats["queue_depth"] = -1
    return stats


def _reset_persistence_stats() -> None:
    """reset process-local counters; intended for deterministic tests"""
    with _persistence_stats_lock:
        for name in _persistence_stats:
            _persistence_stats[name] = 0


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
        self._dq: deque = deque()
        self._maxsize = maxsize
        self._lock = Lock()

    def put_nowait(self, item: Any) -> None:
        with self._lock:
            if len(self._dq) >= self._maxsize:
                raise Full
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
        "event_id": str(event.get("id") or "")[:128],
        "timestamp": timestamp,
        "event_type": str(event.get("type") or "")[:128],
        "level": str(event.get("level") or "info")[:16],
        "user_id": event.get("user_id"),
        "username": event.get("username"),
        "message": str(event.get("message") or ""),
        "metadata_json": meta_json,
    }


def _enqueue_persist_row(row: dict[str, Any], *, retry: bool = False, queue: Any = None) -> bool:
    try:
        target_queue = queue if queue is not None else _get_persist_queue()
        target_queue.put_nowait(row)
    except Exception:
        total = _increment_persistence_stat("dropped_overflow")
        # Log the first and power-of-two totals so sustained overload remains
        # visible without turning the warning itself into another event flood.
        if total == 1 or total & (total - 1) == 0:
            logger.error("event persistence queue overflow; dropped rows total=%d", total)
        return False
    _increment_persistence_stat("retried" if retry else "enqueued")
    return True


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
        *,
        persist: bool = True,
    ) -> EventDict:
        from . import event_bus
        from .models import DISPLAY_DATETIME_FORMAT

        with self.lock:
            event_id = f"{event_bus.get_worker_id()}:{self._next_id}"
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

        self._deliver_local(event, persist=persist)

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

    def log_event_sync(
        self,
        event_type: str,
        message: str,
        user_id: int | None = None,
        username: str | None = None,
        level: str = "info",
        metadata: dict[str, int | float | str | bool | None] | None = None,
        user_flags: dict[str, bool] | None = None,
    ) -> EventDict:
        """Publish an event and commit its audit row before returning.

        Most events deliberately use the bounded asynchronous persistence
        queue. Irreversible administrative evidence disposal uses this method so
        the durable intent record exists before Docker is asked to delete the
        object.
        """
        from CTFd.models import db
        from .models import DesktopEventLogModel

        event = self.log_event(
            event_type,
            message,
            user_id=user_id,
            username=username,
            level=level,
            metadata=metadata,
            user_flags=user_flags,
            persist=False,
        )
        try:
            db.session.add(DesktopEventLogModel(**_row_for_insert(_event_to_row(event))))
            db.session.commit()
        except Exception:
            db.session.rollback()
            raise
        return event

    def _deliver_local(self, event: EventDict, persist: bool = True) -> None:
        # Redis fan-out is live-only; the originating worker owns persistence.
        # Explicit persist=False remains available to local callers.
        if event.pop(_BUS_DELIVERY_MARKER, False):
            persist = False

        with self.lock:
            self.events.append(event)
            listeners = self.listeners[:]

        if persist:
            # Persistence is owned by the process that originated the event.
            # Redis only mirrors the live feed to other workers.
            _enqueue_persist_row(_event_to_row(event))

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


def _row_for_insert(row: dict[str, Any]) -> dict[str, Any]:
    """strip queue-only retry metadata before handing a mapping to SQLAlchemy"""
    if _PERSIST_RETRY_FIELD not in row:
        return row
    return {key: value for key, value in row.items() if key != _PERSIST_RETRY_FIELD}


def _write_persist_batch(app: Any, batch: list[dict[str, Any]]) -> bool:
    """attempt one database transaction for a batch, returning success"""
    try:
        with app.app_context():
            from CTFd.models import db
            from .models import DesktopEventLogModel

            committed = False
            try:
                db.session.bulk_insert_mappings(DesktopEventLogModel, [_row_for_insert(row) for row in batch])
                db.session.commit()
                committed = True
            except Exception:
                logger.warning("event log persistence batch failed", exc_info=True)
                try:
                    db.session.rollback()
                except Exception:
                    logger.warning("event log persistence rollback failed", exc_info=True)
            finally:
                try:
                    db.session.remove()
                except Exception:
                    logger.warning("event log persistence session cleanup failed", exc_info=True)
            return committed
    except Exception:
        logger.warning("event log drainer iteration crashed", exc_info=True)
        return False


def _requeue_failed_batch(q: Any, batch: list[dict[str, Any]]) -> None:
    """retry a failed batch a bounded number of times, then make the loss visible"""
    exhausted = 0
    for row in batch:
        attempts = int(row.get(_PERSIST_RETRY_FIELD, 0)) + 1
        if attempts > _PERSIST_MAX_RETRIES:
            exhausted += 1
            continue
        retry_row = dict(row)
        retry_row[_PERSIST_RETRY_FIELD] = attempts
        _enqueue_persist_row(retry_row, retry=True, queue=q)

    if exhausted:
        total = _increment_persistence_stat("dropped_retry_exhausted", exhausted)
        logger.error(
            "event persistence retries exhausted; dropped batch rows=%d total=%d",
            exhausted,
            total,
        )


def _process_persist_batch(app: Any, q: Any, batch: list[dict[str, Any]]) -> bool:
    """write or boundedly requeue a batch; returns whether the write committed"""
    if _write_persist_batch(app, batch):
        _increment_persistence_stat("persisted", len(batch))
        return True

    _increment_persistence_stat("write_failures")
    # The model's unique event_id makes an ambiguous post-commit retry collide
    # instead of creating a duplicate. Batch retry remains best effort: a
    # uniqueness collision is observed as a failed batch, not a confirmed write.
    _requeue_failed_batch(q, batch)
    return False


def _retry_backoff(interval: float, failure_streak: int) -> float:
    base = max(float(interval), 0.1)
    exponent = min(max(failure_streak - 1, 0), 8)
    return min(base * (2**exponent), _PERSIST_BACKOFF_MAX_SECONDS)


def start_persistence_drainer(app: Any, interval: float = 1.0, batch_size: int = 100) -> Any:
    """spawn a greenlet that bulk-inserts queued events every `interval` seconds.

    each serving worker must call this once because its origin queue is process-local.
    returns the greenlet handle for tests.
    """
    global _drainer_stop, _drainer_app, _drainer_handle
    _drainer_stop = False
    _drainer_app = app

    def _loop() -> None:
        import gevent

        failure_streak = 0
        while not _drainer_stop:
            sleep_for = interval
            try:
                q = _get_persist_queue()
                batch = _drain_batch(q, batch_size)
                if batch:
                    if _process_persist_batch(app, q, batch):
                        failure_streak = 0
                    else:
                        failure_streak += 1
                        sleep_for = _retry_backoff(interval, failure_streak)
            except Exception:
                logger.warning("event log drainer iteration crashed", exc_info=True)
                failure_streak += 1
                sleep_for = _retry_backoff(interval, failure_streak)
            gevent.sleep(sleep_for)

    try:
        import gevent

        _drainer_handle = gevent.spawn(_loop)
        return _drainer_handle
    except Exception:
        logger.exception("event log drainer: failed to spawn greenlet")
        return None


def stop_persistence_drainer(deadline_seconds: float = 3.0, batch_size: int = 100) -> int:
    """Stop and deadline-flush this worker's process-local persistence queue.

    The queue is still best effort under hard kills or a prolonged database
    outage, but normal Gunicorn TERM/worker retirement no longer discards rows
    merely because the periodic greenlet was sleeping.
    """
    global _drainer_stop
    _drainer_stop = True
    deadline = time.monotonic() + max(float(deadline_seconds), 0.0)

    handle = _drainer_handle
    if handle is not None:
        try:
            handle.join(timeout=max(0.0, min(1.1, deadline - time.monotonic())))
        except Exception:
            logger.warning("event persistence drainer join failed during shutdown", exc_info=True)

    app = _drainer_app
    q = _get_persist_queue()
    if app is not None:
        while time.monotonic() < deadline:
            batch = _drain_batch(q, batch_size)
            if not batch:
                break
            _process_persist_batch(app, q, batch)

    remaining = int(q.qsize())
    if remaining:
        logger.error("event persistence shutdown deadline expired; queued rows remaining=%d", remaining)
    return remaining


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


def get_persisted_events(limit: int = 100) -> list[EventDict]:
    """Read the durable cross-worker event tail in chronological order."""
    from .models import DesktopEventLogModel, DISPLAY_DATETIME_FORMAT

    bounded_limit = max(0, min(int(limit), 2000))
    if bounded_limit == 0:
        return []
    rows = DesktopEventLogModel.query.order_by(DesktopEventLogModel.timestamp.desc()).limit(bounded_limit).all()
    events: list[EventDict] = []
    for row in reversed(rows):
        try:
            metadata = json.loads(row.metadata_json) if row.metadata_json else {}
        except (TypeError, ValueError, json.JSONDecodeError):
            metadata = {}
        if not isinstance(metadata, dict):
            metadata = {}
        timestamp = float(row.timestamp)
        events.append(
            {
                "id": str(row.event_id),
                "timestamp": timestamp,
                "datetime": datetime.fromtimestamp(timestamp, UTC).strftime(DISPLAY_DATETIME_FORMAT),
                "type": str(row.event_type),
                "level": str(row.level),
                "message": str(row.message),
                "user_id": row.user_id,
                "username": row.username,
                "metadata": metadata,
            }
        )
    return events
