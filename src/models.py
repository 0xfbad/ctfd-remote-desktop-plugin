from __future__ import annotations

from CTFd.models import db, Users
from markupsafe import escape as _markup_escape
from sqlalchemy.exc import IntegrityError

from .settings import (
    INTERNAL_SETTING_KEYS,
    PUBLIC_SETTING_KEYS,
    SETTING_DEFAULTS,
    SETTING_SPECS,
    SettingValue,
    SettingsValidationError,
    decode_stored_setting,
    serialize_setting,
    validate_effective_settings,
    validate_setting_value,
)

# end_reason values persisted to the desktop_session_history.end_reason column.
# these strings live in the DB, the names exist only to prevent typo drift at call sites
END_REASON_RECONCILIATION = "reconciliation"
END_REASON_USER_DESTROYED = "user_destroyed"
END_REASON_ADMIN_KILLED = "admin_killed"
END_REASON_EXPIRED = "expired"

# Durable lifecycle states.  The active session row remains authoritative until
# Docker teardown is confirmed; non-active states are deliberately retained so
# recovery never has to reconstruct a session from an incomplete operation row.
LIFECYCLE_ACTIVE = "active"
LIFECYCLE_STOPPING = "stopping"
LIFECYCLE_CLEANUP_PENDING = "cleanup_pending"
LIFECYCLE_HELD = "held"
LIFECYCLE_UNPAUSING = "unpausing"

OP_IDLE = "idle"
OP_QUEUED = "queued"
OP_SELECTING = "selecting"
OP_RESERVED = "reserved"
OP_CREATING = "creating"
OP_WAITING_READY = "waiting_ready"
OP_ACTIVE = "active"
OP_CANCEL_REQUESTED = "cancel_requested"
OP_STOPPING = "stopping"
OP_CLEANUP_PENDING = "cleanup_pending"
OP_HELD = "held"
OP_UNPAUSING = "unpausing"
OP_FAILED = "failed"

CREATE_OPERATION_STATES = frozenset(
    {
        OP_QUEUED,
        OP_SELECTING,
        OP_RESERVED,
        OP_CREATING,
        OP_WAITING_READY,
        OP_CANCEL_REQUESTED,
    }
)

# noVNC viewer query string shared by the absolute and relative vnc.html URL builders
VNC_VIEWER_QUERY = "autoconnect=true&resize=remote&reconnect=true"


def proxy_vnc_url(user_id: int, password: str) -> str:
    # Password stays in the fragment: browsers do not send it in HTTP requests
    # or Referer headers. noVNC and ttyd are only exposed through authenticated
    # same-origin reverse-proxy routes.
    return f"/remote-desktop/vnc/{user_id}/vnc.html?{VNC_VIEWER_QUERY}#password={password}"


# strftime format for human-facing timestamps (event log datetime, image build date).
# %-d / %-I are glibc-specific no-pad directives, fine on the linux deploy target
DISPLAY_DATETIME_FORMAT = "%b %-d, %Y %-I:%M:%S %p"


def _esc(val: str | None) -> str:
    """html-escape a string for safe embedding in JSON / innerHTML contexts"""
    return str(_markup_escape(val)) if val else ""


def username_or_fallback(user: Users | None, user_id: int) -> str:
    """display name for a user, falling back to "User {id}" when the row is gone"""
    return user.name if user else f"User {user_id}"


class DesktopDockerContextModel(db.Model):
    __tablename__ = "desktop_docker_contexts"
    id = db.Column(db.Integer, primary_key=True)
    context_name = db.Column(db.String(512), unique=True, nullable=False)
    hostname = db.Column(db.String(512), nullable=True)
    pub_hostname = db.Column(db.String(512), nullable=False)
    weight = db.Column(db.Integer, default=1)
    enabled = db.Column(db.Boolean, default=True)
    # admission control: NULL = auto-derive from host RAM, 0 = drain, N = explicit cap
    max_containers = db.Column(db.Integer, nullable=True)
    # authoritative concurrent-session counter, shared across gunicorn workers.
    # incremented by the atomic conditional reserve, decremented on release,
    # absolute-synced by the leader reconcile, healed by the leader audit
    active_sessions = db.Column(db.Integer, nullable=False, default=0, server_default="0")


class DesktopContainerInfoModel(db.Model):
    __tablename__ = "desktop_container_info"
    container_id = db.Column(db.String(512), primary_key=True)
    # Intentionally not a Users FK: deleting an account must not erase the
    # authoritative row for a live, held, or cleanup-pending Docker object.
    user_id = db.Column(db.Integer, nullable=False)
    container_name = db.Column(db.String(512), nullable=False)
    vnc_port = db.Column(db.Integer, nullable=False)
    novnc_port = db.Column(db.Integer, nullable=False)
    ssh_port = db.Column(db.Integer, nullable=True)
    ttyd_port = db.Column(db.Integer, nullable=True)
    vnc_password = db.Column(db.String(256), nullable=False)
    vnc_url = db.Column(db.Text, nullable=False)
    docker_context = db.Column(db.String(512), nullable=False)
    pub_hostname = db.Column(db.String(512), nullable=False)
    container_username = db.Column(db.String(64), nullable=True)
    created_at = db.Column(db.Float(precision=53), nullable=False)
    timer_started = db.Column(db.Boolean, default=False)
    timer_start_time = db.Column(db.Float(precision=53), nullable=True)
    timer_duration = db.Column(db.Float(precision=53), default=0)
    extensions_used = db.Column(db.Integer, default=0)
    max_extensions = db.Column(db.Integer, default=3)
    # raw sid of the CTFd session minted for autologin into the container.
    # A cookie may not be minted in every session creation path. On destroy,
    # NULL means there is no server-side session cache entry to revoke.
    cookie_sid = db.Column(db.String(128), nullable=True)
    # set when the container is paused (io tripwire or admin hold); expiry and
    # shutdown cleanup skip paused rows so the writable layer survives as evidence
    paused_at = db.Column(db.Float(precision=53), nullable=True)
    # Generated before any Docker side effect. Unlike container_id this exists
    # throughout create, cancellation, active use, teardown, and history.
    session_uuid = db.Column(db.String(36), nullable=False)
    lifecycle_state = db.Column(
        db.String(32), nullable=False, default=LIFECYCLE_ACTIVE, server_default=LIFECYCLE_ACTIVE
    )
    lifecycle_reason = db.Column(db.String(128), nullable=True)

    __table_args__ = (
        db.UniqueConstraint("user_id", name="uq_desktop_container_info_user_id"),
        db.UniqueConstraint("session_uuid", name="uq_desktop_container_info_session_uuid"),
    )


class DesktopSessionOperationModel(db.Model):
    """Stable per-user lifecycle mutex and crash-recovery record.

    There is intentionally no Users foreign key and no cascading delete. A
    deleted CTFd user can still own a live or cleanup-pending Docker object, and
    recovery must remain able to lock and finish that generation.
    """

    __tablename__ = "desktop_session_operations"
    user_id = db.Column(db.Integer, primary_key=True)
    operation_uuid = db.Column(db.String(36), nullable=False)
    worker_lease_uuid = db.Column(db.String(36), nullable=True)
    session_uuid = db.Column(db.String(36), nullable=True)
    state = db.Column(db.String(32), nullable=False, default=OP_IDLE, server_default=OP_IDLE, index=True)
    cancel_requested = db.Column(db.Boolean, nullable=False, default=False, server_default="0")
    docker_context = db.Column(db.String(512), nullable=True)
    container_name = db.Column(db.String(512), nullable=True)
    capacity_reserved = db.Column(db.Boolean, nullable=False, default=False, server_default="0")
    requested_reason = db.Column(db.String(128), nullable=True)
    created_at = db.Column(db.Float(precision=53), nullable=False)
    updated_at = db.Column(db.Float(precision=53), nullable=False, index=True)
    heartbeat_at = db.Column(db.Float(precision=53), nullable=True)
    error = db.Column(db.Text, nullable=True)

    __table_args__ = (
        db.UniqueConstraint("operation_uuid", name="uq_desktop_session_operations_operation_uuid"),
        db.UniqueConstraint("session_uuid", name="uq_desktop_session_operations_session_uuid"),
    )


class DesktopSessionHistoryModel(db.Model):
    __tablename__ = "desktop_session_history"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, nullable=False)
    username = db.Column(db.String(512), nullable=False)
    docker_context = db.Column(db.String(512), nullable=False)
    started_at = db.Column(db.Float(precision=53), nullable=False)
    ended_at = db.Column(db.Float(precision=53), nullable=False)
    duration = db.Column(db.Float(precision=53), nullable=False)
    end_reason = db.Column(db.String(128), nullable=False)
    extensions_used = db.Column(db.Integer, default=0)
    container_name = db.Column(db.String(512), nullable=True)
    session_uuid = db.Column(db.String(36), nullable=False)

    __table_args__ = (db.UniqueConstraint("session_uuid", name="uq_desktop_session_history_session_uuid"),)


def history_from_row(
    row: DesktopContainerInfoModel,
    username: str,
    ended_at: float,
    reason: str,
) -> DesktopSessionHistoryModel:
    """build a history row from a live container row, snapshotting the session.
    ended_at is passed in so the caller controls the teardown timestamp"""
    return DesktopSessionHistoryModel(
        user_id=row.user_id,
        username=username,
        docker_context=row.docker_context,
        started_at=row.created_at,
        ended_at=ended_at,
        duration=ended_at - row.created_at,
        end_reason=reason,
        extensions_used=row.extensions_used,
        container_name=row.container_name,
        session_uuid=row.session_uuid,
    )


class DesktopReportModel(db.Model):
    __tablename__ = "desktop_reports"
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    username = db.Column(db.String(512), nullable=False)
    timestamp = db.Column(db.Float(precision=53), nullable=False)
    content = db.Column(db.Text, nullable=False)


class DesktopSettingsModel(db.Model):
    __tablename__ = "desktop_settings"
    key = db.Column(db.String(512), primary_key=True)
    value = db.Column(db.Text)


class DesktopEventLogModel(db.Model):
    __tablename__ = "desktop_event_log"
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    event_id = db.Column(db.String(128), nullable=False)
    # no FK on user_id, deleting a user should not cascade-wipe their audit trail
    timestamp = db.Column(db.Float(precision=53), nullable=False, index=True)
    event_type = db.Column(db.String(128), nullable=False, index=True)
    level = db.Column(db.String(16), nullable=False)
    user_id = db.Column(db.Integer, nullable=True)
    username = db.Column(db.String(512), nullable=True)
    message = db.Column(db.Text, nullable=False)
    metadata_json = db.Column(db.Text, nullable=True)

    __table_args__ = (db.UniqueConstraint("event_id", name="uq_desktop_event_log_event_id"),)


def get_setting(key: str, default: SettingValue | None = None) -> SettingValue:
    if key not in SETTING_SPECS:
        raise SettingsValidationError(f"unknown setting {key!r}")
    row = DesktopSettingsModel.query.filter_by(key=key).first()
    if row is not None:
        return decode_stored_setting(key, row.value)
    fallback = SETTING_SPECS[key].default if default is None else default
    return validate_setting_value(key, fallback)


def _lock_revision_row():
    return DesktopSettingsModel.query.filter_by(key="_settings_revision").with_for_update().first()


def _ensure_revision_row() -> None:
    if DesktopSettingsModel.query.filter_by(key="_settings_revision").first() is not None:
        return
    db.session.add(DesktopSettingsModel(key="_settings_revision", value="1"))
    try:
        db.session.commit()
    except IntegrityError:
        # Another worker won the one-time seed race.
        db.session.rollback()


def _decode_rows(rows) -> tuple[dict[str, SettingValue], dict[str, DesktopSettingsModel]]:
    effective = dict(SETTING_DEFAULTS)
    by_key: dict[str, DesktopSettingsModel] = {}
    for row in rows:
        key = str(row.key)
        if key in by_key:
            raise SettingsValidationError(f"duplicate persisted setting {key!r}")
        if key not in SETTING_SPECS:
            raise SettingsValidationError(f"unknown persisted setting {key!r}")
        by_key[key] = row
        decoded = decode_stored_setting(key, row.value)
        if key in PUBLIC_SETTING_KEYS:
            effective[key] = decoded
    return validate_effective_settings(effective), by_key


def _locked_profile() -> tuple[
    dict[str, SettingValue],
    dict[str, DesktopSettingsModel],
    DesktopSettingsModel,
]:
    revision = _lock_revision_row()
    if revision is None:
        raise SettingsValidationError("settings revision row is missing")
    rows = DesktopSettingsModel.query.order_by(DesktopSettingsModel.key).all()
    effective, by_key = _decode_rows(rows)
    return effective, by_key, revision


def initialize_settings() -> None:
    """Seed, strictly validate, and canonically rewrite settings at startup."""
    _ensure_revision_row()
    try:
        effective, by_key, revision = _locked_profile()
        for key, value in effective.items():
            canonical = serialize_setting(key, value)
            row = by_key.get(key)
            if row is None:
                db.session.add(DesktopSettingsModel(key=key, value=canonical))
            else:
                row.value = canonical
        # Canonicalize registered internal values without exposing them.
        for key in INTERNAL_SETTING_KEYS:
            row = by_key.get(key)
            if row is not None:
                row.value = serialize_setting(key, decode_stored_setting(key, row.value))
        revision.value = serialize_setting(
            "_settings_revision", decode_stored_setting("_settings_revision", revision.value)
        )
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise


def set_setting(key: str, value: SettingValue) -> None:
    """Persist an explicitly classified internal setting."""
    if key != "image_cache":
        raise SettingsValidationError(f"setting {key!r} is not writable through the internal settings path")
    parsed = validate_setting_value(key, value)
    _ensure_revision_row()
    try:
        _effective, by_key, revision = _locked_profile()
        row = by_key.get(key)
        if row is None:
            db.session.add(DesktopSettingsModel(key=key, value=serialize_setting(key, parsed)))
        else:
            row.value = serialize_setting(key, parsed)
        revision_number = int(str(decode_stored_setting("_settings_revision", revision.value)))
        revision.value = str(1 if revision_number >= 2147483647 else revision_number + 1)
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise


def user_flags(user: object | None) -> dict[str, bool]:
    """extract is_admin/is_hidden/is_banned from a CTFd User, only includes truthy keys"""
    if not user:
        return {}
    flags: dict[str, bool] = {}
    if getattr(user, "type", None) == "admin":
        flags["is_admin"] = True
    if getattr(user, "hidden", False):
        flags["is_hidden"] = True
    if getattr(user, "banned", False):
        flags["is_banned"] = True
    return flags


def get_all_settings() -> dict[str, SettingValue]:
    rows = DesktopSettingsModel.query.all()
    effective, _by_key = _decode_rows(rows)
    return effective


def set_settings(updates: dict[str, SettingValue]) -> None:
    """Validate and persist one public batch under the singleton revision lock."""
    if not updates:
        raise SettingsValidationError("settings update cannot be empty")
    parsed: dict[str, SettingValue] = {}
    for key, value in updates.items():
        if key not in PUBLIC_SETTING_KEYS:
            raise SettingsValidationError(f"unknown or internal setting {key!r}")
        parsed[key] = validate_setting_value(key, value)

    _ensure_revision_row()
    try:
        effective, by_key, revision = _locked_profile()
        effective.update(parsed)
        validate_effective_settings(effective)
        for key, value in parsed.items():
            row = by_key.get(key)
            if row is None:
                db.session.add(DesktopSettingsModel(key=key, value=serialize_setting(key, value)))
            else:
                row.value = serialize_setting(key, value)
        revision_number = int(str(decode_stored_setting("_settings_revision", revision.value)))
        revision.value = str(1 if revision_number >= 2147483647 else revision_number + 1)
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
