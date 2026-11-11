# SWF to C++

Convert Adobe Flash `.swf` files into a self-contained C++ project that
reconstructs the timeline and replays it with [SFML](https://www.sfml-dev.org/).

This is **not** a 1:1 ActionScript→C++ translator — that is not viable. Instead,
it is an *extraction and reconstruction* pipeline:

1. **Extract assets** — images (JPEG, lossless PNG), sounds (MP3, PCM/WAV),
   sprite timelines.
2. **Rebuild the timeline** — display-list placements, frame labels and sound
   cues are parsed into a compact `timeline.json` plus generated C++ data.
3. **Generate a C++ project** — a CMake project with a `SwfAssetLoader`
   player that draws the extracted assets frame by frame.

## Requirements

- Python 3.9+ (no third-party packages; the SWF parser is built in)
- CMake 3.16+
- A C++17 compiler
- SFML 2.x development libraries

### Installing SFML

| OS      | Command                                                        |
|---------|----------------------------------------------------------------|
| Ubuntu  | `sudo apt install libsfml-dev`                                 |
| Debian  | `sudo apt install libsfml-dev`                                 |
| Fedora  | `sudo dnf install sfml-devel`                                  |
| macOS   | `brew install sfml`                                            |
| Windows | `vcpkg install sfml` or download from sfml-dev.org             |
| Conda   | `conda install -c conda-forge sfml`                             |

## Usage

```bash
python3 swf_to_cpp.py input.swf output_dir
```

This creates:

```
output_dir/
├── assets/
│   ├── images/      # extracted bitmaps as PNG/JPEG
│   └── sounds/      # extracted sounds as MP3/WAV
├── src/
│   ├── main.cpp            # SFML player + SwfAssetLoader
│   ├── timeline_data.h     # generated timeline tables (header)
│   └── timeline_data.cpp   # generated timeline tables (source)
├── timeline.json           # full extracted timeline metadata
├── CMakeLists.txt
└── README.generated.md
```

Then build and run the generated player:

```bash
cd output_dir
mkdir build && cd build
cmake .. -DCMAKE_PREFIX_PATH=<path-to-SFML>   # optional, if not on PATH
cmake --build .
./swf_player          # or swf_player.exe on Windows
```

Assets are resolved relative to the working directory, so run the binary
from `output_dir/` (or copy `assets/` next to it).

## What is supported

- SWF containers: `FWS` (uncompressed) and `CWS` (zlib-compressed).
  `ZWS` (LZMA) is rejected with a clear error.
- Tags: `ShowFrame`, `DefineShape*` (parsed for bitmap fills),
  `PlaceObject2/3`, `RemoveObject2`, `DefineBits` (JPEG), `DefineBitsJPEG2/3`,
  `DefineBitsLossless/Lossless2`, `DefineSound`, `StartSound`,
  `SoundStreamHead/Head2/Block`, `DefineSprite`, `FrameLabel`,
  `SetBackgroundColor`, `JPEGTables`.
- Timeline reconstruction: display-list snapshots per frame, sprite
  sub-timelines, frame labels and sound cues.
- Bitmap fills inside vector shapes are approximated by drawing the
  referenced bitmap (a common Flash-game pattern).

## Limitations

- **ActionScript is not translated.** All bytecode (`DoAction`,
  `DoInitAction`, sprite class scripts, event handlers) is ignored.
  Interaction/gameplay logic must be ported to C++ manually — use
  `timeline.json` and the FFDec/JPEXS decompiler for reference.
- **Vector shapes are not rendered.** Shapes without bitmap fills are
  skipped; shapes *with* bitmap fills draw the bitmap stretched over the
  fill area. Gradients, strokes and morphs are not reproduced.
- **JPEG3 alpha** is not merged — the JPEG is saved without its alpha
  channel (a warning is printed).
- `DefineBitsJPEG4` is skipped. `PlaceObject3` filters, blend modes and
  clip actions are ignored.
- Fonts (`DefineFont*`, `DefineEditText`) are not extracted yet.
- Sounds stream correctly as cues, but streaming audio blocks are only
  partially reconstructed.
- `ZWS` (LZMA-compressed) SWFs are not supported.

## Repository layout

```
swf_to_cpp.py          # the converter (single-file, stdlib only)
tools/make_test_swf.py # synthetic SWF generator used by the test suite
tests/                 # fixtures and validation scripts
```

## Development

```bash
# regenerate the bundled test fixture
python3 tools/make_test_swf.py tests/fixtures/simple.swf

# convert it and build the result
python3 swf_to_cpp.py tests/fixtures/simple.swf /tmp/simple_out
cd /tmp/simple_out/build && cmake .. && cmake --build .
```

## License

MIT — see [LICENSE](LICENSE).
