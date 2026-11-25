#include "kernel.hpp"

#include <algorithm>
#include <array>
#include <atomic>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <initializer_list>
#include <limits>
#include <memory>
#include <new>
#include <stdexcept>
#include <thread>
#include <vector>

namespace {
[[noreturn]] void fail(const char* what, int line) noexcept {
    std::fprintf(stderr, "kernel_test:%d: %s\n", line, what);
    std::abort();
}
#define CHECK(expr) do { if (!(expr)) fail(#expr, __LINE__); } while (false)

thread_local bool in_render = false;
void memory_check() noexcept { CHECK(!in_render); }
void* allocate(std::size_t n) {
    memory_check();
    if (auto* p = std::malloc(n ? n : 1)) return p;
    throw std::bad_alloc();
}
void deallocate(void* p) noexcept { memory_check(); std::free(p); }
void* allocate_aligned(std::size_t n, std::align_val_t alignment) {
    memory_check();
    const auto a = static_cast<std::size_t>(alignment);
    const auto max = (std::numeric_limits<std::size_t>::max)();
    n = n ? n : 1;
    if (!a || a - 1 > max - sizeof(void*) || n > max - sizeof(void*) - (a - 1))
        throw std::bad_alloc();
    auto space = n + a - 1;
    void* raw = std::malloc(space + sizeof(void*));
    if (!raw) throw std::bad_alloc();
    void* p = static_cast<unsigned char*>(raw) + sizeof(void*);
    if (!std::align(a, n, p, space)) {
        std::free(raw);
        throw std::bad_alloc();
    }
    std::memcpy(static_cast<unsigned char*>(p) - sizeof(void*), &raw, sizeof(raw));
    return p;
}
void deallocate_aligned(void* p) noexcept {
    memory_check();
    if (!p) return;
    void* raw;
    std::memcpy(&raw, static_cast<unsigned char*>(p) - sizeof(void*), sizeof(raw));
    std::free(raw);
}
} // namespace

void* operator new(std::size_t n) { return allocate(n); }
void* operator new[](std::size_t n) { return allocate(n); }
void operator delete(void* p) noexcept { deallocate(p); }
void operator delete[](void* p) noexcept { deallocate(p); }
void operator delete(void* p, std::size_t) noexcept { deallocate(p); }
void operator delete[](void* p, std::size_t) noexcept { deallocate(p); }
void* operator new(std::size_t n, std::align_val_t a) { return allocate_aligned(n, a); }
void* operator new[](std::size_t n, std::align_val_t a) { return allocate_aligned(n, a); }
void operator delete(void* p, std::align_val_t) noexcept { deallocate_aligned(p); }
void operator delete[](void* p, std::align_val_t) noexcept { deallocate_aligned(p); }
void operator delete(void* p, std::size_t, std::align_val_t) noexcept { deallocate_aligned(p); }
void operator delete[](void* p, std::size_t, std::align_val_t) noexcept { deallocate_aligned(p); }

namespace {
using namespace astra::rt;
using Clock = std::chrono::steady_clock;

struct Gate {
    std::atomic<bool> entered{false}, proceed{false};
};

struct Fake {
    enum class Mode { impulse, voices, constant };
    struct Call {
        Sample p;
        std::uint32_t frames, first, count, playing;
        double bpm;
    };
    std::array<Call, 512> log{};
    std::array<MidiEvent, 16384> events{};
    std::uint32_t calls = 0, seen = 0;
    std::array<unsigned, 16 * 128> keys{};
    std::array<unsigned, 16> held{};
    std::array<bool, 16> pedal{};
    unsigned keyed = 0;
    Mode mode = Mode::impulse;
    bool record = true, fail_music = false, fail_reset = false;
    Gate* gate = nullptr;

    unsigned sounding() const noexcept {
        unsigned n = keyed;
        for (auto h : held) n += h;
        return n;
    }

    void take(const MidiEvent& m) noexcept {
        const auto ch = m.status & 15u;
        const auto type = m.status & 0xf0u;
        auto& key = keys[ch * 128u + m.data1];
        if (type == 0x90u && m.data2) {
            ++key;
            ++keyed;
        } else if (type == 0x80u || type == 0x90u) {
            if (key) {
                --key;
                --keyed;
                if (pedal[ch]) ++held[ch];
            }
        } else if (type == 0xb0u && m.data1 == 64) {
            pedal[ch] = m.data2 >= 64;
            if (!pedal[ch]) held[ch] = 0;
        }
        // Deliberately ignore all-notes-off/sound-off, like an unmapped CC.
    }

    static std::int32_t process(void* user, float* out, std::uint32_t n, const MidiEvent* midi,
                                std::uint32_t count, Sample p, double bpm,
                                std::uint32_t playing) noexcept {
        auto& f = *static_cast<Fake*>(user);
        CHECK(in_render && n && out && (midi || !count) && count <= kMidiCapacity);
        CHECK(playing <= 1 && p >= 0 && bpm > 0.0);
        if (!playing) CHECK(n == 1);
        for (std::uint32_t i = 0; i < count; ++i) {
            CHECK(midi[i].sample_offset < n && midi[i].data1 < 128 && midi[i].data2 < 128);
            CHECK(midi[i].reserved == 0);
            if (i) CHECK(midi[i - 1].sample_offset <= midi[i].sample_offset);
        }
        if (f.record) {
            CHECK(f.calls < f.log.size() && count <= f.events.size() - f.seen);
            f.log[f.calls++] = {p, n, f.seen, count, playing, bpm};
            for (std::uint32_t i = 0; i < count; ++i) f.events[f.seen++] = midi[i];
        }
        if (f.gate && playing) {
            // Deliberate RT contract violation by this fake only: model a plugin
            // stuck in process while the UI requests exclusivity.
            f.gate->entered.store(true, std::memory_order_release);
            while (!f.gate->proceed.load(std::memory_order_acquire)) std::this_thread::yield();
        }
        if (playing ? f.fail_music : f.fail_reset) {
            std::fill_n(out, static_cast<std::size_t>(n) * 2u, 99.0f);
            return -73;
        }
        std::uint32_t j = 0;
        for (std::uint32_t i = 0; i < n; ++i) {
            float impulse = 0.0f;
            while (j < count && midi[j].sample_offset == i) {
                const auto& m = midi[j++];
                if ((m.status & 0xf0u) == 0x90u) impulse += static_cast<float>(m.data2);
                f.take(m);
            }
            const auto v = f.mode == Mode::constant ? 1.0f :
                           f.mode == Mode::voices ? static_cast<float>(f.sounding()) : impulse;
            out[2u * i] = v;
            out[2u * i + 1] = -v;
        }
        CHECK(j == count);
        return 0;
    }
};

Binding bind(const Timeline& t, Fake& f, std::uint32_t max_frames = 256) {
    return {&t, &f, &Fake::process, max_frames};
}
void run(Kernel& k, float* out, std::uint32_t n) noexcept {
    CHECK(!in_render);
    in_render = true;
    k.render(out, n);
    in_render = false;
}
template<std::size_t N> void run(Kernel& k, std::array<float, N>& out) noexcept {
    static_assert(N % 2 == 0, "stereo");
    run(k, out.data(), static_cast<std::uint32_t>(N / 2));
}
std::uint64_t push(Kernel& k, Op op, Guard g, Sample a = 0, Sample b = 0) {
    const auto s = k.submit(op, g, a, b);
    CHECK(s.code == SubmitCode::queued && s.id && !s.park_ticket);
    return s.id;
}
std::uint64_t push(Kernel& k, Op op) { return push(k, op, k.snapshot().guard); }
Receipt expect(Kernel& k, std::uint64_t id, Result result = Result::applied) {
    Receipt r;
    CHECK(k.poll_receipt(r) && r.id == id && r.result == result);
    return r;
}
template<std::size_t N> void silence(const std::array<float, N>& out) {
    for (auto v : out) CHECK(v == 0.0f);
}

void timeline_validation() {
    const auto bad = [](Sample last, double bpm, const std::vector<Event>& e) {
        try { Timeline::prepare(last, bpm, e); }
        catch (const std::invalid_argument&) { return; }
        CHECK(false);
    };
    bad(0, 120, {});
    bad(20, (std::numeric_limits<double>::quiet_NaN)(), {});
    bad(20, 120, {{20, 0x91, 60, 1}});
    bad(20, 120, {{-1, 0x91, 60, 1}});
    bad(20, 120, {{0, 0x90, 128, 1}});
    bad(20, 120, {{0, 0xc1, 1, 0}});
    bad(20, 120, {{0, 0xd1, 1, 2}});
    bad(20, 120, {{0, 0xf0, 1, 0}});
}

void exact_offsets() {
    std::vector<Event> input{
        {255, 0x93, 71, 7}, {16, 0x92, 65, 3}, {0, 0x91, 60, 1},
        {15, 0x9a, 50, 2}, {16, 0x92, 65, 0}, {16, 0xb3, 1, 11},
        {16, 0x92, 65, 4}, {32, 0x94, 66, 5}, {64, 0x9f, 67, 6}, {256, 0x90, 72, 8}
    };
    const auto t = Timeline::prepare(300, 137, input);
    input[0].data2 = 100; // prepared data is independent of the caller's storage
    std::array<float, 256> want{};
    want[0] = 1; want[15] = 2; want[16] = 7; want[32] = 5; want[64] = 6; want[255] = 7;
    const std::array<Event, 10> ordered{{
        {0, 0x91, 60, 1}, {15, 0x9a, 50, 2}, {16, 0x92, 65, 3}, {16, 0x82, 65, 0},
        {16, 0xb3, 1, 11}, {16, 0x92, 65, 4}, {32, 0x94, 66, 5}, {64, 0x9f, 67, 6},
        {255, 0x93, 71, 7}, {256, 0x90, 72, 8}
    }};
    for (const auto quantum : {256u, 16u, 32u, 64u}) {
        Fake f;
        Kernel k(bind(*t, f, quantum), 0);
        const auto play = push(k, Op::play);
        std::array<float, 512> out{};
        run(k, out);
        CHECK(expect(k, play).position == 0);
        CHECK(f.calls == 256u / quantum && f.seen == 9);
        CHECK(k.snapshot().render_position == 256);
        for (std::size_t i = 0; i < want.size(); ++i)
            CHECK(out[2 * i] == want[i] && out[2 * i + 1] == -want[i]);
        std::array<float, 2> next{};
        run(k, next);
        CHECK(next[0] == 8.0f && next[1] == -8.0f && f.seen == ordered.size());
        std::size_t checked = 0;
        for (std::uint32_t i = 0; i < f.calls; ++i) {
            const auto& c = f.log[i];
            CHECK(c.playing && c.bpm == 137 && c.p == static_cast<Sample>(i) * quantum);
            CHECK(c.frames == (i + 1 == f.calls ? 1u : quantum));
            for (std::uint32_t j = 0; j < c.count; ++j) {
                const auto& m = f.events[c.first + j];
                CHECK(checked < ordered.size());
                const auto& e = ordered[checked++];
                CHECK(m.sample_offset == static_cast<std::uint32_t>(e.sample - c.p));
                CHECK(m.status == e.status && m.data1 == e.data1 && m.data2 == e.data2);
            }
        }
        CHECK(checked == ordered.size());
    }
}

void loop_splits() {
    auto t = Timeline::prepare(50, 120, {{9, 0x90, 60, 9}, {10, 0x93, 61, 2},
        {11, 0xb3, 64, 127}, {12, 0x93, 61, 0}, {13, 0x94, 62, 3}, {14, 0x90, 63, 9}});
    Fake f;
    Kernel k(bind(*t, f, 3), 0);
    auto g = k.snapshot().guard;
    const auto loop = push(k, Op::loop_on, g, 10, 14);
    g.locate = loop;
    const auto play = push(k, Op::play, g);
    std::array<float, 20> out{};
    run(k, out);
    CHECK(expect(k, loop).position == 10);
    expect(k, play);
    const std::array<float, 10> want{2, 0, 0, 3, 2, 0, 0, 3, 2, 0};
    for (std::size_t i = 0; i < want.size(); ++i) CHECK(out[2 * i] == want[i]);
    CHECK(f.calls == 7);
    for (const auto base : {0u, 3u}) {
        CHECK(f.log[base].p == 10 && f.log[base].frames == 3 && f.log[base].playing);
        CHECK(f.log[base + 1].p == 13 && f.log[base + 1].frames == 1);
        CHECK(f.log[base + 2].p == 14 && !f.log[base + 2].playing);
    }
    auto s = k.snapshot();
    CHECK(s.output_frames == 10 && s.music_frames == 10 && s.render_position == 12);
    std::array<float, 4> edge{};
    run(k, edge); // reaches the loop end exactly at the callback edge
    s = k.snapshot();
    CHECK(edge[0] == 0 && edge[2] == 3 && s.render_position == 10 && s.guard.locate == loop);
    CHECK(!f.sounding() && !f.pedal[3]);
    const auto bad_seek = push(k, Op::seek, s.guard, 14);
    run(k, nullptr, 0);
    expect(k, bad_seek, Result::range);
    CHECK(k.snapshot().render_position == 10);
    std::array<float, 2> next{};
    run(k, next);
    CHECK(next[0] == 2.0f && k.snapshot().render_position == 11);
}

void pause_seek_stop() {
    auto t = Timeline::prepare(60, 120, {{0, 0xb5, 64, 127}, {0, 0x95, 60, 5},
        {1, 0x95, 61, 9}, {2, 0x95, 61, 0}, {3, 0x95, 60, 4}, {4, 0x92, 60, 6},
        {16, 0x96, 64, 7}, {20, 0x93, 70, 12}});
    Fake f;
    f.mode = Fake::Mode::voices;
    Kernel k(bind(*t, f), 0);
    const auto play = push(k, Op::play);
    std::array<float, 12> out{};
    run(k, out);
    expect(k, play);
    CHECK(out[10] == 4.0f && f.held[5] == 1 && f.pedal[5]);
    const auto pause = push(k, Op::pause);
    std::array<float, 4> idle{};
    run(k, idle);
    expect(k, pause);
    silence(idle);
    CHECK(k.snapshot().render_position == 6 && !k.snapshot().playing);
    CHECK(f.calls == 2 && !f.log[1].playing && f.log[1].p == 6 && !f.sounding() && !f.pedal[5]);
    unsigned repeated = 0, other = 0, pedals = 0;
    for (std::uint32_t i = f.log[1].first; i < f.seen; ++i) {
        const auto& m = f.events[i];
        if (m.status == 0x85 && m.data1 == 60) ++repeated;
        if (m.status == 0x82 && m.data1 == 60) ++other;
        if (m.status == 0xb5 && m.data2 == 0 && (m.data1 == 64 || m.data1 == 66 || m.data1 == 69))
            ++pedals;
    }
    CHECK(repeated == 2 && other == 1 && pedals == 3);

    const auto old = k.snapshot().guard;
    const auto seek = push(k, Op::seek, old, 16);
    const auto stale = push(k, Op::play, old);
    run(k, idle);
    CHECK(expect(k, seek).guard.locate == seek);
    expect(k, stale, Result::stale);
    silence(idle);
    CHECK(f.calls == 2 && k.snapshot().render_position == 16);
    const auto again = push(k, Op::play);
    run(k, idle);
    expect(k, again);
    CHECK(idle[0] == 1.0f && f.sounding() == 1 && k.snapshot().render_position == 18);
    const auto jump = push(k, Op::seek, k.snapshot().guard, 20);
    run(k, idle);
    CHECK(expect(k, jump).position == 20);
    CHECK(!f.log[3].playing && f.log[3].p == 18 && f.log[4].p == 20 && f.log[4].playing);
    CHECK(idle[0] == 1.0f && f.keys[6 * 128 + 64] == 0 && f.keys[3 * 128 + 70] == 1);
    const auto stop = push(k, Op::stop);
    run(k, idle);
    CHECK(expect(k, stop).position == 0);
    silence(idle);
    CHECK(!f.sounding() && !k.snapshot().playing && k.snapshot().render_position == 0);
}

void ramp_and_end() {
    auto t = Timeline::prepare(6, 120, {});
    Fake f;
    f.mode = Fake::Mode::constant;
    Kernel k(bind(*t, f), 4);
    const auto play = push(k, Op::play);
    std::array<float, 8> up{};
    run(k, up);
    expect(k, play);
    for (std::size_t i = 0; i < 4; ++i) CHECK(up[2 * i] == static_cast<float>(i + 1) / 4.0f);
    const auto pause = push(k, Op::pause);
    std::array<float, 4> down{};
    run(k, down);
    expect(k, pause);
    CHECK(down[0] == 0.75f && down[2] == 0.5f && k.snapshot().render_position == 4);
    run(k, down);
    CHECK(down[0] == 0.25f && down[2] == 0.0f && f.calls == 1);
    CHECK(k.snapshot().render_position == 4);
    const auto resume = push(k, Op::play);
    std::array<float, 12> end{};
    run(k, end);
    expect(k, resume);
    const std::array<float, 6> want{0.25f, 0.5f, 0.375f, 0.25f, 0.125f, 0.0f};
    for (std::size_t i = 0; i < want.size(); ++i) CHECK(end[2 * i] == want[i]);
    const auto s = k.snapshot();
    CHECK(!s.playing && s.render_position == 6 && s.music_frames == 6 && s.output_frames == 14);
    CHECK(f.calls == 2 && f.log[1].p == 4 && f.log[1].frames == 2);
}

void extreme_position() {
    const auto end = (std::numeric_limits<Sample>::max)();
    auto t = Timeline::prepare(end, 120, {{end - 2, 0x91, 60, 2}, {end - 1, 0x92, 60, 5}});
    Fake f;
    Kernel k(bind(*t, f), 0);
    auto g = k.snapshot().guard;
    const auto seek = push(k, Op::seek, g, end - 2);
    g.locate = seek;
    const auto play = push(k, Op::play, g);
    std::array<float, 8> out{};
    run(k, out);
    expect(k, seek);
    expect(k, play);
    CHECK(out[0] == 2 && out[2] == 5 && out[4] == 0 && out[6] == 0);
    CHECK(f.calls == 2 && f.log[0].frames == 2 && f.log[1].p == end && !f.log[1].playing);
    CHECK(k.snapshot().render_position == end && !k.snapshot().playing && !f.sounding());
}

void command_overflow() {
    auto t = Timeline::prepare(100, 120, {{0, 0xb0, 64, 127}, {0, 0x90, 60, 5}});
    Fake f;
    Kernel k(bind(*t, f), 0);
    const auto play = push(k, Op::play);
    std::array<float, 4> out{};
    run(k, out);
    expect(k, play);
    CHECK(f.sounding() == 1 && f.pedal[0]);
    const auto g = k.snapshot().guard;
    std::array<std::uint64_t, kCommandCapacity> ids{};
    for (auto& id : ids) id = push(k, Op::seek, g, 50);
    const auto overflow = k.submit(Op::pause, g);
    const auto token = overflow.park_ticket;
    CHECK(overflow.code == SubmitCode::full && !overflow.id && token && !k.quiesced(token));
    CHECK(!k.resume(token) && k.submit(Op::play, g).code == SubmitCode::parked);
    run(k, out);
    silence(out);
    CHECK(k.quiesced(token) && !f.sounding() && !f.pedal[0]);
    for (const auto id : ids) expect(k, id, Result::canceled);
    CHECK(k.snapshot().render_position == 2);
    CHECK(k.resume(token) && !k.quiesced(token));
    CHECK(k.submit(Op::play, g).code == SubmitCode::stale);
    run(k, out);
    silence(out);
    CHECK(f.calls == 2 && !k.snapshot().playing);

    // Finished but unread receipts also consume ring credits.
    for (auto& id : ids) id = push(k, Op::pause);
    run(k, nullptr, 0);
    const auto unread = k.submit(Op::pause, k.snapshot().guard);
    CHECK(unread.code == SubmitCode::full && unread.park_ticket && !k.quiesced(unread.park_ticket));
    for (const auto id : ids) expect(k, id);
    Receipt extra;
    CHECK(!k.poll_receipt(extra));
    run(k, out);
    CHECK(k.quiesced(unread.park_ticket) && f.calls == 2);
}

void event_and_voice_limits() {
    {
        std::vector<Event> e{{0, 0xb0, 64, 127}, {0, 0x90, 60, 1}};
        e.insert(e.end(), kMidiCapacity, Event{1, 0xb0, 7, 1});
        e.push_back({1, 0x80, 60, 0});
        auto t = Timeline::prepare(20, 120, e);
        Fake f;
        Kernel k(bind(*t, f, 1), 0);
        const auto play = push(k, Op::play);
        std::array<float, 4> out{};
        run(k, out);
        expect(k, play);
        const auto s = k.snapshot();
        CHECK(out[0] == 1 && out[2] == 0 && s.fault == Fault::event_capacity);
        CHECK(s.render_position == 1 && s.music_frames == 1 && !s.playing);
        CHECK(f.calls == 2 && !f.log[1].playing && !f.sounding() && !f.pedal[0]);
        const auto again = push(k, Op::play);
        run(k, out);
        expect(k, again, Result::faulted);
        silence(out);
        CHECK(f.calls == 2);
    }
    {
        std::vector<Event> e;
        for (std::uint32_t i = 0; i < kVoiceCapacity; ++i)
            e.push_back({0, static_cast<std::uint8_t>(0x90u | (i & 15u)), 0, 1});
        e.push_back({1, 0x80, 0, 0});
        e.push_back({1, 0x90, 0, 1}); // at cap, but ordered off-before-on fits
        e.push_back({2, 0x91, 0, 1}); // exceeds cap before processor is called
        auto t = Timeline::prepare(20, 120, e);
        Fake f;
        Kernel k(bind(*t, f, 1), 0);
        const auto play = push(k, Op::play);
        std::array<float, 6> out{};
        run(k, out);
        expect(k, play);
        CHECK(k.snapshot().fault == Fault::voice_capacity && k.snapshot().render_position == 2);
        CHECK(out[2] == 1 && out[4] == 0 && f.calls == 3 && !f.sounding());
        CHECK(f.log[2].count == kMidiCapacity && !f.log[2].playing);
    }
}

void work_and_buffer_limits() {
    {
        auto t = Timeline::prepare(10, 120, {{0, 0x90, 60, 1}});
        Fake f;
        Kernel k(bind(*t, f), 0);
        const auto loop = push(k, Op::loop_on, k.snapshot().guard, 0, 1);
        auto g = k.snapshot().guard;
        g.locate = loop;
        const auto play = push(k, Op::play, g);
        std::array<float, 2 * (kMaxMusicCalls + 1)> out{};
        run(k, out);
        expect(k, loop);
        expect(k, play);
        const auto s = k.snapshot();
        CHECK(s.fault == Fault::work_budget && s.music_frames == kMaxMusicCalls && !s.playing);
        CHECK(s.render_position == 0 && !f.sounding() && f.calls == 2 * kMaxMusicCalls);
        CHECK(out[2 * (kMaxMusicCalls - 1)] == 1 && out[2 * kMaxMusicCalls] == 0);
    }
    {
        std::vector<Event> e;
        for (std::uint32_t i = 0; i < kMaxMusicEvents; ++i)
            e.push_back({static_cast<Sample>(i / kMidiCapacity), 0xb0, 1, 1});
        e.push_back({static_cast<Sample>(kMaxMusicEvents / kMidiCapacity), 0xb0, 1, 1});
        auto t = Timeline::prepare(20, 120, e);
        Fake f;
        Kernel k(bind(*t, f, 1), 0);
        const auto play = push(k, Op::play);
        std::array<float, 8> out{};
        run(k, out);
        expect(k, play);
        CHECK(k.snapshot().fault == Fault::work_budget && k.snapshot().music_frames == 2);
        CHECK(f.calls == 3 && !f.log[2].playing);
    }
    {
        auto t = Timeline::prepare(20, 120, {});
        Fake f;
        Kernel k(bind(*t, f), 0);
        const auto play = push(k, Op::play);
        const auto n = kMaxCallbackFrames + 1;
        std::vector<float> buf(static_cast<std::size_t>(n) * 2 + 2, 9.0f);
        run(k, buf.data() + 1, n);
        expect(k, play, Result::faulted);
        CHECK(k.snapshot().fault == Fault::callback_buffer && !f.calls);
        CHECK(buf.front() == 9.0f && buf.back() == 9.0f);
        for (std::size_t i = 1; i + 1 < buf.size(); ++i) CHECK(buf[i] == 0.0f);
    }
}

void lifecycle_and_retirement() {
    auto t = Timeline::prepare(20, 120, {});
    auto f = std::make_unique<Fake>();
    Kernel k(bind(*t, *f), 0);
    const auto old = k.snapshot().guard;
    const auto first = k.request_quiesce(); // already musically paused
    CHECK(first && !k.request_quiesce());
    for (int i = 0; i < 20; ++i) CHECK(!k.quiesced(first));
    CHECK(!k.replace_binding(first, {}) && !k.resume(first));
    std::array<float, 4> out{1, 2, 3, 4};
    run(k, out);
    silence(out);
    CHECK(k.quiesced(first) && !f->calls && !k.snapshot().bound);
    const auto empty = k.replace_binding(first, {});
    CHECK(empty > old.generation);
    f.reset();
    t.reset();
    run(k, out); // callback lane still alive after plugin/timeline retirement
    silence(out);
    CHECK(k.quiesced(first) && k.resume(first));
    CHECK(k.submit(Op::play, old).code == SubmitCode::stale);

    // A new request can overtake a resume not yet seen by the callback.
    const auto second = k.request_quiesce();
    CHECK(second > first && !k.quiesced(first) && !k.quiesced(second));
    CHECK(!k.resume(first) && !k.replace_binding(first, {}));
    run(k, out);
    CHECK(k.quiesced(second) && !k.quiesced(first) && !k.replace_binding(first, {}));
    CHECK(k.resume(second));
    run(k, out);
    CHECK(!k.snapshot().bound && k.snapshot().guard.generation == empty);
    const auto absent = push(k, Op::play);
    run(k, out);
    expect(k, absent, Result::no_binding);

    auto new_t = Timeline::prepare(20, 120, {{0, 0x90, 60, 6}});
    auto new_f = std::make_unique<Fake>();
    const auto third = k.request_quiesce();
    run(k, out);
    const auto generation = k.replace_binding(third, bind(*new_t, *new_f));
    CHECK(generation > empty && k.resume(third));
    run(k, out);
    CHECK(k.snapshot().guard.generation == generation && k.snapshot().bound && !new_f->calls);
    const auto play = push(k, Op::play);
    run(k, out);
    expect(k, play);
    CHECK(out[0] == 6 && new_f->calls == 1);
    // Simulate definitive device retirement: no more render calls below.
}

void processor_failures() {
    auto t = Timeline::prepare(20, 120, {{0, 0x90, 60, 1}});
    {
        Fake f;
        f.fail_music = true;
        Kernel k(bind(*t, f), 0);
        const auto play = push(k, Op::play);
        std::array<float, 4> out{};
        run(k, out);
        expect(k, play); // command was applied before the process failure
        silence(out);
        const auto s = k.snapshot();
        CHECK(s.fault == Fault::processor && s.processor_code == -73);
        CHECK(!s.playing && s.render_position == 0 && s.music_frames == 0 && f.calls == 1);
        const auto park = k.request_quiesce();
        run(k, out);
        CHECK(k.quiesced(park) && f.calls == 1);
    }
    {
        Fake f;
        Kernel k(bind(*t, f), 0);
        const auto play = push(k, Op::play);
        std::array<float, 4> out{};
        run(k, out);
        expect(k, play);
        f.fail_reset = true;
        const auto park = k.request_quiesce();
        run(k, out);
        silence(out);
        CHECK(k.quiesced(park) && k.snapshot().fault == Fault::reset && f.calls == 2);
        CHECK(k.snapshot().processor_code == -73 && f.sounding() == 1);
        CHECK(k.resume(park)); // reusing the same generation does not clear quarantine
        run(k, out);
        const auto again = push(k, Op::play);
        run(k, out);
        expect(k, again, Result::faulted);
        silence(out);
        CHECK(f.calls == 2);
    }
}

void inflight_quiesce() {
    auto t = Timeline::prepare(20, 120, {{0, 0x90, 60, 1}});
    Fake f;
    Gate gate;
    f.gate = &gate;
    Kernel k(bind(*t, f), 0);
    const auto play = push(k, Op::play);
    std::thread lane([&] { std::array<float, 4> out{}; run(k, out); });
    const auto deadline = Clock::now() + std::chrono::seconds(15);
    while (!gate.entered.load(std::memory_order_acquire)) {
        CHECK(Clock::now() < deadline);
        std::this_thread::yield();
    }
    const auto park = k.request_quiesce();
    CHECK(park && !k.quiesced(park) && !k.replace_binding(park, {}) && !k.resume(park));
    gate.proceed.store(true, std::memory_order_release);
    lane.join(); // synchronizes the test's callback-lane handoff below
    CHECK(!k.quiesced(park));
    expect(k, play);
    std::array<float, 4> out{};
    run(k, out);
    CHECK(k.quiesced(park) && !f.sounding() && f.calls == 2);
    silence(out);
    f.gate = nullptr; // safe state mutation under grant, even with later callbacks
    f.mode = Fake::Mode::constant;
    run(k, out);
    CHECK(f.calls == 2);
    CHECK(k.resume(park));
    run(k, out);
    CHECK(k.snapshot().render_position == 2 && f.calls == 2);
    const auto again = push(k, Op::play);
    run(k, out);
    expect(k, again);
    CHECK(out[0] == 1.0f && f.calls == 3);
}

void concurrent_ring_and_snapshot() {
    auto t = Timeline::prepare(2000, 120, {});
    Fake f;
    Kernel k(bind(*t, f), 0);
    auto g = k.snapshot().guard;
    g.locate = kAnyLocate;
    std::atomic<bool> stop{false};
    std::thread lane([&] {
        std::array<float, 16> out{};
        while (!stop.load(std::memory_order_acquire)) {
            run(k, out);
            std::this_thread::yield();
        }
    });
    constexpr std::uint64_t total = 20000;
    const auto target = [](std::uint64_t id) { return static_cast<Sample>(100 + id % 1000); };
    const auto deadline = Clock::now() + std::chrono::seconds(15);
    std::uint64_t sent = 0, read = 0, last_callback = 0;
    while (read < total) {
        while (sent < total && sent - read < kCommandCapacity) {
            const auto next = sent + 1;
            CHECK(push(k, Op::seek, g, target(next)) == next);
            sent = next;
        }
        Receipt r;
        while (k.poll_receipt(r)) {
            ++read;
            CHECK(r.id == read && r.result == Result::applied && r.guard.locate == r.id);
            CHECK(r.position == target(r.id));
        }
        const auto s = k.snapshot();
        CHECK(s.callbacks >= last_callback && s.output_frames == s.callbacks * 8u);
        CHECK(s.fault == Fault::none && !s.playing && !s.music_frames);
        CHECK(s.render_position == (s.guard.locate ? target(s.guard.locate) : 0));
        last_callback = s.callbacks;
        CHECK(Clock::now() < deadline);
        std::this_thread::yield();
    }
    stop.store(true, std::memory_order_release);
    lane.join();
    CHECK(k.snapshot().guard.locate == total && !f.calls);
}
} // namespace

int main() {
    timeline_validation();
    exact_offsets();
    loop_splits();
    pause_seek_stop();
    ramp_and_end();
    extreme_position();
    command_overflow();
    event_and_voice_limits();
    work_and_buffer_limits();
    lifecycle_and_retirement();
    processor_failures();
    inflight_quiesce();
    concurrent_ring_and_snapshot();
    std::puts("kernel tests passed");
    return 0;
}
