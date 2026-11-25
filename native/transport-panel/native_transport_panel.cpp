// Windows transport for Astra's existing footer slot. The window handles
// input and renders supplied state. It does not open or own the audio device.
#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#define _WIN32_WINNT 0x0A00
#include <windows.h>
#include <windowsx.h>
#include <objidl.h>
#include <gdiplus.h>

#include <algorithm>
#include <array>
#include <cmath>
#include <cstdint>
#include <cwchar>
#include <iterator>
#include <limits>
#include <new>

#pragma comment(lib, "gdiplus.lib")

#define API extern "C" __declspec(dllexport)

namespace {
constexpr wchar_t panel_class[] = L"AstraNativeTransportBar_2";
constexpr int max_sources = 24;
constexpr size_t label_capacity = 96;
constexpr int seek_interval_ms = 60;
constexpr UINT_PTR seek_confirmation_timer = 1;
constexpr UINT seek_confirmation_ms = 2000;
constexpr UINT web_confirmation_ms = 5500;
constexpr UINT seek_timer_interval_ms = 125;
constexpr UINT_PTR visual_frame_timer = 2;
constexpr UINT frame_interval_ms = 16;
constexpr int play_release_ms = 90;
constexpr double clock_horizon = .05;
constexpr double clock_step_slack = .03;

using Submit = int (*)(int route, int kind, int source, double seconds);
using Auxiliary = int (*)(int kind, int source, double value);
enum class Part : int { none = 0, play = 1, timeline = 2, source = 3, stop = 5, volume = 6 };

struct PanelMetrics {
    uint32_t size = sizeof(PanelMetrics);
    uint32_t version = 1;
    uint64_t serial = 0;
    int32_t route = 0;
    int32_t kind = 0;
    int32_t source = -1;
    int32_t result = 0;
    double seconds = 0;
    int64_t input_qpc = 0;
    int64_t submit_qpc = 0;
    int64_t return_qpc = 0;
};
static_assert(sizeof(PanelMetrics) == 64, "Stable Win64 C ABI");

struct PreviewState {
    uint32_t size = sizeof(PreviewState);
    uint32_t version = 1;
    int32_t gesture = 0;
    int32_t phase = 0;
    int32_t route = 0;
    int32_t sequence = 0;
    double seconds = 0;
    double displayed = 0;
    int32_t remote_revision = 0;
    int32_t remote_phase = 0;
};
static_assert(sizeof(PreviewState) == 48, "Stable Win64 C ABI");

struct MotionMetrics {
    uint32_t size = sizeof(MotionMetrics);
    uint32_t version = 1;
    uint64_t paint_count = 0;
    uint64_t glyph_changes = 0;
    int64_t input_qpc = 0;
    int64_t first_input_paint_qpc = 0;
    int64_t glyph_paint_qpc = 0;
    int64_t rail_paint_qpc = 0;
    double displayed = 0;
    double painted = 0;
    double sampled_at = 0;
    double press = 0;
    int32_t playing = 0;
    int32_t moving = 0;
};
static_assert(sizeof(MotionMetrics) == 96, "Stable Win64 C ABI");

enum class SeekPhase : int { idle = 0, held = 1, pending = 2, confirmed = 3 };

struct SeekVisual {
    SeekPhase phase = SeekPhase::idle;
    double seconds = 0;
    int sequence = 0;
    int route = 0;
    int64_t issued = 0;
};

int64_t counter() noexcept {
    LARGE_INTEGER value{};
    QueryPerformanceCounter(&value);
    return value.QuadPart;
}

int64_t frequency() noexcept {
    LARGE_INTEGER value{};
    QueryPerformanceFrequency(&value);
    return value.QuadPart;
}

struct BackBuffer {
    HDC dc = nullptr;
    HBITMAP bitmap = nullptr;
    HGDIOBJ previous = nullptr;
    LONG width = 0;
    LONG height = 0;
    HFONT font = nullptr;
    HFONT current_font = nullptr;
    int font_dpi = 0;

    ~BackBuffer() {
        clear();
        if (font) DeleteObject(font);
        if (current_font) DeleteObject(current_font);
    }

    HGDIOBJ text_font(int dpi, bool current = false) noexcept {
        if (!font || dpi != font_dpi) {
            if (font) DeleteObject(font);
            if (current_font) DeleteObject(current_font);
            current_font = CreateFontW(-MulDiv(16, dpi, 96), 0, 0, 0, FW_NORMAL, FALSE, FALSE, FALSE,
                                       DEFAULT_CHARSET, OUT_DEFAULT_PRECIS, CLIP_DEFAULT_PRECIS,
                                       CLEARTYPE_QUALITY, DEFAULT_PITCH, L"Segoe UI");
            font = CreateFontW(-MulDiv(13, dpi, 96), 0, 0, 0, FW_NORMAL, FALSE, FALSE, FALSE,
                               DEFAULT_CHARSET, OUT_DEFAULT_PRECIS, CLIP_DEFAULT_PRECIS,
                               CLEARTYPE_QUALITY, DEFAULT_PITCH, L"Microsoft YaHei UI");
            font_dpi = dpi;
        }
        const auto selected = current ? current_font : font;
        return selected ? selected : GetStockObject(DEFAULT_GUI_FONT);
    }

    void clear() noexcept {
        if (bitmap) {
            SelectObject(dc, previous);
            DeleteObject(bitmap);
            bitmap = nullptr;
        }
        if (dc) {
            DeleteDC(dc);
            dc = nullptr;
        }
    }

    HDC resize(HDC output, LONG w, LONG h) noexcept {
        if (bitmap && w == width && h == height) return dc;
        clear();
        dc = CreateCompatibleDC(output);
        if (!dc) return output;
        bitmap = CreateCompatibleBitmap(output, w, h);
        if (!bitmap) {
            clear();
            return output;
        }
        previous = SelectObject(dc, bitmap);
        width = w;
        height = h;
        return dc;
    }
};

using SourceLabel = std::array<wchar_t, label_capacity>;
struct Panel {
    Submit submit = nullptr;
    Auxiliary auxiliary = nullptr;
    int route = 0;
    int source = -1;
    int source_count = 0;
    int volume = 65;
    bool ready = false;
    bool playing = false;
    double duration = 0;
    double heard = 0;
    double sampled_at = 0;
    double visual_anchor = 0;
    int64_t visual_started = 0;
    double rate = 1;
    bool moving = false;
    bool frame_timer = false;
    int64_t play_released = 0;
    std::array<SourceLabel, max_sources> labels{};
    uint64_t view_generation = 0;

    int heard_sequence = 0;
    int gesture = 0;
    int remote_revision = 0;
    SeekVisual local_seek;
    SeekVisual remote_seek;
    int64_t qpc_frequency = frequency();
    int64_t last_drag_submit = 0;
    double last_drag_position = -1;

    Part pressed = Part::none;
    Part hover = Part::none;
    Part focus = Part::play;
    bool keyboard_focus = false;
    bool source_menu_open = false;
    LONG drag_start = 0;
    double drag_position = 0;
    int drag_volume = 65;
    bool drag_moved = false;
    BackBuffer buffer;
    PanelMetrics last;
    MotionMetrics motion;
    bool glyph_painted = false;
    ULONG_PTR graphics_token = 0;
};

struct Layout {
    RECT client{};
    RECT tray{};
    RECT source{};
    RECT play{};
    RECT stop{};
    RECT current{};
    RECT slash{};
    RECT timeline{};
    RECT duration{};
    RECT speaker{};
    RECT volume{};
    RECT volume_value{};
    int dpi = 96;
};

LRESULT CALLBACK panel_proc(HWND hwnd, UINT msg, WPARAM wp, LPARAM lp);

Panel* local_panel(HWND hwnd) noexcept {
    if (!hwnd || GetWindowThreadProcessId(hwnd, nullptr) != GetCurrentThreadId()
        || GetClassLongPtrW(hwnd, GCLP_WNDPROC) != reinterpret_cast<ULONG_PTR>(&panel_proc)) {
        return nullptr;
    }
    return reinterpret_cast<Panel*>(GetWindowLongPtrW(hwnd, GWLP_USERDATA));
}

int scaled(int pixels, int dpi) noexcept { return MulDiv(pixels, dpi, 96); }

Layout layout(HWND hwnd) noexcept {
    Layout l{};
    GetClientRect(hwnd, &l.client);
    l.dpi = static_cast<int>(GetDpiForWindow(hwnd));
    if (!l.dpi) l.dpi = 96;
    const auto dp = [&l](int pixels) { return scaled(pixels, l.dpi); };
    // Match the web workspace gutter when the tray sits below its navigation rail.
    const int side = l.client.right >= dp(700) ? dp(72 + (l.client.right >= dp(820) ? 12 : 8)) : dp(8);
    l.tray = {side, dp(6), std::max(side + 1L, l.client.right - dp(12)),
              std::max(dp(7) * 1L, l.client.bottom - dp(5))};
    const int tray_width = static_cast<int>(l.tray.right - l.tray.left);
    const bool compact = tray_width < dp(620);
    const bool narrow = tray_width < dp(490);
    const int gap = dp(narrow ? 4 : 7);
    const int middle = std::min(dp(33), static_cast<int>(l.client.bottom) / 2 - dp(5));
    const int source_width = dp(narrow ? 82 : compact ? 150 : 240);
    const int play_width = dp(42), stop_width = dp(30);
    const int current_width = dp(narrow ? 40 : compact ? 50 : 60), duration_width = dp(narrow ? 34 : compact ? 43 : 48);
    const int slash_width = dp(9), speaker_width = dp(32), volume_width = dp(narrow ? 54 : 79);
    const int group = play_width + stop_width + source_width + current_width + duration_width
        + slash_width + speaker_width + volume_width + 5 * gap;
    const int group_left = static_cast<int>(l.tray.left) + std::max(dp(7), (tray_width - group) / 2);
    int left = group_left;
    const auto take = [&](int width, int height, int after) {
        const RECT box{left, middle - height / 2, left + width, middle + (height + 1) / 2};
        left += width + after;
        return box;
    };
    l.play = take(play_width, dp(42), gap);
    l.stop = take(stop_width, dp(34), gap);
    l.source = take(source_width, dp(33), gap);
    l.current = take(current_width, dp(34), 0);
    l.slash = take(slash_width, dp(34), 0);
    l.duration = take(duration_width, dp(34), gap);
    l.speaker = take(speaker_width, dp(32), 0);
    l.volume = take(volume_width, dp(32), gap);
    l.volume_value = {l.volume.right - dp(narrow ? 23 : 28), l.volume.top,
                      l.volume.right, l.volume.bottom};
    const int rail_y = std::min(dp(67), static_cast<int>(l.client.bottom) - dp(10));
    l.timeline = {group_left, std::max(middle + dp(21), rail_y - dp(10)),
                  std::max(group_left + 1L, std::min(l.tray.right - dp(7), l.volume.right)),
                  rail_y + dp(10)};
    return l;
}

RECT part_rect(const Layout& l, Part part) noexcept {
    switch (part) {
    case Part::source: return l.source;
    case Part::play: return l.play;
    case Part::stop: return l.stop;
    case Part::timeline: return l.timeline;
    case Part::volume: return l.volume;
    default: return {};
    }
}

bool available(const Panel& panel, Part part) noexcept {
    if (!panel.ready) return false;
    switch (part) {
    case Part::play: return panel.route || panel.auxiliary;
    case Part::timeline: return panel.duration > 0 && (panel.route || panel.auxiliary);
    case Part::source: return panel.source_count > 1 && (panel.route || panel.auxiliary);
    case Part::stop:
    case Part::volume: return panel.auxiliary != nullptr;
    default: return false;
    }
}

constexpr std::array<Part, 5> tab_order{Part::source, Part::play, Part::stop, Part::timeline, Part::volume};

bool keyboard_modifier(int key) noexcept { return (GetKeyState(key) & 0x8000) != 0; }

bool transport_space_modified() noexcept {
    return keyboard_modifier(VK_SHIFT) || keyboard_modifier(VK_CONTROL) || keyboard_modifier(VK_MENU)
        || keyboard_modifier(VK_LWIN) || keyboard_modifier(VK_RWIN);
}

Part hit(const Layout& l, const Panel& panel, POINT point) noexcept {
    for (Part part : tab_order) {
        const RECT area = part_rect(l, part);
        if (available(panel, part) && PtInRect(&area, point)) return part;
    }
    return Part::none;
}

RECT rail_inside(RECT area, int dpi) noexcept {
    const int margin = scaled(8, dpi);
    const LONG middle = (area.top + area.bottom) / 2;
    const int thickness = std::max(1, scaled(2, dpi));
    area.left += margin;
    area.right = std::max(area.left + 1, area.right - margin);
    area.top = middle - thickness;
    area.bottom = middle + thickness;
    return area;
}

RECT volume_rail(const Layout& l) noexcept {
    RECT box = l.volume;
    box.right = l.volume_value.left + scaled(4, l.dpi);
    return rail_inside(box, l.dpi);
}

double fraction_at(RECT rail, LONG x) noexcept {
    return std::clamp(double(x - rail.left) / std::max(1L, rail.right - rail.left - 1), 0.0, 1.0);
}

double position_on(RECT rail, double value) noexcept {
    return rail.left + value * std::max(1L, rail.right - rail.left - 1);
}

double clock_position(const Panel& panel, int64_t now) noexcept {
    if (!panel.moving) return panel.heard;
    const auto elapsed = std::clamp(
        static_cast<double>(now - panel.visual_started) / static_cast<double>(panel.qpc_frequency),
        0.0, clock_horizon);
    return std::min(panel.heard, panel.visual_anchor + elapsed * panel.rate);
}

double display_position(const Panel& panel, int64_t now = counter()) noexcept {
    if (panel.pressed == Part::timeline) return panel.drag_position;
    if (panel.remote_seek.phase != SeekPhase::idle) return panel.remote_seek.seconds;
    if (panel.local_seek.phase == SeekPhase::pending) return panel.local_seek.seconds;
    return clock_position(panel, now);
}

// CSS cubic-bezier(.2,.7,.3,1): invert the time axis, then evaluate easing.
double release_ease(double time) noexcept {
    double left = 0, right = 1;
    for (int i = 0; i < 12; ++i) {
        const double u = (left + right) * .5;
        const double v = 1 - u;
        const double x = .6 * v * v * u + .9 * v * u * u + u * u * u;
        if (x < time) left = u;
        else right = u;
    }
    const auto u = (left + right) * .5;
    const auto v = 1 - u;
    return 2.1 * v * v * u + 3 * v * u * u + u * u * u;
}

double play_press(const Panel& panel, int64_t now) noexcept {
    if (panel.pressed == Part::play) return 1;
    if (!panel.play_released) return 0;
    const auto duration = panel.qpc_frequency * play_release_ms / 1000;
    const auto elapsed = now - panel.play_released;
    if (elapsed >= duration || duration <= 0) return 0;
    if (elapsed <= 0) return 1;
    return 1 - release_ease(static_cast<double>(elapsed) / static_cast<double>(duration));
}

double playback_fraction(double at, double duration) noexcept {
    return duration > 0 ? std::clamp(at / duration, 0.0, 1.0) : 0.0;
}

long long clock_second(double value) noexcept {
    return static_cast<long long>(std::clamp(value, 0.0, 315360000.0));
}

void clock_text(double value, wchar_t (&text)[24]) noexcept {
    const auto total = clock_second(value);
    if (total >= 3600) {
        std::swprintf(text, 24, L"%lld:%02lld:%02lld", total / 3600, total / 60 % 60, total % 60);
    } else {
        std::swprintf(text, 24, L"%lld:%02lld", total / 60, total % 60);
    }
}

const wchar_t* source_label(const Panel& panel, int source) noexcept {
    if (source >= 0 && source < panel.source_count && panel.labels[source][0]) {
        return panel.labels[source].data();
    }
    if (source <= 0) return L"原声";
    return L"分轨";
}

Gdiplus::Color paint_color(COLORREF color, BYTE opacity = 255) noexcept {
    return {opacity, GetRValue(color), GetGValue(color), GetBValue(color)};
}

Gdiplus::RectF float_rect(RECT rect) noexcept {
    return {static_cast<float>(rect.left), static_cast<float>(rect.top),
            static_cast<float>(rect.right - rect.left), static_cast<float>(rect.bottom - rect.top)};
}

void fill_rounded(Gdiplus::Graphics& graphics, const Gdiplus::RectF& box, float diameter,
                  Gdiplus::Color color) noexcept {
    if (box.Width <= 0 || box.Height <= 0) return;
    const float d = std::min({diameter, box.Width, box.Height});
    Gdiplus::SolidBrush brush(color);
    if (d < 1) {
        graphics.FillRectangle(&brush, box);
        return;
    }
    Gdiplus::GraphicsPath path;
    const float x = box.GetRight() - d, y = box.GetBottom() - d;
    path.AddArc(box.X, box.Y, d, d, 180.f, 90.f);
    path.AddArc(x, box.Y, d, d, 270.f, 90.f);
    path.AddArc(x, y, d, d, 0.f, 90.f);
    path.AddArc(box.X, y, d, d, 90.f, 90.f);
    path.CloseFigure();
    graphics.FillPath(&brush, &path);
}

void paint(HWND hwnd, HDC output, Panel& panel, RECT dirty) noexcept {
    const Layout l = layout(hwnd);
    const RECT r = l.client;
    if (r.right <= 0 || r.bottom <= 0) return;
    const auto now = counter();
    const HDC dc = panel.buffer.resize(output, r.right, r.bottom);
    const auto distance = [&l](float units) { return units * static_cast<float>(l.dpi) / 96.f; };
    const auto has_dirty = [&dirty](RECT region) {
        RECT overlap{};
        return IntersectRect(&overlap, &region, &dirty) != 0;
    };
    const bool can_play = available(panel, Part::play);
    const auto press = play_press(panel, now);
    const auto shown = display_position(panel, now);
    const int volume_value = panel.pressed == Part::volume ? panel.drag_volume : panel.volume;
    const auto text = panel.ready ? RGB(47, 54, 81) : RGB(120, 130, 153);
    const auto accent = RGB(67, 107, 196);
    const auto timeline = panel.ready ? RGB(114, 100, 167) : RGB(163, 169, 189);
    const auto source_box = float_rect(l.source);
    const auto play_box = float_rect(l.play);
    const float glyph_x = play_box.X + play_box.Width / 2;
    const float glyph_y = play_box.Y + play_box.Height / 2;
    {
        using namespace Gdiplus;
        Graphics graphics(dc);
        graphics.SetClip(float_rect(dirty));
        graphics.SetSmoothingMode(SmoothingModeAntiAlias);
        graphics.SetPixelOffsetMode(PixelOffsetModeHalf);
        const auto dot = [&graphics](float x, float y, float radius, Color color) {
            SolidBrush brush(color);
            graphics.FillEllipse(&brush, x - radius, y - radius, 2 * radius, 2 * radius);
        };
        const auto lines = [&graphics, &distance](const PointF* points, int count, COLORREF color,
                                                  float width) {
            Pen pen(paint_color(color), distance(width));
            pen.SetLineJoin(LineJoinRound);
            pen.SetStartCap(LineCapRound);
            pen.SetEndCap(LineCapRound);
            graphics.DrawLines(&pen, points, count);
        };
        const auto rounded = [&graphics, &distance](const RectF& box, COLORREF color, float diameter) {
            fill_rounded(graphics, box, distance(diameter), paint_color(color));
        };
        const auto draw_rail = [&](RECT track, double fraction, COLORREF color, Part part) {
            const auto bounds = float_rect(track);
            const auto thumb = static_cast<float>(position_on(track, fraction));
            const auto y = bounds.Y + bounds.Height / 2;
            rounded(bounds, RGB(236, 239, 248), 4);
            rounded({bounds.X, bounds.Y, thumb - bounds.X, bounds.Height}, color, 4);
            if (part == Part::timeline) {
                const bool held = panel.hover == part || panel.pressed == part;
                if (held) dot(thumb, y, distance(9), paint_color(RGB(226, 222, 245), 150));
                dot(thumb, y, distance(7), paint_color(RGB(199, 193, 235)));
                dot(thumb, y, distance(6), Color(255, 255, 255));
                dot(thumb, y, distance(4.4f), paint_color(color));
            } else {
                dot(thumb, y, distance(3.4f), paint_color(color));
            }
        };

        SolidBrush background(paint_color(RGB(245, 246, 252)));
        graphics.FillRectangle(&background, float_rect(r));
        const auto tray = float_rect(l.tray);
        fill_rounded(graphics, {tray.X, tray.Y + distance(2), tray.Width, tray.Height}, distance(30),
                     paint_color(RGB(43, 47, 90), 10));
        rounded(tray, RGB(255, 255, 255), 28);

        if (has_dirty(l.source)) {
            rounded(source_box, panel.hover == Part::source ? RGB(234, 239, 253) : RGB(245, 246, 252), 16);
            const float icon_x = source_box.X + distance(18);
            const std::array<float, 5> heights{4, 8, 13, 9, 5};
            for (size_t i = 0; i < heights.size(); ++i) {
                const auto x = icon_x + distance((static_cast<float>(i) - 2) * 3);
                const auto half = distance(heights[i]) / 2;
                const PointF beam[]{{x, glyph_y - half}, {x, glyph_y + half}};
                lines(beam, 2, accent, 1.4f);
            }
            if (panel.source_count > 1) {
                const auto x = source_box.GetRight() - distance(14);
                const PointF chevron[]{{x - distance(4), glyph_y - distance(2)},
                                       {x, glyph_y + distance(2)},
                                       {x + distance(4), glyph_y - distance(2)}};
                lines(chevron, 3, text, 1.4f);
            }
        }

        auto play_dirty = l.play;
        InflateRect(&play_dirty, scaled(7, l.dpi), scaled(7, l.dpi));
        if (has_dirty(play_dirty)) {
            const auto strength = 1 - .04 * press;
            auto color = [strength](COLORREF value) {
                return Color(255, static_cast<BYTE>(std::lround(GetRValue(value) * strength)),
                             static_cast<BYTE>(std::lround(GetGValue(value) * strength)),
                             static_cast<BYTE>(std::lround(GetBValue(value) * strength)));
            };
            const auto scale = static_cast<float>(1 - .05 * press);
            const auto cy = glyph_y + distance(static_cast<float>(press));
            const float radius = distance(19) * scale;
            if (can_play) {
                // A small layered software shadow stays local to the button's dirty region.
                for (int layer = 5; layer > 0; --layer) {
                    dot(glyph_x, cy + distance(2), radius + distance(static_cast<float>(layer)),
                        paint_color(accent, static_cast<BYTE>(3 + (5 - layer) * 3)));
                }
            }
            const RectF circle{glyph_x - radius, cy - radius, radius * 2, radius * 2};
            if (can_play && panel.hover != Part::play) {
                const Color gradient[] {color(RGB(97, 136, 218)), color(accent), color(RGB(90, 95, 200))};
                const REAL stops[] {0, .55f, 1};
                LinearGradientBrush fill(circle, gradient[0], gradient[2], LinearGradientModeForwardDiagonal);
                fill.SetInterpolationColors(gradient, stops, 3);
                graphics.FillEllipse(&fill, circle);
            } else {
                SolidBrush fill(color(can_play ? RGB(52, 85, 160) : RGB(212, 221, 239)));
                graphics.FillEllipse(&fill, circle);
            }
            const auto glyph_color = color(can_play ? RGB(255, 255, 255) : RGB(121, 134, 162));
            const auto unit = [&distance, scale](float value) { return distance(value) * scale; };
            if (panel.playing) {
                for (const float offset : {-4.4f, 1.4f}) {
                    fill_rounded(graphics, {glyph_x + unit(offset), cy - unit(5.9f), unit(3), unit(12)},
                                 unit(1.4f), glyph_color);
                }
            } else {
                const PointF triangle[]{{glyph_x - unit(3), cy - unit(5.2f)},
                                        {glyph_x - unit(3), cy + unit(5.2f)},
                                        {glyph_x + unit(5.4f), cy}};
                SolidBrush glyph(glyph_color);
                graphics.FillPolygon(&glyph, triangle, 3);
            }
        }

        if (has_dirty(l.stop)) {
            if (panel.hover == Part::stop) rounded(float_rect(l.stop), RGB(238, 241, 250), 12);
            const float x = static_cast<float>(l.stop.left + l.stop.right) / 2;
            rounded({x - distance(3.5f), glyph_y - distance(3.5f), distance(7), distance(7)},
                    RGB(109, 112, 123), 1.6f);
        }
        if (has_dirty(l.speaker)) {
            const float x = static_cast<float>(l.speaker.left + l.speaker.right) / 2;
            const PointF cone[]{{x - distance(5), glyph_y - distance(2)},
                                {x - distance(2), glyph_y - distance(2)},
                                {x + distance(1), glyph_y - distance(5)},
                                {x + distance(1), glyph_y + distance(5)},
                                {x - distance(2), glyph_y + distance(2)},
                                {x - distance(5), glyph_y + distance(2)},
                                {x - distance(5), glyph_y - distance(2)}};
            const auto gray = RGB(109, 112, 123);
            lines(cone, 7, gray, 1.35f);
            Pen wave(paint_color(gray), distance(1.25f));
            wave.SetStartCap(LineCapRound);
            wave.SetEndCap(LineCapRound);
            if (volume_value > 0) {
                for (const float arc : {3.5f, 6.f}) {
                    graphics.DrawArc(&wave, x + distance(1 - arc), glyph_y - distance(arc),
                                     distance(2 * arc), distance(2 * arc), -42.f, 84.f);
                }
            }
        }
        if (has_dirty(l.timeline)) {
            draw_rail(rail_inside(l.timeline, l.dpi), playback_fraction(shown, panel.duration), timeline,
                      Part::timeline);
        }
        if (has_dirty(l.volume)) {
            draw_rail(volume_rail(l), volume_value / 100.0, accent, Part::volume);
        }
    }

    const int saved_dc = SaveDC(dc);
    if (saved_dc) IntersectClipRect(dc, dirty.left, dirty.top, dirty.right, dirty.bottom);
    const HGDIOBJ old_font = SelectObject(dc, panel.buffer.text_font(l.dpi));
    SetBkMode(dc, TRANSPARENT);
    const auto label = [dc](RECT rect, const wchar_t* value, COLORREF color, UINT align = DT_CENTER) {
        SetTextColor(dc, color);
        DrawTextW(dc, value, -1, &rect, align | DT_VCENTER | DT_SINGLELINE | DT_NOPREFIX | DT_END_ELLIPSIS);
    };
    auto source_text = l.source;
    source_text.left += scaled(35, l.dpi);
    source_text.right -= scaled(21, l.dpi);
    if (has_dirty(l.source)) label(source_text, source_label(panel, panel.source), text, DT_LEFT);
    wchar_t current[24]{}, duration[24]{}, volume[16]{};
    clock_text(shown, current);
    clock_text(panel.duration, duration);
    std::swprintf(volume, 16, L"%d%%", volume_value);
    if (has_dirty(l.duration)) label(l.duration, duration, RGB(105, 114, 137), DT_LEFT);
    if (has_dirty(l.slash)) label(l.slash, L"/", RGB(136, 146, 173));
    if (has_dirty(l.volume_value)) label(l.volume_value, volume, RGB(105, 114, 137), DT_RIGHT);
    SelectObject(dc, panel.buffer.text_font(l.dpi, true));
    if (has_dirty(l.current)) label(l.current, current, text, DT_RIGHT);
    SelectObject(dc, old_font);
    if (saved_dc) RestoreDC(dc, saved_dc);
    if (dc != output) {
        BitBlt(output, dirty.left, dirty.top, dirty.right - dirty.left, dirty.bottom - dirty.top,
               dc, dirty.left, dirty.top, SRCCOPY);
    }
    const auto painted = counter();
    ++panel.motion.paint_count;
    RECT overlap{};
    if (IntersectRect(&overlap, &l.play, &dirty)) {
        panel.motion.press = press;
        if (panel.motion.input_qpc && !panel.motion.first_input_paint_qpc) {
            panel.motion.first_input_paint_qpc = painted;
        }
        if (!panel.glyph_painted || panel.motion.playing != static_cast<int32_t>(panel.playing)) {
            if (panel.glyph_painted) ++panel.motion.glyph_changes;
            panel.glyph_painted = true;
            panel.motion.playing = panel.playing;
            panel.motion.glyph_paint_qpc = painted;
        }
    }
    if (IntersectRect(&overlap, &l.timeline, &dirty)) {
        panel.motion.painted = shown;
        panel.motion.rail_paint_qpc = painted;
    }
}

void invalidate_play(HWND hwnd) noexcept {
    const auto l = layout(hwnd);
    auto region = l.play;
    InflateRect(&region, scaled(7, l.dpi), scaled(7, l.dpi));
    InvalidateRect(hwnd, &region, FALSE);
}

void invalidate_timeline(HWND hwnd, const Panel& panel, int64_t now) noexcept {
    const auto l = layout(hwnd);
    const auto rail = rail_inside(l.timeline, l.dpi);
    const auto displayed = display_position(panel, now);
    const auto previous = panel.motion.painted;
    const bool new_second = clock_second(displayed) != clock_second(previous);
    const auto delta = std::abs(position_on(rail, playback_fraction(previous, panel.duration))
        - position_on(rail, playback_fraction(displayed, panel.duration)));
    if (!new_second && delta < .25) return;
    auto region = l.timeline;
    if (new_second) UnionRect(&region, &l.current, &l.timeline);
    InvalidateRect(hwnd, &region, FALSE);
}

void update_frame_timer(HWND hwnd, Panel& panel, int64_t now) noexcept {
    const bool running = panel.moving && clock_position(panel, now) < panel.heard;
    const bool animate = panel.play_released &&
        now - panel.play_released < panel.qpc_frequency * play_release_ms / 1000;
    const bool needed = IsWindowVisible(hwnd) && (running || animate);
    if (needed == panel.frame_timer) return;
    if (needed) {
        panel.frame_timer = SetTimer(hwnd, visual_frame_timer, frame_interval_ms, nullptr) != 0;
    } else {
        KillTimer(hwnd, visual_frame_timer);
        panel.frame_timer = false;
    }
}

void start_play_feedback(Panel& panel, int64_t input) noexcept {
    panel.motion.input_qpc = input;
    panel.motion.first_input_paint_qpc = 0;
    panel.play_released = 0;
}

void release_play_feedback(HWND hwnd, Panel& panel, int64_t now) noexcept {
    panel.play_released = now;
    invalidate_play(hwnd);
    update_frame_timer(hwnd, panel, now);
}

void update_seek_timer(HWND hwnd, const Panel& panel) noexcept {
    if (panel.local_seek.phase == SeekPhase::pending || panel.remote_seek.phase == SeekPhase::pending) {
        SetTimer(hwnd, seek_confirmation_timer, seek_timer_interval_ms, nullptr);
    } else {
        KillTimer(hwnd, seek_confirmation_timer);
    }
}

void next_gesture(HWND hwnd, Panel& panel) noexcept {
    panel.gesture = panel.gesture == std::numeric_limits<int>::max() ? 1 : panel.gesture + 1;
    panel.remote_revision = 0;
    panel.remote_seek = {};
    panel.local_seek = {};
    update_seek_timer(hwnd, panel);
}

int issue(HWND hwnd, Panel& panel, Part part, int source, double value, int64_t input) noexcept {
    if (!available(panel, part)) return 0;
    const int kind = static_cast<int>(part);
    const int route = panel.route;
    const bool direct = route > 0 && kind <= 3;
    const auto serial = ++panel.last.serial;
    auto& last = panel.last;
    last.route = route;
    last.kind = kind;
    last.source = source;
    last.seconds = value;
    last.input_qpc = input;
    last.submit_qpc = counter();
    last.result = 0;
    const int result = direct ? panel.submit(route, kind, source, value)
                              : panel.auxiliary(kind, source, value);
    auto* current = local_panel(hwnd);
    if (current != &panel || current->last.serial != serial) return result;
    last.result = result;
    last.return_qpc = counter();
    if (part == Part::timeline && current->route == route) {
        if (result > 0) {
            // A cold lifecycle request has no C++ receipt until a ready sample.
            panel.local_seek = {SeekPhase::pending, value, direct ? result : 0, route, last.submit_qpc};
        } else {
            panel.local_seek = {};
        }
        update_seek_timer(hwnd, panel);
    }
    return result;
}

void cancel(HWND hwnd, Panel& panel) noexcept {
    const bool play_pressed = panel.pressed == Part::play;
    panel.pressed = Part::none;
    if (play_pressed) release_play_feedback(hwnd, panel, counter());
    if (GetCapture() == hwnd) ReleaseCapture();
}

void set_hover(HWND hwnd, Panel& panel, Part part) noexcept {
    if (part == panel.hover) return;
    panel.hover = part;
    InvalidateRect(hwnd, nullptr, FALSE);
}

int volume_at(const Layout& l, LONG x) noexcept {
    return static_cast<int>(std::lround(fraction_at(volume_rail(l), x) * 100));
}

double seconds_at(const Layout& l, const Panel& panel, LONG x) noexcept {
    return fraction_at(rail_inside(l.timeline, l.dpi), x) * panel.duration;
}

void preview_seek(HWND hwnd, Panel& panel, const Layout& l, LONG x) noexcept {
    panel.drag_position = seconds_at(l, panel, x);
    if (std::abs(x - panel.drag_start) >= scaled(3, l.dpi)) panel.drag_moved = true;
    const auto now = counter();
    const auto interval = panel.qpc_frequency * seek_interval_ms / 1000;
    if (panel.drag_moved && panel.playing && panel.route && panel.last_drag_position != panel.drag_position
        && (!panel.last_drag_submit || now - panel.last_drag_submit >= interval)) {
        if (issue(hwnd, panel, Part::timeline, -1, panel.drag_position, now) > 0) {
            panel.last_drag_submit = now;
            panel.last_drag_position = panel.drag_position;
        }
    }
}

void choose_source(HWND hwnd, Panel& panel, int64_t input) noexcept {
    if (!available(panel, Part::source)) return;
    const HMENU menu = CreatePopupMenu();
    if (!menu) return;
    const uint64_t generation = panel.view_generation;
    for (int i = 0; i < panel.source_count; ++i) {
        wchar_t escaped[label_capacity * 2]{};
        const wchar_t* label = source_label(panel, i);
        size_t at = 0;
        for (; *label && at + 2 < std::size(escaped); ++label) {
            if (*label == L'&') escaped[at++] = L'&';
            escaped[at++] = *label;
        }
        AppendMenuW(menu, MF_STRING | (i == panel.source ? MF_CHECKED : MF_UNCHECKED),
                    static_cast<UINT_PTR>(i + 1), escaped);
    }
    const auto rect = layout(hwnd).source;
    POINT point{rect.left, rect.top};
    ClientToScreen(hwnd, &point);
    const auto selected = static_cast<int>(TrackPopupMenuEx(menu, TPM_RETURNCMD | TPM_NONOTIFY | TPM_BOTTOMALIGN,
                                                              point.x, point.y, hwnd, nullptr));
    DestroyMenu(menu);
    auto* current = local_panel(hwnd);
    if (current != &panel || current->view_generation != generation) return;
    if (selected > 0 && selected <= current->source_count) {
        // Always submit an explicit menu selection; the last painted source
        // can be older than a command already in flight.
        issue(hwnd, *current, Part::source, selected - 1, 0, input);
    }
}

LRESULT CALLBACK panel_proc(HWND hwnd, UINT msg, WPARAM wp, LPARAM lp) {
    if (msg == WM_NCCREATE) {
        const auto* parameters = reinterpret_cast<const CREATESTRUCTW*>(lp);
        auto* panel = new (std::nothrow) Panel;
        if (!panel) return FALSE;
        Gdiplus::GdiplusStartupInput options;
        options.SuppressExternalCodecs = TRUE;
        if (Gdiplus::GdiplusStartup(&panel->graphics_token, &options, nullptr) != Gdiplus::Ok) {
            delete panel;
            return FALSE;
        }
        panel->submit = *static_cast<Submit*>(parameters->lpCreateParams);
        SetWindowLongPtrW(hwnd, GWLP_USERDATA, reinterpret_cast<LONG_PTR>(panel));
        return TRUE;
    }
    auto* panel = reinterpret_cast<Panel*>(GetWindowLongPtrW(hwnd, GWLP_USERDATA));
    if (!panel) return DefWindowProcW(hwnd, msg, wp, lp);
    switch (msg) {
    case WM_NCDESTROY: {
        SetWindowLongPtrW(hwnd, GWLP_USERDATA, 0);
        const auto graphics_token = panel->graphics_token;
        delete panel;
        Gdiplus::GdiplusShutdown(graphics_token);
        return DefWindowProcW(hwnd, msg, wp, lp);
    }
    case WM_ERASEBKGND:
        return 1;
    case WM_PAINT: {
        PAINTSTRUCT ps{};
        const HDC dc = BeginPaint(hwnd, &ps);
        if (dc) paint(hwnd, dc, *panel, ps.rcPaint);
        EndPaint(hwnd, &ps);
        return 0;
    }
    case WM_LBUTTONDOWN: {
        const auto l = layout(hwnd);
        const POINT point{GET_X_LPARAM(lp), GET_Y_LPARAM(lp)};
        const Part part = hit(l, *panel, point);
        if (part == Part::none) return 0;
        // Take key input after a pointer gesture without entering Tab navigation.
        panel->keyboard_focus = false;
        SetFocus(hwnd);
        SetCapture(hwnd);
        panel->pressed = panel->focus = part;
        if (part == Part::timeline) {
            next_gesture(hwnd, *panel);
            panel->drag_start = point.x;
            panel->drag_position = seconds_at(l, *panel, point.x);
            panel->drag_moved = false;
            panel->last_drag_submit = 0;
            panel->last_drag_position = -1;
        } else if (part == Part::volume) {
            panel->drag_volume = volume_at(l, point.x);
        } else if (part == Part::play) {
            start_play_feedback(*panel, counter());
            invalidate_play(hwnd);
            return 0;
        }
        InvalidateRect(hwnd, nullptr, FALSE);
        return 0;
    }
    case WM_MOUSEMOVE: {
        const auto l = layout(hwnd);
        const POINT point{GET_X_LPARAM(lp), GET_Y_LPARAM(lp)};
        const double old_position = panel->drag_position;
        const auto old_volume = panel->drag_volume;
        if (panel->pressed == Part::timeline) preview_seek(hwnd, *panel, l, point.x);
        if (panel->pressed == Part::volume) panel->drag_volume = volume_at(l, point.x);
        if (panel->drag_position != old_position || panel->drag_volume != old_volume) {
            InvalidateRect(hwnd, nullptr, FALSE);
        }
        if (panel->hover == Part::none) {
            TRACKMOUSEEVENT track{sizeof(track), TME_LEAVE, hwnd, 0};
            TrackMouseEvent(&track);
        }
        set_hover(hwnd, *panel, hit(l, *panel, point));
        return 0;
    }
    case WM_MOUSELEAVE:
        set_hover(hwnd, *panel, Part::none);
        return 0;
    case WM_LBUTTONUP: {
        const Part pressed = panel->pressed;
        if (pressed == Part::none) return 0;
        const auto input = counter();
        const auto l = layout(hwnd);
        const POINT point{GET_X_LPARAM(lp), GET_Y_LPARAM(lp)};
        const double destination = pressed == Part::timeline ? seconds_at(l, *panel, point.x) : 0.0;
        // A live-preview receipt can arrive while the pointer is held still.
        // If playback has advanced since that receipt, release still locates
        // the exact pointer target. An in-flight identical preview is reused.
        const bool preview_advanced = panel->playing && panel->last_drag_submit &&
            panel->local_seek.phase == SeekPhase::confirmed &&
            std::abs(panel->heard - destination) > .06 &&
            input - panel->last_drag_submit >= panel->qpc_frequency * seek_interval_ms / 1000;
        const bool seek_remaining = destination != panel->last_drag_position || preview_advanced;
        cancel(hwnd, *panel);
        if (pressed == Part::timeline && seek_remaining) {
            issue(hwnd, *panel, pressed, -1, destination, input);
        } else if (pressed == Part::volume) {
            const int value = volume_at(l, point.x);
            if (value != panel->volume) issue(hwnd, *panel, pressed, -1, value, input);
        } else if (pressed == hit(l, *panel, point)) {
            if (pressed == Part::source) choose_source(hwnd, *panel, input);
            else if (pressed == Part::play || pressed == Part::stop) issue(hwnd, *panel, pressed, -1, 0, input);
        }
        InvalidateRect(hwnd, nullptr, FALSE);
        return 0;
    }
    case WM_TIMER:
        if (wp == visual_frame_timer) {
            const auto now = counter();
            if (panel->play_released) {
                invalidate_play(hwnd);
                if (play_press(*panel, now) == 0) panel->play_released = 0;
            }
            if (panel->moving) invalidate_timeline(hwnd, *panel, now);
            update_frame_timer(hwnd, *panel, now);
            return 0;
        }
        if (wp == seek_confirmation_timer) {
            const auto now = counter();
            bool expired = false;
            auto expire = [&](SeekVisual& seek, UINT milliseconds) {
                if (seek.phase == SeekPhase::pending &&
                    now - seek.issued >= panel->qpc_frequency * milliseconds / 1000) {
                    seek = {};
                    expired = true;
                }
            };
            expire(panel->local_seek, seek_confirmation_ms);
            expire(panel->remote_seek, web_confirmation_ms);
            if (expired) InvalidateRect(hwnd, nullptr, FALSE);
            if (panel->local_seek.phase != SeekPhase::pending && panel->remote_seek.phase != SeekPhase::pending) {
                KillTimer(hwnd, seek_confirmation_timer);
            }
            return 0;
        }
        break;
    case WM_CANCELMODE:
        cancel(hwnd, *panel);
        InvalidateRect(hwnd, nullptr, FALSE);
        return 0;
    case WM_CAPTURECHANGED:
        if (panel->pressed == Part::play) release_play_feedback(hwnd, *panel, counter());
        panel->pressed = Part::none;
        InvalidateRect(hwnd, nullptr, FALSE);
        return 0;
    case WM_SHOWWINDOW:
        if (!wp) {
            ++panel->view_generation;
            cancel(hwnd, *panel);
            next_gesture(hwnd, *panel);
            panel->hover = Part::none;
            panel->play_released = 0;
        }
        update_frame_timer(hwnd, *panel, counter());
        break;
    case WM_ENTERMENULOOP:
        panel->source_menu_open = true;
        break;
    case WM_EXITMENULOOP:
        panel->source_menu_open = false;
        break;
    case WM_GETDLGCODE:
        return DLGC_WANTALLKEYS | DLGC_WANTTAB;
    case WM_KEYUP:
        if (wp == VK_SPACE && !panel->source_menu_open) return 0;
        break;
    case WM_CHAR:
        if (wp == L' ' && !panel->source_menu_open) return 0;
        break;
    case WM_KEYDOWN: {
        if (!panel->ready || panel->source_menu_open) break;
        if (wp == VK_ESCAPE) {
            cancel(hwnd, *panel);
            InvalidateRect(hwnd, nullptr, FALSE);
            return 0;
        }
        if (wp == VK_TAB) {
            const bool back = keyboard_modifier(VK_SHIFT);
            auto it = std::find(tab_order.begin(), tab_order.end(), panel->focus);
            int index = static_cast<int>(it - tab_order.begin());
            const int direction = back ? -1 : 1;
            const int count = static_cast<int>(tab_order.size());
            for (index += direction; index >= 0 && index < count; index += direction) {
                if (available(*panel, tab_order[index])) {
                    panel->focus = tab_order[index];
                    panel->keyboard_focus = true;
                    return 0;
                }
            }
            if (const HWND parent = GetParent(hwnd)) {
                SendMessageW(parent, WM_KEYDOWN, wp, lp);
            }
            return 0;
        }
        if (wp == VK_SPACE || wp == VK_RETURN) {
            if (wp == VK_SPACE && transport_space_modified()) break;
            // The Win32 previous-key-state bit covers repeats even when a
            // popup receives the key release. Space never changes focus modality.
            if (lp & (1LL << 30)) return 0;
            const bool focused_action = wp == VK_RETURN || panel->keyboard_focus;
            if (focused_action && panel->focus == Part::stop) issue(hwnd, *panel, Part::stop, -1, 0, counter());
            else if (focused_action && panel->focus == Part::source) choose_source(hwnd, *panel, counter());
            else {
                const auto input = counter();
                start_play_feedback(*panel, input);
                release_play_feedback(hwnd, *panel, input);
                issue(hwnd, *panel, Part::play, -1, 0, input);
            }
            return 0;
        }
        if (wp == VK_LEFT || wp == VK_RIGHT || wp == VK_HOME || wp == VK_END) {
            panel->keyboard_focus = true;
            if (panel->focus == Part::source && available(*panel, Part::source)) {
                const int source = wp == VK_HOME ? 0 : wp == VK_END ? panel->source_count - 1
                    : std::clamp(panel->source + (wp == VK_LEFT ? -1 : 1), 0, panel->source_count - 1);
                if (source != panel->source) issue(hwnd, *panel, Part::source, source, 0, counter());
            } else if (panel->focus == Part::volume) {
                const int value = wp == VK_HOME ? 0 : wp == VK_END ? 100
                    : std::clamp(panel->volume + (wp == VK_LEFT ? -5 : 5), 0, 100);
                if (value != panel->volume) issue(hwnd, *panel, Part::volume, -1, value, counter());
            } else {
                const double current = display_position(*panel);
                const double destination = wp == VK_HOME ? 0 : wp == VK_END ? panel->duration
                    : std::clamp(current + (wp == VK_LEFT ? -1.0 : 1.0), 0.0, panel->duration);
                next_gesture(hwnd, *panel);
                issue(hwnd, *panel, Part::timeline, -1, destination, counter());
            }
            InvalidateRect(hwnd, nullptr, FALSE);
            return 0;
        }
        break;
    }
    }
    return DefWindowProcW(hwnd, msg, wp, lp);
}
} // namespace

API void* astra_transport_panel_create(void* parent_hwnd, Submit submit) {
    const HWND parent = static_cast<HWND>(parent_hwnd);
    if (!parent || !submit || GetWindowThreadProcessId(parent, nullptr) != GetCurrentThreadId()) return nullptr;
    HMODULE module = nullptr;
    if (!GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS | GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,
                            reinterpret_cast<LPCWSTR>(&panel_proc), &module)) return nullptr;
    WNDCLASSW wc{};
    wc.hInstance = module;
    wc.lpfnWndProc = panel_proc;
    wc.lpszClassName = panel_class;
    wc.hCursor = LoadCursorW(nullptr, MAKEINTRESOURCEW(32512));
    if (!RegisterClassW(&wc) && GetLastError() != ERROR_CLASS_ALREADY_EXISTS) return nullptr;
    RECT r{};
    GetClientRect(parent, &r);
    return CreateWindowExW(0, panel_class, L"Astra Studio 分轨走带", WS_CHILD | WS_VISIBLE | WS_TABSTOP,
                           0, 0, r.right, r.bottom, parent, nullptr, module, &submit);
}

API int astra_transport_panel_destroy(void* child) {
    const auto hwnd = static_cast<HWND>(child);
    return local_panel(hwnd) && DestroyWindow(hwnd);
}

API int astra_transport_panel_fit(void* child) {
    const auto hwnd = static_cast<HWND>(child);
    if (!local_panel(hwnd)) return 0;
    RECT r{};
    if (!GetClientRect(GetParent(hwnd), &r)) return 0;
    return SetWindowPos(hwnd, nullptr, 0, 0, r.right, r.bottom, SWP_NOZORDER | SWP_NOACTIVATE);
}

API int astra_transport_panel_focus_keyboard(void* child, int backwards) {
    const auto hwnd = static_cast<HWND>(child);
    auto* panel = local_panel(hwnd);
    if (!panel) return 0;
    const auto enter = [&](Part part) {
        if (!available(*panel, part)) return false;
        panel->focus = part;
        panel->keyboard_focus = true;
        SetFocus(hwnd);
        return GetFocus() == hwnd;
    };
    if (backwards) {
        return std::any_of(tab_order.rbegin(), tab_order.rend(), enter);
    }
    return std::any_of(tab_order.begin(), tab_order.end(), enter);
}

API int astra_transport_panel_focus_part(void* child) {
    const auto hwnd = static_cast<HWND>(child);
    const auto* panel = local_panel(hwnd);
    return panel && GetFocus() == hwnd ? static_cast<int>(panel->focus) : 0;
}

API int astra_transport_panel_set_aux(void* child, Auxiliary auxiliary) {
    auto* panel = local_panel(static_cast<HWND>(child));
    if (!panel) return 0;
    panel->auxiliary = auxiliary;
    return 1;
}

API int astra_transport_panel_set_sources(void* child, const wchar_t* const* labels, int count) {
    const auto hwnd = static_cast<HWND>(child);
    auto* panel = local_panel(hwnd);
    if (!panel || count < 0 || count > max_sources || (count && !labels)) return 0;
    std::array<SourceLabel, max_sources> next{};
    for (int i = 0; i < count; ++i) {
        if (!labels[i]) continue;
        for (size_t j = 0; labels[i][j] && j < label_capacity - 1; ++j) next[i][j] = labels[i][j];
    }
    if (next != panel->labels) {
        panel->labels = next;
        ++panel->view_generation;
        InvalidateRect(hwnd, nullptr, FALSE);
    }
    return 1;
}

API int astra_transport_panel_set_volume(void* child, int percent) {
    const auto hwnd = static_cast<HWND>(child);
    auto* panel = local_panel(hwnd);
    if (!panel || percent < 0 || percent > 100) return 0;
    if (panel->volume != percent) {
        panel->volume = percent;
        InvalidateRect(hwnd, nullptr, FALSE);
    }
    return 1;
}

API int astra_transport_panel_sync_clock(void* child, int route, double duration, double heard,
                                        int playing, int source, int source_count, int ready,
                                        double sampled_at, double rate, int moving) {
    const auto hwnd = static_cast<HWND>(child);
    auto* panel = local_panel(hwnd);
    if (!panel || route < 0 || !std::isfinite(duration) || !std::isfinite(heard)
        || !std::isfinite(sampled_at) || !std::isfinite(rate) || rate <= 0
        || duration < 0 || source_count < 0 || source_count > max_sources) return 0;
    const bool enabled = ready && source_count > 0;
    const auto now = counter();
    const bool route_changed = panel->route != route;
    const bool catalog_changed = panel->source_count != source_count;
    const bool opened = route_changed && !panel->route && route && !catalog_changed && enabled && panel->ready;
    if (route_changed) panel->heard_sequence = 0;
    if (opened) {
        // Cold decoder readiness does not supersede a held locator.
        panel->local_seek.route = panel->remote_seek.route = route;
    } else if (route_changed || catalog_changed || (panel->ready && !enabled)) {
        ++panel->view_generation;
        cancel(hwnd, *panel);
        next_gesture(hwnd, *panel);
        panel->hover = Part::none;
    }
    const double bounded_heard = std::clamp(heard, 0.0, duration);
    const bool playing_now = playing != 0;
    const bool moving_now = playing_now && moving && sampled_at > 0 && enabled;
    const bool same_clock = !route_changed && !catalog_changed && enabled && panel->ready
        && panel->duration == duration && panel->source == source && panel->rate == rate
        && sampled_at > 0 && panel->sampled_at > 0;
    if (same_clock && sampled_at < panel->sampled_at) return 1;
    const bool locating = panel->pressed == Part::timeline || panel->local_seek.phase == SeekPhase::pending
        || panel->remote_seek.phase != SeekPhase::idle;
    const double advance = (sampled_at - panel->sampled_at) * rate;
    const bool continuous = same_clock && bounded_heard >= panel->heard
        && bounded_heard - panel->heard <= advance + clock_step_slack * rate;
    // Catch up only over confirmed audible samples. A pause at the real DAC
    // boundary can no longer land behind a position the rail predicted.
    panel->visual_anchor = (moving_now && continuous && !locating)
        ? std::clamp(clock_position(*panel, now), bounded_heard - clock_horizon * rate, bounded_heard)
        : bounded_heard;
    panel->visual_started = now;
    const bool changed = route_changed || catalog_changed || panel->ready != enabled || panel->duration != duration
        || panel->playing != playing_now || panel->source != source;
    panel->ready = enabled;
    panel->route = route;
    panel->duration = duration;
    panel->heard = bounded_heard;
    panel->sampled_at = sampled_at;
    panel->rate = rate;
    panel->playing = playing_now;
    panel->moving = moving_now;
    panel->source = source;
    panel->source_count = source_count;
    if (changed) InvalidateRect(hwnd, nullptr, FALSE);
    else invalidate_timeline(hwnd, *panel, now);
    update_frame_timer(hwnd, *panel, now);
    return 1;
}

API int astra_transport_panel_sync(void* child, int route, double duration, double heard,
                                  int playing, int source, int source_count, int ready) {
    return astra_transport_panel_sync_clock(child, route, duration, heard, playing, source, source_count, ready,
                                            0, 1, 0);
}

API int astra_transport_panel_motion(void* child, void* out, uint32_t size) {
    const auto* panel = local_panel(static_cast<HWND>(child));
    if (!panel || !out || size != sizeof(MotionMetrics)) return 0;
    auto metrics = panel->motion;
    metrics.displayed = display_position(*panel);
    metrics.sampled_at = panel->sampled_at;
    metrics.moving = panel->moving;
    *static_cast<MotionMetrics*>(out) = metrics;
    return 1;
}

API int astra_transport_panel_ack(void* child, int heard_sequence) {
    const auto hwnd = static_cast<HWND>(child);
    auto* panel = local_panel(hwnd);
    if (!panel || !panel->ready || heard_sequence < 0) return 0;
    panel->heard_sequence = std::max(panel->heard_sequence, heard_sequence);
    auto heard = [&](const SeekVisual& seek, bool allow_unsequenced) {
        return seek.phase == SeekPhase::pending && seek.route == panel->route &&
            (seek.sequence > 0 ? panel->heard_sequence >= seek.sequence
                               : allow_unsequenced && std::abs(panel->heard - seek.seconds) <= .035);
    };
    const bool heard_local = heard(panel->local_seek, true);
    const bool heard_remote = heard(panel->remote_seek, panel->route == 0);
    if (heard_local) {
        panel->local_seek.phase = SeekPhase::confirmed;
        panel->local_seek.seconds = panel->heard;
    }
    if (heard_remote) panel->remote_seek = {};
    if (heard_local || heard_remote) {
        update_seek_timer(hwnd, *panel);
        invalidate_timeline(hwnd, *panel, counter());
    }
    return 1;
}

API int astra_transport_panel_preview_seek(void* child, int expected_gesture, int revision,
                                          int phase, int route, double seconds, int sequence) {
    const auto hwnd = static_cast<HWND>(child);
    auto* panel = local_panel(hwnd);
    if (!panel || !panel->ready || panel->pressed == Part::timeline ||
        expected_gesture != panel->gesture || revision <= panel->remote_revision ||
        phase < 0 || phase > 2 || (phase && route != panel->route) ||
        !std::isfinite(seconds) || sequence < 0) return 0;
    panel->remote_revision = revision;
    if (phase == 0) {
        panel->remote_seek = {};
    } else {
        panel->local_seek = {};
        const auto target = std::clamp(seconds, 0.0, panel->duration);
        // The receipt may reach the visual path after the playback packet.
        if (phase == 2 && sequence > 0 && panel->heard_sequence >= sequence) {
            panel->remote_seek = {};
        } else {
            panel->remote_seek = {static_cast<SeekPhase>(phase), target, sequence, route, counter()};
        }
    }
    update_seek_timer(hwnd, *panel);
    invalidate_timeline(hwnd, *panel, counter());
    return 1;
}

API int astra_transport_panel_preview_state(void* child, void* out, uint32_t size) {
    const auto* panel = local_panel(static_cast<HWND>(child));
    if (!panel || !out || size != sizeof(PreviewState)) return 0;
    const auto seek = panel->pressed == Part::timeline
        ? SeekVisual{SeekPhase::held, panel->drag_position, 0, panel->route, 0}
        : panel->local_seek;
    PreviewState state;
    state.gesture = panel->gesture;
    state.phase = static_cast<int32_t>(seek.phase);
    state.route = panel->route;
    state.sequence = seek.sequence;
    state.seconds = seek.phase == SeekPhase::idle ? panel->heard : seek.seconds;
    state.displayed = display_position(*panel);
    state.remote_revision = panel->remote_revision;
    state.remote_phase = static_cast<int32_t>(panel->remote_seek.phase);
    *static_cast<PreviewState*>(out) = state;
    return 1;
}

API int astra_transport_panel_last(void* child, void* out, uint32_t size) {
    const auto* panel = local_panel(static_cast<HWND>(child));
    if (!panel || !out || size != sizeof(PanelMetrics)) return 0;
    *static_cast<PanelMetrics*>(out) = panel->last;
    return 1;
}

API int astra_transport_panel_rect(void* child, int part, int32_t* edges) {
    const auto hwnd = static_cast<HWND>(child);
    if (!local_panel(hwnd) || !edges) return 0;
    const auto rect = part_rect(layout(hwnd), static_cast<Part>(part));
    if (rect.left >= rect.right || rect.top >= rect.bottom) return 0;
    edges[0] = rect.left;
    edges[1] = rect.top;
    edges[2] = rect.right;
    edges[3] = rect.bottom;
    return 1;
}

