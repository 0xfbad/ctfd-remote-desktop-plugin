import logging
import urllib.request
import urllib.error

logger = logging.getLogger(__name__)


def proxy_http(check_hostname, novnc_port, path, query_string):
    """Proxy an HTTP request to the backend noVNC container.
    Returns a (status_code, content_type, body) tuple."""

    url = f"http://{check_hostname}:{novnc_port}/{path}"
    if query_string:
        url += f"?{query_string}"

    try:
        req = urllib.request.Request(url, method="GET")
        req.add_header("User-Agent", "CTFd-VNC-Proxy")
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read()
            content_type = resp.headers.get("Content-Type", "application/octet-stream")
            return resp.status, content_type, body
    except urllib.error.HTTPError as e:
        return e.code, "text/plain", e.read()
    except Exception as e:
        logger.error(f"vnc proxy: http proxy failed for {url}: {e}")
        return 502, "text/plain", b"proxy error"
