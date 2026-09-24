from __future__ import annotations

import base64
import time
import datetime
import logging
from functools import wraps
import json
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import TypedDict, TypeGuard
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from flask import Blueprint, request, jsonify, render_template, Response, stream_with_context
from CTFd.models import db, Users
from CTFd.utils.decorators import authed_only, admins_only
from CTFd.utils.user import authed, get_current_user, is_admin, is_verified, get_ip
from .container_manager import ContainerManager, ContainerInfoDict, TimerDict, TimerStatusDict
from .orchestrator import Orchestrator
from .event_logger import event_logger, get_persisted_events, EventDict
from .models import (
    user_flags,
    username_or_fallback,
    SETTING_DEFAULTS,
    END_REASON_RECONCILIATION,
    END_REASON_ADMIN_KILLED,
    _esc,
)
from .docker_host_manager import (
    IMAGE_CONTRACT_VERSION,
    LOCAL_CONTEXT_NAME,
    LOCAL_SOCKET_PATH,
    discover_contexts,
    ping_endpoint,
)
from .exceptions import HostsUnavailableException
from .messages import (
    CONFIRM_REQUIRED,
    CREATE_ALREADY_RUNNING,
    CREATE_IN_PROGRESS,
    EMAIL_VERIFICATION_PAGE,
    EMAIL_VERIFICATION_REQUIRED,
    FEATURE_DISABLED,
    FEATURE_DISABLED_PAGE,
    INVALID_REQUEST,
    LIFECYCLE_BUSY,
    NOT_DESTROYABLE,
    NO_ACTIVE_SESSION,
    NO_ACTIVE_CONTAINER,
    REPORT_EMPTY,
    REPORT_TOO_LONG,
    SERVER_ERROR,
    SESSION_ALREADY_EXISTS,
    SESSION_SUSPENDED,
    SETTINGS_INVALID,
    STATE_UNKNOWN,
)
from .utils import normalize_public_hostname, ratelimit_per_user

logger = logging.getLogger(__name__)


UserInfoDict = dict[str, str | bool]
SessionDict = dict[str, float | str | TimerDict | None]

_DEFAULT_MAX_EXTENSIONS = int(str(SETTING_DEFAULTS["max_extensions"]))


class TopUserAccum(TypedDict):
    total_duration: float
    session_count: int
    username: str


class HostAccum(TypedDict):
    sessions: int
    total_duration: float
    failures: int


def _user_info(user: Users | None, fallback_id: int | None = None) -> UserInfoDict:
    if not user:
        return {"username": f"User {fallback_id}"}
    return {"username": _esc(user.name), **user_flags(user)}


def _target_flags(user: Users | None) -> dict[str, bool]:
    return {f"target_{k}": v for k, v in user_flags(user).items()}


def _log_admin_target_action(
    admin_user: Users,
    message: str,
    action: str,
    user_id: int,
    target_username: str,
    target_user: Users | None,
    *,
    level: str,
) -> None:
    event_logger.log_event(
        "admin_action",
        message,
        user_id=admin_user.id,
        username=admin_user.name,
        level=level,
        metadata={
            "action": action,
            "target_id": user_id,
            "target": target_username,
            **_target_flags(target_user),
        },
    )


_INFRA_ERROR_TOKENS = ("context", "docker host", "unreachable", "unavailable", "no healthy contexts", "at capacity")
_TERMINAL_ERRORS = frozenset(
    {
        SESSION_SUSPENDED,
        NOT_DESTROYABLE,
        NO_ACTIVE_SESSION,
        STATE_UNKNOWN,
        # create race losers are a routine double submit, 409 not 500
        CREATE_IN_PROGRESS,
        CREATE_ALREADY_RUNNING,
        LIFECYCLE_BUSY,
    }
)


def _infra_status(error: str | None) -> int:
    # 503 marks the failure as infrastructure so clients and probes know a retry is worthwhile
    if not error:
        return 500
    if error in _TERMINAL_ERRORS:
        return 409
    lowered = error.lower()
    return 503 if any(tok in lowered for tok in _INFRA_ERROR_TOKENS) else 500


def _json_integer(value: object, *, minimum: int, field: str) -> int:
    """exact int check, bool and float are rejected rather than truncated"""
    if type(value) is not int:
        if field == "weight":
            raise ValueError("weight must be an integer")
        raise ValueError("max_containers must be a non-negative integer or null")
    if value < minimum:
        if field == "weight":
            raise ValueError("weight must be at least 1")
        raise ValueError("max_containers must be a non-negative integer or null")
    return value


def _is_clean_hostname_string(value: object) -> TypeGuard[str]:
    return (
        isinstance(value, str)
        and bool(value)
        and len(value) <= 512
        and value == value.strip()
        and not any(char.isspace() or ord(char) == 127 for char in value)
    )


def _is_ssh_target(candidate: str) -> bool:
    try:
        parsed = urlparse(candidate)
        parsed_port = parsed.port
    except ValueError:
        return False
    if parsed.scheme != "ssh" or not parsed.hostname or parsed.password is not None:
        return False
    if parsed.netloc.endswith(":") or parsed_port == 0:
        return False
    if parsed.username is not None and (not parsed.username or "@" in parsed.username):
        return False
    return parsed.path in ("", "/") and not parsed.params and not parsed.query and not parsed.fragment


def _context_hostname(value: object) -> str | None:
    if value is None:
        return None
    if not _is_clean_hostname_string(value):
        raise ValueError("hostname must be a non-empty string of at most 512 characters without whitespace")
    candidate = value if value.startswith("ssh://") else f"ssh://{'root@' if '@' not in value else ''}{value}"
    if not _is_ssh_target(candidate):
        raise ValueError("hostname must be an SSH target such as root@runner.example or ssh://root@runner.example")
    return value


def _request_tz() -> datetime.tzinfo:
    name = request.args.get("tz", "")
    if name:
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            pass
    return datetime.UTC


def _require_confirm():
    # confirm token keeps a stray post from admin xss or a hijacked session from wiping audit data
    payload = request.get_json(silent=True) or {}
    if payload.get("confirm") != "DELETE":
        return jsonify({"error": CONFIRM_REQUIRED}), 400
    return None


def create_routes(container_manager: ContainerManager, orchestrator: Orchestrator) -> Blueprint:
    remote_desktop_bp = Blueprint(
        "remote_desktop",
        __name__,
        template_folder="templates",
        static_folder="static",
        static_url_path="/remote-desktop/static",
    )

    def _reload_contexts_everywhere() -> None:
        from . import event_bus

        orchestrator.load_from_db()
        event_bus.publish({"_control": "reload_contexts"})

    def _lock_context_for_administration(model, context_id: int):
        """the lock makes a drain, edit, or delete decision atomic with Orchestrator._try_reserve
        populate_existing avoids a stale identity map value left by the rollback"""
        db.session.rollback()
        return model.query.filter_by(id=context_id).populate_existing().with_for_update().first()

    def _context_has_live_work(context, container_model, operation_model) -> bool:
        has_rows = container_model.query.filter_by(docker_context=context.context_name).first() is not None
        has_reservations = (
            operation_model.query.filter_by(
                docker_context=context.context_name,
                capacity_reserved=True,
            ).first()
            is not None
        )
        return int(context.active_sessions or 0) > 0 or has_rows or has_reservations

    # keep in sync with container_manager._timer_from_row which builds the same shape
    def _timer_dict(timer_status: TimerStatusDict) -> TimerDict | None:
        if not timer_status.get("success"):
            return None
        return {
            "active": bool(timer_status.get("started", False)),
            "time_remaining": int(timer_status.get("time_remaining", 0)),
            "extensions_used": int(timer_status.get("extensions_used", 0)),
            "max_extensions": int(timer_status.get("max_extensions", _DEFAULT_MAX_EXTENSIONS)),
        }

    def _session_dict(container_info: ContainerInfoDict, timer_status: TimerStatusDict) -> SessionDict:
        return {
            "created_at": container_info["created_at"],
            "vnc_url": container_info.get("vnc_url", ""),
            "timer": _timer_dict(timer_status),
        }

    def _apply_period_filter(query: db.Query, column: db.Column, period: str | None) -> db.Query:
        if period == "week":
            query = query.filter(column >= time.time() - 7 * 86400)
        elif period == "month":
            query = query.filter(column >= time.time() - 30 * 86400)
        return query

    @remote_desktop_bp.route("/remote-desktop")
    @authed_only
    def remote_desktop_page():
        from .models import get_setting

        if not get_setting("remote_desktop_enabled", True):
            return render_template(
                "remote_desktop.html",
                page_blocked="disabled",
                disabled_message=FEATURE_DISABLED_PAGE,
                verification_message=EMAIL_VERIFICATION_PAGE,
                server_error_message=SERVER_ERROR,
            )

        user = get_current_user()

        if get_setting("require_verified") and not is_admin() and not is_verified():
            return render_template(
                "remote_desktop.html",
                page_blocked="unverified",
                disabled_message=FEATURE_DISABLED_PAGE,
                verification_message=EMAIL_VERIFICATION_PAGE,
                server_error_message=SERVER_ERROR,
            )

        container_info = container_manager.get_container_info(user.id)
        creation_status = container_manager.get_creation_status(user.id)

        vnc_url = ""
        terminal_url = ""
        template_container_info = None
        ssh_info = None

        if container_info:
            vnc_url = str(container_info.get("vnc_url", ""))
            template_container_info = {
                "container_id": container_info["container_id"],
                "container_name": container_info["container_name"],
                "vnc_port": container_info["vnc_port"],
                "novnc_port": container_info["novnc_port"],
                "docker_context": container_info["docker_context"],
                "created_at": container_info["created_at"],
            }

            if container_info.get("ttyd_port"):
                terminal_url = f"/remote-desktop/terminal/{user.id}/"

            # ssh connects straight to the container host, it is not proxied through ctfd
            if container_info.get("ssh_port"):
                ssh_host = str(container_info["pub_hostname"])
                if ssh_host.startswith("[") and ssh_host.endswith("]"):
                    ssh_host = ssh_host[1:-1]
                ssh_info = {
                    "host": ssh_host,
                    "port": container_info["ssh_port"],
                    "username": container_info["container_username"],
                    "password": container_info["vnc_password"],
                }

        return render_template(
            "remote_desktop.html",
            disabled_message=FEATURE_DISABLED_PAGE,
            verification_message=EMAIL_VERIFICATION_PAGE,
            server_error_message=SERVER_ERROR,
            container_info=template_container_info,
            vnc_url=vnc_url,
            terminal_url=terminal_url,
            creation_status=creation_status,
            ssh_info=ssh_info,
            max_extensions=get_setting("max_extensions"),
            ssh_enabled=get_setting("ssh_enabled"),
            web_terminal_enabled=get_setting("web_terminal_enabled"),
        )

    @remote_desktop_bp.route("/remote-desktop/api/status", methods=["GET"])
    @authed_only
    def get_status():
        user = get_current_user()
        container_info = container_manager.get_container_info(user.id)

        if not container_info:
            return jsonify({"session": None})

        timer_status = container_manager.get_session_timer_status(user.id)
        return jsonify({"session": _session_dict(container_info, timer_status)})

    @remote_desktop_bp.route("/remote-desktop/api/create", methods=["POST"])
    @authed_only
    @ratelimit_per_user(method="POST", limit=5, interval=300)
    def create_session():
        from .models import SettingsValidationError, get_all_settings

        try:
            effective_settings = get_all_settings()
        except SettingsValidationError as exc:
            logger.error("session admission refused because stored settings are invalid: %s", exc)
            return jsonify({"error": SETTINGS_INVALID}), 503

        if not effective_settings["remote_desktop_enabled"]:
            return jsonify({"error": FEATURE_DISABLED}), 403

        user = get_current_user()

        if effective_settings["require_verified"] and not is_admin() and not is_verified():
            return jsonify({"error": EMAIL_VERIFICATION_REQUIRED}), 403

        logger.info(f"create session request from user {user.name} (ID: {user.id})")

        if container_manager.get_container_info(user.id):
            event_logger.log_event(
                "session_error",
                "attempted to create session but already exists",
                user_id=user.id,
                username=user.name,
                level="warning",
            )
            return jsonify({"error": SESSION_ALREADY_EXISTS}), 400

        creation_status = container_manager.get_creation_status(user.id)
        if creation_status and creation_status.get("status") not in ["failed", "none"]:
            event_logger.log_event(
                "session_error",
                "attempted to create session but creation already in progress",
                user_id=user.id,
                username=user.name,
                level="warning",
            )
            return jsonify({"error": CREATE_IN_PROGRESS}), 400

        # localhost inside the container is the container itself, so firefox needs host.docker.internal
        parsed = urlparse(request.url_root)
        if parsed.hostname in ("localhost", "127.0.0.1"):
            container_host = "host.docker.internal"
            extra_hosts = {"host.docker.internal": "host-gateway"}
        else:
            container_host = parsed.hostname or ""
            extra_hosts = None
        port_part = f":{parsed.port}" if parsed.port else ""
        container_url = f"{parsed.scheme}://{container_host}{port_part}/"

        try:
            # get_ip walks TRUSTED_PROXIES so the recorded ip matches ctfd, unlike a hand parse of forwarded headers
            result = container_manager.create_container(user.id, container_url, extra_hosts, client_ip=get_ip())
        except HostsUnavailableException as err:
            return jsonify({"error": str(err)}), 503

        if not result.get("success"):
            error = str(result.get("error", "Creation failed"))
            return jsonify({"error": error}), _infra_status(error)

        return jsonify(
            {
                "status": "creating",
                "message": "Container creation started",
            }
        )

    @remote_desktop_bp.route("/remote-desktop/api/creation-status", methods=["GET"])
    @authed_only
    def get_creation_status():
        user = get_current_user()
        status = container_manager.get_creation_status(user.id)

        if not status:
            container_info = container_manager.get_container_info(user.id)
            if container_info:
                timer_status = container_manager.get_session_timer_status(user.id)
                return jsonify(
                    {
                        "status": "ready",
                        "message": "Desktop ready!",
                        "session": _session_dict(container_info, timer_status),
                    }
                )
            return jsonify({"status": "none"})

        if status.get("status") == "ready":
            container_info = container_manager.get_container_info(user.id)
            # status can still say ready after the container expired or was reaped, leaving no session
            if not container_info:
                return jsonify({"status": "none"})
            timer_status = container_manager.get_session_timer_status(user.id)
            return jsonify(
                {
                    "status": "ready",
                    "message": status.get("message", "Desktop ready!"),
                    "session": _session_dict(container_info, timer_status),
                }
            )

        return jsonify(status)

    @remote_desktop_bp.route("/remote-desktop/api/destroy", methods=["POST"])
    @authed_only
    @ratelimit_per_user(method="POST", limit=20, interval=300)
    def destroy_session():
        user = get_current_user()

        if not container_manager.get_container_info(user.id):
            return jsonify({"error": NO_ACTIVE_SESSION}), 400

        result = container_manager.destroy_container(user.id)
        if not result.get("success"):
            error = str(result.get("error", "Destruction failed"))
            return jsonify({"error": error}), _infra_status(error)

        return jsonify({"session": None})

    @remote_desktop_bp.route("/remote-desktop/api/extend", methods=["POST"])
    @authed_only
    @ratelimit_per_user(method="POST", limit=10, interval=300)
    def extend_session():
        user = get_current_user()

        if not container_manager.get_container_info(user.id):
            return jsonify({"error": NO_ACTIVE_SESSION}), 400

        result = container_manager.extend_session_timer(user.id)
        if not result.get("success"):
            return jsonify({"error": result.get("error", "Extension failed")}), 400

        timer_status = container_manager.get_session_timer_status(user.id)
        return jsonify({"timer": _timer_dict(timer_status)})

    @remote_desktop_bp.route("/remote-desktop/api/report", methods=["POST"])
    @authed_only
    @ratelimit_per_user(method="POST", limit=5, interval=3600, count_4xx=False)
    def submit_report():
        from .models import DesktopReportModel, get_setting

        if not get_setting("remote_desktop_enabled", True):
            return jsonify({"error": FEATURE_DISABLED}), 403

        user = get_current_user()

        if get_setting("require_verified") and not is_admin() and not is_verified():
            return jsonify({"error": EMAIL_VERIFICATION_REQUIRED}), 403

        content = (request.form.get("content") or "").strip()
        if not content:
            return jsonify({"error": REPORT_EMPTY}), 400

        # cap length so one user cannot dump megabytes through repeated posts under the rate limit
        if len(content) > 5000:
            return jsonify({"error": REPORT_TOO_LONG}), 400

        report = DesktopReportModel(
            user_id=user.id,
            username=user.name,
            timestamp=time.time(),
            content=content,
        )
        db.session.add(report)
        db.session.commit()

        return jsonify({"success": True, "id": report.id})

    @remote_desktop_bp.route("/remote-desktop/api/cleanup", methods=["POST"])
    @admins_only
    def trigger_cleanup():
        from flask import current_app
        from . import _claim_scheduler_leader

        # the sweep audits and reconciles every host, the same single leader contract as the scheduled job
        if not _claim_scheduler_leader(current_app._get_current_object()):
            return jsonify({"error": "cleanup runs on the scheduler leader"}), 409

        container_manager.periodic_cleanup()
        return jsonify({"success": True, "message": "Cleanup triggered"})

    @remote_desktop_bp.route("/remote-desktop/dashboard")
    @admins_only
    def admin_dashboard():
        return render_template("remote_desktop_dashboard.html")

    @remote_desktop_bp.route("/remote-desktop/dashboard/api/user-flags", methods=["GET"])
    @admins_only
    def admin_user_flags():
        # cap at 1000 to avoid scanning the whole users table on large instances
        rows = Users.query.limit(1000).all()
        flags = {}
        for u in rows:
            f = user_flags(u)
            if f:
                flags[u.id] = f
        return jsonify(flags)

    @remote_desktop_bp.route("/remote-desktop/dashboard/api/containers", methods=["GET"])
    @admins_only
    def admin_get_containers():
        containers = container_manager.get_all_containers()
        return jsonify({"containers": containers})

    @remote_desktop_bp.route("/remote-desktop/dashboard/api/paused-orphans", methods=["GET"])
    @admins_only
    def admin_get_paused_orphans():
        # docker identity values stay raw, the dashboard escapes at render so removal requests match byte for byte
        return jsonify({"orphans": container_manager.list_paused_orphans()})

    @remote_desktop_bp.route("/remote-desktop/dashboard/api/paused-orphans/remove", methods=["POST"])
    @admins_only
    def admin_remove_paused_orphan():
        rejection = _require_confirm()
        if rejection is not None:
            return rejection
        payload = request.get_json(silent=True) or {}
        context_name = payload.get("context")
        container_id = payload.get("container_id")
        container_name = payload.get("container_name")
        if not all(
            isinstance(value, str) and 0 < len(value) <= 512 for value in (context_name, container_id, container_name)
        ):
            return jsonify({"error": "context, container_id, and container_name must be non-empty strings"}), 400
        assert isinstance(context_name, str)
        assert isinstance(container_id, str)
        assert isinstance(container_name, str)

        result = container_manager.remove_paused_orphan_admin(
            get_current_user(),
            context_name,
            container_id,
            container_name,
        )
        if result.get("success"):
            return jsonify(result)
        error = str(result.get("error", "Failed to remove paused orphan"))
        status = 503 if "unavailable" in error.lower() else 409
        return jsonify({"error": error}), status

    @remote_desktop_bp.route("/remote-desktop/dashboard/api/hosts", methods=["GET"])
    @admins_only
    def admin_get_hosts():
        status = orchestrator.get_status()
        return jsonify({"hosts": status})

    @remote_desktop_bp.route("/remote-desktop/dashboard/api/kill", methods=["POST"])
    @admins_only
    def admin_kill_container():
        admin_user = get_current_user()
        user_id = request.form.get("user_id", type=int)
        if user_id is None:
            return jsonify({"error": "user_id must be an integer"}), 400

        target_user = Users.query.filter_by(id=user_id).first()
        if not target_user:
            return jsonify({"error": "User not found"}), 404
        result = container_manager.destroy_container(user_id, reason=END_REASON_ADMIN_KILLED)
        if not result.get("success"):
            error = str(result.get("error", "Failed to kill container"))
            status = 400 if error in {NO_ACTIVE_CONTAINER, NO_ACTIVE_SESSION} else _infra_status(error)
            return jsonify({"error": error}), status

        target_username = username_or_fallback(target_user, user_id)
        action_text = "requested session cancellation" if result.get("status") == "cancelling" else "killed session"
        _log_admin_target_action(
            admin_user,
            f"admin {admin_user.name} {action_text} for {target_username}",
            "kill",
            user_id,
            target_username,
            target_user,
            level="warning",
        )

        return jsonify({"success": True})

    @remote_desktop_bp.route("/remote-desktop/dashboard/api/peek", methods=["POST"])
    @admins_only
    def admin_peek_session():
        # fail closed, same origin monitoring would forward the admin ctfd cookie to student controlled assets
        return jsonify({"error": "Cross-user session monitoring is disabled"}), 403

    @remote_desktop_bp.route("/remote-desktop/dashboard/api/extend", methods=["POST"])
    @admins_only
    def admin_extend_session():
        admin_user = get_current_user()
        user_id = request.form.get("user_id", type=int)
        if user_id is None:
            return jsonify({"error": "user_id must be an integer"}), 400

        if not container_manager.get_container_info(user_id):
            return jsonify({"error": "No active session for user"}), 400

        target_user = Users.query.filter_by(id=user_id).first()
        if not target_user:
            return jsonify({"error": "User not found"}), 404
        target_username = username_or_fallback(target_user, user_id)

        _log_admin_target_action(
            admin_user,
            f"admin {admin_user.name} extended session for {target_username}",
            "extend",
            user_id,
            target_username,
            target_user,
            level="info",
        )

        result = container_manager.extend_session_timer(user_id)

        if result.get("success"):
            return jsonify({"success": True})
        return jsonify({"error": result.get("error", "Failed to extend session")}), 400

    @remote_desktop_bp.route("/remote-desktop/dashboard/api/pause", methods=["POST"])
    @admins_only
    def admin_pause_session():
        admin_user = get_current_user()
        user_id = request.form.get("user_id", type=int)
        if user_id is None:
            return jsonify({"error": "user_id must be an integer"}), 400
        target_user = Users.query.filter_by(id=user_id).first()
        if not target_user:
            return jsonify({"error": "User not found"}), 404
        target_username = username_or_fallback(target_user, user_id)

        result = container_manager.pause_session(user_id)
        if not result.get("success"):
            return jsonify({"error": result.get("error", "Failed to pause session")}), 400

        _log_admin_target_action(
            admin_user,
            f"admin {admin_user.name} paused session for {target_username}",
            "pause",
            user_id,
            target_username,
            target_user,
            level="warning",
        )
        event_logger.log_event(
            "session_paused",
            f"session paused by admin {admin_user.name}",
            user_id=user_id,
            username=target_username,
            level="error",
            metadata={"source": "admin"},
        )
        return jsonify({"success": True})

    @remote_desktop_bp.route("/remote-desktop/dashboard/api/unpause", methods=["POST"])
    @admins_only
    def admin_unpause_session():
        admin_user = get_current_user()
        user_id = request.form.get("user_id", type=int)
        if user_id is None:
            return jsonify({"error": "user_id must be an integer"}), 400
        target_user = Users.query.filter_by(id=user_id).first()
        if not target_user:
            return jsonify({"error": "User not found"}), 404
        target_username = username_or_fallback(target_user, user_id)

        result = container_manager.unpause_session(user_id)
        if not result.get("success"):
            return jsonify({"error": result.get("error", "Failed to unpause session")}), 400

        _log_admin_target_action(
            admin_user,
            f"admin {admin_user.name} unpaused session for {target_username}",
            "unpause",
            user_id,
            target_username,
            target_user,
            level="info",
        )
        event_logger.log_event(
            "session_unpaused",
            f"session unpaused by admin {admin_user.name}",
            user_id=user_id,
            username=target_username,
            level="info",
            metadata={"source": "admin"},
        )
        return jsonify({"success": True})

    @remote_desktop_bp.route("/remote-desktop/dashboard/api/kill-all", methods=["POST"])
    @admins_only
    def admin_kill_all():
        admin_user = get_current_user()
        summary = container_manager.destroy_all_containers_admin(admin_user)
        payload: dict[str, object] = {
            "success": summary["failed"] == 0,
            "killed": summary["completed"],
            **summary,
        }
        if summary["failed"]:
            payload["error"] = (
                f"Fleet teardown partially failed: {summary['completed']} completed, "
                f"{summary['cancelling']} cancelling, {summary['stopping']} stopping, "
                f"{summary['failed']} failed"
            )
        return jsonify(payload)

    @remote_desktop_bp.route("/remote-desktop/dashboard/api/clear_history", methods=["POST"])
    @admins_only
    def admin_clear_history():
        rejection = _require_confirm()
        if rejection is not None:
            return rejection

        from .models import DesktopSessionHistoryModel

        session_count = DesktopSessionHistoryModel.query.count()
        DesktopSessionHistoryModel.query.delete()
        db.session.commit()

        admin_user = get_current_user()
        event_logger.log_event(
            "admin_action",
            f"cleared {session_count} sessions",
            user_id=admin_user.id if admin_user else None,
            username=admin_user.name if admin_user else None,
            level="warning",
            metadata={"action": "clear_history", "sessions": session_count},
        )
        return jsonify({"success": True, "sessions": session_count})

    @remote_desktop_bp.route("/remote-desktop/dashboard/api/reports", methods=["GET"])
    @admins_only
    def admin_list_reports():
        from .models import DesktopReportModel

        rows = DesktopReportModel.query.order_by(DesktopReportModel.timestamp.desc()).all()
        user_ids = {r.user_id for r in rows}
        users_by_id = {u.id: u for u in Users.query.filter(Users.id.in_(user_ids)).all()}
        target_users = {r.user_id: users_by_id.get(r.user_id) for r in rows}
        reports = [
            {
                "id": r.id,
                "user_id": r.user_id,
                "username": _esc(r.username),
                "timestamp": r.timestamp,
                "content": _esc(r.content),
                **_target_flags(target_users.get(r.user_id)),
            }
            for r in rows
        ]
        return jsonify({"reports": reports})

    @remote_desktop_bp.route("/remote-desktop/dashboard/api/reports/<int:report_id>/delete", methods=["POST"])
    @admins_only
    def admin_delete_report(report_id: int):
        from .models import DesktopReportModel

        row = DesktopReportModel.query.filter_by(id=report_id).first()
        if not row:
            return jsonify({"error": "Report not found"}), 404
        db.session.delete(row)
        db.session.commit()
        return jsonify({"success": True})

    @remote_desktop_bp.route("/remote-desktop/dashboard/api/reports/clear", methods=["POST"])
    @admins_only
    def admin_clear_reports():
        rejection = _require_confirm()
        if rejection is not None:
            return rejection

        from .models import DesktopReportModel

        count = DesktopReportModel.query.count()
        DesktopReportModel.query.delete()
        db.session.commit()

        admin_user = get_current_user()
        event_logger.log_event(
            "admin_action",
            f"cleared {count} reports",
            user_id=admin_user.id if admin_user else None,
            username=admin_user.name if admin_user else None,
            level="warning",
            metadata={"action": "clear_reports", "reports": count},
        )
        return jsonify({"success": True, "reports": count})

    @remote_desktop_bp.route("/remote-desktop/dashboard/api/images/matrix", methods=["GET"])
    @admins_only
    def admin_images_matrix():
        from .models import get_all_settings, set_setting

        settings = get_all_settings()
        docker_image = str(settings.get("docker_image", "ctfd-remote-desktop:latest"))
        display = docker_image.removesuffix(":latest")

        connected = container_manager.host_manager.get_connected_contexts()
        matrix: dict[str, dict[str, dict[str, object]]] = {display: {}}
        if not connected:
            set_setting(
                "image_cache",
                json.dumps({"matrix": matrix, "contexts": [], "scanned_at": time.time()}),
            )
            return jsonify(images=[display], contexts=[], matrix=matrix)

        def _info(ctx_name):
            return ctx_name, container_manager.host_manager.get_image_info(ctx_name, docker_image)

        def _future_entry(future) -> dict[str, object]:
            try:
                _ctx_name, info = future.result()
            except Exception:
                return {"available": False}
            entry: dict[str, object] = {"available": bool(info and info.get("contract_status") == "compatible")}
            if info:
                entry["info"] = info
            return entry

        pool = ThreadPoolExecutor(max_workers=min(len(connected), 8))
        futures = {}
        try:
            futures = {pool.submit(_info, ctx): ctx for ctx in connected}
            for future in as_completed(futures, timeout=15):
                matrix[display][futures[future]] = _future_entry(future)
        except TimeoutError:
            pass
        finally:
            for future in futures:
                future.cancel()
            # no wait shutdown, a wedged ssh transport would stretch the 15s scan into an unbounded wait
            pool.shutdown(wait=False, cancel_futures=True)

        for ctx in connected:
            matrix[display].setdefault(ctx, {"available": False})

        set_setting(
            "image_cache",
            json.dumps({"matrix": matrix, "contexts": connected, "scanned_at": time.time()}),
        )
        return jsonify(images=[display], contexts=connected, matrix=matrix)

    @remote_desktop_bp.route("/remote-desktop/dashboard/api/images/cache", methods=["GET"])
    @admins_only
    def admin_images_cache():
        from .models import get_setting

        raw = get_setting("image_cache")
        if not raw:
            return jsonify(cached=False)
        try:
            cache = json.loads(str(raw))
        except (json.JSONDecodeError, TypeError):
            return jsonify(cached=False)

        if (
            not isinstance(cache, dict)
            or not isinstance(cache.get("matrix"), dict)
            or "contexts" not in cache
            or "scanned_at" not in cache
        ):
            return jsonify(cached=False)

        return jsonify(
            cached=True,
            images=sorted(cache["matrix"].keys()),
            contexts=cache["contexts"],
            matrix=cache["matrix"],
            scanned_at=cache["scanned_at"],
        )

    @remote_desktop_bp.route("/remote-desktop/dashboard/api/stats/top-users", methods=["GET"])
    @admins_only
    def admin_stats_top_users():
        period = request.args.get("period", "all")
        rows = _session_query(period).all()

        user_stats: defaultdict[int, TopUserAccum] = defaultdict(
            lambda: {"total_duration": 0.0, "session_count": 0, "username": ""}
        )
        for row in rows:
            entry = user_stats[row.user_id]
            entry["total_duration"] += row.duration
            entry["session_count"] += 1
            entry["username"] = row.username

        top = sorted(user_stats.items(), key=lambda x: x[1]["total_duration"], reverse=True)[:15]
        users_by_id = {u.id: u for u in Users.query.filter(Users.id.in_([uid for uid, _ in top])).all()}
        users = []
        for uid, stats in top:
            out: dict[str, object] = {"user_id": uid, **stats}
            u = users_by_id.get(uid)
            if u:
                out.update(_user_info(u))
            users.append(out)
        return jsonify({"users": users})

    @remote_desktop_bp.route("/remote-desktop/dashboard/api/stats/summary", methods=["GET"])
    @admins_only
    def admin_stats_summary():
        from .models import DesktopContainerInfoModel

        active = DesktopContainerInfoModel.query.count()
        healthy_contexts = sum(1 for h in orchestrator.health.values() if h)
        total_contexts = len(orchestrator.health)

        rows = _session_query().all()
        total_sessions = len(rows) + active

        durations = [r.duration for r in rows if r.duration and r.duration > 0]
        avg_duration = sum(durations) / len(durations) if durations else 0

        events = []
        now = time.time()
        for r in rows:
            if r.started_at:
                events.append((r.started_at, 1))
                events.append((r.ended_at or now, -1))

        events.sort(key=lambda e: (e[0], e[1]))
        peak = 0
        current = 0
        for _, delta in events:
            current += delta
            peak = max(peak, current)
        peak = max(peak, active)

        unique_users = len({r.user_id for r in rows})
        total_hours = sum(durations) / 3600

        return jsonify(
            {
                "active": active,
                "total_sessions": total_sessions,
                "avg_duration": avg_duration,
                "peak_concurrent": peak,
                "unique_users": unique_users,
                "healthy_contexts": healthy_contexts,
                "total_contexts": total_contexts,
                "total_hours": round(total_hours, 1),
            }
        )

    def _proxy_auth(
        user_id_header: str,
        port_attr: str,
        host_header: str,
        port_header: str,
        authorization_header: str | None = None,
    ) -> Response | tuple[str, int]:
        from .models import DesktopContainerInfoModel, LIFECYCLE_ACTIVE

        raw_user_id = request.headers.get(user_id_header)
        if not raw_user_id:
            return "", 400

        try:
            user_id = int(raw_user_id)
        except (ValueError, TypeError):
            return "", 400
        current_user = get_current_user()
        # admins reach their own desktop only, cross user access needs a separate origin and a trusted viewer
        if current_user.id != user_id:
            return "", 403

        # auth_request fires per static asset, liveness reap stays in /api/status so this path only reads
        row = DesktopContainerInfoModel.query.filter_by(user_id=user_id).first()
        port = getattr(row, port_attr, None) if row else None
        lifecycle_state = getattr(row, "lifecycle_state", LIFECYCLE_ACTIVE) if row else None
        if row and not isinstance(lifecycle_state, str):
            lifecycle_state = LIFECYCLE_ACTIVE
        if not row or lifecycle_state != LIFECYCLE_ACTIVE or port is None:
            return "", 404

        # pub_hostname is the browser address, ctfd reaches ports through the endpoint, fenced while work exists
        try:
            docker_context = getattr(row, "docker_context", None)
            if not isinstance(docker_context, str) or not docker_context:
                return "", 502
            check_hostname = container_manager.host_manager.get_check_hostname(docker_context)
            if not isinstance(check_hostname, str) or not check_hostname:
                return "", 502
        except Exception:
            logger.exception("failed to resolve internal proxy host for user %s", user_id)
            return "", 502

        resp = Response("", 200)
        resp.headers[host_header] = check_hostname
        resp.headers[port_header] = str(port)
        if authorization_header is not None:
            username = getattr(row, "container_username", None)
            password = getattr(row, "vnc_password", None)
            if not isinstance(username, str) or not username or not isinstance(password, str) or not password:
                return "", 502
            token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
            resp.headers[authorization_header] = f"Basic {token}"
        return resp

    def _subrequest_authed(fn):
        # nginx auth_request maps anything but 401 and 403 to a 500, so never redirect anonymous callers
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if not authed():
                return "", 401
            return fn(*args, **kwargs)

        return wrapper

    @remote_desktop_bp.route("/remote-desktop/vnc/auth", methods=["GET"])
    @_subrequest_authed
    def vnc_auth():
        return _proxy_auth("X-VNC-User-ID", "novnc_port", "X-VNC-Host", "X-VNC-Port")

    @remote_desktop_bp.route("/remote-desktop/terminal/auth", methods=["GET"])
    @_subrequest_authed
    def terminal_auth():
        return _proxy_auth(
            "X-Terminal-User-ID",
            "ttyd_port",
            "X-Terminal-Host",
            "X-Terminal-Port",
            "X-Terminal-Authorization",
        )

    @remote_desktop_bp.route("/remote-desktop/dashboard/api/stats/per-host", methods=["GET"])
    @admins_only
    def admin_stats_per_host():
        period = request.args.get("period", "all")
        rows = _session_query(period).all()

        hosts: defaultdict[str, HostAccum] = defaultdict(lambda: {"sessions": 0, "total_duration": 0.0, "failures": 0})
        for row in rows:
            ctx = row.docker_context
            hosts[ctx]["sessions"] += 1
            hosts[ctx]["total_duration"] += row.duration
            if row.end_reason == END_REASON_RECONCILIATION:
                hosts[ctx]["failures"] += 1

        result = [{"host": _esc(h), **hosts[h]} for h in sorted(hosts.keys())]
        return jsonify({"hosts": result})

    @remote_desktop_bp.route("/remote-desktop/dashboard/api/stats/heatmap", methods=["GET"])
    @admins_only
    def admin_stats_heatmap():
        period = request.args.get("period", "all")
        rows = _session_query(period).all()
        tz = _request_tz()

        counts = [[0] * 7 for _ in range(24)]
        durations = [[0.0] * 7 for _ in range(24)]

        for r in rows:
            if not r.started_at:
                continue
            dt = datetime.datetime.fromtimestamp(r.started_at, tz=tz)
            day_idx = dt.weekday()
            counts[dt.hour][day_idx] += 1
            durations[dt.hour][day_idx] += r.duration or 0

        data = []
        for hour in range(24):
            for day in range(7):
                if counts[hour][day] > 0:
                    data.append([day, hour, counts[hour][day], round(durations[hour][day] / 3600, 1)])

        return jsonify({"data": data, "days": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]})

    @remote_desktop_bp.route("/remote-desktop/dashboard/api/stats/duration-distribution", methods=["GET"])
    @admins_only
    def admin_stats_duration_dist():
        from .models import get_setting

        period = request.args.get("period", "all")
        rows = _session_query(period).all()

        initial = int(get_setting("initial_duration") or 3600)
        ext_dur = int(get_setting("extension_duration") or 1800)
        max_ext = int(get_setting("max_extensions") or 3)

        def _fmt(s: int | float) -> str:
            if s < 3600:
                return f"{int(s // 60)}m"
            h = int(s // 3600)
            m = int((s % 3600) // 60)
            return f"{h}h{m}m" if m else f"{h}h"

        edges = [0, 300, initial / 2, initial]
        labels = ["<5m", f"5m-{_fmt(initial / 2)}", f"{_fmt(initial / 2)}-{_fmt(initial)}"]
        hints = [
            "very short sessions may indicate remote desktop config issues",
            "users who left before using most of their time",
            "used most of the base session time",
        ]
        for i in range(1, max_ext + 1):
            lo = initial + ext_dur * (i - 1)
            hi = initial + ext_dur * i
            edges.append(hi)
            labels.append(f"{_fmt(lo)}-{_fmt(hi)}")
            if i == max_ext:
                hints.append("used all extensions, consider increasing time or extensions")
            elif i == 1:
                hints.append("needed a bit more time than the base session")
            else:
                hints.append(f"used {i} of {max_ext} extensions, may need a longer base time")

        counts = [0] * len(labels)
        for row in rows:
            d = row.duration or 0
            placed = False
            for j in range(len(edges) - 1):
                if d < edges[j + 1]:
                    counts[j] = counts[j] + 1
                    placed = True
                    break
            if not placed:
                counts[-1] = counts[-1] + 1

        result = [{"range": labels[i], "count": counts[i], "hint": hints[i]} for i in range(len(labels))]
        return jsonify({"buckets": result})

    @remote_desktop_bp.route("/remote-desktop/dashboard/api/stats/extensions", methods=["GET"])
    @admins_only
    def admin_stats_extensions():
        period = request.args.get("period", "all")
        rows = _session_query(period).all()

        ext_counts: defaultdict[int, int] = defaultdict(int)
        end_reasons: defaultdict[str, int] = defaultdict(int)
        for row in rows:
            ext_counts[row.extensions_used] += 1
            end_reasons[row.end_reason] += 1

        return jsonify(
            {
                "extensions": [{"count": k, "sessions": v} for k, v in sorted(ext_counts.items())],
                "end_reasons": [{"reason": k, "count": v} for k, v in sorted(end_reasons.items(), key=lambda x: -x[1])],
            }
        )

    def _session_query(period: str | None = None, limit: int = 10000) -> db.Query:
        from .models import DesktopSessionHistoryModel

        query = DesktopSessionHistoryModel.query.join(Users, DesktopSessionHistoryModel.user_id == Users.id).filter(
            Users.hidden.is_(False)
        )
        return _apply_period_filter(query, DesktopSessionHistoryModel.started_at, period).limit(limit)

    @remote_desktop_bp.route("/remote-desktop/dashboard/api/contexts", methods=["GET"])
    @admins_only
    def admin_get_contexts():
        from .models import DesktopDockerContextModel

        contexts = DesktopDockerContextModel.query.all()
        connected = set(container_manager.host_manager.get_connected_contexts())
        data = []
        for ctx in contexts:
            data.append(
                {
                    "id": ctx.id,
                    "context_name": _esc(ctx.context_name),
                    "hostname": _esc(ctx.hostname),
                    "pub_hostname": _esc(ctx.pub_hostname),
                    "weight": ctx.weight,
                    "enabled": ctx.enabled,
                    # raw column, null means auto derived cap, 0 means drain, any other value is an explicit cap
                    "max_containers": ctx.max_containers,
                    "active_sessions": int(ctx.active_sessions or 0),
                    "connected": ctx.context_name in connected,
                    "is_local": ctx.context_name == LOCAL_CONTEXT_NAME,
                }
            )
        docker_ok = ping_endpoint(f"unix://{LOCAL_SOCKET_PATH}")
        return jsonify({"contexts": data, "docker_socket": docker_ok})

    @remote_desktop_bp.route("/remote-desktop/dashboard/api/contexts/discover", methods=["GET"])
    @admins_only
    def admin_discover_contexts():
        from .models import DesktopDockerContextModel
        from .docker_host_manager import _get_host_gateway

        found = discover_contexts()
        existing = {ctx.context_name for ctx in DesktopDockerContextModel.query.all()}

        available = []
        for ctx in found:
            if ctx["name"] in existing:
                continue

            ep = ctx["endpoint"]
            if ep.startswith("unix://"):
                suggested = _get_host_gateway()
            elif "://" in ep:
                stripped = ep.split("://", 1)[-1]
                if "@" in stripped:
                    stripped = stripped.split("@", 1)[-1]
                stripped = stripped.split(":")[0].split("/")[0]
                suggested = stripped
            else:
                suggested = ""

            available.append(
                {
                    "name": _esc(ctx["name"]),
                    "endpoint": _esc(ctx["endpoint"]),
                    "suggested_hostname": _esc(suggested),
                }
            )

        if available:

            def _ping(ctx):
                ctx["reachable"] = ping_endpoint(ctx["endpoint"])
                return ctx

            with ThreadPoolExecutor(max_workers=len(available)) as pool:
                list(pool.map(_ping, available))

        return jsonify({"contexts": available})

    @remote_desktop_bp.route("/remote-desktop/dashboard/api/contexts", methods=["POST"])
    @admins_only
    def admin_add_context():
        from .models import DesktopDockerContextModel

        if not isinstance(request.json, dict):
            return jsonify({"error": INVALID_REQUEST}), 400

        context_name = request.json.get("context_name")
        hostname = request.json.get("hostname")
        pub_hostname = request.json.get("pub_hostname")
        weight = request.json.get("weight", 1)
        enabled = request.json.get("enabled", True)
        max_containers = request.json.get("max_containers")

        if (
            not isinstance(context_name, str)
            or not context_name
            or context_name != context_name.strip()
            or len(context_name) > 512
        ):
            return jsonify({"error": "context_name is required"}), 400
        try:
            hostname = _context_hostname(hostname)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        if type(enabled) is not bool:
            return jsonify({"error": "enabled must be a boolean"}), 400
        try:
            pub_hostname = normalize_public_hostname(pub_hostname)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

        try:
            weight = _json_integer(weight, minimum=1, field="weight")
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

        if max_containers in (None, ""):
            max_containers = None
        else:
            try:
                max_containers = _json_integer(max_containers, minimum=0, field="max_containers")
            except ValueError as exc:
                return jsonify({"error": str(exc)}), 400

        existing = DesktopDockerContextModel.query.filter_by(context_name=context_name).first()
        if existing:
            return jsonify({"error": "context already exists"}), 400

        new_context = DesktopDockerContextModel(
            context_name=context_name,
            hostname=hostname,
            pub_hostname=pub_hostname,
            weight=weight,
            enabled=enabled,
            max_containers=max_containers,
        )
        db.session.add(new_context)
        db.session.commit()

        _reload_contexts_everywhere()

        return jsonify({"success": True, "id": new_context.id})

    @remote_desktop_bp.route("/remote-desktop/dashboard/api/contexts/<int:context_id>", methods=["PUT"])
    @admins_only
    def admin_update_context(context_id):
        from .models import DesktopContainerInfoModel, DesktopDockerContextModel, DesktopSessionOperationModel

        if not isinstance(request.json, dict):
            return jsonify({"error": INVALID_REQUEST}), 400

        payload = request.json

        if "enabled" in payload and type(payload["enabled"]) is not bool:
            return jsonify({"error": "enabled must be a boolean"}), 400
        parsed_hostname = None
        if "hostname" in payload:
            try:
                parsed_hostname = _context_hostname(payload["hostname"])
            except ValueError as exc:
                return jsonify({"error": str(exc)}), 400

        normalized_pub_hostname = None
        if "pub_hostname" in payload:
            try:
                normalized_pub_hostname = normalize_public_hostname(payload["pub_hostname"])
            except ValueError as exc:
                return jsonify({"error": str(exc)}), 400

        weight = None
        if "weight" in payload:
            try:
                weight = _json_integer(payload["weight"], minimum=1, field="weight")
            except ValueError as exc:
                return jsonify({"error": str(exc)}), 400

        parsed_max_containers = None
        if "max_containers" in payload:
            raw = payload["max_containers"]
            if raw not in (None, ""):
                try:
                    parsed_max_containers = _json_integer(raw, minimum=0, field="max_containers")
                except ValueError as exc:
                    return jsonify({"error": str(exc)}), 400

        context = _lock_context_for_administration(DesktopDockerContextModel, context_id)
        if not context:
            db.session.rollback()
            return jsonify({"error": "context not found"}), 404

        current_pub_hostname = context.pub_hostname
        try:
            current_pub_hostname = normalize_public_hostname(current_pub_hostname)
        except ValueError:
            pass
        endpoint_change = ("hostname" in payload and parsed_hostname != context.hostname) or (
            "pub_hostname" in payload and normalized_pub_hostname != current_pub_hostname
        )
        disabling = "enabled" in payload and not payload["enabled"] and bool(context.enabled)

        # the row lock cannot stop a reservation made after commit, so an endpoint edit must stay fenced afterwards
        final_enabled = payload["enabled"] if "enabled" in payload else bool(context.enabled)
        final_max_containers = parsed_max_containers if "max_containers" in payload else context.max_containers
        remains_fenced = not final_enabled or final_max_containers == 0
        if (endpoint_change or disabling) and _context_has_live_work(
            context,
            DesktopContainerInfoModel,
            DesktopSessionOperationModel,
        ):
            db.session.rollback()
            return jsonify({"error": "context has active or in-flight sessions; drain it before changing access"}), 409

        if endpoint_change and not remains_fenced:
            db.session.rollback()
            return jsonify({"error": "drain or disable the context before changing its endpoint"}), 409

        if "hostname" in payload:
            context.hostname = parsed_hostname

        if "pub_hostname" in payload:
            context.pub_hostname = normalized_pub_hostname

        if "weight" in payload:
            context.weight = weight

        if "max_containers" in payload:
            context.max_containers = parsed_max_containers

        if "enabled" in payload:
            context.enabled = payload["enabled"]

        db.session.commit()
        _reload_contexts_everywhere()

        return jsonify({"success": True})

    @remote_desktop_bp.route("/remote-desktop/dashboard/api/contexts/<int:context_id>", methods=["DELETE"])
    @admins_only
    def admin_delete_context(context_id):
        from .models import DesktopContainerInfoModel, DesktopDockerContextModel, DesktopSessionOperationModel

        context = _lock_context_for_administration(DesktopDockerContextModel, context_id)
        if not context:
            db.session.rollback()
            return jsonify({"error": "context not found"}), 404

        if _context_has_live_work(context, DesktopContainerInfoModel, DesktopSessionOperationModel):
            db.session.rollback()
            return jsonify({"error": "context has active or in-flight sessions; drain it before deletion"}), 409

        if bool(context.enabled) and context.max_containers != 0:
            db.session.rollback()
            return jsonify({"error": "drain or disable the context before deletion"}), 409

        db.session.delete(context)
        db.session.commit()
        _reload_contexts_everywhere()

        return jsonify({"success": True})

    @remote_desktop_bp.route("/remote-desktop/dashboard/api/contexts/<int:context_id>/test", methods=["GET"])
    @admins_only
    def admin_test_context(context_id):
        from .models import DesktopDockerContextModel, get_setting

        context = DesktopDockerContextModel.query.get(context_id)
        if not context:
            return jsonify({"error": "context not found"}), 404

        ping_ok = container_manager.host_manager.ping(context.context_name)
        if not ping_ok:
            return jsonify({"error": "context unreachable (ping failed)"}), 503

        docker_image = str(get_setting("docker_image"))
        image_info = container_manager.host_manager.get_image_info(context.context_name, docker_image)
        if image_info is None:
            return jsonify({"error": f"image {docker_image} not found on context"}), 503
        if image_info.get("contract_status") != "compatible":
            found_contract = image_info.get("contract", "missing")
            return (
                jsonify(
                    {
                        "error": (
                            f"image {docker_image} has incompatible remote-desktop contract "
                            f"{found_contract!r}; expected {IMAGE_CONTRACT_VERSION!r}"
                        )
                    }
                ),
                503,
            )

        return jsonify({"success": True})

    @remote_desktop_bp.route("/remote-desktop/dashboard/api/contexts/reload", methods=["POST"])
    @admins_only
    def admin_reload_contexts():
        _reload_contexts_everywhere()
        return jsonify({"success": True})

    @remote_desktop_bp.route("/remote-desktop/dashboard/api/settings", methods=["GET"])
    @admins_only
    def admin_get_settings():
        from .models import SettingsValidationError, get_all_settings
        from .settings import RESTART_REQUIRED_SETTINGS

        try:
            settings = get_all_settings()
        except SettingsValidationError as exc:
            return jsonify({"error": f"stored settings are invalid: {exc}"}), 500
        return jsonify(
            {
                "settings": settings,
                "restart_required_settings": sorted(RESTART_REQUIRED_SETTINGS),
            }
        )

    @remote_desktop_bp.route("/remote-desktop/dashboard/api/settings", methods=["PUT"])
    @admins_only
    def admin_update_settings():
        from .models import SettingsValidationError, get_all_settings, set_settings
        from .settings import RESTART_REQUIRED_SETTINGS, parse_api_updates, validate_effective_settings

        try:
            updates = parse_api_updates(request.json)
            # preflight only, set_settings repeats it under the revision lock so concurrent writes cannot bypass
            effective = get_all_settings()
            effective.update(updates)
            validate_effective_settings(effective)
            set_settings(updates)
        except SettingsValidationError as exc:
            return jsonify({"error": str(exc)}), 400

        # image, resource, network, and connection settings change host eligibility and new container kwargs
        _reload_contexts_everywhere()
        restart_required = sorted(RESTART_REQUIRED_SETTINGS.intersection(updates))
        response: dict[str, object] = {"success": True}
        if restart_required:
            response["restart_required"] = restart_required
        return jsonify(response)

    @remote_desktop_bp.route("/remote-desktop/dashboard/api/events/stream")
    @admins_only
    def admin_events_stream():
        def event_stream():
            import queue

            event_queue: queue.Queue[EventDict] = queue.Queue(maxsize=100)

            def event_listener(event: EventDict) -> None:
                try:
                    event_queue.put_nowait(event)
                except queue.Full:
                    pass

            event_logger.add_listener(event_listener)

            try:
                try:
                    recent_events = get_persisted_events(limit=200)
                except Exception:
                    logger.warning(
                        "persisted event bootstrap unavailable; using this worker's live cache", exc_info=True
                    )
                    recent_events = event_logger.get_recent_events(limit=200)
                seen_order = deque(str(event.get("id")) for event in recent_events if event.get("id"))
                seen_ids = set(seen_order)
                for event in recent_events:
                    yield f"data: {json.dumps(event)}\n\n"

                while True:
                    try:
                        event = event_queue.get(timeout=30)
                        event_id = str(event.get("id")) if event.get("id") else ""
                        if event_id and event_id in seen_ids:
                            continue
                        if event_id:
                            seen_ids.add(event_id)
                            seen_order.append(event_id)
                            while len(seen_order) > 1000:
                                seen_ids.discard(seen_order.popleft())
                        yield f"data: {json.dumps(event)}\n\n"
                    except queue.Empty:
                        yield ": keepalive\n\n"

            finally:
                event_logger.remove_listener(event_listener)

        return Response(
            stream_with_context(event_stream()),
            mimetype="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

    @remote_desktop_bp.route("/remote-desktop/dashboard/api/events/recent")
    @admins_only
    def admin_get_recent_events():
        limit = min(request.args.get("limit", 100, type=int), 2000)
        try:
            events = get_persisted_events(limit=limit)
        except Exception:
            logger.exception("failed to read persisted event log")
            return jsonify({"error": "persistent event log unavailable"}), 503
        return jsonify({"events": events})

    return remote_desktop_bp
