"""Serve the page and app API on 127.0.0.1.

    GET  /             app resources/web files
    GET  /api/events   server-sent `state` events
    POST /api/cmd      command result {"result"}, or {"error", "code"}
                       with the HTTP status defined by FAILURES
    GET  /api/store    saved settings
    POST /api/store    merge settings; null deletes a key

Do not serve media to Kodi here: its playback threads must not wait on
Kodi's Python interpreter. /api accepts the configured development origin.

Reject foreign Origin headers before processing commands. CORS response
filtering alone cannot stop a web page from sending simple requests.
Require Host to name 127.0.0.1 or localhost at this server's port, including
for requests without Origin, to prevent DNS rebinding.
"""
import json
import math
import mimetypes
import os
import queue
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Map command failures to HTTP status: invalid request, unavailable app state,
# unexpected handler failure, or an expired response deadline with unknown outcome.
FAILURES = {"invalid": 400, "unavailable": 409, "failed": 500, "timeout": 504}


def finite(text):
    # Reject valid JSON numbers such as 1e309 that overflow Python floats.
    value = float(text)
    if not math.isfinite(value):
        raise ValueError(f"{text} is too large")
    return value


def not_json(constant):
    # Reject NaN and Infinity, which Python accepts but JSON forbids.
    raise ValueError(f"{constant} is not JSON")


def failure(code, message):
    return FAILURES[code], {"error": message, "code": code}


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, port, web_root, store_path, on_command, origin):
        super().__init__(("127.0.0.1", port), Handler)
        self.web_root = os.path.realpath(web_root)
        self.store_path = store_path
        # Controller callback returns the command's (HTTP status, response body).
        self.on_command = on_command
        self.origin = origin
        self.lock = threading.Lock()
        self.clients = set()
        self.state = {}
        self.serving = False

    def store(self, changes=None):
        with self.lock:
            try:
                with open(self.store_path) as f:
                    data = json.load(f)
            except (FileNotFoundError, ValueError):
                data = {}
            if changes:
                data.update(changes)
                data = {k: v for k, v in data.items() if v is not None}
                tmp = self.store_path + ".tmp"
                fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w") as f:
                    json.dump(data, f)
                os.replace(tmp, self.store_path)
            return data

    def start(self):
        self.serving = True
        threading.Thread(target=self.serve_forever, name="oriel-http", daemon=True).start()

    def publish(self, state):
        with self.lock:
            self.state = state
            for q in self.clients:
                q.put(state)

    def stop(self):
        with self.lock:
            for q in self.clients:
                q.put(None)
        if self.serving:  # shutdown() blocks unless serve_forever() is running.
            self.shutdown()
        self.server_close()


class Handler(BaseHTTPRequestHandler):
    server: Server

    def trusted(self):
        port = self.server.server_address[1]
        own = (f"127.0.0.1:{port}", f"localhost:{port}")
        origin = self.headers.get("Origin")
        return self.headers.get("Host") in own and (
            origin is None or origin == self.server.origin or origin in (f"http://{host}" for host in own))

    def do_GET(self):
        if not self.trusted():
            return self.send_error(403)
        path = self.path.split("?", 1)[0]
        if path == "/api/events":
            self.events()
        elif path == "/api/store":
            self.json(self.server.store())
        else:
            self.static(path)

    def do_POST(self):
        if not self.trusted():
            return self.send_error(403)
        if self.path not in ("/api/cmd", "/api/store"):
            return self.send_error(404)
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        try:
            data = json.loads(body, parse_float=finite, parse_constant=not_json)
            if not isinstance(data, dict):
                raise ValueError("not an object")
            if self.path == "/api/cmd" and not isinstance(data.get("cmd"), str):
                raise ValueError("not a command")
        except ValueError as e:
            status, body = failure("invalid", str(e))
            return self.json(body, status)
        if self.path == "/api/store":
            return self.json(self.server.store(data))
        status, reply = self.server.on_command(data)
        try:
            self.json(reply, status)
        except (TypeError, ValueError) as e:
            status, body = failure("failed", f"the result is not JSON: {e}")
            self.json(body, status)

    def json(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.cors()
        self.end_headers()
        self.wfile.write(body)

    def cors(self):
        if self.server.origin:
            self.send_header("Access-Control-Allow-Origin", self.server.origin)

    def events(self):
        q = queue.Queue()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.cors()
        self.end_headers()
        with self.server.lock:
            self.server.clients.add(q)
            q.put(self.server.state)
        try:
            while True:
                try:
                    state = q.get(timeout=15)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    continue
                if state is None:
                    break
                self.wfile.write(f"event: state\ndata: {json.dumps(state)}\n\n".encode())
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            with self.server.lock:
                self.server.clients.discard(q)

    def static(self, path):
        root = self.server.web_root
        file = os.path.realpath(os.path.join(root, path.lstrip("/")))
        if not (file == root or file.startswith(root + os.sep)):
            return self.send_error(404)
        if os.path.isdir(file):
            file = os.path.join(file, "index.html")
        if not os.path.isfile(file):
            if "." in os.path.basename(path):
                return self.send_error(404)
            file = os.path.join(root, "index.html")  # Serve extensionless app routes.
        with open(file, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(file)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format, *args):
        pass
