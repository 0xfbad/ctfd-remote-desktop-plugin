"""recent shell commands from a live session, the only surface the ai tutor plugin touches.
best effort and silent by contract, a missing capture snippet, a paused container, or a dead host degrades to an empty string.
"""

from __future__ import annotations

import logging
import re
import threading
import time

from CTFd.models import db

from .models import LIFECYCLE_ACTIVE, DesktopContainerInfoModel, get_setting

logger = logging.getLogger(__name__)

WORKSPACE_LOG_PATH = "/var/lib/rd-workspace/commands.log"
READ_BYTES = 16384
MAX_COMMANDS = 25
MAX_COMMAND_CHARS = 160
MAX_CONTEXT_CHARS = 8000

_READ_SLOTS = threading.BoundedSemaphore(2)
# per gunicorn worker and set by install, preload is rejected so there is no shared parent state
_host_manager = None

_HEADER = "## recent shell commands (newest last)"
_REDACTED = "[redacted]"

_NUMERIC_RE = re.compile(r"-?\d{1,15}")
_ANSI_CSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_ANSI_OSC_RE = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_FLAG_RE = re.compile(r"[A-Za-z0-9_]{2,16}\{[^}]{0,120}\}")
_BLOB_RE = re.compile(r"[A-Za-z0-9+/=_-]{40,}")
_PASSWORD_RE = re.compile(r"(?<![\w-])(-p|--pass|--password|sshpass)(\s+)(\S+)")


def install(host_manager) -> None:
    global _host_manager
    _host_manager = host_manager


def _active_row(user_id: int) -> DesktopContainerInfoModel | None:
    row = DesktopContainerInfoModel.query.filter_by(user_id=user_id).first()
    if row is None or row.lifecycle_state != LIFECYCLE_ACTIVE or row.paused_at is not None:
        return None
    return row


def context_available(user_id: int) -> bool:
    """no docker io, safe to call on every state poll"""
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
    return _CONTROL_RE.sub("", value)[:MAX_COMMAND_CHARS]


def _redact(value: str) -> str:
    value = _FLAG_RE.sub(_REDACTED, value)
    value = _BLOB_RE.sub(_REDACTED, value)
    return _PASSWORD_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{_REDACTED}", value)


def _redact_cwd(value: str) -> str:
    # only the flag shape, the blob rule would swallow any path deeper than 40 characters
    return _FLAG_RE.sub(_REDACTED, value)


def _age(seconds: float) -> str:
    seconds = max(0.0, seconds)
    if seconds < 60:
        return f"{int(seconds)}s ago"
    if seconds < 3600:
        return f"{int(seconds // 60)}m ago"
    return f"{int(seconds // 3600)}h ago"


def _parse(payload: str) -> list[tuple[int, int, str, str]]:
    rows: list[tuple[int, int, str, str]] = []
    # the first element of a tail -c read can be a partial line
    for line in payload.split("\n")[1:]:
        fields = line.split("\t")
        if len(fields) != 5:
            continue
        if not all(_NUMERIC_RE.fullmatch(field) for field in fields[:3]):
            continue
        command = _redact(_sanitize(fields[4]))
        if not command:
            continue
        rows.append((int(fields[0]), int(fields[1]), _redact_cwd(_sanitize(fields[3])), command))
    return rows[-MAX_COMMANDS:]


def _render(rows: list[tuple[int, int, str, str]], limit: int) -> str:
    now = time.time()
    lines = [_HEADER]
    lines += [f"- {_age(now - epoch)}, exit {code}, {cwd}: {command}" for epoch, code, cwd, command in rows]
    block = "\n".join(lines)
    if len(block) <= limit:
        return block
    kept: list[str] = []
    used = 0
    for line in lines:
        used += len(line) + (1 if kept else 0)
        if used > limit:
            break
        kept.append(line)
    return "\n".join(kept) + "\n[truncated]" if kept else ""


def read_workspace_context(user_id: int, *, max_chars: int = 4000) -> str:
    if not context_available(user_id):
        return ""
    if not _READ_SLOTS.acquire(blocking=False):
        return ""
    try:
        host_manager = _host_manager
        row = _active_row(user_id)
        if row is None or host_manager is None:
            return ""
        context_name, container_id = row.docker_context, row.container_id
        db.session.rollback()  # end the implicit transaction opened by the reads above
        code, output = host_manager.exec_in_container(
            context_name,
            container_id,
            # an absent log from an old image or disabled capture exits 0 so only a real read failure warns
            [
                "/bin/sh",
                "-c",
                f"[ -r {WORKSPACE_LOG_PATH} ] || exit 0; tail -c {READ_BYTES} {WORKSPACE_LOG_PATH} 2>/dev/null",
            ],
        )
        if code == -1:
            # container gone or transport fault, indistinguishable by design
            return ""
        if code != 0:
            logger.warning("workspace context read on %s exited %s", context_name, code)
            return ""
        rows = _parse(output if isinstance(output, str) else "")
        if not rows:
            return ""
        return _render(rows, min(max_chars, MAX_CONTEXT_CHARS))
    except Exception:
        logger.warning("workspace context read failed", exc_info=True)
        return ""
    finally:
        _READ_SLOTS.release()
