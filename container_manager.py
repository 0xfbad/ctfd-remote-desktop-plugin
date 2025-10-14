import time
import socket
import logging
import traceback
from threading import Lock
from .event_logger import event_logger

logger = logging.getLogger(__name__)

class ContainerManager:
	def __init__(self, config, orchestrator):
		self.config = config
		self.orchestrator = orchestrator
		self.active_containers = {}
		self.session_timers = {}
		self.creation_status = {}
		self.lock = Lock()

	def wait_for_vnc_ready(self, hostname, novnc_port, max_attempts=None):
		if max_attempts is None:
			max_attempts = self.config['timeouts']['vnc_ready_attempts']
		import urllib.request
		import urllib.error

		for attempt in range(max_attempts):
			try:
				req = urllib.request.Request(f"http://{hostname}:{novnc_port}/", method='GET')
				req.add_header('User-Agent', 'CTFd-VNC-Check')
				with urllib.request.urlopen(req, timeout=self.config['timeouts']['http_request']) as response:
					if response.status == 200:
						logger.info(f"VNC ready on {hostname}:{novnc_port} after {attempt + 1} attempts")
						return True
			except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ConnectionRefusedError):
				pass
			except Exception as e:
				logger.debug(f"VNC check attempt {attempt + 1} error: {str(e)}")

			if attempt < max_attempts - 1:
				time.sleep(0.5)

		logger.warning(f"VNC not ready on {hostname}:{novnc_port} after {max_attempts} attempts")
		return False

	def _create_container_background_wrapper(self, app, user_id):
		with app.app_context():
			self._create_container_background(user_id)

	def _create_container_background(self, user_id):
		logger.info(f"[BACKGROUND TASK STARTED] Creating container for user {user_id}")

		from CTFd.models import Users
		try:
			user = Users.query.filter_by(id=user_id).first()
			username = user.name if user else f"User {user_id}"
			logger.info(f"[BACKGROUND] Retrieved user: {username}")
		except Exception as e:
			logger.error(f"[BACKGROUND] Failed to get user {user_id}: {str(e)}")
			username = f"User {user_id}"

		logger.info(f"[BACKGROUND] Starting container creation for {username}")

		ssh = None
		selected_host = None

		try:
			with self.lock:
				self.creation_status[user_id] = {'status': 'selecting_host', 'message': 'Requesting a server...'}

			selected_host = self.orchestrator.get_next_host()
			hostname = selected_host['hostname']
			pub_hostname = selected_host['pub_hostname']
			display_hostname = hostname.replace('.infra.slugsec.club', '')

			logger.info(f"Selected host: {hostname} (public: {pub_hostname}) for user {user_id}")

			self.orchestrator.reserve_slot(hostname)

			with self.lock:
				self.creation_status[user_id] = {'status': 'connecting', 'message': f'Connecting to {display_hostname}...'}

			ssh = self.orchestrator.checkout_connection(hostname)
			logger.debug(f"Checked out SSH connection to {hostname}")

			container_name = f"kali-desktop-{user_id}-{int(time.time())}"

			resolution = self.config['container_defaults']['resolution']
			shm_size = self.config['container_defaults']['shm_size']
			memory_limit = self.config['container_defaults']['memory_limit']
			cpu_limit = self.config['container_defaults']['cpu_limit']

			docker_cmd = f"""docker run -d --rm --name {container_name} -p 0:5900 -p 0:6080 -e VNC_PASSWORD=ctfdvnc -e RESOLUTION={resolution} --shm-size={shm_size} --memory={memory_limit} --cpus={cpu_limit} {self.config['docker_image']}"""

			with self.lock:
				self.creation_status[user_id] = {'status': 'starting_container', 'message': f'Starting container on {display_hostname}...'}

			logger.info(f"Executing docker command on {hostname}")
			stdin, stdout, stderr = ssh.exec_command(docker_cmd, timeout=self.config['timeouts']['docker_command'])
			container_id = stdout.read().decode().strip()
			error = stderr.read().decode().strip()

			if error and "Error" in error:
				raise Exception(f"Docker error: {error}")

			with self.lock:
				self.creation_status[user_id] = {'status': 'getting_ports', 'message': f'Container started on {display_hostname}...'}

			for attempt in range(5):
				stdin, stdout, stderr = ssh.exec_command(f"docker port {container_name}", timeout=self.config['timeouts']['docker_quick'])
				port_output = stdout.read().decode().strip()

				if port_output:
					break

				if attempt < 4:
					time.sleep(0.3)

			if not port_output:
				raise Exception("Could not get port mappings from Docker")

			vnc_port = None
			novnc_port = None

			for line in port_output.split('\n'):
				if '5900/tcp' in line and '->' in line:
					vnc_port = int(line.split(':')[-1])
				elif '6080/tcp' in line and '->' in line:
					novnc_port = int(line.split(':')[-1])

			if not vnc_port or not novnc_port:
				raise Exception(f"Could not parse port mappings: {port_output}")

			logger.info(f"Container {container_name} created - VNC:{vnc_port} noVNC:{novnc_port}")

			self.orchestrator.checkin_connection(hostname, ssh)
			ssh = None

			with self.lock:
				self.creation_status[user_id] = {'status': 'waiting_vnc', 'message': f'Waiting for {display_hostname} display server...'}

			vnc_ready = self.wait_for_vnc_ready(hostname, novnc_port)

			if not vnc_ready:
				raise Exception(f"VNC server on {hostname}:{novnc_port} did not become ready in time")

			with self.lock:
				self.active_containers[user_id] = {
					'container_id': container_id,
					'container_name': container_name,
					'vnc_port': vnc_port,
					'novnc_port': novnc_port,
					'hostname': hostname,
					'pub_hostname': pub_hostname,
					'created_at': time.time()
				}

				self.session_timers[user_id] = {
					'started': False,
					'start_time': None,
					'duration': 0,
					'extensions_used': 0,
					'max_extensions': self.config['session_defaults']['max_extensions']
				}

				self.creation_status[user_id] = {'status': 'ready', 'message': 'Desktop ready!', 'hostname': display_hostname}

			event_logger.log_event(
				'session_created',
				'remote desktop session created successfully',
				user_id=user_id,
				username=username,
				level='info',
				metadata={
					'hostname': hostname,
					'container_name': container_name,
					'vnc_port': vnc_port,
					'novnc_port': novnc_port
				}
			)

		except Exception as e:
			try:
				if ssh:
					self.orchestrator.checkin_connection(hostname, ssh)
			except Exception as checkin_error:
				logger.error(f"Failed to checkin connection during cleanup: {str(checkin_error)}")

			if selected_host:
				try:
					self.orchestrator.release_slot(selected_host['hostname'])
				except Exception as release_error:
					logger.error(f"Failed to release slot during cleanup: {str(release_error)}")

				try:
					self.orchestrator.mark_unhealthy(selected_host['hostname'])
				except Exception as health_error:
					logger.error(f"Failed to mark host unhealthy during cleanup: {str(health_error)}")

			logger.error(f"Error creating container for user {user_id}: {str(e)}")
			logger.error(traceback.format_exc())

			failed_hostname = None
			if selected_host:
				try:
					failed_hostname = selected_host['hostname'].replace('.infra.slugsec.club', '')
				except:
					pass

			with self.lock:
				self.creation_status[user_id] = {
					'status': 'failed',
					'error': str(e),
					'hostname': failed_hostname
				}

			event_logger.log_event(
				'session_error',
				f'failed to create session: {str(e)}',
				user_id=user_id,
				username=username,
				level='error',
				metadata={'error': str(e), 'traceback': traceback.format_exc()}
			)

	def create_container(self, user_id):
		from CTFd.models import Users
		from flask import current_app
		user = Users.query.filter_by(id=user_id).first()
		username = user.name if user else f"User {user_id}"

		logger.info(f"[MAIN] create_container called for user {user_id} ({username})")

		event_logger.log_event(
			'session_requested',
			'requested remote desktop session',
			user_id=user_id,
			username=username,
			level='info'
		)

		with self.lock:
			self.creation_status[user_id] = {'status': 'queued', 'message': 'Queued...'}

		logger.info(f"[MAIN] Submitting background task for user {user_id}")

		app = current_app._get_current_object()

		try:
			import gevent
			greenlet = gevent.spawn(self._create_container_background_wrapper, app, user_id)
			logger.info(f"[MAIN] Background task submitted successfully via gevent: {greenlet}")
		except Exception as e:
			logger.error(f"[MAIN] Failed to submit background task: {str(e)}")
			logger.error(traceback.format_exc())
			with self.lock:
				self.creation_status[user_id] = {
					'status': 'failed',
					'error': f'Failed to start background task: {str(e)}'
				}
			return {'success': False, 'error': str(e)}

		return {'success': True, 'status': 'creating'}

	def get_creation_status(self, user_id):
		with self.lock:
			return self.creation_status.get(user_id)

	def destroy_container(self, user_id):
		from CTFd.models import Users
		user = Users.query.filter_by(id=user_id).first()
		username = user.name if user else f"User {user_id}"

		container_info = None
		with self.lock:
			if user_id not in self.active_containers:
				return {'success': False, 'error': 'No active container found'}
			container_info = self.active_containers[user_id].copy()

		hostname = container_info['hostname']
		container_name = container_info['container_name']

		ssh = None
		try:
			ssh = self.orchestrator.checkout_connection(hostname)
			ssh.exec_command(f"docker stop {container_name}", timeout=self.config['timeouts']['docker_quick'])
			logger.info(f"Destroyed container {container_name} on {hostname}")
		except Exception as e:
			logger.error(f"Error executing docker commands for user {user_id}: {str(e)}")
		finally:
			if ssh:
				self.orchestrator.checkin_connection(hostname, ssh)

		self.orchestrator.release_slot(hostname)

		with self.lock:
			if user_id in self.active_containers:
				del self.active_containers[user_id]
			if user_id in self.session_timers:
				del self.session_timers[user_id]
			if user_id in self.creation_status:
				del self.creation_status[user_id]

		event_logger.log_event(
			'session_destroyed',
			'remote desktop session destroyed',
			user_id=user_id,
			username=username,
			level='info',
			metadata={'hostname': hostname, 'container_name': container_name}
		)

		return {'success': True}

	def get_container_info(self, user_id):
		with self.lock:
			info = self.active_containers.get(user_id)
			return info.copy() if info else None

	def get_all_containers(self):
		from CTFd.models import Users

		containers = []
		active_containers_copy = {}
		with self.lock:
			active_containers_copy = dict(self.active_containers)

		for user_id, container_info in active_containers_copy.items():
			user = Users.query.filter_by(id=user_id).first()

			timer_status = self.get_session_timer_status(user_id)

			container_data = {
				'user_id': user_id,
				'username': user.name if user else 'Unknown',
				'container_name': container_info['container_name'],
				'container_id': container_info['container_id'],
				'hostname': container_info['hostname'],
				'created_at': container_info['created_at'],
				'vnc_port': container_info['vnc_port'],
				'novnc_port': container_info['novnc_port'],
				'timer': {
					'active': timer_status.get('started', False),
					'time_remaining': timer_status.get('time_remaining', 0),
					'extensions_used': timer_status.get('extensions_used', 0),
					'max_extensions': timer_status.get('max_extensions', 3)
				} if timer_status.get('success') else None
			}
			containers.append(container_data)

		return containers

	def start_session_timer(self, user_id, duration=None):
		if duration is None:
			duration = self.config['session_defaults']['initial_duration']

		with self.lock:
			if user_id not in self.session_timers:
				return {'success': False, 'error': 'No active session'}

			timer = self.session_timers[user_id]
			if timer['started']:
				return {'success': False, 'error': 'Timer already started'}

			timer['started'] = True
			timer['start_time'] = time.time()
			timer['duration'] = duration
			timer['extensions_used'] = 0

			logger.info(f"Started timer for user {user_id}: {duration}s")
			return {'success': True, 'duration': duration}

	def stop_session_timer(self, user_id):
		with self.lock:
			if user_id not in self.session_timers:
				return {'success': False, 'error': 'No active session'}

			timer = self.session_timers[user_id]
			if not timer['started']:
				return {'success': False, 'error': 'Timer not started'}

			timer['started'] = False
			timer['start_time'] = None
			timer['duration'] = 0

			logger.info(f"Stopped timer for user {user_id}")
			return {'success': True}

	def extend_session_timer(self, user_id, new_duration=None):
		from CTFd.models import Users
		user = Users.query.filter_by(id=user_id).first()
		username = user.name if user else f"User {user_id}"

		if new_duration is None:
			new_duration = self.config['session_defaults']['extension_duration']

		with self.lock:
			if user_id not in self.session_timers:
				return {'success': False, 'error': 'No active session'}

			timer = self.session_timers[user_id]
			if not timer['started']:
				return {'success': False, 'error': 'Timer not started'}

			max_extensions = timer['max_extensions']
			if timer['extensions_used'] >= max_extensions:
				return {'success': False, 'error': 'Maximum extensions reached'}

			elapsed = time.time() - timer['start_time']
			remaining = max(0, timer['duration'] - elapsed)
			timer['start_time'] = time.time()
			timer['duration'] = remaining + new_duration
			timer['extensions_used'] += 1

			logger.info(f"Extended timer for user {user_id}: {timer['extensions_used']}/{max_extensions}")

			event_logger.log_event(
				'session_extended',
				f'session extended ({timer["extensions_used"]}/{max_extensions} extensions used)',
				user_id=user_id,
				username=username,
				level='info',
				metadata={
					'extensions_used': timer['extensions_used'],
					'max_extensions': max_extensions,
					'new_duration': new_duration
				}
			)

			return {'success': True, 'extensions_used': timer['extensions_used'], 'max_extensions': max_extensions}

	def get_session_timer_status(self, user_id):
		with self.lock:
			if user_id not in self.session_timers:
				return {'success': False, 'error': 'No active session'}

			timer = self.session_timers[user_id]
			if not timer['started']:
				return {'success': True, 'started': False, 'time_remaining': 0}

			elapsed = time.time() - timer['start_time']
			time_remaining = max(0, timer['duration'] - elapsed)

			if time_remaining <= 0:
				return {'success': True, 'started': False, 'time_remaining': 0, 'expired': True}

			return {
				'success': True,
				'started': True,
				'time_remaining': int(time_remaining),
				'extensions_used': timer['extensions_used'],
				'max_extensions': timer['max_extensions']
			}

	def periodic_cleanup(self):
		from CTFd.models import Users

		user_ids = []
		with self.lock:
			user_ids = list(self.session_timers.keys())

		expired_users = []
		for user_id in user_ids:
			status = self.get_session_timer_status(user_id)
			if status.get('expired'):
				expired_users.append(user_id)

		for user_id in expired_users:
			user = Users.query.filter_by(id=user_id).first()
			username = user.name if user else f"User {user_id}"

			logger.info(f"Auto-destroying expired session for user {user_id}")

			event_logger.log_event(
				'session_expired',
				'session expired and will be destroyed',
				user_id=user_id,
				username=username,
				level='warning'
			)

			try:
				self.destroy_container(user_id)
			except Exception as e:
				logger.error(f"Failed to destroy expired session for user {user_id}: {str(e)}")

	def cleanup_all_containers(self):
		logger.info("Cleaning up all containers on shutdown")

		containers_to_cleanup = {}
		with self.lock:
			containers_to_cleanup = dict(self.active_containers)

		for user_id, container_info in containers_to_cleanup.items():
			try:
				hostname = container_info['hostname']
				container_name = container_info['container_name']

				ssh = None
				try:
					ssh = self.orchestrator.checkout_connection(hostname)
					ssh.exec_command(f"docker stop {container_name}", timeout=self.config['timeouts']['docker_quick'])
					logger.info(f"Cleaned up {container_name}")
				finally:
					if ssh:
						self.orchestrator.checkin_connection(hostname, ssh)

			except Exception as e:
				logger.error(f"Failed to cleanup container for user {user_id}: {str(e)}")

		with self.lock:
			self.active_containers.clear()
			self.session_timers.clear()
			self.creation_status.clear()

		self.orchestrator.cleanup()
		logger.info("Cleanup completed")
