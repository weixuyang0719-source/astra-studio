#include "bridge.h"
#include "kernel.hpp"
#include "live.hpp"
#include <cstdio>
#include "../astra_vst_host.h"
#include "../../audio_core/portaudio_host.h"

#include <array>
#include <atomic>
#include <cstddef>
#include <limits>
#include <memory>
#include <new>
#include <stdexcept>
#include <thread>
#include <type_traits>
#include <utility>
#include <vector>

namespace rt = astra::rt;

static_assert(std::is_same<AstraRtCallback, AstraPaCallback>::value);
static_assert(std::is_same<decltype(&astra_rt_render), AstraPaCallback>::value);
static_assert(std::is_same<AstraRtDeviceReference, AstraHostReference>::value);
static_assert(std::is_same<decltype(&astra_rt_device_retain), AstraHostReference>::value);
static_assert(std::is_same<decltype(&astra_rt_device_release), AstraHostReference>::value);
static_assert(std::is_same<AstraRtVstProcess, decltype(&astra_vst_process)>::value);
static_assert(sizeof(AvMidiEvent) == 8 && offsetof(AvMidiEvent, status) == 4);
static_assert(sizeof(int) == 4 && sizeof(float) == 4 && sizeof(double) == 8 &&
              std::numeric_limits<float>::is_iec559 && std::numeric_limits<double>::is_iec559);
static_assert(sizeof(AstraRtConfig) == 16);
static_assert(sizeof(AstraRtTimelineSpec) == 32 && offsetof(AstraRtTimelineSpec, duration) == 8 &&
              offsetof(AstraRtTimelineSpec, bpm) == 16);
static_assert(sizeof(AstraRtEvent) == 16 && offsetof(AstraRtEvent, status) == 8 &&
              offsetof(AstraRtEvent, reserved2) == 12);
static_assert(sizeof(AstraRtGuard) == 24);
static_assert(sizeof(AstraRtCommand) == 56 && offsetof(AstraRtCommand, guard) == 16 &&
              offsetof(AstraRtCommand, a) == 40);
static_assert(sizeof(AstraRtSubmission) == 32 && offsetof(AstraRtSubmission, id) == 16);
static_assert(sizeof(AstraRtReceipt) == 56 && offsetof(AstraRtReceipt, guard) == 24 &&
              offsetof(AstraRtReceipt, position) == 48);
static_assert(sizeof(AstraRtSnapshot) == 128 && offsetof(AstraRtSnapshot, guard) == 8 &&
              offsetof(AstraRtSnapshot, callbacks) == 56 && offsetof(AstraRtSnapshot, park_ticket) == 80 &&
              offsetof(AstraRtSnapshot, active_leases) == 88 && offsetof(AstraRtSnapshot, bad_callback) == 124);
static_assert(ASTRA_RT_MAX_EVENTS == rt::kMaxTimelineEvents &&
              ASTRA_RT_MAX_PLUGIN_FRAMES == rt::kMaxPluginFrames &&
              ASTRA_RT_MAX_CALLBACK_FRAMES == rt::kMaxCallbackFrames &&
              ASTRA_RT_MAX_RAMP_FRAMES == rt::kMaxRampFrames &&
              ASTRA_RT_COMMAND_CAPACITY == rt::kCommandCapacity && ASTRA_RT_ANY_LOCATE == rt::kAnyLocate);
static_assert(std::atomic<uint64_t>::is_always_lock_free && std::atomic<uint32_t>::is_always_lock_free);

// The public ABI numbers are fixed. Check every direct enum conversion here.
#define ASTRA_RT_MATCH(K, V) static_assert(static_cast<uint32_t>(rt::K) == (V))
ASTRA_RT_MATCH(Op::play, ASTRA_RT_PLAY);
ASTRA_RT_MATCH(Op::pause, ASTRA_RT_PAUSE);
ASTRA_RT_MATCH(Op::seek, ASTRA_RT_SEEK);
ASTRA_RT_MATCH(Op::stop, ASTRA_RT_STOP);
ASTRA_RT_MATCH(Op::loop_on, ASTRA_RT_LOOP_ON);
ASTRA_RT_MATCH(Op::loop_off, ASTRA_RT_LOOP_OFF);
ASTRA_RT_MATCH(SubmitCode::queued, ASTRA_RT_SUBMIT_QUEUED);
ASTRA_RT_MATCH(SubmitCode::bad_command, ASTRA_RT_SUBMIT_BAD_COMMAND);
ASTRA_RT_MATCH(SubmitCode::parked, ASTRA_RT_SUBMIT_PARKED);
ASTRA_RT_MATCH(SubmitCode::stale, ASTRA_RT_SUBMIT_STALE);
ASTRA_RT_MATCH(SubmitCode::full, ASTRA_RT_SUBMIT_FULL);
ASTRA_RT_MATCH(SubmitCode::exhausted, ASTRA_RT_SUBMIT_EXHAUSTED);
ASTRA_RT_MATCH(Result::applied, ASTRA_RT_APPLIED);
ASTRA_RT_MATCH(Result::canceled, ASTRA_RT_CANCELED);
ASTRA_RT_MATCH(Result::stale, ASTRA_RT_STALE_COMMAND);
ASTRA_RT_MATCH(Result::range, ASTRA_RT_RANGE);
ASTRA_RT_MATCH(Result::no_binding, ASTRA_RT_NO_BINDING);
ASTRA_RT_MATCH(Result::faulted, ASTRA_RT_FAULTED);
ASTRA_RT_MATCH(Fault::none, ASTRA_RT_FAULT_NONE);
ASTRA_RT_MATCH(Fault::callback_buffer, ASTRA_RT_FAULT_CALLBACK_BUFFER);
ASTRA_RT_MATCH(Fault::event_capacity, ASTRA_RT_FAULT_EVENT_CAPACITY);
ASTRA_RT_MATCH(Fault::voice_capacity, ASTRA_RT_FAULT_VOICE_CAPACITY);
ASTRA_RT_MATCH(Fault::work_budget, ASTRA_RT_FAULT_WORK_BUDGET);
ASTRA_RT_MATCH(Fault::processor, ASTRA_RT_FAULT_PROCESSOR);
ASTRA_RT_MATCH(Fault::reset, ASTRA_RT_FAULT_RESET);
#undef ASTRA_RT_MATCH

namespace {
// 0 = never retained; 1 = last lease returned; 2..MAX-1 = lease count + 1.
// MAX is a fail-closed saturation state, never decremented.
constexpr uint64_t kLeaseOverflow = UINT64_MAX;

struct Adapter {
    void* plugin;
    AstraRtVstProcess process;
    std::array<AvMidiEvent, rt::kMidiCapacity> midi;
};

int32_t dispatch_vst(void* user, float* out, uint32_t frames, const rt::MidiEvent* events,
                     uint32_t count, rt::Sample position, double bpm, uint32_t playing) noexcept {
    auto& a = *static_cast<Adapter*>(user);
    if (!a.process || count > a.midi.size() || (count && !events))
        return ASTRA_RT_PROCESS_BAD_DISPATCH;
    for (uint32_t i = 0; i < count; ++i) {
        const auto& m = events[i];
        a.midi[i] = {m.sample_offset, m.status, m.data1, m.data2, 0};
    }
    try {
        return a.process(a.plugin, out, frames, count ? a.midi.data() : nullptr,
                         count, position, bpm, playing);
    } catch (...) {
        return ASTRA_RT_PROCESS_EXCEPTION;
    }
}

rt::Binding binding(const rt::Timeline* timeline, Adapter& adapter, uint32_t max_frames) noexcept {
    if (!timeline) return {};
    return {timeline, &adapter, &dispatch_vst, max_frames};
}

template<class T> bool valid_record(const T* p) noexcept {
    return p && p->size == sizeof(T) && p->version == ASTRA_RT_ABI_VERSION;
}
template<class T> T record() noexcept {
    T p{};
    p.size = static_cast<uint32_t>(sizeof(T));
    p.version = ASTRA_RT_ABI_VERSION;
    return p;
}

bool valid_source(const AstraRtTimelineSpec* spec, const AstraRtEvent* events,
                  void* plugin, AstraRtVstProcess process) noexcept {
    if (!spec) return !events && !plugin && !process;
    return valid_record(spec) && plugin && process &&
           spec->event_count <= ASTRA_RT_MAX_EVENTS && (!spec->event_count || events) &&
           spec->max_frames && spec->max_frames <= ASTRA_RT_MAX_PLUGIN_FRAMES;
}

std::unique_ptr<const rt::Timeline> prepare(const AstraRtTimelineSpec* spec,
                                            const AstraRtEvent* events) {
    if (!spec) return {};
    std::vector<rt::Event> copied;
    copied.reserve(spec->event_count);
    for (uint32_t i = 0; i < spec->event_count; ++i) {
        const auto& e = events[i];
        if (e.reserved || e.reserved2) throw std::invalid_argument("reserved event fields");
        copied.push_back({e.sample, e.status, e.data1, e.data2});
    }
    return rt::Timeline::prepare(spec->duration, spec->bpm, copied);
}
}

namespace {
// Leave all old ABI-only clients on the creating thread; handoff is opt-in.
enum class LiveDispatchPhase : uint32_t { legacy, armed, binding, bound, released };
std::atomic<uint64_t> next_live_dispatch_epoch{1};
uint64_t new_dispatch_epoch() noexcept {
    auto n=next_live_dispatch_epoch.load(std::memory_order_relaxed);
    while(n!=UINT64_MAX) {
        if(next_live_dispatch_epoch.compare_exchange_weak(n,n+1,std::memory_order_relaxed)) return n;
    }
    return 0;
}
}
struct AstraRtBridge final {
    const std::thread::id owner;
    std::atomic<LiveDispatchPhase> live_dispatch_phase{LiveDispatchPhase::legacy};
    std::atomic<uint64_t> live_dispatch_epoch{0}; // new token on every borrow
    std::thread::id live_dispatch_owner; // written before bound publication, immutable thereafter
    std::unique_ptr<const rt::Timeline> timeline;
    Adapter adapter;
    std::unique_ptr<rt::Live> live;
    rt::Kernel kernel;
    std::atomic<uint64_t> underruns{0};
    uint64_t park_ticket = 0; // UI only, includes parks initiated by submit()
    std::atomic<uint64_t> leases{0};
    std::atomic<uint32_t> bad_callback{0};

    AstraRtBridge(std::unique_ptr<const rt::Timeline> prepared, void* plugin,
                  AstraRtVstProcess process, uint32_t max_frames, uint32_t ramp)
        : owner(std::this_thread::get_id()), timeline(std::move(prepared)),
          adapter{plugin, process, {}}, kernel(binding(timeline.get(), adapter, max_frames), ramp) {}

    AstraRtBridge(const AstraLiveConfig& config, void* plugin, AstraLiveProcess process)
        : owner(std::this_thread::get_id()), adapter{},
          live(std::make_unique<rt::Live>(config, plugin, process)), kernel({}, 0, live.get()) {}
};

namespace {
int32_t control(AstraRtBridge* b) noexcept {
    if (!b) return ASTRA_RT_INVALID;
    return b->owner == std::this_thread::get_id() ? ASTRA_RT_OK : ASTRA_RT_WRONG_THREAD;
}
int32_t live_dispatch_control(AstraRtBridge* b, bool final_view=false) noexcept {
    if (!b || !b->live) return ASTRA_RT_INVALID;
    const auto phase=b->live_dispatch_phase.load(std::memory_order_acquire);
    if (phase == LiveDispatchPhase::legacy || (final_view && phase == LiveDispatchPhase::released))
        return control(b);
    if (phase == LiveDispatchPhase::bound && b->live_dispatch_owner == std::this_thread::get_id())
        return ASTRA_RT_OK;
    return ASTRA_RT_WRONG_THREAD;
}
bool live_dispatch_outstanding(const AstraRtBridge& b) noexcept {
    const auto phase=b.live_dispatch_phase.load(std::memory_order_acquire);
    return phase != LiveDispatchPhase::legacy && phase != LiveDispatchPhase::released;
}
uint32_t park_state(const AstraRtBridge& b, uint64_t ticket) noexcept {
    if (ticket != b.park_ticket) return ASTRA_RT_PARK_STALE;
    return b.kernel.quiesced(ticket) ? ASTRA_RT_PARK_ACK : ASTRA_RT_PARK_PENDING;
}
int32_t require_grant(const AstraRtBridge& b, uint64_t ticket) noexcept {
    if (!(ticket & 1u)) return ASTRA_RT_INVALID;
    if (ticket != b.park_ticket) return ASTRA_RT_STALE_TICKET;
    return b.kernel.quiesced(ticket) ? ASTRA_RT_OK : ASTRA_RT_PENDING;
}
}

extern "C" {
uint32_t ASTRA_RT_CALL astra_rt_abi_version(void) { return ASTRA_RT_ABI_VERSION; }

int32_t ASTRA_RT_CALL astra_rt_create(const AstraRtConfig* config,
    const AstraRtTimelineSpec* spec, const AstraRtEvent* events, void* plugin,
    AstraRtVstProcess process, AstraRtBridge** out) {
    if (!out || !valid_record(config) || config->reserved ||
        config->ramp_frames > ASTRA_RT_MAX_RAMP_FRAMES || !valid_source(spec, events, plugin, process))
        return ASTRA_RT_INVALID;
    try {
        auto prepared = prepare(spec, events);
        *out = new AstraRtBridge(std::move(prepared), plugin, process, spec ? spec->max_frames : 0u,
                                 config->ramp_frames);
        return ASTRA_RT_OK;
    } catch (const std::invalid_argument&) { return ASTRA_RT_INVALID; }
      catch (const std::bad_alloc&) { return ASTRA_RT_NO_MEMORY; }
      catch (...) { return ASTRA_RT_INTERNAL; }
}

int32_t ASTRA_RT_CALL astra_rt_destroy(AstraRtBridge* b) {
    if (const auto rc = control(b); rc != ASTRA_RT_OK) return rc;
    // No new attachment may race this call. The acquire pairs with the final
    // release, whose successful CAS is that hook's last access to *b.
    if (live_dispatch_outstanding(*b) || b->leases.load(std::memory_order_acquire) > 1) return ASTRA_RT_BUSY;
    delete b;
    return ASTRA_RT_OK;
}

int32_t ASTRA_RT_CALL astra_rt_snapshot(AstraRtBridge* b, AstraRtSnapshot* out) {
    if (const auto rc = control(b); rc != ASTRA_RT_OK) return rc;
    if (live_dispatch_outstanding(*b)) return ASTRA_RT_WRONG_THREAD;
    if (!valid_record(out)) return ASTRA_RT_INVALID;
    const auto s = b->kernel.snapshot();
    const auto leases = b->leases.load(std::memory_order_acquire);
    auto r = record<AstraRtSnapshot>();
    r.guard = {s.guard.generation, s.guard.phase, s.guard.locate};
    r.render_position = s.render_position;
    r.loop_begin = s.loop_begin;
    r.loop_end = s.loop_end;
    r.callbacks = s.callbacks;
    r.output_frames = s.output_frames;
    r.music_frames = s.music_frames;
    r.park_ticket = b->park_ticket;
    r.park_state = r.park_ticket ? park_state(*b, r.park_ticket) : uint32_t{ASTRA_RT_PARK_NONE};
    r.active_leases = leases == kLeaseOverflow ? kLeaseOverflow : (leases > 1 ? leases - 1 : 0);
    r.released = leases == 1;
    r.fault = static_cast<uint32_t>(s.fault);
    r.processor_code = s.processor_code;
    r.bound = s.bound;
    r.playing = s.playing;
    r.loop = s.loop;
    r.bad_callback = b->bad_callback.load(std::memory_order_relaxed);
    *out = r;
    return ASTRA_RT_OK;
}

int32_t ASTRA_RT_CALL astra_rt_submit(AstraRtBridge* b, const AstraRtCommand* command,
                                      AstraRtSubmission* out) {
    if (const auto rc = control(b); rc != ASTRA_RT_OK) return rc;
    if (b->live || !valid_record(command) || command->reserved || !valid_record(out)) return ASTRA_RT_INVALID;
    auto r = record<AstraRtSubmission>();
    r.code = ASTRA_RT_SUBMIT_BAD_COMMAND;
    if (command->op <= ASTRA_RT_LOOP_OFF) {
        const auto& g = command->guard;
        const auto s = b->kernel.submit(static_cast<rt::Op>(command->op),
                                        {g.generation, g.phase, g.locate}, command->a, command->b);
        r.code = static_cast<uint32_t>(s.code);
        r.id = s.id;
        r.park_ticket = s.park_ticket;
        if (s.park_ticket) b->park_ticket = s.park_ticket;
    }
    *out = r;
    return ASTRA_RT_OK;
}

int32_t ASTRA_RT_CALL astra_rt_poll_receipt(AstraRtBridge* b, AstraRtReceipt* out) {
    if (const auto rc = control(b); rc != ASTRA_RT_OK) return rc;
    if (!valid_record(out)) return ASTRA_RT_INVALID;
    rt::Receipt receipt{};
    if (!b->kernel.poll_receipt(receipt)) return ASTRA_RT_EMPTY;
    auto r = record<AstraRtReceipt>();
    r.id = receipt.id;
    r.result = static_cast<uint32_t>(receipt.result);
    r.guard = {receipt.guard.generation, receipt.guard.phase, receipt.guard.locate};
    r.position = receipt.position;
    *out = r;
    return ASTRA_RT_OK;
}

int32_t ASTRA_RT_CALL astra_rt_request_park(AstraRtBridge* b, uint64_t* out_ticket) {
    if (const auto rc = control(b); rc != ASTRA_RT_OK) return rc;
    if (!out_ticket) return ASTRA_RT_INVALID;
    if (b->park_ticket) return ASTRA_RT_BUSY;
    const auto ticket = b->kernel.request_quiesce();
    if (!ticket) return ASTRA_RT_BUSY;
    b->park_ticket = ticket;
    *out_ticket = ticket;
    return ASTRA_RT_OK;
}

int32_t ASTRA_RT_CALL astra_rt_park_status(AstraRtBridge* b, uint64_t ticket, uint32_t* out_state) {
    if (const auto rc = control(b); rc != ASTRA_RT_OK) return rc;
    if (!(ticket & 1u) || !out_state) return ASTRA_RT_INVALID;
    *out_state = park_state(*b, ticket);
    return ASTRA_RT_OK;
}

int32_t ASTRA_RT_CALL astra_rt_replace(AstraRtBridge* b, uint64_t ticket,
    const AstraRtTimelineSpec* spec, const AstraRtEvent* events, void* plugin,
    AstraRtVstProcess process, uint64_t* out_generation) {
    if (const auto rc = control(b); rc != ASTRA_RT_OK) return rc;
    if (b->live || !out_generation) return ASTRA_RT_INVALID;
    if (const auto rc = require_grant(*b, ticket); rc != ASTRA_RT_OK) return rc;
    if (!valid_source(spec, events, plugin, process)) return ASTRA_RT_INVALID;
    try {
        auto prepared = prepare(spec, events);
        const auto gen = b->kernel.replace_binding(ticket,
            binding(prepared.get(), b->adapter, spec ? spec->max_frames : 0u));
        if (!gen) return ASTRA_RT_EXHAUSTED; // grant and binding already checked
        // RT has dropped its binding, and cannot read staging until resume.
        b->adapter.plugin = plugin;
        b->adapter.process = process;
        b->timeline = std::move(prepared); // delete old timeline here on UI, not RT
        *out_generation = gen;
        return ASTRA_RT_OK;
    } catch (const std::invalid_argument&) { return ASTRA_RT_INVALID; }
      catch (const std::bad_alloc&) { return ASTRA_RT_NO_MEMORY; }
      catch (...) { return ASTRA_RT_INTERNAL; }
}

int32_t ASTRA_RT_CALL astra_rt_resume(AstraRtBridge* b, uint64_t ticket) {
    if (const auto rc = control(b); rc != ASTRA_RT_OK) return rc;
    if (const auto rc = require_grant(*b, ticket); rc != ASTRA_RT_OK) return rc;
    if (!b->kernel.resume(ticket)) return ASTRA_RT_EXHAUSTED;
    b->park_ticket = 0;
    return ASTRA_RT_OK;
}

void ASTRA_RT_CALL astra_rt_device_retain(void* user) {
    auto* b = static_cast<AstraRtBridge*>(user);
    if (!b) return;
    auto n = b->leases.load(std::memory_order_relaxed);
    while (n != kLeaseOverflow) {
        if (b->leases.compare_exchange_weak(n, n == 0 ? uint64_t{2} : n + 1,
                std::memory_order_acq_rel, std::memory_order_relaxed)) return;
    }
}

void ASTRA_RT_CALL astra_rt_device_release(void* user) {
    auto* b = static_cast<AstraRtBridge*>(user);
    if (!b) return;
    auto n = b->leases.load(std::memory_order_relaxed);
    while (n > 1 && n != kLeaseOverflow) {
        // Successful CAS is the last bridge access, including on the final
        // release. The UI may delete *b before this function returns.
        if (b->leases.compare_exchange_weak(n, n - 1,
                std::memory_order_acq_rel, std::memory_order_relaxed)) return;
    }
}

AstraRtCallback ASTRA_RT_CALL astra_rt_get_callback(void) { return &astra_rt_render; }

int ASTRA_RT_CALL astra_rt_render(const void*, void* output, unsigned long frames,
                                 const void*, unsigned long flags, void* user) {
    auto* b = static_cast<AstraRtBridge*>(user);
    // This is a consistency check on a LIVE handle, not protection from an
    // owner calling with a dangling pointer after releasing its lease.
    if (!b || b->leases.load(std::memory_order_acquire) <= 1) return 2; // paAbort: no valid device lease
    if (b->live && (flags & 0x04ul)) { // PortAudio paOutputUnderflow, one count per flagged callback
        const auto n = b->underruns.load(std::memory_order_relaxed);
        if (n != UINT64_MAX) b->underruns.store(n + 1, std::memory_order_relaxed);
    }
    if (frames > ASTRA_RT_MAX_CALLBACK_FRAMES || (b->live && frames > b->live->max_frames())) {
        b->bad_callback.store(1, std::memory_order_relaxed);
        // Reject before narrowing (unsigned long is 64-bit on LP64). The
        // kernel fills a non-null output even for an oversized frame count.
        // Pass no buffer so this real callback can service control but not
        // write an unbounded amount of memory.
        b->kernel.render(nullptr, rt::kMaxCallbackFrames + 1u);
        return 2; // paAbort: do not submit an unwritten buffer as paContinue
    } else {
        if (!output && frames) {
            b->bad_callback.store(1, std::memory_order_relaxed);
            b->kernel.render(nullptr, static_cast<uint32_t>(frames));
            return 2; // paAbort
        }
        b->kernel.render(static_cast<float*>(output), static_cast<uint32_t>(frames));
    }
    return 0; // paContinue
}
}

namespace {
thread_local char live_error[240]{};
template<class T> bool valid_live(const T* p) noexcept {
    return p && p->size == sizeof(T) && p->version == ASTRA_LIVE_VERSION;
}
template<class F> int32_t live_control(F action) noexcept {
    live_error[0] = 0;
    try { return action(); }
    catch (const std::invalid_argument& e) {
        std::snprintf(live_error, sizeof live_error, "%s", e.what());
        return ASTRA_RT_INVALID;
    } catch (const std::bad_alloc&) { return ASTRA_RT_NO_MEMORY; }
      catch (...) { return ASTRA_RT_INTERNAL; }
}
}

extern "C" {
uint32_t ASTRA_RT_CALL astra_rt_live_version(void) { return ASTRA_LIVE_VERSION; }
const char* ASTRA_RT_CALL astra_rt_live_error(void) { return live_error; }
int32_t ASTRA_RT_CALL astra_rt_live_create(const AstraLiveConfig* c, void* plugin,
                                           AstraLiveProcess process, AstraRtBridge** out) {
    if (!valid_live(c) || !out || c->reserved || !plugin || !process) return ASTRA_RT_INVALID;
    return live_control([&] { *out = new AstraRtBridge(*c, plugin, process); return ASTRA_RT_OK; });
}
int32_t ASTRA_RT_CALL astra_rt_live_dispatch_prepare(AstraRtBridge* b, uint64_t* out_epoch) {
    if (auto rc = control(b); rc != ASTRA_RT_OK) return rc;
    if (!b->live || !out_epoch) return ASTRA_RT_INVALID;
    if (b->live_dispatch_phase.load(std::memory_order_acquire)!=LiveDispatchPhase::legacy) return ASTRA_RT_BUSY;
    const auto epoch=new_dispatch_epoch();
    if (!epoch) return ASTRA_RT_EXHAUSTED;
    b->live_dispatch_epoch.store(epoch,std::memory_order_relaxed);
    *out_epoch=epoch;
    b->live_dispatch_phase.store(LiveDispatchPhase::armed,std::memory_order_release);
    return ASTRA_RT_OK;
}
int32_t ASTRA_RT_CALL astra_rt_live_dispatch_claim(AstraRtBridge* b, uint64_t epoch) {
    if (!b || !b->live || !epoch) return ASTRA_RT_INVALID;
    if (b->owner==std::this_thread::get_id()) return ASTRA_RT_WRONG_THREAD;
    auto phase=b->live_dispatch_phase.load(std::memory_order_acquire);
    if (phase==LiveDispatchPhase::legacy || epoch!=b->live_dispatch_epoch) return ASTRA_LIVE_STALE;
    if (phase!=LiveDispatchPhase::armed ||
        !b->live_dispatch_phase.compare_exchange_strong(phase,LiveDispatchPhase::binding,std::memory_order_acq_rel))
        return ASTRA_RT_BUSY;
    b->live_dispatch_owner=std::this_thread::get_id();
    b->live_dispatch_phase.store(LiveDispatchPhase::bound,std::memory_order_release);
    return ASTRA_RT_OK;
}
int32_t ASTRA_RT_CALL astra_rt_live_dispatch_release(AstraRtBridge* b, uint64_t epoch) {
    if (auto rc=live_dispatch_control(b); rc!=ASTRA_RT_OK) return rc;
    auto phase=b->live_dispatch_phase.load(std::memory_order_acquire);
    if (phase!=LiveDispatchPhase::bound || !epoch || epoch!=b->live_dispatch_epoch) return ASTRA_LIVE_STALE;
    // Last use of *b by the dispatcher. Once visible, destroy may proceed.
    b->live_dispatch_phase.store(LiveDispatchPhase::released,std::memory_order_release);
    return ASTRA_RT_OK;
}
int32_t ASTRA_RT_CALL astra_rt_live_dispatch_cancel_unclaimed(AstraRtBridge* b, uint64_t epoch) {
    if (auto rc=control(b); rc!=ASTRA_RT_OK) return rc;
    if (!b->live || !epoch) return ASTRA_RT_INVALID;
    auto phase=b->live_dispatch_phase.load(std::memory_order_acquire);
    if (phase==LiveDispatchPhase::legacy || epoch!=b->live_dispatch_epoch) return ASTRA_LIVE_STALE;
    if (phase!=LiveDispatchPhase::armed ||
        !b->live_dispatch_phase.compare_exchange_strong(phase,LiveDispatchPhase::released,std::memory_order_acq_rel))
        return ASTRA_RT_BUSY;
    return ASTRA_RT_OK;
}
int32_t ASTRA_RT_CALL astra_rt_live_dispatch_reclaim(AstraRtBridge* b, uint64_t epoch) {
    if (auto rc = control(b); rc != ASTRA_RT_OK) return rc;
    if (!b->live || !epoch || epoch != b->live_dispatch_epoch) return ASTRA_LIVE_STALE;
    auto phase=LiveDispatchPhase::released;
    if(!b->live_dispatch_phase.compare_exchange_strong(phase, LiveDispatchPhase::legacy, std::memory_order_acq_rel))
        return ASTRA_RT_BUSY;
    return ASTRA_RT_OK;
}
int32_t ASTRA_RT_CALL astra_rt_live_publish(AstraRtBridge* b, const AstraLiveSpec* s,
    const AstraRtEvent* events, const AstraLiveClock* clock, uint64_t* superseded, uint32_t* unsupported) {
    if (auto rc = live_dispatch_control(b); rc != ASTRA_RT_OK) return rc;
    if (!b->live || !valid_live(s) || !superseded || !unsupported) return ASTRA_RT_INVALID;
    return live_control([&] { return b->live->publish(*s, events, clock, *superseded, *unsupported); });
}
uint32_t ASTRA_RT_CALL astra_rt_live_score_click_gates(void) { return 1; }
uint32_t ASTRA_RT_CALL astra_rt_live_score_count_in(void) { return 1; }
uint32_t ASTRA_RT_CALL astra_rt_live_score_rate(void) { return 1; }
int32_t ASTRA_RT_CALL astra_rt_live_publish_clicks(AstraRtBridge* b,const AstraLiveSpec* s,
    const AstraRtEvent* events,const AstraLiveClock* clock,const AstraLiveClick* clicks,uint32_t click_count,uint64_t* superseded,uint32_t* unsupported) {
    if(auto rc = live_dispatch_control(b);rc!=ASTRA_RT_OK)return rc;
    if(!b->live||!valid_live(s)||!superseded||!unsupported)return ASTRA_RT_INVALID;
    return live_control([&]{return b->live->publish(*s,events,clock,*superseded,*unsupported,clicks,click_count);});
}
int32_t ASTRA_RT_CALL astra_rt_live_bind_pcm(AstraRtBridge* b,uint64_t ticket,const AstraLivePcmSource* source) {
    if(auto rc=control(b);rc!=ASTRA_RT_OK)return rc;
    if(!b->live)return ASTRA_RT_INVALID;
    if(auto rc=require_grant(*b,ticket);rc!=ASTRA_RT_OK)return rc;
    return live_control([&]{b->live->bind_pcm(source);return ASTRA_RT_OK;});
}
int32_t ASTRA_RT_CALL astra_rt_live_submit(AstraRtBridge* b, const AstraLiveCommand* c) {
    if (auto rc = live_dispatch_control(b); rc != ASTRA_RT_OK) return rc;
    if (!b->live || !valid_live(c)) return ASTRA_RT_INVALID;
    return b->live->submit(*c);
}
int32_t ASTRA_RT_CALL astra_rt_live_poll(AstraRtBridge* b, AstraLiveReceipt* out) {
    if (auto rc = live_dispatch_control(b); rc != ASTRA_RT_OK) return rc;
    if (!b->live || !valid_live(out)) return ASTRA_RT_INVALID;
    return b->live->poll(*out) ? ASTRA_RT_OK : ASTRA_RT_EMPTY;
}
static int32_t write_live_snapshot(AstraRtBridge* b, AstraLiveSnapshot* out, const rt::Snapshot& s) {
    auto fault = static_cast<uint32_t>(s.fault);
    if (b->bad_callback.load(std::memory_order_relaxed)) fault = ASTRA_RT_FAULT_CALLBACK_BUFFER;
    *out = {sizeof(*out), ASTRA_LIVE_VERSION, s.live_state, s.loop, s.live_timeline,
            fault, s.processor_code, static_cast<uint32_t>(s.guard.phase & 1u),
            s.live_epoch, s.live_sequence, s.live_intent, s.callbacks, s.output_frames,
            b->underruns.load(std::memory_order_relaxed), s.process_calls, s.render_position, s.live_gain, s.live_effective_gain, s.live_muted, s.live_subframe, s.live_midi_gain, s.live_audio_gain, s.live_click_gain, static_cast<uint32_t>(s.live_metronome),s.live_rate};
    return ASTRA_RT_OK;
}
int32_t ASTRA_RT_CALL astra_rt_live_snapshot(AstraRtBridge* b, AstraLiveSnapshot* out) {
    if (auto rc = live_dispatch_control(b,true); rc != ASTRA_RT_OK) return rc;
    if (!b->live || !valid_live(out)) return ASTRA_RT_INVALID;
    return write_live_snapshot(b,out,b->kernel.snapshot());
}
int32_t ASTRA_RT_CALL astra_rt_render_snapshot(AstraRtBridge* b, AstraLiveSnapshot* out) {
    if (!b || !b->live || !valid_live(out)) return ASTRA_RT_INVALID;
    return write_live_snapshot(b,out,b->kernel.completed_render());
}
}
