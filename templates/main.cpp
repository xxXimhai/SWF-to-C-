// Generated SWF player — native, dependency-free build.
// Re-run swf_to_cpp.py to regenerate assets and timeline data.
//
// NOTE: replays the extracted SWF timeline (bitmap/sprite placements per
// frame). ActionScript logic, fonts and filters are NOT translated.
//
// Usage:
//   swf_player                 play the movie in a window
//   swf_player --frames N      headless: export N frames as PNG files
//   swf_player --frames-dir DIR --frames N   choose export directory
//
// Controls: Space pause, R restart, P pause, Esc quit.

#include "timeline_data.h"

#include "audio.h"
#include "platform.h"
#include "swfrender.h"

#include <chrono>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>

#ifdef _WIN32
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>
#else
#include <unistd.h>
#endif

namespace {

// Compose parent matrix P with child matrix C: result = P * C.
void mulMatrix(const float P[6], const float C[6], float out[6]) {
    out[0] = P[0] * C[0] + P[2] * C[1];
    out[1] = P[1] * C[0] + P[3] * C[1];
    out[2] = P[0] * C[2] + P[2] * C[3];
    out[3] = P[1] * C[2] + P[3] * C[3];
    out[4] = P[0] * C[4] + P[2] * C[5] + P[4];
    out[5] = P[1] * C[4] + P[3] * C[5] + P[5];
}

// Compose color transforms: out = P applied after C.
void mulColor(const float P[8], const float C[8], float out[8]) {
    for (int i = 0; i < 4; ++i) {
        int m = i, a = i + 4;
        out[m] = P[m] * C[m];
        out[a] = P[m] * C[a] + P[a];
    }
}

const float* identityMatrix() {
    static const float I[6] = {1.f, 0.f, 0.f, 1.f, 0.f, 0.f};
    return I;
}

const float* identityColor() {
    static const float I[8] = {1.f, 1.f, 1.f, 1.f, 0.f, 0.f, 0.f, 0.f};
    return I;
}

void drawItem(const DrawItem& item, const SpriteTable& sprites,
              Renderer& r, const float M[6], const float C[8], int depth) {
    if (depth > 16) return;
    float m[6];
    mulMatrix(M, item.matrix, m);
    float c[8];
    if (item.hasCx) {
        mulColor(C, item.color, c);
    } else {
        std::memcpy(c, C, sizeof(c));
    }
    if (item.spriteId >= 0) {
        const SpriteDef* def = sprites.find(item.spriteId);
        if (!def) return;
        // sprite frame index is driven by the *current playback frame* of
        // the enclosing timeline; callers pass the frame via a thread-local
        // set in renderFrame (see below).
        extern int g_spriteFrame;
        int f = def->frameCount > 0 ? (g_spriteFrame % def->frameCount) : 0;
        const FrameView& fv = def->frames[f];
        for (int j = 0; j < fv.count; ++j) {
            drawItem(fv.items[j], sprites, r, m, c, depth + 1);
        }
        return;
    }
    if (!item.file || !item.file[0]) return;
    const Texture& tex = r.texture(item.file);
    r.drawImage(tex, m, c);
}

int g_spriteFrame = 0;

void renderFrame(int frameIndex, Renderer& r, const SpriteTable& sprites) {
    const TimelineInfo& info = timelineInfo();
    g_spriteFrame = frameIndex;
    const FrameView& fv = info.frames[frameIndex % info.frameCount];
    r.clear(0xFF000000u | (uint32_t(info.bgR) << 16) |
            (uint32_t(info.bgG) << 8) | uint32_t(info.bgB));
    for (int j = 0; j < fv.count; ++j) {
        drawItem(fv.items[j], sprites, r, identityMatrix(), identityColor(),
                 0);
    }
}

std::string resolveAsset(const std::string& rel, const std::string& exeDir) {
    if (rel.empty()) return rel;
    FILE* f = std::fopen(rel.c_str(), "rb");
    if (f) {
        std::fclose(f);
        return rel;
    }
    std::string cand = exeDir.empty() ? rel : exeDir + "/" + rel;
    f = std::fopen(cand.c_str(), "rb");
    if (f) {
        std::fclose(f);
        return cand;
    }
    return rel;
}

void playCuesForFrame(int frame, Audio& audio, const std::string& exeDir) {
    const TimelineInfo& info = timelineInfo();
    for (int i = 0; i < info.soundCount; ++i) {
        const SoundCue& cue = info.sounds[i];
        if (cue.frame == frame && cue.file && cue.file[0]) {
            audio.play(resolveAsset(cue.file, exeDir));
        }
    }
}

} // namespace

int main(int argc, char** argv) {
    int exportFrames = -1;   // -1 = interactive, >=0 = headless export count
    std::string frameDir = ".";
    for (int i = 1; i < argc; ++i) {
        std::string a = argv[i];
        if (a == "--frames" && i + 1 < argc) {
            exportFrames = std::atoi(argv[++i]);
        } else if (a == "--frames-dir" && i + 1 < argc) {
            frameDir = argv[++i];
        } else if (a == "--help" || a == "-h") {
            std::printf("usage: %s [--frames N] [--frames-dir DIR]\n", argv[0]);
            return 0;
        }
    }

    const TimelineInfo& info = timelineInfo();
    if (info.frameCount <= 0 || info.width <= 0 || info.height <= 0) {
        std::fprintf(stderr, "empty timeline; nothing to play\n");
        return 1;
    }
    SpriteTable sprites;

    // asset lookup: try cwd first, then directory of the executable
    std::string exeDir;
#ifdef _WIN32
    char buf[MAX_PATH];
    DWORD n = GetModuleFileNameA(nullptr, buf, MAX_PATH);
    if (n > 0 && n < MAX_PATH) {
        std::string p(buf, n);
        size_t s = p.find_last_of("\\/");
        if (s != std::string::npos) exeDir = p.substr(0, s);
    }
#else
    char buf[4096];
    ssize_t n = readlink("/proc/self/exe", buf, sizeof(buf) - 1);
    if (n > 0) {
        buf[n] = 0;
        std::string p(buf);
        size_t s = p.find_last_of('/');
        if (s != std::string::npos) exeDir = p.substr(0, s);
    }
#endif

    Renderer renderer;
    renderer.setAssetDirs({"", exeDir});
    renderer.init(static_cast<int>(info.width),
                  static_cast<int>(info.height));

    Audio audio;
    audio.init();   // silently disabled if no output device

    // ---- headless frame export -------------------------------------------
    if (exportFrames >= 0) {
        int total = exportFrames > 0 ? exportFrames : info.frameCount;
        for (int i = 0; i < total; ++i) {
            int f = i % info.frameCount;
            renderFrame(f, renderer, sprites);
            char name[512];
            std::snprintf(name, sizeof(name), "%s/frame_%05d.png",
                          frameDir.c_str(), i);
            if (!renderer.savePng(name)) {
                std::fprintf(stderr, "error: could not write %s\n", name);
                return 1;
            }
            playCuesForFrame(f, audio, exeDir);   // harmless if no device
        }
        std::printf("exported %d frames to %s\n", total, frameDir.c_str());
        return 0;
    }

    // ---- interactive playback --------------------------------------------
    Platform* platform = createPlatform();
    if (!platform ||
        !platform->create(static_cast<int>(info.width),
                          static_cast<int>(info.height),
                          "SWF player")) {
        std::fprintf(stderr,
                     "no display available; use --frames N for headless "
                     "export\n");
        delete platform;
        return 1;
    }

    const double frameTime = 1.0 / (info.frameRate > 0 ? info.frameRate : 12);
    int frame = 0;
    bool paused = false;
    auto last = std::chrono::steady_clock::now();
    double acc = 0.0;

    while (platform->poll()) {
        if (platform->keyPressed(KEY_ESC)) break;
        if (platform->keyPressed(KEY_R)) {
            frame = 0;
            acc = 0.0;
        }
        if (platform->keyPressed(KEY_SPACE) || platform->keyPressed(KEY_P)) {
            paused = !paused;
        }

        auto now = std::chrono::steady_clock::now();
        double dt = std::chrono::duration<double>(now - last).count();
        last = now;
        if (!paused) {
            acc += dt;
            while (acc >= frameTime) {
                acc -= frameTime;
                ++frame;
            }
        }
        frame %= info.frameCount;

        renderFrame(frame, renderer, sprites);
        playCuesForFrame(frame, audio, exeDir);
        platform->present(renderer.pixels());
    }

    platform->destroy();
    delete platform;
    return 0;
}
