#pragma once

#include <string>

// Audio playback: WAV (PCM) and MP3 decoding + miniaudio output.
// All decoding happens up-front into float32 PCM; voices are cached.

class Audio {
public:
    Audio();
    ~Audio();

    // Initialize the output device. Returns false if unavailable
    // (playback silently disabled).
    bool init();
    void shutdown();

    // Play an asset file ("sounds/x.wav" or ".mp3"). Missing/undecodable
    // files are ignored.
    void play(const std::string& resolvedPath);

private:
    struct Impl;
    Impl* impl_;
};
