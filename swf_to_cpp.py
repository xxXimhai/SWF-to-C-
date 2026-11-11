#!/usr/bin/env python3
"""
swf_to_cpp.py — Extract assets and timeline metadata from a .swf file and
generate a buildable, self-contained native C++ player (Linux/Windows).

This is NOT an ActionScript-to-C++ translator. It is a pipeline that:

  1. Parses the SWF container (FWS/CWS/ZWS).
  2. Extracts bitmap characters (JPEG / lossless) to PNG/JPEG files.
  3. Rasterizes vector shape characters to PNG files.
  4. Extracts sound characters (MP3 / PCM->WAV) and MP3 sound streams.
  5. Reconstructs the display list per frame (PlaceObject / RemoveObject /
     ShowFrame) into a compact timeline description, including matrices
     and color transforms.
  6. Emits a dependency-free CMake project (software renderer, X11 on
     Linux / Win32 on Windows, miniaudio audio) that plays the extracted
     frames back.

Usage:
    python3 swf_to_cpp.py input.swf output_dir
"""

import argparse
import binascii
import io
import json
import lzma
import math
import os
import shutil
import struct
import sys
import zlib

# ---------------------------------------------------------------------------
# Bit-level reader (SWF stores bit fields MSB-first inside bytes)
# ---------------------------------------------------------------------------


class BitReader:
    """Reads bits MSB-first from a bytes buffer."""

    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0  # bit position

    def read_bits(self, n: int) -> int:
        v = 0
        for _ in range(n):
            byte = self.data[self.pos >> 3]
            bit = (byte >> (7 - (self.pos & 7))) & 1
            v = (v << 1) | bit
            self.pos += 1
        return v

    def read_sbits(self, n: int) -> int:
        v = self.read_bits(n)
        if n and (v >> (n - 1)):
            v -= 1 << n
        return v

    def align(self):
        self.pos = (self.pos + 7) & ~7

    def byte_len(self) -> int:
        return (self.pos + 7) // 8


# ---------------------------------------------------------------------------
# Low level record readers
# ---------------------------------------------------------------------------


def read_rect(br: BitReader):
    """Returns (xmin, xmax, ymin, ymax) in twips."""
    nbits = br.read_bits(5)
    vals = [br.read_sbits(nbits) for _ in range(4)]
    br.align()
    return vals


def read_matrix(br: BitReader):
    """Returns dict with translate (twips) and scale/skew (16.16 fixed)."""
    if br.read_bits(1):  # has scale
        n = br.read_bits(5)
        scale_x = br.read_sbits(n) / 65536.0
        scale_y = br.read_sbits(n) / 65536.0
    else:
        scale_x = scale_y = 1.0
    if br.read_bits(1):  # has rotate/skew
        n = br.read_bits(5)
        skew0 = br.read_sbits(n) / 65536.0
        skew1 = br.read_sbits(n) / 65536.0
    else:
        skew0 = skew1 = 0.0
    n = br.read_bits(5)
    return {"tx": br.read_sbits(n), "ty": br.read_sbits(n),
            "sx": scale_x, "sy": scale_y, "k0": skew0, "k1": skew1}


def read_cxform(br: BitReader, alpha=True):
    """Parse CXFORM/CXFORMWITHALPHA.

    Returns (mult, add): 4-element lists of floats. mult is the 8.8 fixed
    multiply factor (1.0 = identity), add the -1..1 additive term.
    """
    has_add = br.read_bits(1)
    has_mult = br.read_bits(1)
    nbits = br.read_bits(4)
    nch = 4 if alpha else 3
    mult = [1.0] * 4
    add = [0.0] * 4
    if has_mult:
        for i in range(nch):
            mult[i] = br.read_sbits(nbits) / 256.0
    if has_add:
        for i in range(nch):
            add[i] = br.read_sbits(nbits) / 255.0
    return mult, add


def read_cstring(buf: bytes, off: int):
    end = buf.index(b"\x00", off)
    return buf[off:end].decode("utf-8", "replace"), end + 1


# ---------------------------------------------------------------------------
# PNG writer (pure python, no dependencies)
# ---------------------------------------------------------------------------


def _png_chunk(tag: bytes, data: bytes) -> bytes:
    return (struct.pack(">I", len(data)) + tag + data +
            struct.pack(">I", binascii.crc32(tag + data) & 0xFFFFFFFF))


def _png_bytes(w, h, color_type, scanlines, palette=b"", trns=b""):
    buf = io.BytesIO()
    buf.write(b"\x89PNG\r\n\x1a\n")
    buf.write(_png_chunk(b"IHDR",
                         struct.pack(">IIBBBBB", w, h, 8, color_type, 0, 0, 0)))
    if palette:
        buf.write(_png_chunk(b"PLTE", palette))
    if trns:
        buf.write(_png_chunk(b"tRNS", trns))
    buf.write(_png_chunk(b"IDAT", zlib.compress(scanlines, 6)))
    buf.write(_png_chunk(b"IEND", b""))
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Lossless bitmap decoding (DefineBitsLossless=20, DefineBitsLossless2=36)
# ---------------------------------------------------------------------------


def decode_lossless(tag_id: int, body: bytes):
    """Returns (width, height, png_bytes, char_id)."""
    off = 0
    char_id = struct.unpack_from("<H", body, off)[0]
    off += 2
    fmt = body[off]
    off += 1
    width, height = struct.unpack_from("<HH", body, off)
    off += 4

    has_alpha = (tag_id == 36)
    if fmt == 3:
        ncolors = body[off] + 1
        off += 1
        raw = zlib.decompress(body[off:])
        entry = 4 if has_alpha else 3
        table = raw[:ncolors * entry]
        stride = (width + 3) & ~3          # index rows padded to 4 bytes
        idx = raw[ncolors * entry:ncolors * entry + stride * height]

        plte = bytearray()
        trns = bytearray()
        for i in range(ncolors):
            o = i * entry
            if has_alpha:
                a, r, g, b = table[o], table[o + 1], table[o + 2], table[o + 3]
                plte += bytes((r, g, b))
                trns.append(a)
            else:
                plte += table[o:o + 3]

        # indices for PNG need the original index rows (unpadded width)
        idxrows = bytearray()
        for y in range(height):
            idxrows.append(0)
            idxrows += idx[y * stride:y * stride + width]
        png = _png_bytes(width, height, 3, bytes(idxrows),
                         palette=bytes(plte),
                         trns=bytes(trns) if trns else b"")
        return width, height, png, char_id

    raw = zlib.decompress(body[off:])
    rows = bytearray()
    if fmt == 4:
        # 15-bit pixels: 0RRRRRGGGGGBBBBB stored little-endian
        stride = ((width * 2) + 3) & ~3
        for y in range(height):
            rows.append(0)
            base = y * stride
            for x in range(width):
                p = raw[base + x * 2] | (raw[base + x * 2 + 1] << 8)
                r = ((p >> 10) & 0x1F) * 255 // 31
                g = ((p >> 5) & 0x1F) * 255 // 31
                b = (p & 0x1F) * 255 // 31
                rows += bytes((r, g, b))
        return width, height, _png_bytes(width, height, 2, bytes(rows)), char_id

    if fmt == 5:
        stride = width * 4  # already 4-byte aligned
        if has_alpha:
            for y in range(height):
                rows.append(0)
                base = y * stride
                for x in range(width):
                    o = base + x * 4
                    a, r, g, b = raw[o], raw[o + 1], raw[o + 2], raw[o + 3]
                    if 0 < a < 255:  # unpremultiply
                        r = min(r * 255 // a, 255)
                        g = min(g * 255 // a, 255)
                        b = min(b * 255 // a, 255)
                    rows += bytes((r, g, b, a))
            return width, height, _png_bytes(width, height, 6, bytes(rows)), char_id
        for y in range(height):
            rows.append(0)
            base = y * stride
            for x in range(width):
                o = base + x * 4
                rows += raw[o + 1:o + 4]  # skip pad byte -> RGB
        return width, height, _png_bytes(width, height, 2, bytes(rows)), char_id

    raise ValueError(f"unsupported lossless format {fmt}")


# ---------------------------------------------------------------------------
# JPEG helpers
# ---------------------------------------------------------------------------


def reconstruct_jpeg(jpeg_data: bytes, tables: bytes = None) -> bytes:
    """Merge JPEGTables into a DefineBits payload and strip the bogus
    FFD9 FFD8 sequence some SWF<8 files contain."""
    out = bytearray()
    if tables:
        t = tables
        if t[:2] == b"\xff\xd8":
            t = t[2:]
        if t[-2:] == b"\xff\xd9":
            t = t[:-2]
        d = jpeg_data
        if d[:2] == b"\xff\xd8":
            d = d[2:]
        out += b"\xff\xd8" + t + d
    else:
        out += jpeg_data
    res = bytearray()
    k = 0
    while k < len(out):
        if k + 3 < len(out) and out[k] == 0xFF and out[k + 1] == 0xD9 \
                and out[k + 2] == 0xFF and out[k + 3] == 0xD8:
            k += 4
            continue
        res.append(out[k])
        k += 1
    if res[:2] != b"\xff\xd8":
        res[0:0] = b"\xff\xd8"
    if res[-2:] != b"\xff\xd9":
        res += b"\xff\xd9"
    return bytes(res)


def sniff_image_ext(data: bytes) -> str:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:3] == b"GIF":
        return "gif"
    if data[:2] == b"\xff\xd8":
        return "jpg"
    return "bin"


def _jpeg_size(data: bytes):
    """Return (width, height) of a JPEG by scanning for an SOF marker."""
    n = len(data)
    i = 2  # skip SOI
    while i + 4 <= n:
        if data[i] != 0xFF:
            i += 1
            continue
        j = i + 1
        while j < n and data[j] == 0xFF:
            j += 1
        if j >= n:
            break
        mk = data[j]
        if mk == 0x00 or mk == 0x01 or 0xD0 <= mk <= 0xD9:
            i = j + 1
            continue
        if mk == 0xDA:      # SOS: entropy data follows, dims already found
            break
        if j + 9 > n:
            break
        seglen = struct.unpack_from(">H", data, j + 1)[0]
        if mk in (0xC0, 0xC1, 0xC2):
            h = struct.unpack_from(">H", data, j + 4)[0]
            w = struct.unpack_from(">H", data, j + 6)[0]
            return w, h
        i = j + 1 + seglen
    return None


# ---------------------------------------------------------------------------
# Vector shape parsing (DefineShape/2/3/4) + CPU rasterization.
#
# Style arrays are byte aligned; shape records form a continuous MSB-first
# bit stream. Style indices in records are 1-based (0 = no style).
# ---------------------------------------------------------------------------


def _shape_color(buf, off, has_alpha):
    """RGB or RGBA color record -> (r, g, b, a) with a in 0..255."""
    if has_alpha:
        return (buf[off], buf[off + 1], buf[off + 2], buf[off + 3]), off + 4
    return (buf[off], buf[off + 1], buf[off + 2], 255), off + 3


def _read_gradient(buf, off, has_alpha):
    """GRADIENT/FOCALGRADIENT record (minus focal point) -> (dict, new_off)."""
    flags = buf[off]
    off += 1
    spread = flags >> 6
    interp = (flags >> 4) & 0x3
    n = flags & 0x0F
    stops = []
    for _ in range(n):
        ratio = buf[off] / 255.0
        col, off = _shape_color(buf, off + 1, has_alpha)
        stops.append((ratio, col))
    return {"spread": spread, "interp": interp, "stops": stops}, off


def _read_matrix_at(buf, off):
    br = BitReader(buf[off:])
    m = read_matrix(br)
    return m, off + br.byte_len()


def _read_fill_style(buf, off, version):
    """One FILLSTYLE record -> (style_dict, new_off)."""
    has_alpha = version >= 3
    t = buf[off]
    off += 1
    if t == 0x00:                                    # solid
        col, off = _shape_color(buf, off, has_alpha)
        return {"type": "solid", "color": col}, off
    if t in (0x10, 0x12, 0x13):                      # gradients
        m, off = _read_matrix_at(buf, off)
        g, off = _read_gradient(buf, off, has_alpha)
        st = {"type": "grad",
              "kind": {0x10: "linear", 0x12: "radial", 0x13: "focal"}[t],
              "matrix": m, "grad": g}
        if t == 0x13:                               # focal point FIXED8
            st["focal"] = struct.unpack_from("<h", buf, off)[0] / 256.0
            off += 2
        return st, off
    if 0x40 <= t <= 0x43:                            # bitmap fill
        bmp = struct.unpack_from("<H", buf, off)[0]
        off += 2
        m, off = _read_matrix_at(buf, off)
        return {"type": "bitmap", "bmp": bmp, "matrix": m,
                "smooth": t in (0x40, 0x41)}, off
    raise ValueError("fill style type 0x%02x" % t)


def _read_line_style(buf, off, version):
    """One LINESTYLE record -> (style_dict, new_off)."""
    width = struct.unpack_from("<H", buf, off)[0]
    off += 2
    if version <= 3:
        col, off = _shape_color(buf, off, version >= 3)
        return {"width": width / 20.0, "paint": ("solid", col)}, off
    # LINESTYLE2 (DefineShape4): 16 bits of flags, then fill or color
    flags = struct.unpack_from(">H", buf, off)[0]
    off += 2
    join = (flags >> 12) & 0x3
    has_fill = (flags >> 11) & 0x1
    if join == 2:                                    # miter limit FIXED8
        off += 2
    if has_fill:
        fill, off = _read_fill_style(buf, off, version)
        paint = ("fill", fill)
    else:
        col, off = _shape_color(buf, off, True)
        paint = ("solid", col)
    return {"width": width / 20.0, "paint": paint}, off


def _read_style_array(buf, off, version, kind):
    """FILLSTYLEARRAY/LINESTYLEARRAY -> (styles, new_off).

    styles[0] is None: record index 0 means "no style".
    """
    n = buf[off]
    off += 1
    if n == 0xFF and (kind == "line" or version != 1):
        n = struct.unpack_from("<H", buf, off)[0]
        off += 2
    reader = _read_fill_style if kind == "fill" else _read_line_style
    styles = [None]
    for _ in range(n):
        s, off = reader(buf, off, version)
        styles.append(s)
    return styles, off


def _shape_records(br, data, base_off, version, fills, lines,
                   fill_bits, line_bits):
    """Parse SHAPERECORDS -> (edges, fills, lines).

    br reads bits from data[base_off:]; br.pos is relative to that slice.
    """
    edges = []
    f0 = f1 = ln = 0
    cx = cy = 0
    while True:
        if br.read_bits(1) == 0:                      # non-edge record
            newstyles = br.read_bits(1)
            has_line = br.read_bits(1)
            has_f1 = br.read_bits(1)
            has_f0 = br.read_bits(1)
            has_move = br.read_bits(1)
            if not (newstyles or has_line or has_f1 or has_f0 or has_move):
                break                                 # EndShapeRecord
            # value order per spec/FFDec: MoveTo, Fill0, Fill1, Line
            if has_move:
                mb = br.read_bits(5)
                cx += br.read_sbits(mb)
                cy += br.read_sbits(mb)
                edges.append({"kind": "move", "x": cx, "y": cy})
            if has_f0:
                f0 = br.read_bits(fill_bits)
            if has_f1:
                f1 = br.read_bits(fill_bits)
            if has_line:
                ln = br.read_bits(line_bits)
            if newstyles:
                br.align()
                off = base_off + (br.pos >> 3)
                fills, off = _read_style_array(data, off, version, "fill")
                lines, off = _read_style_array(data, off, version, "line")
                br2 = BitReader(data[off:])
                fill_bits = br2.read_bits(4)
                line_bits = br2.read_bits(4)
                br.pos = (off + br2.byte_len() - base_off) * 8
            continue
        # edge record
        if br.read_bits(1):                           # straight edge
            nbits = br.read_bits(4)
            if br.read_bits(1):                       # general line
                dx = br.read_sbits(nbits + 2)
                dy = br.read_sbits(nbits + 2)
            elif br.read_bits(1):                     # vertical line
                dx, dy = 0, br.read_sbits(nbits + 2)
            else:                                     # horizontal line
                dx, dy = br.read_sbits(nbits + 2), 0
            nx, ny = cx + dx, cy + dy
            edges.append({"kind": "line", "f0": f0, "f1": f1, "line": ln,
                          "pts": ((cx, cy), (nx, ny))})
            cx, cy = nx, ny
        else:                                         # curved edge
            nbits = br.read_bits(4)
            cdx = br.read_sbits(nbits + 2)
            cdy = br.read_sbits(nbits + 2)
            adx = br.read_sbits(nbits + 2)
            ady = br.read_sbits(nbits + 2)
            nx, ny = cx + adx, cy + ady
            edges.append({"kind": "quad", "f0": f0, "f1": f1, "line": ln,
                          "pts": ((cx, cy), (cx + cdx, cy + cdy), (nx, ny))})
            cx, cy = nx, ny
    return edges, fills, lines


def parse_shape(tag_id, p):
    """Fully parse a DefineShape/2/3/4 tag.

    Returns dict with bounds (twips), style arrays and edge records,
    or None on failure.
    """
    version = {2: 1, 22: 2, 32: 3, 83: 4}.get(tag_id)
    if version is None:
        return None
    try:
        sid = struct.unpack_from("<H", p, 0)[0]
        br = BitReader(p[2:])
        xmin, xmax, ymin, ymax = read_rect(br)
        br.align()
        off = 2 + br.byte_len()
        if version == 4:
            # DefineShape4 (SHAPE4): a second RECT (edgeBounds) followed
            # by 8 flag bits (5 reserved + fillWinding + nonScaling +
            # scaling strokes) precede the style arrays.
            br = BitReader(p[off:])
            read_rect(br)
            br.align()
            off += br.byte_len() + 1
        # SHAPEWITHSTYLE (all DefineShape* tags): style arrays precede
        # the 4+4 bit style-index counts.
        fills, off = _read_style_array(p, off, version, "fill")
        lines, off = _read_style_array(p, off, version, "line")
        br = BitReader(p[off:])
        fill_bits = br.read_bits(4)
        line_bits = br.read_bits(4)
        edges, fills, lines = _shape_records(br, p, off, version, fills,
                                             lines, fill_bits, line_bits)
        return {"sid": sid, "version": version,
                "bounds": (xmin, xmax, ymin, ymax),
                "fills": fills, "lines": lines, "edges": edges}
    except Exception:
        return None


# --- PNG decoding (for bitmap fills sourced from our own PNG output) -------


def decode_png_rgba(data):
    """Decode a PNG to (w, h, rgba_bytes). Supports 8-bit gray/RGB/palette/
    RGBA, non-interlaced, all scanline filters. Returns None on failure."""
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    pos = 8
    idat = bytearray()
    plte = b""
    trns = b""
    w = h = bitdepth = ctype = interlace = None
    try:
        while pos + 8 <= len(data):
            ln = struct.unpack_from(">I", data, pos)[0]
            tag = data[pos + 4:pos + 8]
            chunk = data[pos + 8:pos + 8 + ln]
            pos += 12 + ln
            if tag == b"IHDR":
                w, h, bitdepth, ctype, _comp, _filt, interlace = \
                    struct.unpack(">IIBBBBB", chunk)
            elif tag == b"PLTE":
                plte = chunk
            elif tag == b"tRNS":
                trns = chunk
            elif tag == b"IDAT":
                idat += chunk
            elif tag == b"IEND":
                break
        if (w is None or bitdepth != 8 or interlace != 0
                or ctype not in (0, 2, 3, 6)):
            return None
        channels = {0: 1, 2: 3, 3: 1, 6: 4}[ctype]
        bpp = channels
        stride = w * channels
        raw = zlib.decompress(bytes(idat))
        if len(raw) < h * (stride + 1):
            return None
        rows = []
        prev = bytearray(stride)
        rp = 0
        for _y in range(h):
            f = raw[rp]
            rp += 1
            line = bytearray(raw[rp:rp + stride])
            rp += stride
            if f == 1:
                for i in range(bpp, stride):
                    line[i] = (line[i] + line[i - bpp]) & 0xFF
            elif f == 2:
                for i in range(stride):
                    line[i] = (line[i] + prev[i]) & 0xFF
            elif f == 3:
                for i in range(stride):
                    a = line[i - bpp] if i >= bpp else 0
                    line[i] = (line[i] + ((a + prev[i]) >> 1)) & 0xFF
            elif f == 4:
                for i in range(stride):
                    a = line[i - bpp] if i >= bpp else 0
                    b = prev[i]
                    c = prev[i - bpp] if i >= bpp else 0
                    pp = a + b - c
                    pa, pb, pc = abs(pp - a), abs(pp - b), abs(pp - c)
                    pr = a if (pa <= pb and pa <= pc) else \
                        (b if pb <= pc else c)
                    line[i] = (line[i] + pr) & 0xFF
            elif f != 0:
                return None
            rows.append(bytes(line))
            prev = line
    except Exception:
        return None

    out = bytearray(w * h * 4)
    if ctype == 6:
        flat = bytearray()
        for r_ in rows:
            flat += r_
        out = flat
    elif ctype == 2:
        for y in range(h):
            s = y * stride
            d = y * w * 4
            for x in range(w):
                o = s + x * 3
                out[d:d + 4] = bytes((rows[y][o], rows[y][o + 1],
                                      rows[y][o + 2], 255))
                d += 4
    elif ctype == 0:
        for y in range(h):
            s = y * stride
            d = y * w * 4
            for x in range(w):
                g = rows[y][s + x]
                a = trns[1] if len(trns) >= 2 and g == trns[0] else 255
                out[d:d + 4] = bytes((g, g, g, a))
                d += 4
    elif ctype == 3:
        nplte = len(plte) // 3
        for y in range(h):
            s = y * stride
            d = y * w * 4
            for x in range(w):
                idx = rows[y][s + x]
                if idx < nplte:
                    o = idx * 3
                    r_, g_, b_ = plte[o], plte[o + 1], plte[o + 2]
                else:
                    r_ = g_ = b_ = 0
                a = trns[idx] if idx < len(trns) else 255
                out[d:d + 4] = bytes((r_, g_, b_, a))
                d += 4
    return w, h, bytes(out)


# --- rasterizer -------------------------------------------------------------


def _flatten_quad(p0, pc, p1, tol, out, depth=0):
    x0, y0 = p0
    xc, yc = pc
    x1, y1 = p1
    dx, dy = x1 - x0, y1 - y0
    l2 = dx * dx + dy * dy
    if l2 == 0:
        d = math.hypot(xc - x0, yc - y0)
    else:
        d = abs(dy * xc - dx * yc + x1 * y0 - y1 * x0) / math.sqrt(l2)
    if d <= tol or depth > 12:
        out.append(p1)
        return
    ax = (x0 + xc) * 0.5
    ay = (y0 + yc) * 0.5
    bx = (xc + x1) * 0.5
    by = (yc + y1) * 0.5
    mx = (ax + bx) * 0.5
    my = (ay + by) * 0.5
    _flatten_quad(p0, (ax, ay), (mx, my), tol, out, depth + 1)
    _flatten_quad((mx, my), (bx, by), p1, tol, out, depth + 1)


def _coverage(segs, w, h, ss):
    """Nonzero-winding coverage of directed segments.

    segs: list of (x0, y0, x1, y1) in device pixels.
    Returns list of w*h floats in 0..1.
    """
    cov = [0.0] * (w * h)
    nrows = h * ss
    scale = float(ss)
    for sy in range(nrows):
        y = (sy + 0.5) / scale
        xs = []
        for x0, y0, x1, y1 in segs:
            if (y0 <= y < y1) or (y1 <= y < y0):
                xs.append((x0 + (y - y0) * (x1 - x0) / (y1 - y0),
                           1 if y1 > y0 else -1))
        if not xs:
            continue
        xs.sort()
        dy = sy // ss
        base = dy * w
        wind = 0
        prev_x = 0.0
        for i, (x, s) in enumerate(xs):
            if i and wind:
                a = prev_x * scale
                b = x * scale
                if b > a:
                    c0 = int(a) // ss
                    c1 = int(b - 1e-9) // ss
                    for c in range(max(c0, 0), min(c1, w - 1) + 1):
                        lo = max(a, c * scale)
                        hi = min(b, (c + 1) * scale)
                        if hi > lo:
                            cov[base + c] += (hi - lo) / scale
            wind += s
            prev_x = x
    inv = 1.0 / ss
    return [c * inv for c in cov]


def _stamp_circle(cov, w, h, ss, cx, cy, r):
    """Add a filled circle of radius r (px) centered at (cx, cy) to cov."""
    x0 = max(0, int(cx - r - 1))
    x1 = min(w - 1, int(cx + r + 1))
    y0 = max(0, int(cy - r - 1))
    y1 = min(h - 1, int(cy + r + 1))
    r2 = r * r
    for py in range(y0, y1 + 1):
        for px in range(x0, x1 + 1):
            cnt = 0
            for sy in range(ss):
                ddy = (py + (sy + 0.5) / ss) - cy
                for sx in range(ss):
                    ddx = (px + (sx + 0.5) / ss) - cx
                    if ddx * ddx + ddy * ddy <= r2:
                        cnt += 1
            if cnt:
                i = py * w + px
                cov[i] = min(1.0, cov[i] + cnt / (ss * ss))


def _stroke_coverage(runs, w, h, ss):
    """runs: list of (width_px, [points]) -> coverage list."""
    cov = [0.0] * (w * h)
    for width, pts in runs:
        if len(pts) < 2 or width <= 0:
            continue
        r = width * 0.5
        step = max(0.35, r * 0.6)
        for i in range(len(pts) - 1):
            x0, y0 = pts[i]
            x1, y1 = pts[i + 1]
            seglen = math.hypot(x1 - x0, y1 - y0)
            if seglen == 0:
                continue
            n = max(1, int(seglen / step))
            for k in range(n + 1):
                t = k / n
                _stamp_circle(cov, w, h, ss, x0 + (x1 - x0) * t,
                              y0 + (y1 - y0) * t, r)
    return cov


def _mat_apply(m, x, y):
    """Apply SWF matrix dict (shape units) to point -> (u, v)."""
    return (m["sx"] * x + m["k0"] * y + m["tx"],
            m["k1"] * x + m["sy"] * y + m["ty"])


def _gradient_lut(grad):
    """Precompute t in [0,1] -> (r,g,b,a) for a gradient (256 entries)."""
    stops = grad["stops"]
    lut = []
    si = 0
    for i in range(256):
        t = i / 255.0
        while si + 1 < len(stops) and stops[si + 1][0] < t:
            si += 1
        if si + 1 >= len(stops):
            lut.append(stops[-1][1])
            continue
        r0, c0 = stops[si]
        r1, c1 = stops[si + 1]
        if r1 <= r0:
            lut.append(c1)
            continue
        f = (t - r0) / (r1 - r0)
        lut.append(tuple(int(c0[k] + (c1[k] - c0[k]) * f + 0.5)
                         for k in range(4)))
    return lut


def _spread_t(t, spread):
    if spread == 1:                    # reflect
        t = t - 2.0 * math.floor((t + 1.0) * 0.5)
        return abs(t)
    if spread == 2:                    # repeat
        return t - math.floor(t)
    return min(max(t, 0.0), 1.0)       # pad


def rasterize_shape(shape, bitmap_pixels):
    """Rasterize a parsed shape to (w, h, rgba_bytes) or None.

    bitmap_pixels: callable(bmp_id) -> (w, h, rgba_bytes) or None.
    """
    xmin, xmax, ymin, ymax = shape["bounds"]
    fills = shape["fills"]
    lines = shape["lines"]
    edges = shape["edges"]
    if not edges:
        return None

    pad = 2.0
    maxw = 0.0
    for ln in lines:
        if ln:
            maxw = max(maxw, ln["width"])
    pad += maxw * 0.5 + 1.0
    w = int((xmax - xmin) / 20.0 + 2 * pad + 0.999)
    h = int((ymax - ymin) / 20.0 + 2 * pad + 0.999)
    if w <= 0 or h <= 0 or w * h > 40_000_000:
        return None
    ss = 2 if w * h <= 500_000 else 1

    def to_px(pt):
        return ((pt[0] - xmin) / 20.0 + pad, (pt[1] - ymin) / 20.0 + pad)

    # --- build directed fill segments and stroke runs --------------------
    segs_by_fill = {}
    stroke_runs = []
    cur_run = None                     # [line_idx, [pts]]
    prev_end = None
    tol = 0.3
    for e in edges:
        k = e["kind"]
        if k == "move":
            if cur_run is not None:
                stroke_runs.append(cur_run)
                cur_run = None
            prev_end = None
            continue
        if k == "line":
            flat = [to_px(e["pts"][0]), to_px(e["pts"][1])]
        else:
            p0, pc, p1 = (to_px(q) for q in e["pts"])
            flat = [p0]
            _flatten_quad(p0, pc, p1, tol, flat)
        li = e["line"]
        ls = lines[li] if 0 < li < len(lines) else None
        if ls is not None:
            if (cur_run is not None and cur_run[0] == li
                    and prev_end is not None and prev_end == flat[0]):
                cur_run[2].extend(flat[1:])
            else:
                if cur_run is not None:
                    stroke_runs.append(cur_run)
                cur_run = [li, ls, list(flat)]
        f0, f1 = e["f0"], e["f1"]
        todo = []
        if f0 == f1:
            if f0:
                todo.append((f0, False))
        else:
            if f0:
                todo.append((f0, False))
            if f1:
                todo.append((f1, True))
        for f, rev in todo:
            lst = segs_by_fill.setdefault(f, [])
            for i in range(len(flat) - 1):
                a, b = flat[i], flat[i + 1]
                if rev:
                    a, b = b, a
                lst.append((a[0], a[1], b[0], b[1]))
        prev_end = flat[-1]
    if cur_run is not None:
        stroke_runs.append(cur_run)

    strokes = [(li, ls["width"], ls["paint"], pts)
               for li, ls, pts in stroke_runs
               if 0 < li < len(lines) and lines[li] is not None]

    img = bytearray(w * h * 4)

    # --- paint helpers ----------------------------------------------------
    grad_cache = {}

    def paint_at(style, px, py):
        """Return (r, g, b, a01) for a fill style at device pixel."""
        t = style["type"]
        if t == "solid":
            r, g, b, a = style["color"]
            return r, g, b, a / 255.0
        # shape-space point (twips)
        sx = (px - pad) * 20.0 + xmin
        sy = (py - pad) * 20.0 + ymin
        if t == "grad":
            u, v = _mat_apply(style["matrix"], sx, sy)
            kind = style["kind"]
            if kind == "linear":
                tt = u
            elif kind == "radial":
                tt = math.sqrt(u * u + v * v)
            else:  # focal
                fx = style.get("focal", 0.0)
                tt = math.sqrt((u - fx) * (u - fx) + v * v)
            tt = _spread_t(tt, style["grad"]["spread"])
            lut = grad_cache.get(id(style["grad"]))
            if lut is None:
                lut = _gradient_lut(style["grad"])
                grad_cache[id(style["grad"])] = lut
            r, g, b, a = lut[min(255, max(0, int(tt * 255.0)))]
            return r, g, b, a / 255.0
        if t == "bitmap":
            bp = bitmap_pixels(style["bmp"])
            if bp is None:
                return 255, 0, 255, 1.0
            bw, bh, bpx = bp
            u, v = _mat_apply(style["matrix"], sx, sy)
            if style.get("smooth"):
                fx, fy = u - 0.5, v - 0.5
                x0, y0 = int(math.floor(fx)), int(math.floor(fy))
                tx, ty = fx - x0, fy - y0
                acc = [0.0, 0.0, 0.0, 0.0]
                for dy_ in (0, 1):
                    for dx_ in (0, 1):
                        wx = (1 - tx) if dx_ == 0 else tx
                        wy = (1 - ty) if dy_ == 0 else ty
                        wt = wx * wy
                        xx = (x0 + dx_) % bw
                        yy = (y0 + dy_) % bh
                        o = (yy * bw + xx) * 4
                        for k in range(4):
                            acc[k] += bpx[o + k] * wt
                return (min(255.0, acc[0]), min(255.0, acc[1]),
                        min(255.0, acc[2]), min(1.0, acc[3] / 255.0))
            xx = int(math.floor(u)) % bw
            yy = int(math.floor(v)) % bh
            o = (yy * bw + xx) * 4
            return bpx[o], bpx[o + 1], bpx[o + 2], bpx[o + 3] / 255.0
        return 0, 0, 0, 0.0

    def composite(cov, style):
        is_solid = style is not None and style.get("type") == "solid"
        if is_solid:
            r, g, b, a = style["color"]
            sa0 = a / 255.0
            for i in range(w * h):
                c = cov[i]
                if c <= 0.0:
                    continue
                sa = c * sa0
                if sa <= 0.0:
                    continue
                d = i * 4
                dr, dg, db, da = (img[d], img[d + 1], img[d + 2],
                                  img[d + 3] / 255.0)
                oa = sa + da * (1.0 - sa)
                if oa <= 0.0:
                    img[d:d + 4] = b"\x00\x00\x00\x00"
                    continue
                k = sa / oa
                j = da * (1.0 - sa) / oa
                img[d] = int(r * k + dr * j + 0.5)
                img[d + 1] = int(g * k + dg * j + 0.5)
                img[d + 2] = int(b * k + db * j + 0.5)
                img[d + 3] = int(oa * 255.0 + 0.5)
        else:
            for y in range(h):
                for x in range(w):
                    c = cov[y * w + x]
                    if c <= 0.0:
                        continue
                    r, g, b, sa1 = paint_at(style, x + 0.5, y + 0.5)
                    sa = c * sa1
                    if sa <= 0.0:
                        continue
                    d = (y * w + x) * 4
                    dr, dg, db, da = (img[d], img[d + 1], img[d + 2],
                                      img[d + 3] / 255.0)
                    oa = sa + da * (1.0 - sa)
                    if oa <= 0.0:
                        img[d:d + 4] = b"\x00\x00\x00\x00"
                        continue
                    k = sa / oa
                    j = da * (1.0 - sa) / oa
                    img[d] = int(r * k + dr * j + 0.5)
                    img[d + 1] = int(g * k + dg * j + 0.5)
                    img[d + 2] = int(b * k + db * j + 0.5)
                    img[d + 3] = int(oa * 255.0 + 0.5)

    # fills in style order, then strokes on top
    for fidx in sorted(segs_by_fill):
        if fidx >= len(fills) or fills[fidx] is None:
            continue
        cov = _coverage(segs_by_fill[fidx], w, h, ss)
        composite(cov, fills[fidx])

    def line_paint_style(paint):
        kind, val = paint
        if kind == "solid":
            return {"type": "solid", "color": val}
        return val                       # ("fill", fillstyle_dict)

    for li, width, paint, pts in strokes:
        cov = _stroke_coverage([(width, pts)], w, h, ss)
        composite(cov, line_paint_style(paint))

    return w, h, bytes(img)


# ---------------------------------------------------------------------------
# SWF parser
# ---------------------------------------------------------------------------


class SwfParser:
    def __init__(self, path, out_assets):
        self.path = path
        self.out_assets = out_assets
        self.chars = {}              # charId -> info dict
        self.sprites = {}            # charId -> sprite timeline
        self.jpeg_tables = None
        self.warnings = []
        self.frame_labels = []
        self.sound_streams = []      # list of (codec, bytearray)
        self.sound_cues = []         # [{"frame": n, "soundId": id}]
        self._pending_jpegs = {}     # charId -> raw JPEG payload (tag 6)
        self.export_names = {}       # charId -> name
        self.metadata = None
        self._png_cache = {}         # asset path -> decoded (w,h,rgba)
        self._skipped_tags = {}      # tag code -> count (informational)

        self.version = 0
        self.frame_rate = 0.0
        self.frame_count = 0
        self.width_px = 0.0
        self.height_px = 0.0
        self.bg_color = "#FFFFFF"
        self._scope_frames = None    # sprite frame list while inside DefineSprite
        self._display = {}           # depth -> placement (current scope)

    # -- tag iteration -----------------------------------------------------

    @staticmethod
    def iter_tags(buf: bytes, start: int, end: int):
        off = start
        while off + 2 <= end:
            th = struct.unpack_from("<H", buf, off)[0]
            off += 2
            code = th >> 6
            length = th & 0x3F
            if length == 0x3F:
                length = struct.unpack_from("<I", buf, off)[0]
                off += 4
            yield code, buf[off:off + length]
            off += length
            if code == 0:
                return

    # -- main entry --------------------------------------------------------

    def parse(self):
        raw = open(self.path, "rb").read()
        sig = raw[:3]
        self.version = raw[3]
        if sig == b"CWS":
            body = zlib.decompress(raw[8:])
        elif sig == b"ZWS":
            body = lzma.decompress(raw[8:])
        elif sig == b"FWS":
            body = raw[8:]
        else:
            raise ValueError(f"not a SWF file (signature {sig!r})")

        br = BitReader(body)
        xmin, xmax, ymin, ymax = read_rect(br)
        br.align()
        byte_off = br.byte_len()
        frame_rate_raw, frame_count = struct.unpack_from("<HH", body, byte_off)
        self.frame_rate = frame_rate_raw / 256.0
        self.frame_count = frame_count
        self.width_px = (xmax - xmin) / 20.0
        self.height_px = (ymax - ymin) / 20.0

        self.frames = []
        self._parse_tag_stream(body, byte_off + 4, len(body))
        return self

    def _current_frames(self):
        return self._scope_frames if self._scope_frames is not None else self.frames

    def _parse_tag_stream(self, buf, start, end):
        for code, payload in self.iter_tags(buf, start, end):
            try:
                self._handle_tag(code, payload)
            except Exception as e:
                self.warnings.append(f"tag {code}: parse error: {e}")

    # -- tag dispatch --------------------------------------------------------

    def _handle_tag(self, code, p):
        if code == 0:      # End
            return
        if code == 1:      # ShowFrame
            self._current_frames().append(
                [dict(v) for k, v in sorted(self._display.items())])
            return
        if code == 5:      # RemoveObject (charId u16, depth u16)
            if len(p) >= 4:
                self._display.pop(struct.unpack_from("<H", p, 2)[0], None)
            return
        if code == 28:     # RemoveObject2 (depth u16)
            if len(p) >= 2:
                self._display.pop(struct.unpack_from("<H", p, 0)[0], None)
            return
        if code == 4:      # PlaceObject (SWF<5)
            if len(p) < 6:
                return
            char_id, depth = struct.unpack_from("<HH", p, 0)
            br = BitReader(p[4:])
            m = read_matrix(br)
            self._apply_place(depth, char_id, m, None, False)
            return
        if code == 26:     # PlaceObject2
            self._place_object2(p)
            return
        if code == 70:     # PlaceObject3
            self._place_object3(p)
            return

        # --- definitions -------------------------------------------------
        if code == 6:      # DefineBits (JPEG, needs JPEGTables)
            if len(p) >= 2:
                cid = struct.unpack_from("<H", p, 0)[0]
                self._pending_jpegs[cid] = p[2:]
                self.chars[cid] = {"kind": "image", "pending": True}
            return
        if code == 8:      # JPEGTables
            self.jpeg_tables = p
            return
        if code in (20, 36):   # DefineBitsLossless / Lossless2
            if len(p) >= 7:
                cid = struct.unpack_from("<H", p, 0)[0]
                try:
                    w, h, png, _ = decode_lossless(code, p)
                    fname = f"img_{cid}.png"
                    with open(os.path.join(self.out_assets, "images", fname),
                              "wb") as f:
                        f.write(png)
                    self.chars[cid] = {"kind": "image", "file": fname,
                                       "ext": "png", "w": w, "h": h}
                except Exception as e:
                    self.warnings.append(f"lossless bitmap id {cid}: {e}")
            return
        if code == 21:     # DefineBitsJPEG2 (self-contained JPEG/PNG/GIF)
            if len(p) >= 2:
                cid = struct.unpack_from("<H", p, 0)[0]
                data = p[2:]
                ext = sniff_image_ext(data)
                if ext == "jpg":
                    data = reconstruct_jpeg(data)
                fname = f"img_{cid}.{ext}"
                with open(os.path.join(self.out_assets, "images", fname),
                          "wb") as f:
                    f.write(data)
                self.chars[cid] = {"kind": "image", "file": fname, "ext": ext,
                                   "w": 0, "h": 0}
            return
        if code == 35:     # DefineBitsJPEG3 (JPEG + zlib alpha)
            if len(p) >= 6:
                cid = struct.unpack_from("<H", p, 0)[0]
                alpha_off = struct.unpack_from("<I", p, 2)[0]
                jpeg = p[6:] if alpha_off == 0 else p[6:6 + alpha_off]
                has_alpha = alpha_off != 0
                data = reconstruct_jpeg(jpeg)
                fname = f"img_{cid}.jpg"
                with open(os.path.join(self.out_assets, "images", fname),
                          "wb") as f:
                    f.write(data)
                info = {"kind": "image", "file": fname, "ext": "jpg",
                        "w": 0, "h": 0}
                if has_alpha:
                    # AlphaData = zlib stream of w*h grayscale bytes.
                    # Emit a sibling .alpha.png mask; the player applies
                    # it as the texture's alpha channel.
                    try:
                        wh = _jpeg_size(jpeg)
                        alpha = None
                        for base in (6 + alpha_off, alpha_off):
                            try:
                                alpha = zlib.decompress(p[base:])
                                break
                            except Exception:
                                continue
                        if wh and alpha and len(alpha) >= wh[0] * wh[1]:
                            w, h = wh
                            raw = bytearray(h * (w + 1))
                            for y in range(h):
                                row = y * (w + 1)
                                raw[row + 1:row + 1 + w] = \
                                    alpha[y * w:(y + 1) * w]
                            afname = f"img_{cid}.alpha.png"
                            with open(os.path.join(
                                    self.out_assets, "images", afname),
                                    "wb") as f:
                                f.write(_png_bytes(w, h, 0, bytes(raw)))
                            info["alphaFile"] = afname
                        else:
                            self.warnings.append(
                                f"image id {cid}: JPEG3 alpha decode failed")
                    except Exception as e:
                        self.warnings.append(
                            f"image id {cid}: JPEG3 alpha error: {e}")
                self.chars[cid] = info
            return
        if code == 90:     # DefineBitsJPEG4
            self.warnings.append("DefineBitsJPEG4 encountered; skipped")
            return
        if code in (2, 22, 32, 83):   # DefineShape / 2 / 3 / 4
            shape = parse_shape(code, p)
            if shape:
                sid = shape["sid"]
                b = shape["bounds"]
                entry = {"kind": "shape", "shape": shape,
                         "w": (b[1] - b[0]) / 20.0, "h": (b[3] - b[2]) / 20.0,
                         "bitmaps": self._shape_bitmap_ids(shape)}
                self.chars[sid] = entry
            else:
                self.warnings.append(f"shape tag {code}: parse failed")
            return

        if code == 14:     # DefineSound
            self._define_sound(p)
            return
        if code in (15, 89):   # StartSound / StartSound2 -> cue
            self._start_sound(p)
            return
        if code == 18:     # SoundStreamHead (always MP3)
            self.sound_streams.append([2, bytearray()])
            return
        if code == 45:     # SoundStreamHead2 (has compression byte)
            comp = p[4] if len(p) > 4 else 2
            self.sound_streams.append([comp, bytearray()])
            return
        if code == 19:     # SoundStreamBlock: u32 samples + data
            if len(p) > 4 and self.sound_streams:
                comp, buf = self.sound_streams[-1]
                if comp == 2:
                    buf += p[4:]
            return

        if code == 39:     # DefineSprite
            self._define_sprite(p)
            return

        if code == 9:      # SetBackgroundColor
            if len(p) >= 3:
                self.bg_color = "#%02X%02X%02X" % tuple(p[:3])
            return
        if code == 43:     # FrameLabel
            if p:
                try:
                    name, _ = read_cstring(p, 0)
                    self.frame_labels.append(
                        {"frame": len(self._current_frames()), "name": name})
                except Exception:
                    pass
            return
        if code in (56, 76):   # ExportAssets / SymbolClass
            self._named_ids(p)
            return
        if code == 77:     # Metadata
            if p:
                try:
                    self.metadata, _ = read_cstring(p, 0)
                except Exception:
                    pass
            return
        if code == 86:     # SceneAndFrameLabelData
            self._scene_labels(p)
            return
        if code == 87:     # DefineBinaryData
            if len(p) >= 4:
                bid = struct.unpack_from("<I", p, 0)[0]
                os.makedirs(os.path.join(self.out_assets, "binary"),
                            exist_ok=True)
                with open(os.path.join(self.out_assets, "binary",
                                       f"data_{bid}.bin"), "wb") as f:
                    f.write(p[4:])
            return
        # shapes, fonts, buttons, actions etc. are intentionally skipped
        self._skipped_tags[code] = self._skipped_tags.get(code, 0) + 1

    # -- PlaceObject --------------------------------------------------------

    @staticmethod
    def _identity_matrix():
        return {"tx": 0, "ty": 0, "sx": 1.0, "sy": 1.0, "k0": 0.0, "k1": 0.0}

    @staticmethod
    def _shape_bitmap_ids(shape):
        """Bitmap character ids referenced by a parsed shape's styles."""
        ids = []

        def scan_fill(fs):
            if not fs:
                return
            if fs.get("type") == "bitmap":
                ids.append(fs["bmp"])

        for fs in shape["fills"]:
            scan_fill(fs)
        for ls in shape["lines"]:
            if not ls:
                continue
            kind, val = ls.get("paint", (None, None))
            if kind == "fill":
                scan_fill(val)
        return ids

    def _apply_place(self, depth, char_id, matrix, name, is_move,
                     cxform=None, visible=None):
        old = self._display.get(depth)
        if is_move and old is not None:
            if matrix is not None:
                old["m"] = matrix
            if cxform is not None:
                old["cx"] = cxform
            if name:
                old["name"] = name
            if visible is not None:
                old["vis"] = visible
            if char_id is not None:
                old["charId"] = char_id
            return
        entry = {
            "depth": depth,
            "charId": char_id if char_id is not None
                      else (old or {}).get("charId"),
            "name": name or (old or {}).get("name"),
            "m": matrix if matrix is not None
                 else (old or {}).get("m") or self._identity_matrix(),
            "cx": cxform if cxform is not None else (old or {}).get("cx"),
            "vis": visible if visible is not None
                   else (old or {}).get("vis", True),
            "isMove": is_move,
        }
        self._display[depth] = entry

    @staticmethod
    def _skip_clip_actions(p, off, extended):
        while off + 4 <= len(p):
            if extended:
                flags = struct.unpack_from(">I", p, off)[0]
                off += 4
            else:
                flags = struct.unpack_from(">H", p, off)[0] << 16
                off += 2
            if flags == 0:
                break
            if off + 4 > len(p):
                break
            size = struct.unpack_from("<I", p, off)[0]
            off += 4
            if flags & (1 << 9):        # key press -> keycode byte
                if size > 0:
                    size -= 1
                off += 1
            off += size
        return off

    def _place_object2(self, p):
        if len(p) < 3:
            return
        flags = p[0]
        has_clip_actions = flags & 0x80
        has_clip_depth = flags & 0x40
        has_name = flags & 0x20
        has_ratio = flags & 0x10
        has_color_transform = flags & 0x08
        has_matrix = flags & 0x04
        has_char = flags & 0x02
        is_move = flags & 0x01

        off = 1
        depth = struct.unpack_from("<H", p, off)[0]
        off += 2
        char_id = None
        if has_char:
            char_id = struct.unpack_from("<H", p, off)[0]
            off += 2
        matrix = None
        if has_matrix:
            br = BitReader(p[off:])
            matrix = read_matrix(br)
            off += br.byte_len()
        cxform = None
        if has_color_transform:
            br = BitReader(p[off:])
            cxform = read_cxform(br, alpha=True)
            off += br.byte_len()
        if has_ratio:
            off += 2
        name = None
        if has_name:
            name, off = read_cstring(p, off)
        if has_clip_depth:
            off += 2
        if has_clip_actions:
            off = self._skip_clip_actions(p, off, self.version >= 6)

        self._apply_place(depth, char_id, matrix, name, bool(is_move),
                          cxform=cxform)

    def _place_object3(self, p):
        if len(p) < 4:
            return
        flags = struct.unpack_from(">H", p, 0)[0]  # big-endian bit field
        # bit order per SWF spec / FFDec PlaceObject3Tag
        has_clip_actions = flags & 0x8000
        has_clip_depth = flags & 0x4000
        has_name = flags & 0x2000
        has_ratio = flags & 0x1000
        has_color_transform = flags & 0x0800
        has_matrix = flags & 0x0400
        has_char = flags & 0x0200
        is_move = flags & 0x0100
        has_bg = flags & 0x0040
        has_visibility = flags & 0x0020
        has_image = flags & 0x0010
        has_class_name = flags & 0x0008
        has_cache = flags & 0x0004
        has_blend = flags & 0x0002
        has_filters = flags & 0x0001

        off = 2
        depth = struct.unpack_from("<H", p, off)[0]
        off += 2
        if has_class_name or (has_image and has_char):
            _, off = read_cstring(p, off)
        char_id = None
        if has_char:
            char_id = struct.unpack_from("<H", p, off)[0]
            off += 2
        matrix = None
        if has_matrix:
            br = BitReader(p[off:])
            matrix = read_matrix(br)
            off += br.byte_len()
        cxform = None
        if has_color_transform:
            br = BitReader(p[off:])
            cxform = read_cxform(br, alpha=True)
            off += br.byte_len()
        if has_ratio:
            off += 2
        name = None
        if has_name:
            name, off = read_cstring(p, off)
        if has_clip_depth:
            off += 2
        visible = None
        if has_filters:
            # FilterList records are variable-length and rare; keep the
            # placement (drawing it unfiltered is a better approximation
            # than dropping the object) — later fields are unreachable.
            if "PlaceObject3 with filters" not in self.warnings:
                self.warnings.append("PlaceObject3 with filters skipped")
            self._apply_place(depth, char_id, matrix, name, bool(is_move),
                              cxform=cxform)
            return
        # field order: blend, cache, visible, bgColor, clipActions
        if has_blend:
            off += 1
        if has_cache:
            off += 1
        if has_visibility:
            visible = bool(p[off])
            off += 1
        if has_bg:
            off += 4
        if has_clip_actions:
            off = self._skip_clip_actions(p, off, self.version >= 6)

        self._apply_place(depth, char_id, matrix, name, bool(is_move),
                          cxform=cxform, visible=visible)

    # -- sprites ------------------------------------------------------------

    def _define_sprite(self, p):
        if len(p) < 4:
            return
        sid, frame_count = struct.unpack_from("<HH", p, 0)
        saved_frames = self._scope_frames
        saved_display = self._display
        self._scope_frames = []
        self._display = {}
        try:
            for code, payload in self.iter_tags(p, 4, len(p)):
                if code == 0:
                    break
                try:
                    self._handle_tag(code, payload)
                except Exception as e:
                    self.warnings.append(f"sprite {sid} tag {code}: {e}")
        finally:
            frames = self._scope_frames
            self._scope_frames = saved_frames
            self._display = saved_display
        while len(frames) < frame_count:
            frames.append(list(frames[-1]) if frames else [])
        self.sprites[sid] = {"frameCount": frame_count, "frames": frames}
        self.chars[sid] = {"kind": "sprite", "spriteId": sid,
                           "frameCount": frame_count}

    # -- sounds -------------------------------------------------------------

    def _define_sound(self, p):
        if len(p) < 7:
            return
        sid = struct.unpack_from("<H", p, 0)[0]
        fmt_byte = p[2]
        codec = fmt_byte >> 4
        rate_bits = (fmt_byte >> 2) & 3
        size_bit = (fmt_byte >> 1) & 1
        stereo = fmt_byte & 1
        data = p[7:]
        rates = [5512, 11025, 22050, 44100]
        rate = rates[rate_bits] if rate_bits < 4 else 11025

        os.makedirs(os.path.join(self.out_assets, "sounds"), exist_ok=True)
        if codec == 2:      # MP3
            fname, ext = f"snd_{sid}.mp3", "mp3"
        elif codec in (0, 3):   # uncompressed PCM
            fname, ext = f"snd_{sid}.wav", "wav"
            self._pcm_to_wav(os.path.join(self.out_assets, "sounds", fname),
                             data, codec, rate, size_bit, stereo)
        else:
            fname, ext = f"snd_{sid}.bin", "bin"
            self.warnings.append(
                f"sound id {sid}: codec {codec} not supported, dumped raw")
        if ext != "wav":
            with open(os.path.join(self.out_assets, "sounds", fname),
                      "wb") as f:
                f.write(data)
        self.chars[sid] = {"kind": "sound", "file": f"sounds/{fname}",
                           "codec": codec}

    @staticmethod
    def _pcm_to_wav(path, data, codec, rate, size_bit, stereo):
        channels = 2 if stereo else 1
        bits = 16 if size_bit else 8
        bytes_per = bits // 8
        if codec == 0 and bits == 16:
            # big-endian samples -> little-endian for WAV
            data = b"".join(data[i + 1:i + 2] + data[i:i + 1]
                            for i in range(0, len(data) - 1, 2))
        block_align = channels * bytes_per
        byte_rate = rate * block_align
        data_size = len(data)
        fmt_chunk = struct.pack("<HHIIHH", 1, channels, rate, byte_rate,
                                block_align, bits)
        with open(path, "wb") as f:
            f.write(b"RIFF" + struct.pack("<I", 36 + data_size) + b"WAVE")
            f.write(b"fmt " + struct.pack("<I", 16) + fmt_chunk)
            f.write(b"data" + struct.pack("<I", data_size) + data)

    def _start_sound(self, p):
        if len(p) < 2:
            return
        sid = struct.unpack_from("<H", p, 0)[0]
        if self._scope_frames is None:   # only track on main timeline
            self.sound_cues.append({"frame": len(self.frames), "soundId": sid})

    # -- names / labels ------------------------------------------------------

    def _named_ids(self, p):
        if len(p) < 2:
            return
        count = struct.unpack_from("<H", p, 0)[0]
        off = 2
        for _ in range(count):
            if off + 2 > len(p):
                break
            cid = struct.unpack_from("<H", p, off)[0]
            off += 2
            name, off = read_cstring(p, off)
            self.export_names[cid] = name

    def _scene_labels(self, p):
        try:
            off = 0
            nscenes = struct.unpack_from("<I", p, off)[0]
            off += 4
            for _ in range(nscenes):
                ln = p[off]
                off += 1 + ln + 2
            nlabels = struct.unpack_from("<H", p, off)[0]
            off += 2
            for _ in range(nlabels):
                name, off = read_cstring(p, off)
                fr = struct.unpack_from("<H", p, off)[0]
                off += 2
                self.frame_labels.append({"frame": fr, "name": name})
        except Exception:
            pass

    # -- bitmaps / shape rasterization ---------------------------------------

    def _bitmap_pixels(self, bmp_id):
        """RGBA pixels for a bitmap character, or None if unavailable."""
        if bmp_id in self._png_cache:
            return self._png_cache[bmp_id]
        res = None
        info = self.chars.get(bmp_id)
        if info and info.get("kind") == "image" and info.get("ext") == "png":
            try:
                path = os.path.join(self.out_assets, "images",
                                    info["file"])
                with open(path, "rb") as f:
                    res = decode_png_rgba(f.read())
            except Exception:
                res = None
        self._png_cache[bmp_id] = res
        return res

    def _rasterize_shapes(self):
        n_done = 0
        for cid, info in list(self.chars.items()):
            if info.get("kind") != "shape" or "shape" not in info:
                continue
            try:
                res = rasterize_shape(info["shape"], self._bitmap_pixels)
            except Exception as e:
                res = None
                self.warnings.append(f"shape {cid}: rasterize error: {e}")
            if res is None:
                continue
            w, h, rgba = res
            raw = bytearray(h * (w * 4 + 1))
            for y in range(h):
                row = y * (w * 4 + 1)
                raw[row + 1:row + 1 + w * 4] = rgba[y * w * 4:(y + 1) * w * 4]
            png = _png_bytes(w, h, 6, bytes(raw))
            fname = f"shape_{cid}.png"
            with open(os.path.join(self.out_assets, "images", fname),
                      "wb") as f:
                f.write(png)
            self.chars[cid] = {"kind": "image", "file": fname, "ext": "png",
                               "w": w, "h": h}
            n_done += 1
        if n_done:
            self.warnings.append(f"rasterized {n_done} vector shapes")

    # -- finalisation ---------------------------------------------------------

    def finish(self):
        # flush pending DefineBits images (need JPEGTables)
        for cid, data in self._pending_jpegs.items():
            jpg = reconstruct_jpeg(data, self.jpeg_tables)
            fname = f"img_{cid}.jpg"
            with open(os.path.join(self.out_assets, "images", fname),
                      "wb") as f:
                f.write(jpg)
            self.chars[cid] = {"kind": "image", "file": fname, "ext": "jpg",
                               "w": 0, "h": 0}
        self._pending_jpegs.clear()

        # write MP3 sound streams
        for i, (comp, buf) in enumerate(self.sound_streams):
            if comp == 2 and buf:
                with open(os.path.join(self.out_assets, "sounds",
                                       f"stream_{i}.mp3"), "wb") as f:
                    f.write(bytes(buf))

        for cid, name in self.export_names.items():
            if cid in self.chars:
                self.chars[cid]["name"] = name

        self._rasterize_shapes()

        if self._skipped_tags:
            names = {0: "End", 1: "ShowFrame", 7: "DefineButton",
                     9: "SetBackgroundColor", 10: "DefineFont",
                     11: "DefineText", 12: "DoAction", 13: "DefineFontInfo",
                     17: "DefineButtonCxform", 24: "Protect",
                     33: "DefineText2", 34: "DefineButton2",
                     37: "DefineEditText", 43: "FrameLabel",
                     46: "DefineMorphShape", 48: "DefineFont2",
                     59: "DoInitAction", 64: "EnableDebugger2",
                     69: "FileAttributes", 72: "DoABC", 73: "DefineShape?",
                     74: "DefineScene?", 75: "DefineBinaryData?",
                     82: "DoABC", 84: "DefineMorphShape2",
                     86: "SceneAndFrameLabelData", 87: "DefineBinaryData",
                     88: "DefineFontName", 89: "StartSound2",
                     90: "DefineBitsJPEG4"}
            parts = []
            for code, n in sorted(self._skipped_tags.items()):
                nm = names.get(code, f"tag{code}")
                parts.append(f"{nm}x{n}")
            self.warnings.append(
                "unsupported tags ignored: " + ", ".join(parts))

        return {
            "version": self.version,
            "frameRate": self.frame_rate,
            "frameCount": self.frame_count,
            "width": self.width_px,
            "height": self.height_px,
            "background": self.bg_color,
            "source": os.path.basename(self.path),
            "warnings": self.warnings,
            "frameLabels": sorted(self.frame_labels, key=lambda d: d["frame"]),
            "characters": {str(k): v for k, v in self.chars.items()},
            "frames": self._serialize_frames(self.frames),
            "sprites": {str(k): {"frameCount": v["frameCount"],
                                 "frames": self._serialize_frames(v["frames"])}
                        for k, v in self.sprites.items()},
            "soundCues": [
                {"frame": c["frame"],
                 "soundId": c["soundId"],
                 "file": ("assets/" + self.chars[c["soundId"]]["file"])
                 if c["soundId"] in self.chars
                 and self.chars[c["soundId"]].get("file") else None}
                for c in self.sound_cues],
        }

    def _serialize_frames(self, frames):
        out = []
        ident = self._identity_matrix()
        for frame in frames:
            items = []
            for pl in frame:
                if pl.get("vis") is False:
                    continue
                cid = pl.get("charId")
                info = self.chars.get(cid, {}) if cid is not None else {}
                kind = info.get("kind", "unknown")
                m = pl.get("m") or ident
                entry = {
                    "depth": pl["depth"],
                    "charId": cid,
                    "kind": kind,
                    "name": pl.get("name") or info.get("name"),
                    "a": round(m["sx"], 5), "b": round(m["k1"], 5),
                    "c": round(m["k0"], 5), "d": round(m["sy"], 5),
                    "e": round(m["tx"] / 20.0, 3),
                    "f": round(m["ty"] / 20.0, 3),
                }
                cx = pl.get("cx")
                if cx:
                    entry["mr"], entry["mg"], entry["mb"], entry["ma"] = \
                        [round(v, 5) for v in cx[0]]
                    entry["ar"], entry["ag"], entry["ab"], entry["aa"] = \
                        [round(v, 5) for v in cx[1]]
                if kind == "image" and info.get("file"):
                    entry["file"] = "assets/images/" + info["file"]
                elif kind == "sprite":
                    entry["spriteId"] = cid
                elif kind == "sound" and info.get("file"):
                    entry["file"] = "assets/" + info["file"]
                items.append(entry)
                # Fallback for shapes that failed to rasterize: draw each
                # referenced bitmap stretched over the shape bounds.
                if kind == "shape":
                    for bmp in info.get("bitmaps", []):
                        binfo = self.chars.get(bmp, {})
                        if binfo.get("kind") == "image" and binfo.get("file"):
                            items.append({
                                "depth": pl["depth"],
                                "charId": bmp,
                                "kind": "image",
                                "name": pl.get("name") or info.get("name"),
                                "a": entry["a"], "b": entry["b"],
                                "c": entry["c"], "d": entry["d"],
                                "e": entry["e"], "f": entry["f"],
                                "file": "assets/images/" + binfo["file"],
                            })
            items.sort(key=lambda it: it.get("depth", 0))
            out.append(items)
        return out


# ---------------------------------------------------------------------------
# C++ project generation
# ---------------------------------------------------------------------------

CPP_CMAKE = r'''cmake_minimum_required(VERSION 3.16)
project(swf_player CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

file(GLOB PLAYER_SOURCES CONFIGURE_DEPENDS src/*.cpp)
add_executable(swf_player ${PLAYER_SOURCES})
target_include_directories(swf_player PRIVATE src src/vendor)

if(WIN32)
    target_link_libraries(swf_player PRIVATE gdi32 user32)
else()
    find_package(X11 REQUIRED)
    target_link_libraries(swf_player PRIVATE X11)
endif()
'''

BUILD_SH = r'''#!/bin/sh
# Build the generated SWF player on Linux.
set -e
cd "$(dirname "$0")"
mkdir -p build
cd build
cmake ..
cmake --build . -j"$(nproc 2>/dev/null || echo 4)"
echo "Built: build/swf_player"
echo "Run:   ./build/swf_player            (windowed)"
echo "       ./build/swf_player --frames 30 (headless PNG export)"
'''

BUILD_BAT = r'''@echo off
rem Build the generated SWF player on Windows.
setlocal
cd /d "%~dp0"
if not exist build mkdir build
cd build
cmake .. -G "Visual Studio 17 2022" -A x64 2>nul || cmake ..
cmake --build . --config Release
if errorlevel 1 (
    echo Build failed.
    exit /b 1
)
echo Built: build\Release\swf_player.exe
echo Run:   build\Release\swf_player.exe
endlocal
'''

# Source files copied verbatim from this repo's templates/ and vendor/
# directories into the generated project so it is fully self-contained.
PLAYER_TEMPLATE_FILES = [
    "main.cpp", "platform.h", "platform_x11.cpp", "platform_win32.cpp",
    "audio.h", "audio.cpp", "swfrender.h", "swfrender.cpp",
]
PLAYER_VENDOR_FILES = [
    "stb_image.h", "stb_image_write.h", "minimp3.h", "miniaudio.h",
]


def _repo_dir():
    return os.path.dirname(os.path.abspath(__file__))


def _cpp_str(s):
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _draw_item_init(it):
    """Return the C++ initializer text for one DrawItem (or None to skip)."""
    if it.get("kind") == "sound":
        return None
    file = it.get("file", "")
    spr = it.get("spriteId", -1)
    if it.get("kind") == "sprite":
        file = ""
        spr = it.get("charId", -1)
    m = it
    color = [it.get(k, d) for k, d in
             (("mr", 1.0), ("mg", 1.0), ("mb", 1.0), ("ma", 1.0),
              ("ar", 0.0), ("ag", 0.0), ("ab", 0.0), ("aa", 0.0))]
    has_cx = 1 if "mr" in it else 0
    def fv(v):
        return f"{float(v)}f"
    return ("{" + _cpp_str(file) + f", {spr}, "
            "{%s,%s,%s,%s,%s,%s}, {%s,%s,%s,%s,%s,%s,%s,%s}, "
            "%s}" % (
                fv(m.get("a", 1.0)), fv(m.get("b", 0.0)),
                fv(m.get("c", 0.0)), fv(m.get("d", 1.0)),
                fv(m.get("e", 0.0)), fv(m.get("f", 0.0)),
                *(fv(v) for v in color),
                "true" if has_cx else "false"))


def generate_timeline_files(timeline, header_path, source_path):
    """Emit src/timeline_data.h + src/timeline_data.cpp using plain static
    arrays (keeps cc1plus happy even for large timelines)."""
    frames = timeline["frames"]
    sprites = timeline["sprites"]
    bg = timeline["background"].lstrip("#")
    bg_r, bg_g, bg_b = int(bg[0:2], 16), int(bg[2:4], 16), int(bg[4:6], 16)

    h = []
    h.append("// Generated by swf_to_cpp.py — do not edit.")
    h.append("#pragma once")
    h.append("")
    h.append("struct DrawItem {")
    h.append("    const char* file;   // asset path, nullptr/\"\" if none")
    h.append("    int spriteId;       // >=0 -> draw sprite instead of file")
    h.append("    float matrix[6];    // a,b,c,d,e,f affine transform")
    h.append("    float color[8];     // mr,mg,mb,ma, ar,ag,ab,aa")
    h.append("    bool hasCx;         // color transform present")
    h.append("};")
    h.append("")
    h.append("struct FrameView { const DrawItem* items; int count; };")
    h.append("struct SpriteDef {")
    h.append("    int id;")
    h.append("    int frameCount;")
    h.append("    const FrameView* frames;")
    h.append("};")
    h.append("struct SoundCue { int frame; const char* file; };")
    h.append("struct TimelineInfo {")
    h.append("    int width, height, frameRate;")
    h.append("    int bgR, bgG, bgB;")
    h.append("    int frameCount;")
    h.append("    const FrameView* frames;")
    h.append("    int soundCount;")
    h.append("    const SoundCue* sounds;")
    h.append("};")
    h.append("")
    h.append("const TimelineInfo& timelineInfo();")
    h.append("")
    h.append("class SpriteTable {")
    h.append("public:")
    h.append("    SpriteTable();")
    h.append("    const SpriteDef* find(int id) const;")
    h.append("private:")
    h.append("    // small linear-probed map is overkill; we use a sorted")
    h.append("    // array + binary search filled in by the .cpp")
    h.append("    const SpriteDef** entries_;")
    h.append("    int count_;")
    h.append("};")
    h.append("")

    cpp = []
    cpp.append("// Generated by swf_to_cpp.py — do not edit.")
    cpp.append('#include "timeline_data.h"')
    cpp.append("")
    cpp.append("#include <algorithm>")
    cpp.append("")
    cpp.append("namespace {")

    uid = [0]

    def emit_items(frame_items, prefix):
        """Emit `static const DrawItem <prefix>_items[] = {...};` and return
        the array name (or None if empty)."""
        inits = []
        for it in frame_items:
            init = _draw_item_init(it)
            if init is not None:
                inits.append("    " + init + ",")
        if not inits:
            return None
        name = f"k{prefix}_items"
        cpp.append(f"const DrawItem {name}[] = {{")
        cpp.extend(inits)
        cpp.append("};")
        return name

    # ---- sprite frame tables -------------------------------------------
    sprite_frame_views = {}   # sid -> list of FrameView initializers
    for sid, sdef in sprites.items():
        views = []
        for fi, frame_items in enumerate(sdef["frames"]):
            uid[0] += 1
            arr = emit_items(frame_items, f"Spr{sid}f{fi}")
            if arr:
                n = sum(1 for it in frame_items if _draw_item_init(it))
                views.append(f"{{{arr}, {n}}}")
            else:
                views.append("{nullptr, 0}")
        sprite_frame_views[sid] = views

    # ---- sprite defs ----------------------------------------------------
    for sid, sdef in sprites.items():
        uid[0] += 1
        vname = f"kSpr{sid}_frames"
        cpp.append(f"const FrameView {vname}[] = {{")
        for v in sprite_frame_views[sid]:
            cpp.append("    " + v + ",")
        cpp.append("};")
        # store for later SpriteDef emission
        sdef["_frames_var"] = vname

    # ---- main frames ----------------------------------------------------
    main_views = []
    for fi, frame_items in enumerate(frames):
        uid[0] += 1
        arr = emit_items(frame_items, f"Main{fi}")
        if arr:
            n = sum(1 for it in frame_items if _draw_item_init(it))
            main_views.append(f"{{{arr}, {n}}}")
        else:
            main_views.append("{nullptr, 0}")
    cpp.append("const FrameView kMainFrames[] = {")
    for v in main_views:
        cpp.append("    " + v + ",")
    cpp.append("};")

    # ---- sprite defs array ----------------------------------------------
    cpp.append("const SpriteDef kSpriteDefs[] = {")
    for sid, sdef in sprites.items():
        cpp.append(f"    {{{sid}, {sdef['frameCount']}, {sdef['_frames_var']}}},")
    cpp.append("};")
    cpp.append("const int kSpriteDefCount = %d;" % len(sprites))

    # ---- sound cues ------------------------------------------------------
    cues = [c for c in timeline.get("soundCues", []) if c.get("file")]
    if cues:
        cpp.append("const SoundCue kSoundCues[] = {")
        for c in cues:
            cpp.append("    {%d, %s}," % (c["frame"], _cpp_str(c["file"])))
        cpp.append("};")
    else:
        cpp.append("const SoundCue kSoundCues[] = {};")

    # ---- public API -------------------------------------------------------
    cpp.append("} // namespace")
    cpp.append("")
    cpp.append("const TimelineInfo& timelineInfo() {")
    cpp.append("    static const TimelineInfo info = {")
    cpp.append(f"        {int(timeline['width'])}, {int(timeline['height'])}, "
               f"{max(1, int(round(timeline['frameRate'])))},")
    cpp.append(f"        {bg_r}, {bg_g}, {bg_b},")
    cpp.append(f"        {len(frames)}, kMainFrames,")
    cpp.append(f"        {len(cues)}, kSoundCues}};")
    cpp.append("    return info;")
    cpp.append("}")
    cpp.append("")
    cpp.append("SpriteTable::SpriteTable() {")
    cpp.append("    entries_ = new const SpriteDef*[kSpriteDefCount];")
    cpp.append("    count_ = kSpriteDefCount;")
    cpp.append("    for (int i = 0; i < count_; ++i) entries_[i] = &kSpriteDefs[i];")
    cpp.append("    std::sort(entries_, entries_ + count_,")
    cpp.append("              [](const SpriteDef* a, const SpriteDef* b) {"
               " return a->id < b->id; });")
    cpp.append("}")
    cpp.append("")
    cpp.append("const SpriteDef* SpriteTable::find(int id) const {")
    cpp.append("    const SpriteDef key{id, 0, nullptr};")
    cpp.append("    const SpriteDef** it = std::lower_bound(")
    cpp.append("        entries_, entries_ + count_, &key,")
    cpp.append("        [](const SpriteDef* a, const SpriteDef* b) {"
               " return a->id < b->id; });")
    cpp.append("    if (it != entries_ + count_ && (*it)->id == id) return *it;")
    cpp.append("    return nullptr;")
    cpp.append("}")
    cpp.append("")

    with open(header_path, "w") as f:
        f.write("\n".join(h))
    with open(source_path, "w") as f:
        f.write("\n".join(cpp))


def write_project(timeline, out_dir):
    src = os.path.join(out_dir, "src")
    vendor_out = os.path.join(src, "vendor")
    os.makedirs(src, exist_ok=True)
    os.makedirs(vendor_out, exist_ok=True)
    with open(os.path.join(out_dir, "CMakeLists.txt"), "w") as f:
        f.write(CPP_CMAKE)

    # copy the self-contained player sources + vendored single-header libs
    repo = _repo_dir()
    for name in PLAYER_TEMPLATE_FILES:
        shutil.copyfile(os.path.join(repo, "templates", name),
                        os.path.join(src, name))
    for name in PLAYER_VENDOR_FILES:
        shutil.copyfile(os.path.join(repo, "vendor", name),
                        os.path.join(vendor_out, name))

    generate_timeline_files(timeline,
                            os.path.join(src, "timeline_data.h"),
                            os.path.join(src, "timeline_data.cpp"))

    with open(os.path.join(out_dir, "build.sh"), "w") as f:
        f.write(BUILD_SH)
    os.chmod(os.path.join(out_dir, "build.sh"), 0o755)
    with open(os.path.join(out_dir, "build.bat"), "w") as f:
        f.write(BUILD_BAT)

    with open(os.path.join(out_dir, "README.generated.md"), "w") as f:
        f.write(f"""# Generated SWF player

Source: `{timeline['source']}` ({int(timeline['frameCount'])} frames @
{timeline['frameRate']} fps, {int(timeline['width'])}x{int(timeline['height'])} px)

Self-contained native player: software renderer, X11 (Linux) / Win32
(Windows) window, WAV+MP3 audio. No SFML or other system libraries are
required beyond a C++17 compiler and (on Linux) libX11.

## Build — Linux

    ./build.sh            # or: mkdir build && cd build && cmake .. && make

Requires: g++ or clang++, cmake >= 3.16, libx11-dev (X11 headers).

## Build — Windows

    build.bat             # or open CMakeLists.txt in your IDE

Requires: Visual Studio 2019+ or MinGW-w64 with cmake on PATH.
Produces `build\\Release\\swf_player.exe`.

## Run

Run from the project root so `assets/` resolves (or from anywhere — the
player also searches next to the executable):

    ./build/swf_player

Headless frame export (useful for CI / machines without a display):

    ./build/swf_player --frames 120 --frames-dir out/

Controls: Space pause, R restart, P pause, Esc quit.

## Limitations

ActionScript is not translated; fonts, morph shapes, blend modes and
bitmap filters are approximated or skipped (see `timeline.json`
`warnings` for details of this conversion).
""")

    with open(os.path.join(out_dir, "timeline.json"), "w") as f:
        json.dump(timeline, f, indent=1)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Convert a .swf file into a self-contained native "
                    "C++ player project (Linux/Windows).")
    ap.add_argument("input", help="input .swf file")
    ap.add_argument("output_dir", help="output directory for the project")
    ap.add_argument("--quiet", action="store_true", help="less output")
    args = ap.parse_args(argv)

    if not os.path.isfile(args.input):
        print(f"error: {args.input} not found", file=sys.stderr)
        return 1

    out = args.output_dir
    for sub in ("assets/images", "assets/sounds", "assets/binary", "src"):
        os.makedirs(os.path.join(out, sub), exist_ok=True)

    parser = SwfParser(args.input, os.path.join(out, "assets"))
    parser.parse()
    timeline = parser.finish()

    write_project(timeline, out)

    n_img = sum(1 for c in timeline["characters"].values()
                if c.get("kind") == "image")
    n_snd = sum(1 for c in timeline["characters"].values()
                if c.get("kind") == "sound")
    n_sprites = len(timeline["sprites"])
    if not args.quiet:
        print(f"SWF v{timeline['version']}: {int(timeline['width'])}x"
              f"{int(timeline['height'])} px, {timeline['frameCount']} frames "
              f"@ {timeline['frameRate']} fps")
        print(f"  images: {n_img}   sounds: {n_snd}   sprites: {n_sprites}   "
              f"timeline frames: {len(timeline['frames'])}")
        for w in timeline["warnings"][:20]:
            print(f"  warning: {w}", file=sys.stderr)
        print(f"Project written to {out}")
        print("Next: cd %s && mkdir build && cd build && cmake .. && "
              "cmake --build ." % out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
