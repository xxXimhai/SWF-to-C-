#!/usr/bin/env python3
"""
swf_to_cpp.py — Extract assets and timeline metadata from a .swf file and
generate a buildable C++ (SFML) project skeleton.

This is NOT an ActionScript-to-C++ translator. It is a pipeline that:

  1. Parses the SWF container (FWS/CWS; ZWS/LZMA is rejected).
  2. Extracts bitmap characters (JPEG / lossless) to PNG/JPEG files.
  3. Extracts sound characters (MP3 / PCM->WAV) and MP3 sound streams.
  4. Reconstructs the display list per frame (PlaceObject / RemoveObject /
     ShowFrame) into a compact timeline description.
  5. Emits a CMake project (src/main.cpp + src/timeline_data.h) that plays
     the extracted frames back with SFML.

Usage:
    python3 swf_to_cpp.py input.swf output_dir
"""

import argparse
import binascii
import io
import json
import os
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


def read_cxform_with_alpha(br: BitReader):
    """Skip a CXFORMWITHALPHA record."""
    has_add = br.read_bits(1)
    has_mult = br.read_bits(1)
    nbits = br.read_bits(4)
    if has_mult:
        for _ in range(4):
            br.read_bits(nbits)          # fixed 8.8 mults (RGBA)
    if has_add:
        for _ in range(4):
            br.read_sbits(nbits)         # signed adds (RGBA)


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


# ---------------------------------------------------------------------------
# Shape style scanning — we don't render vector shapes, but we record which
# bitmap characters each shape references through bitmap fill/line styles so
# the generated player can approximate them with the bitmap image.
# ---------------------------------------------------------------------------


def _read_count(buf, off, extended):
    n = buf[off]
    off += 1
    if n == 0xFF and extended:
        n = struct.unpack_from("<H", buf, off)[0]
        off += 2
    return n, off


def _skip_gradient(buf, off, with_alpha):
    flags = buf[off]
    off += 1
    ncolors = flags & 0x0F
    csize = 4 if with_alpha else 3
    off += ncolors * (1 + csize)   # ratio u8 + color
    return off


def _skip_fill_style(buf, off, with_alpha):
    """Skip one fill style; returns (bitmap_id_or_None, new_off)."""
    t = buf[off]
    off += 1
    if t == 0x00:                       # solid
        off += 4 if with_alpha else 3
        return None, off
    if t in (0x10, 0x12):               # linear / radial gradient
        off += _matrix_len(buf, off)
        return None, _skip_gradient(buf, off, with_alpha)
    if t == 0x13:                       # focal gradient
        off += _matrix_len(buf, off)
        off = _skip_gradient(buf, off, with_alpha)
        off += 4                        # focal point (2 x fixed8.8)
        return None, off
    if 0x40 <= t <= 0x43:               # bitmap fill
        bmp = struct.unpack_from("<H", buf, off)[0]
        off += 2 + _matrix_len(buf, off + 2)
        return bmp, off
    raise ValueError(f"unknown fill style type 0x{t:02x}")


def _matrix_len(buf, off):
    br = BitReader(buf[off:])
    read_matrix(br)
    return br.byte_len()


def _skip_line_style(buf, off, version):
    """Skip one line style; returns (bitmap_id_or_None, new_off).

    Shape1 line styles are width+RGB only; Shape2+ carry a full fill style.
    """
    off += 2                            # width u16
    if version == 1:
        off += 3                        # RGB
        return None, off
    return _skip_fill_style(buf, off, with_alpha=(version >= 3))


def scan_shape(tag_id, p):
    """Parse a DefineShape* tag header + style arrays.

    Returns (shape_id, width_px, height_px, [bitmap_ids]) or None.
    tag_id: 2=Shape1, 22=Shape2, 32=Shape3, 83=Shape4.
    """
    version = {2: 1, 22: 2, 32: 3, 83: 4}.get(tag_id, 2)
    with_alpha = version >= 3
    extended = version != 1
    try:
        sid = struct.unpack_from("<H", p, 0)[0]
        br = BitReader(p[2:])
        xmin, xmax, ymin, ymax = read_rect(br)
        off = 2 + br.byte_len()

        bitmaps = []
        n, off = _read_count(p, off, extended)
        for _ in range(n):
            bmp, off = _skip_fill_style(p, off, with_alpha)
            if bmp is not None:
                bitmaps.append(bmp)
        n, off = _read_count(p, off, extended)
        for _ in range(n):
            bmp, off = _skip_line_style(p, off, version)
            if bmp is not None:
                bitmaps.append(bmp)
        return sid, (xmax - xmin) / 20.0, (ymax - ymin) / 20.0, bitmaps
    except Exception:
        return None


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
            raise NotImplementedError("ZWS (LZMA) SWF files are not supported")
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
                    info["alpha"] = True
                    self.warnings.append(
                        f"image id {cid}: JPEG3 alpha channel not merged "
                        "(saved as JPEG)")
                self.chars[cid] = info
            return
        if code == 90:     # DefineBitsJPEG4
            self.warnings.append("DefineBitsJPEG4 encountered; skipped")
            return
        if code in (2, 22, 32, 83):   # DefineShape / 2 / 3 / 4
            res = scan_shape(code, p)
            if res:
                sid, w, h, bitmaps = res
                self.chars[sid] = {"kind": "shape", "w": w, "h": h,
                                   "bitmaps": bitmaps}
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

    # -- PlaceObject --------------------------------------------------------

    @staticmethod
    def _identity_matrix():
        return {"tx": 0, "ty": 0, "sx": 1.0, "sy": 1.0, "k0": 0.0, "k1": 0.0}

    def _apply_place(self, depth, char_id, matrix, name, is_move):
        old = self._display.get(depth)
        if is_move and char_id is None and old is not None:
            old["x"] = matrix["tx"] / 20.0
            old["y"] = matrix["ty"] / 20.0
            old["sx"], old["sy"] = matrix["sx"], matrix["sy"]
            if name:
                old["name"] = name
            return
        entry = {
            "depth": depth,
            "charId": char_id if char_id is not None
                      else (old or {}).get("charId"),
            "name": name or (old or {}).get("name"),
            "x": matrix["tx"] / 20.0,
            "y": matrix["ty"] / 20.0,
            "sx": matrix["sx"],
            "sy": matrix["sy"],
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
        matrix = self._identity_matrix()
        if has_matrix:
            br = BitReader(p[off:])
            matrix = read_matrix(br)
            off += br.byte_len()
        if has_color_transform:
            br = BitReader(p[off:])
            read_cxform_with_alpha(br)
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

        self._apply_place(depth, char_id, matrix, name, bool(is_move))

    def _place_object3(self, p):
        if len(p) < 4:
            return
        flags = struct.unpack_from(">H", p, 0)[0]  # big-endian bit field
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
        matrix = self._identity_matrix()
        if has_matrix:
            br = BitReader(p[off:])
            matrix = read_matrix(br)
            off += br.byte_len()
        if has_color_transform:
            br = BitReader(p[off:])
            read_cxform_with_alpha(br)
            off += br.byte_len()
        if has_ratio:
            off += 2
        name = None
        if has_name:
            name, off = read_cstring(p, off)
        if has_clip_depth:
            off += 2
        if has_filters:
            # FilterList records are variable-length; precise skipping is
            # complex, so drop this placement rather than corrupt the frame.
            self.warnings.append("PlaceObject3 with filters skipped")
            return
        if has_blend:
            off += 1
        if has_cache:
            off += 1
        if has_visibility:
            off += 1
        if has_bg:
            off += 4
        if has_clip_actions:
            off = self._skip_clip_actions(p, off, self.version >= 6)

        self._apply_place(depth, char_id, matrix, name, bool(is_move))

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
        for frame in frames:
            items = []
            for pl in frame:
                cid = pl.get("charId")
                info = self.chars.get(cid, {}) if cid is not None else {}
                kind = info.get("kind", "unknown")
                entry = {
                    "depth": pl["depth"],
                    "charId": cid,
                    "kind": kind,
                    "name": pl.get("name") or info.get("name"),
                    "x": round(pl["x"], 2),
                    "y": round(pl["y"], 2),
                    "scaleX": round(pl.get("sx", 1.0), 4),
                    "scaleY": round(pl.get("sy", 1.0), 4),
                }
                if kind == "image" and info.get("file"):
                    entry["file"] = "assets/images/" + info["file"]
                elif kind == "sprite":
                    entry["spriteId"] = cid
                elif kind == "sound" and info.get("file"):
                    entry["file"] = "assets/" + info["file"]
                items.append(entry)
                # Approximation: a placed vector shape is drawn as each of
                # its bitmap fill images (fills are the only raster content
                # we can reproduce without a full SWF renderer).
                if kind == "shape":
                    for bmp in info.get("bitmaps", []):
                        binfo = self.chars.get(bmp, {})
                        if binfo.get("kind") == "image" and binfo.get("file"):
                            items.append({
                                "depth": pl["depth"],
                                "charId": bmp,
                                "kind": "image",
                                "name": pl.get("name") or info.get("name"),
                                "x": entry["x"], "y": entry["y"],
                                "scaleX": entry["scaleX"],
                                "scaleY": entry["scaleY"],
                                "file": "assets/images/" + binfo["file"],
                            })
            out.append(items)
        return out


# ---------------------------------------------------------------------------
# C++ project generation
# ---------------------------------------------------------------------------

CPP_MAIN = r'''// Generated by swf_to_cpp.py — do not edit by hand.
// Re-run the converter to regenerate this file.
//
// NOTE: This skeleton replays the extracted SWF timeline (bitmap and
// sprite placements per frame). ActionScript logic, vector shapes and
// fonts are NOT translated — port interaction/gameplay manually.

#include <SFML/Audio.hpp>
#include <SFML/Graphics.hpp>

#include <algorithm>
#include <iostream>
#include <memory>
#include <string>
#include <unordered_map>
#include <vector>

#include "timeline_data.h"

namespace {

sf::Texture& getTexture(const std::string& path,
                        std::unordered_map<std::string, sf::Texture>& cache) {
    auto it = cache.find(path);
    if (it != cache.end()) return it->second;
    sf::Texture tex;
    if (!tex.loadFromFile(path)) {
        std::cerr << "warning: could not load texture " << path << "\n";
        sf::Image img;
        img.create(1, 1, sf::Color::Magenta);  // placeholder
        tex.loadFromImage(img);
    }
    tex.setSmooth(true);
    auto res = cache.emplace(path, std::move(tex));
    return res.first->second;
}

sf::SoundBuffer& getSound(const std::string& path,
                          std::unordered_map<std::string, sf::SoundBuffer>& cache) {
    auto it = cache.find(path);
    if (it != cache.end()) return it->second;
    sf::SoundBuffer buf;
    if (!buf.loadFromFile(path)) {
        std::cerr << "warning: could not load sound " << path << "\n";
    }
    auto res = cache.emplace(path, std::move(buf));
    return res.first->second;
}

void drawItem(const DrawItem& item, const SpriteTable& sprites,
              sf::RenderWindow& window,
              std::unordered_map<std::string, sf::Texture>& texCache,
              int frameIndex, int depth) {
    if (depth > 8) return;
    if (item.spriteId >= 0) {
        const SpriteDef* def = sprites.find(item.spriteId);
        if (!def) return;
        int f = frameIndex % def->frameCount;
        const FrameView& fv = def->frames[f];
        for (int j = 0; j < fv.count; ++j) {
            DrawItem child = fv.items[j];
            child.x += item.x;
            child.y += item.y;
            drawItem(child, sprites, window, texCache, frameIndex, depth + 1);
        }
        return;
    }
    if (!item.file || !item.file[0]) return;
    sf::Texture& tex = getTexture(item.file, texCache);
    sf::Sprite sprite(tex);
    sprite.setPosition(item.x, item.y);
    sprite.setScale(item.scaleX, item.scaleY);
    window.draw(sprite);
}

} // namespace

int main() {
    const TimelineInfo& info = timelineInfo();
    SpriteTable sprites;

    sf::RenderWindow window(
        sf::VideoMode(static_cast<unsigned>(info.width),
                      static_cast<unsigned>(info.height)),
        "SWF to C++ player", sf::Style::Close);
    window.setFramerateLimit(info.frameRate);

    std::unordered_map<std::string, sf::Texture> texCache;
    std::unordered_map<std::string, sf::SoundBuffer> sndCache;
    std::vector<std::unique_ptr<sf::Sound>> activeSounds;

    std::size_t frame = 0;
    std::size_t cueIndex = 0;

    while (window.isOpen()) {
        sf::Event event;
        while (window.pollEvent(event)) {
            if (event.type == sf::Event::Closed) window.close();
            if (event.type == sf::Event::KeyPressed &&
                event.key.code == sf::Keyboard::Escape)
                window.close();
        }

        if (frame >= static_cast<std::size_t>(info.frameCount)) {
            frame = 0;
            cueIndex = 0;
        }

        // play sound cues scheduled for this frame
        while (cueIndex < static_cast<std::size_t>(info.soundCount) &&
               info.sounds[cueIndex].frame == static_cast<int>(frame)) {
            auto snd = std::make_unique<sf::Sound>();
            snd->setBuffer(getSound(info.sounds[cueIndex].file, sndCache));
            snd->play();
            activeSounds.push_back(std::move(snd));
            ++cueIndex;
        }
        activeSounds.erase(
            std::remove_if(activeSounds.begin(), activeSounds.end(),
                           [](const std::unique_ptr<sf::Sound>& s) {
                               return s->getStatus() == sf::Sound::Stopped;
                           }),
            activeSounds.end());

        window.clear(sf::Color(info.bgR, info.bgG, info.bgB));

        const FrameView& fv = info.frames[frame];
        for (int j = 0; j < fv.count; ++j) {
            drawItem(fv.items[j], sprites, window, texCache,
                     static_cast<int>(frame), 0);
        }

        window.display();
        ++frame;
    }
    return 0;
}
'''

CPP_CMAKE = r'''cmake_minimum_required(VERSION 3.16)
project(swf_player CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

find_package(SFML 2.5 REQUIRED COMPONENTS graphics audio window system)

add_executable(swf_player src/main.cpp src/timeline_data.cpp)
target_include_directories(swf_player PRIVATE src)
target_link_libraries(swf_player PRIVATE
    sfml-graphics sfml-audio sfml-window sfml-system)
'''


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
    return ("{" + _cpp_str(file) + f", {spr}, "
            f"{it['x']}f, {it['y']}f, {it['scaleX']}f, {it['scaleY']}f}}")


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
    h.append("    float x, y;")
    h.append("    float scaleX, scaleY;")
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
    os.makedirs(src, exist_ok=True)
    with open(os.path.join(out_dir, "CMakeLists.txt"), "w") as f:
        f.write(CPP_CMAKE)
    with open(os.path.join(src, "main.cpp"), "w") as f:
        f.write(CPP_MAIN)
    generate_timeline_files(timeline,
                            os.path.join(src, "timeline_data.h"),
                            os.path.join(src, "timeline_data.cpp"))

    with open(os.path.join(out_dir, "README.generated.md"), "w") as f:
        f.write(f"""# Generated SWF player

Source: `{timeline['source']}` ({int(timeline['frameCount'])} frames @
{timeline['frameRate']} fps, {int(timeline['width'])}x{int(timeline['height'])} px)

## Build

    mkdir build && cd build
    cmake .. -DCMAKE_PREFIX_PATH=/path/to/sfml
    cmake --build .

Requires SFML 2.x (graphics, audio, window, system components).
See the main project README for install instructions.

Run from the project root so `assets/` resolves:

    ./build/swf_player
""")

    with open(os.path.join(out_dir, "timeline.json"), "w") as f:
        json.dump(timeline, f, indent=1)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Convert a .swf file into a C++/SFML project skeleton.")
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
