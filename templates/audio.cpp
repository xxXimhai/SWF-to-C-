// Audio backend: WAV via miniaudio, MP3 via minimp3 -> PCM data source.

#include "audio.h"

#define MINIAUDIO_IMPLEMENTATION
#include "miniaudio.h"

#define MINIMP3_IMPLEMENTATION
#include "minimp3.h"

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <string>
#include <unordered_map>
#include <vector>

namespace {

struct SoundData {
    int sampleRate = 0;
    int channels = 0;
    std::vector<float> pcm;   // interleaved float32
};

// In-memory PCM data source for miniaudio (used for MP3-decoded audio).
struct PcmDataSource {
    ma_data_source_base base;
    const float* pcm = nullptr;
    ma_uint64 frames = 0;
    ma_uint32 channels = 0;
    ma_uint32 sampleRate = 0;
    ma_uint64 cursor = 0;
};

ma_result pcm_on_read(ma_data_source* pDs, void* pFramesOut,
                      ma_uint64 frameCount, ma_uint64* pFramesRead) {
    PcmDataSource* s = reinterpret_cast<PcmDataSource*>(pDs);
    ma_uint64 avail = s->frames > s->cursor ? s->frames - s->cursor : 0;
    ma_uint64 n = frameCount < avail ? frameCount : avail;
    if (pFramesOut) {
        size_t floats = static_cast<size_t>(n) * s->channels;
        std::memcpy(pFramesOut, s->pcm + s->cursor * s->channels,
                    floats * sizeof(float));
    }
    s->cursor += n;
    *pFramesRead = n;
    return n < frameCount ? MA_AT_END : MA_SUCCESS;
}

ma_result pcm_on_seek(ma_data_source* pDs, ma_uint64 frameIndex) {
    PcmDataSource* s = reinterpret_cast<PcmDataSource*>(pDs);
    s->cursor = frameIndex < s->frames ? frameIndex : s->frames;
    return MA_SUCCESS;
}

ma_result pcm_on_format(ma_data_source* pDs, ma_format* pFormat,
                        ma_uint32* pChannels, ma_uint32* pSampleRate,
                        ma_channel* pChannelMap, size_t channelMapCap) {
    PcmDataSource* s = reinterpret_cast<PcmDataSource*>(pDs);
    if (pFormat) *pFormat = ma_format_f32;
    if (pChannels) *pChannels = s->channels;
    if (pSampleRate) *pSampleRate = s->sampleRate;
    if (pChannelMap) {
        ma_channel_map_init_standard(ma_standard_channel_map_default,
                                     pChannelMap, channelMapCap,
                                     s->channels);
    }
    return MA_SUCCESS;
}

ma_result pcm_on_cursor(ma_data_source* pDs, ma_uint64* pCursor) {
    PcmDataSource* s = reinterpret_cast<PcmDataSource*>(pDs);
    if (pCursor) *pCursor = s->cursor;
    return MA_SUCCESS;
}

ma_result pcm_on_length(ma_data_source* pDs, ma_uint64* pLength) {
    PcmDataSource* s = reinterpret_cast<PcmDataSource*>(pDs);
    if (pLength) *pLength = s->frames;
    return MA_SUCCESS;
}

ma_result pcm_on_looping(ma_data_source*, ma_bool32) { return MA_SUCCESS; }

ma_data_source_vtable g_pcmVTable = {
    pcm_on_read, pcm_on_seek, pcm_on_format,
    pcm_on_cursor, pcm_on_length, pcm_on_looping, 0
};

bool initPcmSource(PcmDataSource& ds, const SoundData& sd) {
    ma_data_source_config cfg = ma_data_source_config_init();
    cfg.vtable = &g_pcmVTable;
    if (ma_data_source_init(&cfg, reinterpret_cast<ma_data_source*>(&ds))
        != MA_SUCCESS)
        return false;
    ds.pcm = sd.pcm.data();
    ds.frames = sd.pcm.size() / sd.channels;
    ds.channels = static_cast<ma_uint32>(sd.channels);
    ds.sampleRate = static_cast<ma_uint32>(sd.sampleRate);
    ds.cursor = 0;
    return true;
}

bool decodeMp3(const std::string& path, SoundData& out) {
    FILE* f = std::fopen(path.c_str(), "rb");
    if (!f) return false;
    std::fseek(f, 0, SEEK_END);
    long size = std::ftell(f);
    std::fseek(f, 0, SEEK_SET);
    if (size <= 0) {
        std::fclose(f);
        return false;
    }
    std::vector<uint8_t> d(static_cast<size_t>(size));
    if (std::fread(d.data(), 1, d.size(), f) != d.size()) {
        std::fclose(f);
        return false;
    }
    std::fclose(f);

    // skip ID3v2 tag
    size_t off = 0;
    if (d.size() > 10 && d[0] == 'I' && d[1] == 'D' && d[2] == '3') {
        size_t sz = ((d[6] & 0x7F) << 21) | ((d[7] & 0x7F) << 14) |
                    ((d[8] & 0x7F) << 7) | (d[9] & 0x7F);
        off = 10 + sz;
        if (off >= d.size()) return false;
    }

    mp3dec_t dec;
    mp3dec_init(&dec);
    mp3dec_frame_info_t info;
    mp3d_sample_t pcm[1152 * 2];
    int channels = 0, hz = 0;
    while (off < d.size()) {
        // returns samples per channel; info.frame_bytes = bytes consumed
        // (including any junk skipped before the frame sync)
        int samples = mp3dec_decode_frame(&dec, d.data() + off,
                                          static_cast<int>(d.size() - off),
                                          pcm, &info);
        if (samples <= 0 || info.frame_bytes <= 0) break;
        if (info.channels > 0 && info.hz > 0) {
            channels = info.channels;
            hz = info.hz;
        }
        size_t total = static_cast<size_t>(samples) * info.channels;
        size_t old = out.pcm.size();
        out.pcm.resize(old + total);
        for (size_t i = 0; i < total; ++i) {
            out.pcm[old + i] = pcm[i] / 32768.f;
        }
        off += static_cast<size_t>(info.frame_bytes);
    }
    if (out.pcm.empty() || !channels || !hz) return false;
    out.channels = channels;
    out.sampleRate = hz;
    return true;
}

} // namespace

struct Audio::Impl {
    ma_engine engine{};
    bool engineOk = false;
    struct Voice {
        SoundData data;         // only for MP3 (PCM) voices
        PcmDataSource src{};
        bool usePcm = false;
        ma_sound snd{};
        bool ok = false;
    };
    std::unordered_map<std::string, Voice*> voices;
};

Audio::Audio() : impl_(new Impl) {}

Audio::~Audio() {
    shutdown();
    for (auto& kv : impl_->voices) {
        if (kv.second->ok) {
            ma_sound_uninit(&kv.second->snd);
            if (kv.second->usePcm)
                ma_data_source_uninit(
                    reinterpret_cast<ma_data_source*>(&kv.second->src));
        }
        delete kv.second;
    }
    delete impl_;
}

bool Audio::init() {
    if (impl_->engineOk) return true;
    ma_engine_config cfg = ma_engine_config_init();
    if (ma_engine_init(&cfg, &impl_->engine) != MA_SUCCESS) return false;
    impl_->engineOk = true;
    return true;
}

void Audio::shutdown() {
    if (impl_->engineOk) {
        ma_engine_uninit(&impl_->engine);
        impl_->engineOk = false;
    }
}

void Audio::play(const std::string& resolvedPath) {
    if (!impl_->engineOk || resolvedPath.empty()) return;
    auto it = impl_->voices.find(resolvedPath);
    if (it == impl_->voices.end()) {
        auto* v = new Impl::Voice();
        size_t dot = resolvedPath.find_last_of('.');
        std::string ext = dot == std::string::npos
                              ? ""
                              : resolvedPath.substr(dot + 1);
        std::transform(ext.begin(), ext.end(), ext.begin(), ::tolower);
        ma_result res;
        if (ext == "mp3") {
            if (decodeMp3(resolvedPath, v->data) &&
                initPcmSource(v->src, v->data)) {
                v->usePcm = true;
                res = ma_sound_init_from_data_source(
                    &impl_->engine,
                    reinterpret_cast<ma_data_source*>(&v->src),
                    MA_SOUND_FLAG_DECODE, nullptr, &v->snd);
            } else {
                res = MA_INVALID_ARGS;
            }
        } else {
            // WAV and anything else miniaudio understands natively
            res = ma_sound_init_from_file(&impl_->engine,
                                          resolvedPath.c_str(),
                                          MA_SOUND_FLAG_DECODE, nullptr,
                                          nullptr, &v->snd);
        }
        v->ok = (res == MA_SUCCESS);
        it = impl_->voices.emplace(resolvedPath, v).first;
    }
    Impl::Voice* v = it->second;
    if (v->ok) ma_sound_start(&v->snd);
}
