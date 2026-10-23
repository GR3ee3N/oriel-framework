#!/usr/bin/env python3
"""Call Kodi JSON-RPC over TCP at localhost:9090 without HTTP or authentication.

    kodirpc.py <Method> [json-params]
    kodirpc.py --listen
    kodirpc.py --enable <add-on id>
"""
import codecs
import json
import socket
import sys
import time

HOST, PORT = "127.0.0.1", 9090


class Kodi:
    def __init__(self, timeout=10.0):
        self.sock = socket.create_connection((HOST, PORT), timeout=timeout)
        self.buf = ""
        self.dec = json.JSONDecoder()
        self.utf8 = codecs.getincrementaldecoder("utf-8")()
        self.next_id = 1

    def read(self):
        while True:
            text = self.buf.lstrip()
            if text:
                try:
                    msg, end = self.dec.raw_decode(text)
                    self.buf = text[end:]
                    return msg
                except json.JSONDecodeError:
                    pass
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("kodi closed the connection")
            self.buf += self.utf8.decode(chunk)

    def call(self, method, params=None):
        rid = self.next_id
        self.next_id += 1
        req = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            req["params"] = params
        self.sock.sendall(json.dumps(req).encode())
        while True:
            msg = self.read()
            if msg.get("id") == rid:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result")


def enable(addon):
    kodi = Kodi()
    deadline = time.monotonic() + 10
    while True:
        try:
            details = kodi.call("Addons.GetAddonDetails", {"addonid": addon, "properties": ["enabled"]})
            if details["addon"]["enabled"]:
                return
            kodi.call("Addons.SetAddonEnabled", {"addonid": addon, "enabled": True})
        except RuntimeError:
            pass  # Registration after UpdateLocalAddons can still be pending.
        if time.monotonic() >= deadline:
            raise RuntimeError(f"Kodi did not enable {addon}")
        time.sleep(0.25)


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "--enable":
        enable(sys.argv[2])
        return
    if len(sys.argv) >= 2 and sys.argv[1] == "--listen":
        k = Kodi(timeout=None)
        while True:
            msg = k.read()
            print(f"{time.monotonic():.3f} {json.dumps(msg)}", flush=True)
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    params = json.loads(sys.argv[2]) if len(sys.argv) > 2 else None
    print(json.dumps(Kodi().call(sys.argv[1], params), indent=1))


if __name__ == "__main__":
    main()
