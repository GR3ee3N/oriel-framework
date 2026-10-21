"""Media proxy in a separate process to avoid deadlocking Kodi's Python.

Kodi plays http://127.0.0.1:32500/<path>; the proxy adds the media source's
headers so tokens stay out of Kodi's URLs, logs and video database. Only
Kodi may use it: a request with another Host (DNS rebinding) or with an
Origin (a web page) is refused, as it would get the source's tokens.

Kodi's playback threads wait on proxy reads while the controller can hold
the Python lock inside a Kodi call waiting on those threads. The proxy
therefore runs under /usr/bin/python3 using only the standard library.

VideoApp sends sources on stdin as JSON lines:
{"base": "http://<server>:32400", "headers": {…}}. Command-line arguments
would expose secret headers through `ps`. Replies on stdout are `ready`
once listening, then `ok` or `error <why>` for each source update.
"""
import json
import os
import re
import select
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit

HERE = os.path.abspath(__file__)
ANSWER_TIMEOUT = 5
CHUNK = 256 * 1024
# Seconds before each attempt to fetch the rest of a body the source ended
# early; Kodi gives up on a read after about 20 s without data.
RESUME_DELAYS = (0, 1, 2, 4)


class ProxyProcess:
    """VideoApp's handle on the proxy process. set_source raises ValueError
    for a source the proxy rejects, RuntimeError when the proxy can't take one."""

    def __init__(self, port, log):
        self.port = port
        self.log = log
        self.process = None

    def start(self):
        process = subprocess.Popen(
            ["/usr/bin/python3", HERE, str(self.port)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, env={"PATH": "/usr/bin:/bin"}, start_new_session=True)
        # Its traceback (the port taken, say) goes to the log.
        threading.Thread(target=self._stderr, args=(process,), name="proxy-err", daemon=True).start()
        if self._answer(process) != "ready":
            process.kill()
            process.wait()
            raise RuntimeError("the media proxy did not start")
        self.process = process
        self.log("proxy started, pid", process.pid, "port", self.port)

    @staticmethod
    def _answer(process):
        """The proxy's next line, or None if none comes in time."""
        if not select.select([process.stdout], [], [], ANSWER_TIMEOUT)[0]:
            return None
        return process.stdout.readline().decode("utf-8", "replace").rstrip("\n") or None

    def _stderr(self, process):
        for line in process.stderr:
            self.log("proxy:", line.decode("utf-8", "replace").rstrip())

    def set_source(self, base, headers):
        if not self.process or self.process.poll() is not None:
            self.start()
        try:
            self.process.stdin.write((json.dumps({"base": base, "headers": headers}) + "\n").encode())
            self.process.stdin.flush()
        except OSError as e:
            raise RuntimeError(f"cannot set the media source: {e}") from e
        answer = self._answer(self.process)
        if answer is None:
            # A late answer would be taken for the next source's.
            self.process.kill()
            self.process.wait()
            self.process = None
            raise RuntimeError("the media proxy did not answer")
        if answer != "ok":
            raise ValueError(answer.removeprefix("error "))

    def stop(self):
        if not self.process:
            return
        try:
            self.process.stdin.close()  # the proxy exits on end of input
            self.process.wait(2)
        except (OSError, subprocess.TimeoutExpired):
            self.process.kill()
            self.process.wait()


def parse_source(line):
    s = json.loads(line)
    u = urlsplit(s["base"])
    if u.scheme not in ("http", "https") or not u.hostname:
        raise ValueError("the media source must be an http or https URL")
    headers = s.get("headers") or {}
    if not isinstance(headers, dict) or not all(isinstance(v, str) for v in headers.values()):
        raise ValueError("headers must be an object of strings")
    return u.scheme, u.hostname, u.port or (443 if u.scheme == "https" else 80), u.path.rstrip("/"), headers


def body_span(status, length, content_range):
    """The first and last byte of the file in a response body of `length`
    bytes, or None if unknown (no Content-Length, or not the file)."""
    if length is None:
        return None
    if status == 200:
        return 0, length - 1
    m = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+|\*)", content_range or "")
    if status == 206 and m and int(m[2]) - int(m[1]) + 1 == length:
        return int(m[1]), int(m[2])
    return None


def serve(port):
    import http.client
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    source = {}

    def log(*args):
        print(*args, file=sys.stderr, flush=True)

    class Upstream:
        """The connection to the source for one of Kodi's requests."""

        def __init__(self, s, method, url, headers):
            scheme, self.host, self.port, prefix, extra = s
            self.kind = http.client.HTTPSConnection if scheme == "https" else http.client.HTTPConnection
            self.method, self.url = method, prefix + url
            self.headers = {**headers, **extra}
            self.conn = None

        def open(self, more=None):
            self.close()
            self.conn = self.kind(self.host, self.port, timeout=30)
            self.conn.request(self.method, self.url, headers={**self.headers, **(more or {})})
            return self.conn.getresponse()

        def close(self):
            if self.conn:
                self.conn.close()

    def read(r, size):
        """Up to `size` bytes of r's body; nothing if the source let go."""
        try:
            return r.read(size)
        except (OSError, http.client.HTTPException):
            return b""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.forward("GET")

        def do_HEAD(self):
            self.forward("HEAD")

        def forward(self, method):
            listening = self.server.server_address[1]
            own = (f"127.0.0.1:{listening}", f"localhost:{listening}")
            if self.headers.get("Host") not in own or "Origin" in self.headers:
                return self.send_error(403)
            s = source.get("value")
            if not s:
                return self.send_error(503, "no media source")
            headers = {k.title(): v for k, v in self.headers.items() if k.lower() in ("range", "user-agent", "accept")}
            upstream = Upstream(s, method, self.path, headers)
            # The path, never a query: it can hold tokens.
            path = self.path.split("?")[0]
            try:
                r = upstream.open()
                if r.status >= 400:
                    log(f"{method} {path}: {r.status} {r.reason}")
                self.send_response(r.status, r.reason)
                for k, v in r.getheaders():
                    if k.lower() not in ("connection", "transfer-encoding", "keep-alive"):
                        self.send_header(k, v)
                self.send_header("Connection", "close")
                self.end_headers()
                if method == "GET":
                    self.copy(r, upstream, path)
            except (BrokenPipeError, ConnectionResetError, http.client.IncompleteRead):
                pass  # Kodi or the source let go (a stream replaced, say)
            except OSError as e:
                log(f"{method} {path} failed: {e}")
            finally:
                upstream.close()

        def copy(self, r, upstream, path):
            """Sends r's body to Kodi. A source can end a body early (Plex
            drops a user's streams while another of their playbacks runs).
            Kodi takes a short body read with nothing cached, as right after
            a seek, for the end of the file, so the rest of a file's body is
            fetched with a range request and Kodi gets it whole."""
            span = body_span(r.status, r.length, r.getheader("Content-Range"))
            if not span:
                while chunk := r.read(CHUNK):
                    self.wfile.write(chunk)
                return
            at, end = span
            # A range of a changed file must not be spliced in.
            validator = r.getheader("ETag") or r.getheader("Last-Modified")
            tries = resumed = 0
            try:
                while at <= end:
                    chunk = read(r, min(CHUNK, end - at + 1)) if r else b""
                    if chunk:
                        self.wfile.write(chunk)
                        at += len(chunk)
                        tries = 0
                        continue
                    if tries == len(RESUME_DELAYS):
                        log(f"GET {path}: the source ended the body at byte {at} of {end + 1}")
                        return
                    time.sleep(RESUME_DELAYS[tries])
                    tries += 1
                    more = {"Range": f"bytes={at}-{end}"}
                    if validator:
                        more["If-Range"] = validator
                    try:
                        r = upstream.open(more)
                    except (OSError, http.client.HTTPException):
                        r = None
                        continue
                    if (r.status, body_span(r.status, r.length, r.getheader("Content-Range"))) != (206, (at, end)):
                        log(f"GET {path}: the source did not resume at byte {at} ({r.status} {r.reason})")
                        return
                    resumed += 1
            finally:
                if resumed:
                    log(f"GET {path}: early ends resumed: {resumed}")

        def log_message(self, *args):
            pass

    class Server(ThreadingHTTPServer):
        daemon_threads = True
        allow_reuse_address = True

    server = Server(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print("ready", flush=True)
    for line in sys.stdin:
        try:
            source["value"] = parse_source(line)
            print("ok", flush=True)
        except (ValueError, KeyError, TypeError) as e:
            print("error", str(e).replace("\n", " "), flush=True)
    server.shutdown()


if __name__ == "__main__":
    serve(int(sys.argv[1]))
