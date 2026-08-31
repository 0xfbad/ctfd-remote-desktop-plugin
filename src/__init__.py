from __future__ import annotations

import atexit
import errno
import fcntl
import hashlib
import logging
import os
import shlex
import sys
import tempfile
import threading
from typing import Callable

import docker
import paramiko
from flask import Flask
from CTFd.plugins import register_user_page_menu_bar

from .docker_host_manager import DockerHostManager, LOCAL_CONTEXT_NAME, LOCAL_SOCKET_PATH, _get_host_gateway
from .orchestrator import Orchestrator
from .container_manager import ContainerManager
from .routes import create_routes
from . import event_bus
from .database import prepare_database
from .database import validate_database_schema

# import the submodule FIRST, before pulling `event_logger` (the instance) into this
# namespace. otherwise line 22 overwrites the package's `event_logger` attribute with the
# instance, and `from . import event_logger as event_logger_module` resolves to the
# instance, not the submodule. then event_logger_module.start_persistence_drainer crashes
from . import event_logger as event_logger_module
from .event_logger import event_logger

# MariaDB named locks and local file locks are connection/descriptor scoped.
# Keeping the owner alive here makes leadership durable for the worker lifetime;
# every contender re-checks automatically on each scheduled tick so failover
# requires no replica-specific configuration.
_scheduler_state_lock = threading.Lock()
_scheduler_lock_connection = None
_scheduler_lock_fd = None
_scheduler_lock_name: str | None = None
_scheduler_release_registered = False


def _invalidate_scheduler_connection(connection, error: BaseException) -> None:
    """Physically discard a named-lock connection after an uncertain result."""
    try:
        # Connection.close() normally returns the DBAPI connection to
        # SQLAlchemy's pool. MariaDB named locks survive that pool reset, so an
        # errored ownership/acquisition check must invalidate the underlying
        # connection instead of potentially stranding leadership in the pool.
        connection.invalidate(error)
    except Exception:
        logger.warning("scheduler database lock connection invalidation failed", exc_info=True)
    finally:
        try:
            connection.close()
        except Exception:
            logger.warning("scheduler database lock connection close failed", exc_info=True)


def _release_scheduler_leader() -> None:
    global _scheduler_lock_connection, _scheduler_lock_fd, _scheduler_lock_name
    with _scheduler_state_lock:
        connection = _scheduler_lock_connection
        fd = _scheduler_lock_fd
        lock_name = _scheduler_lock_name
        _scheduler_lock_connection = None
        _scheduler_lock_fd = None
        _scheduler_lock_name = None

        if connection is not None:
            try:
                from sqlalchemy import text

                connection.execute(text("SELECT RELEASE_LOCK(:lock_name)"), {"lock_name": lock_name})
            except Exception as exc:
                logger.warning("scheduler database lock release failed", exc_info=True)
                _invalidate_scheduler_connection(connection, exc)
            else:
                connection.close()
        if fd is not None:
            try:
                fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
            finally:
                fd.close()


def _register_scheduler_release() -> None:
    global _scheduler_release_registered
    if not _scheduler_release_registered:
        atexit.register(_release_scheduler_leader)
        _scheduler_release_registered = True


def _claim_mariadb_scheduler_leader(app: Flask) -> bool:
    global _scheduler_lock_connection, _scheduler_lock_name
    from sqlalchemy import text

    if _scheduler_lock_connection is not None:
        try:
            owns_lock = _scheduler_lock_connection.execute(
                text("SELECT IS_USED_LOCK(:lock_name) = CONNECTION_ID()"),
                {"lock_name": _scheduler_lock_name},
            ).scalar()
            if owns_lock == 1:
                return True
        except Exception as exc:
            logger.warning("scheduler database leadership check failed", exc_info=True)
            _invalidate_scheduler_connection(_scheduler_lock_connection, exc)
            _scheduler_lock_connection = None
            _scheduler_lock_name = None
        else:
            try:
                _scheduler_lock_connection.close()
            finally:
                _scheduler_lock_connection = None
                _scheduler_lock_name = None

    connection = app.db.engine.connect()
    try:
        database_name = connection.execute(text("SELECT DATABASE()"), {}).scalar()
        if not database_name:
            raise RuntimeError("scheduler leadership requires a selected MariaDB database")
        database_key = hashlib.sha256(str(database_name).encode()).hexdigest()[:24]
        lock_name = f"ctfd_remote_desktop.scheduler.{database_key}"
        acquired = connection.execute(
            text("SELECT GET_LOCK(:lock_name, 0)"),
            {"lock_name": lock_name},
        ).scalar()
        if acquired != 1:
            connection.close()
            return False
        _scheduler_lock_connection = connection
        _scheduler_lock_name = lock_name
        _register_scheduler_release()
        return True
    except Exception as exc:
        # The server may have granted GET_LOCK before the client observed an
        # error. A normal pooled close would not release that session lock.
        _invalidate_scheduler_connection(connection, exc)
        raise


def _claim_file_scheduler_leader(app: Flask, database_url: str) -> bool:
    global _scheduler_lock_fd
    if _scheduler_lock_fd is not None:
        return True

    database_path = getattr(getattr(app.db.engine, "url", None), "database", None)
    if isinstance(database_path, str) and database_path not in {"", ":memory:"}:
        lock_path = os.path.abspath(database_path) + ".ctfd-remote-desktop-scheduler.lock"
    else:
        database_key = hashlib.sha256(database_url.encode()).hexdigest()[:24]
        lock_path = os.path.join(tempfile.gettempdir(), f"ctfd-remote-desktop-scheduler-{database_key}.lock")

    fd = None
    try:
        fd = open(lock_path, "a+")
        try:
            fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            fd.close()
            return False
        except OSError as exc:
            fd.close()
            if exc.errno in {errno.EACCES, errno.EAGAIN}:
                return False
            raise RuntimeError(f"scheduler leader lock failed: {exc}") from exc
        fd.seek(0)
        fd.truncate()
        fd.write(str(os.getpid()))
        fd.flush()
        _scheduler_lock_fd = fd
        _register_scheduler_release()
        return True
    except OSError as exc:
        if fd is not None and not fd.closed:
            fd.close()
        raise RuntimeError(f"scheduler leader lock could not be opened: {exc}") from exc


def _claim_scheduler_leader(app: Flask) -> bool:
    database_url = app.config.get("SQLALCHEMY_DATABASE_URI")
    if not isinstance(database_url, str):
        raise RuntimeError("scheduler leadership requires SQLALCHEMY_DATABASE_URI")

    with _scheduler_state_lock:
        if database_url.startswith(("mysql", "mariadb")):
            return _claim_mariadb_scheduler_leader(app)
        if database_url.startswith("sqlite"):
            return _claim_file_scheduler_leader(app, database_url)
        raise RuntimeError("scheduler leadership supports MariaDB/MySQL or SQLite databases")


logger = logging.getLogger(__name__)


def _prepare_database(app: Flask) -> None:
    prepare_database(app)


def _validate_database_schema(app: Flask) -> None:
    validate_database_schema(app)


def _gunicorn_master_preload_active(frame=None) -> bool:
    """Detect Gunicorn's master-side application load from the live stack.

    Gunicorn resolves Python configuration before calling ``Arbiter.setup``;
    when ``preload_app`` is true, that method calls ``app.wsgi()`` directly.
    Detecting the actual load path covers default, file, and module configs
    without executing or attempting to parse configuration a second time.
    """
    current = frame if frame is not None else sys._getframe(1)
    while current is not None:
        if current.f_globals.get("__name__") == "gunicorn.arbiter" and current.f_code.co_name == "setup":
            arbiter = current.f_locals.get("self")
            return bool(getattr(getattr(arbiter, "cfg", None), "preload_app", False))
        current = current.f_back
    return False


def _reject_gunicorn_preload() -> None:
    """Fail for any resolved Gunicorn preload before spawning worker state."""
    try:
        environment_args = shlex.split(os.environ.get("GUNICORN_CMD_ARGS", ""))
    except ValueError as exc:
        raise RuntimeError("invalid GUNICORN_CMD_ARGS quoting") from exc
    if "--preload" in [*sys.argv[1:], *environment_args] or _gunicorn_master_preload_active():
        raise RuntimeError("Gunicorn preload is unsupported: load ctfd-remote-desktop independently in each worker")


def _make_bus_callback(orchestrator: Orchestrator) -> Callable[[dict], None]:
    def _on_bus_message(message: dict) -> None:
        from CTFd.models import db

        try:
            if message.get("_control") == "reload_contexts":
                orchestrator.load_from_db()
                return
            # Redis is live fan-out only. The originating worker owns durable
            # persistence, so a bus delivery must never enqueue a duplicate.
            event_logger._deliver_local(message, persist=False)
        finally:
            # The subscriber's app context lives for the pubsub thread's
            # lifetime. End the scoped session after each message so reloads
            # observe committed rows and do not retain a connection forever.
            db.session.remove()

    return _on_bus_message


def _seed_defaults(app: Flask) -> None:
    from .models import initialize_settings

    # This also locks the singleton revision row and fails plugin startup on
    # any corrupted or unsafe effective profile.
    initialize_settings()


def _seed_local_context(app: Flask) -> None:
    from CTFd.models import db
    from sqlalchemy.exc import IntegrityError

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
    try:
        db.session.commit()
        logger.info("seeded local docker context")
    except IntegrityError:
        # Another first-boot worker can pass the empty-table check before this
        # one commits. The unique context name makes that race safe.
        db.session.rollback()
        if DesktopDockerContextModel.query.filter_by(context_name=LOCAL_CONTEXT_NAME).first() is None:
            raise


def _reconcile_containers(
    app: Flask,
    host_manager: DockerHostManager,
    orchestrator: Orchestrator,
    container_manager: ContainerManager | None = None,
) -> None:
    # leader-only: concurrent reconciles from every gunicorn worker would race
    # row deletes and corrupt the shared active_sessions counters
    from CTFd.models import db
    from .models import (
        DesktopContainerInfoModel,
        END_REASON_RECONCILIATION,
        LIFECYCLE_CLEANUP_PENDING,
        LIFECYCLE_HELD,
        LIFECYCLE_STOPPING,
    )
    from .exceptions import HostsUnavailableException

    if container_manager is None:
        container_manager = ContainerManager(host_manager, orchestrator, app)

    rows = [
        (
            int(row.user_id),
            str(row.docker_context),
            str(row.container_id),
            ContainerManager._row_lifecycle_state(row),
            str(row.lifecycle_reason or END_REASON_RECONCILIATION),
        )
        for row in DesktopContainerInfoModel.query.all()
    ]
    db.session.rollback()
    removed = 0
    kept = 0

    for user_id, docker_context, container_id, lifecycle_state, lifecycle_reason in rows:
        if lifecycle_state == LIFECYCLE_HELD:
            kept += 1
            continue

        # A worker may have died after persisting a teardown transition. Retry
        # that exact retained row even if the remote object is still running.
        if lifecycle_state in (LIFECYCLE_STOPPING, LIFECYCLE_CLEANUP_PENDING):
            result = container_manager.destroy_container(
                user_id,
                reason=lifecycle_reason,
                log_destruction=True,
            )
            if result.get("success"):
                removed += 1
            else:
                kept += 1
            continue

        try:
            running = host_manager.is_container_running(docker_context, container_id)
        except (
            docker.errors.DockerException,
            paramiko.ssh_exception.SSHException,
            EOFError,
            OSError,
            # a context that failed its startup connection check raises this for
            # every call; deleting those rows would orphan live sessions on a
            # merely-slow host (then force-remove them 300s later). transient.
            HostsUnavailableException,
        ):
            kept += 1
            continue
        except Exception:
            # Unknown failures are not proof the container is absent.  Keeping
            # the row and reservation may under-admit temporarily; deleting it
            # can orphan a live desktop and over-admit after reboot.
            logger.warning(f"reconcile: could not verify {container_id}; retaining row", exc_info=True)
            kept += 1
            continue

        if running:
            kept += 1
        else:
            result = container_manager.destroy_container(
                user_id,
                reason=END_REASON_RECONCILIATION,
                log_destruction=True,
            )
            if result.get("success"):
                removed += 1
            else:
                kept += 1

    # Use the same exact counter audit as periodic cleanup. In addition to
    # active rows and observed Docker objects, it includes durable operation
    # reservations and suppresses down-healing across the reserve/operation-row
    # commit gap. A row-only startup sync could otherwise over-admit during a
    # rolling worker restart while another worker is creating a session.
    orchestrator.audit_counts()

    if removed or kept:
        logger.info(f"reconciled containers on startup: {kept} recovered, {removed} stale records removed")


def load(app: Flask) -> None:
    # Gunicorn preload imports the application in the master before forking
    # workers. Reject it before schema creation, event subscribers, or any other
    # process-local/stateful plugin initialization.
    _reject_gunicorn_preload()

    _prepare_database(app)

    host_manager = DockerHostManager()
    orchestrator = Orchestrator(host_manager)

    with app.app_context():
        _seed_defaults(app)
        _seed_local_context(app)
        orchestrator.load_from_db()

    container_manager = ContainerManager(host_manager, orchestrator, app)

    event_bus.init(app, on_message=_make_bus_callback(orchestrator))

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
        app.overridden_templates["remote_desktop_config.html"] = f.read()

    # only when serving HTTP, not CLI commands where scheduler threads prevent exit
    _serving = (
        "gunicorn" in sys.modules or os.environ.get("WERKZEUG_RUN_MAIN") or (len(sys.argv) > 1 and sys.argv[1] == "run")
    )
    if not _serving:
        logger.info("remote desktop plugin loaded (scheduler skipped, CLI mode)")
        return

    # Persistence queues are process-local, so every HTTP worker drains only
    # its own originated events. Redis-delivered fan-out is never persisted.
    event_logger_module.start_persistence_drainer(app)
    atexit.register(event_logger_module.stop_persistence_drainer)

    # Reconciliation is leader-only. Every HTTP worker continues below as a
    # scheduler contender; a follower automatically acquires MariaDB leadership
    # on a later tick if the owning connection/process disappears.
    if _claim_scheduler_leader(app):
        with app.app_context():
            _reconcile_containers(app, host_manager, orchestrator, container_manager)

    from .models import get_setting
    from apscheduler.schedulers.gevent import GeventScheduler

    scheduler = GeventScheduler()

    def _with_app_ctx(fn: Callable[[], None]) -> Callable[[], None]:
        def wrapper() -> None:
            # Flask-SQLAlchemy auto-teardown fires reliably only on REQUEST contexts; manually-opened
            # app contexts leak the scoped session's connection on exit. explicit remove() in finally
            # covers every scheduled job (periodic cleanup, pause watch, and event pruning)
            with app.app_context():
                from CTFd.models import db

                try:
                    if not _claim_scheduler_leader(app):
                        return
                    fn()
                finally:
                    db.session.remove()

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

    # mirrors out-of-band docker pause/unpause (host io tripwire) into
    # paused_at + the admin event feed. interval read at registration like
    # cleanup_interval - restart to change
    scheduler.add_job(
        func=_with_app_ctx(container_manager.pause_watch),
        trigger="interval",
        seconds=get_setting("pause_watch_interval"),
        misfire_grace_time=30,
        coalesce=True,
        id="pause_watch",
    )

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

    # GeventScheduler.shutdown raises BlockingSwitchOutError from atexit
    # (no active greenlet). process is exiting either way, so just swallow it
    def _safe_shutdown_scheduler() -> None:
        if not scheduler.running:
            return
        try:
            scheduler.shutdown(wait=False)
        except Exception:
            pass

    atexit.register(_safe_shutdown_scheduler)

    # Gunicorn owns TERM/INT/HUP. A routine worker restart must preserve every
    # student session; fleet teardown remains an explicit audited admin action.
    logger.info("remote desktop plugin loaded (automatic scheduler contender)")
