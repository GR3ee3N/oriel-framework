from contextlib import ExitStack
from http.client import IncompleteRead
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import re
import socket
import struct
import subprocess
import threading
import unittest
from unittest.mock import patch
from urllib.request import Request, urlopen

from .kodi import kodi

FILE = bytes(range(256)) * 4096  # 1 MiB


class Source(BaseHTTPRequestHandler):
    """A file on a media server that ends the first bodies early. The server
    has drops (bytes sent before ending each body, in order), reset (ends
    with a reset rather than a close), etag and seen (each request's Range
    and If-Range)."""

    def do_GET(self):
        server = self.server
        server.seen.append((self.headers["Range"], self.headers["If-Range"]))
        start, end = 0, len(FILE) - 1
        ranged = re.fullmatch(r"bytes=(\d+)-(\d*)", self.headers["Range"] or "")
        if ranged and self.headers["If-Range"] in (None, server.etag):
            start, end = int(ranged[1]), int(ranged[2] or end)
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(FILE)}")
        else:
            self.send_response(200)
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("ETag", server.etag)
        self.end_headers()
        body = FILE[start:end + 1]
        if server.drops:
            body = body[:server.drops.pop(0)]
            if server.reset:
                self.wfile.write(body)
                self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                return
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class Server(ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        pass  # the proxy let go of a body it would not splice in


class ProxyResumeTests(unittest.TestCase):
    def setUp(self):
        _, video, _ = self.enterContext(kodi())
        processes = self.enterContext(ExitStack())
        spawn = subprocess.Popen
        self.enterContext(patch.object(subprocess, "Popen", side_effect=lambda *args, **kwargs:
                                       processes.enter_context(spawn(*args, **kwargs))))
        with socket.socket() as free:
            free.bind(("127.0.0.1", 0))
            self.port = free.getsockname()[1]
        self.logged = []
        proxy = video.ProxyProcess(self.port, lambda *args: self.logged.append(" ".join(map(str, args))))
        self.addCleanup(proxy.stop)
        self.source = Server(("127.0.0.1", 0), Source)
        self.source.drops, self.source.reset, self.source.etag, self.source.seen = [], False, '"v1"', []
        self.addCleanup(self.source.server_close)
        threading.Thread(target=self.source.serve_forever, daemon=True).start()
        self.addCleanup(self.source.shutdown)
        proxy.set_source(f"http://127.0.0.1:{self.source.server_address[1]}", {})

    def fetch(self, range_=None):
        """What Kodi gets: the status and the body."""
        headers = {"Range": range_} if range_ else {}
        with urlopen(Request(f"http://127.0.0.1:{self.port}/file.mkv", headers=headers), timeout=10) as response:
            return response.status, response.read()

    def test_body_ended_early_is_resumed_from_where_it_ended(self):
        for reset in (False, True):
            for range_, start, end in ((None, 0, len(FILE) - 1), ("bytes=0-", 0, len(FILE) - 1),
                                       ("bytes=300000-", 300000, len(FILE) - 1), ("bytes=1000-599999", 1000, 599999)):
                with self.subTest(reset=reset, range=range_):
                    self.source.reset, self.source.drops, self.source.seen = reset, [100000, 50000], []
                    status, body = self.fetch(range_)
                    self.assertEqual(status, 206 if range_ else 200)
                    self.assertEqual(body, FILE[start:end + 1])
                    self.assertEqual(self.source.seen, [
                        (range_, None),
                        (f"bytes={start + 100000}-{end}", '"v1"'),
                        (f"bytes={start + 150000}-{end}", '"v1"'),
                    ])

    def test_changed_file_is_not_spliced_in(self):
        self.source.drops = [100000]

        class Changing(Source):
            def do_GET(self):
                Source.do_GET(self)
                self.server.etag = '"v2"'

        self.source.RequestHandlerClass = Changing
        with self.assertRaises(IncompleteRead) as short:
            self.fetch("bytes=0-")
        self.assertEqual(short.exception.partial, FILE[:100000])
        self.assertEqual(self.source.seen, [("bytes=0-", None), ("bytes=100000-1048575", '"v1"')])

    def test_whole_body_needs_one_request(self):
        self.assertEqual(self.fetch("bytes=5-"), (206, FILE[5:]))
        self.assertEqual(self.source.seen, [("bytes=5-", None)])


if __name__ == "__main__":
    unittest.main()
