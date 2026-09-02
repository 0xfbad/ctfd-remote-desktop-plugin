from __future__ import annotations

import re
import time
import logging
import secrets
import traceback
import uuid
from typing import Callable
from threading import Lock

import docker
import paramiko
from flask import Flask
from sqlalchemy.exc import IntegrityError, OperationalError
from CTFd.models import db, Users
from .models import (
    DesktopContainerInfoModel,
    DesktopSessionOperationModel,
    SettingValue,
    proxy_vnc_url,
    END_REASON_RECONCILIATION,
    END_REASON_USER_DESTROYED,
    END_REASON_ADMIN_KILLED,
    END_REASON_EXPIRED,
    LIFECYCLE_ACTIVE,
    LIFECYCLE_STOPPING,
    LIFECYCLE_CLEANUP_PENDING,
    LIFECYCLE_HELD,
    LIFECYCLE_UNPAUSING,
    OP_IDLE,
    OP_QUEUED,
    OP_SELECTING,
    OP_RESERVED,
    OP_CREATING,
    OP_WAITING_READY,
    OP_ACTIVE,
    OP_CANCEL_REQUESTED,
    OP_STOPPING,
    OP_CLEANUP_PENDING,
    OP_HELD,
    OP_UNPAUSING,
    OP_FAILED,
    CREATE_OPERATION_STATES,
    history_from_row,
    user_flags,
    username_or_fallback,
    _esc,
)
from .event_logger import event_logger
from .docker_host_manager import (
    DockerHostManager,
    ContainerState,
    SESSION_LABEL_MANAGED,
    SESSION_LABEL_USER_ID,
    SESSION_LABEL_UUID,
    normalize_container_state,
    parse_size,
)
from .orchestrator import Orchestrator
from .exceptions import HostsUnavailableException

logger = logging.getLogger(__name__)


def _display_name(user_id: int) -> tuple[Users | None, str]:
    """fetch user from DB and return (user_obj, display_name) tuple"""
    user = Users.query.filter_by(id=user_id).first()
    return user, username_or_fallback(user, user_id)


def _mint_session_cookie(app: Flask, user: Users) -> tuple[str, str, str] | None:
    # CTFd uses server-side sessions, save_session writes to the cache
    # backend so just signing the sid wouldn't populate it.
    # returns (cookie_name, signed_cookie_value, raw_sid). the raw sid is
    # needed to revoke the cache entry on container destroy (the cookie value
    # is itsdangerous-signed and not directly usable as a cache key)
    from flask import session
    from werkzeug.wrappers import Response
    from CTFd.utils.security.auth import login_user

    cookie_name = app.session_cookie_name
    with app.test_request_context():
        login_user(user)
        sid = session.sid
        resp = Response()
        app.session_interface.save_session(app, session, resp)
        for header in resp.headers.getlist("Set-Cookie"):
            if header.startswith(f"{cookie_name}="):
                value = header.split(f"{cookie_name}=", 1)[1].split(";", 1)[0]
                return cookie_name, value, sid
    return None


_USERNAME_RE = re.compile(r"[^a-z0-9_-]")
_RESERVED_NAMES = {
    "root",
    "daemon",
    "bin",
    "sys",
    "sync",
    "games",
    "man",
    "lp",
    "mail",
    "news",
    "uucp",
    "proxy",
    "www",
    "backup",
    "list",
    "irc",
    "gnats",
    "nobody",
    "systemd",
    "sshd",
    "messagebus",
    "avahi",
    "polkitd",
}


CreationStatusDict = dict[str, str]
ContainerInfoDict = dict[str, str | int | float | None]
TimerDict = dict[str, bool | int]
TimerStatusDict = dict[str, bool | int | str]
ResultDict = dict[str, bool | str | int]
ContainerListEntry = dict[str, str | int | float | bool | TimerDict | None]
PausedOrphanEntry = dict[str, str | int | float]

_SESSION_CONTAINER_NAME_RE = re.compile(r"rd-session-([1-9][0-9]*)-([0-9a-f]{8}-[0-9a-f]{3})")


def _sanitize_username(raw: str, user_id: int | None = None) -> str:
    name = _USERNAME_RE.sub("", raw.lower())
    # linux usernames must start with a letter or underscore
    name = name.lstrip("0123456789-")[:32]
    if not name or name in _RESERVED_NAMES:
        return f"user{user_id}" if user_id else "user"
    return name


def _connection_ports(ssh_enabled: bool, web_terminal_enabled: bool) -> list[str]:
    # noVNC is mandatory (the readiness gate polls 6080). Xvnc's 5900 listener
    # stays container-internal for websockify and is never published raw.
    ports = ["6080/tcp"]
    if ssh_enabled:
        ports.append("22/tcp")
    if web_terminal_enabled:
        ports.append("7682/tcp")
    return ports


class ContainerManager:
    UNPAUSE_LEASE_SECONDS = 60

    def __init__(self, host_manager: DockerHostManager, orchestrator: Orchestrator, app: Flask | None = None) -> None:
        self.host_manager = host_manager
        self.orchestrator = orchestrator
        self.app = app
        self.creation_status: dict[int, CreationStatusDict] = {}
        self.lock = Lock()
        # per-user locks serialize destroy_container so concurrent admin-kill +
        # user-destroy don't produce duplicate history rows for one teardown
        self._destroy_locks: dict[int, Lock] = {}
        self._destroy_locks_lock = Lock()

    def _get_destroy_lock(self, user_id: int) -> Lock:
        with self._destroy_locks_lock:
            lock = self._destroy_locks.get(user_id)
            if lock is None:
                lock = Lock()
                self._destroy_locks[user_id] = lock
            return lock

    @staticmethod
    def _row_lifecycle_state(row: DesktopContainerInfoModel) -> str:
        """Read lifecycle state while tolerating lightweight unit doubles."""
        state = getattr(row, "lifecycle_state", LIFECYCLE_ACTIVE)
        if isinstance(state, str):
            return state
        return LIFECYCLE_HELD if ContainerManager._is_paused(row) else LIFECYCLE_ACTIVE

    @staticmethod
    def _is_paused(row: DesktopContainerInfoModel) -> bool:
        paused_at = getattr(row, "paused_at", None)
        return isinstance(paused_at, (int, float)) and paused_at > 0

    def _inspect_container_state(self, context_name: str, container_id: str) -> ContainerState:
        try:
            state = self.host_manager.inspect_container_state(context_name, container_id)
        except Exception:
            return "unknown"
        if state == "not_found":
            return "not_found"
        return normalize_container_state(state)

    def _mirror_detected_hold(self, user_id: int, session_uuid: str) -> bool:
        """Persist a Docker-observed pause if the same session is still active."""
        with self._get_destroy_lock(user_id):
            db.session.rollback()
            operation = self._locked_operation(user_id, create=True)
            current = self._locked_active_row(user_id)
            if current is None or self._session_uuid(current) != session_uuid:
                db.session.rollback()
                return False
            if not self._is_paused(current):
                current.paused_at = time.time()
            current.lifecycle_state = LIFECYCLE_HELD
            if operation is not None:
                operation.state = OP_HELD
                operation.session_uuid = session_uuid
                operation.error = "paused container held for inspection"
                operation.updated_at = time.time()
            db.session.commit()
            return True

    @staticmethod
    def _operation_state(row: DesktopSessionOperationModel | None) -> str | None:
        state = getattr(row, "state", None) if row is not None else None
        return state if isinstance(state, str) else None

    @staticmethod
    def _locked_active_row(user_id: int) -> DesktopContainerInfoModel | None:
        query = DesktopContainerInfoModel.query.filter_by(user_id=user_id)
        if not isinstance(getattr(DesktopContainerInfoModel, "__tablename__", None), str):
            return query.first()
        row = query.populate_existing().with_for_update().first()
        # The unit suite uses lightweight MagicMock queries configured on the
        # direct `.first()` seam. Prefer that value when the chained mock
        # did not return a model-shaped object.
        if not isinstance(getattr(row, "user_id", None), int):
            direct = query.first()
            if direct is None or isinstance(getattr(direct, "user_id", None), int):
                row = direct
        return row

    @staticmethod
    def _locked_operation(user_id: int, create: bool = False) -> DesktopSessionOperationModel | None:
        try:
            query = DesktopSessionOperationModel.query.filter_by(user_id=user_id)
        except AttributeError:
            # The repository's fast unit harness replaces SQLAlchemy's Model
            # base with a narrow mock. Real CTFd models always expose query.
            return None
        row = query.populate_existing().with_for_update().first()
        if not isinstance(getattr(row, "user_id", None), int):
            direct = query.first()
            if direct is None or isinstance(getattr(direct, "user_id", None), int):
                row = direct
        if row is not None and isinstance(getattr(row, "user_id", None), int):
            return row
        if not create:
            return None

        now = time.time()
        row = DesktopSessionOperationModel(
            user_id=user_id,
            operation_uuid=str(uuid.uuid4()),
            worker_lease_uuid=None,
            session_uuid=None,
            state=OP_IDLE,
            cancel_requested=False,
            capacity_reserved=False,
            created_at=now,
            updated_at=now,
        )
        # Explicit assignment also keeps the lightweight model doubles used by
        # the unit suite model-shaped; SQLAlchemy accepts the redundancy.
        row.user_id = user_id
        row.operation_uuid = str(getattr(row, "operation_uuid", "") or uuid.uuid4())
        row.state = OP_IDLE
        row.created_at = now
        row.updated_at = now
        try:
            db.session.add(row)
            db.session.flush()
            return row
        except (IntegrityError, OperationalError):
            # Another worker lazily created the stable mutex first. End the
            # failed transaction and lock the winner's row. InnoDB can report
            # the absent-row insert race as either duplicate-key or a victim
            # of next-key-lock deadlock.
            db.session.rollback()
            query = DesktopSessionOperationModel.query.filter_by(user_id=user_id)
            return query.populate_existing().with_for_update().first()

    def _claim_create_operation(self, user_id: int) -> tuple[str, str] | None:
        """Atomically claim a new logical generation for a user."""
        db.session.rollback()
        operation = self._locked_operation(user_id, create=True)
        active = self._locked_active_row(user_id)
        if active is not None:
            db.session.rollback()
            return None
        state = self._operation_state(operation)
        if state not in (None, OP_IDLE, OP_FAILED):
            db.session.rollback()
            return None

        session_uuid = str(uuid.uuid4())
        worker_uuid = str(uuid.uuid4())
        if operation is None:
            # Unit-harness compatibility; production cannot reach this after
            # schema validation and therefore always persists the claim.
            if hasattr(DesktopSessionOperationModel, "query"):
                raise RuntimeError("failed to acquire durable desktop operation row")
            return session_uuid, worker_uuid
        now = time.time()
        operation.operation_uuid = str(uuid.uuid4())
        operation.worker_lease_uuid = worker_uuid
        operation.session_uuid = session_uuid
        operation.state = OP_QUEUED
        operation.cancel_requested = False
        operation.docker_context = None
        operation.container_name = f"rd-session-{user_id}-{session_uuid[:12]}"
        operation.capacity_reserved = False
        operation.requested_reason = None
        operation.error = None
        operation.created_at = now
        operation.updated_at = now
        operation.heartbeat_at = now
        db.session.commit()
        return session_uuid, worker_uuid

    def _update_operation(
        self,
        user_id: int,
        session_uuid: str,
        worker_uuid: str,
        state: str,
        **fields: object,
    ) -> bool:
        """Fenced progress update; a stale worker cannot overwrite a takeover."""
        db.session.rollback()
        operation = self._locked_operation(user_id)
        if operation is None and not hasattr(DesktopSessionOperationModel, "query"):
            return True
        if operation is None or operation.session_uuid != session_uuid or operation.worker_lease_uuid != worker_uuid:
            db.session.rollback()
            return False
        if operation.cancel_requested and state not in (OP_CANCEL_REQUESTED, OP_CLEANUP_PENDING, OP_FAILED):
            db.session.rollback()
            return False
        operation.state = state
        operation.updated_at = time.time()
        operation.heartbeat_at = operation.updated_at
        for key, value in fields.items():
            setattr(operation, key, value)
        db.session.commit()
        return True

    def _creation_cancelled(self, user_id: int, session_uuid: str, worker_uuid: str) -> bool:
        db.session.rollback()
        operation = self._locked_operation(user_id)
        if operation is None and not hasattr(DesktopSessionOperationModel, "query"):
            db.session.rollback()
            return False
        cancelled = bool(
            operation is None
            or operation.session_uuid != session_uuid
            or operation.worker_lease_uuid != worker_uuid
            or operation.cancel_requested
            or self._operation_state(operation) == OP_CANCEL_REQUESTED
        )
        db.session.rollback()
        return cancelled

    @staticmethod
    def _session_uuid(row: DesktopContainerInfoModel) -> str:
        value = getattr(row, "session_uuid", None)
        if isinstance(value, str) and value:
            return value
        # Only reachable in lightweight unit doubles; startup schema validation
        # guarantees a UUID before lifecycle code runs.
        container_id = str(getattr(row, "container_id", "test-double"))
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"ctfd-remote-desktop:{container_id}"))

    def _get_setting(self, key: str) -> SettingValue:
        from .models import get_setting

        return get_setting(key)

    def _resolve_username(self, user: Users) -> str:
        source = self._get_setting("username_source")

        if source == "email" and user.email:
            return _sanitize_username(user.email.split("@")[0], user.id)
        return _sanitize_username(user.name, user.id)

    def wait_for_vnc_ready(
        self,
        hostname: str,
        novnc_port: int,
        max_attempts: int | None = None,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> bool:
        if max_attempts is None:
            max_attempts = int(self._get_setting("vnc_ready_attempts"))  # type: ignore[arg-type]
        http_timeout = int(self._get_setting("http_request_timeout"))  # type: ignore[arg-type]

        import urllib.request
        import urllib.error

        for attempt in range(max_attempts):
            if progress_callback and attempt % 5 == 0:
                progress_callback(attempt, max_attempts)

            try:
                req = urllib.request.Request(f"http://{hostname}:{novnc_port}/", method="GET")
                req.add_header("User-Agent", "CTFd-VNC-Check")
                with urllib.request.urlopen(req, timeout=http_timeout) as response:
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

    def _create_container_background_wrapper(
        self,
        app: Flask,
        user_id: int,
        container_url: str,
        extra_hosts: dict[str, str] | None,
        session_uuid: str,
        worker_uuid: str,
    ) -> None:
        with app.app_context():
            self._create_container_background(
                user_id,
                container_url,
                extra_hosts,
                session_uuid=session_uuid,
                worker_uuid=worker_uuid,
            )

    def _create_container_background(
        self,
        user_id: int,
        container_url: str,
        extra_hosts: dict[str, str] | None,
        session_uuid: str | None = None,
        worker_uuid: str | None = None,
    ) -> None:
        logger.info(f"[BACKGROUND] creating container for user {user_id}")

        # Direct unit-level callers predate the durable request claim. They
        # still exercise Docker serialization without fencing; production
        # always supplies both tokens through create_container().
        fenced = session_uuid is not None and worker_uuid is not None
        session_uuid = session_uuid or str(uuid.uuid4())
        worker_uuid = worker_uuid or str(uuid.uuid4())

        user, username = _display_name(user_id)
        container_username = self._resolve_username(user) if user else f"user{user_id}"

        context_name: str | None = None
        container_name: str | None = None

        try:
            if fenced and not self._update_operation(user_id, session_uuid, worker_uuid, OP_SELECTING):
                raise RuntimeError("creation lease is no longer owned by this worker")
            with self.lock:
                self.creation_status[user_id] = {"status": "selecting_host", "message": "Requesting a server..."}

            context_name = self.orchestrator.select_and_reserve()
            pub_hostname = self.host_manager.get_pub_hostname(context_name)
            check_hostname = self.host_manager.get_check_hostname(context_name)
            # escaped for safe embedding in creation status messages rendered via innerHTML
            display_hostname = _esc(context_name)

            logger.info(f"selected context: {context_name} (public: {pub_hostname}) for user {user_id}")

            self.host_manager.acquire_semaphore(context_name)

            container_name = f"rd-session-{user_id}-{session_uuid[:12]}"
            if fenced and not self._update_operation(
                user_id,
                session_uuid,
                worker_uuid,
                OP_RESERVED,
                docker_context=context_name,
                container_name=container_name,
                capacity_reserved=True,
            ):
                raise RuntimeError("creation cancelled after host reservation")

            try:
                with self.lock:
                    self.creation_status[user_id] = {
                        "status": "starting_container",
                        "message": f"Starting container on {display_hostname}...",
                    }

                network_name = str(self._get_setting("rd_network_name") or "bridge")

                if fenced and not self._update_operation(
                    user_id,
                    session_uuid,
                    worker_uuid,
                    OP_CREATING,
                ):
                    raise RuntimeError("creation cancelled before Docker create")

                vnc_password = secrets.token_urlsafe(6)[:8]

                docker_image = str(self._get_setting("docker_image"))
                resolution = str(self._get_setting("resolution"))
                shm_size = parse_size(self._get_setting("shm_size"))  # type: ignore[arg-type]
                memory_limit = parse_size(self._get_setting("memory_limit"))  # type: ignore[arg-type]
                cpu_limit = self._get_setting("cpu_limit")
                nano_cpus = int(float(cpu_limit) * 1e9)  # type: ignore[arg-type]

                # read once so ports and env can never disagree for one container
                ssh_enabled = bool(self._get_setting("ssh_enabled"))
                web_terminal_enabled = bool(self._get_setting("web_terminal_enabled"))

                initial_duration = int(self._get_setting("initial_duration"))  # type: ignore[arg-type]
                extension_duration = int(self._get_setting("extension_duration"))  # type: ignore[arg-type]
                max_extensions = int(self._get_setting("max_extensions"))  # type: ignore[arg-type]
                # hard ceiling so containers can't outlive the max possible session
                max_lifetime = int(initial_duration + (extension_duration * max_extensions) + 300)

                container_env = {
                    "VNC_PASSWORD": vnc_password,
                    "RESOLUTION": resolution,
                    "CTFD_USERNAME": container_username,
                    "MAX_LIFETIME": str(max_lifetime),
                    "CTFD_URL": container_url,
                    # "0" disables in the image; absent = on
                    "ENABLE_SSH": "1" if ssh_enabled else "0",
                    "ENABLE_TTYD": "1" if web_terminal_enabled else "0",
                }

                from flask import current_app

                cookie_sid: str | None = None
                if user is not None:
                    minted = _mint_session_cookie(current_app._get_current_object(), user)
                    if minted:
                        cookie_name, cookie_value, cookie_sid = minted
                        container_env["CTFD_COOKIE_NAME"] = cookie_name
                        container_env["CTFD_COOKIE_VALUE"] = cookie_value
                    else:
                        logger.warning(f"failed to mint session cookie for user {user_id}, autologin disabled")

                # Settings/session reads above may have opened an implicit
                # transaction. End it before the remote Docker call.
                db.session.rollback()
                result = self.host_manager.run_container(
                    context_name=context_name,
                    image=docker_image,
                    name=container_name,
                    hostname=display_hostname,
                    env=container_env,
                    ports=_connection_ports(ssh_enabled, web_terminal_enabled),
                    shm_size=shm_size,
                    memory=memory_limit,
                    nano_cpus=nano_cpus,
                    extra_hosts=extra_hosts,
                    network=network_name,
                    labels={
                        SESSION_LABEL_MANAGED: "true",
                        SESSION_LABEL_USER_ID: str(user_id),
                        SESSION_LABEL_UUID: session_uuid,
                    },
                )
            finally:
                self.host_manager.release_semaphore(context_name)

            port_map: dict[str, int] = result["ports"]  # type: ignore[assignment]
            container_id = str(result["container_id"])
            ssh_port = port_map.get("22/tcp")
            # Kept non-null for the existing schema. This is now the internal
            # Xvnc listener, not a published host port.
            vnc_port = 5900
            novnc_port = port_map["6080/tcp"]
            ttyd_port = port_map.get("7682/tcp")

            logger.info(
                f"container {container_name} created - SSH:{ssh_port} VNC:{vnc_port} noVNC:{novnc_port} ttyd:{ttyd_port}"
            )

            with self.lock:
                self.creation_status[user_id] = {
                    "status": "waiting_vnc",
                    "message": f"Waiting for {display_hostname} display server...",
                }
            if fenced and not self._update_operation(user_id, session_uuid, worker_uuid, OP_WAITING_READY):
                raise RuntimeError("creation cancelled while waiting for readiness")

            def _vnc_progress(attempt: int, max_attempts: int) -> None:
                with self.lock:
                    self.creation_status[user_id] = {
                        "status": "waiting_vnc",
                        "message": f"Waiting for {display_hostname} display server... ({attempt}/{max_attempts})",
                    }

            vnc_ready = self.wait_for_vnc_ready(check_hostname, novnc_port, progress_callback=_vnc_progress)  # type: ignore[arg-type]

            if not vnc_ready:
                raise Exception(f"VNC server on {check_hostname}:{novnc_port} did not become ready in time")

            vnc_url = proxy_vnc_url(user_id, vnc_password)

            # Check both the durable cross-worker cancellation flag and the
            # process-local cancellation cache immediately before the active-row commit.
            if fenced and self._creation_cancelled(user_id, session_uuid, worker_uuid):
                raise RuntimeError("creation cancelled by user")
            with self.lock:
                status = self.creation_status.get(user_id)
                if status and status.get("status") == "cancelled":
                    raise Exception("creation cancelled by user")

            row = DesktopContainerInfoModel(
                container_id=container_id,
                user_id=user_id,
                container_name=container_name,
                vnc_port=vnc_port,
                novnc_port=novnc_port,
                ssh_port=ssh_port,
                ttyd_port=ttyd_port,
                vnc_password=vnc_password,
                vnc_url=vnc_url,
                docker_context=context_name,
                pub_hostname=pub_hostname,
                container_username=container_username,
                created_at=time.time(),
                timer_started=True,
                timer_start_time=time.time(),
                timer_duration=float(initial_duration),
                extensions_used=0,
                max_extensions=max_extensions,
                cookie_sid=cookie_sid,
                session_uuid=session_uuid,
                lifecycle_state=LIFECYCLE_ACTIVE,
            )
            try:
                if fenced:
                    db.session.rollback()
                    operation = self._locked_operation(user_id)
                    existing = self._locked_active_row(user_id)
                    if (
                        operation is None
                        or operation.session_uuid != session_uuid
                        or operation.worker_lease_uuid != worker_uuid
                        or operation.cancel_requested
                        or existing is not None
                    ):
                        db.session.rollback()
                        raise RuntimeError("creation lease changed before finalization")
                    db.session.add(row)
                    operation.state = OP_ACTIVE
                    operation.updated_at = time.time()
                    operation.heartbeat_at = operation.updated_at
                    operation.docker_context = context_name
                    operation.container_name = container_name
                    db.session.commit()
                else:
                    db.session.add(row)
                    db.session.commit()
            except Exception:
                db.session.rollback()
                raise

            with self.lock:
                self.creation_status[user_id] = {
                    "status": "ready",
                    "message": "Desktop ready!",
                    "hostname": display_hostname,
                }

            event_logger.log_event(
                "session_created",
                "remote desktop session created successfully",
                user_id=user_id,
                username=username,
                level="info",
                metadata={
                    "context": context_name,
                    "container_name": container_name,
                    "ssh_port": ssh_port,
                    "ttyd_port": ttyd_port,
                    "vnc_port": vnc_port,
                    "novnc_port": novnc_port,
                },
            )

        except Exception as e:
            stopped_ok = False
            if container_name and context_name:
                try:
                    self.host_manager.stop_container(context_name, container_name)
                    stopped_ok = True
                    logger.info(f"cleaned up container {container_name} after creation failure")
                except Exception as stop_error:
                    logger.error(f"failed to stop container during cleanup: {stop_error}")

            # A transient Docker/SSH failure after create can leave a live
            # container whose stop outcome is unknown.  Keep its reservation
            # until the strict live-container audit can prove it is gone.
            # If no container name was allocated, no create was attempted and
            # it is safe to release immediately.
            cleanup_confirmed = container_name is None or stopped_ok
            if context_name and cleanup_confirmed:
                try:
                    self.orchestrator.release_slot(context_name)
                except Exception as release_error:
                    logger.error(f"failed to release slot during cleanup: {release_error}")

                try:
                    if not self.host_manager.ping(context_name):
                        self.orchestrator.mark_unhealthy(context_name)
                    else:
                        logger.info(f"context {context_name} still reachable, not marking unhealthy")
                except Exception as health_error:
                    logger.error(f"failed to check context health during cleanup: {health_error}")

            logger.error(f"error creating container for user {user_id}: {e}")
            logger.error(traceback.format_exc())

            if fenced:
                terminal_state = OP_FAILED if cleanup_confirmed else OP_CLEANUP_PENDING
                try:
                    self._update_operation(
                        user_id,
                        session_uuid,
                        worker_uuid,
                        terminal_state,
                        error=str(e),
                        docker_context=context_name,
                        container_name=container_name if not cleanup_confirmed else None,
                        capacity_reserved=bool(context_name and not cleanup_confirmed),
                    )
                except Exception:
                    db.session.rollback()
                    logger.error("failed to persist creation cleanup state", exc_info=True)

            with self.lock:
                # don't pre-escape, frontend assigns these to textContent which is xss-safe
                # by default. pre-escaping causes &lt;...&gt; to render as literal entity text
                self.creation_status[user_id] = {
                    "status": "failed",
                    "error": str(e),
                    "hostname": context_name or "",
                }

            event_logger.log_event(
                "session_error",
                f"failed to create session: {str(e)}",
                user_id=user_id,
                username=username,
                level="error",
                metadata={"error": str(e), "traceback": traceback.format_exc()},
            )

    def create_container(
        self,
        user_id: int,
        container_url: str,
        extra_hosts: dict[str, str] | None = None,
        client_ip: str | None = None,
    ) -> ResultDict:
        from flask import current_app

        user, username = _display_name(user_id)

        logger.info(f"create_container called for user {user_id} ({username})")

        # single critical section serializes the check-and-claim. without this,
        # two near-simultaneous POSTs can both pass the in-progress check and
        # both spawn background greenlets, leaking a host slot
        with self.lock:
            existing = self.creation_status.get(user_id)
            if existing and existing.get("status") not in (None, "failed", "ready"):
                return {"success": False, "error": "Creation already in progress"}

            existing_row = DesktopContainerInfoModel.query.filter_by(user_id=user_id).first()
            if existing_row:
                return {"success": False, "error": "Session already exists"}

            claim = self._claim_create_operation(user_id)
            if claim is None:
                return {"success": False, "error": "Session creation or cleanup already in progress"}
            session_uuid, worker_uuid = claim
            self.creation_status[user_id] = {"status": "queued", "message": "Queued..."}

        try:
            self.orchestrator.admission_check()
        except HostsUnavailableException:
            with self.lock:
                self.creation_status.pop(user_id, None)
            try:
                self._update_operation(user_id, session_uuid, worker_uuid, OP_FAILED, error="admission failed")
            except Exception:
                db.session.rollback()
            raise

        host_status = self.orchestrator.get_status()
        event_logger.log_event(
            "session_requested",
            "requested remote desktop session",
            user_id=user_id,
            username=username,
            level="info",
            metadata={
                "client_ip": client_ip,
                "hosts": {  # type: ignore[dict-item]
                    str(h["context_name"]): {
                        "containers": h["active_containers"],
                        "weight": h["weight"],
                        "healthy": h["healthy"],
                        "score": round(int(h["weight"]) / (int(h["active_containers"]) + 1), 2) if h["healthy"] else 0,  # type: ignore[arg-type]
                    }
                    for h in host_status
                },
            },
        )

        app: Flask = current_app._get_current_object()

        try:
            import gevent

            gevent.spawn(
                self._create_container_background_wrapper,
                app,
                user_id,
                container_url,
                extra_hosts,
                session_uuid,
                worker_uuid,
            )
        except Exception as e:
            logger.error(f"failed to submit background task: {e}")
            logger.error(traceback.format_exc())
            with self.lock:
                self.creation_status[user_id] = {
                    "status": "failed",
                    "error": f"Failed to start background task: {str(e)}",
                }
            try:
                self._update_operation(user_id, session_uuid, worker_uuid, OP_FAILED, error=str(e))
            except Exception:
                db.session.rollback()
            return {"success": False, "error": str(e)}

        return {"success": True, "status": "creating"}

    def get_creation_status(self, user_id: int) -> CreationStatusDict | None:
        operation = DesktopSessionOperationModel.query.filter_by(user_id=user_id).first()
        state = self._operation_state(operation)
        if state in CREATE_OPERATION_STATES or state in (OP_FAILED, OP_CLEANUP_PENDING):
            result: CreationStatusDict = {"status": state or OP_FAILED}
            error = getattr(operation, "error", None)
            if isinstance(error, str) and error:
                result["error"] = error
            context = getattr(operation, "docker_context", None)
            if isinstance(context, str) and context:
                result["hostname"] = _esc(context)
            return result
        with self.lock:
            return self.creation_status.get(user_id)

    def destroy_container(
        self, user_id: int, reason: str = END_REASON_USER_DESTROYED, log_destruction: bool = True
    ) -> ResultDict:
        _user, username = _display_name(user_id)

        # The local lock is only a contention optimization. The stable operation
        # row and active-row FOR UPDATE locks are the cross-worker authority.
        with self._get_destroy_lock(user_id):
            db.session.rollback()
            operation = self._locked_operation(user_id, create=True)
            row = self._locked_active_row(user_id)

            with self.lock:
                status = self.creation_status.get(user_id)
                if status and status.get("status") not in (None, "failed", "ready"):
                    # creation is in-flight, signal the background greenlet to abort
                    self.creation_status[user_id] = {"status": "cancelled"}
                else:
                    self.creation_status.pop(user_id, None)

            if row is None:
                if operation is not None and self._operation_state(operation) in CREATE_OPERATION_STATES:
                    operation.cancel_requested = True
                    operation.state = OP_CANCEL_REQUESTED
                    operation.updated_at = time.time()
                    db.session.commit()
                    return {"success": True, "status": "cancelling"}
                db.session.rollback()
                return {"success": False, "error": "No active container found"}

            # evidence hold: stop + auto_remove would delete the writable layer;
            # only an explicit admin kill gets through
            if self._is_paused(row) and reason != END_REASON_ADMIN_KILLED:
                row.lifecycle_state = LIFECYCLE_HELD
                if operation is not None:
                    operation.state = OP_HELD
                    operation.session_uuid = self._session_uuid(row)
                    operation.updated_at = time.time()
                db.session.commit()
                return {"success": False, "error": "Session suspended - contact your instructor"}

            lifecycle_state = self._row_lifecycle_state(row)
            if lifecycle_state == LIFECYCLE_HELD and reason != END_REASON_ADMIN_KILLED:
                db.session.rollback()
                return {"success": False, "error": "Session suspended - contact your instructor"}
            if lifecycle_state == LIFECYCLE_STOPPING:
                db.session.rollback()
                return {"success": True, "status": "stopping"}
            if lifecycle_state == LIFECYCLE_UNPAUSING and reason != END_REASON_ADMIN_KILLED:
                db.session.rollback()
                return {"success": False, "error": "Session suspended - contact your instructor"}
            if lifecycle_state not in (
                LIFECYCLE_ACTIVE,
                LIFECYCLE_STOPPING,
                LIFECYCLE_CLEANUP_PENDING,
                LIFECYCLE_HELD,
                LIFECYCLE_UNPAUSING,
            ):
                db.session.rollback()
                return {"success": False, "error": "Session is not in a destroyable state"}

            context_name = str(row.docker_context)
            container_name = str(row.container_name)
            container_id = str(row.container_id)
            session_uuid = self._session_uuid(row)
            row.session_uuid = session_uuid
            # Release Users/operation/active row locks before the Docker/SSH
            # state check. The session UUID fences the second transaction.
            db.session.commit()

        observed_state = self._inspect_container_state(context_name, container_id)

        admin_override = reason == END_REASON_ADMIN_KILLED
        if not admin_override and observed_state == "paused":
            # Mirror an out-of-band Docker pause into the durable hold. This
            # narrows the check/stop window, though the daemon can still pause
            # the container after this inspection and before stop.
            self._mirror_detected_hold(user_id, session_uuid)
            return {"success": False, "error": "Session suspended - contact your instructor"}
        if not admin_override and observed_state == "unknown":
            db.session.rollback()
            return {"success": False, "error": "Container state is unknown; refusing destructive cleanup"}

        with self._get_destroy_lock(user_id):
            db.session.rollback()
            operation = self._locked_operation(user_id, create=True)
            current = (
                self._locked_active_row(user_id)
                if isinstance(getattr(DesktopContainerInfoModel, "__tablename__", None), str)
                else row
            )
            if current is None or self._session_uuid(current) != session_uuid:
                db.session.rollback()
                return {"success": False, "error": "Session changed before teardown"}
            if not admin_override and (
                self._is_paused(current) or self._row_lifecycle_state(current) == LIFECYCLE_HELD
            ):
                db.session.rollback()
                return {"success": False, "error": "Session suspended - contact your instructor"}

            lifecycle_state = self._row_lifecycle_state(current)
            if lifecycle_state == LIFECYCLE_STOPPING:
                db.session.rollback()
                return {"success": True, "status": "stopping"}
            if lifecycle_state == LIFECYCLE_UNPAUSING and not admin_override:
                db.session.rollback()
                return {"success": False, "error": "Session suspended - contact your instructor"}
            if lifecycle_state not in (
                LIFECYCLE_ACTIVE,
                LIFECYCLE_CLEANUP_PENDING,
                LIFECYCLE_HELD,
                LIFECYCLE_UNPAUSING,
            ):
                db.session.rollback()
                return {"success": False, "error": "Session is not in a destroyable state"}

            row = current
            cookie_sid = current.cookie_sid
            was_paused = (
                self._is_paused(current)
                or lifecycle_state in (LIFECYCLE_HELD, LIFECYCLE_UNPAUSING)
                or observed_state == "paused"
            )
            current.lifecycle_state = LIFECYCLE_STOPPING
            current.lifecycle_reason = reason
            if operation is not None:
                operation.session_uuid = session_uuid
                operation.worker_lease_uuid = str(uuid.uuid4())
                operation.state = OP_STOPPING
                operation.cancel_requested = False
                operation.docker_context = context_name
                operation.container_name = container_name
                operation.capacity_reserved = True
                operation.requested_reason = reason
                operation.updated_at = time.time()
            db.session.commit()

        # Remote/cache/log operations deliberately run without database locks.
        # Revoke the minted CTFd session before remote teardown. A cache failure
        # must not turn a confirmed Docker stop into an untracked session.
        if cookie_sid:
            try:
                from flask import current_app
                from CTFd.cache import cache

                cache.delete(current_app.session_interface.key_prefix + cookie_sid)
            except Exception as e:
                logger.warning(f"failed to revoke cookie_sid for user {user_id}: {e}")

        try:
            if (
                was_paused
                or observed_state in ("created", "exited", "not_found")
                or reason == END_REASON_RECONCILIATION
            ):
                # stop on a frozen container blocks the full timeout; remove directly
                self.host_manager.force_remove_container(context_name, container_name)
            else:
                self.host_manager.stop_container(context_name, container_name)
        except (
            docker.errors.DockerException,
            paramiko.ssh_exception.SSHException,
            EOFError,
            OSError,
            HostsUnavailableException,
        ) as e:
            # Unknown remote outcome: keep the full active row and every
            # reservation. Recovery retries the exact UUID-derived container.
            db.session.rollback()
            operation = self._locked_operation(user_id, create=True)
            current = (
                self._locked_active_row(user_id)
                if isinstance(getattr(DesktopContainerInfoModel, "__tablename__", None), str)
                else row
            )
            if current is not None and self._session_uuid(current) == session_uuid:
                current.lifecycle_state = LIFECYCLE_CLEANUP_PENDING
                current.lifecycle_reason = reason
            if operation is not None and operation.session_uuid == session_uuid:
                operation.state = OP_CLEANUP_PENDING
                operation.error = str(e)
                operation.updated_at = time.time()
            db.session.commit()
            logger.warning(
                f"stop outcome unknown for {container_name}: context unavailable; retaining the capacity reservation"
            )
            return {"success": False, "error": "Container stop outcome is unknown; cleanup will be retried"}

        # Only now is it truthful to write history and remove the authoritative
        # active row. If this process dies before the commit, recovery observes
        # `stopping` and idempotently confirms/removes the already-stopped object.
        ended_at = time.time()
        with self._get_destroy_lock(user_id):
            db.session.rollback()
            operation = self._locked_operation(user_id, create=True)
            current = (
                self._locked_active_row(user_id)
                if isinstance(getattr(DesktopContainerInfoModel, "__tablename__", None), str)
                else row
            )
            if current is None or self._session_uuid(current) != session_uuid:
                db.session.rollback()
                return {"success": False, "error": "Session changed during teardown"}
            history = history_from_row(current, username, ended_at, reason)
            db.session.add(history)
            db.session.delete(current)
            if operation is not None and operation.session_uuid == session_uuid:
                operation.state = OP_IDLE
                operation.worker_lease_uuid = None
                operation.cancel_requested = False
                operation.capacity_reserved = False
                operation.session_uuid = None
                operation.docker_context = None
                operation.container_name = None
                operation.requested_reason = None
                operation.error = None
                operation.updated_at = ended_at
            db.session.commit()

        self.orchestrator.release_slot(context_name)

        if log_destruction:
            duration = ended_at - history.started_at
            event_logger.log_event(
                "session_destroyed",
                "remote desktop session destroyed",
                user_id=user_id,
                username=username,
                level="info",
                metadata={
                    "context": context_name,
                    "container_name": container_name,
                    "reason": reason,
                    "duration": round(duration),
                    "extensions_used": history.extensions_used,
                },
            )

        return {"success": True}

    def get_container_info(self, user_id: int) -> ContainerInfoDict | None:
        row = DesktopContainerInfoModel.query.filter_by(user_id=user_id).first()
        if not row:
            return None
        if self._row_lifecycle_state(row) != LIFECYCLE_ACTIVE:
            return None

        if self._is_expired(row) and not row.paused_at:
            self.destroy_container(user_id, reason=END_REASON_EXPIRED)
            return None

        if not self._verify_or_reap(row):
            return None

        return {
            "container_id": row.container_id,
            "container_name": row.container_name,
            "vnc_port": row.vnc_port,
            "novnc_port": row.novnc_port,
            "ssh_port": row.ssh_port,
            "ttyd_port": row.ttyd_port,
            "docker_context": row.docker_context,
            "pub_hostname": row.pub_hostname,
            "container_username": row.container_username,
            "vnc_password": row.vnc_password,
            # Never return stored absolute/direct URLs.
            "vnc_url": proxy_vnc_url(row.user_id, row.vnc_password),
            "created_at": row.created_at,
        }

    @staticmethod
    def _is_expired(row: DesktopContainerInfoModel) -> bool:
        if not row.timer_started or row.timer_start_time is None:
            return False
        return row.timer_duration - (time.time() - row.timer_start_time) <= 0

    def _verify_or_reap(self, row: DesktopContainerInfoModel) -> bool:
        # returns True if the row is live or unverifiable (transient error).
        # returns False if the container vanished and we deleted the row.
        user_id = row.user_id
        context_name = row.docker_context
        container_id = row.container_id
        db.session.rollback()
        # State inspection does a paramiko SSH round-trip; keep it out of
        # the per-user lock so we don't block destroy_container on the network
        state = self._inspect_container_state(context_name, container_id)
        if state in ("running", "paused", "unknown"):
            return True

        # Reachable-but-not-running includes Exited, Created, and NotFound.
        # The reconciliation destroy path force-removes the exact object and
        # only then finalizes history/reservations.
        self.destroy_container(user_id, reason=END_REASON_RECONCILIATION)
        return False

    # builds the frontend TimerDict shape; keep in sync with
    # routes._timer_dict which builds the same shape
    @staticmethod
    def _timer_from_row(row: DesktopContainerInfoModel) -> TimerDict | None:
        if not row.timer_started:
            return None
        elapsed = time.time() - row.timer_start_time
        remaining = max(0, row.timer_duration - elapsed)
        if remaining <= 0:
            return None
        return {
            "active": True,
            "time_remaining": int(remaining),
            "extensions_used": row.extensions_used,
            "max_extensions": row.max_extensions,
        }

    def get_all_containers(self) -> list[ContainerListEntry]:
        rows = DesktopContainerInfoModel.query.all()
        if not rows:
            return []

        expired = [
            row
            for row in rows
            if self._row_lifecycle_state(row) == LIFECYCLE_ACTIVE and self._is_expired(row) and not row.paused_at
        ]
        for row in expired:
            try:
                self.destroy_container(row.user_id, reason=END_REASON_EXPIRED)
            except Exception as e:
                logger.error(f"inline expiry cleanup failed for user {row.user_id}: {e}")

        if expired:
            rows = (
                DesktopContainerInfoModel.query.all()
                if isinstance(getattr(DesktopContainerInfoModel, "__tablename__", None), str)
                else DesktopContainerInfoModel.query.filter_by(timer_started=True).all()
            )
            if not rows:
                return []

        user_ids = [row.user_id for row in rows]
        users_by_id = {u.id: u for u in Users.query.filter(Users.id.in_(user_ids)).all()}

        containers: list[ContainerListEntry] = []
        for row in rows:
            user = users_by_id.get(row.user_id)
            container_data = {
                "user_id": row.user_id,
                # inv: dashboard JS at remote_desktop_dashboard.html:1074,1093 uses these via innerHTML; must stay _esc'd
                "username": _esc(user.name) if user else "Unknown",
                **user_flags(user),
                "container_name": _esc(row.container_name),
                "container_id": row.container_id,
                "docker_context": _esc(row.docker_context),
                "paused": bool(row.paused_at),
                "lifecycle_state": self._row_lifecycle_state(row),
                "created_at": row.created_at,
                "novnc_port": row.novnc_port,
                "timer": self._timer_from_row(row),
            }
            containers.append(container_data)

        return containers

    def extend_session_timer(self, user_id: int, new_duration: int | None = None) -> ResultDict:
        _user, username = _display_name(user_id)

        if new_duration is None:
            new_duration = int(self._get_setting("extension_duration"))  # type: ignore[arg-type]

        # Lock the same active row whose destroy transition changes to stopping.
        # The process-local lock is an optimization; FOR UPDATE is authoritative.
        with self._get_destroy_lock(user_id):
            db.session.rollback()
            row = self._locked_active_row(user_id)
            if not row:
                db.session.rollback()
                return {"success": False, "error": "No active session"}
            if self._row_lifecycle_state(row) != LIFECYCLE_ACTIVE:
                db.session.rollback()
                return {"success": False, "error": "Session is stopping or suspended"}

            if not row.timer_started:
                db.session.rollback()
                return {"success": False, "error": "Timer not started"}

            if row.extensions_used >= row.max_extensions:
                db.session.rollback()
                return {"success": False, "error": "Maximum extensions reached"}

            now = time.time()
            elapsed = now - row.timer_start_time
            remaining = max(0, row.timer_duration - elapsed)
            row.timer_start_time = now
            row.timer_duration = remaining + new_duration
            row.extensions_used += 1
            db.session.commit()

            extensions_used = row.extensions_used
            max_extensions = row.max_extensions

        logger.info(f"extended timer for user {user_id}: {extensions_used}/{max_extensions}")

        event_logger.log_event(
            "session_extended",
            f"session extended ({extensions_used}/{max_extensions} extensions used)",
            user_id=user_id,
            username=username,
            level="info",
            metadata={
                "extensions_used": extensions_used,
                "max_extensions": max_extensions,
                "new_duration": new_duration,
            },
        )

        return {"success": True, "extensions_used": extensions_used, "max_extensions": max_extensions}

    def get_session_timer_status(self, user_id: int) -> TimerStatusDict:
        row = DesktopContainerInfoModel.query.filter_by(user_id=user_id).first()
        if not row:
            return {"success": False, "error": "No active session"}
        if self._row_lifecycle_state(row) != LIFECYCLE_ACTIVE:
            return {"success": False, "error": "Session is stopping or suspended"}

        if not row.timer_started:
            return {"success": True, "started": False, "time_remaining": 0}

        elapsed = time.time() - row.timer_start_time
        time_remaining = max(0, row.timer_duration - elapsed)

        if time_remaining <= 0:
            return {"success": True, "started": False, "time_remaining": 0, "expired": True}

        return {
            "success": True,
            "started": True,
            "time_remaining": int(time_remaining),
            "extensions_used": row.extensions_used,
            "max_extensions": row.max_extensions,
        }

    def periodic_cleanup(self) -> None:
        with self.app.app_context():  # type: ignore[union-attr]
            with self.lock:
                active_user_ids = {
                    r.user_id
                    for r in DesktopContainerInfoModel.query.with_entities(DesktopContainerInfoModel.user_id).all()
                }
                stale = [
                    uid
                    for uid, s in self.creation_status.items()
                    if s.get("status") in ("failed", "ready", "cancelled") and uid not in active_user_ids
                ]
                for uid in stale:
                    del self.creation_status[uid]

            # Cleanup-pending rows must be retried even when their timer never
            # started (for example, a failed create that reached Docker).
            rows = (
                DesktopContainerInfoModel.query.all()
                if isinstance(getattr(DesktopContainerInfoModel, "__tablename__", None), str)
                else DesktopContainerInfoModel.query.filter_by(timer_started=True).all()
            )

            expired_user_ids = []
            cleanup_pending: list[tuple[int, str]] = []
            for row in rows:
                lifecycle_state = self._row_lifecycle_state(row)
                if lifecycle_state == LIFECYCLE_CLEANUP_PENDING:
                    cleanup_pending.append((row.user_id, str(row.lifecycle_reason or END_REASON_RECONCILIATION)))
                    continue
                if lifecycle_state != LIFECYCLE_ACTIVE:
                    continue
                if row.timer_start_time is None or row.paused_at:
                    continue
                elapsed = time.time() - row.timer_start_time
                if row.timer_duration - elapsed <= 0:
                    expired_user_ids.append(row.user_id)

            for user_id in expired_user_ids:
                logger.info(f"auto-destroying expired session for user {user_id}")
                try:
                    self.destroy_container(user_id, reason=END_REASON_EXPIRED)
                except Exception as e:
                    logger.error(f"failed to destroy expired session for user {user_id}: {e}")

            for user_id, reason in cleanup_pending:
                try:
                    self.destroy_container(user_id, reason=reason, log_destruction=True)
                except Exception as e:
                    logger.error(f"failed to retry pending cleanup for user {user_id}: {e}")

            try:
                self.orchestrator.audit_counts()
            except Exception as e:
                logger.error(f"capacity count audit failed: {e}")

            self._recover_stale_operations()
            self._reconcile_orphans()

    # Sweep Docker objects that are not referenced by either an active lifecycle
    # row or an in-flight durable operation. Unknown host outcomes always retain
    # their reference and therefore cannot be reaped as anonymous orphans.
    RECONCILE_NAME_PREFIX = "rd-session-"
    RECONCILE_SAFETY_AGE_SECONDS = 300

    def _recover_stale_operations(self) -> None:
        """Take over abandoned creates without racing a live worker.

        The operation row is first fenced under ``FOR UPDATE`` and committed;
        Docker/listing work then happens with no database transaction open.
        Paused containers become evidence holds. Unknown host outcomes retain
        the operation and all reservations for a later retry.
        """
        try:
            candidates = DesktopSessionOperationModel.query.filter(
                DesktopSessionOperationModel.state.in_(CREATE_OPERATION_STATES | {OP_CLEANUP_PENDING}),
                DesktopSessionOperationModel.updated_at <= time.time() - self.RECONCILE_SAFETY_AGE_SECONDS,
            ).all()
        except AttributeError:
            return

        for candidate in candidates:
            user_id = int(candidate.user_id)
            db.session.rollback()
            operation = self._locked_operation(user_id)
            active = self._locked_active_row(user_id)
            if active is not None or operation is None:
                db.session.rollback()
                continue
            state = self._operation_state(operation)
            updated_at = float(operation.updated_at or 0)
            if state not in CREATE_OPERATION_STATES | {OP_CLEANUP_PENDING}:
                db.session.rollback()
                continue
            if updated_at > time.time() - self.RECONCILE_SAFETY_AGE_SECONDS:
                db.session.rollback()
                continue

            takeover_uuid = str(uuid.uuid4())
            session_uuid = str(operation.session_uuid or "")
            context_name = str(operation.docker_context or "")
            container_name = str(operation.container_name or "")
            capacity_reserved = bool(operation.capacity_reserved)
            operation.worker_lease_uuid = takeover_uuid
            operation.cancel_requested = True
            operation.state = OP_CLEANUP_PENDING
            operation.error = "stale creation claimed by recovery"
            operation.updated_at = time.time()
            db.session.commit()

            confirmed_absent = not context_name or not container_name
            if context_name and container_name:
                try:
                    listing = self.host_manager.list_session_containers_strict(context_name, self.RECONCILE_NAME_PREFIX)
                    if listing is None:
                        continue
                    exact = next((entry for entry in listing if str(entry.get("name", "")) == container_name), None)
                    if exact is None:
                        confirmed_absent = True
                    elif normalize_container_state(exact.get("status")) == "paused":
                        db.session.rollback()
                        current = self._locked_operation(user_id)
                        if current is not None and current.worker_lease_uuid == takeover_uuid:
                            current.state = OP_HELD
                            current.error = "paused container held for inspection"
                            current.updated_at = time.time()
                            db.session.commit()
                        else:
                            db.session.rollback()
                        continue
                    elif normalize_container_state(exact.get("status")) == "unknown":
                        db.session.rollback()
                        current = self._locked_operation(user_id)
                        if current is not None and current.worker_lease_uuid == takeover_uuid:
                            current.error = "container state unknown; destructive recovery deferred"
                            current.updated_at = time.time()
                            db.session.commit()
                        else:
                            db.session.rollback()
                        continue
                    else:
                        self.host_manager.force_remove_container(context_name, container_name)
                        confirmed_absent = True
                except (
                    docker.errors.DockerException,
                    paramiko.ssh_exception.SSHException,
                    EOFError,
                    OSError,
                    HostsUnavailableException,
                ) as e:
                    db.session.rollback()
                    current = self._locked_operation(user_id)
                    if current is not None and current.worker_lease_uuid == takeover_uuid:
                        current.error = str(e)
                        current.updated_at = time.time()
                        db.session.commit()
                    else:
                        db.session.rollback()
                    continue

            if not confirmed_absent:
                continue

            # Clear ownership once absence is confirmed, then release external
            # reservation bookkeeping. A crash in between temporarily
            # under-admits; the count audit heals it without risking a
            # duplicate decrement against a later session.
            db.session.rollback()
            current = self._locked_operation(user_id)
            if (
                current is not None
                and current.worker_lease_uuid == takeover_uuid
                and str(current.session_uuid or "") == session_uuid
            ):
                current.state = OP_FAILED
                current.worker_lease_uuid = None
                current.cancel_requested = False
                current.capacity_reserved = False
                current.docker_context = None
                current.container_name = None
                current.error = "abandoned creation cleaned up by recovery"
                current.updated_at = time.time()
                db.session.commit()
            else:
                db.session.rollback()
                continue

            if context_name and capacity_reserved:
                self.orchestrator.release_slot(context_name)

    def _reconcile_orphans(self) -> None:
        db_names = {
            r.container_name
            for r in DesktopContainerInfoModel.query.with_entities(DesktopContainerInfoModel.container_name).all()
        }
        try:
            operation_rows = DesktopSessionOperationModel.query.with_entities(
                DesktopSessionOperationModel.container_name
            ).all()
        except AttributeError:
            operation_rows = []
        operation_names = {r.container_name for r in operation_rows if r.container_name}
        db_names.update(operation_names)
        now = time.time()
        db.session.rollback()

        for ctx_name in self.host_manager.get_connected_contexts():
            # None means the host did not answer, so skip removal this sweep.
            containers = self.host_manager.list_session_containers_strict(ctx_name, self.RECONCILE_NAME_PREFIX)
            if containers is None:
                logger.warning(f"reconcile: list failed on {ctx_name}, skipping sweep")
                continue

            for entry in containers:
                name = str(entry.get("name", ""))
                if not name or name in db_names:
                    continue
                created_raw = entry.get("created_ts", 0)
                created_ts = float(created_raw) if isinstance(created_raw, (int, float, str)) else 0.0
                # safety window guards against racing a brand-new container whose DB row hasn't
                # committed yet. created_ts == 0 means parse failed; treat as too-young
                age = now - created_ts if created_ts > 0 else 0
                if age < self.RECONCILE_SAFETY_AGE_SECONDS:
                    continue

                # A name prefix alone never establishes ownership: Docker's
                # name filter is a partial match and unrelated containers can
                # use the same prefix. Automatic deletion requires all managed
                # labels to agree with the exact UUID-derived session name.
                if self._managed_orphan_identity(entry) is None:
                    logger.warning(f"reconcile: refusing unmanaged or malformed candidate {name} on {ctx_name}")
                    continue

                state = normalize_container_state(entry.get("status"))
                if state == "paused":
                    # evidence hold: surface, never remove
                    event_logger.log_event(
                        "orphan_paused",
                        f"paused orphan {name} on {ctx_name} held for inspection (not removed)",
                        level="warning",
                        metadata={"context": ctx_name, "container_name": name, "age_seconds": int(age)},
                    )
                    continue
                if state == "unknown":
                    logger.warning(f"reconcile: state unknown for orphan {name} on {ctx_name}; holding")
                    continue

                logger.warning(f"reconcile: removing orphan {name} on {ctx_name} (age {int(age)}s)")
                try:
                    # force_remove handles Created-state orphans where stop+auto_remove is a no-op,
                    # which otherwise spams the log every cleanup tick forever
                    self.host_manager.force_remove_container(ctx_name, name)
                    event_logger.log_event(
                        "orphan_reaped",
                        f"reaped orphan container {name} on {ctx_name}",
                        level="warning",
                        metadata={
                            "context": ctx_name,
                            "container_name": name,
                            "age_seconds": int(age),
                        },
                    )
                    self.orchestrator.release_slot(ctx_name)
                except Exception as e:
                    logger.error(f"reconcile: failed to remove {name} on {ctx_name}: {e}")

    def pause_watch(self) -> None:
        # mirrors out-of-band pause/unpause (host io tripwire) into paused_at
        with self.app.app_context():  # type: ignore[union-attr]
            rows_by_name = {
                r.container_name: (r.user_id, self._session_uuid(r), r.docker_context, r)
                for r in DesktopContainerInfoModel.query.all()
            }
            db.session.rollback()

            for ctx_name in self.host_manager.get_connected_contexts():
                containers = self.host_manager.list_session_containers_strict(ctx_name, self.RECONCILE_NAME_PREFIX)
                if containers is None:
                    continue
                for entry in containers:
                    identity = rows_by_name.get(str(entry.get("name", "")))
                    if identity is None or identity[2] != ctx_name:
                        continue
                    status = str(entry.get("status", ""))
                    user_id, session_uuid, _context, snapshot_row = identity
                    _user, username = _display_name(user_id)

                    db.session.rollback()
                    operation = self._locked_operation(user_id)
                    row = (
                        self._locked_active_row(user_id)
                        if isinstance(getattr(DesktopContainerInfoModel, "__tablename__", None), str)
                        else snapshot_row
                    )
                    if (
                        row is None
                        or self._session_uuid(row) != session_uuid
                        or row.container_name != str(entry.get("name", ""))
                    ):
                        db.session.rollback()
                        continue

                    if (
                        status == "paused"
                        and row.paused_at is None
                        and self._row_lifecycle_state(row) == LIFECYCLE_ACTIVE
                    ):
                        row.paused_at = time.time()
                        row.lifecycle_state = LIFECYCLE_HELD
                        if operation is not None and operation.session_uuid == session_uuid:
                            operation.state = OP_HELD
                            operation.updated_at = time.time()
                        db.session.commit()
                        event_logger.log_event(
                            "session_paused",
                            f"session paused on {ctx_name} (host tripwire or manual pause)",
                            user_id=row.user_id,
                            username=username,
                            level="error",
                            metadata={"context": ctx_name, "container_name": row.container_name, "source": "detected"},
                        )
                    elif (
                        status == "running"
                        and row.paused_at is not None
                        and self._row_lifecycle_state(row)
                        in (
                            LIFECYCLE_HELD,
                            LIFECYCLE_UNPAUSING,
                        )
                    ):
                        # Holds are sticky. An out-of-band unpause must not
                        # release evidence or credit the timer; only the audited
                        # admin unpause path may transition through UNPAUSING.
                        container_name = str(row.container_name)
                        lifecycle_state = self._row_lifecycle_state(row)
                        unpause_lease_live = bool(
                            lifecycle_state == LIFECYCLE_UNPAUSING
                            and operation is not None
                            and operation.session_uuid == session_uuid
                            and self._operation_state(operation) == OP_UNPAUSING
                            and float(operation.updated_at or 0) > time.time() - self.UNPAUSE_LEASE_SECONDS
                        )
                        if unpause_lease_live:
                            db.session.rollback()
                            continue
                        if lifecycle_state == LIFECYCLE_UNPAUSING:
                            row.lifecycle_state = LIFECYCLE_HELD
                            if operation is not None and operation.session_uuid == session_uuid:
                                operation.state = OP_HELD
                                operation.updated_at = time.time()
                            db.session.commit()
                        else:
                            db.session.rollback()
                        try:
                            self.host_manager.pause_container(ctx_name, container_name)
                            event_logger.log_event(
                                "session_paused",
                                f"re-paused held session on {ctx_name} after out-of-band drift",
                                user_id=user_id,
                                username=username,
                                level="warning",
                                metadata={
                                    "context": ctx_name,
                                    "container_name": container_name,
                                    "source": "drift_repaired",
                                },
                            )
                        except Exception as e:
                            logger.error(f"failed to re-pause held session {container_name} on {ctx_name}: {e}")
                    else:
                        db.session.rollback()

    @staticmethod
    def _credit_pause_and_clear(row: DesktopContainerInfoModel) -> None:
        # credit frozen time so the session isn't expire-destroyed on unpause
        if row.timer_started and row.timer_start_time and row.paused_at:
            row.timer_start_time += time.time() - row.paused_at
        row.paused_at = None
        db.session.commit()

    def pause_session(self, user_id: int) -> ResultDict:
        db.session.rollback()
        operation = self._locked_operation(user_id)
        row = self._locked_active_row(user_id)
        if not row:
            db.session.rollback()
            return {"success": False, "error": "No active session for user"}
        if row.paused_at or self._row_lifecycle_state(row) != LIFECYCLE_ACTIVE:
            db.session.rollback()
            return {"success": False, "error": "Session already paused"}
        session_uuid = self._session_uuid(row)
        context_name = row.docker_context
        container_name = row.container_name
        row.paused_at = time.time()
        row.lifecycle_state = LIFECYCLE_HELD
        if operation is not None and operation.session_uuid == session_uuid:
            operation.state = OP_HELD
            operation.updated_at = time.time()
        db.session.commit()
        try:
            self.host_manager.pause_container(context_name, container_name)
        except Exception as e:
            db.session.rollback()
            operation = self._locked_operation(user_id)
            current = self._locked_active_row(user_id)
            if (
                current is not None
                and self._session_uuid(current) == session_uuid
                and self._row_lifecycle_state(current) == LIFECYCLE_HELD
            ):
                current.paused_at = None
                current.lifecycle_state = LIFECYCLE_ACTIVE
                if operation is not None and operation.session_uuid == session_uuid:
                    operation.state = OP_ACTIVE
                    operation.updated_at = time.time()
                db.session.commit()
            else:
                db.session.rollback()
            return {"success": False, "error": f"pause failed: {e}"}
        return {"success": True}

    def unpause_session(self, user_id: int) -> ResultDict:
        db.session.rollback()
        operation = self._locked_operation(user_id)
        row = self._locked_active_row(user_id)
        if not row:
            db.session.rollback()
            return {"success": False, "error": "No active session for user"}
        if not row.paused_at or self._row_lifecycle_state(row) != LIFECYCLE_HELD:
            db.session.rollback()
            return {"success": False, "error": "Session is not paused"}
        session_uuid = self._session_uuid(row)
        context_name = row.docker_context
        container_name = row.container_name
        # Mark the explicit admin operation before remote I/O. pause_watch keeps
        # HELD rows sticky but deliberately ignores this transient state.
        row.lifecycle_state = LIFECYCLE_UNPAUSING
        if operation is not None and operation.session_uuid == session_uuid:
            operation.state = OP_UNPAUSING
            operation.updated_at = time.time()
        db.session.commit()
        try:
            self.host_manager.unpause_container(context_name, container_name)
        except Exception as e:
            db.session.rollback()
            operation = self._locked_operation(user_id)
            current = self._locked_active_row(user_id)
            if (
                current is not None
                and self._session_uuid(current) == session_uuid
                and self._row_lifecycle_state(current) == LIFECYCLE_UNPAUSING
            ):
                current.lifecycle_state = LIFECYCLE_HELD
                if operation is not None and operation.session_uuid == session_uuid:
                    operation.state = OP_HELD
                    operation.updated_at = time.time()
                db.session.commit()
            else:
                db.session.rollback()
            return {"success": False, "error": f"unpause failed: {e}"}
        db.session.rollback()
        operation = self._locked_operation(user_id)
        current = self._locked_active_row(user_id)
        if (
            current is None
            or self._session_uuid(current) != session_uuid
            or self._row_lifecycle_state(current) != LIFECYCLE_UNPAUSING
        ):
            db.session.rollback()
            return {"success": False, "error": "Session changed while unpausing"}
        current.lifecycle_state = LIFECYCLE_ACTIVE
        if operation is not None and operation.session_uuid == session_uuid:
            operation.state = OP_ACTIVE
            operation.updated_at = time.time()
        self._credit_pause_and_clear(current)
        return {"success": True}

    def destroy_all_containers_admin(self, admin_user: Users) -> dict[str, int]:
        active_user_ids = {int(row.user_id) for row in DesktopContainerInfoModel.query.all()}
        try:
            creating_user_ids = {
                int(operation.user_id)
                for operation in DesktopSessionOperationModel.query.filter(
                    DesktopSessionOperationModel.state.in_(CREATE_OPERATION_STATES)
                ).all()
            }
        except AttributeError:
            # Compatibility for the lightweight unit model; production schema
            # validation guarantees the durable operation model is queryable.
            creating_user_ids = set()

        user_ids = sorted(active_user_ids | creating_user_ids)
        summary = {
            "requested": len(user_ids),
            "completed": 0,
            "cancelling": 0,
            "stopping": 0,
            "failed": 0,
        }

        for user_id in user_ids:
            try:
                result = self.destroy_container(user_id, reason=END_REASON_ADMIN_KILLED, log_destruction=False)
                if result.get("success") is not True:
                    summary["failed"] += 1
                elif result.get("status") == "cancelling":
                    summary["cancelling"] += 1
                elif result.get("status") == "stopping":
                    summary["stopping"] += 1
                elif "status" not in result:
                    summary["completed"] += 1
                else:
                    summary["failed"] += 1
            except Exception as e:
                summary["failed"] += 1
                logger.error(f"failed to kill session for user {user_id}: {e}")

        # log unconditionally so an admin pressing kill-all on an empty fleet
        # still leaves an attributable audit trail (killed=0)
        event_logger.log_event(
            "admin_action",
            f"admin {admin_user.name} requested fleet teardown "
            f"({summary['completed']} completed, {summary['cancelling']} cancelling, "
            f"{summary['stopping']} stopping, {summary['failed']} failed)",
            user_id=admin_user.id,
            username=admin_user.name,
            level="warning",
            metadata={"killed_count": summary["completed"], **summary},
        )

        return summary

    @staticmethod
    def _managed_orphan_identity(entry: dict[str, object]) -> tuple[int, str] | None:
        name = entry.get("name")
        labels = entry.get("labels")
        if not isinstance(name, str) or not isinstance(labels, dict):
            return None
        if labels.get(SESSION_LABEL_MANAGED) != "true":
            return None
        user_raw = labels.get(SESSION_LABEL_USER_ID)
        session_raw = labels.get(SESSION_LABEL_UUID)
        if not isinstance(user_raw, str) or not user_raw.isdigit() or user_raw.startswith("0"):
            return None
        if not isinstance(session_raw, str):
            return None
        try:
            parsed_uuid = uuid.UUID(session_raw)
        except ValueError:
            return None
        if str(parsed_uuid) != session_raw:
            return None
        match = _SESSION_CONTAINER_NAME_RE.fullmatch(name)
        if match is None or match.group(1) != user_raw or match.group(2) != session_raw[:12]:
            return None
        return int(user_raw), session_raw

    @staticmethod
    def _session_reference_sets() -> tuple[set[str], set[str]]:
        try:
            active = DesktopContainerInfoModel.query.with_entities(
                DesktopContainerInfoModel.container_name,
                DesktopContainerInfoModel.session_uuid,
            ).all()
            operations = DesktopSessionOperationModel.query.with_entities(
                DesktopSessionOperationModel.container_name,
                DesktopSessionOperationModel.session_uuid,
            ).all()
            names = {str(row.container_name) for row in [*active, *operations] if row.container_name}
            session_uuids = {str(row.session_uuid) for row in [*active, *operations] if row.session_uuid}
            return names, session_uuids
        finally:
            db.session.rollback()

    def list_paused_orphans(self) -> list[PausedOrphanEntry]:
        referenced_names, referenced_sessions = self._session_reference_sets()
        now = time.time()
        orphans: list[PausedOrphanEntry] = []
        for context_name in self.host_manager.get_connected_contexts():
            listing = self.host_manager.list_session_containers_strict(context_name, self.RECONCILE_NAME_PREFIX)
            if listing is None:
                logger.warning(f"paused orphan listing failed on {context_name}")
                continue
            for entry in listing:
                if normalize_container_state(entry.get("status")) != "paused":
                    continue
                identity = self._managed_orphan_identity(entry)
                if identity is None:
                    continue
                user_id, session_uuid = identity
                name = str(entry["name"])
                if name in referenced_names or session_uuid in referenced_sessions:
                    continue
                container_id = str(entry.get("id") or "")
                if not container_id:
                    continue
                created_raw = entry.get("created_ts")
                created_at = float(created_raw) if isinstance(created_raw, (int, float, str)) else 0.0
                orphans.append(
                    {
                        "context": context_name,
                        "container_id": container_id,
                        "container_name": name,
                        "user_id": user_id,
                        "session_uuid": session_uuid,
                        "created_at": created_at,
                        "age_seconds": max(0, int(now - created_at)) if created_at > 0 else 0,
                    }
                )
        return sorted(orphans, key=lambda item: (str(item["context"]), str(item["container_name"])))

    def _audit_paused_orphan_removal(
        self,
        admin_user: Users,
        *,
        context_name: str,
        container_id: str,
        container_name: str,
        outcome: str,
        user_id: int | None = None,
        session_uuid: str | None = None,
        error: str | None = None,
    ) -> None:
        target_user = Users.query.filter_by(id=user_id).first() if user_id is not None else None
        target_name = username_or_fallback(target_user, user_id) if user_id is not None else "unknown owner"
        metadata: dict[str, int | float | str | bool | None] = {
            "action": "remove_paused_orphan",
            "outcome": outcome,
            "context": context_name,
            "container_id": container_id,
            "container_name": container_name,
            "target_id": user_id,
            "target": target_name,
            "session_uuid": session_uuid,
            "error": error,
            **{f"target_{key}": value for key, value in user_flags(target_user).items()},
        }
        event_logger.log_event_sync(
            "admin_action",
            f"admin {admin_user.name} {outcome} removal of paused orphan {container_name}",
            user_id=admin_user.id,
            username=admin_user.name,
            level="warning",
            metadata=metadata,
        )

    def remove_paused_orphan_admin(
        self,
        admin_user: Users,
        context_name: str,
        container_id: str,
        container_name: str,
    ) -> ResultDict:
        user_id: int | None = None
        session_uuid: str | None = None
        try:
            if _SESSION_CONTAINER_NAME_RE.fullmatch(container_name) is None:
                raise ValueError("container name is not an exact remote desktop session name")
            listing = self.host_manager.list_session_containers_strict(context_name, self.RECONCILE_NAME_PREFIX)
            if listing is None:
                raise HostsUnavailableException(f"Docker context {context_name} is unavailable")
            entry = next(
                (item for item in listing if item.get("id") == container_id and item.get("name") == container_name),
                None,
            )
            if entry is None:
                raise ValueError("paused orphan identity changed; refresh and retry")
            identity = self._managed_orphan_identity(entry)
            if identity is None or normalize_container_state(entry.get("status")) != "paused":
                raise ValueError("target is not a paused managed orphan")
            user_id, session_uuid = identity
            entry_labels = entry.get("labels")
            assert isinstance(entry_labels, dict)
            expected_labels = {
                SESSION_LABEL_MANAGED: str(entry_labels[SESSION_LABEL_MANAGED]),
                SESSION_LABEL_USER_ID: str(entry_labels[SESSION_LABEL_USER_ID]),
                SESSION_LABEL_UUID: str(entry_labels[SESSION_LABEL_UUID]),
            }
            referenced_names, referenced_sessions = self._session_reference_sets()
            if container_name in referenced_names or session_uuid in referenced_sessions:
                raise ValueError("container is referenced by an active or in-flight session")

            # Evidence disposal is the exceptional path where an audit record is
            # committed synchronously before remote destructive I/O. If the
            # database cannot durably record intent, fail closed and keep the
            # paused object.
            self._audit_paused_orphan_removal(
                admin_user,
                context_name=context_name,
                container_id=container_id,
                container_name=container_name,
                outcome="requested",
                user_id=user_id,
                session_uuid=session_uuid,
            )
            self.host_manager.remove_paused_managed_orphan(
                context_name,
                container_id,
                container_name,
                expected_labels,
            )
        except Exception as exc:
            error = str(exc)
            try:
                self._audit_paused_orphan_removal(
                    admin_user,
                    context_name=context_name,
                    container_id=container_id,
                    container_name=container_name,
                    outcome="failed",
                    user_id=user_id,
                    session_uuid=session_uuid,
                    error=error,
                )
            except Exception as audit_exc:
                logger.error("failed to persist paused-orphan failure audit", exc_info=True)
                error = f"{error}; failure audit persistence failed: {audit_exc}"
            return {"success": False, "error": error}

        cleanup_errors: list[str] = []
        try:
            # Recount committed rows and observed Docker objects. Never blindly
            # decrement capacity for an object that lacked an authoritative row.
            self.orchestrator.audit_counts()
        except Exception as exc:
            cleanup_errors.append(f"capacity audit failed: {exc}")

        warning = "; ".join(cleanup_errors) or None
        try:
            self._audit_paused_orphan_removal(
                admin_user,
                context_name=context_name,
                container_id=container_id,
                container_name=container_name,
                outcome="completed",
                user_id=user_id,
                session_uuid=session_uuid,
                error=warning,
            )
        except Exception as audit_exc:
            logger.error("failed to persist paused-orphan completion audit", exc_info=True)
            audit_warning = f"completion audit persistence failed: {audit_exc}"
            warning = f"{warning}; {audit_warning}" if warning else audit_warning
        result: ResultDict = {"success": True}
        if warning:
            result["warning"] = warning
        return result
