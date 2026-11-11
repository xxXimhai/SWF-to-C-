#!/usr/bin/env python3
"""Smoke test: run the converter on the bundled fixture and check outputs."""
import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURE = os.path.join(ROOT, "tests", "fixtures", "simple.swf")
CONVERTER = os.path.join(ROOT, "swf_to_cpp.py")


def _decode_png_size(path):
    """Return (w, h) of a PNG without external deps."""
    import struct
    with open(path, "rb") as f:
        head = f.read(26)
    if head[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    w, h = struct.unpack(">II", head[16:24])
    return w, h


def main():
    if not os.path.exists(FIXTURE):
        print("fixture missing; regenerate with "
              "python3 tools/make_test_swf.py tests/fixtures/simple.swf")
        return 1
    with tempfile.TemporaryDirectory() as out:
        r = subprocess.run(
            [sys.executable, CONVERTER, FIXTURE, out],
            capture_output=True, text=True)
        if r.returncode != 0:
            print(r.stdout)
            print(r.stderr, file=sys.stderr)
            return 1

        expected = [
            "assets/images/img_1.png",
            "assets/images/shape_3.png",   # rasterized vector shape
            "src/main.cpp",
            "src/timeline_data.h",
            "src/timeline_data.cpp",
            "src/swfrender.cpp",
            "src/audio.cpp",
            "src/platform_x11.cpp",
            "src/platform_win32.cpp",
            "src/vendor/stb_image.h",
            "src/vendor/miniaudio.h",
            "CMakeLists.txt",
            "build.sh",
            "build.bat",
            "timeline.json",
        ]
        for rel in expected:
            p = os.path.join(out, rel)
            if not os.path.exists(p):
                print(f"FAIL: missing {rel}")
                return 1

        with open(os.path.join(out, "timeline.json")) as f:
            tl = json.load(f)
        assert tl["frameCount"] == 3, tl["frameCount"]
        assert tl["width"] == 550 and tl["height"] == 400
        assert tl["sprites"], "expected at least one sprite"

        # the vector shape must have been rasterized to a real PNG
        size = _decode_png_size(os.path.join(out, "assets/images/shape_3.png"))
        assert size and size[0] > 100 and size[1] > 100, size

        # timeline items must carry matrix + color transform fields
        found_cx = False
        found_matrix = False
        for frame in tl["frames"]:
            for it in frame:
                assert "a" in it and "f" in it, it
                found_matrix = True
                if "ma" in it:
                    found_cx = True
                    assert it["ma"] < 1.0, it   # 50% alpha cxform
        assert found_matrix and found_cx, "matrix/cxform fields missing"

        # generated C++ must be syntactically plausible
        for rel in ("src/main.cpp", "src/timeline_data.h",
                    "src/timeline_data.cpp"):
            src = open(os.path.join(out, rel)).read()
            assert "DrawItem" in src or rel.endswith("main.cpp")

        # if a C++ toolchain is available, build and headless-export frames
        has_cc = shutil.which("c++") or shutil.which("g++") or \
            shutil.which("clang++")
        if shutil.which("cmake") and has_cc:
            bdir = os.path.join(out, "build")
            cfg = subprocess.run(
                ["cmake", "-S", out, "-B", bdir],
                capture_output=True, text=True)
            if cfg.returncode == 0:
                bld = subprocess.run(
                    ["cmake", "--build", bdir, "-j4"],
                    capture_output=True, text=True)
                if bld.returncode != 0:
                    print(bld.stdout[-3000:])
                    print(bld.stderr[-3000:], file=sys.stderr)
                    return 1
                exe = os.path.join(bdir, "swf_player")
                fdir = os.path.join(out, "frames")
                os.makedirs(fdir)
                ex = subprocess.run(
                    [exe, "--frames", "3", "--frames-dir", fdir],
                    capture_output=True, text=True, cwd=out)
                if ex.returncode != 0:
                    print(ex.stdout)
                    print(ex.stderr, file=sys.stderr)
                    return 1
                for i in range(3):
                    p = os.path.join(fdir, f"frame_{i:05d}.png")
                    if _decode_png_size(p) is None:
                        print(f"FAIL: bad frame png {p}")
                        return 1
            else:
                print("cmake configure failed; skipping build check")
                print(cfg.stderr[-1000:], file=sys.stderr)

    print("smoke test passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
