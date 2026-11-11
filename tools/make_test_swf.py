#!/usr/bin/env python3
"""
make_test_swf.py — Generate a tiny, valid .swf file for testing
swf_to_cpp.py without needing a real Flash authoring tool.

The generated file contains:
  * a 16x16 lossless bitmap (DefineBitsLossless2, format 5 / ARGB),
  * a 2-frame sprite that slides the bitmap,
  * a 3-frame main timeline placing the sprite and the bitmap directly.

Usage: python3 tools/make_test_swf.py tests/fixtures/simple.swf
"""

import os
import struct
import sys
import zlib


class BitWriter:
    def __init__(self):
        self.bits = []

    def w(self, value, n):
        for i in range(n - 1, -1, -1):
            self.bits.append((value >> i) & 1)

    def ws(self, value, n):
        if value < 0:
            value += 1 << n
        self.w(value, n)

    def bytes(self):
        out = bytearray()
        cur = 0
        n = 0
        for b in self.bits:
            cur = (cur << 1) | b
            n += 1
            if n == 8:
                out.append(cur)
                cur = 0
                n = 0
        if n:
            out.append(cur << (8 - n))
        return bytes(out)


def tag(code, payload):
    n = len(payload)
    if n < 0x3F:
        return struct.pack("<H", (code << 6) | n) + payload
    return struct.pack("<HI", (code << 6) | 0x3F, n) + payload


def rect(xmin, xmax, ymin, ymax):
    vals = [xmin, xmax, ymin, ymax]
    # signed n-bit fields need bit_length+1 bits for positive values too
    nbits = max(max(abs(v).bit_length() + 1, 1) for v in vals)
    bw = BitWriter()
    bw.w(nbits, 5)
    for v in vals:
        bw.ws(v, nbits)
    return bw.bytes()


def matrix(tx=0, ty=0):
    """Translate-only MATRIX record."""
    nbits = max(tx.bit_length() + 1, ty.bit_length() + 1, 1)
    bw = BitWriter()
    bw.w(0, 1)          # has scale
    bw.w(0, 1)          # has rotate
    bw.w(nbits, 5)      # n translate bits
    bw.ws(tx, nbits)
    bw.ws(ty, nbits)
    return bw.bytes()


def place_object2(depth, char_id, tx, ty, name=None, move=False):
    flags = 0x02 | 0x04          # has character + has matrix
    if name is not None:
        flags |= 0x20
    if move:
        flags |= 0x01
    p = bytes([flags]) + struct.pack("<H", depth)
    if char_id is not None:
        p += struct.pack("<H", char_id)
    p += matrix(tx, ty)
    if name is not None:
        p += name.encode() + b"\x00"
    return tag(26, p)


def define_bits_lossless2(char_id, w, h, pixels_argb):
    """pixels_argb: bytes of w*h*4 in A,R,G,B order (premultiplied ok)."""
    rows = bytearray()
    for y in range(h):
        rows += pixels_argb[y * w * 4:(y + 1) * w * 4]
    body = struct.pack("<H", char_id) + bytes([5]) + struct.pack("<HH", w, h)
    body += zlib.compress(bytes(rows))
    return tag(36, body)


def define_sprite(sid, frame_count, inner_tags):
    body = struct.pack("<HH", sid, frame_count) + b"".join(inner_tags)
    body += tag(0, b"")
    return tag(39, body)


def main(out_path):
    W, H = 16, 16
    # checkerboard-ish ARGB image: red/blue quadrants
    px = bytearray()
    for y in range(H):
        for x in range(W):
            if (x < 8) == (y < 8):
                px += bytes((255, 255, 0, 0))    # A,R,G,B -> red
            else:
                px += bytes((255, 0, 0, 255))    # blue

    tags = []
    tags.append(tag(9, bytes((255, 255, 255))))         # bg white
    tags.append(define_bits_lossless2(1, W, H, bytes(px)))
    # sprite id=2, 2 frames: image at x=0 and x=40 (twips*20 -> px? we use
    # twips: 40 twips = 2px... use 200 twips = 10px)
    sprite = define_sprite(2, 2, [
        place_object2(1, 1, 0, 0),
        tag(1, b""),                                     # ShowFrame
        place_object2(1, 1, 200, 100, move=True),       # move right/down
        tag(1, b""),
    ])
    tags.append(sprite)
    # main timeline: 3 frames
    tags.append(place_object2(1, 2, 20, 20, name="hero"))   # sprite at 1,1px
    tags.append(place_object2(2, 1, 150, 150))              # bitmap at 7.5px
    tags.append(tag(1, b""))
    tags.append(tag(1, b""))                                # frame 2 (no change)
    tags.append(tag(43, b"final frame\x00"))
    tags.append(tag(1, b""))                                # frame 3
    tags.append(tag(0, b""))

    stage = rect(0, 550 * 20, 0, 400 * 20)   # 550x400 px
    body = stage + struct.pack("<H", 24 * 256) + struct.pack("<H", 3)
    body += b"".join(tags)

    header = b"FWS" + bytes([8]) + struct.pack("<I", 8 + len(body))
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "wb") as f:
        f.write(header + body)
    print(f"wrote {out_path} ({8 + len(body)} bytes)")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "simple.swf")
