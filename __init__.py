import os
import sys
import signal
import atexit
import logging
import yaml
from threading import Thread, Event
from CTFd.plugins import register_plugin_assets_directory, register_user_page_menu_bar, register_admin_plugin_menu_bar

from .orchestrator import HostOrchestrator
from .container_manager import ContainerManager
from .routes import create_routes

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

cleanup_stop_event = None
cleanup_thread = None
container_manager = None

def load(app):
	global cleanup_stop_event, cleanup_thread, container_manager

	plugin_dir = os.path.dirname(__file__)

	config_path = os.path.join(plugin_dir, 'config.yml')
	with open(config_path, 'r') as f:
		config = yaml.safe_load(f)

	orchestrator = HostOrchestrator(config)
	container_manager = ContainerManager(config, orchestrator)

	remote_desktop_bp = create_routes(container_manager, orchestrator, config)

	app.register_blueprint(remote_desktop_bp)
	register_plugin_assets_directory(app, base_path="/plugins/remote_desktop/assets/")
	register_user_page_menu_bar("Remote Desktop", "/remote-desktop")
	register_admin_plugin_menu_bar("Remote Desktop", "/remote-desktop/admin")

	def cleanup_worker():
		while not cleanup_stop_event.is_set():
			try:
				container_manager.periodic_cleanup()
			except Exception as e:
				logger.error(f"Periodic cleanup error: {str(e)}")
			cleanup_stop_event.wait(config['timeouts']['cleanup_interval'])

	def signal_handler(signum, frame):
		logger.info(f"Received signal {signum}, cleaning up containers...")
		cleanup_stop_event.set()
		try:
			import gevent
			gevent.spawn(container_manager.cleanup_all_containers)
			gevent.sleep(2)
		except:
			pass
		sys.exit(0)

	signal.signal(signal.SIGTERM, signal_handler)
	signal.signal(signal.SIGINT, signal_handler)

	atexit.register(container_manager.cleanup_all_containers)

	cleanup_stop_event = Event()
	cleanup_thread = Thread(target=cleanup_worker, daemon=True)
	cleanup_thread.start()

	logger.info(f"Remote Desktop plugin loaded with {len(config['workspace_hosts'])} hosts")
