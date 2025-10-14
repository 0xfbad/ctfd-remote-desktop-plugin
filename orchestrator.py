import logging
from threading import Lock
from collections import defaultdict
from .connection_pool import ConnectionPool
from .event_logger import event_logger

logger = logging.getLogger(__name__)

class HostOrchestrator:
	def __init__(self, config):
		self.config = config
		self.hosts = config['workspace_hosts']
		self.host_pools = {}
		self.host_container_counts = defaultdict(int)
		self.host_health = {}
		self.global_lock = Lock()

		max_connections = config['connection_pool']['max_connections_per_host']
		for host in self.hosts:
			hostname = host['hostname']
			self.host_pools[hostname] = ConnectionPool(hostname, host['user'], max_connections)
			self.host_health[hostname] = {'healthy': True}

		logger.info(f"Orchestrator initialized with {len(self.hosts)} hosts")

	def test_host_connectivity(self):
		docker_image = self.config.get('docker_image', 'unknown')
		healthy_count = 0

		for host in self.hosts:
			hostname = host['hostname']
			ssh = None
			reason = None

			try:
				ssh = self.checkout_connection(hostname)

				stdin, stdout, stderr = ssh.exec_command("docker ps", timeout=5)
				exit_status = stdout.channel.recv_exit_status()
				if exit_status != 0:
					reason = "docker daemon not accessible"
					raise Exception(reason)

				stdin, stdout, stderr = ssh.exec_command(f"docker image inspect {docker_image}", timeout=5)
				exit_status = stdout.channel.recv_exit_status()
				if exit_status != 0:
					reason = "image not found"
					raise Exception(reason)

				self.checkin_connection(hostname, ssh)
				ssh = None
				healthy_count += 1

				event_logger.log_event(
					'host_healthy',
					f'host {hostname} is healthy',
					level='info',
					metadata={'hostname': hostname}
				)

			except Exception as e:
				if ssh:
					try:
						self.checkin_connection(hostname, ssh)
					except:
						pass

				if not reason:
					reason = "ssh connection failed"

				self.mark_unhealthy(hostname, log_event=False)

				event_logger.log_event(
					'host_unhealthy',
					f'host {hostname} marked unhealthy: {reason}',
					level='warning',
					metadata={'hostname': hostname, 'reason': reason}
				)

		logger.info(f"Host connectivity test complete: {healthy_count}/{len(self.hosts)} hosts healthy")

	def get_next_host(self):
		with self.global_lock:
			available_hosts = []

			for host in self.hosts:
				hostname = host['hostname']

				if not self.host_health[hostname]['healthy']:
					continue

				current_count = self.host_container_counts[hostname]
				available_hosts.append((host, current_count, hostname))

			if not available_hosts:
				raise Exception("No healthy hosts available")

			available_hosts.sort(key=lambda x: (x[1], x[2]))
			return available_hosts[0][0]

	def reserve_slot(self, hostname):
		with self.global_lock:
			self.host_container_counts[hostname] += 1
			logger.debug(f"Reserved slot on {hostname}, now {self.host_container_counts[hostname]} containers")

	def release_slot(self, hostname):
		with self.global_lock:
			if self.host_container_counts[hostname] > 0:
				self.host_container_counts[hostname] -= 1
				logger.debug(f"Released slot on {hostname}, now {self.host_container_counts[hostname]} containers")

	def checkout_connection(self, hostname):
		pool = self.host_pools.get(hostname)
		if pool:
			return pool.checkout()
		raise Exception(f"No connection pool for {hostname}")

	def checkin_connection(self, hostname, ssh):
		pool = self.host_pools.get(hostname)
		if pool:
			pool.checkin(ssh)

	def mark_unhealthy(self, hostname, log_event=True):
		with self.global_lock:
			self.host_health[hostname]['healthy'] = False
			logger.warning(f"Host {hostname} marked as unhealthy")

			if log_event:
				event_logger.log_event(
					'host_unhealthy',
					f'host {hostname} marked as unhealthy',
					level='warning',
					metadata={'hostname': hostname}
				)

	def mark_healthy(self, hostname):
		with self.global_lock:
			self.host_health[hostname]['healthy'] = True
			logger.info(f"Host {hostname} marked as healthy")

			event_logger.log_event(
				'host_healthy',
				f'host {hostname} marked as healthy',
				level='info',
				metadata={'hostname': hostname}
			)

	def get_host_status(self):
		with self.global_lock:
			status = []
			for host in self.hosts:
				hostname = host['hostname']
				container_count = self.host_container_counts[hostname]
				healthy = self.host_health[hostname]['healthy']

				status.append({
					'hostname': hostname,
					'pub_hostname': host['pub_hostname'],
					'active_containers': container_count,
					'healthy': healthy,
					'available': healthy
				})
			return status

	def cleanup(self):
		for pool in self.host_pools.values():
			pool.close_all()
		logger.info("Orchestrator cleanup completed")
