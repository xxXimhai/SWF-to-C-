# SWF to C++

Convert Adobe Flash `.swf` files into a self-contained native C++ player
that reconstructs the timeline and replays it — no SFML, no external
dependencies beyond a C++17 compiler (and libX11 on Linux).

This is **not** a 1:1 ActionScript→C++ translator — that is not viable.
Instead, it is an *extraction and reconstruction* pipeline:

1. **Extract assets** — images (JPEG, lossless PNG), sounds (MP3, PCM/WAV),
   sprite timelines.
2. **Rasterize vector shapes** — `DefineShape*` records are parsed
   (fills, gradients, strokes) and rendered to PNG in software.
3. **Rebuild the timeline** — display-list placements with full affine
   matrices and color transforms, frame labels and sound cues are parsed
   into a compact `timeline.json` plus generated C++ data.
4. **Generate a C++ project** — a CMake project with a dependency-free
   player: software renderer, X11 window on Linux, Win32 window on
   Windows, WAV/MP3 audio via miniaudio, and a headless `--frames` mode
   that exports PNG screenshots.

## Requirements

- Python 3.9+ (no third-party packages; the SWF parser is built in)
- CMake 3.16+
- A C++17 compiler
- Linux only: `libx11-dev` (X11 headers); Windows needs nothing extra

## Usage

```bash
python3 swf_to_cpp.py input.swf output_dir
```

This creates:

```
output_dir/
├── assets/
│   ├── images/      # extracted bitmaps + rasterized shapes (PNG/JPEG)
│   └── sounds/      # extracted sounds as MP3/WAV
├── src/
│   ├── main.cpp            # player entry point
│   ├── platform_*.cpp      # X11 / Win32 window backends
│   ├── swfrender.*         # software renderer (stb_image)
│   ├── audio.*             # miniaudio + minimp3 playback
│   ├── timeline_data.*     # generated timeline tables
│   └── vendor/             # vendored single-header libraries
├── timeline.json           # full extracted timeline metadata
├── CMakeLists.txt
├── build.sh / build.bat
└── README.generated.md
```

### Build — Linux

```bash
cd output_dir
./build.sh        # cmake + make
```

Requires `libx11-dev` (e.g. `sudo apt install libx11-dev`) for the
windowed mode. Everything else is vendored.

### Build — Windows

```bat
build.bat
```

Works with Visual Studio 2019+ or MinGW-w64 (cmake on PATH). Produces
`build\Release\swf_player.exe`.

### Run

```bash
./build/swf_player                  # windowed playback
./build/swf_player --frames 120     # headless: export 120 PNG frames
./build/swf_player --frames-dir out/ --frames 0   # export whole movie
```

Run from anywhere — the player resolves `assets/` relative to the
working directory and the executable location. Controls: **Space**
pause, **R** restart, **P** pause, **Esc** quit.

## What is supported

- SWF containers: `FWS` (uncompressed), `CWS` (zlib) and `ZWS` (LZMA).
- Tags: `ShowFrame`, `DefineShape`/`2`/`3`/`4` (full style parsing:
  solid/gradient/focal-gradient/bitmap fills, strokes, line joins),
  `PlaceObject2/3` (matrices incl. rotate/skew, color transforms,
  visibility; filters are tolerated but not rendered),
  `RemoveObject2`, `DefineBits*` (JPEG/lossless), `DefineSound`,
  `StartSound`, `SoundStreamHead/Head2/Block`, `DefineSprite`,
  `FrameLabel`, `SetBackgroundColor`, `JPEGTables`.
- Vector rasterization: fills with supersampled coverage, gradient and
  bitmap fills, strokes with caps/joins — emitted as PNG per shape.
- Timeline reconstruction: per-frame display lists, sprite sub-timelines,
  frame labels, sound cues.

## Limitations

- **ActionScript is not translated.** All bytecode (`DoAction`,
  `DoInitAction`, sprite class scripts, event handlers) is ignored.
  Interaction/gameplay logic must be ported to C++ manually — use
  `timeline.json` and the FFDec/JPEXS decompiler for reference.
- **Filters and blend modes** on `PlaceObject3` are approximated
  (rendered without the effect; a warning is recorded).
- **JPEG3 alpha** is not merged — the JPEG is saved without its alpha
  channel (a warning is printed).
- `DefineBitsJPEG4` is skipped; morph shapes (`DefineMorphShape*`) and
  fonts (`DefineFont*`, `DefineEditText`, `DefineText*`) are not
  extracted; text is not rendered.
- Shape rasterization is an approximation (supersampled coverage, no
  exact pixel-center rules); complex self-intersecting fills may differ
  slightly from Flash.
- Streaming audio (`SoundStreamBlock`) is only partially reconstructed.

## Repository layout

```
swf_to_cpp.py          # the converter (single-file, stdlib only)
templates/             # C++ player sources copied into generated projects
vendor/                # vendored stb_image, stb_image_write, minimp3,
                       # miniaudio single-header libraries
tools/make_test_swf.py # synthetic SWF generator used by the test suite
tests/                 # fixtures and validation scripts
meatboy.swf            # sample game used for development
```

## Development

```bash
# regenerate the bundled test fixture
python3 tools/make_test_swf.py tests/fixtures/simple.swf

# run the test suite (converts, builds the C++ player, exports frames)
python3 tests/test_smoke.py

# convert the bundled sample
python3 swf_to_cpp.py meatboy.swf /tmp/meatboy_out
cd /tmp/meatboy_out && ./build.sh && ./build/swf_player --frames 60
```

## License

MIT — see [LICENSE](LICENSE).
