#include "live.h"
#include <cmath>
#include <vector>
#include "../astra_vst_host.h"

#include <algorithm>
#include <array>
#include <chrono>
#include <climits>
#include <condition_variable>
#include <cstdio>
#include <cstdlib>
#include <memory>
#include <mutex>
#include <thread>

#define CHECK(expr) do { if (!(expr)) { \
    std::fprintf(stderr, "%s:%d: CHECK(%s) failed\n", __FILE__, __LINE__, #expr); \
    std::abort(); \
} } while (false)

// Test-only host-heap tripwire. The fake callback runs on the owner thread;
// all allocation/retirement must stay on control, not inside native render.
thread_local bool callback_heap_forbidden=false;
void* operator new(std::size_t n) {
    if(callback_heap_forbidden)std::abort();
    if(void* p=std::malloc(n?n:1))return p;throw std::bad_alloc();
}
void* operator new[](std::size_t n){return ::operator new(n);}
void operator delete(void* p) noexcept {if(callback_heap_forbidden && p)std::abort();std::free(p);}
void operator delete[](void* p) noexcept {::operator delete(p);}
void operator delete(void* p,std::size_t) noexcept {::operator delete(p);}
void operator delete[](void* p,std::size_t) noexcept {::operator delete(p);}

namespace {
using namespace std::chrono_literals;

template<class T> T record() {
    T r{};
    r.size = static_cast<uint32_t>(sizeof(T));
    r.version = ASTRA_RT_ABI_VERSION;
    return r;
}

struct FakeProcessor {
    explicit FakeProcessor(float v) : value(v) {}
    const float value;
    std::mutex mutex;
    std::condition_variable cv;
    bool block = false, entered = false;
    uint32_t music_calls = 0, cleanup_calls = 0, frames_seen = 0, count_seen = 0;
    int64_t position_seen = 0;
    double bpm_seen = 0;
    std::array<AvMidiEvent, 16> seen{};

    void wait_entered() {
        std::unique_lock<std::mutex> lock(mutex);
        CHECK(cv.wait_for(lock, 10s, [&] { return entered; }));
    }
    void unblock() {
        std::lock_guard<std::mutex> lock(mutex);
        block = false;
        cv.notify_all();
    }
};

// Deliberately blocks in one test. Only the injected fake does this, never
// the bridge. Fake observations are read after a device completion or join.
extern "C" int32_t ASTRA_RT_CALL fake_process(void* user, float* out, uint32_t frames,
    const AvMidiEvent* events, uint32_t count, int64_t pos, double bpm, uint32_t playing) {
    auto& p = *static_cast<FakeProcessor*>(user);
    CHECK(out && frames);
    CHECK((events != nullptr) == (count != 0));
    if (playing) {
        CHECK(playing == 1 && count <= p.seen.size());
        ++p.music_calls;
        p.frames_seen = frames;
        p.count_seen = count;
        p.position_seen = pos;
        p.bpm_seen = bpm;
        if (count) std::copy_n(events, count, p.seen.begin());
        std::unique_lock<std::mutex> lock(p.mutex);
        p.entered = true;
        p.cv.notify_all();
        CHECK(p.cv.wait_for(lock, 10s, [&] { return !p.block; }));
    } else {
        CHECK(frames == 1);
        ++p.cleanup_calls;
    }
    std::fill_n(out, frames * 2u, p.value);
    return 0;
}

// Retain synchronously before starting the lane. Requesting retirement
// prevents new entries, but never releases while an accepted callback is
// outstanding. The worker returns the lease at its final callback boundary.
class FakeOwner {
public:
    explicit FakeOwner(AstraRtBridge* b, unsigned long flags = 0)
        : user_(b), callback_(astra_rt_get_callback()), flags_(flags) {
        CHECK(callback_ == &astra_rt_render);
        astra_rt_device_retain(user_);
        worker_ = std::thread([this] { run(); });
    }
    ~FakeOwner() { request_retire(); join(); }

    bool queue(unsigned long frames, float* out) {
        std::lock_guard<std::mutex> lock(mutex_);
        if (stop_ || pending_) return false;
        frames_ = frames;
        out_ = out;
        pending_ = true;
        cv_.notify_all();
        return true;
    }
    void pump(unsigned long frames, float* out) {
        CHECK(queue(frames, out));
        std::unique_lock<std::mutex> lock(mutex_);
        CHECK(cv_.wait_for(lock, 10s, [&] { return !pending_; }));
    }
    void request_retire() {
        std::lock_guard<std::mutex> lock(mutex_);
        stop_ = true;
        cv_.notify_all();
    }
    void join() { if (worker_.joinable()) worker_.join(); }
    int last_result() { std::lock_guard<std::mutex> lock(mutex_); return last_result_; }
    unsigned calls() {
        std::lock_guard<std::mutex> lock(mutex_);
        return calls_;
    }

private:
    void run() {
        std::unique_lock<std::mutex> lock(mutex_);
        for (;;) {
            cv_.wait(lock, [&] { return pending_ || stop_; });
            if (!pending_) break;
            const auto frames = frames_;
            auto* out = out_;
            lock.unlock();
            callback_heap_forbidden=true;
            const int rc = callback_(nullptr, out, frames, nullptr, flags_, user_);
            callback_heap_forbidden=false;
            CHECK(rc == 0 || rc == 2);
            lock.lock();
            last_result_ = rc;
            if (rc == 2) stop_ = true;
            ++calls_;
            pending_ = false;
            cv_.notify_all();
        }
        lock.unlock();
        astra_rt_device_release(user_);
        // No further use of callback_ or user_.
    }

    void* const user_;
    const AstraRtCallback callback_;
    const unsigned long flags_;
    std::mutex mutex_;
    std::condition_variable cv_;
    bool stop_ = false, pending_ = false;
    unsigned long frames_ = 0;
    float* out_ = nullptr;
    unsigned calls_ = 0;
    int last_result_ = 0;
    std::thread worker_;
};

AstraRtTimelineSpec timeline(int64_t duration, double bpm, uint32_t events) {
    auto spec = record<AstraRtTimelineSpec>();
    spec.duration = duration;
    spec.bpm = bpm;
    spec.event_count = events;
    spec.max_frames = 64;
    return spec;
}
AstraRtBridge* create(const AstraRtTimelineSpec* spec = nullptr,
                      const AstraRtEvent* events = nullptr, FakeProcessor* fake = nullptr) {
    const auto config = record<AstraRtConfig>(); // no ramp: exact sample comparisons
    AstraRtBridge* b = nullptr;
    CHECK(astra_rt_create(&config, spec, events, fake, fake ? &fake_process : nullptr, &b) == ASTRA_RT_OK);
    CHECK(b);
    return b;
}
AstraRtSnapshot view(AstraRtBridge* b) {
    auto out = record<AstraRtSnapshot>();
    CHECK(astra_rt_snapshot(b, &out) == ASTRA_RT_OK);
    return out;
}
AstraRtSubmission submit(AstraRtBridge* b, AstraRtGuard guard, uint32_t op, int64_t a = 0) {
    auto cmd = record<AstraRtCommand>();
    cmd.op = op;
    cmd.guard = guard;
    cmd.a = a;
    auto out = record<AstraRtSubmission>();
    CHECK(astra_rt_submit(b, &cmd, &out) == ASTRA_RT_OK);
    return out;
}
uint64_t park(AstraRtBridge* b) {
    uint64_t ticket = 0;
    CHECK(astra_rt_request_park(b, &ticket) == ASTRA_RT_OK && (ticket & 1u));
    return ticket;
}
uint32_t park_state(AstraRtBridge* b, uint64_t ticket) {
    uint32_t state = 0;
    CHECK(astra_rt_park_status(b, ticket, &state) == ASTRA_RT_OK);
    return state;
}

void dispatch_and_replace() {
    auto old = std::make_unique<FakeProcessor>(0.25f);
    FakeProcessor next(0.5f);
    const AstraRtEvent events[] = {{25, 0x80, 60, 7, 0, 0}, {17, 0x90, 60, 100, 0, 0},
                                  {17, 0xb0, 7, 101, 0, 0}};
    const auto spec = timeline(256, 120.0, 3);
    auto* b = create(&spec, events, old.get());
    auto s = view(b);
    CHECK(s.bound && !s.playing && !s.callbacks && !s.active_leases && !s.released);
    FakeOwner owner(b);
    s = view(b);
    CHECK(s.active_leases == 1 && !s.released);
    const auto play = submit(b, s.guard, ASTRA_RT_PLAY);
    CHECK(play.code == ASTRA_RT_SUBMIT_QUEUED && play.id && !play.park_ticket);
    auto receipt = record<AstraRtReceipt>();
    CHECK(astra_rt_poll_receipt(b, &receipt) == ASTRA_RT_EMPTY);

    std::array<float, 64> output{};
    owner.pump(32, output.data());
    CHECK(astra_rt_poll_receipt(b, &receipt) == ASTRA_RT_OK);
    CHECK(receipt.id == play.id && receipt.result == ASTRA_RT_APPLIED && receipt.position == 0);
    CHECK(old->music_calls == 1 && old->frames_seen == 32 && old->count_seen == 3);
    CHECK(old->position_seen == 0 && old->bpm_seen == 120.0);
    CHECK(old->seen[0].sample_offset == 17 && old->seen[0].status == 0x90 &&
          old->seen[0].data1 == 60 && old->seen[0].data2 == 100 && !old->seen[0].reserved);
    CHECK(old->seen[1].sample_offset == 17 && old->seen[1].status == 0xb0);
    CHECK(old->seen[2].sample_offset == 25 && old->seen[2].status == 0x80);
    CHECK(std::all_of(output.begin(), output.end(), [](float x) { return x == 0.25f; }));
    s = view(b);
    CHECK(s.callbacks == 1 && s.music_frames == 32 && s.output_frames == 32 && s.render_position == 32);
    const auto old_gen = s.guard.generation;
    const auto pause = submit(b, s.guard, ASTRA_RT_PAUSE);
    CHECK(pause.code == ASTRA_RT_SUBMIT_QUEUED);
    const auto ticket = park(b);
    uint64_t gen = 0;
    CHECK(astra_rt_request_park(b, &gen) == ASTRA_RT_BUSY && !gen);
    CHECK(astra_rt_resume(b, ticket) == ASTRA_RT_PENDING);
    CHECK(astra_rt_replace(b, ticket, nullptr, nullptr, nullptr, nullptr, &gen) == ASTRA_RT_PENDING);
    CHECK(park_state(b, ticket + 2) == ASTRA_RT_PARK_STALE);
    CHECK(park_state(b, ticket) == ASTRA_RT_PARK_PENDING);
    CHECK(view(b).park_state == ASTRA_RT_PARK_PENDING && owner.calls() == 1);
    CHECK(astra_rt_destroy(b) == ASTRA_RT_BUSY);
    CHECK(submit(b, s.guard, ASTRA_RT_PLAY).code == ASTRA_RT_SUBMIT_PARKED);

    owner.pump(0, nullptr); // actual native lane, not a UI-side acknowledgement
    CHECK(park_state(b, ticket) == ASTRA_RT_PARK_ACK);
    CHECK(!view(b).bound && old->cleanup_calls == 1);
    CHECK(astra_rt_poll_receipt(b, &receipt) == ASTRA_RT_OK);
    CHECK(receipt.id == pause.id && receipt.result == ASTRA_RT_CANCELED);

    const AstraRtEvent new_event{4, 0x91, 62, 100, 0, 0};
    const auto new_spec = timeline(128, 90.0, 1);
    CHECK(astra_rt_replace(b, ticket + 2, &new_spec, &new_event, &next, &fake_process, &gen) ==
          ASTRA_RT_STALE_TICKET);
    auto invalid = new_spec;
    invalid.duration = 4; // event at exclusive end: rejected without changing staging
    CHECK(astra_rt_replace(b, ticket, &invalid, &new_event, &next, &fake_process, &gen) == ASTRA_RT_INVALID);
    CHECK(!gen && park_state(b, ticket) == ASTRA_RT_PARK_ACK);
    CHECK(astra_rt_replace(b, ticket, &new_spec, &new_event, &next, &fake_process, &gen) == ASTRA_RT_OK);
    CHECK(gen == old_gen + 1);
    old.reset(); // no RT reference to the former plugin or timeline remains
    owner.pump(32, output.data());
    CHECK(!next.music_calls && std::all_of(output.begin(), output.end(), [](float x) { return x == 0; }));
    CHECK(astra_rt_resume(b, ticket) == ASTRA_RT_OK);
    CHECK(astra_rt_resume(b, ticket) == ASTRA_RT_STALE_TICKET);
    CHECK(park_state(b, ticket) == ASTRA_RT_PARK_STALE);
    owner.pump(0, nullptr);
    s = view(b);
    CHECK(s.bound && !s.playing && s.guard.generation == gen && s.guard.phase == ticket + 1);
    CHECK(submit(b, s.guard, ASTRA_RT_PLAY).code == ASTRA_RT_SUBMIT_QUEUED);
    owner.pump(32, output.data());
    CHECK(next.music_calls == 1 && next.position_seen == 0 && next.bpm_seen == 90.0);
    CHECK(next.count_seen == 1 && next.seen[0].sample_offset == 4 && next.seen[0].status == 0x91);
    CHECK(std::all_of(output.begin(), output.end(), [](float x) { return x == 0.5f; }));

    const auto detach = park(b);
    owner.pump(0, nullptr);
    CHECK(park_state(b, detach) == ASTRA_RT_PARK_ACK && next.cleanup_calls == 1);
    CHECK(astra_rt_replace(b, detach, nullptr, nullptr, nullptr, nullptr, &gen) == ASTRA_RT_OK);
    CHECK(astra_rt_resume(b, detach) == ASTRA_RT_OK);
    owner.pump(0, nullptr);
    s = view(b);
    CHECK(!s.bound && s.guard.generation == gen && next.music_calls == 1);
    owner.request_retire();
    owner.join();
    s = view(b);
    CHECK(!s.active_leases && s.released);
    CHECK(astra_rt_destroy(b) == ASTRA_RT_OK);
    CHECK(!owner.queue(32, output.data()));
}

void blocked_retirement() {
    FakeProcessor fake(0.75f);
    fake.block = true;
    const auto spec = timeline(256, 120.0, 0);
    auto* b = create(&spec, nullptr, &fake);
    FakeOwner owner(b);
    CHECK(submit(b, view(b).guard, ASTRA_RT_PLAY).code == ASTRA_RT_SUBMIT_QUEUED);
    std::array<float, 64> output{};
    CHECK(owner.queue(32, output.data()));
    fake.wait_entered();
    const auto ticket = park(b); // render already sampled the previous phase
    owner.request_retire();      // cannot deliver release while fake_process is blocked
    CHECK(!owner.queue(32, output.data()));
    const auto live = view(b);
    CHECK(live.active_leases == 1 && !live.released);
    CHECK(park_state(b, ticket) == ASTRA_RT_PARK_PENDING);
    CHECK(astra_rt_destroy(b) == ASTRA_RT_BUSY && owner.calls() == 0);
    fake.unblock();
    owner.join();
    const auto retired = view(b);
    CHECK(retired.active_leases == 0 && retired.released && retired.callbacks == 1);
    CHECK(park_state(b, ticket) == ASTRA_RT_PARK_PENDING); // release did not manufacture an ack
    CHECK(astra_rt_resume(b, ticket) == ASTRA_RT_PENDING);
    CHECK(!owner.queue(32, output.data()) && owner.calls() == 1 && fake.music_calls == 1);
    CHECK(astra_rt_destroy(b) == ASTRA_RT_OK);
    CHECK(!owner.queue(32, output.data()) && owner.calls() == 1);
}

void counted_leases() {
    auto* b = create();
    FakeOwner first(b), second(b); // neither owner renders; this only tests lease accounting
    CHECK(view(b).active_leases == 2);
    first.request_retire();
    first.join();
    const auto live = view(b);
    CHECK(live.active_leases == 1 && !live.released);
    CHECK(astra_rt_destroy(b) == ASTRA_RT_BUSY);
    second.request_retire();
    second.join();
    const auto retired = view(b);
    CHECK(!retired.active_leases && retired.released);
    CHECK(astra_rt_destroy(b) == ASTRA_RT_OK);
}

void emergency_park() {
    auto* b = create();
    FakeOwner owner(b);
    const auto g = view(b).guard;
    for (uint32_t i = 0; i < ASTRA_RT_COMMAND_CAPACITY; ++i) {
        const auto s = submit(b, g, ASTRA_RT_STOP);
        CHECK(s.code == ASTRA_RT_SUBMIT_QUEUED && s.id == uint64_t{i} + 1);
    }
    const auto full = submit(b, g, ASTRA_RT_STOP);
    CHECK(full.code == ASTRA_RT_SUBMIT_FULL && !full.id && full.park_ticket == view(b).park_ticket);
    CHECK(park_state(b, full.park_ticket) == ASTRA_RT_PARK_PENDING);
    owner.pump(0, nullptr);
    CHECK(park_state(b, full.park_ticket) == ASTRA_RT_PARK_ACK);
    auto receipt = record<AstraRtReceipt>();
    for (uint32_t i = 0; i < ASTRA_RT_COMMAND_CAPACITY; ++i) {
        CHECK(astra_rt_poll_receipt(b, &receipt) == ASTRA_RT_OK);
        CHECK(receipt.id == uint64_t{i} + 1 && receipt.result == ASTRA_RT_CANCELED);
    }
    CHECK(astra_rt_poll_receipt(b, &receipt) == ASTRA_RT_EMPTY);
    CHECK(astra_rt_resume(b, full.park_ticket) == ASTRA_RT_OK);
    owner.request_retire();
    owner.join();
    CHECK(astra_rt_destroy(b) == ASTRA_RT_OK);
}

void invalid_and_wrong_thread() {
    CHECK(astra_rt_abi_version() == ASTRA_RT_ABI_VERSION);
    auto config = record<AstraRtConfig>();
    AstraRtBridge* b = nullptr;
    CHECK(astra_rt_create(nullptr, nullptr, nullptr, nullptr, nullptr, &b) == ASTRA_RT_INVALID);
    config.version = 2;
    CHECK(astra_rt_create(&config, nullptr, nullptr, nullptr, nullptr, &b) == ASTRA_RT_INVALID);
    config.version = ASTRA_RT_ABI_VERSION;
    config.ramp_frames = ASTRA_RT_MAX_RAMP_FRAMES + 1;
    CHECK(astra_rt_create(&config, nullptr, nullptr, nullptr, nullptr, &b) == ASTRA_RT_INVALID);
    config.ramp_frames = 0;
    FakeProcessor fake(0.25f);
    auto spec = timeline(64, 120.0, ASTRA_RT_MAX_EVENTS + 1);
    AstraRtEvent event{17, 0x90, 60, 100, 0, 0};
    CHECK(astra_rt_create(&config, &spec, &event, &fake, &fake_process, &b) == ASTRA_RT_INVALID);
    spec.event_count = 1;
    CHECK(astra_rt_create(&config, &spec, nullptr, &fake, &fake_process, &b) == ASTRA_RT_INVALID);
    CHECK(astra_rt_create(&config, &spec, &event, nullptr, &fake_process, &b) == ASTRA_RT_INVALID);
    event.reserved2 = 1;
    CHECK(astra_rt_create(&config, &spec, &event, &fake, &fake_process, &b) == ASTRA_RT_INVALID);
    event.reserved2 = 0;
    event.status = 0xc0; // unsupported by the kernel
    CHECK(astra_rt_create(&config, &spec, &event, &fake, &fake_process, &b) == ASTRA_RT_INVALID);
    CHECK(!b);

    b = create();
    auto out = record<AstraRtSnapshot>();
    --out.size;
    out.callbacks = 123;
    CHECK(astra_rt_snapshot(b, &out) == ASTRA_RT_INVALID && out.callbacks == 123);
    const auto g = view(b).guard;
    CHECK(submit(b, g, UINT32_MAX).code == ASTRA_RT_SUBMIT_BAD_COMMAND);
    CHECK(submit(b, g, ASTRA_RT_PLAY, 1).code == ASTRA_RT_SUBMIT_BAD_COMMAND);
    uint32_t state = 99;
    CHECK(astra_rt_park_status(b, 0, &state) == ASTRA_RT_INVALID && state == 99);
    CHECK(astra_rt_request_park(b, nullptr) == ASTRA_RT_INVALID);
    std::thread wrong([&] {
        auto s = record<AstraRtSnapshot>();
        auto cmd = record<AstraRtCommand>();
        auto sub = record<AstraRtSubmission>();
        auto receipt = record<AstraRtReceipt>();
        uint64_t u = 0;
        uint32_t p = 0;
        CHECK(astra_rt_snapshot(b, &s) == ASTRA_RT_WRONG_THREAD);
        CHECK(astra_rt_submit(b, &cmd, &sub) == ASTRA_RT_WRONG_THREAD);
        CHECK(astra_rt_poll_receipt(b, &receipt) == ASTRA_RT_WRONG_THREAD);
        CHECK(astra_rt_request_park(b, &u) == ASTRA_RT_WRONG_THREAD);
        CHECK(astra_rt_park_status(b, 1, &p) == ASTRA_RT_WRONG_THREAD);
        CHECK(astra_rt_replace(b, 1, nullptr, nullptr, nullptr, nullptr, &u) == ASTRA_RT_WRONG_THREAD);
        CHECK(astra_rt_resume(b, 1) == ASTRA_RT_WRONG_THREAD);
        CHECK(astra_rt_destroy(b) == ASTRA_RT_WRONG_THREAD);
    });
    wrong.join();
    const auto fresh = view(b);
    CHECK(!fresh.active_leases && !fresh.released && !fresh.callbacks && !fresh.park_ticket);
    CHECK(astra_rt_destroy(b) == ASTRA_RT_OK); // never handed to a device
}

void bad_frame_count() {
    FakeProcessor fake(0.25f);
    const auto spec = timeline(64, 120.0, 0);
    auto* b = create(&spec, nullptr, &fake);
    FakeOwner owner(b);
    std::array<float, 2> sentinel{123.0f, 456.0f};
#if ULONG_MAX > UINT32_MAX
    const auto too_many = static_cast<unsigned long>(UINT64_C(4294967297));
#else
    const unsigned long too_many = ASTRA_RT_MAX_CALLBACK_FRAMES + 1ul;
#endif
    owner.pump(too_many, sentinel.data());
    CHECK(owner.last_result() == 2);
    CHECK(sentinel[0] == 123.0f && sentinel[1] == 456.0f);
    const auto s = view(b);
    CHECK(s.bad_callback && s.fault == ASTRA_RT_FAULT_CALLBACK_BUFFER && !s.output_frames);
    CHECK(s.callbacks == 1 && !fake.music_calls);
    owner.request_retire();
    owner.join();
    CHECK(astra_rt_destroy(b) == ASTRA_RT_OK);
}
}

#include "live_test.inc"
#include "click_test.inc"
#include "pcm_hook_test.inc"
#include "transport_test.inc"

int main() {
    declick_sample_contract();
    declick_live_buses();
    live_timeline_and_audition();
    live_master_mix();
    live_click_same_callback();
    live_pcm_grant_and_bus();
    dispatch_and_replace();
    blocked_retirement();
    counted_leases();
    emergency_park();
    invalid_and_wrong_thread();
    bad_frame_count();
    std::puts("bridge tests passed");
}
