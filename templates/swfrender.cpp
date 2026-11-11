// Software renderer for the generated SWF player.
// No external dependencies: stb_image for decoding, stb_image_write for PNG.

#include "swfrender.h"

#define STB_IMAGE_IMPLEMENTATION
#include "stb_image.h"

#define STB_IMAGE_WRITE_IMPLEMENTATION
#include "stb_image_write.h"

#include <algorithm>
#include <cmath>
#include <cstdio>

void Renderer::init(int w, int h) {
    width = w;
    height = h;
    fb.assign(static_cast<size_t>(w) * h, 0xFF000000u);
}

void Renderer::clear(uint32_t argb) {
    std::fill(fb.begin(), fb.end(), argb);
}

const Texture& Renderer::texture(const std::string& relPath) {
    auto it = cache_.find(relPath);
    if (it != cache_.end()) {
        return it->second;
    }
    Texture tex;
    std::string full;
    for (const std::string& dir : assetDirs_) {
        std::string cand = dir.empty() ? relPath : dir + "/" + relPath;
        FILE* f = std::fopen(cand.c_str(), "rb");
        if (f) {
            std::fclose(f);
            full = cand;
            break;
        }
    }
    if (full.empty()) {
        // last resort: try the path verbatim
        FILE* f = std::fopen(relPath.c_str(), "rb");
        if (f) {
            std::fclose(f);
            full = relPath;
        }
    }
    if (!full.empty()) {
        int w = 0, h = 0, ch = 0;
        stbi_uc* data = stbi_load(full.c_str(), &w, &h, &ch, 4);
        if (data && w > 0 && h > 0) {
            tex.w = w;
            tex.h = h;
            tex.px.resize(static_cast<size_t>(w) * h);
            for (size_t i = 0, n = static_cast<size_t>(w) * h; i < n; ++i) {
                const stbi_uc* p = data + i * 4;
                tex.px[i] = (uint32_t(p[3]) << 24) | (uint32_t(p[0]) << 16) |
                            (uint32_t(p[1]) << 8) | uint32_t(p[2]);
            }
            tex.ok = true;

            // JPEG3 alpha: a sibling "<name>.alpha.png" grayscale mask
            // supplies the texture's alpha channel.
            std::string maskPath;
            size_t dot = full.find_last_of('.');
            if (dot != std::string::npos) {
                maskPath = full.substr(0, dot) + ".alpha.png";
            }
            if (!maskPath.empty()) {
                int mw = 0, mh = 0, mch = 0;
                stbi_uc* mask = stbi_load(maskPath.c_str(), &mw, &mh, &mch, 1);
                if (mask && mw == w && mh == h) {
                    for (size_t i = 0, n = static_cast<size_t>(w) * h;
                         i < n; ++i) {
                        tex.px[i] = (uint32_t(mask[i]) << 24) |
                                    (tex.px[i] & 0x00FFFFFFu);
                    }
                }
                if (mask) stbi_image_free(mask);
            }
        }
        if (data) stbi_image_free(data);
    }
    if (!tex.ok) {
        // 1x1 magenta placeholder so missing assets are visible
        tex.w = tex.h = 1;
        tex.px.assign(1, 0xFFFF00FFu);
        tex.ok = true;
    }
    auto res = cache_.emplace(relPath, std::move(tex));
    return res.first->second;
}

void Renderer::drawImage(const Texture& tex, const float M[6],
                         const float C[8]) {
    if (!tex.ok || tex.w <= 0 || tex.h <= 0 || fb.empty()) return;
    const float a = M[0], b = M[1], c = M[2], d = M[3], e = M[4], f = M[5];
    const float det = a * d - b * c;
    if (std::fabs(det) < 1e-9f) return;

    // screen-space bounding box of the transformed texture quad
    float x0 = a * 0.f + c * 0.f + e, y0 = b * 0.f + d * 0.f + f;
    float x1 = a * tex.w + c * 0.f + e, y1 = b * tex.w + d * 0.f + f;
    float x2 = a * 0.f + c * tex.h + e, y2 = b * 0.f + d * tex.h + f;
    float x3 = a * tex.w + c * tex.h + e, y3 = b * tex.w + d * tex.h + f;
    int minx = std::max(0, static_cast<int>(std::floor(
        std::min(std::min(x0, x1), std::min(x2, x3)))));
    int maxx = std::min(width - 1, static_cast<int>(std::ceil(
        std::max(std::max(x0, x1), std::max(x2, x3)))));
    int miny = std::max(0, static_cast<int>(std::floor(
        std::min(std::min(y0, y1), std::min(y2, y3)))));
    int maxy = std::min(height - 1, static_cast<int>(std::ceil(
        std::max(std::max(y0, y1), std::max(y2, y3)))));
    if (minx > maxx || miny > maxy) return;

    const float mr = C[0], mg = C[1], mb = C[2], ma = C[3];
    const float ar = C[4], ag = C[5], ab = C[6], aa = C[7];
    const float idet = 1.0f / det;

    for (int py = miny; py <= maxy; ++py) {
        for (int px = minx; px <= maxx; ++px) {
            // destination pixel center -> local texture coords
            float sx = px + 0.5f - e;
            float sy = py + 0.5f - f;
            float lx = (d * sx - c * sy) * idet;
            float ly = (-b * sx + a * sy) * idet;
            if (lx < 0.f || ly < 0.f || lx >= tex.w || ly >= tex.h) continue;

            // bilinear sample
            float fx = lx - 0.5f, fy = ly - 0.5f;
            int x0 = static_cast<int>(std::floor(fx));
            int y0 = static_cast<int>(std::floor(fy));
            float tx = fx - x0, ty = fy - y0;
            float r = 0.f, g = 0.f, bl = 0.f, al = 0.f;
            for (int dy = 0; dy <= 1; ++dy) {
                int yy = y0 + dy;
                if (yy < 0) yy = 0;
                if (yy >= tex.h) yy = tex.h - 1;
                float wy = dy ? ty : 1.f - ty;
                for (int dx = 0; dx <= 1; ++dx) {
                    int xx = x0 + dx;
                    if (xx < 0) xx = 0;
                    if (xx >= tex.w) xx = tex.w - 1;
                    float wx = dx ? tx : 1.f - tx;
                    float wgt = wx * wy;
                    uint32_t p = tex.px[static_cast<size_t>(yy) * tex.w + xx];
                    r += ((p >> 16) & 0xFF) * wgt;
                    g += ((p >> 8) & 0xFF) * wgt;
                    bl += (p & 0xFF) * wgt;
                    al += ((p >> 24) & 0xFF) * wgt;
                }
            }

            // color transform
            float sr = std::min(255.f, std::max(0.f, r * mr + ar * 255.f));
            float sg = std::min(255.f, std::max(0.f, g * mg + ag * 255.f));
            float sb = std::min(255.f, std::max(0.f, bl * mb + ab * 255.f));
            float sa = std::min(255.f, std::max(0.f, al * ma + aa * 255.f));
            sa /= 255.f;
            if (sa <= 0.f) continue;

            size_t di = static_cast<size_t>(py) * width + px;
            uint32_t dp = fb[di];
            float da = ((dp >> 24) & 0xFF) / 255.f;
            float oa = sa + da * (1.f - sa);
            if (oa <= 0.f) {
                fb[di] = 0;
                continue;
            }
            float k = sa / oa;
            float j = da * (1.f - sa) / oa;
            unsigned or_ = static_cast<unsigned>(sr * k +
                ((dp >> 16) & 0xFF) * j + 0.5f);
            unsigned og = static_cast<unsigned>(sg * k +
                ((dp >> 8) & 0xFF) * j + 0.5f);
            unsigned ob = static_cast<unsigned>(sb * k +
                (dp & 0xFF) * j + 0.5f);
            unsigned oa8 = static_cast<unsigned>(oa * 255.f + 0.5f);
            fb[di] = (oa8 << 24) | (or_ << 16) | (og << 8) | ob;
        }
    }
}

bool Renderer::savePng(const std::string& path) const {
    std::vector<stbi_uc> rgba(static_cast<size_t>(width) * height * 4);
    for (size_t i = 0, n = static_cast<size_t>(width) * height; i < n; ++i) {
        uint32_t p = fb[i];
        rgba[i * 4 + 0] = static_cast<stbi_uc>((p >> 16) & 0xFF);
        rgba[i * 4 + 1] = static_cast<stbi_uc>((p >> 8) & 0xFF);
        rgba[i * 4 + 2] = static_cast<stbi_uc>(p & 0xFF);
        rgba[i * 4 + 3] = static_cast<stbi_uc>((p >> 24) & 0xFF);
    }
    return stbi_write_png(path.c_str(), width, height, 4,
                          rgba.data(), width * 4) != 0;
}
