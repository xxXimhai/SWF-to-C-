#pragma once

#include <cstdint>
#include <string>
#include <unordered_map>
#include <vector>

// A decoded image kept as 0xAARRGGBB pixels.
struct Texture {
    int w = 0;
    int h = 0;
    std::vector<uint32_t> px;
    bool ok = false;
};

class Renderer {
public:
    int width = 0;
    int height = 0;
    std::vector<uint32_t> fb;   // 0xAARRGGBB, row-major, top to bottom

    void init(int w, int h);
    void clear(uint32_t argb);

    // Directories that asset paths are resolved against (first match wins).
    void setAssetDirs(std::vector<std::string> dirs) { assetDirs_ = dirs; }

    const Texture& texture(const std::string& relPath);

    // Draw `tex` transformed by affine M = {a,b,c,d,e,f} with color
    // transform C = {mr,mg,mb,ma, ar,ag,ab,aa}.
    void drawImage(const Texture& tex, const float M[6], const float C[8]);

    // Write the framebuffer to a PNG file (RGBA). Returns false on error.
    bool savePng(const std::string& path) const;

    const uint32_t* pixels() const { return fb.data(); }

private:
    std::unordered_map<std::string, Texture> cache_;
    std::vector<std::string> assetDirs_;
};
