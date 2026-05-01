from __future__ import annotations

import re
import json
import time
import logging
import secrets
import traceback
from typing import Callable
from threading import Lock

import docker
import paramiko
from flask import Flask
from CTFd.models import db, Users
from .models import (
    DesktopContainerInfoModel,
    DesktopSessionHistoryModel,
    CommandLogModel,
    SettingValue,
    user_flags,
    _esc,
)
from .event_logger import event_logger
from .docker_host_manager import DockerHostManager, parse_size
from .orchestrator import Orchestrator

logger = logging.getLogger(__name__)


def _display_name(user_id: int) -> tuple[Users | None, str]:  # type: ignore[type-arg]
    """fetch user from DB and return (user_obj, display_name) tuple"""
    user = Users.query.filter_by(id=user_id).first()
    return user, user.name if user else f"User {user_id}"


def _mint_session_cookie(app: Flask, user: Users) -> tuple[str, str] | None:  # type: ignore[type-arg]
    # CTFd uses server-side sessions, save_session writes to the cache
    # backend so just signing the sid wouldn't populate it
    from flask import session
    from werkzeug.wrappers import Response
    from CTFd.utils.security.auth import login_user

    cookie_name = app.session_cookie_name
    with app.test_request_context():
        login_user(user)
        resp = Response()
        app.session_interface.save_session(app, session, resp)
        for header in resp.headers.getlist("Set-Cookie"):
            if header.startswith(f"{cookie_name}="):
                value = header.split(f"{cookie_name}=", 1)[1].split(";", 1)[0]
                return cookie_name, value
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


def _sanitize_username(raw: str, user_id: int | None = None) -> str:
    name = _USERNAME_RE.sub("", raw.lower())
    # linux usernames must start with a letter or underscore
    name = name.lstrip("0123456789-")[:32]
    if not name or name in _RESERVED_NAMES:
        return f"user{user_id}" if user_id else "user"
    return name


class ContainerManager:
    def __init__(self, host_manager: DockerHostManager, orchestrator: Orchestrator, app: Flask | None = None) -> None:
        self.host_manager = host_manager
        self.orchestrator = orchestrator
        self.app = app
        self.creation_status: dict[int, CreationStatusDict] = {}
        self.lock = Lock()
        self._log_offsets: dict[str, int] = {}

    def _get_setting(self, key: str) -> SettingValue:
        from .models import get_setting

        return get_setting(key)

    def _resolve_username(self, user: Users) -> str:  # type: ignore[type-arg]
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
    ) -> None:
        with app.app_context():
            self._create_container_background(user_id, container_url, extra_hosts)

    def _create_container_background(
        self,
        user_id: int,
        container_url: str,
        extra_hosts: dict[str, str] | None,
    ) -> None:
        logger.info(f"[BACKGROUND] creating container for user {user_id}")

        user, username = _display_name(user_id)
        container_username = self._resolve_username(user) if user else f"user{user_id}"

        context_name: str | None = None
        container_name: str | None = None

        try:
            with self.lock:
                self.creation_status[user_id] = {"status": "selecting_host", "message": "Requesting a server..."}

            context_name = self.orchestrator.select_and_reserve()
            pub_hostname = self.host_manager.get_pub_hostname(context_name)
            check_hostname = self.host_manager.get_check_hostname(context_name)
            # escaped for safe embedding in creation status messages rendered via innerHTML
            display_hostname = _esc(context_name)

            logger.info(f"selected context: {context_name} (public: {pub_hostname}) for user {user_id}")

            self.host_manager.acquire_semaphore(context_name)

            container_name = f"rd-session-{user_id}-{int(time.time())}"

            try:
                with self.lock:
                    self.creation_status[user_id] = {
                        "status": "starting_container",
                        "message": f"Starting container on {display_hostname}...",
                    }
                vnc_password = secrets.token_urlsafe(6)[:8]

                docker_image = str(self._get_setting("docker_image"))
                resolution = str(self._get_setting("resolution"))
                shm_size = parse_size(self._get_setting("shm_size"))  # type: ignore[arg-type]
                memory_limit = parse_size(self._get_setting("memory_limit"))  # type: ignore[arg-type]
                cpu_limit = self._get_setting("cpu_limit")
                nano_cpus = int(float(cpu_limit) * 1e9)  # type: ignore[arg-type]

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
                }

                if self._get_setting("command_logging_enabled"):
                    container_env["SHELL_LOGGING"] = "1"

                from flask import current_app

                if user is not None:
                    minted = _mint_session_cookie(current_app._get_current_object(), user)  # type: ignore[attr-defined]
                    if minted:
                        cookie_name, cookie_value = minted
                        container_env["CTFD_COOKIE_NAME"] = cookie_name
                        container_env["CTFD_COOKIE_VALUE"] = cookie_value
                    else:
                        logger.warning(f"failed to mint session cookie for user {user_id}, autologin disabled")

                result = self.host_manager.run_container(
                    context_name=context_name,
                    image=docker_image,
                    name=container_name,
                    hostname=display_hostname,
                    env=container_env,
                    ports=["22/tcp", "5900/tcp", "6080/tcp", "7682/tcp"],
                    shm_size=shm_size,
                    memory=memory_limit,
                    nano_cpus=nano_cpus,
                    extra_hosts=extra_hosts,
                )
            finally:
                self.host_manager.release_semaphore(context_name)

            port_map: dict[str, int] = result["ports"]  # type: ignore[assignment]
            container_id = str(result["container_id"])
            ssh_port = port_map.get("22/tcp")
            vnc_port = port_map["5900/tcp"]
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

            def _vnc_progress(attempt: int, max_attempts: int) -> None:
                with self.lock:
                    self.creation_status[user_id] = {
                        "status": "waiting_vnc",
                        "message": f"Waiting for {display_hostname} display server... ({attempt}/{max_attempts})",
                    }

            vnc_ready = self.wait_for_vnc_ready(check_hostname, novnc_port, progress_callback=_vnc_progress)  # type: ignore[arg-type]

            if not vnc_ready:
                raise Exception(f"VNC server on {check_hostname}:{novnc_port} did not become ready in time")

            vnc_url = f"/remote-desktop/vnc/{user_id}/vnc.html?autoconnect=true&resize=remote&reconnect=true#password={vnc_password}"

            # check if destroy was called while we were setting up
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
            )
            try:
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
            if container_name and context_name:
                try:
                    self.host_manager.stop_container(context_name, container_name)
                    logger.info(f"cleaned up container {container_name} after creation failure")
                except Exception as stop_error:
                    logger.error(f"failed to stop container during cleanup: {stop_error}")

            if context_name:
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

            with self.lock:
                self.creation_status[user_id] = {
                    "status": "failed",
                    "error": _esc(str(e)),
                    "hostname": _esc(context_name or ""),
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
    ) -> ResultDict:
        from flask import current_app

        user, username = _display_name(user_id)

        logger.info(f"create_container called for user {user_id} ({username})")

        with self.lock:
            existing = self.creation_status.get(user_id)
            if existing and existing.get("status") not in (None, "failed", "ready"):
                return {"success": False, "error": "Creation already in progress"}

        existing_row = DesktopContainerInfoModel.query.filter_by(user_id=user_id).first()
        if existing_row:
            return {"success": False, "error": "Session already exists"}

        if not self.orchestrator.has_healthy_context():
            return {"success": False, "error": "no healthy contexts available"}

        with self.lock:
            self.creation_status[user_id] = {"status": "queued", "message": "Queued..."}

        host_status = self.orchestrator.get_status()
        event_logger.log_event(
            "session_requested",
            "requested remote desktop session",
            user_id=user_id,
            username=username,
            level="info",
            metadata={
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

        app: Flask = current_app._get_current_object()  # type: ignore[assignment]

        try:
            import gevent

            gevent.spawn(self._create_container_background_wrapper, app, user_id, container_url, extra_hosts)
        except Exception as e:
            logger.error(f"failed to submit background task: {e}")
            logger.error(traceback.format_exc())
            with self.lock:
                self.creation_status[user_id] = {
                    "status": "failed",
                    "error": f"Failed to start background task: {str(e)}",
                }
            return {"success": False, "error": str(e)}

        return {"success": True, "status": "creating"}

    def get_creation_status(self, user_id: int) -> CreationStatusDict | None:
        with self.lock:
            return self.creation_status.get(user_id)

    def destroy_container(
        self, user_id: int, reason: str = "user_destroyed", log_destruction: bool = True
    ) -> ResultDict:
        _user, username = _display_name(user_id)

        row = DesktopContainerInfoModel.query.filter_by(user_id=user_id).first()

        with self.lock:
            status = self.creation_status.get(user_id)
            if status and status.get("status") not in (None, "failed", "ready"):
                # creation is in-flight, signal the background greenlet to abort
                self.creation_status[user_id] = {"status": "cancelled"}
            else:
                self.creation_status.pop(user_id, None)

        if row is None:
            return {"success": False, "error": "No active container found"}

        context_name = row.docker_context
        container_name = row.container_name

        try:
            self._collect_logs_for_container(row)
        except Exception as e:
            logger.debug(f"failed to collect final logs for {container_name}: {e}")

        self._log_offsets.pop(row.container_id, None)

        ended_at = time.time()
        history = DesktopSessionHistoryModel(
            user_id=row.user_id,
            username=username,
            docker_context=context_name,
            started_at=row.created_at,
            ended_at=ended_at,
            duration=ended_at - row.created_at,
            end_reason=reason,
            extensions_used=row.extensions_used,
        )
        db.session.add(history)

        db.session.delete(row)
        db.session.commit()

        self.host_manager.stop_container(context_name, container_name)
        self.orchestrator.release_slot(context_name)

        if log_destruction:
            duration = ended_at - history.started_at
            cmd_count = (
                CommandLogModel.query.filter_by(user_id=user_id).count()
                if self._get_setting("command_logging_enabled")
                else None
            )
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
                    "commands": cmd_count,
                },
            )

        return {"success": True}

    def get_container_info(self, user_id: int) -> ContainerInfoDict | None:
        row = DesktopContainerInfoModel.query.filter_by(user_id=user_id).first()
        if not row:
            return None

        if self._is_expired(row):
            self.destroy_container(user_id, reason="expired")
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
            "vnc_url": row.vnc_url,
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
        try:
            running = self.host_manager.is_container_running(row.docker_context, row.container_id)
        except (docker.errors.DockerException, paramiko.ssh_exception.SSHException, EOFError, OSError):
            return True
        if running:
            return True

        ended_at = time.time()
        user = Users.query.filter_by(id=row.user_id).first()
        db.session.add(
            DesktopSessionHistoryModel(
                user_id=row.user_id,
                username=user.name if user else f"User {row.user_id}",
                docker_context=row.docker_context,
                started_at=row.created_at,
                ended_at=ended_at,
                duration=ended_at - row.created_at,
                end_reason="reconciliation",
                extensions_used=row.extensions_used,
            )
        )
        self.orchestrator.release_slot(row.docker_context)
        db.session.delete(row)
        db.session.commit()
        return False

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

        expired = [row for row in rows if self._is_expired(row)]
        for row in expired:
            try:
                self.destroy_container(row.user_id, reason="expired")
            except Exception as e:
                logger.error(f"inline expiry cleanup failed for user {row.user_id}: {e}")

        if expired:
            rows = DesktopContainerInfoModel.query.all()
            if not rows:
                return []

        user_ids = [row.user_id for row in rows]
        users_by_id = {u.id: u for u in Users.query.filter(Users.id.in_(user_ids)).all()}

        containers: list[ContainerListEntry] = []
        for row in rows:
            user = users_by_id.get(row.user_id)
            container_data = {
                "user_id": row.user_id,
                "username": _esc(user.name) if user else "Unknown",
                **user_flags(user),
                "container_name": _esc(row.container_name),
                "container_id": row.container_id,
                "docker_context": _esc(row.docker_context),
                "created_at": row.created_at,
                "vnc_port": row.vnc_port,
                "novnc_port": row.novnc_port,
                "vnc_password": row.vnc_password,
                "vnc_url": row.vnc_url,
                "timer": self._timer_from_row(row),
            }
            containers.append(container_data)

        return containers

    def extend_session_timer(self, user_id: int, new_duration: int | None = None) -> ResultDict:
        _user, username = _display_name(user_id)

        if new_duration is None:
            new_duration = int(self._get_setting("extension_duration"))  # type: ignore[arg-type]

        row = DesktopContainerInfoModel.query.filter_by(user_id=user_id).first()
        if not row:
            return {"success": False, "error": "No active session"}

        if not row.timer_started:
            return {"success": False, "error": "Timer not started"}

        if row.extensions_used >= row.max_extensions:
            return {"success": False, "error": "Maximum extensions reached"}

        elapsed = time.time() - row.timer_start_time
        remaining = max(0, row.timer_duration - elapsed)
        row.timer_start_time = time.time()
        row.timer_duration = remaining + new_duration
        row.extensions_used += 1
        db.session.commit()

        logger.info(f"extended timer for user {user_id}: {row.extensions_used}/{row.max_extensions}")

        event_logger.log_event(
            "session_extended",
            f"session extended ({row.extensions_used}/{row.max_extensions} extensions used)",
            user_id=user_id,
            username=username,
            level="info",
            metadata={
                "extensions_used": row.extensions_used,
                "max_extensions": row.max_extensions,
                "new_duration": new_duration,
            },
        )

        return {"success": True, "extensions_used": row.extensions_used, "max_extensions": row.max_extensions}

    def get_session_timer_status(self, user_id: int) -> TimerStatusDict:
        row = DesktopContainerInfoModel.query.filter_by(user_id=user_id).first()
        if not row:
            return {"success": False, "error": "No active session"}

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

            rows = DesktopContainerInfoModel.query.filter_by(timer_started=True).all()

            expired_user_ids = []
            for row in rows:
                if row.timer_start_time is None:
                    continue
                elapsed = time.time() - row.timer_start_time
                if row.timer_duration - elapsed <= 0:
                    expired_user_ids.append(row.user_id)

            for user_id in expired_user_ids:
                logger.info(f"auto-destroying expired session for user {user_id}")
                try:
                    self.destroy_container(user_id, reason="expired")
                except Exception as e:
                    logger.error(f"failed to destroy expired session for user {user_id}: {e}")

    def destroy_all_containers_admin(self, admin_user: Users) -> int:  # type: ignore[type-arg]
        rows = DesktopContainerInfoModel.query.all()
        killed = 0

        for row in rows:
            try:
                self.destroy_container(row.user_id, reason="admin_killed", log_destruction=False)
                killed += 1
            except Exception as e:
                logger.error(f"failed to kill session for user {row.user_id}: {e}")

        if killed:
            event_logger.log_event(
                "admin_action",
                f"admin {admin_user.name} killed all sessions ({killed} total)",
                user_id=admin_user.id,
                username=admin_user.name,
                level="warning",
                metadata={"killed_count": killed},
            )

        return killed

    def cleanup_all_containers(self) -> None:
        logger.info("cleaning up all containers on shutdown")

        rows = DesktopContainerInfoModel.query.all()

        for row in rows:
            try:
                self.host_manager.stop_container(row.docker_context, row.container_name)
                logger.info(f"cleaned up {row.container_name}")
            except Exception as e:
                logger.error(f"failed to cleanup container for user {row.user_id}: {e}")

        logger.info("cleanup completed")

    def _get_log_offset(self, container_id: str) -> int:
        # on restart, derive from DB count to avoid re-ingesting existing lines
        offset = self._log_offsets.get(container_id)
        if offset is not None:
            return offset

        offset = CommandLogModel.query.filter_by(container_id=container_id).count()
        self._log_offsets[container_id] = offset
        return offset

    def _collect_logs_for_container(self, row: DesktopContainerInfoModel) -> None:
        if not self._get_setting("command_logging_enabled"):
            return

        offset = self._get_log_offset(row.container_id)
        cmd = ["sh", "-c", f"tail -n +{offset + 1} /var/log/.session-init/data.jsonl 2>/dev/null"]

        exit_code, output = self.host_manager.exec_in_container(row.docker_context, row.container_name, cmd)

        if exit_code != 0 or not output.strip():
            return

        lines = output.strip().split("\n")
        new_entries = []
        parsed = 0

        for line in lines:
            try:
                data = json.loads(line)
                new_entries.append(
                    CommandLogModel(
                        user_id=row.user_id,
                        container_id=row.container_id,
                        timestamp=data.get("ts", 0),
                        command=data.get("cmd", ""),
                        exit_code=data.get("exit"),
                        duration=data.get("dur"),
                        cwd=data.get("cwd"),
                        tty=data.get("tty"),
                    )
                )
                parsed += 1
            except (json.JSONDecodeError, KeyError):
                continue

        if new_entries:
            try:
                db.session.bulk_save_objects(new_entries)
                db.session.commit()
            except Exception:
                db.session.rollback()
                return

        # only advance offset by lines we actually parsed and committed,
        # so truncated/partial lines get retried next cycle
        self._log_offsets[row.container_id] = offset + parsed

    def collect_all_command_logs(self) -> None:
        with self.app.app_context():  # type: ignore[union-attr]
            if not self._get_setting("command_logging_enabled"):
                return

            rows = DesktopContainerInfoModel.query.all()
            for row in rows:
                try:
                    self._collect_logs_for_container(row)
                except Exception as e:
                    logger.debug(f"log collection failed for {row.container_name}: {e}")
