from __future__ import annotations

import fcntl
import os
import sys
import signal
import atexit
import logging
import docker
import paramiko
import tempfile
from types import FrameType
from typing import Callable

from flask import Flask
from CTFd.plugins import register_user_page_menu_bar

from .docker_host_manager import DockerHostManager, LOCAL_CONTEXT_NAME, LOCAL_SOCKET_PATH, _get_host_gateway
from .orchestrator import Orchestrator
from .container_manager import ContainerManager
from .routes import create_routes
from . import event_bus

# import the submodule FIRST, before pulling `event_logger` (the instance) into this
# namespace. otherwise line 22 overwrites the package's `event_logger` attribute with the
# instance, and `from . import event_logger as event_logger_module` resolves to the
# instance, not the submodule. then event_logger_module.start_persistence_drainer crashes
from . import event_logger as event_logger_module
from .event_logger import event_logger

# module-global keeps the lock fd alive for the worker's lifetime, kernel releases on exit
_scheduler_lock_fd = None


def _claim_scheduler_leader() -> bool:
    global _scheduler_lock_fd
    lock_path = os.environ.get(
        "REMOTE_DESKTOP_SCHEDULER_LOCK", os.path.join(tempfile.gettempdir(), "ctfd-remote-desktop-scheduler.lock")
    )
    fd = None
    try:
        # open in "a+" so a losing worker doesn't truncate the leader's pid; truncate after flock
        fd = open(lock_path, "a+")
        try:
            fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            fd.close()
            return False
        fd.seek(0)
        fd.truncate()
        fd.write(str(os.getpid()))
        fd.flush()
        _scheduler_lock_fd = fd
        return True
    except OSError:
        if fd is not None:
            fd.close()
        return False


logger = logging.getLogger(__name__)


def _seed_defaults(app: Flask) -> None:
    from CTFd.models import db
    from .models import DesktopSettingsModel, SETTING_DEFAULTS

    existing = {s.key for s in DesktopSettingsModel.query.all()}
    for key, value in SETTING_DEFAULTS.items():
        if key not in existing:
            db.session.add(DesktopSettingsModel(key=key, value=str(value)))
    db.session.commit()


def _seed_local_context(app: Flask) -> None:
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


def _reconcile_containers(app: Flask, host_manager: DockerHostManager, orchestrator: Orchestrator) -> None:
    import time as _time

    from CTFd.models import db, Users
    from .models import (
        DesktopContainerInfoModel,
        DesktopSessionHistoryModel,
        END_REASON_RECONCILIATION,
        username_or_fallback,
    )

    rows = DesktopContainerInfoModel.query.all()
    removed = 0
    kept = 0

    for row in rows:
        try:
            running = host_manager.is_container_running(row.docker_context, row.container_id)
        except (docker.errors.DockerException, paramiko.ssh_exception.SSHException, EOFError, OSError):
            # transient connectivity issue, keep the row and retry on next reconcile cycle
            kept += 1
            continue
        except Exception:
            db.session.delete(row)
            removed += 1
            continue

        if running:
            orchestrator.reserve_slot(row.docker_context)
            kept += 1
        else:
            ended_at = _time.time()
            user = Users.query.filter_by(id=row.user_id).first()
            username = username_or_fallback(user, row.user_id)
            history = DesktopSessionHistoryModel(
                user_id=row.user_id,
                username=username,
                docker_context=row.docker_context,
                started_at=row.created_at,
                ended_at=ended_at,
                duration=ended_at - row.created_at,
                end_reason=END_REASON_RECONCILIATION,
                extensions_used=row.extensions_used,
            )
            db.session.add(history)
            db.session.delete(row)
            removed += 1

    if removed:
        db.session.commit()

    if removed or kept:
        logger.info(f"reconciled containers on startup: {kept} recovered, {removed} stale records removed")


def load(app: Flask) -> None:
    app.db.create_all()  # type: ignore[attr-defined]

    host_manager = DockerHostManager()
    orchestrator = Orchestrator(host_manager)

    with app.app_context():
        _seed_defaults(app)
        _seed_local_context(app)
        orchestrator.load_from_db()
        _reconcile_containers(app, host_manager, orchestrator)

    container_manager = ContainerManager(host_manager, orchestrator, app)

    event_bus.init(app, on_message=event_logger._deliver_local)

    remote_desktop_bp = create_routes(container_manager, orchestrator)

    @remote_desktop_bp.after_request
    def _add_frame_headers(resp):
        resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        resp.headers.setdefault("Content-Security-Policy", "frame-ancestors 'self'")
        return resp

    app.register_blueprint(remote_desktop_bp)
    register_user_page_menu_bar("Remote Desktop", "/remote-desktop")

    # register config template in the DictLoader so {% include %} on
    # /admin/config can find it without hardcoding the plugin folder name
    config_tpl = os.path.join(os.path.dirname(__file__), "templates", "remote_desktop_config.html")
    with open(config_tpl) as f:
        app.overridden_templates["remote_desktop_config.html"] = f.read()  # type: ignore[attr-defined]

    # only when serving HTTP, not CLI commands where scheduler threads prevent exit
    _serving = (
        "gunicorn" in sys.modules or os.environ.get("WERKZEUG_RUN_MAIN") or (len(sys.argv) > 1 and sys.argv[1] == "run")
    )
    if not _serving:
        logger.info("remote desktop plugin loaded (scheduler skipped, CLI mode)")
        return

    # leader election so the cleanup/health/log-collection jobs fire once, not WORKERS times
    if not _claim_scheduler_leader():
        logger.info("remote desktop plugin loaded (scheduler skipped, another worker holds the leader lock)")
        return

    from .models import get_setting
    from apscheduler.schedulers.gevent import GeventScheduler

    scheduler = GeventScheduler()

    def _with_app_ctx(fn: Callable[[], None]) -> Callable[[], None]:
        def wrapper() -> None:
            with app.app_context():
                fn()

        return wrapper

    cleanup_interval = get_setting("cleanup_interval")

    scheduler.add_job(
        func=_with_app_ctx(container_manager.periodic_cleanup),
        trigger="interval",
        seconds=cleanup_interval,
        misfire_grace_time=30,
        coalesce=True,
        id="expiry_check",
    )

    scheduler.add_job(
        func=_with_app_ctx(orchestrator.health_check),
        trigger="interval",
        seconds=30,
        misfire_grace_time=30,
        coalesce=True,
        id="health_check",
    )

    cmd_log_interval = get_setting("command_log_interval")
    scheduler.add_job(
        func=_with_app_ctx(container_manager.collect_all_command_logs),
        trigger="interval",
        seconds=cmd_log_interval,
        misfire_grace_time=30,
        coalesce=True,
        id="command_log_collection",
    )

    # leader-only: drain queued events into persistent storage so the audit trail
    # survives worker restarts and is queryable across workers
    event_logger_module.start_persistence_drainer(app)

    def _prune_event_log() -> None:
        days = get_setting("retention_days")
        try:
            days_int = int(days) if days is not None else 60
        except (TypeError, ValueError):
            days_int = 60
        event_logger_module.prune_event_log(days_int)

    scheduler.add_job(
        func=_with_app_ctx(_prune_event_log),
        trigger="interval",
        seconds=86400,
        misfire_grace_time=3600,
        coalesce=True,
        id="event_log_prune",
    )

    scheduler.start()

    # GeventScheduler.shutdown raises BlockingSwitchOutError when called from atexit/signal
    # (no active greenlet). process is exiting either way, so just swallow it
    def _safe_shutdown_scheduler() -> None:
        if not scheduler.running:
            return
        try:
            scheduler.shutdown(wait=False)
        except Exception:
            pass

    atexit.register(_safe_shutdown_scheduler)

    def signal_handler(signum: int, _frame: FrameType | None) -> None:
        logger.info(f"received signal {signum}, cleaning up containers...")
        if scheduler.running:
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
