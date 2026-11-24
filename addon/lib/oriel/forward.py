"""Pass page requests on to the app's server, for servers that answer pages
from other origins without CORS headers.

The page requests /api/forward/<path> on the page server; the request goes
to <base>/<path> with the page's method, query, body and headers, and the
page receives the server's status, headers and body. Only the base the app
set with `forward-source` is reachable, so the route cannot fetch other
hosts.

Replies are read whole before they are answered, so forward API requests,
not media. Kodi plays media through the media proxy.
"""
import http.client
import json
from urllib.parse import urlsplit

PREFIX = "/api/forward"
TIMEOUT = 30
# A larger reply fails rather than filling the add-on's memory.
MAX_BODY = 16 * 1024 * 1024

# Headers that belong to one connection, describe the page, or that the
# page server sets itself.
REQUEST_SKIP = {
    "host", "origin", "referer", "cookie", "connection", "keep-alive", "proxy-authorization",
    "proxy-connection", "te", "trailer", "transfer-encoding", "upgrade", "content-length",
    "accept-encoding",
}
RESPONSE_SKIP = {
    "connection", "keep-alive", "proxy-authenticate", "trailer", "transfer-encoding", "upgrade",
    "content-length", "content-encoding", "set-cookie", "date", "server",
}


class Source:
    """A server base: scheme, host, port and path prefix."""

    def __init__(self, base):
        if not isinstance(base, str):
            raise ValueError("base must be a string")
        u = urlsplit(base)
        if u.scheme not in ("http", "https") or not u.hostname:
            raise ValueError("the forward source must be an http or https URL")
        if u.username or u.password or u.query or u.fragment:
            raise ValueError("the forward source must not carry credentials, a query or a fragment")
        self.base = f"{u.scheme}://{u.netloc}{u.path.rstrip('/')}"
        self.https = u.scheme == "https"
        self.host = u.hostname
        self.port = u.port or (443 if self.https else 80)
        self.prefix = u.path.rstrip("/")

    def local(self, location):
        """Map a redirect into the source back onto the forward route, so the
        page follows it here; leave other locations as they are."""
        if location.startswith(self.base + "/") or location == self.base:
            return PREFIX + location[len(self.base):]
        if location.startswith("/") and not location.startswith("//"):
            if location == self.prefix or location.startswith(self.prefix + "/"):
                return PREFIX + location[len(self.prefix):]
        return location


class Unreachable(Exception):
    pass


def send(source, method, path, headers, body):
    """Send one request to the source. `path` is the part after PREFIX, with
    its query. Returns (status, headers, body); raises Unreachable when the
    server doesn't answer or its reply is too large."""
    kind = http.client.HTTPSConnection if source.https else http.client.HTTPConnection
    conn = kind(source.host, source.port, timeout=TIMEOUT)
    sent = {k: v for k, v in headers.items() if k.lower() not in REQUEST_SKIP and not k.lower().startswith("sec-")}
    try:
        conn.request(method, source.prefix + (path or "/"), body=body, headers=sent)
        response = conn.getresponse()
        data = response.read(MAX_BODY + 1)
        if len(data) > MAX_BODY:
            raise Unreachable("the reply is too large to forward")
        kept = []
        for k, v in response.getheaders():
            if k.lower() in RESPONSE_SKIP or k.lower().startswith("access-control-"):
                continue
            kept.append((k, source.local(v) if k.lower() == "location" else v))
        return response.status, kept, data
    except (OSError, http.client.HTTPException) as e:
        raise Unreachable(str(e) or type(e).__name__) from e
    finally:
        conn.close()


def failure(code, message):
    """A reply the page tells apart from the server's own: `Oriel-Forward`
    names the failure."""
    status = 409 if code == "unavailable" else 502
    return status, [("Content-Type", "application/json"), ("Oriel-Forward", code)], json.dumps(
        {"error": message, "code": code}).encode()
