from __future__ import annotations

import base64
import collections
import datetime
import json
import logging
import math
import shlex
import time
from collections.abc import Callable

from flask import Blueprint, jsonify, request
from sqlalchemy import case, func
from sqlalchemy.exc import IntegrityError

from CTFd.models import Users, db
from CTFd.utils.decorators import admins_only

from .models import (
    DesktopCommandCursorModel,
    DesktopContainerInfoModel,
    DesktopRecordedCommandModel,
    LIFECYCLE_ACTIVE,
    _esc,
    user_flags,
    username_or_fallback,
)

logger = logging.getLogger(__name__)

JOURNAL_PATH = "/var/log/.session-init/data.jsonl"
COLLECTOR_SOCKET_PATH = "/run/.session-init.sock"
_READ_JOURNAL = """import base64,json,os,socket,stat,sys
capture_available = False
try:
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as probe:
        probe.connect(sys.argv[3])
        capture_available = True
except OSError:
    pass
try:
    descriptor = os.open(sys.argv[1], os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(descriptor, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ValueError('command journal is not a regular file')
        offset = int(sys.argv[2])
        stream.seek(offset)
        print(json.dumps({'capture_available': capture_available, 'identity': f'{info.st_dev}:{info.st_ino}', 'size': info.st_size,
                          'data': base64.b64encode(stream.read(1024 * 1024)).decode('ascii')}))
except FileNotFoundError:
    print('null')
"""


def _tool(command: str) -> str:
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    if not tokens:
        return ""
    return tokens[0].rsplit("/", 1)[-1]


def _parse(payload: bytes, offset: int) -> tuple[list[dict], int]:
    records = []
    complete = payload[: payload.rfind(b"\n") + 1]
    for line in complete.split(b"\n")[:-1]:
        position = offset
        offset += len(line) + 1
        try:
            data = json.loads(line)
        except (ValueError, UnicodeError):
            continue
        if not isinstance(data, dict):
            continue
        timestamp = data.get("ts")
        exit_code = data.get("exit")
        duration = data.get("duration_ms")
        command = data.get("cmd")
        cwd, tty = data.get("cwd"), data.get("tty")
        if (
            not isinstance(timestamp, (int, float))
            or isinstance(timestamp, bool)
            or timestamp < 0
            or timestamp > 253402300799
            or not math.isfinite(timestamp)
            or type(exit_code) is not int
            or not 0 <= exit_code <= 255
            or (duration is not None and (type(duration) is not int or not 0 <= duration <= 9223372036854775807))
            or not isinstance(command, str)
            or not command.strip()
            or not isinstance(cwd, str)
            or not isinstance(tty, str)
        ):
            continue
        records.append(
            {
                "byte_offset": position,
                "timestamp": timestamp,
                "command": command,
                "tool": _tool(command),
                "exit_code": exit_code,
                "duration_ms": duration,
                "cwd": cwd,
                "tty": tty,
            }
        )
    return records, offset


def _cursor(session_uuid: str, user_id: int, container_id: str) -> DesktopCommandCursorModel:
    cursor = db.session.get(DesktopCommandCursorModel, session_uuid)
    if cursor is not None:
        return cursor
    db.session.add(DesktopCommandCursorModel(session_uuid=session_uuid, user_id=user_id, container_id=container_id))
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
    return db.session.get(DesktopCommandCursorModel, session_uuid)


def collect_session(host_manager, user_id: int, session_uuid: str, context: str, container_id: str) -> None:
    cursor = _cursor(session_uuid, user_id, container_id)
    if cursor is None or cursor.user_id != user_id or cursor.container_id != container_id:
        raise RuntimeError("command journal cursor identity mismatch")
    offset = cursor.byte_offset
    if type(offset) is not int or offset < 0:
        raise RuntimeError("invalid command journal byte cursor")
    if cursor.status == "discontinuous":
        db.session.rollback()
        return
    db.session.rollback()
    try:
        code, output = host_manager.exec_in_container(
            context, container_id, ["python3", "-c", _READ_JOURNAL, JOURNAL_PATH, str(offset), COLLECTOR_SOCKET_PATH]
        )
        result = json.loads(output) if code == 0 and output else None
        payload = base64.b64decode(result["data"], validate=True) if result is not None else b""
        records, next_offset = _parse(payload, offset)
    except Exception:
        logger.warning("command journal read failed for session %s", session_uuid, exc_info=True)
        result = None
    cursor = DesktopCommandCursorModel.query.filter_by(session_uuid=session_uuid).with_for_update().first()
    if cursor is None or cursor.byte_offset != offset:
        db.session.rollback()
        return
    if result is None:
        cursor.status = "unavailable"
        db.session.commit()
        return
    journal_id = result["identity"]
    if (
        result["size"] < offset
        or (cursor.journal_id is not None and cursor.journal_id != journal_id)
        or (len(payload) == 1024 * 1024 and b"\n" not in payload)
    ):
        cursor.status = "discontinuous"
        db.session.commit()
        return
    db.session.add_all(
        DesktopRecordedCommandModel(user_id=user_id, session_uuid=session_uuid, **record) for record in records
    )
    cursor.byte_offset = next_offset
    cursor.journal_id = journal_id
    cursor.last_read_at = time.time()
    cursor.status = "recording" if result.get("capture_available") is True else "unavailable"
    db.session.commit()


def collect_all(host_manager) -> None:
    rows = (
        DesktopContainerInfoModel.query.filter_by(lifecycle_state=LIFECYCLE_ACTIVE, paused_at=None)
        .with_entities(
            DesktopContainerInfoModel.user_id,
            DesktopContainerInfoModel.session_uuid,
            DesktopContainerInfoModel.docker_context,
            DesktopContainerInfoModel.container_id,
        )
        .all()
    )
    db.session.rollback()
    for user_id, session_uuid, context, container_id in rows:
        try:
            collect_session(host_manager, user_id, session_uuid, context, container_id)
        except Exception:
            db.session.rollback()
            logger.warning("command journal collection failed for session %s", session_uuid, exc_info=True)


def _query():
    query = DesktopRecordedCommandModel.query.join(Users, DesktopRecordedCommandModel.user_id == Users.id)
    query = query.filter(Users.hidden.is_(False))
    period = request.args.get("period", "all")
    days = {"week": 7, "month": 30}.get(period)
    if days is not None:
        query = query.filter(DesktopRecordedCommandModel.timestamp >= time.time() - days * 86400)
    return query


def register_routes(blueprint: Blueprint, request_timezone: Callable[[], datetime.tzinfo]) -> None:
    @blueprint.route("/remote-desktop/dashboard/api/command-logs")
    @admins_only
    def command_logs():
        query = _query()
        user_id = request.args.get("user_id", type=int)
        if user_id is not None:
            query = query.filter(DesktopRecordedCommandModel.user_id == user_id)
        offset = max(0, request.args.get("offset", 0, type=int))
        limit = max(1, min(request.args.get("limit", 50, type=int), 1000))
        total = query.count()
        rows = (
            query.with_entities(DesktopRecordedCommandModel, Users)
            .order_by(DesktopRecordedCommandModel.timestamp.desc(), DesktopRecordedCommandModel.id.desc())
            .offset(offset)
            .limit(limit)
            .all()
        )
        return jsonify(
            {
                "total": total,
                "logs": [
                    {
                        "id": row.id,
                        "user_id": row.user_id,
                        "username": _esc(user.name),
                        **user_flags(user),
                        "timestamp": row.timestamp,
                        "command": _esc(row.command),
                        "exit_code": row.exit_code,
                        "duration_ms": row.duration_ms,
                        "cwd": _esc(row.cwd),
                        "tty": _esc(row.tty),
                    }
                    for row, user in rows
                ],
            }
        )

    @blueprint.route("/remote-desktop/dashboard/api/command-logs/stats/summary")
    @admins_only
    def command_summary():
        query = _query()
        total = query.count()
        return jsonify(
            {
                "total_commands": total,
                "failed_commands": query.filter(DesktopRecordedCommandModel.exit_code != 0).count(),
                "unique_commands": query.with_entities(DesktopRecordedCommandModel.command).distinct().count(),
                "unique_tools": query.with_entities(DesktopRecordedCommandModel.tool).distinct().count(),
            }
        )

    @blueprint.route("/remote-desktop/dashboard/api/command-logs/stats/per-user")
    @admins_only
    def command_users():
        rows = (
            _query()
            .with_entities(
                DesktopRecordedCommandModel.user_id,
                func.count(DesktopRecordedCommandModel.id),
                func.count(DesktopRecordedCommandModel.session_uuid.distinct()),
                func.count(DesktopRecordedCommandModel.tool.distinct()),
            )
            .group_by(DesktopRecordedCommandModel.user_id)
            .order_by(func.count(DesktopRecordedCommandModel.id).desc())
            .all()
        )
        users = {user.id: user for user in Users.query.filter(Users.id.in_([row[0] for row in rows])).all()}
        return jsonify(
            {
                "users": [
                    {
                        "user_id": user_id,
                        "username": _esc(username_or_fallback(users.get(user_id), user_id)),
                        **user_flags(users.get(user_id)),
                        "total_commands": count,
                        "avg_per_session": round(count / sessions, 1),
                        "unique_tools": tools,
                    }
                    for user_id, count, sessions, tools in rows
                ]
            }
        )

    @blueprint.route("/remote-desktop/dashboard/api/command-logs/stats/tools")
    @admins_only
    def command_tools():
        rows = (
            _query()
            .with_entities(
                DesktopRecordedCommandModel.tool,
                func.count(DesktopRecordedCommandModel.id),
                func.sum(case((DesktopRecordedCommandModel.exit_code != 0, 1), else_=0)),
            )
            .group_by(DesktopRecordedCommandModel.tool)
            .order_by(func.count(DesktopRecordedCommandModel.id).desc())
            .all()
        )
        return jsonify(
            {"tools": [{"tool": _esc(tool), "count": count, "errors": int(errors)} for tool, count, errors in rows]}
        )

    @blueprint.route("/remote-desktop/dashboard/api/command-logs/stats/heatmap")
    @admins_only
    def command_heatmap():
        timezone = request_timezone()
        counts: collections.Counter[tuple[int, int]] = collections.Counter()
        for (timestamp,) in _query().with_entities(DesktopRecordedCommandModel.timestamp).yield_per(1000):
            try:
                date = datetime.datetime.fromtimestamp(timestamp, tz=timezone)
            except (OverflowError, OSError, ValueError):
                continue
            counts[date.weekday(), date.hour] += 1
        return jsonify(
            {
                "data": [[day, hour, count] for (day, hour), count in sorted(counts.items())],
                "days": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
            }
        )
