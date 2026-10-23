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
MAX_COMMAND_FIELD_BYTES = 65535  # mariadb text columns store at most 65535 bytes
MAX_JOURNAL_READ_BYTES = 1024 * 1024
MAX_RECORDS_PER_POLL = 500
MAX_RECORD_BYTES_PER_POLL = 256 * 1024
MAX_RETAINED_ROWS_PER_USER = 20000
RECORDED_FIELD_BYTES = {"command": 4096, "tool": 256, "cwd": 1024, "tty": 128}
MAX_LINES_PER_POLL = 2000
MAX_CURSOR_OFFSET = 9223372036854775807
_READ_JOURNAL = r"""import base64,contextlib,json,os,socket,stat,sys
capture_available = False
try:
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as probe:
        probe.connect(sys.argv[3])
        capture_available = True
except OSError:
    pass
try:
    with contextlib.ExitStack() as streams:
        def open_journal(path):
            descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
            stream = streams.enter_context(os.fdopen(descriptor, 'rb'))
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise ValueError('command journal is not a regular file')
            return stream, info, f'{info.st_dev}:{info.st_ino}'
        active = open_journal(sys.argv[1])
        selected = active
        offset = int(sys.argv[2])
        known = sys.argv[4] if len(sys.argv) > 4 else ''
        identity, separator, stored_base = known.partition('@')
        base = int(stored_base) if separator else 0
        rotated = gap = False
        if identity and identity != active[2]:
            try:
                previous = open_journal(sys.argv[1] + '.1')
                if previous[2] == identity:
                    selected = previous
            except FileNotFoundError:
                pass
            if selected is active:
                base, rotated, gap = offset, True, True
        physical = offset - base
        if physical > selected[1].st_size or (selected is not active and physical == selected[1].st_size):
            gap = gap or physical > selected[1].st_size
            selected = active
            base, physical, rotated = offset, 0, True
        stream, info, identity = selected
        stream.seek(max(0, physical - 1))
        starts_mid_line = physical > 0 and stream.read(1) != b'\n'
        stream.seek(physical)
        chunk = stream.read(1024 * 1024)
        skipped = chunk.find(b'\n') + 1 if starts_mid_line else 0
        if starts_mid_line and not skipped:
            skipped = len(chunk)
        if selected is not active and chunk and b'\n' not in chunk and physical + len(chunk) == info.st_size:
            skipped, gap = len(chunk), True
        print(json.dumps({'capture_available': capture_available, 'identity': identity, 'base': base,
                          'size': base + info.st_size, 'rotated': rotated, 'gap': gap,
                          'skipped': skipped, 'data': base64.b64encode(chunk).decode('ascii')}))
except FileNotFoundError:
    print('null')
"""


def _tool(command: str) -> str:
    word = command.partition(" ")[0]
    if word and word.isascii() and word.isprintable() and not any(char in word for char in ("'", '"', "\\")):
        return word.rsplit("/", 1)[-1]

    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    if not tokens:
        return ""
    return tokens[0].rsplit("/", 1)[-1]


def normalize_command_record(line: bytes) -> dict | None:
    if len(line) > MAX_JOURNAL_READ_BYTES:
        return None
    try:
        data = json.loads(line)
    except (ValueError, UnicodeError, RecursionError):
        return None
    if not isinstance(data, dict):
        return None
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
        or (duration is not None and (type(duration) is not int or not 0 <= duration <= MAX_CURSOR_OFFSET))
        or not isinstance(command, str)
        or not command.strip()
        or not isinstance(cwd, str)
        or not isinstance(tty, str)
    ):
        return None
    try:
        if any(len(value.encode("utf-8")) > MAX_COMMAND_FIELD_BYTES for value in (command, cwd, tty)):
            return None
    except UnicodeEncodeError:
        return None
    return {
        "timestamp": timestamp,
        "command": command,
        "tool": _tool(command),
        "exit_code": exit_code,
        "duration_ms": duration,
        "cwd": cwd,
        "tty": tty,
    }


def _parse(payload: bytes, offset: int) -> tuple[list[dict], int]:
    records = []
    record_bytes = 0
    payload = payload[:MAX_JOURNAL_READ_BYTES]
    start = 0
    for _ in range(MAX_LINES_PER_POLL):
        end = payload.find(b"\n", start)
        if end < 0:
            if start == 0 and len(payload) == MAX_JOURNAL_READ_BYTES:
                offset += len(payload)  # the reader discards the rest of this oversized line on the next poll
            break
        record = normalize_command_record(payload[start:end])
        if record is not None:
            for field, limit in RECORDED_FIELD_BYTES.items():
                record[field] = record[field].encode("utf-8")[:limit].decode("utf-8", errors="ignore")
        size = sum(len(record[field].encode("utf-8")) for field in ("command", "tool", "cwd", "tty")) if record else 0
        if record_bytes + size > MAX_RECORD_BYTES_PER_POLL:
            break
        if record is not None:
            records.append({"byte_offset": offset, **record})
            record_bytes += size
        offset += end + 1 - start
        start = end + 1
        if len(records) == MAX_RECORDS_PER_POLL:
            break
    return records, offset


def _journal_result(output: str, offset: int) -> tuple[dict | None, bytes, int]:
    if not isinstance(output, str) or len(output) > MAX_JOURNAL_READ_BYTES * 2:
        raise ValueError("invalid command journal response size")
    result = json.loads(output)
    if result is None:
        return None, b"", 0
    if not isinstance(result, dict):
        raise ValueError("invalid command journal response")
    identity = result.get("identity")
    size = result.get("size")
    encoded = result.get("data")
    skipped = result.get("skipped", 0)
    base = result.get("base", 0)
    if (
        not isinstance(identity, str)
        or not 0 < len(identity) <= 128
        or not identity.isascii()
        or not identity.isprintable()
        or "@" in identity
        or type(base) is not int
        or not 0 <= base <= offset
        or len(f"{identity}@{base}") > 128
        or type(result.get("rotated", False)) is not bool
        or type(result.get("gap", False)) is not bool
        or (result.get("rotated") is True and base != offset)
        or type(size) is not int
        or not 0 <= size <= MAX_CURSOR_OFFSET
        or type(skipped) is not int
        or not 0 <= skipped <= MAX_JOURNAL_READ_BYTES
        or not isinstance(encoded, str)
        or len(encoded) > ((MAX_JOURNAL_READ_BYTES + 2) // 3) * 4
        or type(result.get("capture_available", False)) is not bool
    ):
        raise ValueError("invalid command journal response fields")
    payload = base64.b64decode(encoded, validate=True)
    if len(payload) > MAX_JOURNAL_READ_BYTES or skipped > len(payload) or offset + len(payload) > MAX_CURSOR_OFFSET:
        raise ValueError("command journal response exceeds read budget")
    if size >= offset and offset + len(payload) > size:
        raise ValueError("command journal response exceeds file size")
    return result, payload[skipped:], skipped


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
    if type(offset) is not int or not 0 <= offset <= MAX_CURSOR_OFFSET:
        raise RuntimeError("invalid command journal byte cursor")
    known_identity = cursor.journal_id or ""
    db.session.rollback()
    try:
        code, output = host_manager.exec_in_container(
            context,
            container_id,
            ["python3", "-c", _READ_JOURNAL, JOURNAL_PATH, str(offset), COLLECTOR_SOCKET_PATH, known_identity],
            max_output_bytes=2 * 1024 * 1024,
            timeout_seconds=3.0,
        )
        result, payload, skipped = _journal_result(output, offset) if code == 0 and output else (None, b"", 0)
        records, next_offset = _parse(payload, offset + skipped)
    except Exception:
        logger.warning("command journal read failed for session %s", session_uuid, exc_info=True)
        result = None
    cursor = DesktopCommandCursorModel.query.filter_by(session_uuid=session_uuid).with_for_update().first()
    if cursor is None or cursor.byte_offset != offset:
        db.session.rollback()
        return
    if result is None:
        if cursor.status != "discontinuous":
            cursor.status = "unavailable"
        db.session.commit()
        return
    journal_id = f"{result['identity']}@{result.get('base', 0)}"
    previous_id = cursor.journal_id
    identity_changed = previous_id is not None and previous_id not in (journal_id, result["identity"])
    if result["size"] < offset or (identity_changed and result.get("rotated") is not True):
        cursor.status = "discontinuous"
        db.session.commit()
        return
    db.session.add_all(
        DesktopRecordedCommandModel(user_id=user_id, session_uuid=session_uuid, **record) for record in records
    )
    if records:
        cutoff = (
            DesktopRecordedCommandModel.query.filter_by(user_id=user_id)
            .with_entities(DesktopRecordedCommandModel.id)
            .order_by(DesktopRecordedCommandModel.id.desc())
            .offset(MAX_RETAINED_ROWS_PER_USER - 1)
            .limit(1)
            .scalar()
        )
        if cutoff is not None:
            DesktopRecordedCommandModel.query.filter(
                DesktopRecordedCommandModel.user_id == user_id, DesktopRecordedCommandModel.id < cutoff
            ).delete(synchronize_session=False)
    cursor.byte_offset = next_offset
    cursor.journal_id = journal_id
    cursor.last_read_at = time.time()
    if cursor.status == "discontinuous" or result.get("gap") is True:
        cursor.status = "discontinuous"
    else:
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
