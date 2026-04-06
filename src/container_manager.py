import re
import time
import logging
import secrets
import traceback
from threading import Lock

from CTFd.models import db, Users
from .models import DesktopContainerInfoModel, DesktopSessionHistoryModel
from .event_logger import event_logger
from .docker_host_manager import parse_size

logger = logging.getLogger(__name__)

_ALNUM_RE = re.compile(r"[^a-z0-9]")


def _sanitize_username(raw):
    return _ALNUM_RE.sub("", raw.lower())[:32]


class ContainerManager:
    def __init__(self, host_manager, orchestrator):
        self.host_manager = host_manager
        self.orchestrator = orchestrator
        self.creation_status = {}
        self.lock = Lock()

    def _get_setting(self, key):
        from .models import get_setting

        return get_setting(key)

    def _resolve_username(self, user):
        source = self._get_setting("username_source")

        if source == "email" and user.email:
            username = _sanitize_username(user.email.split("@")[0])
        else:
            username = _sanitize_username(user.name)

        return username or f"user{user.id}"

    def wait_for_vnc_ready(self, hostname, novnc_port, max_attempts=None, progress_callback=None):
        if max_attempts is None:
            max_attempts = self._get_setting("vnc_ready_attempts")
        http_timeout = self._get_setting("http_request_timeout")

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

    def _create_container_background_wrapper(self, app, user_id):
        with app.app_context():
            self._create_container_background(user_id)

    def _create_container_background(self, user_id):
        logger.info(f"[BACKGROUND] creating container for user {user_id}")

        try:
            user = Users.query.filter_by(id=user_id).first()
            username = user.name if user else f"User {user_id}"
            container_username = self._resolve_username(user) if user else f"user{user_id}"
        except Exception as e:
            logger.error(f"[BACKGROUND] failed to get user {user_id}: {e}")
            username = f"User {user_id}"
            container_username = f"user{user_id}"

        context_name = None

        try:
            with self.lock:
                self.creation_status[user_id] = {"status": "selecting_host", "message": "Requesting a server..."}

            context_name = self.orchestrator.select_and_reserve()
            pub_hostname = self.host_manager.get_pub_hostname(context_name)
            display_hostname = context_name

            logger.info(f"selected context: {context_name} (public: {pub_hostname}) for user {user_id}")

            self.host_manager.acquire_semaphore(context_name)

            try:
                with self.lock:
                    self.creation_status[user_id] = {
                        "status": "starting_container",
                        "message": f"Starting container on {display_hostname}...",
                    }

                container_name = f"rd-session-{user_id}-{int(time.time())}"
                vnc_password = secrets.token_urlsafe(6)[:8]

                docker_image = self._get_setting("docker_image")
                resolution = self._get_setting("resolution")
                shm_size = parse_size(self._get_setting("shm_size"))
                memory_limit = parse_size(self._get_setting("memory_limit"))
                cpu_limit = self._get_setting("cpu_limit")
                nano_cpus = int(float(cpu_limit) * 1e9)

                result = self.host_manager.run_container(
                    context_name=context_name,
                    image=docker_image,
                    name=container_name,
                    env={
                        "VNC_PASSWORD": vnc_password,
                        "RESOLUTION": resolution,
                        "CTFD_USERNAME": container_username,
                    },
                    ports=["5900/tcp", "6080/tcp"],
                    shm_size=shm_size,
                    memory=memory_limit,
                    nano_cpus=nano_cpus,
                )
            finally:
                self.host_manager.release_semaphore(context_name)

            container_id = result["container_id"]
            vnc_port = result["ports"]["5900/tcp"]
            novnc_port = result["ports"]["6080/tcp"]

            logger.info(f"container {container_name} created - VNC:{vnc_port} noVNC:{novnc_port}")

            with self.lock:
                self.creation_status[user_id] = {
                    "status": "waiting_vnc",
                    "message": f"Waiting for {display_hostname} display server...",
                }

            def _vnc_progress(attempt, max_attempts):
                with self.lock:
                    self.creation_status[user_id] = {
                        "status": "waiting_vnc",
                        "message": f"Waiting for {display_hostname} display server... ({attempt}/{max_attempts})",
                    }

            vnc_ready = self.wait_for_vnc_ready(pub_hostname, novnc_port, progress_callback=_vnc_progress)

            if not vnc_ready:
                raise Exception(f"VNC server on {pub_hostname}:{novnc_port} did not become ready in time")

            vnc_url = f"http://{pub_hostname}:{novnc_port}/vnc.html?autoconnect=true&password={vnc_password}&resize=remote&reconnect=true"

            max_extensions = self._get_setting("max_extensions")

            row = DesktopContainerInfoModel(
                container_id=container_id,
                user_id=user_id,
                container_name=container_name,
                vnc_port=vnc_port,
                novnc_port=novnc_port,
                vnc_password=vnc_password,
                vnc_url=vnc_url,
                docker_context=context_name,
                pub_hostname=pub_hostname,
                created_at=time.time(),
                timer_started=False,
                timer_start_time=None,
                timer_duration=0,
                extensions_used=0,
                max_extensions=max_extensions,
            )
            db.session.add(row)
            db.session.commit()

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
                    "vnc_port": vnc_port,
                    "novnc_port": novnc_port,
                },
            )

        except Exception as e:
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
                    "error": str(e),
                    "hostname": context_name,
                }

            event_logger.log_event(
                "session_error",
                f"failed to create session: {str(e)}",
                user_id=user_id,
                username=username,
                level="error",
                metadata={"error": str(e), "traceback": traceback.format_exc()},
            )

    def create_container(self, user_id):
        from flask import current_app

        user = Users.query.filter_by(id=user_id).first()
        username = user.name if user else f"User {user_id}"

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

        event_logger.log_event(
            "session_requested", "requested remote desktop session", user_id=user_id, username=username, level="info"
        )

        app = current_app._get_current_object()

        try:
            import gevent

            gevent.spawn(self._create_container_background_wrapper, app, user_id)
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

    def get_creation_status(self, user_id):
        with self.lock:
            return self.creation_status.get(user_id)

    def destroy_container(self, user_id, reason="user_destroyed", log_destruction=True):
        user = Users.query.filter_by(id=user_id).first()
        username = user.name if user else f"User {user_id}"

        row = DesktopContainerInfoModel.query.filter_by(user_id=user_id).first()

        with self.lock:
            self.creation_status.pop(user_id, None)

        if row is None:
            return {"success": False, "error": "No active container found"}

        context_name = row.docker_context
        container_name = row.container_name

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
            event_logger.log_event(
                "session_destroyed",
                "remote desktop session destroyed",
                user_id=user_id,
                username=username,
                level="info",
                metadata={"context": context_name, "container_name": container_name},
            )

        return {"success": True}

    def get_container_info(self, user_id):
        row = DesktopContainerInfoModel.query.filter_by(user_id=user_id).first()
        if not row:
            return None

        return {
            "container_id": row.container_id,
            "container_name": row.container_name,
            "vnc_port": row.vnc_port,
            "novnc_port": row.novnc_port,
            "docker_context": row.docker_context,
            "pub_hostname": row.pub_hostname,
            "vnc_password": row.vnc_password,
            "vnc_url": row.vnc_url,
            "created_at": row.created_at,
        }

    def get_all_containers(self):
        rows = DesktopContainerInfoModel.query.all()
        containers = []

        for row in rows:
            user = Users.query.filter_by(id=row.user_id).first()
            timer_status = self.get_session_timer_status(row.user_id)

            container_data = {
                "user_id": row.user_id,
                "username": user.name if user else "Unknown",
                "container_name": row.container_name,
                "container_id": row.container_id,
                "docker_context": row.docker_context,
                "created_at": row.created_at,
                "vnc_port": row.vnc_port,
                "novnc_port": row.novnc_port,
                "vnc_url": row.vnc_url,
                "timer": {
                    "active": timer_status.get("started", False),
                    "time_remaining": timer_status.get("time_remaining", 0),
                    "extensions_used": timer_status.get("extensions_used", 0),
                    "max_extensions": timer_status.get("max_extensions", 3),
                }
                if timer_status.get("success")
                else None,
            }
            containers.append(container_data)

        return containers

    def start_session_timer(self, user_id, duration=None):
        if duration is None:
            duration = self._get_setting("initial_duration")

        row = DesktopContainerInfoModel.query.filter_by(user_id=user_id).first()
        if not row:
            return {"success": False, "error": "No active session"}

        if row.timer_started:
            return {"success": False, "error": "Timer already started"}

        row.timer_started = True
        row.timer_start_time = time.time()
        row.timer_duration = duration
        row.extensions_used = 0
        db.session.commit()

        logger.info(f"started timer for user {user_id}: {duration}s")
        return {"success": True, "duration": duration}

    def stop_session_timer(self, user_id):
        row = DesktopContainerInfoModel.query.filter_by(user_id=user_id).first()
        if not row:
            return {"success": False, "error": "No active session"}

        if not row.timer_started:
            return {"success": False, "error": "Timer not started"}

        row.timer_started = False
        row.timer_start_time = None
        row.timer_duration = 0
        db.session.commit()

        logger.info(f"stopped timer for user {user_id}")
        return {"success": True}

    def extend_session_timer(self, user_id, new_duration=None):
        user = Users.query.filter_by(id=user_id).first()
        username = user.name if user else f"User {user_id}"

        if new_duration is None:
            new_duration = self._get_setting("extension_duration")

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

    def get_session_timer_status(self, user_id):
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

    def periodic_cleanup(self):
        rows = DesktopContainerInfoModel.query.filter_by(timer_started=True).all()

        expired_user_ids = []
        for row in rows:
            if row.timer_start_time is None:
                continue
            elapsed = time.time() - row.timer_start_time
            if row.timer_duration - elapsed <= 0:
                expired_user_ids.append(row.user_id)

        for user_id in expired_user_ids:
            user = Users.query.filter_by(id=user_id).first()
            username = user.name if user else f"User {user_id}"

            logger.info(f"auto-destroying expired session for user {user_id}")

            event_logger.log_event(
                "session_expired", "session expired", user_id=user_id, username=username, level="warning"
            )

            try:
                self.destroy_container(user_id, reason="expired", log_destruction=False)
            except Exception as e:
                logger.error(f"failed to destroy expired session for user {user_id}: {e}")

    def destroy_all_containers_admin(self, admin_user):
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

    def cleanup_all_containers(self):
        logger.info("cleaning up all containers on shutdown")

        rows = DesktopContainerInfoModel.query.all()

        for row in rows:
            try:
                self.host_manager.stop_container(row.docker_context, row.container_name)
                logger.info(f"cleaned up {row.container_name}")
            except Exception as e:
                logger.error(f"failed to cleanup container for user {row.user_id}: {e}")

        logger.info("cleanup completed")
