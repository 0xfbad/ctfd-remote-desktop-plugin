import sys
import socket
import signal
import atexit
import logging
from CTFd.plugins import register_plugin_assets_directory, register_user_page_menu_bar, register_admin_plugin_menu_bar

from .docker_host_manager import DockerHostManager, LOCAL_CONTEXT_NAME, LOCAL_SOCKET_PATH
from .orchestrator import Orchestrator
from .container_manager import ContainerManager
from .routes import create_routes

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)


def _seed_defaults(app):
    from CTFd.models import db
    from .models import DesktopSettingsModel, SETTING_DEFAULTS

    existing = {s.key for s in DesktopSettingsModel.query.all()}
    for key, value in SETTING_DEFAULTS.items():
        if key not in existing:
            db.session.add(DesktopSettingsModel(key=key, value=str(value)))
    db.session.commit()


def _seed_local_context(app):
    from CTFd.models import db
    from .models import DesktopDockerContextModel

    if DesktopDockerContextModel.query.count() > 0:
        return

    import docker as docker_lib

    try:
        client = docker_lib.DockerClient(base_url=f"unix://{LOCAL_SOCKET_PATH}")
        client.ping()
        client.close()
    except Exception:
        return

    db.session.add(
        DesktopDockerContextModel(
            context_name=LOCAL_CONTEXT_NAME,
            hostname=None,
            pub_hostname=socket.gethostname(),
            weight=1,
            enabled=True,
        )
    )
    db.session.commit()
    logger.info("seeded local docker context")


def _reconcile_containers(app, host_manager, orchestrator):
    from CTFd.models import db
    from .models import DesktopContainerInfoModel

    rows = DesktopContainerInfoModel.query.all()
    removed = 0
    kept = 0

    for row in rows:
        try:
            if host_manager.is_container_running(row.docker_context, row.container_id):
                # container still alive, count it in the orchestrator
                orchestrator.reserve_slot(row.docker_context)
                kept += 1
            else:
                db.session.delete(row)
                removed += 1
        except Exception:
            db.session.delete(row)
            removed += 1

    if removed:
        db.session.commit()

    if removed or kept:
        logger.info(f"reconciled containers on startup: {kept} recovered, {removed} stale records removed")


def load(app):
    app.db.create_all()

    host_manager = DockerHostManager()
    orchestrator = Orchestrator(host_manager)

    with app.app_context():
        _seed_defaults(app)
        _seed_local_context(app)
        orchestrator.load_from_db()
        _reconcile_containers(app, host_manager, orchestrator)

    container_manager = ContainerManager(host_manager, orchestrator)

    app.desktop_host_manager = host_manager
    app.desktop_orchestrator = orchestrator

    remote_desktop_bp = create_routes(container_manager, orchestrator)

    app.register_blueprint(remote_desktop_bp)
    register_plugin_assets_directory(app, base_path="/plugins/remote_desktop/assets/")
    register_user_page_menu_bar("Remote Desktop", "/remote-desktop")
    register_admin_plugin_menu_bar("Remote Desktop", "/remote-desktop/admin")

    # scheduler setup
    from .models import get_setting

    try:
        from apscheduler.schedulers.gevent import GeventScheduler

        scheduler = GeventScheduler()
    except ImportError:
        from apscheduler.schedulers.background import BackgroundScheduler

        scheduler = BackgroundScheduler()

    cleanup_interval = get_setting("cleanup_interval")

    scheduler.add_job(
        func=container_manager.periodic_cleanup,
        trigger="interval",
        seconds=cleanup_interval,
        misfire_grace_time=30,
        coalesce=True,
        id="expiry_check",
    )

    scheduler.add_job(
        func=orchestrator.health_check,
        trigger="interval",
        seconds=30,
        misfire_grace_time=30,
        coalesce=True,
        id="health_check",
    )

    # wrap periodic_cleanup so it runs within app context
    _original_cleanup = container_manager.periodic_cleanup

    def _cleanup_with_context():
        with app.app_context():
            _original_cleanup()

    container_manager.periodic_cleanup = _cleanup_with_context

    scheduler.start()
    atexit.register(lambda: scheduler.shutdown(wait=False))

    def signal_handler(signum, frame):
        logger.info(f"received signal {signum}, cleaning up containers...")
        try:
            scheduler.shutdown(wait=False)
        except Exception:
            pass
        try:
            with app.app_context():
                container_manager.cleanup_all_containers()
        except Exception:
            pass
        sys.exit(0)

    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    logger.info("remote desktop plugin loaded")
