import sys
import signal
import atexit
import logging
from threading import Thread, Event
from CTFd.plugins import register_plugin_assets_directory, register_user_page_menu_bar, register_admin_plugin_menu_bar

from .docker_host_manager import DockerHostManager
from .orchestrator import Orchestrator
from .container_manager import ContainerManager
from .routes import create_routes

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

cleanup_stop_event = None
cleanup_thread = None
container_manager = None


def load(app):
	global cleanup_stop_event, cleanup_thread, container_manager

	app.db.create_all()

	host_manager = DockerHostManager()
	orchestrator = Orchestrator(host_manager)

	with app.app_context():
		orchestrator.load_from_db()

	container_manager = ContainerManager(host_manager, orchestrator)

	app.desktop_host_manager = host_manager
	app.desktop_orchestrator = orchestrator

	remote_desktop_bp = create_routes(container_manager, orchestrator)

	app.register_blueprint(remote_desktop_bp)
	register_plugin_assets_directory(app, base_path="/plugins/remote_desktop/assets/")
	register_user_page_menu_bar("Remote Desktop", "/remote-desktop")
	register_admin_plugin_menu_bar("Remote Desktop", "/remote-desktop/admin")

	def cleanup_worker():
		from .models import get_setting
		while not cleanup_stop_event.is_set():
			try:
				with app.app_context():
					container_manager.periodic_cleanup()
			except Exception as e:
				logger.error(f"periodic cleanup error: {e}")
			interval = int(get_setting('cleanup_interval', '300'))
			cleanup_stop_event.wait(interval)

	def signal_handler(signum, frame):
		logger.info(f"received signal {signum}, cleaning up containers...")
		cleanup_stop_event.set()
		try:
			import gevent
			gevent.spawn(container_manager.cleanup_all_containers)
			gevent.sleep(2)
		except Exception:
			pass
		sys.exit(0)

	signal.signal(signal.SIGTERM, signal_handler)
	signal.signal(signal.SIGINT, signal_handler)

	atexit.register(container_manager.cleanup_all_containers)

	cleanup_stop_event = Event()
	cleanup_thread = Thread(target=cleanup_worker, daemon=True)
	cleanup_thread.start()

	logger.info("remote desktop plugin loaded")
