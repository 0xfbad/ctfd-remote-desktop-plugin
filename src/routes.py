from __future__ import annotations

import time
import datetime
import logging
import json
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Blueprint, request, jsonify, render_template, Response, stream_with_context
from CTFd.models import db, Users
from CTFd.utils.decorators import authed_only, admins_only, ratelimit
from CTFd.utils.user import get_current_user, is_admin, is_verified
from .container_manager import ContainerManager, ContainerInfoDict, TimerStatusDict
from .orchestrator import Orchestrator
from .event_logger import event_logger, EventDict
from .models import user_flags
from .docker_host_manager import LOCAL_CONTEXT_NAME, LOCAL_SOCKET_PATH, discover_contexts, ping_endpoint

logger = logging.getLogger(__name__)

UserInfoDict = dict[str, str | bool]
SessionDict = dict[str, float | str | dict[str, bool | int] | None]
TimerDisplayDict = dict[str, bool | int]


def _user_info(user: Users | None, fallback_id: int | None = None) -> UserInfoDict:  # type: ignore[type-arg]
    if not user:
        return {"username": f"User {fallback_id}"}
    return {"username": user.name, **user_flags(user)}


def _target_flags(user: Users | None) -> dict[str, bool]:  # type: ignore[type-arg]
    return {f"target_{k}": v for k, v in user_flags(user).items()}


def _direct_vnc_url(host: str, novnc_port: int, password: str) -> str:
    return f"http://{host}:{novnc_port}/vnc.html?autoconnect=true&password={password}&resize=remote&reconnect=true"


def create_routes(container_manager: ContainerManager, orchestrator: Orchestrator) -> Blueprint:
    remote_desktop_bp = Blueprint(
        "remote_desktop",
        __name__,
        template_folder="templates",
        static_folder="static",
        static_url_path="/remote-desktop/static",
    )

    def _timer_dict(timer_status: TimerStatusDict) -> TimerDisplayDict | None:
        if not timer_status.get("success"):
            return None
        return {
            "active": bool(timer_status.get("started", False)),
            "time_remaining": int(timer_status.get("time_remaining", 0)),
            "extensions_used": int(timer_status.get("extensions_used", 0)),
            "max_extensions": int(timer_status.get("max_extensions", 3)),
        }

    def _session_dict(container_info: ContainerInfoDict, timer_status: TimerStatusDict) -> SessionDict:
        return {
            "created_at": container_info["created_at"],
            "vnc_url": container_info.get("vnc_url", ""),
            "timer": _timer_dict(timer_status),
        }

    def _apply_period_filter(query: db.Query, column: db.Column, period: str | None) -> db.Query:  # type: ignore[name-defined]
        if period == "week":
            query = query.filter(column >= time.time() - 7 * 86400)
        elif period == "month":
            query = query.filter(column >= time.time() - 30 * 86400)
        return query

    def _extract_tool(cmd: str) -> str:
        """pull the primary tool name from a command string, skipping sudo"""
        parts = cmd.strip().split()
        if not parts:
            return ""
        tool = parts[0]
        if tool == "sudo" and len(parts) > 1:
            tool = parts[1]
        return tool

    @remote_desktop_bp.route("/remote-desktop")
    @authed_only
    def remote_desktop_page():
        from .models import get_setting

        if not get_setting("remote_desktop_enabled", True):
            return render_template("remote_desktop.html", page_blocked="disabled")

        user = get_current_user()

        if get_setting("require_verified") and not is_admin() and not is_verified():
            return render_template("remote_desktop.html", page_blocked="unverified")

        container_info = container_manager.get_container_info(user.id)
        creation_status = container_manager.get_creation_status(user.id)

        vnc_url = ""
        if container_info:
            vnc_url = container_info.get("vnc_url", "")

        template_container_info = None
        ssh_info = None
        terminal_url = ""
        if container_info:
            template_container_info = {
                "container_id": container_info["container_id"],
                "container_name": container_info["container_name"],
                "vnc_port": container_info["vnc_port"],
                "novnc_port": container_info["novnc_port"],
                "docker_context": container_info["docker_context"],
                "created_at": container_info["created_at"],
            }

            # no reverse proxy means nginx proxy paths won't work, use direct URLs
            behind_proxy = request.headers.get("X-Forwarded-For") or request.headers.get("X-Real-IP")
            if not behind_proxy:
                host = request.host.split(":")[0]
                vnc_url = _direct_vnc_url(host, container_info["novnc_port"], container_info["vnc_password"])
                if container_info.get("ttyd_port"):
                    terminal_url = f"http://{host}:{container_info['ttyd_port']}/"
            else:
                if container_info.get("ttyd_port"):
                    terminal_url = f"/remote-desktop/terminal/{user.id}/"

            # ssh is a direct connection to the container host, not proxied through CTFd
            if container_info.get("ssh_port"):
                ssh_info = {
                    "host": container_info["pub_hostname"],
                    "port": container_info["ssh_port"],
                    "username": container_info["container_username"],
                    "password": container_info["vnc_password"],
                }

        return render_template(
            "remote_desktop.html",
            container_info=template_container_info,
            vnc_url=vnc_url,
            terminal_url=terminal_url,
            creation_status=creation_status,
            ssh_info=ssh_info,
            max_extensions=get_setting("max_extensions"),
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
    @ratelimit(method="POST", limit=5, interval=300)
    def create_session():
        from .models import get_setting

        if not get_setting("remote_desktop_enabled", True):
            return jsonify({"error": "Remote Desktop is currently disabled"}), 403

        user = get_current_user()

        if get_setting("require_verified") and not is_admin() and not is_verified():
            return jsonify({"error": "Email verification required"}), 403

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
    def destroy_session():
        user = get_current_user()

        if not container_manager.get_container_info(user.id):
            return jsonify({"error": "No active session"}), 400

        result = container_manager.destroy_container(user.id)
        if not result.get("success"):
            return jsonify({"error": result.get("error", "Destruction failed")}), 500

        return jsonify({"session": None})

    @remote_desktop_bp.route("/remote-desktop/api/extend", methods=["POST"])
    @authed_only
    @ratelimit(method="POST", limit=10, interval=300)
    def extend_session():
        user = get_current_user()

        if not container_manager.get_container_info(user.id):
            return jsonify({"error": "No active session"}), 400

        result = container_manager.extend_session_timer(user.id)
        if not result.get("success"):
            return jsonify({"error": result.get("error", "Extension failed")}), 400

        timer_status = container_manager.get_session_timer_status(user.id)
        return jsonify({"timer": _timer_dict(timer_status)})

    @remote_desktop_bp.route("/remote-desktop/api/cleanup", methods=["POST"])
    @admins_only
    def trigger_cleanup():
        container_manager.periodic_cleanup()
        return jsonify({"success": True, "message": "Cleanup triggered"})

    @remote_desktop_bp.route("/remote-desktop/dashboard")
    @admins_only
    def admin_dashboard():
        return render_template("remote_desktop_dashboard.html")

    @remote_desktop_bp.route("/remote-desktop/dashboard/api/user-flags", methods=["GET"])
    @admins_only
    def admin_user_flags():
        rows = Users.query.all()
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
        behind_proxy = request.headers.get("X-Forwarded-For") or request.headers.get("X-Real-IP")
        if not behind_proxy:
            host = request.host.split(":")[0]
            for c in containers:
                if c.get("novnc_port"):
                    c["vnc_url"] = _direct_vnc_url(host, c["novnc_port"], c.get("vnc_password", ""))
        return jsonify({"containers": containers})

    @remote_desktop_bp.route("/remote-desktop/dashboard/api/hosts", methods=["GET"])
    @admins_only
    def admin_get_hosts():
        status = orchestrator.get_status()
        return jsonify({"hosts": status})

    @remote_desktop_bp.route("/remote-desktop/dashboard/api/kill", methods=["POST"])
    @admins_only
    def admin_kill_container():
        admin_user = get_current_user()
        user_id = request.form.get("user_id")
        if not user_id:
            return jsonify({"error": "user_id required"}), 400

        user_id = int(user_id)

        target_user = Users.query.filter_by(id=user_id).first()
        target_username = target_user.name if target_user else f"User {user_id}"

        event_logger.log_event(
            "admin_action",
            f"admin {admin_user.name} killed session for {target_username}",
            user_id=admin_user.id,
            username=admin_user.name,
            level="warning",
            metadata={
                "action": "kill",
                "target_id": user_id,
                "target": target_username,
                **_target_flags(target_user),
            },
        )

        result = container_manager.destroy_container(user_id, reason="admin_killed")

        if result.get("success"):
            return jsonify({"success": True})
        return jsonify({"error": result.get("error", "Failed to kill container")}), 500

    @remote_desktop_bp.route("/remote-desktop/dashboard/api/peek", methods=["POST"])
    @admins_only
    def admin_peek_session():
        admin_user = get_current_user()
        user_id = request.form.get("user_id")
        if not user_id:
            return jsonify({"error": "user_id required"}), 400

        user_id = int(user_id)
        target_user = Users.query.filter_by(id=user_id).first()
        target_username = target_user.name if target_user else f"User {user_id}"

        event_logger.log_event(
            "admin_action",
            f"admin {admin_user.name} viewing session for {target_username}",
            user_id=admin_user.id,
            username=admin_user.name,
            level="info",
            metadata={
                "action": "peek",
                "target_id": user_id,
                "target": target_username,
                **_target_flags(target_user),
            },
        )
        return jsonify({"success": True})

    @remote_desktop_bp.route("/remote-desktop/dashboard/api/extend", methods=["POST"])
    @admins_only
    def admin_extend_session():
        admin_user = get_current_user()
        user_id = request.form.get("user_id")
        if not user_id:
            return jsonify({"error": "user_id required"}), 400

        user_id = int(user_id)

        if not container_manager.get_container_info(user_id):
            return jsonify({"error": "No active session for user"}), 400

        target_user = Users.query.filter_by(id=user_id).first()
        target_username = target_user.name if target_user else f"User {user_id}"

        event_logger.log_event(
            "admin_action",
            f"admin {admin_user.name} extended session for {target_username}",
            user_id=admin_user.id,
            username=admin_user.name,
            level="info",
            metadata={
                "action": "extend",
                "target_id": user_id,
                "target": target_username,
                **_target_flags(target_user),
            },
        )

        result = container_manager.extend_session_timer(user_id)

        if result.get("success"):
            return jsonify({"success": True})
        return jsonify({"error": result.get("error", "Failed to extend session")}), 400

    @remote_desktop_bp.route("/remote-desktop/dashboard/api/kill-all", methods=["POST"])
    @admins_only
    def admin_kill_all():
        admin_user = get_current_user()
        killed = container_manager.destroy_all_containers_admin(admin_user)
        return jsonify({"success": True, "killed": killed})

    @remote_desktop_bp.route("/remote-desktop/dashboard/api/clear_history", methods=["POST"])
    @admins_only
    def admin_clear_history():
        from .models import DesktopSessionHistoryModel, CommandLogModel

        session_count = DesktopSessionHistoryModel.query.count()
        cmd_count = CommandLogModel.query.count()
        DesktopSessionHistoryModel.query.delete()
        CommandLogModel.query.delete()
        db.session.commit()

        admin_user = get_current_user()
        event_logger.log_event(
            "admin_action",
            f"cleared {session_count} sessions, {cmd_count} command logs",
            user_id=admin_user.id if admin_user else None,
            username=admin_user.name if admin_user else None,
            level="warning",
            metadata={"action": "clear_history", "sessions": session_count, "commands": cmd_count},
        )
        return jsonify({"success": True, "sessions": session_count, "commands": cmd_count})

    @remote_desktop_bp.route("/remote-desktop/dashboard/api/images/matrix", methods=["GET"])
    @admins_only
    def admin_images_matrix():
        from .models import get_all_settings, set_setting

        settings = get_all_settings()
        docker_image = settings.get("docker_image", "ctfd-remote-desktop:latest")
        display = docker_image.removesuffix(":latest")

        connected = container_manager.host_manager.get_connected_contexts()
        if not connected:
            return jsonify(images=[display], contexts=[], matrix={})

        matrix = {display: {}}

        def _info(ctx_name):
            return ctx_name, container_manager.host_manager.get_image_info(ctx_name, docker_image)

        with ThreadPoolExecutor(max_workers=min(len(connected), 8)) as pool:
            futures = {pool.submit(_info, ctx): ctx for ctx in connected}
            try:
                for future in as_completed(futures, timeout=15):
                    try:
                        ctx_name, info = future.result()
                        entry = {"available": info is not None}
                        if info:
                            entry["info"] = info
                        matrix[display][ctx_name] = entry
                    except Exception:
                        matrix[display][futures[future]] = {"available": False}
            except TimeoutError:
                for future, ctx in futures.items():
                    if ctx not in matrix[display]:
                        matrix[display][ctx] = {"available": False}

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
            cache = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
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

        user_stats = defaultdict(lambda: {"total_duration": 0.0, "session_count": 0, "username": ""})
        for row in rows:
            entry = user_stats[row.user_id]
            entry["total_duration"] += row.duration
            entry["session_count"] += 1
            entry["username"] = row.username

        top = sorted(user_stats.items(), key=lambda x: x[1]["total_duration"], reverse=True)[:15]
        users_by_id = {u.id: u for u in Users.query.filter(Users.id.in_([uid for uid, _ in top])).all()}
        users = []
        for uid, stats in top:
            entry = {"user_id": uid, **stats}
            u = users_by_id.get(uid)
            if u:
                entry.update(_user_info(u))
            users.append(entry)
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

        # sweep-line algorithm for peak concurrent sessions
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
        service: str, user_id_header: str, port_attr: str, host_header: str, port_header: str
    ) -> Response | tuple[str, int]:
        from .models import DesktopContainerInfoModel

        user_id = request.headers.get(user_id_header)
        if not user_id:
            return "", 400

        user_id = int(user_id)
        current_user = get_current_user()
        if current_user.id != user_id and not is_admin():
            return "", 403

        row = DesktopContainerInfoModel.query.filter_by(user_id=user_id).first()
        port = getattr(row, port_attr, None) if row else None
        if not row or port is None:
            return "", 404

        check_hostname = container_manager.host_manager.get_check_hostname(row.docker_context)
        if not check_hostname:
            return "", 502

        resp = Response("", 200)
        resp.headers[host_header] = check_hostname
        resp.headers[port_header] = str(port)
        return resp

    @remote_desktop_bp.route("/remote-desktop/vnc/auth", methods=["GET"])
    @authed_only
    def vnc_auth():
        return _proxy_auth("vnc", "X-VNC-User-ID", "novnc_port", "X-VNC-Host", "X-VNC-Port")

    @remote_desktop_bp.route("/remote-desktop/terminal/auth", methods=["GET"])
    @authed_only
    def terminal_auth():
        return _proxy_auth("terminal", "X-Terminal-User-ID", "ttyd_port", "X-Terminal-Host", "X-Terminal-Port")

    @remote_desktop_bp.route("/remote-desktop/dashboard/api/stats/per-host", methods=["GET"])
    @admins_only
    def admin_stats_per_host():
        period = request.args.get("period", "all")
        rows = _session_query(period).all()

        hosts = defaultdict(lambda: {"sessions": 0, "total_duration": 0.0, "failures": 0})
        for row in rows:
            ctx = row.docker_context
            hosts[ctx]["sessions"] += 1
            hosts[ctx]["total_duration"] += row.duration
            if row.end_reason == "reconciliation":
                hosts[ctx]["failures"] += 1

        result = [{"host": h, **hosts[h]} for h in sorted(hosts.keys())]
        return jsonify({"hosts": result})

    @remote_desktop_bp.route("/remote-desktop/dashboard/api/stats/heatmap", methods=["GET"])
    @admins_only
    def admin_stats_heatmap():
        period = request.args.get("period", "all")
        rows = _session_query(period).all()

        counts = [[0] * 7 for _ in range(24)]
        durations = [[0.0] * 7 for _ in range(24)]
        week_mode = period == "week"

        if week_mode:
            utc_now = datetime.datetime.utcnow()
            start_date = (utc_now - datetime.timedelta(days=6)).date()

        for r in rows:
            if not r.started_at:
                continue
            dt = datetime.datetime.utcfromtimestamp(r.started_at)
            if week_mode:
                day_idx = (dt.date() - start_date).days
                if not (0 <= day_idx < 7):
                    continue
            else:
                day_idx = dt.weekday()
            counts[dt.hour][day_idx] += 1
            durations[dt.hour][day_idx] += r.duration or 0

        data = []
        for hour in range(24):
            for day in range(7):
                if counts[hour][day] > 0:
                    data.append([day, hour, counts[hour][day], round(durations[hour][day] / 3600, 1)])

        result: dict = {"data": data}
        if week_mode:
            epoch = datetime.datetime(1970, 1, 1)
            result["start_ts"] = int((datetime.datetime.combine(start_date, datetime.time()) - epoch).total_seconds())
        else:
            result["days"] = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        return jsonify(result)

    @remote_desktop_bp.route("/remote-desktop/dashboard/api/stats/duration-distribution", methods=["GET"])
    @admins_only
    def admin_stats_duration_dist():
        from .models import get_setting

        period = request.args.get("period", "all")
        rows = _session_query(period).all()

        initial = get_setting("initial_duration")
        ext_dur = get_setting("extension_duration")
        max_ext = get_setting("max_extensions")

        def _fmt(s: int | float) -> str:
            if s < 3600:
                return f"{int(s // 60)}m"
            h = int(s // 3600)
            m = int((s % 3600) // 60)
            return f"{h}h{m}m" if m else f"{h}h"

        # 3 base buckets, then one per extension level
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

        ext_counts = defaultdict(int)
        end_reasons = defaultdict(int)
        for row in rows:
            ext_counts[row.extensions_used] += 1
            end_reasons[row.end_reason] += 1

        return jsonify(
            {
                "extensions": [{"count": k, "sessions": v} for k, v in sorted(ext_counts.items())],
                "end_reasons": [{"reason": k, "count": v} for k, v in sorted(end_reasons.items(), key=lambda x: -x[1])],
            }
        )

    @remote_desktop_bp.route("/remote-desktop/dashboard/api/command-logs", methods=["GET"])
    @admins_only
    def admin_get_command_logs():
        from .models import CommandLogModel

        user_id = request.args.get("user_id", type=int)
        limit = min(request.args.get("limit", 200, type=int), 1000)
        offset = request.args.get("offset", 0, type=int)

        query = CommandLogModel.query
        if user_id:
            query = query.filter_by(user_id=user_id)

        total = query.count()
        logs = query.order_by(CommandLogModel.timestamp.desc()).offset(offset).limit(limit).all()

        user_map = {}
        for log in logs:
            if log.user_id not in user_map:
                u = Users.query.filter_by(id=log.user_id).first()
                user_map[log.user_id] = _user_info(u, log.user_id)

        return jsonify(
            {
                "logs": [
                    {
                        "id": log.id,
                        "user_id": log.user_id,
                        **user_map.get(log.user_id, {"username": f"User {log.user_id}"}),
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

    def _session_query(period: str | None = None, limit: int = 10000) -> db.Query:  # type: ignore[name-defined]
        from .models import DesktopSessionHistoryModel

        query = DesktopSessionHistoryModel.query.join(Users, DesktopSessionHistoryModel.user_id == Users.id).filter(
            Users.hidden.is_(False)
        )
        return _apply_period_filter(query, DesktopSessionHistoryModel.started_at, period).limit(limit)

    def _cmd_log_query(period: str | None = None, limit: int = 10000) -> db.Query:  # type: ignore[name-defined]
        from .models import CommandLogModel

        query = CommandLogModel.query.join(Users, CommandLogModel.user_id == Users.id).filter(Users.hidden.is_(False))
        return _apply_period_filter(query, CommandLogModel.timestamp, period).limit(limit)

    @remote_desktop_bp.route("/remote-desktop/dashboard/api/command-logs/stats/per-user", methods=["GET"])
    @admins_only
    def admin_command_stats_per_user():
        period = request.args.get("period", "all")
        rows = _cmd_log_query(period).all()

        user_stats = defaultdict(lambda: {"total": 0, "sessions": set(), "commands": set(), "username": ""})

        user_map = {}
        for row in rows:
            if row.user_id not in user_map:
                user = Users.query.filter_by(id=row.user_id).first()
                user_map[row.user_id] = _user_info(user, row.user_id)

            entry = user_stats[row.user_id]
            entry["total"] += 1
            entry["user_info"] = user_map[row.user_id]
            entry["sessions"].add(row.container_id)

            entry["commands"].add(_extract_tool(row.command))

        users = []
        for uid, s in sorted(user_stats.items(), key=lambda x: x[1]["total"], reverse=True):
            users.append(
                {
                    "user_id": uid,
                    **s["user_info"],
                    "total_commands": s["total"],
                    "avg_per_session": round(s["total"] / len(s["sessions"]), 1) if s["sessions"] else 0,
                    "unique_tools": len(s["commands"]),
                }
            )

        return jsonify({"users": users})

    @remote_desktop_bp.route("/remote-desktop/dashboard/api/command-logs/stats/tools", methods=["GET"])
    @admins_only
    def admin_command_stats_tools():
        period = request.args.get("period", "all")
        rows = _cmd_log_query(period).all()

        tool_counts = defaultdict(int)
        tool_errors = defaultdict(int)
        for row in rows:
            tool = _extract_tool(row.command)
            if not tool:
                continue
            tool_counts[tool] += 1
            if row.exit_code and row.exit_code != 0:
                tool_errors[tool] += 1

        tools = sorted(
            [{"tool": t, "count": c, "errors": tool_errors.get(t, 0)} for t, c in tool_counts.items()],
            key=lambda x: x["count"],
            reverse=True,
        )[:30]

        return jsonify({"tools": tools})

    @remote_desktop_bp.route("/remote-desktop/dashboard/api/command-logs/stats/activity", methods=["GET"])
    @admins_only
    def admin_command_stats_activity():
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

        points = [
            {
                "time": datetime.datetime.strptime(h, "%Y-%m-%d %H:00").strftime("%b %-d, %Y %-I:00 %p"),
                "commands": c,
            }
            for h, c in sorted(hourly.items())
        ]
        return jsonify({"points": points})

    @remote_desktop_bp.route("/remote-desktop/dashboard/api/command-logs/stats/summary", methods=["GET"])
    @admins_only
    def admin_command_stats_summary():
        from .models import CommandLogModel, get_setting

        enabled = get_setting("command_logging_enabled")
        base = CommandLogModel.query.join(Users, CommandLogModel.user_id == Users.id).filter(Users.hidden.is_(False))
        total = base.count()

        unique_commands = 0
        unique_tools = 0
        if total:
            unique_commands = (
                db.session.query(CommandLogModel.command)
                .join(Users, CommandLogModel.user_id == Users.id)
                .filter(Users.hidden.is_(False))
                .distinct()
                .count()
            )

            rows = (
                db.session.query(CommandLogModel.command)
                .join(Users, CommandLogModel.user_id == Users.id)
                .filter(Users.hidden.is_(False))
                .distinct()
                .all()
            )
            unique_tools = len({_extract_tool(cmd) for (cmd,) in rows} - {""})

        return jsonify(
            {
                "enabled": enabled,
                "total_commands": total,
                "unique_tools": unique_tools,
                "unique_commands": unique_commands,
            }
        )

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
                    "context_name": ctx.context_name,
                    "hostname": ctx.hostname,
                    "pub_hostname": ctx.pub_hostname,
                    "weight": ctx.weight,
                    "enabled": ctx.enabled,
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

    @remote_desktop_bp.route("/remote-desktop/dashboard/api/contexts", methods=["POST"])
    @admins_only
    def admin_add_context():
        from .models import DesktopDockerContextModel

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

    @remote_desktop_bp.route("/remote-desktop/dashboard/api/contexts/<int:context_id>", methods=["PUT"])
    @admins_only
    def admin_update_context(context_id):
        from .models import DesktopDockerContextModel

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

    @remote_desktop_bp.route("/remote-desktop/dashboard/api/contexts/<int:context_id>", methods=["DELETE"])
    @admins_only
    def admin_delete_context(context_id):
        from .models import DesktopDockerContextModel

        context = DesktopDockerContextModel.query.get(context_id)
        if not context:
            return jsonify({"error": "context not found"}), 404

        db.session.delete(context)
        db.session.commit()
        orchestrator.load_from_db()

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
            return jsonify({"error": "context unreachable (ping failed)"}), 500

        docker_image = get_setting("docker_image")
        image_ok = container_manager.host_manager.check_image(context.context_name, docker_image)
        if not image_ok:
            return jsonify({"error": f"image {docker_image} not found on context"}), 500

        return jsonify({"success": True})

    @remote_desktop_bp.route("/remote-desktop/dashboard/api/contexts/reload", methods=["POST"])
    @admins_only
    def admin_reload_contexts():
        orchestrator.load_from_db()
        return jsonify({"success": True})

    @remote_desktop_bp.route("/remote-desktop/dashboard/api/settings", methods=["GET"])
    @admins_only
    def admin_get_settings():
        from .models import get_all_settings

        settings = get_all_settings()
        return jsonify({"settings": settings})

    @remote_desktop_bp.route("/remote-desktop/dashboard/api/settings", methods=["PUT"])
    @admins_only
    def admin_update_settings():
        from .models import set_setting, SETTING_DEFAULTS

        if not request.is_json:
            return jsonify({"error": "invalid request"}), 400

        for key, value in request.json.items():
            if key not in SETTING_DEFAULTS:
                continue
            set_setting(key, value)
        return jsonify({"success": True})

    @remote_desktop_bp.route("/remote-desktop/dashboard/api/events/stream")
    @admins_only
    def admin_events_stream():
        def event_stream():  # type: ignore[return]
            import queue

            event_queue: queue.Queue[EventDict] = queue.Queue(maxsize=100)

            def event_listener(event: EventDict) -> None:
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

    @remote_desktop_bp.route("/remote-desktop/dashboard/api/events/recent")
    @admins_only
    def admin_get_recent_events():
        limit = request.args.get("limit", 100, type=int)
        events = event_logger.get_recent_events(limit=limit)
        return jsonify({"events": events})

    return remote_desktop_bp
