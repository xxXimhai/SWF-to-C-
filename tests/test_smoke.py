#!/usr/bin/env python3
"""Smoke test: run the converter on the bundled fixture and check outputs."""
import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURE = os.path.join(ROOT, "tests", "fixtures", "simple.swf")
CONVERTER = os.path.join(ROOT, "swf_to_cpp.py")


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
            "src/main.cpp",
            "src/timeline_data.h",
            "src/timeline_data.cpp",
            "CMakeLists.txt",
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

        # generated C++ must be syntactically plausible
        for rel in ("src/main.cpp", "src/timeline_data.h",
                    "src/timeline_data.cpp"):
            src = open(os.path.join(out, rel)).read()
            assert "DrawItem" in src or rel.endswith("main.cpp")

    print("smoke test passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
