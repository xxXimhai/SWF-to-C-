// X11 window backend (Linux and other Unix-like systems with libX11).

#include "platform.h"

#ifndef _WIN32

#include <X11/Xlib.h>
#include <X11/Xutil.h>
#include <X11/keysym.h>

#include <cstdio>
#include <cstring>
#include <unordered_map>

namespace {

class X11Platform : public Platform {
public:
    ~X11Platform() override { destroy(); }

    bool create(int w, int h, const char* title) override {
        display_ = XOpenDisplay(nullptr);
        if (!display_) return false;
        screen_ = DefaultScreen(display_);
        window_ = XCreateSimpleWindow(display_, RootWindow(display_, screen_),
                                      0, 0, w, h, 0,
                                      BlackPixel(display_, screen_),
                                      WhitePixel(display_, screen_));
        XStoreName(display_, window_, title);
        XSelectInput(display_, window_,
                     ExposureMask | KeyPressMask | KeyReleaseMask |
                         StructureNotifyMask);
        // handle WM_DELETE_WINDOW
        Atom wmProto = XInternAtom(display_, "WM_PROTOCOLS", False);
        Atom wmDelete = XInternAtom(display_, "WM_DELETE_WINDOW", False);
        XSetWMProtocols(display_, window_, &wmDelete, 1);
        XMapWindow(display_, window_);
        gc_ = XCreateGC(display_, window_, 0, nullptr);

        visual_ = DefaultVisual(display_, screen_);
        depth_ = DefaultDepth(display_, screen_);
        ximage_ = XCreateImage(display_, visual_, depth_, ZPixmap, 0,
                               nullptr, w, h, 32,
                               static_cast<int>(w) * 4);
        if (!ximage_) {
            destroy();
            return false;
        }
        // request 32-bit TrueColor; masks are filled in on first present
        w_ = w;
        h_ = h;
        XFlush(display_);
        return true;
    }

    void destroy() override {
        if (display_) {
            if (ximage_) {
                XDestroyImage(ximage_);
                ximage_ = nullptr;
            }
            if (gc_) {
                XFreeGC(display_, gc_);
                gc_ = nullptr;
            }
            if (window_) {
                XDestroyWindow(display_, window_);
                window_ = 0;
            }
            XCloseDisplay(display_);
            display_ = nullptr;
        }
    }

    void present(const uint32_t* pixels) override {
        if (!display_ || !ximage_ || !pixels) return;
        // copy pixels into the XImage buffer
        std::memcpy(ximage_->data, pixels,
                    static_cast<size_t>(w_) * h_ * 4);
        XPutImage(display_, window_, gc_, ximage_, 0, 0, 0, 0, w_, h_);
        XFlush(display_);
    }

    bool poll() override {
        if (!display_) return false;
        while (XPending(display_)) {
            XEvent ev;
            XNextEvent(display_, &ev);
            if (ev.type == KeyPress || ev.type == KeyRelease) {
                KeySym ks = XLookupKeysym(&ev.xkey, 0);
                int code = mapKeysym(ks);
                if (code) keys_[code] = (ev.type == KeyPress);
            } else if (ev.type == Expose) {
                // repaint handled by caller's next present
            } else if (ev.type == ClientMessage) {
                Atom wmDelete = XInternAtom(display_, "WM_DELETE_WINDOW",
                                            False);
                if (ev.xclient.data.l[0] == static_cast<long>(wmDelete))
                    closed_ = true;
            } else if (ev.type == DestroyNotify) {
                closed_ = true;
            }
        }
        return !closed_;
    }

    int width() const override { return w_; }
    int height() const override { return h_; }

    bool keyPressed(int code) const override {
        auto it = keys_.find(code);
        return it != keys_.end() && it->second;
    }

private:
    static int mapKeysym(KeySym ks) {
        switch (ks) {
        case XK_Escape: return KEY_ESC;
        case XK_space: return KEY_SPACE;
        case XK_Left: return KEY_LEFT;
        case XK_Right: return KEY_RIGHT;
        case XK_Up: return KEY_UP;
        case XK_Down: return KEY_DOWN;
        case XK_p:
        case XK_P: return KEY_P;
        case XK_r:
        case XK_R: return KEY_R;
        }
        return KEY_NONE;
    }

    Display* display_ = nullptr;
    Window window_ = 0;
    GC gc_ = nullptr;
    XImage* ximage_ = nullptr;
    Visual* visual_ = nullptr;
    int screen_ = 0;
    int depth_ = 0;
    int w_ = 0, h_ = 0;
    bool closed_ = false;
    std::unordered_map<int, bool> keys_;
};

} // namespace

Platform* createPlatform() {
    return new X11Platform();
}

#endif // !_WIN32
