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

# per-session network pool naming, shared by the allocator, the host-side
# provisioning script (net-pool.sh) and the nftables rdb* bridge match.
# changing either prefix means re-provisioning every runner - decide once
NETWORK_POOL_PREFIX = "rd-net-"
NETWORK_BRIDGE_PREFIX = "rdb"


def network_slot_name(i: int) -> str:
    return f"{NETWORK_POOL_PREFIX}{i:02d}"

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
    # per-session network from the rd-net pool; NULL in shared-network mode
    network_name = db.Column(db.String(64), nullable=True)
    # set when the container is paused (io tripwire or admin hold); expiry and
    # shutdown cleanup skip paused rows so the writable layer survives as evidence
    paused_at = db.Column(db.Float(precision=53), nullable=True)


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
    # links the history row to its tlog transcript on the runner
    # (/var/lib/rd-tlog/sessions/<container_name>.tlog.jsonl)
    container_name = db.Column(db.String(512), nullable=True)


class DesktopNetworkSlotModel(db.Model):
    __tablename__ = "desktop_network_slots"
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    docker_context = db.Column(db.String(512), nullable=False)
    slot_index = db.Column(db.Integer, nullable=False)
    network_name = db.Column(db.String(64), nullable=False)
    # claimant, known before the container exists so the claim can precede create
    container_name = db.Column(db.String(512), nullable=False)
    user_id = db.Column(db.Integer, nullable=True)
    claimed_at = db.Column(db.Float(precision=53), nullable=False)
    # a free slot is the absence of a row; this constraint is the cross-worker
    # concurrency control (losers of the insert race get IntegrityError and retry)
    __table_args__ = (db.UniqueConstraint("docker_context", "slot_index", name="uq_rd_net_slot"),)


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
    )


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
    "command_log_interval": 30,
    "cap_drop": "ALL",
    "cap_add": "CHOWN,SETUID,SETGID,FOWNER,DAC_OVERRIDE,NET_RAW,NET_BIND_SERVICE,AUDIT_WRITE",
    "retention_days": 60,
    "rd_network_name": "rd-isolated",
    # connection toggles: applies to new sessions only; when off the service is
    # not started in the container and its port is never published
    "ssh_enabled": True,
    "web_terminal_enabled": True,
    # consent notice on the session start screen
    "consent_notice_enabled": True,
    # feature 1: storage budget. "" omits the kwarg (required on ext4 dev boxes -
    # the daemon refuses storage_opt unless the data-root is xfs+pquota). prod: "20g"
    "storage_limit": "",
    "log_max_size": "50m",  # "" omits the json-file log cap (escape hatch)
    "log_max_file": 3,
    "pause_watch_interval": 60,  # seconds between paused-container sweeps
    # feature 2: per-session network segmentation. False = shared rd_network_name
    # (dev/rollback); True = pooled single-tenant networks, fail closed
    "network_isolation": False,
    "network_pool_size": 24,
    # feature 3: fair-share governance. empty/0 omits the corresponding kwarg
    "memory_reservation": "1g",
    # swap cushion above memory_limit so long-lived desktops spill instead of
    # OOM-killing a live app. "" = cushion equal to memory_limit, "0" = no
    # swap, "-1" = unlimited, "<size>" = explicit swap amount
    "swap_limit": "",
    "oom_score_adj": 500,
    "nofile_soft": 1024,
    "nofile_hard": 1048576,
    "cgroup_parent": "rd.slice",
    # single master switch for all data collection: shell command logs,
    # per-session resource telemetry (docker stats), host cgroup/PSI/OOM
    # snapshots, and session recording. these feed usage analytics, the
    # activity feed, and the AI tutor's context. each mechanism self-skips
    # when its runner-side dependency is absent (host telemetry needs
    # provisioning/compute; recording needs tlog_socket_path set + collector)
    "telemetry_enabled": True,
    "telemetry_interval": 60,
    "telemetry_mem_warn_pct": 90,
    "telemetry_pids_warn_pct": 80,
    "telemetry_write_mbps_warn": 200,
    "telemetry_realert_seconds": 600,
    "telemetry_reader_image": "busybox:latest",
    # feature 4: admission control (auto-derived caps = fraction * host RAM / memory_limit)
    "capacity_ram_fraction": 0.7,
    # recording activates when telemetry is on AND this points at the
    # rd-tlog-collector socket. empty = recording off (dev default)
    "tlog_socket_path": "",
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
