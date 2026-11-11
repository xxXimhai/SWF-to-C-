#pragma once

#include <cstdint>
#include <string>

// Minimal windowing abstraction. Implemented per-OS in
// platform_win32.cpp / platform_x11.cpp.

// Anonymous enum: avoids colliding with X11's `KeyCode` typedef.
enum {
    KEY_NONE = 0,
    KEY_ESC,
    KEY_SPACE,
    KEY_LEFT,
    KEY_RIGHT,
    KEY_UP,
    KEY_DOWN,
    KEY_P,
    KEY_R
};

class Platform {
public:
    virtual ~Platform() {}
    virtual bool create(int w, int h, const char* title) = 0;
    virtual void destroy() = 0;
    // Present the framebuffer (0xAARRGGBB, tightly packed rows).
    virtual void present(const uint32_t* pixels) = 0;
    // Process pending events; returns false when the window should close.
    virtual bool poll() = 0;
    virtual int width() const = 0;
    virtual int height() const = 0;
    virtual bool keyPressed(int code) const = 0;
};

// Returns nullptr if no usable display is available.
Platform* createPlatform();
