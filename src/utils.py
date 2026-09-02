from __future__ import annotations

import functools
import ipaddress
import re

from flask import jsonify, request


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


def ratelimit_per_user(method="POST", limit=50, interval=300, key_prefix="rl_user", count_4xx=True):
    def decorator(f):
        @functools.wraps(f)
        def wrapper(*args, **kwargs):
            from CTFd.cache import cache
            from CTFd.utils.user import get_current_user, get_ip

            if request.method != method:
                return f(*args, **kwargs)

            user = get_current_user()
            if user is not None:
                # keyed on user_id not ip so students behind one egress ip are not throttled together
                bucket = f"u{user.id}"
            else:
                bucket = f"ip{get_ip()}"
            key = f"{key_prefix}:{bucket}:{request.endpoint}"

            current = cache.get(key)
            if current is not None and int(current) >= limit:
                resp = jsonify(
                    {
                        "code": 429,
                        "message": f"Too many requests. Limit is {limit} requests in {interval} seconds",
                    }
                )
                resp.status_code = 429
                resp.headers["Retry-After"] = str(interval)
                return resp

            def _bump():
                if current is None:
                    cache.set(key, 1, timeout=interval)
                else:
                    cache.set(key, int(current) + 1, timeout=interval)

            if count_4xx:
                _bump()
                return f(*args, **kwargs)

            response = f(*args, **kwargs)
            status = _response_status(response)
            # skip 4xx so cheap rejections do not burn the user budget
            if status < 400 or status >= 500:
                _bump()
            return response

        return wrapper

    return decorator
