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

	def log_event(self, event_type, message, user_id=None, username=None, level='info', metadata=None):
		event = {
			'timestamp': time.time(),
			'datetime': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
			'type': event_type,
			'level': level,
			'message': message,
			'user_id': user_id,
			'username': username,
			'metadata': metadata or {}
		}

		with self.lock:
			self.events.append(event)

			for listener in self.listeners[:]:
				try:
					listener(event)
				except Exception as e:
					logger.warning(f"event listener failed and was removed: {str(e)}")
					self.listeners.remove(listener)

		log_msg = f"[{event_type}] {message}"
		if username:
			log_msg = f"[{event_type}] User {username} (ID: {user_id}): {message}"

		if level == 'error':
			logger.error(log_msg)
		elif level == 'warning':
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
