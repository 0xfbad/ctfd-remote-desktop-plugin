from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import json
import math
import re


SettingValue = bool | int | float | str | None


class SettingsValidationError(ValueError):
    """A submitted or persisted settings profile is not safe to use."""


@dataclass(frozen=True)
class SettingSpec:
    default: SettingValue
    value_type: type
    minimum: int | float | None = None
    maximum: int | float | None = None
    max_length: int | None = None
    allow_empty: bool = False
    choices: frozenset[str] | None = None
    public: bool = True
    restart_required: bool = False


def _spec(
    default: SettingValue,
    *,
    minimum: int | float | None = None,
    maximum: int | float | None = None,
    max_length: int | None = None,
    allow_empty: bool = False,
    choices: tuple[str, ...] | None = None,
    public: bool = True,
    restart_required: bool = False,
) -> SettingSpec:
    return SettingSpec(
        default=default,
        value_type=type(default),
        minimum=minimum,
        maximum=maximum,
        max_length=max_length,
        allow_empty=allow_empty,
        choices=frozenset(choices) if choices else None,
        public=public,
        restart_required=restart_required,
    )


# This registry is the sole definition of setting names, types, defaults,
# generic scalar bounds, API visibility, and restart behavior.
SETTING_SPECS: dict[str, SettingSpec] = {
    "remote_desktop_enabled": _spec(False),
    "docker_image": _spec("ctfd-remote-desktop:latest", max_length=512),
    "memory_limit": _spec("4g", max_length=32),
    "shm_size": _spec("512m", max_length=32),
    "resolution": _spec("1920x1080", max_length=32),
    "cpu_limit": _spec(2.0, minimum=0.1, maximum=64.0),
    "initial_duration": _spec(3600, minimum=60, maximum=604800),
    "extension_duration": _spec(1800, minimum=0, maximum=604800),
    "max_extensions": _spec(3, minimum=0, maximum=100),
    # The image deliberately withholds noVNC until Xvnc, the XFCE session,
    # window manager, and panel have each passed their bounded startup gate.
    # At the 0.5s polling interval, 420 attempts gives that 150s worst-case
    # phase budget another minute of host-load/key-generation headroom.
    "vnc_ready_attempts": _spec(420, minimum=1, maximum=3600),
    "http_request_timeout": _spec(3, minimum=1, maximum=120),
    "cleanup_interval": _spec(300, minimum=5, maximum=86400, restart_required=True),
    "pids_limit": _spec(4096, minimum=64, maximum=1048576),
    "max_concurrent_creates": _spec(2, minimum=1, maximum=128),
    "username_source": _spec("name", max_length=16, choices=("name", "email")),
    "require_verified": _spec(True),
    "cap_drop": _spec("ALL", max_length=512),
    "cap_add": _spec(
        "CHOWN,SETUID,SETGID,FOWNER,DAC_OVERRIDE,NET_RAW,NET_BIND_SERVICE,AUDIT_WRITE,SYS_CHROOT",
        max_length=512,
        allow_empty=True,
    ),
    "retention_days": _spec(60, minimum=1, maximum=3650),
    "rd_network_name": _spec("bridge", max_length=128),
    "ssh_enabled": _spec(True),
    "web_terminal_enabled": _spec(True),
    "storage_limit": _spec("", max_length=32, allow_empty=True),
    "log_max_size": _spec("50m", max_length=32, allow_empty=True),
    "log_max_file": _spec(3, minimum=1, maximum=100),
    "pause_watch_interval": _spec(60, minimum=5, maximum=86400, restart_required=True),
    "memory_reservation": _spec("1g", max_length=32, allow_empty=True),
    "swap_limit": _spec("1g", max_length=32, allow_empty=True),
    "oom_score_adj": _spec(500, minimum=0, maximum=1000),
    "nofile_soft": _spec(1024, minimum=0, maximum=1048576),
    "nofile_hard": _spec(1048576, minimum=0, maximum=1048576),
    "cgroup_parent": _spec("", max_length=128, allow_empty=True),
    "capacity_ram_fraction": _spec(0.7, minimum=0.01, maximum=1.0),
    # Internal settings have an explicit non-public path and never appear in
    # the admin settings API. The revision row is the cross-worker DB mutex.
    "image_cache": _spec("", max_length=2000000, allow_empty=True, public=False),
    "_settings_revision": _spec(1, minimum=1, maximum=2147483647, public=False),
}

SETTING_DEFAULTS: dict[str, SettingValue] = {key: spec.default for key, spec in SETTING_SPECS.items() if spec.public}
PUBLIC_SETTING_KEYS = frozenset(SETTING_DEFAULTS)
INTERNAL_SETTING_KEYS = frozenset(key for key, spec in SETTING_SPECS.items() if not spec.public)
RESTART_REQUIRED_SETTINGS = frozenset(
    key for key, spec in SETTING_SPECS.items() if spec.public and spec.restart_required
)

_CANONICAL_INT_RE = re.compile(r"0|-?[1-9][0-9]*")
_FINITE_DECIMAL_RE = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?")
_SIZE_RE = re.compile(r"([0-9]+(?:\.[0-9]+)?)([kmgt]i?b?|b)?", re.IGNORECASE)
_CAP_RE = re.compile(r"[A-Z][A-Z0-9_]*")
_CGROUP_RE = re.compile(r"[A-Za-z0-9_.-]+\.slice")
_NETWORK_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")


def _has_control_characters(value: str) -> bool:
    return any(ord(char) < 32 or ord(char) == 127 for char in value)


def _parse_size(value: str) -> int:
    match = _SIZE_RE.fullmatch(value.strip())
    if match is None:
        raise SettingsValidationError(f"invalid size value {value!r}")
    amount = float(match.group(1))
    if not math.isfinite(amount):
        raise SettingsValidationError(f"invalid size value {value!r}")
    unit = (match.group(2) or "b").lower()
    powers = {
        "b": 0,
        "k": 1,
        "kb": 1,
        "kib": 1,
        "m": 2,
        "mb": 2,
        "mib": 2,
        "g": 3,
        "gb": 3,
        "gib": 3,
        "t": 4,
        "tb": 4,
        "tib": 4,
    }
    result = int(amount * (1024 ** powers[unit]))
    if result < 0:
        raise SettingsValidationError(f"invalid size value {value!r}")
    return result


def _validate_caps(key: str, value: str, *, allow_empty: bool) -> None:
    if not value and allow_empty:
        return
    tokens = value.split(",")
    if not tokens or any(not token or _CAP_RE.fullmatch(token) is None for token in tokens):
        raise SettingsValidationError(f"{key} must be a comma-separated uppercase capability list")
    if len(tokens) != len(set(tokens)):
        raise SettingsValidationError(f"{key} must not contain duplicate capabilities")


def validate_setting_value(key: str, value: object) -> SettingValue:
    spec = SETTING_SPECS.get(key)
    if spec is None:
        raise SettingsValidationError(f"unknown setting {key!r}")

    if spec.value_type is bool:
        if type(value) is not bool:
            raise SettingsValidationError(f"{key} must be a boolean")
        parsed: SettingValue = value
    elif spec.value_type is int:
        if type(value) is not int:
            raise SettingsValidationError(f"{key} must be an integer")
        parsed = value
    elif spec.value_type is float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise SettingsValidationError(f"{key} must be a finite number")
        parsed = float(value)
        if not math.isfinite(parsed):
            raise SettingsValidationError(f"{key} must be a finite number")
    else:
        if type(value) is not str:
            raise SettingsValidationError(f"{key} must be a string")
        parsed = value
        if parsed != parsed.strip():
            raise SettingsValidationError(f"{key} must not have leading or trailing whitespace")
        if not parsed and not spec.allow_empty:
            raise SettingsValidationError(f"{key} cannot be empty")
        if spec.max_length is not None and len(parsed) > spec.max_length:
            raise SettingsValidationError(f"{key} exceeds the {spec.max_length}-character limit")
        if _has_control_characters(parsed):
            raise SettingsValidationError(f"{key} contains control characters")
        if spec.choices is not None and parsed not in spec.choices:
            choices = ", ".join(sorted(spec.choices))
            raise SettingsValidationError(f"{key} must be one of: {choices}")

    if isinstance(parsed, (int, float)) and not isinstance(parsed, bool):
        if spec.minimum is not None and parsed < spec.minimum:
            raise SettingsValidationError(f"{key} must be at least {spec.minimum}")
        if spec.maximum is not None and parsed > spec.maximum:
            raise SettingsValidationError(f"{key} must be at most {spec.maximum}")

    if not isinstance(parsed, str):
        return parsed

    if key == "docker_image" and any(char.isspace() for char in parsed):
        raise SettingsValidationError(f"{key} must not contain whitespace")
    if key in {"memory_limit", "shm_size"}:
        if _parse_size(parsed) <= 0:
            raise SettingsValidationError(f"{key} must be greater than zero")
    elif key == "storage_limit" and parsed:
        size = _parse_size(parsed)
        if size < 1024**3 or size > 1024**4:
            raise SettingsValidationError("storage_limit must be empty or between 1g and 1t")
    elif key == "log_max_size" and parsed:
        size = _parse_size(parsed)
        if size < 1024**2 or size > 1024**3:
            raise SettingsValidationError("log_max_size must be empty or between 1m and 1g")
    elif key == "memory_reservation" and parsed not in {"", "0"}:
        if _parse_size(parsed) <= 0:
            raise SettingsValidationError("memory_reservation must be empty, 0, or a positive size")
    elif key == "swap_limit" and parsed not in {"", "0", "-1"}:
        size = _parse_size(parsed)
        if size <= 0 or size > 1024**4:
            raise SettingsValidationError("swap_limit must be empty, 0, -1, or a size no greater than 1t")
    elif key == "resolution":
        match = re.fullmatch(r"([0-9]{3,4})x([0-9]{3,4})", parsed)
        if match is None or not (320 <= int(match.group(1)) <= 7680 and 200 <= int(match.group(2)) <= 4320):
            raise SettingsValidationError("resolution must be WIDTHxHEIGHT between 320x200 and 7680x4320")
    elif key == "cgroup_parent" and parsed and _CGROUP_RE.fullmatch(parsed) is None:
        raise SettingsValidationError("cgroup_parent must be empty or a systemd .slice name")
    elif key == "rd_network_name" and _NETWORK_RE.fullmatch(parsed) is None:
        raise SettingsValidationError("rd_network_name is not a valid Docker network name")
    elif key == "cap_drop":
        _validate_caps(key, parsed, allow_empty=False)
    elif key == "cap_add":
        _validate_caps(key, parsed, allow_empty=True)
    return parsed


def parse_api_updates(payload: object) -> dict[str, SettingValue]:
    if not isinstance(payload, dict):
        raise SettingsValidationError("request body must be a JSON object")
    if not payload:
        raise SettingsValidationError("settings update cannot be empty")

    updates: dict[str, SettingValue] = {}
    for key, value in payload.items():
        if type(key) is not str or not key or len(key) > 128 or _has_control_characters(key):
            raise SettingsValidationError("setting names must be printable strings of at most 128 characters")
        spec = SETTING_SPECS.get(key)
        if spec is None:
            raise SettingsValidationError(f"unknown setting {key!r}")
        if not spec.public:
            raise SettingsValidationError(f"setting {key!r} is internal and cannot be changed through this API")
        if value is None:
            raise SettingsValidationError(f"{key} cannot be null")
        updates[key] = validate_setting_value(key, value)
    return updates


def serialize_setting(key: str, value: object) -> str:
    parsed = validate_setting_value(key, value)
    if isinstance(parsed, bool):
        return "true" if parsed else "false"
    if isinstance(parsed, int):
        return str(parsed)
    if isinstance(parsed, float):
        return json.dumps(parsed, allow_nan=False, separators=(",", ":"))
    assert isinstance(parsed, str)
    if key in {"memory_limit", "shm_size", "storage_limit", "log_max_size", "memory_reservation", "swap_limit"}:
        return parsed.lower()
    return parsed


def decode_stored_setting(key: str, raw: object) -> SettingValue:
    spec = SETTING_SPECS.get(key)
    if spec is None:
        raise SettingsValidationError(f"unknown persisted setting {key!r}")
    if type(raw) is not str:
        raise SettingsValidationError(f"persisted setting {key} must contain text")

    if spec.value_type is bool:
        if raw == "true":
            value: object = True
        elif raw == "false":
            value = False
        else:
            raise SettingsValidationError(f"persisted setting {key} is not a canonical boolean")
    elif spec.value_type is int:
        if _CANONICAL_INT_RE.fullmatch(raw):
            value = int(raw)
        else:
            raise SettingsValidationError(f"persisted setting {key} is not an integer")
    elif spec.value_type is float:
        if _FINITE_DECIMAL_RE.fullmatch(raw) is None:
            raise SettingsValidationError(f"persisted setting {key} is not a canonical finite number")
        try:
            value = float(raw)
        except ValueError as exc:
            raise SettingsValidationError(f"persisted setting {key} is not a finite number") from exc
        if not math.isfinite(value):
            raise SettingsValidationError(f"persisted setting {key} is not a finite number")
    else:
        value = raw
    return validate_setting_value(key, value)


def validate_effective_settings(settings: Mapping[str, object]) -> dict[str, SettingValue]:
    missing = PUBLIC_SETTING_KEYS.difference(settings)
    if missing:
        raise SettingsValidationError(f"settings profile is missing {', '.join(sorted(missing))}")

    validated = {key: validate_setting_value(key, settings[key]) for key in PUBLIC_SETTING_KEYS}
    memory = _parse_size(str(validated["memory_limit"]))
    shm = _parse_size(str(validated["shm_size"]))
    if shm > memory:
        raise SettingsValidationError("shm_size cannot exceed memory_limit")

    reservation_raw = str(validated["memory_reservation"])
    if reservation_raw not in {"", "0"} and _parse_size(reservation_raw) > memory:
        raise SettingsValidationError("memory_reservation cannot exceed memory_limit")
    if int(str(validated["nofile_soft"])) > int(str(validated["nofile_hard"])):
        raise SettingsValidationError("nofile_soft cannot exceed nofile_hard")
    if int(str(validated["max_extensions"])) > 0 and int(str(validated["extension_duration"])) == 0:
        raise SettingsValidationError("extension_duration must be positive when max_extensions is positive")
    max_lifetime = int(str(validated["initial_duration"])) + (
        int(str(validated["extension_duration"])) * int(str(validated["max_extensions"]))
    )
    if max_lifetime > 30 * 86400:
        raise SettingsValidationError("maximum possible session lifetime cannot exceed 30 days")
    return validated
