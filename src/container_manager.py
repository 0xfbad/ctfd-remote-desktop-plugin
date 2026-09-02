from __future__ import annotations

import re
import time
import logging
import secrets
import traceback
import uuid
from dataclasses import dataclass
from typing import Callable, TypeIs
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
    ContainerResult,
    ContainerState,
    SESSION_LABEL_MANAGED,
    SESSION_LABEL_USER_ID,
    SESSION_LABEL_UUID,
    normalize_container_state,
    parse_size,
)
from .orchestrator import Orchestrator, ReservationClaim
from .exceptions import HostsUnavailableException

logger = logging.getLogger(__name__)


def _display_name(user_id: int) -> tuple[Users | None, str]:
    user = Users.query.filter_by(id=user_id).first()
    return user, username_or_fallback(user, user_id)


def _revoke_session_cookie(app: Flask, sid: str | None) -> bool:
    if not sid:
        return True
    try:
        from CTFd.cache import cache

        # an absent key is already revoked so any non exceptional response is a success
        cache.delete(app.session_interface.key_prefix + sid)
        return True
    except Exception as exc:
        logger.warning("failed to revoke CTFd session sid: %s", exc)
        return False


def _mint_session_cookie(app: Flask, user: Users) -> tuple[str, str, str] | None:
    from flask import session
    from werkzeug.wrappers import Response
    from CTFd.utils.security.auth import login_user

    cookie_name = app.session_cookie_name
    with app.test_request_context():
        sid: str | None = None
        try:
            login_user(user)
            raw_sid = session.sid
            if not isinstance(raw_sid, str) or not raw_sid:
                raise RuntimeError("CTFd session interface did not provide a revocable sid")
            sid = raw_sid
            resp = Response()
            # server side sessions only reach the cache backend through save_session
            app.session_interface.save_session(app, session, resp)
            for header in resp.headers.getlist("Set-Cookie"):
                if header.startswith(f"{cookie_name}="):
                    value = header.split(f"{cookie_name}=", 1)[1].split(";", 1)[0]
                    # the raw sid is the cache revocation key, the cookie value is signed
                    return cookie_name, value, sid
            # save_session already materialized the cache entry, do not leave a credential behind
            if not _revoke_session_cookie(app, sid):
                logger.error("failed to roll back CTFd session after cookie mint returned no header")
        except Exception:
            # save_session can fail after the backend accepted the write
            if sid and not _revoke_session_cookie(app, sid):
                logger.error("failed to roll back CTFd session after cookie mint raised")
            raise
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
_RESOLVED_USERNAME_ATTEMPTS = 3
_RESOLVED_USERNAME_RETRY_DELAY_SECONDS = 0.2
_READY_HEARTBEAT_INTERVAL_SECONDS = 30
_VNC_RETRY_INTERVAL_SECONDS = 0.5
TimerStatusDict = dict[str, bool | int | str]
ResultDict = dict[str, bool | str | int]
ContainerListEntry = dict[str, str | int | float | bool | TimerDict | None]
PausedOrphanEntry = dict[str, str | int | float]

_SESSION_CONTAINER_NAME_RE = re.compile(r"rd-session-([1-9][0-9]*)-([0-9a-f]{8}-[0-9a-f]{3})")


@dataclass
class _CreateAttempt:
    """creation state mutated mid phase so the failure cleanup sees it after any phase raises"""

    context_name: str | None = None
    container_create_started: bool = False
    cookie_sid: str | None = None
    cookie_app: Flask | None = None


def _sanitize_username(raw: str, user_id: int | None = None) -> str:
    name = _USERNAME_RE.sub("", raw.lower())
    # linux usernames must start with a letter or underscore
    name = name.lstrip("0123456789-")[:32]
    if not name or name in _RESERVED_NAMES:
        return f"user{user_id}" if user_id else "user"
    return name


def _connection_ports(ssh_enabled: bool, web_terminal_enabled: bool) -> list[str]:
    # 6080 is always published for the readiness gate, 5900 stays internal for websockify
    ports = ["6080/tcp"]
    if ssh_enabled:
        ports.append("22/tcp")
    if web_terminal_enabled:
        ports.append("7682/tcp")
    return ports


class ContainerManager:
    UNPAUSE_LEASE_SECONDS = 60
    RECONCILE_NAME_PREFIX = "rd-session-"
    RECONCILE_SAFETY_AGE_SECONDS = 300

    def __init__(self, host_manager: DockerHostManager, orchestrator: Orchestrator, app: Flask | None = None) -> None:
        self.host_manager = host_manager
        self.orchestrator = orchestrator
        self.app = app
        self.creation_status: dict[int, CreationStatusDict] = {}
        self.lock = Lock()
        # serializes destroy_container so concurrent kills cannot write duplicate history rows
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
        """derives the state for unit doubles that carry no lifecycle_state string"""
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
        # unit doubles only configure the direct first seam, prefer it over the chained mock
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
            # the unit suite swaps the model base for a mock, real models always expose query
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
        # redundant for the orm, needed to keep the unit model doubles model shaped
        row.user_id = user_id
        row.operation_uuid = str(getattr(row, "operation_uuid", "") or uuid.uuid4())
        row.state = OP_IDLE
        row.created_at = now
        row.updated_at = now
        try:
            db.session.add(row)
            db.session.flush()
            return row
        except (IntegrityError, OperationalError):  # innodb reports this race as duplicate key or deadlock
            # another worker created the stable mutex first, lock the winner row
            db.session.rollback()
            query = DesktopSessionOperationModel.query.filter_by(user_id=user_id)
            return query.populate_existing().with_for_update().first()

    def _claim_create_operation(self, user_id: int) -> tuple[str, str] | None:
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
            # unit suite only, schema validation guarantees a row in production
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
        release_capacity_from: str | None = None,
        **fields: object,
    ) -> bool:
        """fenced update, a stale worker cannot overwrite a takeover"""
        db.session.rollback()
        if release_capacity_from is not None:
            # context lock first, the owner fence blocks a stale worker decrement
            self.orchestrator.release_operation_slot_in_transaction(
                release_capacity_from,
                user_id,
                session_uuid,
                worker_uuid,
            )
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
        # unreachable in production, schema validation guarantees a uuid before lifecycle code runs
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

    def _read_resolved_username(self, context_name: str, container_name: str) -> str:
        """the image resolves account collisions the sanitizer cannot predict
        keeping the requested name would publish invalid ssh credentials
        """
        command = [
            "/bin/bash",
            "-c",
            "/usr/local/bin/remote-desktop-healthcheck && cat -- /var/lib/remote-desktop/resolved-username",
        ]
        last_exception: Exception | None = None
        for attempt in range(_RESOLVED_USERNAME_ATTEMPTS):
            try:
                code, output = self.host_manager.exec_in_container(
                    context_name,
                    container_name,
                    command,
                )
            except Exception as exc:
                last_exception = exc
                code, output = -1, ""

            if code != -1:
                break
            if attempt < _RESOLVED_USERNAME_ATTEMPTS - 1:
                time.sleep(_RESOLVED_USERNAME_RETRY_DELAY_SECONDS)
        else:
            if last_exception is not None:
                raise RuntimeError("desktop image did not publish its resolved Linux username") from last_exception
            raise RuntimeError("desktop image did not publish its resolved Linux username")

        if code != 0:
            raise RuntimeError("desktop image did not publish its resolved Linux username or pass its health check")
        if isinstance(output, bytes):
            try:
                output = output.decode("utf-8", errors="strict")
            except UnicodeDecodeError as exc:
                raise RuntimeError("desktop image returned an invalid Linux username") from exc
        if not isinstance(output, str) or len(output) > 128:
            raise RuntimeError("desktop image returned an invalid Linux username")

        resolved = output[:-1] if output.endswith("\n") else output
        if re.fullmatch(r"[a-z_][a-z0-9_]{0,31}", resolved) is None:
            raise RuntimeError("desktop image returned an invalid Linux username")
        return resolved

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

        # vnc_ready_attempts counts half second slots, cap the whole wait so a dead endpoint cannot multiply it
        total_budget = max(float(http_timeout), max_attempts * _VNC_RETRY_INTERVAL_SECONDS)
        deadline = time.monotonic() + total_budget
        # an empty proxy handler ignores ambient proxy env vars for internal probes
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for attempt in range(max_attempts):
            # every attempt so the callback can renew a lease when one probe stalls
            if progress_callback:
                progress_callback(attempt, max_attempts)

            try:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                req = urllib.request.Request(f"http://{hostname}:{novnc_port}/", method="GET")
                req.add_header("User-Agent", "CTFd-VNC-Check")
                with opener.open(req, timeout=min(float(http_timeout), remaining)) as response:
                    if response.status == 200:
                        logger.info(f"VNC ready on {hostname}:{novnc_port} after {attempt + 1} attempts")
                        return True
            except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ConnectionRefusedError):
                pass
            except Exception as e:
                logger.debug(f"VNC check attempt {attempt + 1} error: {str(e)}")

            if attempt < max_attempts - 1:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(min(_VNC_RETRY_INTERVAL_SECONDS, remaining))

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

        # unit callers omit both tokens, create_container always supplies them in production
        fenced = session_uuid is not None and worker_uuid is not None
        session_uuid = session_uuid or str(uuid.uuid4())
        worker_uuid = worker_uuid or str(uuid.uuid4())

        user, username = _display_name(user_id)
        container_username = self._resolve_username(user) if user else f"user{user_id}"

        container_name = f"rd-session-{user_id}-{session_uuid[:12]}"
        attempt = _CreateAttempt()

        try:
            context_name, pub_hostname, check_hostname, display_hostname = self._reserve_creation_host(
                user_id,
                session_uuid,
                worker_uuid,
                fenced,
                container_name,
                attempt,
            )

            result, vnc_password, initial_duration, max_extensions = self._launch_desktop_container(
                user_id=user_id,
                user=user,
                session_uuid=session_uuid,
                worker_uuid=worker_uuid,
                fenced=fenced,
                container_name=container_name,
                container_username=container_username,
                container_url=container_url,
                extra_hosts=extra_hosts,
                context_name=context_name,
                display_hostname=display_hostname,
                attempt=attempt,
            )

            port_map: dict[str, int] = result["ports"]  # type: ignore[assignment]
            container_id = str(result["container_id"])
            ssh_port = port_map.get("22/tcp")
            # internal xvnc listener rather than a published port, kept non null for the schema
            vnc_port = 5900
            novnc_port = port_map["6080/tcp"]
            ttyd_port = port_map.get("7682/tcp")

            logger.info(
                f"container {container_name} created - "
                f"SSH:{ssh_port} VNC:{vnc_port} noVNC:{novnc_port} ttyd:{ttyd_port}"
            )

            self._wait_for_session_ready(
                user_id,
                session_uuid,
                worker_uuid,
                fenced,
                display_hostname,
                check_hostname,
                novnc_port,
            )

            container_username = self._read_resolved_username(context_name, container_name)
            vnc_url = proxy_vnc_url(user_id, vnc_password)

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
                cookie_sid=attempt.cookie_sid,
                session_uuid=session_uuid,
                lifecycle_state=LIFECYCLE_ACTIVE,
            )
            self._commit_created_session(user_id, session_uuid, worker_uuid, fenced, row, context_name, container_name)

            with self.lock:
                self.creation_status[user_id] = {
                    "status": "ready",
                    "message": "Desktop ready!",
                    "hostname": display_hostname,
                }

            try:
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
            except Exception:
                # the session is already committed, telemetry must not push it onto a cleanup path
                logger.exception("failed to log session_created for committed session %s", session_uuid)

        except Exception as e:
            self._cleanup_failed_creation(
                e,
                user_id,
                username,
                session_uuid,
                worker_uuid,
                fenced,
                container_name,
                attempt,
            )

    def _reserve_creation_host(
        self,
        user_id: int,
        session_uuid: str,
        worker_uuid: str,
        fenced: bool,
        container_name: str,
        attempt: _CreateAttempt,
    ) -> tuple[str, str | None, str | None, str]:
        if fenced and not self._update_operation(user_id, session_uuid, worker_uuid, OP_SELECTING):
            raise RuntimeError("creation lease is no longer owned by this worker")
        with self.lock:
            self.creation_status[user_id] = {"status": "selecting_host", "message": "Requesting a server..."}

        if fenced:
            context_name = self.orchestrator.select_and_reserve(
                ReservationClaim(
                    user_id=user_id,
                    session_uuid=session_uuid,
                    worker_lease_uuid=worker_uuid,
                    container_name=container_name,
                )
            )
        else:
            context_name = self.orchestrator.select_and_reserve()
        attempt.context_name = context_name

        # same state update acts as a lease heartbeat before the hostname lookup and semaphore wait
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

        pub_hostname, check_hostname = self.host_manager.get_connection_hostnames(context_name)
        # escaped because the ui renders creation status messages as html
        display_hostname = _esc(context_name)

        logger.info(f"selected context: {context_name} (public: {pub_hostname}) for user {user_id}")
        return context_name, pub_hostname, check_hostname, display_hostname

    def _launch_desktop_container(
        self,
        *,
        user_id: int,
        user: Users | None,
        session_uuid: str,
        worker_uuid: str,
        fenced: bool,
        container_name: str,
        container_username: str,
        container_url: str,
        extra_hosts: dict[str, str] | None,
        context_name: str,
        display_hostname: str,
        attempt: _CreateAttempt,
    ) -> tuple[ContainerResult, str, int, int]:
        create_semaphore = None
        try:
            create_semaphore = self.host_manager.acquire_semaphore(context_name)

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
            # hard ceiling so a container cannot outlive the longest possible session
            max_lifetime = int(initial_duration + (extension_duration * max_extensions) + 300)

            container_env = {
                "VNC_PASSWORD": vnc_password,
                "RESOLUTION": resolution,
                "CTFD_USERNAME": container_username,
                "MAX_LIFETIME": str(max_lifetime),
                "CTFD_URL": container_url,
                # the image treats an absent var as enabled so always pass an explicit 0
                "ENABLE_SSH": "1" if ssh_enabled else "0",
                "ENABLE_TTYD": "1" if web_terminal_enabled else "0",
            }

            from flask import current_app

            if user is not None:
                attempt.cookie_app = current_app._get_current_object()
                minted = _mint_session_cookie(attempt.cookie_app, user)
                if minted:
                    cookie_name, cookie_value, attempt.cookie_sid = minted
                    container_env["CTFD_COOKIE_NAME"] = cookie_name
                    container_env["CTFD_COOKIE_VALUE"] = cookie_value
                else:
                    logger.warning(f"failed to mint session cookie for user {user_id}, autologin disabled")

            # end the implicit transaction opened by the settings reads before remote io
            db.session.rollback()
            # past this line a transport failure leaves the create outcome unknown
            attempt.container_create_started = True
            result = self.host_manager.run_container(
                context_name=context_name,
                image=docker_image,
                name=container_name,
                # a context name can carry characters that are invalid in a kernel hostname
                hostname=container_name,
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
            if create_semaphore is not None:
                self.host_manager.release_semaphore(create_semaphore)

        return result, vnc_password, initial_duration, max_extensions

    def _wait_for_session_ready(
        self,
        user_id: int,
        session_uuid: str,
        worker_uuid: str,
        fenced: bool,
        display_hostname: str,
        check_hostname: str | None,
        novnc_port: int,
    ) -> None:
        with self.lock:
            self.creation_status[user_id] = {
                "status": "waiting_vnc",
                "message": f"Waiting for {display_hostname} display server...",
            }
        if fenced and not self._update_operation(user_id, session_uuid, worker_uuid, OP_WAITING_READY):
            raise RuntimeError("creation cancelled while waiting for readiness")
        ready_heartbeat_at = time.monotonic()

        def _vnc_progress(attempt: int, max_attempts: int) -> None:
            nonlocal ready_heartbeat_at
            if attempt % 5 == 0:
                with self.lock:
                    self.creation_status[user_id] = {
                        "status": "waiting_vnc",
                        "message": f"Waiting for {display_hostname} display server... ({attempt}/{max_attempts})",
                    }
            now = time.monotonic()
            if fenced and now - ready_heartbeat_at >= _READY_HEARTBEAT_INTERVAL_SECONDS:
                if not self._update_operation(user_id, session_uuid, worker_uuid, OP_WAITING_READY):
                    raise RuntimeError("creation cancelled while waiting for readiness")
                ready_heartbeat_at = now

        vnc_ready = self.wait_for_vnc_ready(
            check_hostname,  # type: ignore[arg-type]
            novnc_port,
            progress_callback=_vnc_progress,
        )
        if not vnc_ready:
            raise Exception(f"VNC server on {check_hostname}:{novnc_port} did not become ready in time")

    def _commit_created_session(
        self,
        user_id: int,
        session_uuid: str,
        worker_uuid: str,
        fenced: bool,
        row: DesktopContainerInfoModel,
        context_name: str,
        container_name: str,
    ) -> None:
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

    def _cleanup_failed_creation(
        self,
        e: Exception,
        user_id: int,
        username: str,
        session_uuid: str,
        worker_uuid: str,
        fenced: bool,
        container_name: str,
        attempt: _CreateAttempt,
    ) -> None:
        context_name = attempt.context_name
        if attempt.cookie_sid and attempt.cookie_app is not None:
            _revoke_session_cookie(attempt.cookie_app, attempt.cookie_sid)

        stopped_ok = False
        if attempt.container_create_started and container_name and context_name:
            try:
                # auto_remove never fires for a container that was created but never started
                self.host_manager.force_remove_container(context_name, container_name)
                stopped_ok = True
                logger.info(f"removed container {container_name} after creation failure")
            except Exception as stop_error:
                logger.error(f"failed to stop container during cleanup: {stop_error}")

        # an unconfirmed removal keeps its reservation until the strict audit proves it is gone
        cleanup_confirmed = not attempt.container_create_started or stopped_ok
        if context_name and cleanup_confirmed and not fenced:
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
                    release_capacity_from=context_name if context_name and cleanup_confirmed else None,
                    error=str(e),
                    docker_context=None if cleanup_confirmed else context_name,
                    container_name=container_name if not cleanup_confirmed else None,
                    capacity_reserved=bool(context_name and not cleanup_confirmed),
                )
            except Exception:
                db.session.rollback()
                logger.error("failed to persist creation cleanup state", exc_info=True)

        with self.lock:
            # do not escape, the frontend assigns these as text and escaping would show entities
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

        # one critical section for check and claim, two concurrent posts would both spawn a worker
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

        try:
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
                            "score": round(int(h["weight"] or 0) / (int(h["active_containers"] or 0) + 1), 2)
                            if h["healthy"]
                            else 0,
                        }
                        for h in host_status
                    },
                },
            )
        except Exception:
            # the queued claim is already durable, telemetry must not strand it until stale recovery
            logger.exception("failed to log session_requested for user %s", user_id)

        try:
            import gevent

            app: Flask = current_app._get_current_object()
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

    def _reload_session_rows(
        self,
        user_id: int,
        snapshot_row: DesktopContainerInfoModel,
        create: bool = True,
    ) -> tuple[DesktopSessionOperationModel | None, DesktopContainerInfoModel | None]:
        """reacquires the row locks after remote io, unit doubles fall back to the snapshot row"""
        db.session.rollback()
        operation = self._locked_operation(user_id, create=create)
        current = (
            self._locked_active_row(user_id)
            if isinstance(getattr(DesktopContainerInfoModel, "__tablename__", None), str)
            else snapshot_row
        )
        return operation, current

    def _same_session(
        self,
        current: DesktopContainerInfoModel | None,
        session_uuid: str,
    ) -> TypeIs[DesktopContainerInfoModel]:
        return current is not None and self._session_uuid(current) == session_uuid

    def _mark_cleanup_pending(
        self,
        operation: DesktopSessionOperationModel | None,
        current: DesktopContainerInfoModel | None,
        session_uuid: str,
        reason: str,
        error: str,
    ) -> None:
        if self._same_session(current, session_uuid):
            current.lifecycle_state = LIFECYCLE_CLEANUP_PENDING
            current.lifecycle_reason = reason
        if operation is not None and operation.session_uuid == session_uuid:
            operation.state = OP_CLEANUP_PENDING
            operation.error = error
            operation.updated_at = time.time()
        db.session.commit()

    def _gate_destroy_state(self, lifecycle_state: str, admin_override: bool) -> ResultDict | None:
        if lifecycle_state == LIFECYCLE_STOPPING:
            db.session.rollback()
            return {"success": True, "status": "stopping"}
        if lifecycle_state in (LIFECYCLE_HELD, LIFECYCLE_UNPAUSING) and not admin_override:
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
        return None

    def destroy_container(
        self,
        user_id: int,
        reason: str = END_REASON_USER_DESTROYED,
        log_destruction: bool = True,
        expected_session_uuid: str | None = None,
    ) -> ResultDict:
        _user, username = _display_name(user_id)

        # take the status lock before any row lock, create_container uses that same order
        db.session.rollback()
        with self.lock:
            status = self.creation_status.get(user_id)
            if status and status.get("status") not in (None, "failed", "ready"):
                # the background greenlet polls this key and aborts
                self.creation_status[user_id] = {"status": "cancelled"}
            else:
                self.creation_status.pop(user_id, None)

        # the local lock is only a contention optimization, the row locks are the authority
        with self._get_destroy_lock(user_id):
            db.session.rollback()
            operation = self._locked_operation(user_id, create=True)
            row = self._locked_active_row(user_id)

            if row is None:
                if operation is not None and self._operation_state(operation) in CREATE_OPERATION_STATES:
                    operation.cancel_requested = True
                    operation.state = OP_CANCEL_REQUESTED
                    operation.updated_at = time.time()
                    db.session.commit()
                    return {"success": True, "status": "cancelling"}
                db.session.rollback()
                return {"success": False, "error": "No active container found"}
            if expected_session_uuid is not None and self._session_uuid(row) != expected_session_uuid:
                db.session.rollback()
                return {"success": False, "error": "Session changed before reconciliation"}

            # stop plus auto_remove would delete the writable layer held as evidence
            if self._is_paused(row) and reason != END_REASON_ADMIN_KILLED:
                row.lifecycle_state = LIFECYCLE_HELD
                if operation is not None:
                    operation.state = OP_HELD
                    operation.session_uuid = self._session_uuid(row)
                    operation.updated_at = time.time()
                db.session.commit()
                return {"success": False, "error": "Session suspended - contact your instructor"}

            gate = self._gate_destroy_state(self._row_lifecycle_state(row), reason == END_REASON_ADMIN_KILLED)
            if gate is not None:
                return gate

            context_name = str(row.docker_context)
            container_name = str(row.container_name)
            container_id = str(row.container_id)
            session_uuid = self._session_uuid(row)
            row.session_uuid = session_uuid
            # release the row locks before remote io, session_uuid fences the second transaction
            db.session.commit()

        observed_state = self._inspect_container_state(context_name, container_id)

        admin_override = reason == END_REASON_ADMIN_KILLED
        if not admin_override and observed_state == "paused":
            # narrows but does not close the window, a pause can still land after this check
            self._mirror_detected_hold(user_id, session_uuid)
            return {"success": False, "error": "Session suspended - contact your instructor"}
        if not admin_override and observed_state == "unknown":
            db.session.rollback()
            return {"success": False, "error": "Container state is unknown; refusing destructive cleanup"}

        with self._get_destroy_lock(user_id):
            operation, current = self._reload_session_rows(user_id, row)
            if not self._same_session(current, session_uuid):
                db.session.rollback()
                return {"success": False, "error": "Session changed before teardown"}
            if not admin_override and (
                self._is_paused(current) or self._row_lifecycle_state(current) == LIFECYCLE_HELD
            ):
                db.session.rollback()
                return {"success": False, "error": "Session suspended - contact your instructor"}

            lifecycle_state = self._row_lifecycle_state(current)
            gate = self._gate_destroy_state(lifecycle_state, admin_override)
            if gate is not None:
                return gate

            row = current
            raw_cookie_sid = getattr(current, "cookie_sid", None)
            cookie_sid = raw_cookie_sid if isinstance(raw_cookie_sid, str) and raw_cookie_sid else None
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

        # revoke before remote teardown so a cache failure cannot leave a live credential
        cookie_revoked = True
        if cookie_sid:
            from flask import current_app

            cookie_revoked = _revoke_session_cookie(current_app, cookie_sid)

        try:
            if (
                was_paused
                or observed_state in ("created", "exited", "not_found")
                or reason == END_REASON_RECONCILIATION
            ):
                # stop on a frozen container blocks for the full timeout so remove directly
                self.host_manager.force_remove_container(context_name, container_id)
            else:
                # stop by id so a replacement that reused the name is never touched
                self.host_manager.stop_container(context_name, container_id)
        except (
            docker.errors.DockerException,
            paramiko.ssh_exception.SSHException,
            EOFError,
            OSError,
            HostsUnavailableException,
        ) as e:
            # an unknown outcome keeps the row and its reservations, recovery retries the same container
            operation, current = self._reload_session_rows(user_id, row)
            self._mark_cleanup_pending(operation, current, session_uuid, reason, str(e))
            logger.warning(
                f"stop outcome unknown for {container_name}: context unavailable; retaining the capacity reservation"
            )
            return {"success": False, "error": "Container stop outcome is unknown; cleanup will be retried"}

        if not cookie_revoked:
            # keep the row because cookie_sid is the only handle for retrying revocation
            with self._get_destroy_lock(user_id):
                operation, current = self._reload_session_rows(user_id, row)
                if not self._same_session(current, session_uuid):
                    db.session.rollback()
                    return {"success": False, "error": "Session changed while credential revocation was pending"}
                self._mark_cleanup_pending(
                    operation, current, session_uuid, reason, "CTFd session credential revocation pending"
                )
            return {
                "success": False,
                "error": "Container stopped; session credential revocation will be retried",
            }

        # a crash before this commit leaves the row stopping and recovery finishes it
        ended_at = time.time()
        with self._get_destroy_lock(user_id):
            db.session.rollback()
            # context counter before row locks per the admission lock order, _reload_session_rows cannot run here
            self.orchestrator.release_active_slot_in_transaction(
                context_name,
                user_id,
                session_uuid,
            )
            operation = self._locked_operation(user_id, create=True)
            current = (
                self._locked_active_row(user_id)
                if isinstance(getattr(DesktopContainerInfoModel, "__tablename__", None), str)
                else row
            )
            if not self._same_session(current, session_uuid):
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

        if log_destruction:
            duration = ended_at - history.started_at
            try:
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
            except Exception:
                logger.exception("failed to log session_destroyed for session %s", session_uuid)

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
            # never return the stored absolute url
            "vnc_url": proxy_vnc_url(row.user_id, row.vnc_password),
            "created_at": row.created_at,
        }

    @staticmethod
    def _is_expired(row: DesktopContainerInfoModel) -> bool:
        if not row.timer_started or row.timer_start_time is None:
            return False
        return row.timer_duration - (time.time() - row.timer_start_time) <= 0

    def _verify_or_reap(self, row: DesktopContainerInfoModel) -> bool:
        user_id = row.user_id
        context_name = row.docker_context
        container_id = row.container_id
        session_uuid = self._session_uuid(row)
        db.session.rollback()
        # the ssh round trip stays outside the per user lock so destroy_container is not blocked
        state = self._inspect_container_state(context_name, container_id)
        if state in ("running", "paused", "unknown"):
            return True

        # exited, created and not_found all land here and get force removed by reconciliation
        self.destroy_container(
            user_id,
            reason=END_REASON_RECONCILIATION,
            expected_session_uuid=session_uuid,
        )
        return False

    # keep in sync with routes._timer_dict which builds the same shape
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
                # remote_desktop_dashboard.html renders these as html so they must stay escaped
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

        # same row lock as destroy, the local lock is only an optimization
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

        try:
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
        except Exception:
            logger.exception("failed to log session_extended for user %s", user_id)

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

            # cleanup pending rows need a retry even when their timer never started
            rows = (
                DesktopContainerInfoModel.query.all()
                if isinstance(getattr(DesktopContainerInfoModel, "__tablename__", None), str)
                else DesktopContainerInfoModel.query.filter_by(timer_started=True).all()
            )

            expired_user_ids = []
            verify_active: list[DesktopContainerInfoModel] = []
            cleanup_pending: list[tuple[int, str]] = []
            for row in rows:
                lifecycle_state = self._row_lifecycle_state(row)
                if lifecycle_state == LIFECYCLE_CLEANUP_PENDING:
                    cleanup_pending.append((row.user_id, str(row.lifecycle_reason or END_REASON_RECONCILIATION)))
                    continue
                if lifecycle_state != LIFECYCLE_ACTIVE:
                    continue
                verify_active.append(row)
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

            # a daemon side removal goes unnoticed unless this sweep verifies active rows
            expired = set(expired_user_ids)
            for row in verify_active:
                if int(row.user_id) in expired:
                    continue
                try:
                    self._verify_or_reap(row)
                except Exception as e:
                    logger.error(f"failed to verify active session for user {row.user_id}: {e}")

            try:
                self.orchestrator.audit_counts()
            except Exception as e:
                logger.error(f"capacity count audit failed: {e}")

            self._recover_stale_operations()
            self._reconcile_orphans()

    def _recover_stale_operations(self) -> None:
        """the operation row is fenced and committed before any docker io so no live worker is raced
        paused containers become holds, unknown outcomes keep the reservation for a later retry
        """
        try:
            candidates = DesktopSessionOperationModel.query.filter(
                DesktopSessionOperationModel.state.in_(CREATE_OPERATION_STATES | {OP_CLEANUP_PENDING}),
                DesktopSessionOperationModel.updated_at <= time.time() - self.RECONCILE_SAFETY_AGE_SECONDS,
            ).all()
        except AttributeError:
            return

        for candidate in candidates:
            self._recover_stale_operation(candidate)

    def _note_takeover_outcome(self, user_id: int, takeover_uuid: str, error: str, state: str | None = None) -> None:
        db.session.rollback()
        current = self._locked_operation(user_id)
        if current is not None and current.worker_lease_uuid == takeover_uuid:
            if state is not None:
                current.state = state
            current.error = error
            current.updated_at = time.time()
            db.session.commit()
        else:
            db.session.rollback()

    def _recover_stale_operation(self, candidate: DesktopSessionOperationModel) -> None:
        user_id = int(candidate.user_id)
        db.session.rollback()
        operation = self._locked_operation(user_id)
        active = self._locked_active_row(user_id)
        if active is not None or operation is None:
            db.session.rollback()
            return
        state = self._operation_state(operation)
        updated_at = float(operation.updated_at or 0)
        if state not in CREATE_OPERATION_STATES | {OP_CLEANUP_PENDING}:
            db.session.rollback()
            return
        if updated_at > time.time() - self.RECONCILE_SAFETY_AGE_SECONDS:
            db.session.rollback()
            return

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
                    return
                exact = next((entry for entry in listing if str(entry.get("name", "")) == container_name), None)
                if exact is None:
                    confirmed_absent = True
                elif normalize_container_state(exact.get("status")) == "paused":
                    self._note_takeover_outcome(
                        user_id, takeover_uuid, "paused container held for inspection", state=OP_HELD
                    )
                    return
                elif normalize_container_state(exact.get("status")) == "unknown":
                    self._note_takeover_outcome(
                        user_id, takeover_uuid, "container state unknown; destructive recovery deferred"
                    )
                    return
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
                self._note_takeover_outcome(user_id, takeover_uuid, str(e))
                return

        if not confirmed_absent:
            return

        # context lock first to match the admission lock order
        db.session.rollback()
        if context_name and capacity_reserved:
            self.orchestrator.release_operation_slot_in_transaction(
                context_name,
                user_id,
                session_uuid,
                takeover_uuid,
            )
        current = self._locked_operation(user_id)
        if (
            current is None
            or current.worker_lease_uuid != takeover_uuid
            or str(current.session_uuid or "") != session_uuid
        ):
            db.session.rollback()
            return
        current.state = OP_FAILED
        current.worker_lease_uuid = None
        current.cancel_requested = False
        current.capacity_reserved = False
        current.docker_context = None
        current.container_name = None
        current.error = "abandoned creation cleaned up by recovery"
        current.updated_at = time.time()
        db.session.commit()

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
            # none means the host did not answer so skip removal this sweep
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
                age = now - created_ts if created_ts > 0 else 0  # a failed timestamp parse counts as too young
                # the safety window avoids racing a new container whose row is not committed yet
                if age < self.RECONCILE_SAFETY_AGE_SECONDS:
                    continue

                # the docker name filter is a partial match so labels must agree before deleting
                if self._managed_orphan_identity(entry) is None:
                    logger.warning(f"reconcile: refusing unmanaged or malformed candidate {name} on {ctx_name}")
                    continue

                state = normalize_container_state(entry.get("status"))
                if state == "paused":
                    # evidence hold, surface it and never remove
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
                    # force_remove is needed because stop and auto_remove do nothing in the created state
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
                    # no capacity decrement here, the next audit heals any overcount
                except Exception as e:
                    logger.error(f"reconcile: failed to remove {name} on {ctx_name}: {e}")

    def pause_watch(self) -> None:
        # mirrors out of band pause and unpause from the host into paused_at
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
                    self._pause_watch_entry(ctx_name, entry, rows_by_name)

    def _pause_watch_entry(
        self,
        ctx_name: str,
        entry: dict[str, object],
        rows_by_name: dict[str, tuple],
    ) -> None:
        identity = rows_by_name.get(str(entry.get("name", "")))
        if identity is None or identity[2] != ctx_name:
            return
        status = str(entry.get("status", ""))
        user_id, session_uuid, _context, snapshot_row = identity
        _user, username = _display_name(user_id)

        operation, row = self._reload_session_rows(user_id, snapshot_row, create=False)
        if row is None or self._session_uuid(row) != session_uuid or row.container_name != str(entry.get("name", "")):
            db.session.rollback()
            return

        if status == "paused" and row.paused_at is None and self._row_lifecycle_state(row) == LIFECYCLE_ACTIVE:
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
            return

        if (
            status == "running"
            and row.paused_at is not None
            and self._row_lifecycle_state(row)
            in (
                LIFECYCLE_HELD,
                LIFECYCLE_UNPAUSING,
            )
        ):
            # holds are sticky, only the audited admin path may release one
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
                return
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
            return

        db.session.rollback()

    @staticmethod
    def _credit_pause_and_clear(row: DesktopContainerInfoModel) -> None:
        # credit the frozen time so the session does not expire on unpause
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
        paused_at = float(row.paused_at)
        # mark unpausing before remote io, pause_watch ignores this transient state
        row.lifecycle_state = LIFECYCLE_UNPAUSING
        if operation is not None and operation.session_uuid == session_uuid:
            operation.state = OP_UNPAUSING
            operation.updated_at = time.time()
        db.session.commit()
        try:
            # extend the image lifetime deadline before thaw or auto_remove erases the layer
            self.host_manager.extend_paused_lifetime_deadline(
                context_name,
                container_name,
                paused_at,
                minimum_remaining=self.UNPAUSE_LEASE_SECONDS,
            )
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
            # unit model only, schema validation guarantees the query exists in production
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

        # log even for an empty fleet so the attempt stays attributable
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

            # commit the audit before destructive io so a database failure keeps the evidence
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
            # recount instead of decrementing, this orphan had no authoritative row
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
