import os
import sys
import signal
import atexit
import logging
from CTFd.plugins import register_user_page_menu_bar

from .docker_host_manager import DockerHostManager, LOCAL_CONTEXT_NAME, LOCAL_SOCKET_PATH, _get_host_gateway
from .orchestrator import Orchestrator
from .container_manager import ContainerManager
from .routes import create_routes

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
            pub_hostname=_get_host_gateway(),
            weight=1,
            enabled=True,
        )
    )
    db.session.commit()
    logger.info("seeded local docker context")


def _reconcile_containers(app, host_manager, orchestrator):
    import time as _time

    from CTFd.models import db, Users
    from .models import DesktopContainerInfoModel, DesktopSessionHistoryModel

    rows = DesktopContainerInfoModel.query.all()
    removed = 0
    kept = 0

    for row in rows:
        try:
            if host_manager.is_container_running(row.docker_context, row.container_id):
                orchestrator.reserve_slot(row.docker_context)
                kept += 1
            else:
                ended_at = _time.time()
                user = Users.query.filter_by(id=row.user_id).first()
                username = user.name if user else f"User {row.user_id}"
                history = DesktopSessionHistoryModel(
                    user_id=row.user_id,
                    username=username,
                    docker_context=row.docker_context,
                    started_at=row.created_at,
                    ended_at=ended_at,
                    duration=ended_at - row.created_at,
                    end_reason="reconciliation",
                    extensions_used=row.extensions_used,
                )
                db.session.add(history)
                db.session.delete(row)
                removed += 1
        except Exception:
            db.session.delete(row)
            removed += 1

    if removed:
        db.session.commit()

    if removed or kept:
        logger.info(f"reconciled containers on startup: {kept} recovered, {removed} stale records removed")


def _ensure_columns(app):
    # create_all won't alter existing columns, so we handle migrations manually
    from CTFd.models import db
    from sqlalchemy import inspect, text

    with app.app_context():
        insp = inspect(db.engine)
        cols = {c["name"]: c for c in insp.get_columns("desktop_container_info")}
        for col in ("ssh_port", "ttyd_port"):
            if col not in cols:
                db.session.execute(text(f"ALTER TABLE desktop_container_info ADD COLUMN {col} INTEGER"))
                logger.info(f"added {col} column to desktop_container_info")

        # float(32-bit) rounds unix timestamps, need double
        float_fixes = {
            "desktop_container_info": ["created_at", "timer_start_time", "timer_duration"],
            "desktop_session_history": ["started_at", "ended_at", "duration"],
        }
        for table, columns in float_fixes.items():
            try:
                table_cols = {c["name"]: c for c in insp.get_columns(table)}
                for col in columns:
                    col_info = table_cols.get(col)
                    if col_info and str(col_info["type"]).upper() == "FLOAT":
                        nullable = "NULL" if col_info.get("nullable", True) else "NOT NULL"
                        db.session.execute(text(f"ALTER TABLE {table} MODIFY {col} DOUBLE {nullable}"))
                        logger.info(f"upgraded {table}.{col} from FLOAT to DOUBLE")
            except Exception as e:
                logger.debug(f"float upgrade check for {table}: {e}")

        db.session.commit()


def load(app):
    app.db.create_all()
    _ensure_columns(app)

    host_manager = DockerHostManager()
    orchestrator = Orchestrator(host_manager)

    with app.app_context():
        _seed_defaults(app)
        _seed_local_context(app)
        orchestrator.load_from_db()
        _reconcile_containers(app, host_manager, orchestrator)

    container_manager = ContainerManager(host_manager, orchestrator, app)

    app.desktop_host_manager = host_manager
    app.desktop_orchestrator = orchestrator

    remote_desktop_bp = create_routes(container_manager, orchestrator)

    app.register_blueprint(remote_desktop_bp)
    register_user_page_menu_bar("Remote Desktop", "/remote-desktop")

    # register config template in the DictLoader so {% include %} on
    # /admin/config can find it without hardcoding the plugin folder name
    config_tpl = os.path.join(os.path.dirname(__file__), "templates", "remote_desktop_config.html")
    with open(config_tpl) as f:
        app.overridden_templates["remote_desktop_config.html"] = f.read()

    # only when serving HTTP, not CLI commands where scheduler threads prevent exit
    _serving = (
        "gunicorn" in sys.modules or os.environ.get("WERKZEUG_RUN_MAIN") or (len(sys.argv) > 1 and sys.argv[1] == "run")
    )
    if not _serving:
        logger.info("remote desktop plugin loaded (scheduler skipped, CLI mode)")
        return

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

    cmd_log_interval = get_setting("command_log_interval")
    scheduler.add_job(
        func=container_manager.collect_all_command_logs,
        trigger="interval",
        seconds=cmd_log_interval,
        misfire_grace_time=30,
        coalesce=True,
        id="command_log_collection",
    )

    # wrap periodic jobs so they run within app context
    _original_cleanup = container_manager.periodic_cleanup

    def _cleanup_with_context():
        with app.app_context():
            _original_cleanup()

    container_manager.periodic_cleanup = _cleanup_with_context

    _original_collect = container_manager.collect_all_command_logs

    def _collect_with_context():
        with app.app_context():
            _original_collect()

    container_manager.collect_all_command_logs = _collect_with_context

    scheduler.start()

    def _safe_shutdown_scheduler():
        if scheduler.running:
            scheduler.shutdown(wait=False)

    atexit.register(_safe_shutdown_scheduler)

    def signal_handler(signum, _frame):
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
