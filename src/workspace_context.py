from __future__ import annotations

import base64
import logging
import re
import threading
import time
import unicodedata

from CTFd.models import db

from .command_logs import JOURNAL_PATH, normalize_command_record
from .models import LIFECYCLE_ACTIVE, DesktopContainerInfoModel, get_setting

logger = logging.getLogger(__name__)

READ_BYTES = 65536
MAX_COMMANDS = 25
MAX_COMMAND_CHARS = 160
MAX_CONTEXT_CHARS = 8000

_READ_SLOTS = threading.BoundedSemaphore(2)
_host_manager = None

_READ_TAIL = """import base64,os,stat,sys
remaining = int(sys.argv[2])
chunks = []
seen = set()
for path in (sys.argv[1], sys.argv[1] + '.1'):
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        with os.fdopen(descriptor, 'rb') as stream:
            info = os.fstat(stream.fileno())
            identity = (info.st_dev, info.st_ino)
            if not stat.S_ISREG(info.st_mode) or identity in seen:
                continue
            seen.add(identity)
            take = min(info.st_size, remaining)
            start = info.st_size - take
            stream.seek(max(0, start - 1))
            data = stream.read(take + bool(start))
            remaining -= take
            if start:
                data = data[1:] if data[:1] == b'\\n' else data.partition(b'\\n')[2]
            chunks.insert(0, data[:data.rfind(b'\\n') + 1])
    except (OSError, ValueError):
        pass
    if remaining <= 0:
        break
print(base64.b64encode(b''.join(chunks)).decode('ascii'))
"""

_HEADER = "## recent shell commands (newest last)"
_REDACTED = "[redacted]"

_ANSI_CSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_ANSI_OSC_RE = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_FLAG_RE = re.compile(r"[A-Za-z0-9_]{2,32}\{[^{}\r\n]*\}")
_BLOB_RE = re.compile(r"[A-Za-z0-9+/=_-]{40,}")
_SHELL_WORD = r"""(?:[^\s"'\\]|\\.|"(?:\\.|[^"\\])*(?:"|$)|'[^']*(?:'|$))+"""
_SECRET_RE = re.compile(
    r"(?i)(?<![\w-])((?:--?(?:password|passwd|pass|token|api[-_]?key|secret|access[-_]?token)|-p)"
    r"(?:\s*=\s*|\s+)|(?:[A-Za-z_][A-Za-z0-9_]{0,63}_)?"
    r"(?:password|passwd|pwd|token|api[-_]?key|secret|access[-_]?token)\s*=\s*|sshpass\s+(?!-))" + _SHELL_WORD
)
_ATTACHED_PASSWORD_RE = re.compile(r"(?<![\w-])(-p)(?![\s=])" + _SHELL_WORD)
_BEARER_RE = re.compile(r"(?i)(\bBearer\s+)[^\s'\"]+")
_URL_PASSWORD_RE = re.compile(r"(://[^\s/:@]+:)[^\s/@]+(@)")
_FENCE_RE = re.compile(r"\[\s*/?\s*workspace context", re.IGNORECASE)


def install(host_manager) -> None:
    global _host_manager
    _host_manager = host_manager


def _active_row(user_id: int) -> DesktopContainerInfoModel | None:
    row = DesktopContainerInfoModel.query.filter_by(user_id=user_id).first()
    if row is None or row.lifecycle_state != LIFECYCLE_ACTIVE or row.paused_at is not None:
        return None
    return row


def context_available(user_id: int) -> bool:
    try:
        if _host_manager is None or not bool(get_setting("workspace_context_enabled")):
            return False
        row = _active_row(user_id)
        return row is not None and row.docker_context in _host_manager.get_connected_contexts()
    except Exception:
        logger.warning("workspace context availability check failed", exc_info=True)
        return False


def _sanitize(value: str) -> str:
    value = _ANSI_OSC_RE.sub("", value)
    value = _ANSI_CSI_RE.sub("", value)
    value = "".join(char for char in value if unicodedata.category(char) not in {"Cc", "Cf", "Zl", "Zp"})
    return _FENCE_RE.sub("(workspace context", value)


def _redact(value: str, *, cwd: bool = False) -> str:
    value = _FLAG_RE.sub(_REDACTED, _sanitize(value))
    value = _SECRET_RE.sub(lambda match: match[1] + _REDACTED, value)
    value = _ATTACHED_PASSWORD_RE.sub(lambda match: match[1] + _REDACTED, value)
    value = _BEARER_RE.sub(lambda match: match[1] + _REDACTED, value)
    value = _URL_PASSWORD_RE.sub(lambda match: match[1] + _REDACTED + match[2], value)
    if not cwd:
        value = _BLOB_RE.sub(_REDACTED, value)
    return value[:MAX_COMMAND_CHARS]


def _age(seconds: float) -> str:
    seconds = max(0.0, seconds)
    if seconds < 60:
        return f"{int(seconds)}s ago"
    if seconds < 3600:
        return f"{int(seconds // 60)}m ago"
    return f"{int(seconds // 3600)}h ago"


def _parse(payload: bytes) -> list[tuple[float, int, str, str]]:
    rows = []
    complete = payload[: payload.rfind(b"\n") + 1]
    for line in complete.split(b"\n")[:-1]:
        record = normalize_command_record(line)
        if record is None:
            continue
        command = _redact(record["command"])
        if command:
            rows.append((record["timestamp"], record["exit_code"], _redact(record["cwd"], cwd=True), command))
    return rows[-MAX_COMMANDS:]


def _render(rows: list[tuple[float, int, str, str]], limit: int) -> str:
    now = time.time()
    kept = []
    used = len(_HEADER)
    for epoch, code, cwd, command in reversed(rows):
        line = f"- {_age(now - epoch)}, exit {code}, {cwd}: {command}"
        used += len(line) + 1
        if used > limit:
            break
        kept.append(line)
    if not kept:
        return ""
    return "\n".join([_HEADER, *reversed(kept)])


def _identity(row) -> tuple:
    return row.user_id, row.session_uuid, row.docker_context, row.container_id


def read_workspace_context(user_id: int, *, max_chars: int = 4000) -> str:
    if not context_available(user_id) or not _READ_SLOTS.acquire(blocking=False):
        return ""
    try:
        host_manager = _host_manager
        row = _active_row(user_id)
        if row is None or host_manager is None:
            return ""
        identity = _identity(row)
        context_name, container_id = row.docker_context, row.container_id
        db.session.rollback()  # remote io must not hold a database transaction
        code, output = host_manager.exec_in_container(
            context_name,
            container_id,
            ["python3", "-c", _READ_TAIL, JOURNAL_PATH, str(READ_BYTES)],
            max_output_bytes=128 * 1024,
            timeout_seconds=3.0,
        )
        if code != 0 or not isinstance(output, str) or len(output) > ((READ_BYTES + 2) // 3) * 4 + 1:
            return ""
        payload = base64.b64decode(output.strip(), validate=True)
        if len(payload) > READ_BYTES:
            return ""
        db.session.expire_all()  # session identity must come from the database after remote io
        current = _active_row(user_id) if context_available(user_id) and host_manager is _host_manager else None
        matches = current is not None and _identity(current) == identity
        db.session.rollback()
        if not matches:
            return ""
        rows = _parse(payload)
        return _render(rows, min(max_chars, MAX_CONTEXT_CHARS)) if rows else ""
    except Exception:
        logger.warning("workspace context read failed", exc_info=True)
        return ""
    finally:
        _READ_SLOTS.release()
