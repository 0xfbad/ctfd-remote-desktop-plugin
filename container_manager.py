import time
import logging
import secrets
import traceback
from threading import Lock
from .event_logger import event_logger
from .docker_host_manager import parse_size

logger = logging.getLogger(__name__)


class ContainerManager:
    def __init__(self, host_manager, orchestrator):
        self.host_manager = host_manager
        self.orchestrator = orchestrator
        self.active_containers = {}
        self.session_timers = {}
        self.creation_status = {}
        self.lock = Lock()

    def _get_setting(self, key, cast=None):
        from .models import get_setting

        val = get_setting(key)
        if cast and val is not None:
            return cast(val)
        return val

    def wait_for_vnc_ready(self, hostname, novnc_port, max_attempts=None):
        if max_attempts is None:
            max_attempts = self._get_setting("vnc_ready_attempts", cast=int)
        http_timeout = self._get_setting("http_request_timeout", cast=int)

        import urllib.request
        import urllib.error

        for attempt in range(max_attempts):
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

        from CTFd.models import Users

        try:
            user = Users.query.filter_by(id=user_id).first()
            username = user.name if user else f"User {user_id}"
        except Exception as e:
            logger.error(f"[BACKGROUND] failed to get user {user_id}: {e}")
            username = f"User {user_id}"

        context_name = None

        try:
            with self.lock:
                self.creation_status[user_id] = {"status": "selecting_host", "message": "Requesting a server..."}

            context_name = self.orchestrator.get_next_context()
            pub_hostname = self.host_manager.get_pub_hostname(context_name)
            display_hostname = context_name

            logger.info(f"selected context: {context_name} (public: {pub_hostname}) for user {user_id}")

            self.orchestrator.reserve_slot(context_name)

            with self.lock:
                self.creation_status[user_id] = {
                    "status": "starting_container",
                    "message": f"Starting container on {display_hostname}...",
                }

            container_name = f"kali-desktop-{user_id}-{int(time.time())}"
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
                    "CTFD_USERNAME": username,
                },
                ports=["5900/tcp", "6080/tcp"],
                shm_size=shm_size,
                memory=memory_limit,
                nano_cpus=nano_cpus,
            )

            container_id = result["container_id"]
            vnc_port = result["ports"]["5900/tcp"]
            novnc_port = result["ports"]["6080/tcp"]

            logger.info(f"container {container_name} created - VNC:{vnc_port} noVNC:{novnc_port}")

            with self.lock:
                self.creation_status[user_id] = {
                    "status": "waiting_vnc",
                    "message": f"Waiting for {display_hostname} display server...",
                }

            # vnc readiness check hits the novnc http endpoint on the pub_hostname
            vnc_ready = self.wait_for_vnc_ready(pub_hostname, novnc_port)

            if not vnc_ready:
                raise Exception(f"VNC server on {pub_hostname}:{novnc_port} did not become ready in time")

            vnc_url = f"http://{pub_hostname}:{novnc_port}/vnc.html?autoconnect=true&password={vnc_password}&resize=remote&reconnect=true"

            with self.lock:
                self.active_containers[user_id] = {
                    "container_id": container_id,
                    "container_name": container_name,
                    "vnc_port": vnc_port,
                    "novnc_port": novnc_port,
                    "docker_context": context_name,
                    "pub_hostname": pub_hostname,
                    "vnc_password": vnc_password,
                    "vnc_url": vnc_url,
                    "created_at": time.time(),
                }

                max_extensions = self._get_setting("max_extensions", cast=int)
                self.session_timers[user_id] = {
                    "started": False,
                    "start_time": None,
                    "duration": 0,
                    "extensions_used": 0,
                    "max_extensions": max_extensions,
                }

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
                    self.orchestrator.mark_unhealthy(context_name)
                except Exception as health_error:
                    logger.error(f"failed to mark context unhealthy during cleanup: {health_error}")

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
        from CTFd.models import Users
        from flask import current_app

        user = Users.query.filter_by(id=user_id).first()
        username = user.name if user else f"User {user_id}"

        logger.info(f"create_container called for user {user_id} ({username})")

        event_logger.log_event(
            "session_requested", "requested remote desktop session", user_id=user_id, username=username, level="info"
        )

        with self.lock:
            self.creation_status[user_id] = {"status": "queued", "message": "Queued..."}

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

    def destroy_container(self, user_id, log_destruction=True):
        from CTFd.models import Users

        user = Users.query.filter_by(id=user_id).first()
        username = user.name if user else f"User {user_id}"

        container_info = None
        with self.lock:
            if user_id not in self.active_containers:
                return {"success": False, "error": "No active container found"}
            container_info = self.active_containers[user_id].copy()

        context_name = container_info["docker_context"]
        container_name = container_info["container_name"]

        self.host_manager.stop_container(context_name, container_name)

        self.orchestrator.release_slot(context_name)

        with self.lock:
            if user_id in self.active_containers:
                del self.active_containers[user_id]
            if user_id in self.session_timers:
                del self.session_timers[user_id]
            if user_id in self.creation_status:
                del self.creation_status[user_id]

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
        with self.lock:
            info = self.active_containers.get(user_id)
            return info.copy() if info else None

    def get_all_containers(self):
        from CTFd.models import Users

        containers = []
        with self.lock:
            active_containers_copy = dict(self.active_containers)

        for user_id, container_info in active_containers_copy.items():
            user = Users.query.filter_by(id=user_id).first()
            timer_status = self.get_session_timer_status(user_id)
            vnc_url = container_info.get("vnc_url", "")

            container_data = {
                "user_id": user_id,
                "username": user.name if user else "Unknown",
                "container_name": container_info["container_name"],
                "container_id": container_info["container_id"],
                "docker_context": container_info["docker_context"],
                "created_at": container_info["created_at"],
                "vnc_port": container_info["vnc_port"],
                "novnc_port": container_info["novnc_port"],
                "vnc_url": vnc_url,
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
            duration = self._get_setting("initial_duration", cast=int)

        with self.lock:
            if user_id not in self.session_timers:
                return {"success": False, "error": "No active session"}

            timer = self.session_timers[user_id]
            if timer["started"]:
                return {"success": False, "error": "Timer already started"}

            timer["started"] = True
            timer["start_time"] = time.time()
            timer["duration"] = duration
            timer["extensions_used"] = 0

            logger.info(f"started timer for user {user_id}: {duration}s")
            return {"success": True, "duration": duration}

    def stop_session_timer(self, user_id):
        with self.lock:
            if user_id not in self.session_timers:
                return {"success": False, "error": "No active session"}

            timer = self.session_timers[user_id]
            if not timer["started"]:
                return {"success": False, "error": "Timer not started"}

            timer["started"] = False
            timer["start_time"] = None
            timer["duration"] = 0

            logger.info(f"stopped timer for user {user_id}")
            return {"success": True}

    def extend_session_timer(self, user_id, new_duration=None):
        from CTFd.models import Users

        user = Users.query.filter_by(id=user_id).first()
        username = user.name if user else f"User {user_id}"

        if new_duration is None:
            new_duration = self._get_setting("extension_duration", cast=int)

        with self.lock:
            if user_id not in self.session_timers:
                return {"success": False, "error": "No active session"}

            timer = self.session_timers[user_id]
            if not timer["started"]:
                return {"success": False, "error": "Timer not started"}

            max_extensions = timer["max_extensions"]
            if timer["extensions_used"] >= max_extensions:
                return {"success": False, "error": "Maximum extensions reached"}

            elapsed = time.time() - timer["start_time"]
            remaining = max(0, timer["duration"] - elapsed)
            timer["start_time"] = time.time()
            timer["duration"] = remaining + new_duration
            timer["extensions_used"] += 1

            logger.info(f"extended timer for user {user_id}: {timer['extensions_used']}/{max_extensions}")

            event_logger.log_event(
                "session_extended",
                f"session extended ({timer['extensions_used']}/{max_extensions} extensions used)",
                user_id=user_id,
                username=username,
                level="info",
                metadata={
                    "extensions_used": timer["extensions_used"],
                    "max_extensions": max_extensions,
                    "new_duration": new_duration,
                },
            )

            return {"success": True, "extensions_used": timer["extensions_used"], "max_extensions": max_extensions}

    def get_session_timer_status(self, user_id):
        with self.lock:
            if user_id not in self.session_timers:
                return {"success": False, "error": "No active session"}

            timer = self.session_timers[user_id]
            if not timer["started"]:
                return {"success": True, "started": False, "time_remaining": 0}

            elapsed = time.time() - timer["start_time"]
            time_remaining = max(0, timer["duration"] - elapsed)

            if time_remaining <= 0:
                return {"success": True, "started": False, "time_remaining": 0, "expired": True}

            return {
                "success": True,
                "started": True,
                "time_remaining": int(time_remaining),
                "extensions_used": timer["extensions_used"],
                "max_extensions": timer["max_extensions"],
            }

    def periodic_cleanup(self):
        from CTFd.models import Users

        with self.lock:
            user_ids = list(self.session_timers.keys())

        expired_users = []
        for user_id in user_ids:
            status = self.get_session_timer_status(user_id)
            if status.get("expired"):
                expired_users.append(user_id)

        for user_id in expired_users:
            user = Users.query.filter_by(id=user_id).first()
            username = user.name if user else f"User {user_id}"

            logger.info(f"auto-destroying expired session for user {user_id}")

            event_logger.log_event(
                "session_expired", "session expired", user_id=user_id, username=username, level="warning"
            )

            try:
                self.destroy_container(user_id, log_destruction=False)
            except Exception as e:
                logger.error(f"failed to destroy expired session for user {user_id}: {e}")

    def cleanup_all_containers(self):
        logger.info("cleaning up all containers on shutdown")

        with self.lock:
            containers_to_cleanup = dict(self.active_containers)

        for user_id, container_info in containers_to_cleanup.items():
            try:
                context_name = container_info["docker_context"]
                container_name = container_info["container_name"]
                self.host_manager.stop_container(context_name, container_name)
                logger.info(f"cleaned up {container_name}")
            except Exception as e:
                logger.error(f"failed to cleanup container for user {user_id}: {e}")

        with self.lock:
            self.active_containers.clear()
            self.session_timers.clear()
            self.creation_status.clear()

        self.orchestrator.cleanup()
        logger.info("cleanup completed")
