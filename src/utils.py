from __future__ import annotations

import functools
import ipaddress
import re

from flask import jsonify, request

from .messages import RATE_LIMITED


_DNS_NAME_RE = re.compile(
    r"^(?=.{1,253}\.?$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.?$"
)


def normalize_public_hostname(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 253:
        raise ValueError("pub_hostname must be a valid hostname or IP address without a port")
    if value != value.strip() or any(ord(char) <= 32 or ord(char) == 127 for char in value):
        raise ValueError("pub_hostname must be a valid hostname or IP address without a port")
    if any(char in value for char in ("/", "@", "?", "#")):
        raise ValueError("pub_hostname must be a valid hostname or IP address without a port")

    bracketed = value.startswith("[") or value.endswith("]")
    if bracketed and not (value.startswith("[") and value.endswith("]")):
        raise ValueError("pub_hostname must be a valid hostname or IP address without a port")
    address_candidate = value[1:-1] if bracketed else value
    try:
        address = ipaddress.ip_address(address_candidate)
        if bracketed and address.version != 6:
            raise ValueError
        return f"[{address.compressed}]" if address.version == 6 else address.compressed
    except ValueError:
        if bracketed or not _DNS_NAME_RE.fullmatch(value):
            raise ValueError("pub_hostname must be a valid hostname or IP address without a port") from None
    return value.lower()


def _response_status(response):
    if isinstance(response, tuple) and len(response) >= 2 and isinstance(response[1], int):
        return response[1]
    return getattr(response, "status_code", 200)


_RATE_LIMIT_INCR_LUA = """
local current = redis.call('INCR', KEYS[1])
if current == 1 then
    redis.call('EXPIRE', KEYS[1], ARGV[1])
end
return current
"""

_RATE_LIMIT_DECR_LUA = """
local current = tonumber(redis.call('GET', KEYS[1]) or '0')
if current > 0 then
    return redis.call('DECR', KEYS[1])
end
return current
"""


def _increment_rate_limit(key: str, interval: int) -> int:
    from . import event_bus

    client = event_bus._get_publish_client()
    if client is not None:
        return int(client.eval(_RATE_LIMIT_INCR_LUA, 1, key, interval))

    # no portable atomic increment outside redis, this fallback can undercount under concurrent requests
    from CTFd.cache import cache

    try:
        if cache.add(key, 1, timeout=interval):
            return 1
    except (AttributeError, NotImplementedError):
        pass
    current = int(cache.get(key) or 0) + 1
    cache.set(key, current, timeout=interval)
    return current


def _decrement_rate_limit(key: str, interval: int) -> None:
    from . import event_bus

    client = event_bus._get_publish_client()
    if client is not None:
        client.eval(_RATE_LIMIT_DECR_LUA, 1, key)
        return

    from CTFd.cache import cache

    current = int(cache.get(key) or 0)
    if current > 0:
        # the cache backend cannot rewrite a value without also restarting its window
        cache.set(key, current - 1, timeout=interval)


def ratelimit_per_user(method="POST", limit=50, interval=300, key_prefix="rl_user", count_4xx=True):
    def decorator(f):
        @functools.wraps(f)
        def wrapper(*args, **kwargs):
            from CTFd.utils.user import get_current_user, get_ip

            if request.method != method:
                return f(*args, **kwargs)

            user = get_current_user()
            if user is not None:
                # keyed on user_id not ip so students behind one egress ip are not throttled together
                bucket = f"u{user.id}"
            else:
                bucket = f"ip{get_ip()}"
            # the effective policy is part of the key so a limit change does not inherit an old bucket
            key = f"ctfd-remote-desktop:{key_prefix}:{bucket}:{request.endpoint}:{limit}:{interval}"

            if _increment_rate_limit(key, interval) > limit:
                resp = jsonify({"code": 429, "message": RATE_LIMITED.format(limit=limit, interval=interval)})
                resp.status_code = 429
                resp.headers["Retry-After"] = str(interval)
                return resp

            response = f(*args, **kwargs)
            # a 4xx burst still occupies budget until the compensating decrements land
            if not count_4xx and 400 <= _response_status(response) < 500:
                _decrement_rate_limit(key, interval)
            return response

        return wrapper

    return decorator
