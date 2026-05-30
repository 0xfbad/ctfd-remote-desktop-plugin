from __future__ import annotations

import functools

from flask import jsonify, request


def _response_status(response):
    if isinstance(response, tuple) and len(response) >= 2 and isinstance(response[1], int):
        return response[1]
    return getattr(response, "status_code", 200)


def ratelimit_per_user(method="POST", limit=50, interval=300, key_prefix="rl_user", count_4xx=True):
    # keyed on user_id (not ip) so shared-egress students aren't throttled together.
    # count_4xx=False post-counts so cheap 4xx rejections don't burn the user's budget
    def decorator(f):
        @functools.wraps(f)
        def wrapper(*args, **kwargs):
            from CTFd.cache import cache
            from CTFd.utils.user import get_current_user, get_ip

            if request.method != method:
                return f(*args, **kwargs)

            user = get_current_user()
            if user is not None:
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
            # post-count: skip client-error 4xx, count everything else
            if status < 400 or status >= 500:
                _bump()
            return response

        return wrapper

    return decorator
