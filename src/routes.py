import time
import datetime
import logging
import traceback
import json
from collections import defaultdict
from flask import Blueprint, request, jsonify, render_template, Response, stream_with_context
from CTFd.models import db, Users
from CTFd.utils.decorators import authed_only, admins_only
from CTFd.plugins import bypass_csrf_protection
from CTFd.utils.user import get_current_user, is_admin, is_verified
from .event_logger import event_logger
from .docker_host_manager import LOCAL_CONTEXT_NAME, discover_contexts, ping_endpoint

logger = logging.getLogger(__name__)


def create_routes(container_manager, orchestrator):
    remote_desktop_bp = Blueprint("remote_desktop", __name__, template_folder="templates")

    # user endpoints

    @remote_desktop_bp.route("/remote-desktop")
    @authed_only
    def remote_desktop_page():
        from .models import get_setting

        if not get_setting("remote_desktop_enabled", True):
            return render_template("remote_desktop.html", page_blocked="disabled")

        user = get_current_user()

        if not is_admin() and not is_verified():
            return render_template("remote_desktop.html", page_blocked="unverified")

        try:
            container_info = container_manager.get_container_info(user.id)
            creation_status = container_manager.get_creation_status(user.id)

            vnc_url = ""
            formatted_time = ""
            if container_info:
                vnc_url = container_info.get("vnc_url", "")
                created_timestamp = container_info["created_at"]
                created_dt = datetime.datetime.fromtimestamp(created_timestamp)
                formatted_time = created_dt.strftime("%B %d, %Y at %I:%M %p")

            template_container_info = None
            if container_info:
                template_container_info = {
                    "container_id": container_info["container_id"],
                    "container_name": container_info["container_name"],
                    "vnc_port": container_info["vnc_port"],
                    "novnc_port": container_info["novnc_port"],
                    "docker_context": container_info["docker_context"],
                    "created_at": container_info["created_at"],
                }

            return render_template(
                "remote_desktop.html",
                container_info=template_container_info,
                vnc_url=vnc_url,
                formatted_time=formatted_time,
                creation_status=creation_status,
            )
        except Exception as e:
            logger.error(f"error rendering remote desktop page: {e}")
            logger.error(traceback.format_exc())
            return f"Error loading remote desktop page: {str(e)}", 500

    @remote_desktop_bp.route("/remote-desktop/api/status", methods=["GET"])
    @authed_only
    def get_status():
        try:
            user = get_current_user()
            container_info = container_manager.get_container_info(user.id)

            if not container_info:
                return jsonify({"session": None})

            timer_status = container_manager.get_session_timer_status(user.id)

            if timer_status.get("expired"):
                container_manager.destroy_container(user.id, reason="expired")
                return jsonify({"session": None})

            if timer_status.get("success") and not timer_status.get("started"):
                container_manager.start_session_timer(user.id)
                timer_status = container_manager.get_session_timer_status(user.id)

            vnc_url = container_info.get("vnc_url", "")

            return jsonify(
                {
                    "session": {
                        "created_at": container_info["created_at"],
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
                }
            )
        except Exception as e:
            logger.error(f"API error getting status: {e}")
            return jsonify({"error": str(e)}), 500

    @remote_desktop_bp.route("/remote-desktop/api/create", methods=["POST"])
    @authed_only
    @bypass_csrf_protection
    def create_session():
        from .models import get_setting

        if not get_setting("remote_desktop_enabled", True):
            return jsonify({"error": "Remote Desktop is currently disabled"}), 403

        user = get_current_user()

        if not is_admin() and not is_verified():
            return jsonify({"error": "Email verification required"}), 403

        try:
            logger.info(f"create session request from user {user.name} (ID: {user.id})")

            if container_manager.get_container_info(user.id):
                event_logger.log_event(
                    "session_error",
                    "attempted to create session but already exists",
                    user_id=user.id,
                    username=user.name,
                    level="warning",
                )
                return jsonify({"error": "Session already exists"}), 400

            creation_status = container_manager.get_creation_status(user.id)
            if creation_status and creation_status.get("status") not in ["failed", "none"]:
                event_logger.log_event(
                    "session_error",
                    "attempted to create session but creation already in progress",
                    user_id=user.id,
                    username=user.name,
                    level="warning",
                )
                return jsonify({"error": "Session creation already in progress"}), 400

            result = container_manager.create_container(user.id)

            if not result.get("success"):
                return jsonify({"error": result.get("error", "Creation failed")}), 500

            return jsonify(
                {
                    "status": "creating",
                    "message": "Container creation started",
                }
            )
        except Exception as e:
            logger.error(f"API error creating session: {e}", exc_info=True)
            return jsonify({"error": str(e)}), 500

    @remote_desktop_bp.route("/remote-desktop/api/creation-status", methods=["GET"])
    @authed_only
    def get_creation_status():
        try:
            user = get_current_user()
            status = container_manager.get_creation_status(user.id)

            if not status:
                container_info = container_manager.get_container_info(user.id)
                if container_info:
                    container_manager.start_session_timer(user.id)
                    timer_status = container_manager.get_session_timer_status(user.id)
                    vnc_url = container_info.get("vnc_url", "")
                    return jsonify(
                        {
                            "status": "ready",
                            "message": "Desktop ready!",
                            "session": {
                                "created_at": container_info["created_at"],
                                "vnc_url": vnc_url,
                                "timer": {
                                    "active": timer_status.get("started", False),
                                    "time_remaining": timer_status.get("time_remaining", 0),
                                    "extensions_used": timer_status.get("extensions_used", 0),
                                    "max_extensions": timer_status.get("max_extensions", 3),
                                }
                                if timer_status.get("success")
                                else None,
                            },
                        }
                    )
                return jsonify({"status": "none"})

            if status.get("status") == "ready":
                container_manager.start_session_timer(user.id)
                container_info = container_manager.get_container_info(user.id)
                timer_status = container_manager.get_session_timer_status(user.id)
                vnc_url = container_info.get("vnc_url", "")

                return jsonify(
                    {
                        "status": "ready",
                        "message": status.get("message", "Desktop ready!"),
                        "session": {
                            "created_at": container_info["created_at"],
                            "vnc_url": vnc_url,
                            "timer": {
                                "active": timer_status.get("started", False),
                                "time_remaining": timer_status.get("time_remaining", 0),
                                "extensions_used": timer_status.get("extensions_used", 0),
                                "max_extensions": timer_status.get("max_extensions", 3),
                            }
                            if timer_status.get("success")
                            else None,
                        },
                    }
                )

            return jsonify(status)
        except Exception as e:
            logger.error(f"API error getting creation status: {e}")
            return jsonify({"error": str(e)}), 500

    @remote_desktop_bp.route("/remote-desktop/api/destroy", methods=["POST"])
    @authed_only
    @bypass_csrf_protection
    def destroy_session():
        try:
            user = get_current_user()

            if not container_manager.get_container_info(user.id):
                return jsonify({"error": "No active session"}), 400

            result = container_manager.destroy_container(user.id)
            if not result.get("success"):
                return jsonify({"error": result.get("error", "Destruction failed")}), 500

            return jsonify({"session": None})
        except Exception as e:
            logger.error(f"API error destroying session: {e}")
            return jsonify({"error": str(e)}), 500

    @remote_desktop_bp.route("/remote-desktop/api/extend", methods=["POST"])
    @authed_only
    @bypass_csrf_protection
    def extend_session():
        try:
            user = get_current_user()

            if not container_manager.get_container_info(user.id):
                return jsonify({"error": "No active session"}), 400

            result = container_manager.extend_session_timer(user.id)
            if not result.get("success"):
                return jsonify({"error": result.get("error", "Extension failed")}), 400

            timer_status = container_manager.get_session_timer_status(user.id)
            return jsonify(
                {
                    "timer": {
                        "active": timer_status.get("started", False),
                        "time_remaining": timer_status.get("time_remaining", 0),
                        "extensions_used": timer_status.get("extensions_used", 0),
                        "max_extensions": timer_status.get("max_extensions", 3),
                    }
                }
            )
        except Exception as e:
            logger.error(f"API error extending session: {e}")
            return jsonify({"error": str(e)}), 500

    @remote_desktop_bp.route("/remote-desktop/api/cleanup", methods=["POST"])
    @authed_only
    @bypass_csrf_protection
    def trigger_cleanup():
        try:
            get_current_user()
            if not is_admin():
                return jsonify({"error": "Admin access required"}), 403

            container_manager.periodic_cleanup()
            return jsonify({"success": True, "message": "Cleanup triggered"})
        except Exception as e:
            logger.error(f"API error triggering cleanup: {e}")
            return jsonify({"error": str(e)}), 500

    # admin dashboard

    @remote_desktop_bp.route("/remote-desktop/admin")
    @admins_only
    def admin_dashboard():
        return render_template("admin_dashboard.html")

    @remote_desktop_bp.route("/remote-desktop/admin/api/containers", methods=["GET"])
    @admins_only
    @bypass_csrf_protection
    def admin_get_containers():
        try:
            containers = container_manager.get_all_containers()
            return jsonify({"containers": containers})
        except Exception as e:
            logger.error(f"admin API error getting containers: {e}")
            return jsonify({"error": str(e)}), 500

    @remote_desktop_bp.route("/remote-desktop/admin/api/hosts", methods=["GET"])
    @admins_only
    @bypass_csrf_protection
    def admin_get_hosts():
        try:
            status = orchestrator.get_status()
            return jsonify({"hosts": status})
        except Exception as e:
            logger.error(f"admin API error getting hosts: {e}")
            return jsonify({"error": str(e)}), 500

    @remote_desktop_bp.route("/remote-desktop/admin/api/kill", methods=["POST"])
    @admins_only
    @bypass_csrf_protection
    def admin_kill_container():
        try:
            admin_user = get_current_user()
            user_id = request.form.get("user_id")
            if not user_id:
                return jsonify({"error": "user_id required"}), 400

            user_id = int(user_id)

            from CTFd.models import Users

            target_user = Users.query.filter_by(id=user_id).first()
            target_username = target_user.name if target_user else f"User {user_id}"

            event_logger.log_event(
                "admin_action",
                f"admin {admin_user.name} manually killed session for {target_username}",
                user_id=user_id,
                username=target_username,
                level="warning",
                metadata={"admin_id": admin_user.id, "admin_name": admin_user.name},
            )

            result = container_manager.destroy_container(user_id, reason="admin_killed")

            if result.get("success"):
                return jsonify({"success": True})
            else:
                return jsonify({"error": result.get("error", "Failed to kill container")}), 500

        except Exception as e:
            logger.error(f"admin API error killing container: {e}")
            return jsonify({"error": str(e)}), 500

    @remote_desktop_bp.route("/remote-desktop/admin/api/extend", methods=["POST"])
    @admins_only
    @bypass_csrf_protection
    def admin_extend_session():
        try:
            admin_user = get_current_user()
            user_id = request.form.get("user_id")
            if not user_id:
                return jsonify({"error": "user_id required"}), 400

            user_id = int(user_id)

            if not container_manager.get_container_info(user_id):
                return jsonify({"error": "No active session for user"}), 400

            from CTFd.models import Users

            target_user = Users.query.filter_by(id=user_id).first()
            target_username = target_user.name if target_user else f"User {user_id}"

            event_logger.log_event(
                "admin_action",
                f"admin {admin_user.name} extended session for {target_username}",
                user_id=user_id,
                username=target_username,
                level="info",
                metadata={"admin_id": admin_user.id, "admin_name": admin_user.name},
            )

            result = container_manager.extend_session_timer(user_id)

            if result.get("success"):
                return jsonify({"success": True})
            else:
                return jsonify({"error": result.get("error", "Failed to extend session")}), 400

        except Exception as e:
            logger.error(f"admin API error extending session: {e}")
            return jsonify({"error": str(e)}), 500

    # kill-all

    @remote_desktop_bp.route("/remote-desktop/admin/api/kill-all", methods=["POST"])
    @admins_only
    @bypass_csrf_protection
    def admin_kill_all():
        try:
            admin_user = get_current_user()
            killed = container_manager.destroy_all_containers_admin(admin_user)
            return jsonify({"success": True, "killed": killed})
        except Exception as e:
            logger.error(f"admin API error killing all containers: {e}")
            return jsonify({"error": str(e)}), 500

    # stats

    @remote_desktop_bp.route("/remote-desktop/admin/api/stats/top-users", methods=["GET"])
    @admins_only
    @bypass_csrf_protection
    def admin_stats_top_users():
        from .models import DesktopSessionHistoryModel

        try:
            period = request.args.get("period", "all")
            query = DesktopSessionHistoryModel.query

            if period == "week":
                cutoff = time.time() - 7 * 86400
                query = query.filter(DesktopSessionHistoryModel.started_at >= cutoff)
            elif period == "month":
                cutoff = time.time() - 30 * 86400
                query = query.filter(DesktopSessionHistoryModel.started_at >= cutoff)

            rows = query.all()

            user_stats = defaultdict(lambda: {"total_duration": 0.0, "session_count": 0, "username": ""})
            for row in rows:
                entry = user_stats[row.user_id]
                entry["total_duration"] += row.duration
                entry["session_count"] += 1
                entry["username"] = row.username

            users = sorted(user_stats.values(), key=lambda u: u["total_duration"], reverse=True)[:15]
            return jsonify({"users": users})
        except Exception as e:
            logger.error(f"admin API error getting top users: {e}")
            return jsonify({"error": str(e)}), 500

    @remote_desktop_bp.route("/remote-desktop/admin/api/stats/usage", methods=["GET"])
    @admins_only
    @bypass_csrf_protection
    def admin_stats_usage():
        from .models import DesktopSessionHistoryModel

        try:
            period = request.args.get("period", "all")
            query = DesktopSessionHistoryModel.query

            if period == "week":
                cutoff = time.time() - 7 * 86400
                query = query.filter(DesktopSessionHistoryModel.started_at >= cutoff)
            elif period == "month":
                cutoff = time.time() - 30 * 86400
                query = query.filter(DesktopSessionHistoryModel.started_at >= cutoff)

            rows = query.all()

            daily = defaultdict(lambda: {"sessions": 0, "total_duration": 0.0})
            for row in rows:
                date_str = datetime.datetime.fromtimestamp(row.started_at).strftime("%Y-%m-%d")
                daily[date_str]["sessions"] += 1
                daily[date_str]["total_duration"] += row.duration

            days = [{"date": d, **daily[d]} for d in sorted(daily.keys())]
            return jsonify({"days": days})
        except Exception as e:
            logger.error(f"admin API error getting usage stats: {e}")
            return jsonify({"error": str(e)}), 500

    @remote_desktop_bp.route("/remote-desktop/admin/api/stats/summary", methods=["GET"])
    @admins_only
    @bypass_csrf_protection
    def admin_stats_summary():
        from .models import DesktopSessionHistoryModel

        try:
            rows = DesktopSessionHistoryModel.query.all()
            total_sessions = len(rows)

            if total_sessions == 0:
                return jsonify({"total_sessions": 0, "avg_duration": 0, "peak_concurrent": 0})

            avg_duration = sum(r.duration for r in rows) / total_sessions

            # sweep-line algorithm for peak concurrent sessions
            events = []
            for r in rows:
                events.append((r.started_at, 1))
                events.append((r.ended_at, -1))
            events.sort(key=lambda e: (e[0], e[1]))

            peak = 0
            current = 0
            for _, delta in events:
                current += delta
                if current > peak:
                    peak = current

            return jsonify(
                {
                    "total_sessions": total_sessions,
                    "avg_duration": avg_duration,
                    "peak_concurrent": peak,
                }
            )
        except Exception as e:
            logger.error(f"admin API error getting summary stats: {e}")
            return jsonify({"error": str(e)}), 500

    # vnc proxy

    @remote_desktop_bp.route("/remote-desktop/vnc/auth", methods=["GET"])
    @authed_only
    @bypass_csrf_protection
    def vnc_auth():
        """nginx auth_request subrequest endpoint. Returns 200 with backend
        host/port in headers if the user is authorized, or 403/404 otherwise.
        nginx captures the headers and uses them to proxy to the container."""
        from .models import DesktopContainerInfoModel

        user_id = request.headers.get("X-VNC-User-ID")
        if not user_id:
            return "", 400

        user_id = int(user_id)
        current_user = get_current_user()
        if current_user.id != user_id and not is_admin():
            return "", 403

        row = DesktopContainerInfoModel.query.filter_by(user_id=user_id).first()
        if not row:
            return "", 404

        check_hostname = container_manager.host_manager.get_check_hostname(row.docker_context)
        if not check_hostname:
            return "", 502

        resp = Response("", 200)
        resp.headers["X-VNC-Host"] = check_hostname
        resp.headers["X-VNC-Port"] = str(row.novnc_port)
        return resp

    @remote_desktop_bp.route("/remote-desktop/admin/api/stats/per-host", methods=["GET"])
    @admins_only
    @bypass_csrf_protection
    def admin_stats_per_host():
        from .models import DesktopSessionHistoryModel

        try:
            period = request.args.get("period", "all")
            query = DesktopSessionHistoryModel.query

            if period == "week":
                query = query.filter(DesktopSessionHistoryModel.started_at >= time.time() - 7 * 86400)
            elif period == "month":
                query = query.filter(DesktopSessionHistoryModel.started_at >= time.time() - 30 * 86400)

            rows = query.all()

            hosts = defaultdict(lambda: {"sessions": 0, "total_duration": 0.0, "failures": 0})
            for row in rows:
                ctx = row.docker_context
                hosts[ctx]["sessions"] += 1
                hosts[ctx]["total_duration"] += row.duration
                if row.end_reason == "reconciliation":
                    hosts[ctx]["failures"] += 1

            result = [{"host": h, **hosts[h]} for h in sorted(hosts.keys())]
            return jsonify({"hosts": result})
        except Exception as e:
            logger.error(f"admin API error getting per-host stats: {e}")
            return jsonify({"error": str(e)}), 500

    @remote_desktop_bp.route("/remote-desktop/admin/api/stats/duration-distribution", methods=["GET"])
    @admins_only
    @bypass_csrf_protection
    def admin_stats_duration_dist():
        from .models import DesktopSessionHistoryModel

        try:
            period = request.args.get("period", "all")
            query = DesktopSessionHistoryModel.query

            if period == "week":
                query = query.filter(DesktopSessionHistoryModel.started_at >= time.time() - 7 * 86400)
            elif period == "month":
                query = query.filter(DesktopSessionHistoryModel.started_at >= time.time() - 30 * 86400)

            rows = query.all()

            # bucket durations into ranges
            buckets = {"<5m": 0, "5-15m": 0, "15-30m": 0, "30-60m": 0, "1-2h": 0, "2h+": 0}
            for row in rows:
                d = row.duration
                if d < 300:
                    buckets["<5m"] += 1
                elif d < 900:
                    buckets["5-15m"] += 1
                elif d < 1800:
                    buckets["15-30m"] += 1
                elif d < 3600:
                    buckets["30-60m"] += 1
                elif d < 7200:
                    buckets["1-2h"] += 1
                else:
                    buckets["2h+"] += 1

            result = [{"range": k, "count": v} for k, v in buckets.items()]
            return jsonify({"buckets": result})
        except Exception as e:
            logger.error(f"admin API error getting duration distribution: {e}")
            return jsonify({"error": str(e)}), 500

    @remote_desktop_bp.route("/remote-desktop/admin/api/stats/concurrent", methods=["GET"])
    @admins_only
    @bypass_csrf_protection
    def admin_stats_concurrent():
        from .models import DesktopSessionHistoryModel, DesktopContainerInfoModel

        try:
            period = request.args.get("period", "all")
            query = DesktopSessionHistoryModel.query

            if period == "week":
                query = query.filter(DesktopSessionHistoryModel.started_at >= time.time() - 7 * 86400)
            elif period == "month":
                query = query.filter(DesktopSessionHistoryModel.started_at >= time.time() - 30 * 86400)

            rows = query.all()

            # also include currently active sessions
            active = DesktopContainerInfoModel.query.all()
            now = time.time()

            # build time series by sampling at regular intervals
            all_sessions = []
            for r in rows:
                all_sessions.append((r.started_at, r.ended_at))
            for r in active:
                all_sessions.append((r.created_at, now))

            if not all_sessions:
                return jsonify({"points": []})

            min_t = min(s[0] for s in all_sessions)
            max_t = max(s[1] for s in all_sessions)

            # sample every 5 minutes, cap at 2000 points
            interval = max((max_t - min_t) / 2000, 300)
            points = []
            t = min_t
            while t <= max_t:
                count = sum(1 for s in all_sessions if s[0] <= t < s[1])
                date_str = datetime.datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M")
                points.append({"time": date_str, "concurrent": count})
                t += interval

            return jsonify({"points": points})
        except Exception as e:
            logger.error(f"admin API error getting concurrent stats: {e}")
            return jsonify({"error": str(e)}), 500

    @remote_desktop_bp.route("/remote-desktop/admin/api/stats/extensions", methods=["GET"])
    @admins_only
    @bypass_csrf_protection
    def admin_stats_extensions():
        from .models import DesktopSessionHistoryModel

        try:
            period = request.args.get("period", "all")
            query = DesktopSessionHistoryModel.query

            if period == "week":
                query = query.filter(DesktopSessionHistoryModel.started_at >= time.time() - 7 * 86400)
            elif period == "month":
                query = query.filter(DesktopSessionHistoryModel.started_at >= time.time() - 30 * 86400)

            rows = query.all()

            ext_counts = defaultdict(int)
            end_reasons = defaultdict(int)
            for row in rows:
                ext_counts[row.extensions_used] += 1
                end_reasons[row.end_reason] += 1

            return jsonify(
                {
                    "extensions": [{"count": k, "sessions": v} for k, v in sorted(ext_counts.items())],
                    "end_reasons": [
                        {"reason": k, "count": v} for k, v in sorted(end_reasons.items(), key=lambda x: -x[1])
                    ],
                }
            )
        except Exception as e:
            logger.error(f"admin API error getting extension stats: {e}")
            return jsonify({"error": str(e)}), 500

    # command logs

    @remote_desktop_bp.route("/remote-desktop/admin/api/command-logs", methods=["GET"])
    @admins_only
    @bypass_csrf_protection
    def admin_get_command_logs():
        from .models import CommandLogModel

        try:
            user_id = request.args.get("user_id", type=int)
            limit = min(request.args.get("limit", 200, type=int), 1000)
            offset = request.args.get("offset", 0, type=int)

            query = CommandLogModel.query
            if user_id:
                query = query.filter_by(user_id=user_id)

            total = query.count()
            logs = query.order_by(CommandLogModel.timestamp.desc()).offset(offset).limit(limit).all()

            return jsonify(
                {
                    "logs": [
                        {
                            "id": log.id,
                            "user_id": log.user_id,
                            "timestamp": log.timestamp,
                            "command": log.command,
                            "exit_code": log.exit_code,
                            "duration": log.duration,
                            "cwd": log.cwd,
                            "tty": log.tty,
                        }
                        for log in logs
                    ],
                    "total": total,
                }
            )
        except Exception as e:
            logger.error(f"admin API error getting command logs: {e}")
            return jsonify({"error": str(e)}), 500

    def _cmd_log_query(period=None):
        """Base query for command logs, excluding hidden users from aggregate stats."""
        from .models import CommandLogModel

        query = (
            CommandLogModel.query.join(Users, CommandLogModel.user_id == Users.id).filter(Users.hidden == False)  # noqa: E712
        )
        if period == "week":
            query = query.filter(CommandLogModel.timestamp >= time.time() - 7 * 86400)
        elif period == "month":
            query = query.filter(CommandLogModel.timestamp >= time.time() - 30 * 86400)
        return query

    @remote_desktop_bp.route("/remote-desktop/admin/api/command-logs/stats/per-user", methods=["GET"])
    @admins_only
    @bypass_csrf_protection
    def admin_command_stats_per_user():
        try:
            period = request.args.get("period", "all")
            rows = _cmd_log_query(period).all()

            user_stats = defaultdict(lambda: {"total": 0, "errors": 0, "commands": set(), "username": ""})

            user_map = {}
            for row in rows:
                if row.user_id not in user_map:
                    user = Users.query.filter_by(id=row.user_id).first()
                    user_map[row.user_id] = user.name if user else f"User {row.user_id}"

                entry = user_stats[row.user_id]
                entry["total"] += 1
                entry["username"] = user_map[row.user_id]
                if row.exit_code and row.exit_code != 0:
                    entry["errors"] += 1

                tool = row.command.strip().split()[0] if row.command.strip() else ""
                if tool.startswith("sudo") and len(row.command.strip().split()) > 1:
                    tool = row.command.strip().split()[1]
                entry["commands"].add(tool)

            users = []
            for uid, s in sorted(user_stats.items(), key=lambda x: x[1]["total"], reverse=True):
                users.append(
                    {
                        "user_id": uid,
                        "username": s["username"],
                        "total_commands": s["total"],
                        "error_count": s["errors"],
                        "error_rate": round(s["errors"] / s["total"] * 100, 1) if s["total"] else 0,
                        "unique_tools": len(s["commands"]),
                    }
                )

            return jsonify({"users": users})
        except Exception as e:
            logger.error(f"admin API error getting per-user command stats: {e}")
            return jsonify({"error": str(e)}), 500

    @remote_desktop_bp.route("/remote-desktop/admin/api/command-logs/stats/tools", methods=["GET"])
    @admins_only
    @bypass_csrf_protection
    def admin_command_stats_tools():
        try:
            period = request.args.get("period", "all")
            rows = _cmd_log_query(period).all()

            tool_counts = defaultdict(int)
            tool_errors = defaultdict(int)
            for row in rows:
                parts = row.command.strip().split()
                if not parts:
                    continue
                tool = parts[0]
                if tool == "sudo" and len(parts) > 1:
                    tool = parts[1]
                tool_counts[tool] += 1
                if row.exit_code and row.exit_code != 0:
                    tool_errors[tool] += 1

            tools = sorted(
                [{"tool": t, "count": c, "errors": tool_errors.get(t, 0)} for t, c in tool_counts.items()],
                key=lambda x: x["count"],
                reverse=True,
            )[:30]

            return jsonify({"tools": tools})
        except Exception as e:
            logger.error(f"admin API error getting tool stats: {e}")
            return jsonify({"error": str(e)}), 500

    @remote_desktop_bp.route("/remote-desktop/admin/api/command-logs/stats/activity", methods=["GET"])
    @admins_only
    @bypass_csrf_protection
    def admin_command_stats_activity():
        try:
            period = request.args.get("period", "all")
            user_id = request.args.get("user_id", type=int)
            query = _cmd_log_query(period)

            if user_id:
                query = query.filter_by(user_id=user_id)

            rows = query.all()

            hourly = defaultdict(int)
            for row in rows:
                hour_str = datetime.datetime.fromtimestamp(row.timestamp).strftime("%Y-%m-%d %H:00")
                hourly[hour_str] += 1

            points = [{"time": h, "commands": c} for h, c in sorted(hourly.items())]
            return jsonify({"points": points})
        except Exception as e:
            logger.error(f"admin API error getting command activity: {e}")
            return jsonify({"error": str(e)}), 500

    @remote_desktop_bp.route("/remote-desktop/admin/api/command-logs/stats/summary", methods=["GET"])
    @admins_only
    @bypass_csrf_protection
    def admin_command_stats_summary():
        from .models import CommandLogModel, get_setting

        try:
            enabled = get_setting("command_logging_enabled")
            total = (
                CommandLogModel.query.join(Users, CommandLogModel.user_id == Users.id)
                .filter(Users.hidden == False)  # noqa: E712
                .count()
            )
            unique_users = (
                (
                    db.session.query(CommandLogModel.user_id)
                    .join(Users, CommandLogModel.user_id == Users.id)
                    .filter(Users.hidden == False)  # noqa: E712
                    .distinct()
                    .count()
                )
                if total
                else 0
            )

            return jsonify(
                {
                    "enabled": enabled,
                    "total_commands": total,
                    "unique_users": unique_users,
                }
            )
        except Exception as e:
            logger.error(f"admin API error getting command log summary: {e}")
            return jsonify({"error": str(e)}), 500

    # context crud

    @remote_desktop_bp.route("/remote-desktop/admin/api/contexts", methods=["GET"])
    @admins_only
    @bypass_csrf_protection
    def admin_get_contexts():
        from .models import DesktopDockerContextModel

        try:
            contexts = DesktopDockerContextModel.query.all()
            connected = set(container_manager.host_manager.get_connected_contexts())
            data = []
            for ctx in contexts:
                data.append(
                    {
                        "id": ctx.id,
                        "context_name": ctx.context_name,
                        "hostname": ctx.hostname,
                        "pub_hostname": ctx.pub_hostname,
                        "weight": ctx.weight,
                        "enabled": ctx.enabled,
                        "connected": ctx.context_name in connected,
                        "is_local": ctx.context_name == LOCAL_CONTEXT_NAME,
                    }
                )
            from .docker_host_manager import LOCAL_SOCKET_PATH

            docker_ok = ping_endpoint(f"unix://{LOCAL_SOCKET_PATH}")
            return jsonify({"contexts": data, "docker_socket": docker_ok})
        except Exception as e:
            logger.error(f"admin API error getting contexts: {e}")
            return jsonify({"error": str(e)}), 500

    @remote_desktop_bp.route("/remote-desktop/admin/api/contexts/discover", methods=["GET"])
    @admins_only
    @bypass_csrf_protection
    def admin_discover_contexts():
        from concurrent.futures import ThreadPoolExecutor
        from .models import DesktopDockerContextModel
        from .docker_host_manager import _get_host_gateway

        try:
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
                        "name": ctx["name"],
                        "endpoint": ctx["endpoint"],
                        "suggested_hostname": suggested,
                    }
                )

            if available:

                def _ping(ctx):
                    ctx["reachable"] = ping_endpoint(ctx["endpoint"])
                    return ctx

                with ThreadPoolExecutor(max_workers=len(available)) as pool:
                    list(pool.map(_ping, available))

            return jsonify({"contexts": available})
        except Exception as e:
            logger.error(f"admin API error discovering contexts: {e}")
            return jsonify({"error": str(e)}), 500

    @remote_desktop_bp.route("/remote-desktop/admin/api/contexts", methods=["POST"])
    @admins_only
    @bypass_csrf_protection
    def admin_add_context():
        from .models import DesktopDockerContextModel
        from CTFd.models import db

        if not request.is_json:
            return jsonify({"error": "invalid request"}), 400

        context_name = request.json.get("context_name")
        hostname = request.json.get("hostname")
        pub_hostname = request.json.get("pub_hostname")
        weight = request.json.get("weight", 1)
        enabled = request.json.get("enabled", True)

        if not context_name:
            return jsonify({"error": "context_name is required"}), 400
        if not pub_hostname:
            return jsonify({"error": "pub_hostname is required"}), 400

        existing = DesktopDockerContextModel.query.filter_by(context_name=context_name).first()
        if existing:
            return jsonify({"error": "context already exists"}), 400

        try:
            weight = int(weight)
            if weight < 1:
                return jsonify({"error": "weight must be at least 1"}), 400
        except ValueError:
            return jsonify({"error": "weight must be an integer"}), 400

        new_context = DesktopDockerContextModel(
            context_name=context_name,
            hostname=hostname,
            pub_hostname=pub_hostname,
            weight=weight,
            enabled=enabled,
        )
        db.session.add(new_context)
        db.session.commit()

        orchestrator.load_from_db()

        return jsonify({"success": True, "id": new_context.id})

    @remote_desktop_bp.route("/remote-desktop/admin/api/contexts/<int:context_id>", methods=["PUT"])
    @admins_only
    @bypass_csrf_protection
    def admin_update_context(context_id):
        from .models import DesktopDockerContextModel
        from CTFd.models import db

        if not request.is_json:
            return jsonify({"error": "invalid request"}), 400

        context = DesktopDockerContextModel.query.get(context_id)
        if not context:
            return jsonify({"error": "context not found"}), 404

        if "hostname" in request.json:
            context.hostname = request.json["hostname"]

        if "pub_hostname" in request.json:
            if not request.json["pub_hostname"]:
                return jsonify({"error": "pub_hostname cannot be empty"}), 400
            context.pub_hostname = request.json["pub_hostname"]

        if "weight" in request.json:
            try:
                weight = int(request.json["weight"])
                if weight < 1:
                    return jsonify({"error": "weight must be at least 1"}), 400
                context.weight = weight
            except ValueError:
                return jsonify({"error": "weight must be an integer"}), 400

        if "enabled" in request.json:
            context.enabled = bool(request.json["enabled"])

        db.session.commit()
        orchestrator.load_from_db()

        return jsonify({"success": True})

    @remote_desktop_bp.route("/remote-desktop/admin/api/contexts/<int:context_id>", methods=["DELETE"])
    @admins_only
    @bypass_csrf_protection
    def admin_delete_context(context_id):
        from .models import DesktopDockerContextModel
        from CTFd.models import db

        context = DesktopDockerContextModel.query.get(context_id)
        if not context:
            return jsonify({"error": "context not found"}), 404

        db.session.delete(context)
        db.session.commit()
        orchestrator.load_from_db()

        return jsonify({"success": True})

    @remote_desktop_bp.route("/remote-desktop/admin/api/contexts/<int:context_id>/test", methods=["GET"])
    @admins_only
    @bypass_csrf_protection
    def admin_test_context(context_id):
        from .models import DesktopDockerContextModel, get_setting

        context = DesktopDockerContextModel.query.get(context_id)
        if not context:
            return jsonify({"error": "context not found"}), 404

        ping_ok = container_manager.host_manager.ping(context.context_name)
        if not ping_ok:
            return jsonify({"error": "context unreachable (ping failed)"}), 500

        docker_image = get_setting("docker_image")
        image_ok = container_manager.host_manager.check_image(context.context_name, docker_image)
        if not image_ok:
            return jsonify({"error": f"image {docker_image} not found on context"}), 500

        return jsonify({"success": True})

    @remote_desktop_bp.route("/remote-desktop/admin/api/contexts/reload", methods=["POST"])
    @admins_only
    @bypass_csrf_protection
    def admin_reload_contexts():
        try:
            orchestrator.load_from_db()
            return jsonify({"success": True})
        except Exception as e:
            logger.error(f"admin API error reloading contexts: {e}")
            return jsonify({"error": str(e)}), 500

    # settings

    @remote_desktop_bp.route("/remote-desktop/admin/api/settings", methods=["GET"])
    @admins_only
    @bypass_csrf_protection
    def admin_get_settings():
        from .models import get_all_settings

        try:
            settings = get_all_settings()
            return jsonify({"settings": settings})
        except Exception as e:
            logger.error(f"admin API error getting settings: {e}")
            return jsonify({"error": str(e)}), 500

    @remote_desktop_bp.route("/remote-desktop/admin/api/settings", methods=["PUT"])
    @admins_only
    @bypass_csrf_protection
    def admin_update_settings():
        from .models import set_setting

        if not request.is_json:
            return jsonify({"error": "invalid request"}), 400

        try:
            for key, value in request.json.items():
                set_setting(key, value)
            return jsonify({"success": True})
        except Exception as e:
            logger.error(f"admin API error updating settings: {e}")
            return jsonify({"error": str(e)}), 500

    # events sse

    @remote_desktop_bp.route("/remote-desktop/admin/api/events/stream")
    @admins_only
    def admin_events_stream():
        def event_stream():
            import queue

            event_queue = queue.Queue(maxsize=100)

            def event_listener(event):
                try:
                    event_queue.put_nowait(event)
                except queue.Full:
                    pass

            event_logger.add_listener(event_listener)

            try:
                recent_events = event_logger.get_recent_events(limit=200)
                for event in recent_events:
                    yield f"data: {json.dumps(event)}\n\n"

                while True:
                    try:
                        event = event_queue.get(timeout=30)
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

    @remote_desktop_bp.route("/remote-desktop/admin/api/events/recent")
    @admins_only
    def admin_get_recent_events():
        try:
            limit = request.args.get("limit", 100, type=int)
            events = event_logger.get_recent_events(limit=limit)
            return jsonify({"events": events})
        except Exception as e:
            logger.error(f"error getting recent events: {e}")
            return jsonify({"error": str(e)}), 500

    return remote_desktop_bp
