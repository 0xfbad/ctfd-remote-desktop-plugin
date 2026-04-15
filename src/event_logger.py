from __future__ import annotations

import time
import logging
from typing import Callable
from threading import Lock
from collections import deque
from datetime import datetime

logger = logging.getLogger(__name__)

EventDict = dict[str, int | float | str | bool | None | dict[str, int | float | str | bool | None]]
EventListener = Callable[[EventDict], None]


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
        with self.lock:
            event_id = self._next_id
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
            "datetime": datetime.now().strftime("%b %-d, %Y %-I:%M:%S %p"),
            "type": event_type,
            "level": level,
            "message": message,
            "user_id": user_id,
            "username": username,
            **user_flags,
            "metadata": metadata or {},
        }

        with self.lock:
            self.events.append(event)
            listeners = self.listeners[:]

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
