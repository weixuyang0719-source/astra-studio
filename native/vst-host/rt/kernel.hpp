#pragma once

#include <array>
#include <atomic>
#include <cstddef>
#include <cstdint>
#include <limits>
#include <memory>
#include <vector>

namespace astra::rt {

using Sample = std::int64_t;
inline constexpr std::size_t kMaxTimelineEvents = 1u << 20;
inline constexpr std::uint32_t kMidiCapacity = 4096;
inline constexpr std::uint32_t kVoiceCapacity = kMidiCapacity - 16 * 5;
inline constexpr std::uint32_t kCommandCapacity = 32;
inline constexpr std::uint32_t kMaxPluginFrames = 32768;
inline constexpr std::uint32_t kMaxCallbackFrames = 32768;
inline constexpr std::uint32_t kMaxMusicCalls = 64;
inline constexpr std::uint32_t kMaxMusicEvents = 8192;
inline constexpr std::uint32_t kMaxRampFrames = 64;
inline constexpr std::uint64_t kAnyLocate = (std::numeric_limits<std::uint64_t>::max)();

struct Event {
    Sample sample;
    std::uint8_t status, data1, data2;
};

class Timeline final {
public:
    // Control side only. Copies input, validates it, and stably sorts by sample.
    // Duration is exclusive. Throws invalid_argument for invalid input.
    static std::unique_ptr<const Timeline> prepare(Sample duration, double bpm,
                                                    const std::vector<Event>& events);
    ~Timeline() = default;
    Timeline(const Timeline&) = delete;
    Timeline& operator=(const Timeline&) = delete;

private:
    friend class Kernel;
    Timeline(Sample duration, double bpm, std::vector<Event>&& sorted) noexcept;
    const Sample duration_;
    const double bpm_;
    const std::vector<Event> events_;
};

struct MidiEvent {
    std::uint32_t sample_offset;
    std::uint8_t status, data1, data2, reserved;
};

// Native, synchronous, no-throw. 0 means all frames were written as finite
// stereo interleaved f32. Event/output pointers may not be retained. Events
// can be null for count == 0. See README for the ABI v1 adapter.
using ProcessFn = std::int32_t (*)(void*, float*, std::uint32_t, const MidiEvent*,
                                   std::uint32_t, Sample, double, std::uint32_t) noexcept;
struct Binding {
    const Timeline* timeline = nullptr;
    void* user = nullptr;
    ProcessFn process = nullptr;
    std::uint32_t max_frames = 0;
};

struct Guard {
    std::uint64_t generation = 0;
    std::uint64_t phase = 0;             // even: control enabled; odd: park ticket
    std::uint64_t locate = 0;            // exact stamp, or kAnyLocate on an intent
};

enum class Op { play, pause, seek, stop, loop_on, loop_off };
enum class SubmitCode { queued, bad_command, parked, stale, full, exhausted };
struct Submission {
    SubmitCode code;
    std::uint64_t id = 0;
    std::uint64_t park_ticket = 0;       // full/exhausted requests an emergency park
};

enum class Result { applied, canceled, stale, range, no_binding, faulted };
struct Receipt {
    std::uint64_t id = 0;
    Result result = Result::canceled;
    Guard guard{};
    Sample position = 0;               // immediately after evaluating the command
};

enum class Fault { none, callback_buffer, event_capacity, voice_capacity,
                   work_budget, processor, reset };
struct Snapshot {
    Guard guard{};
    Sample render_position = 0;        // project sample for the next musical frame
    Sample loop_begin = 0, loop_end = 0;
    std::uint64_t callbacks = 0;
    std::uint64_t output_frames = 0;   // written to callback buffers, including silence
    std::uint64_t music_frames = 0;    // successful musical process frames only
    Fault fault = Fault::none;
    std::int32_t processor_code = 0;
    bool bound = false;                // callable binding installed, not UI loaded status
    bool playing = false;
    bool loop = false;
    // Internal extension telemetry. Not part of the ABI 1 record.
    bool live_timeline = false;
    std::uint32_t live_state = 0;
    double live_gain = 0., live_effective_gain = 0.;
    bool live_muted = false, live_metronome = false;
    double live_midi_gain=1.,live_audio_gain=1.,live_click_gain=1.;
    std::uint64_t live_epoch = 0, live_sequence = 0, live_intent = 0, process_calls = 0;
};

// Optional fixed processor for the live extension. It shares this kernel's
// exact park/ack/resume and snapshot publication, not a second exclusion lock.
struct BoundaryProcessor {
    virtual void park(Snapshot&) noexcept = 0;
    virtual void render(Snapshot&, float*, std::uint32_t, bool usable) noexcept = 0;
protected:
    ~BoundaryProcessor() = default;
};

class Kernel final {
public:
    // Bindings are borrowed. Starts paused. The all-zero Binding is empty.
    explicit Kernel(Binding initial = {}, std::uint32_t ramp_frames = 16,
                    BoundaryProcessor* extension = nullptr);
    // Only after the callback lane is definitively retired. Does not close plugins.
    ~Kernel() = default;
    Kernel(const Kernel&) = delete;
    Kernel& operator=(const Kernel&) = delete;

    // Single control/UI thread for ALL these methods, including snapshot().
    // seek uses a; loop_on uses [a,b); all unused arguments must be zero.
    Submission submit(Op op, Guard guard, Sample a = 0, Sample b = 0) noexcept;
    bool poll_receipt(Receipt& out) noexcept;
    Snapshot snapshot() noexcept;
    std::uint64_t request_quiesce() noexcept; // 0 if a park is already held or pending
    bool quiesced(std::uint64_t ticket) const noexcept;
    // While holding the exact acknowledged ticket. Returns new generation, or 0.
    // Use {} to detach before freeing the previous binding. May also stay parked.
    std::uint64_t replace_binding(std::uint64_t ticket, Binding next) noexcept;
    bool resume(std::uint64_t ticket) noexcept; // releases exclusivity; does NOT Play

    // Sole callback lane, non-reentrant. For frames > 0, out must have room for
    // 2*frames floats. A zero-frame callback can service control, but renders no
    // musical frames. Never call this from UI to manufacture an acknowledgement.
    void render(float* out, std::uint32_t frames) noexcept;

private:
    static_assert(std::atomic<std::uint64_t>::is_always_lock_free &&
                  std::atomic<std::uint32_t>::is_always_lock_free,
                  "Kernel requires native lock-free word atomics");
    static_assert(kCommandCapacity && !(kCommandCapacity & (kCommandCapacity - 1)),
                  "Command ring size must be a power of two");

    struct Command {
        Op op = Op::pause;
        Guard guard{};
        Sample a = 0, b = 0;
        std::uint64_t id = 0;
    };
    struct Slot { Command command{}; Receipt receipt{}; };
    using Keys = std::array<std::uint16_t, 16 * 128>;

    static bool valid_binding(Binding binding) noexcept;
    static void update_key(Keys& keys, std::uint32_t& total, const MidiEvent& m) noexcept;
    void enter_phase(std::uint64_t phase) noexcept;
    Result apply(const Command& cmd) noexcept;
    bool trackable(std::uint32_t count, std::uint32_t starts) noexcept;
    void accept_midi(std::uint32_t count) noexcept;
    void forget_notes() noexcept;
    std::int32_t release_notes() noexcept;
    bool resolve() noexcept;
    void trip(Fault fault, std::int32_t code = 0, bool try_cleanup = false) noexcept;
    void arm_ramp() noexcept;
    void clear_ramp() noexcept;
    void smooth(float* out, std::uint32_t frames) noexcept;
    void render_output(float* out, std::uint32_t frames) noexcept;
    void publish_snapshot() noexcept;

    std::array<Slot, kCommandCapacity> slots_{};
    std::atomic<std::uint64_t> submitted_{0}, finished_{0};
    std::uint64_t ui_submitted_ = 0, ui_read_ = 0, rt_finished_ = 0;

    std::atomic<std::uint64_t> requested_phase_{0}, acked_phase_{0};
    std::uint64_t ui_phase_ = 0, staged_generation_ = 1;
    Binding staged_{}, rt_binding_{};
    BoundaryProcessor* const extension_;
    Snapshot rt_{};

    std::array<Snapshot, 3> views_{};
    std::atomic<std::uint32_t> middle_{1};
    std::uint32_t front_ = 0, back_ = 2;

    std::array<MidiEvent, kMidiCapacity> midi_{};
    Keys keys_{}, trial_{};
    std::uint32_t keyed_ = 0, channels_ = 0;
    std::uint32_t ramp_size_ = 0, ramp_left_ = 0;
    float origin_l_ = 0, origin_r_ = 0, last_l_ = 0, last_r_ = 0;
};

} // namespace astra::rt
