// Win32 window backend (Windows).

#include "platform.h"

#ifdef _WIN32

#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>

#include <unordered_map>

namespace {

class Win32Platform : public Platform {
public:
    ~Win32Platform() override { destroy(); }

    bool create(int w, int h, const char* title) override {
        w_ = w;
        h_ = h;
        WNDCLASSW wc{};
        wc.lpfnWndProc = &Win32Platform::wndProc;
        wc.hInstance = GetModuleHandleW(nullptr);
        wc.lpszClassName = L"SwfPlayerWindow";
        wc.hCursor = LoadCursor(nullptr, IDC_ARROW);
        wc.hbrBackground = static_cast<HBRUSH>(GetStockObject(BLACK_BRUSH));
        if (!RegisterClassW(&wc) && GetLastError() != ERROR_CLASS_ALREADY_EXISTS)
            return false;
        hwnd_ = CreateWindowW(wc.lpszClassName, utf8ToWide(title).c_str(),
                              WS_OVERLAPPED | WS_CAPTION | WS_SYSMENU |
                                  WS_MINIMIZEBOX,
                              CW_USEDEFAULT, CW_USEDEFAULT, w, h,
                              nullptr, nullptr, wc.hInstance, this);
        if (!hwnd_) return false;
        ShowWindow(hwnd_, SW_SHOW);
        return true;
    }

    void destroy() override {
        if (hwnd_) {
            DestroyWindow(hwnd_);
            hwnd_ = nullptr;
        }
    }

    void present(const uint32_t* pixels) override {
        lastPixels_ = pixels;
        if (!hwnd_) return;
        HDC hdc = GetDC(hwnd_);
        if (hdc) {
            blit(hdc);
            ReleaseDC(hwnd_, hdc);
        }
    }

    void blit(HDC hdc) {
        BITMAPINFO bmi{};
        bmi.bmiHeader.biSize = sizeof(BITMAPINFOHEADER);
        bmi.bmiHeader.biWidth = w_;
        bmi.bmiHeader.biHeight = -h_;   // top-down
        bmi.bmiHeader.biPlanes = 1;
        bmi.bmiHeader.biBitCount = 32;
        bmi.bmiHeader.biCompression = BI_RGB;
        SetDIBitsToDevice(hdc, 0, 0, w_, h_, 0, 0, 0, h_, lastPixels_, &bmi,
                          DIB_RGB_COLORS);
    }

    bool poll() override {
        MSG msg;
        while (PeekMessageW(&msg, nullptr, 0, 0, PM_REMOVE)) {
            if (msg.message == WM_QUIT) {
                closed_ = true;
            } else {
                TranslateMessage(&msg);
                DispatchMessageW(&msg);
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
    static std::wstring utf8ToWide(const char* s) {
        if (!s || !*s) return L"";
        int n = MultiByteToWideChar(CP_UTF8, 0, s, -1, nullptr, 0);
        std::wstring out(n - 1, L'\0');
        MultiByteToWideChar(CP_UTF8, 0, s, -1, out.data(), n);
        return out;
    }

    static LRESULT CALLBACK wndProc(HWND hwnd, UINT msg, WPARAM wp,
                                    LPARAM lp) {
        Win32Platform* self =
            reinterpret_cast<Win32Platform*>(GetWindowLongPtrW(hwnd, GWLP_USERDATA));
        switch (msg) {
        case WM_NCCREATE: {
            auto* cs = reinterpret_cast<CREATESTRUCTW*>(lp);
            self = static_cast<Win32Platform*>(cs->lpCreateParams);
            SetWindowLongPtrW(hwnd, GWLP_USERDATA,
                              reinterpret_cast<LONG_PTR>(self));
            return DefWindowProcW(hwnd, msg, wp, lp);
        }
        case WM_ERASEBKGND:
            return 1;
        case WM_PAINT: {
            PAINTSTRUCT ps;
            HDC hdc = BeginPaint(hwnd, &ps);
            if (self && self->lastPixels_)
                self->blit(hdc);
            EndPaint(hwnd, &ps);
            return 0;
        }
        case WM_KEYDOWN:
        case WM_KEYUP:
            if (self) {
                int code = mapKey(static_cast<int>(wp));
                if (code) self->keys_[code] = (msg == WM_KEYDOWN);
            }
            break;
        case WM_CLOSE:
            if (self) self->closed_ = true;
            return 0;
        case WM_DESTROY:
            PostQuitMessage(0);
            return 0;
        }
        return DefWindowProcW(hwnd, msg, wp, lp);
    }

    static int mapKey(int vk) {
        switch (vk) {
        case VK_ESCAPE: return KEY_ESC;
        case VK_SPACE: return KEY_SPACE;
        case VK_LEFT: return KEY_LEFT;
        case VK_RIGHT: return KEY_RIGHT;
        case VK_UP: return KEY_UP;
        case VK_DOWN: return KEY_DOWN;
        case VK_P: return KEY_P;
        case VK_R: return KEY_R;
        }
        return KEY_NONE;
    }

    HWND hwnd_ = nullptr;
    int w_ = 0, h_ = 0;
    bool closed_ = false;
    const uint32_t* lastPixels_ = nullptr;
    std::unordered_map<int, bool> keys_;
};

} // namespace

Platform* createPlatform() {
    return new Win32Platform();
}

#endif // _WIN32
