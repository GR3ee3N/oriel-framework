"""The page's HTTP server on 127.0.0.1.

    GET  /             the app's resources/web
    GET  /api/events   server-sent `state` events
    POST /api/cmd      a command from the page; replies {"result"}, or
                       {"error", "code"} with the code's status (FAILURES)
    GET  /api/store    saved settings, plus `run`, an id for this start
    POST /api/store    merge settings; null removes a key

Never serve Kodi anything from here (media, say): Kodi's threads must not
wait on Kodi's Python. /api allows the dev server's origin, if there is one.

Requests with a foreign Origin are refused: CORS only hides the reply, and the
page's commands are simple requests that a foreign page can send too. The
Host check stops DNS rebinding, which would make a foreign page same-origin.
Requests without an Origin still require a Host of 127.0.0.1 or localhost
at the server's port.
"""
import json
import math
import mimetypes
import os
import queue
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# A failed command's code and HTTP status. invalid: it can't work as sent;
# unavailable: not now (no playback, say); failed: the handler broke;
# timeout: no reply in time, though it may still have run.
FAILURES = {"invalid": 400, "unavailable": 409, "failed": 500, "timeout": 504}


def finite(text):
    # 1e309 is valid JSON, but Python reads it as infinity.
    value = float(text)
    if not math.isfinite(value):
        raise ValueError(f"{text} is too large")
    return value


def not_json(constant):
    # Python's json takes NaN and Infinity, which JSON doesn't.
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
        # Takes a command, returns (status, body).
        self.on_command = on_command
        self.origin = origin
        self.lock = threading.Lock()
        self.clients = set()
        self.state = {}
        self.serving = False
        self.run = uuid.uuid4().hex

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
        if self.serving:  # shutdown() waits for serve_forever() forever
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
            self.json({**self.server.store(), "run": self.server.run})
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
            file = os.path.join(root, "index.html")  # an app route
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
