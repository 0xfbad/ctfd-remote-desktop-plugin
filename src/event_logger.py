import time
import logging
from threading import Lock
from collections import deque
from datetime import datetime

logger = logging.getLogger(__name__)


class EventLogger:
    def __init__(self, max_events=2000):
        self.events = deque(maxlen=max_events)
        self.lock = Lock()
        self.listeners = []
        self._next_id = 1

    def log_event(self, event_type, message, user_id=None, username=None, level="info", metadata=None, user_flags=None):
        with self.lock:
            event_id = self._next_id
            self._next_id += 1

        if user_flags is None:
            user_flags = {}
            if user_id:
                try:
                    from CTFd.models import Users

                    user = Users.query.filter_by(id=user_id).first()
                    if user:
                        if user.type == "admin":
                            user_flags["is_admin"] = True
                        if getattr(user, "hidden", False):
                            user_flags["is_hidden"] = True
                        if getattr(user, "banned", False):
                            user_flags["is_banned"] = True
                except Exception:
                    pass

        event = {
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

        failed = []
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

    def get_recent_events(self, limit=100):
        with self.lock:
            events_list = list(self.events)
            return events_list[-limit:] if limit else events_list

    def add_listener(self, callback):
        with self.lock:
            self.listeners.append(callback)

    def remove_listener(self, callback):
        with self.lock:
            if callback in self.listeners:
                self.listeners.remove(callback)


event_logger = EventLogger()
