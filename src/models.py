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

END_REASON_RECONCILIATION = "reconciliation"  # persisted reason values must stay compatible with stored history
END_REASON_USER_DESTROYED = "user_destroyed"
END_REASON_ADMIN_KILLED = "admin_killed"
END_REASON_EXPIRED = "expired"

LIFECYCLE_ACTIVE = "active"  # session rows stay authoritative until teardown is confirmed
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

NOVNC_VERSION = "869e3dcb0d8de7f5"
VNC_VIEWER_QUERY = "autoconnect=true&resize=remote&reconnect=true&host="


def proxy_vnc_url(user_id: int, password: str) -> str:
    return (
        f"/remote-desktop/static/novnc/{NOVNC_VERSION}/vnc.html?{VNC_VIEWER_QUERY}"
        f"&path=/remote-desktop/vnc/{user_id}/websockify"
        f"#password={password}"  # fragments keep credentials out of requests and referer headers
    )


DISPLAY_DATETIME_FORMAT = "%b %-d, %Y %-I:%M:%S %p"  # no pad directives require glibc on the deploy target


def _esc(val: str | None) -> str:
    return str(_markup_escape(val)) if val else ""


def username_or_fallback(user: Users | None, user_id: int) -> str:
    return user.name if user else f"User {user_id}"


class DesktopDockerContextModel(db.Model):
    __tablename__ = "desktop_docker_contexts"
    id = db.Column(db.Integer, primary_key=True)
    context_name = db.Column(db.String(512), unique=True, nullable=False)
    hostname = db.Column(db.String(512), nullable=True)
    pub_hostname = db.Column(db.String(512), nullable=False)
    weight = db.Column(db.Integer, default=1)
    enabled = db.Column(db.Boolean, default=True)
    max_containers = db.Column(db.Integer, nullable=True)  # null derives from ram, zero drains the host
    active_sessions = db.Column(db.Integer, nullable=False, default=0, server_default="0")


class DesktopContainerInfoModel(db.Model):
    __tablename__ = "desktop_container_info"
    container_id = db.Column(db.String(512), primary_key=True)
    user_id = db.Column(db.Integer, nullable=False)  # account deletion must preserve live docker object tracking
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
    cookie_sid = db.Column(db.String(128), nullable=True)  # null leaves no autologin cache entry to revoke

    paused_at = db.Column(db.Float(precision=53), nullable=True)  # paused layers survive cleanup as evidence

    session_uuid = db.Column(db.String(36), nullable=False)  # exists before docker creation and survives teardown
    lifecycle_state = db.Column(
        db.String(32), nullable=False, default=LIFECYCLE_ACTIVE, server_default=LIFECYCLE_ACTIVE
    )
    lifecycle_reason = db.Column(db.String(128), nullable=True)

    __table_args__ = (
        db.UniqueConstraint("user_id", name="uq_desktop_container_info_user_id"),
        db.UniqueConstraint("session_uuid", name="uq_desktop_container_info_session_uuid"),
    )


class DesktopSessionOperationModel(db.Model):
    __tablename__ = "desktop_session_operations"
    user_id = db.Column(db.Integer, primary_key=True)  # account deletion must preserve pending recovery
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


class DesktopRecordedCommandModel(db.Model):
    __tablename__ = "desktop_recorded_commands"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, nullable=False, index=True)
    session_uuid = db.Column(db.String(36), nullable=False)
    byte_offset = db.Column(db.BigInteger, nullable=False)
    timestamp = db.Column(db.Float(precision=53), nullable=False, index=True)
    command = db.Column(db.Text, nullable=False)
    tool = db.Column(db.Text, nullable=False)
    exit_code = db.Column(db.Integer, nullable=False)
    duration_ms = db.Column(db.BigInteger, nullable=True)
    cwd = db.Column(db.Text, nullable=False)
    tty = db.Column(db.Text, nullable=False)

    __table_args__ = (db.UniqueConstraint("session_uuid", "byte_offset", name="uq_desktop_recorded_commands_offset"),)


class DesktopCommandCursorModel(db.Model):
    __tablename__ = "desktop_command_cursors"
    session_uuid = db.Column(db.String(36), primary_key=True)
    user_id = db.Column(db.Integer, nullable=False)
    container_id = db.Column(db.String(512), nullable=False)
    byte_offset = db.Column(db.BigInteger, nullable=False, default=0, server_default="0")
    journal_id = db.Column(db.String(128), nullable=True)
    last_read_at = db.Column(db.Float(precision=53), nullable=True)
    status = db.Column(db.String(32), nullable=False, default="unknown", server_default="unknown")


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


class DesktopPluginMetadataModel(db.Model):
    __tablename__ = "desktop_plugin_metadata"  # older binaries reject extra keys in desktop_settings
    key = db.Column(db.String(128), primary_key=True)
    value = db.Column(db.String(512), nullable=False)


class DesktopEventLogModel(db.Model):
    __tablename__ = "desktop_event_log"
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    event_id = db.Column(db.String(128), nullable=False)
    timestamp = db.Column(db.Float(precision=53), nullable=False, index=True)
    event_type = db.Column(db.String(128), nullable=False, index=True)
    level = db.Column(db.String(16), nullable=False)
    user_id = db.Column(db.Integer, nullable=True)  # account deletion must preserve the audit trail
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
        db.session.rollback()  # another worker can win the initial seed race


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


def _migrate_v1_defaults(
    effective: dict[str, SettingValue],
    by_key: dict[str, DesktopSettingsModel],
) -> bool:
    migrated = False
    legacy_cap_add = "CHOWN,SETUID,SETGID,FOWNER,DAC_OVERRIDE,NET_RAW,NET_BIND_SERVICE,AUDIT_WRITE"
    cap_row = by_key.get("cap_add")
    if cap_row is not None and decode_stored_setting("cap_add", cap_row.value) == legacy_cap_add:
        effective["cap_add"] = SETTING_DEFAULTS["cap_add"]
        migrated = True
    readiness_row = by_key.get("vnc_ready_attempts")
    if readiness_row is not None and decode_stored_setting("vnc_ready_attempts", readiness_row.value) == 180:
        effective["vnc_ready_attempts"] = SETTING_DEFAULTS["vnc_ready_attempts"]
        migrated = True
    return migrated


def initialize_settings() -> None:
    _ensure_revision_row()
    try:
        effective, by_key, revision = _locked_profile()
        schema_row = DesktopPluginMetadataModel.query.filter_by(key="settings_schema_version").first()
        try:
            schema_version = int(str(schema_row.value)) if schema_row is not None else 1
        except (TypeError, ValueError) as exc:
            raise SettingsValidationError("invalid settings schema version") from exc
        if schema_version < 1 or schema_version > 3:
            raise SettingsValidationError("unsupported settings schema version")
        public_profile_migrated = False
        if schema_version < 2:
            public_profile_migrated = _migrate_v1_defaults(effective, by_key)

        if schema_version < 3:
            if effective["cap_add"] == (
                "CHOWN,SETUID,SETGID,FOWNER,DAC_OVERRIDE,NET_RAW,NET_BIND_SERVICE,AUDIT_WRITE,SYS_CHROOT"
            ):
                effective["cap_add"] = SETTING_DEFAULTS["cap_add"]
                public_profile_migrated = True
            schema_version = 3

        for key, value in effective.items():
            canonical = serialize_setting(key, value)
            row = by_key.get(key)
            if row is None:
                db.session.add(DesktopSettingsModel(key=key, value=canonical))
            else:
                row.value = canonical
        for key in INTERNAL_SETTING_KEYS:
            row = by_key.get(key)
            if row is not None:
                row.value = serialize_setting(key, decode_stored_setting(key, row.value))
        if schema_row is None:
            db.session.add(DesktopPluginMetadataModel(key="settings_schema_version", value=str(schema_version)))
        else:
            schema_row.value = str(schema_version)

        revision_number = int(str(decode_stored_setting("_settings_revision", revision.value)))
        if public_profile_migrated:
            revision_number = 1 if revision_number >= 2147483647 else revision_number + 1
        revision.value = serialize_setting("_settings_revision", revision_number)
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise


def set_setting(key: str, value: SettingValue) -> None:
    if key != "image_cache":
        raise SettingsValidationError(f"setting {key!r} is not writable through the internal settings path")
    parsed = validate_setting_value(key, value)
    _ensure_revision_row()
    try:
        _effective, by_key, _revision = _locked_profile()
        row = by_key.get(key)
        if row is None:
            db.session.add(DesktopSettingsModel(key=key, value=serialize_setting(key, parsed)))
        else:
            row.value = serialize_setting(key, parsed)
        db.session.commit()  # diagnostics must not advance the settings revision
    except Exception:
        db.session.rollback()
        raise


def user_flags(user: object | None) -> dict[str, bool]:
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
