from __future__ import annotations

from CTFd.models import db, Users
from markupsafe import escape as _markup_escape

SettingValue = bool | int | float | str | None

# end_reason values persisted to the desktop_session_history.end_reason column.
# these strings live in the DB, the names exist only to prevent typo drift at call sites
END_REASON_RECONCILIATION = "reconciliation"
END_REASON_USER_DESTROYED = "user_destroyed"
END_REASON_ADMIN_KILLED = "admin_killed"
END_REASON_EXPIRED = "expired"

# noVNC viewer query string shared by the absolute and relative vnc.html URL builders
VNC_VIEWER_QUERY = "autoconnect=true&resize=remote&reconnect=true"

# strftime format for human-facing timestamps (event log datetime, image build date).
# %-d / %-I are glibc-specific no-pad directives, fine on the linux deploy target
DISPLAY_DATETIME_FORMAT = "%b %-d, %Y %-I:%M:%S %p"


def _esc(val: str | None) -> str:
    """html-escape a string for safe embedding in JSON / innerHTML contexts"""
    return str(_markup_escape(val)) if val else ""


def username_or_fallback(user: Users | None, user_id: int) -> str:  # type: ignore[type-arg]
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


class DesktopContainerInfoModel(db.Model):
    __tablename__ = "desktop_container_info"
    container_id = db.Column(db.String(512), primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
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
    # nullable=True so legacy rows from before this column existed survive
    # without a data migration. on destroy, a NULL skips the cache revocation
    cookie_sid = db.Column(db.String(128), nullable=True)


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


class CommandLogModel(db.Model):
    __tablename__ = "desktop_command_logs"
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    container_id = db.Column(db.String(512), nullable=False)
    timestamp = db.Column(db.Float(precision=53), nullable=False)
    command = db.Column(db.Text, nullable=False)
    exit_code = db.Column(db.Integer, nullable=True)
    duration = db.Column(db.Integer, nullable=True)
    cwd = db.Column(db.Text, nullable=True)
    tty = db.Column(db.String(64), nullable=True)


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
    # no FK on user_id, deleting a user should not cascade-wipe their audit trail
    timestamp = db.Column(db.Float(precision=53), nullable=False, index=True)
    event_type = db.Column(db.String(128), nullable=False, index=True)
    level = db.Column(db.String(16), nullable=False)
    user_id = db.Column(db.Integer, nullable=True)
    username = db.Column(db.String(512), nullable=True)
    message = db.Column(db.Text, nullable=False)
    metadata_json = db.Column(db.Text, nullable=True)


SETTING_DEFAULTS: dict[str, SettingValue] = {
    "remote_desktop_enabled": False,
    "docker_image": "ctfd-remote-desktop:latest",
    "memory_limit": "4g",
    "shm_size": "512m",
    "resolution": "1920x1080",
    "cpu_limit": 2,
    "initial_duration": 3600,
    "extension_duration": 1800,
    "max_extensions": 3,
    "vnc_ready_attempts": 180,
    "http_request_timeout": 3,
    "cleanup_interval": 300,
    "pids_limit": 4096,
    "max_concurrent_creates": 2,
    "username_source": "name",
    "require_verified": True,
    "command_logging_enabled": False,
    "command_log_interval": 30,
    "cap_drop": "ALL",
    "cap_add": "CHOWN,SETUID,SETGID,FOWNER,DAC_OVERRIDE,NET_RAW,NET_BIND_SERVICE,AUDIT_WRITE",
    "retention_days": 60,
    "rd_network_name": "rd-isolated",
}


def _coerce(raw: str, default: SettingValue) -> SettingValue:
    if default is None:
        return raw

    target = type(default)
    if target is bool:
        return raw.lower() in ("true", "1", "yes") if isinstance(raw, str) else bool(raw)
    if target is int:
        return int(float(raw))
    if target is float:
        return float(raw)
    return raw


def get_setting(key: str, default: SettingValue = None) -> SettingValue:
    if default is None:
        default = SETTING_DEFAULTS.get(key)
    row = DesktopSettingsModel.query.filter_by(key=key).first()
    if row and row.value is not None:
        return _coerce(row.value, default)
    return default


def set_setting(key: str, value: SettingValue) -> None:
    row = DesktopSettingsModel.query.filter_by(key=key).first()
    if row:
        row.value = str(value)
    else:
        row = DesktopSettingsModel(key=key, value=str(value))
        db.session.add(row)
    db.session.commit()


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
    settings: dict[str, SettingValue] = dict(SETTING_DEFAULTS)
    rows = DesktopSettingsModel.query.all()
    for row in rows:
        default = SETTING_DEFAULTS.get(row.key)
        settings[row.key] = _coerce(row.value, default)
    return settings
