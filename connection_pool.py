import os
import paramiko
import logging
from queue import Queue, Empty
from threading import Lock

logger = logging.getLogger(__name__)

class ConnectionPool:
	def __init__(self, hostname, username, max_connections):
		self.hostname = hostname
		self.username = username
		self.max_connections = max_connections
		self.available_connections = Queue()
		self.total_connections = 0
		self.lock = Lock()
		self.ssh_key_paths = [
			'/root/.ssh/id_ed25519',
			'/root/.ssh/id_rsa',
			os.path.expanduser('~/.ssh/id_ed25519'),
			os.path.expanduser('~/.ssh/id_rsa'),
			'/opt/CTFd/.ssh/id_ed25519',
			'/opt/CTFd/.ssh/id_rsa'
		]

	def _create_connection(self):
		ssh = paramiko.SSHClient()
		ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

		connected = False
		for key_path in self.ssh_key_paths:
			if os.path.exists(key_path):
				try:
					ssh.connect(self.hostname, username=self.username, key_filename=key_path, timeout=15)
					connected = True
					logger.debug(f"Connected to {self.hostname} with key {key_path}")
					break
				except (paramiko.AuthenticationException, Exception):
					continue

		if not connected:
			try:
				ssh.connect(self.hostname, username=self.username, timeout=15)
				connected = True
				logger.debug(f"Connected to {self.hostname} with agent")
			except Exception:
				pass

		if not connected:
			raise Exception(f"Could not authenticate to {self.hostname}")

		return ssh

	def _is_connection_valid(self, ssh):
		try:
			transport = ssh.get_transport()
			return transport and transport.is_active()
		except:
			return False

	def checkout(self):
		try:
			ssh = self.available_connections.get_nowait()
			if self._is_connection_valid(ssh):
				return ssh
			else:
				try:
					ssh.close()
				except:
					pass
		except Empty:
			pass

		with self.lock:
			if self.total_connections < self.max_connections:
				self.total_connections += 1
				try:
					return self._create_connection()
				except:
					self.total_connections -= 1
					raise

			ssh = self.available_connections.get(timeout=60)
			if self._is_connection_valid(ssh):
				return ssh
			else:
				try:
					ssh.close()
				except:
					pass
				return self._create_connection()

	def checkin(self, ssh):
		if ssh:
			if self._is_connection_valid(ssh):
				self.available_connections.put(ssh)
				return
			try:
				ssh.close()
			except:
				pass

	def close_all(self):
		while not self.available_connections.empty():
			try:
				ssh = self.available_connections.get_nowait()
				ssh.close()
			except:
				pass
