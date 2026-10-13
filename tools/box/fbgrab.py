#!/usr/bin/env python3
"""Writes a framebuffer plane as PNG to stdout. Video isn't included, and in
HDR the image looks washed out.

    fbgrab.py [/dev/fb1]
"""
import fcntl
import os
import struct
import sys
import zlib

FBIOGET_VSCREENINFO = 0x4600
FBIOGET_FSCREENINFO = 0x4602


def main():
    fd = os.open(sys.argv[1] if len(sys.argv) > 1 else "/dev/fb1", os.O_RDONLY)
    var = fcntl.ioctl(fd, FBIOGET_VSCREENINFO, bytes(160))
    fix = fcntl.ioctl(fd, FBIOGET_FSCREENINFO, bytes(80))
    width, height, _, _, _, yoffset, bpp = struct.unpack_from("7I", var)
    line = struct.unpack_from("I", fix, 48)[0]
    if bpp != 32:
        sys.exit(f"{bpp} bpp, need 32")
    data = os.pread(fd, line * height, line * yoffset)
    rgb = bytearray(width * height * 3)
    rows = b"".join(data[y * line:y * line + width * 4] for y in range(height))
    rgb[0::3], rgb[1::3], rgb[2::3] = rows[2::4], rows[1::4], rows[0::4]
    stride = width * 3
    raw = b"".join(b"\0" + rgb[y * stride:(y + 1) * stride] for y in range(height))

    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))

    out = sys.stdout.buffer
    out.write(b"\x89PNG\r\n\x1a\n")
    out.write(chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)))
    out.write(chunk(b"IDAT", zlib.compress(raw, 1)))
    out.write(chunk(b"IEND", b""))


if __name__ == "__main__":
    main()
