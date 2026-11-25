#include "kernel.hpp"

#include <algorithm>
#include <cmath>
#include <stdexcept>
#include <utility>

namespace astra::rt {
namespace {
constexpr std::uint32_t kDirty = 4;
constexpr std::uint32_t kIndexMask = 3;

bool parked(std::uint64_t phase) noexcept { return (phase & 1u) != 0; }
std::size_t slot_index(std::uint64_t zero_based) noexcept {
    return static_cast<std::size_t>(zero_based & (kCommandCapacity - 1));
}
void add_count(std::uint64_t& count, std::uint64_t n) noexcept {
    count += std::min(n, kAnyLocate - count);
}
bool note_start(std::uint8_t status, std::uint8_t velocity) noexcept {
    return (status & 0xf0u) == 0x90u && velocity != 0;
}
bool earlier(const Event& event, Sample p) noexcept { return event.sample < p; }
} // namespace

Timeline::Timeline(Sample duration, double bpm, std::vector<Event>&& sorted) noexcept
    : duration_(duration), bpm_(bpm), events_(std::move(sorted)) {}

std::unique_ptr<const Timeline> Timeline::prepare(Sample duration, double bpm,
                                                  const std::vector<Event>& input) {
    if (duration <= 0 || !std::isfinite(bpm) || bpm <= 0.0 || bpm > 1000.0 ||
        input.size() > kMaxTimelineEvents)
        throw std::invalid_argument("invalid timeline length, tempo, or size");
    std::vector<Event> sorted(input);
    for (auto& event : sorted) {
        const auto type = event.status & 0xf0u;
        if (event.sample < 0 || event.sample >= duration || event.status < 0x80u ||
            event.status >= 0xf0u || type == 0xc0u || event.data1 > 127 ||
            event.data2 > 127 || (type == 0xd0u && event.data2 != 0))
            throw std::invalid_argument("unsupported MIDI message or event out of range");
        if (type == 0x90u && event.data2 == 0)
            event.status = static_cast<std::uint8_t>(0x80u | (event.status & 0x0fu));
    }
    std::stable_sort(sorted.begin(), sorted.end(), [](const Event& a, const Event& b) {
        return a.sample < b.sample;
    });
    return std::unique_ptr<const Timeline>(new Timeline(duration, bpm, std::move(sorted)));
}

bool Kernel::valid_binding(Binding b) noexcept {
    if (!b.timeline) return !b.user && !b.process && b.max_frames == 0;
    return b.process && b.max_frames > 0 && b.max_frames <= kMaxPluginFrames;
}

Kernel::Kernel(Binding initial, std::uint32_t ramp_frames, BoundaryProcessor* extension)
    : staged_(initial), rt_binding_(initial), extension_(extension), ramp_size_(ramp_frames) {
    if (!valid_binding(initial) || (extension && initial.timeline) || ramp_frames > kMaxRampFrames)
        throw std::invalid_argument("invalid kernel binding or ramp length");
    rt_.guard.generation = staged_generation_;
    rt_.bound = extension || initial.timeline;
    views_[front_] = rt_;
}

Submission Kernel::submit(Op op, Guard guard, Sample a, Sample b) noexcept {
    switch (op) {
    case Op::seek:
        if (a < 0 || b != 0) return {SubmitCode::bad_command};
        break;
    case Op::loop_on:
        if (a < 0 || b <= a) return {SubmitCode::bad_command};
        break;
    case Op::play: case Op::pause: case Op::stop: case Op::loop_off:
        if (a != 0 || b != 0) return {SubmitCode::bad_command};
        break;
    default:
        return {SubmitCode::bad_command};
    }
    if (parked(ui_phase_)) return {SubmitCode::parked};
    if (guard.phase != ui_phase_ || guard.generation != staged_generation_)
        return {SubmitCode::stale};
    // Credit is returned only when UI has READ the receipt, not just when RT
    // executes the command. Each slot is reserved for both directions.
    if (ui_submitted_ - ui_read_ == kCommandCapacity)
        return {SubmitCode::full, 0, request_quiesce()};
    if (ui_submitted_ == kAnyLocate - 1)
        return {SubmitCode::exhausted, 0, request_quiesce()};
    const auto id = ui_submitted_ + 1;
    slots_[slot_index(ui_submitted_)].command = {op, guard, a, b, id};
    ui_submitted_ = id;
    submitted_.store(id, std::memory_order_release);
    return {SubmitCode::queued, id, 0};
}

bool Kernel::poll_receipt(Receipt& out) noexcept {
    if (ui_read_ == finished_.load(std::memory_order_acquire)) return false;
    out = slots_[slot_index(ui_read_)].receipt;
    ++ui_read_;
    return true;
}

Snapshot Kernel::snapshot() noexcept {
    // The callback cannot clear the dirty bit; between load and exchange it
    // can only publish a newer complete view. Each side owns its local slot.
    if (middle_.load(std::memory_order_acquire) & kDirty)
        front_ = middle_.exchange(front_, std::memory_order_acq_rel) & kIndexMask;
    return views_[front_];
}

void Kernel::publish_snapshot() noexcept {
    views_[back_] = rt_;
    back_ = middle_.exchange(back_ | kDirty, std::memory_order_acq_rel) & kIndexMask;
}

std::uint64_t Kernel::request_quiesce() noexcept {
    if (parked(ui_phase_)) return 0;
    ++ui_phase_;
    requested_phase_.store(ui_phase_, std::memory_order_release);
    return ui_phase_;
}

bool Kernel::quiesced(std::uint64_t ticket) const noexcept {
    return parked(ticket) && ticket == ui_phase_ &&
           acked_phase_.load(std::memory_order_acquire) == ticket;
}

std::uint64_t Kernel::replace_binding(std::uint64_t ticket, Binding next) noexcept {
    if (!quiesced(ticket) || !valid_binding(next) || staged_generation_ == kAnyLocate)
        return 0;
    staged_ = next;
    return ++staged_generation_;
}

bool Kernel::resume(std::uint64_t ticket) noexcept {
    if (!quiesced(ticket) || ticket == kAnyLocate) return false;
    ++ui_phase_;
    requested_phase_.store(ui_phase_, std::memory_order_release);
    return true;
}

void Kernel::enter_phase(std::uint64_t phase) noexcept {
    if (extension_) {
        if (parked(phase) && !parked(rt_.guard.phase)) extension_->park(rt_);
        rt_.bound = !parked(phase);
        rt_.guard.phase = phase;
        return;
    }
    if (parked(phase)) {
        if (!parked(rt_.guard.phase)) {
            resolve();  // best effort; a failure is visible but still permits exclusivity
            rt_binding_ = {};
        }
        rt_.bound = false;
        rt_.playing = false;
        clear_ramp();
    } else {
        // UI can only modify staging under a park grant. This acquire-observed
        // resume publishes the staging, and a new grant needs a new RT boundary.
        rt_binding_ = staged_;
        if (staged_generation_ != rt_.guard.generation) {
            rt_.guard.generation = staged_generation_;
            rt_.guard.locate = 0;
            rt_.render_position = 0;
            rt_.loop = false;
            rt_.loop_begin = rt_.loop_end = 0;
            rt_.fault = Fault::none;
            rt_.processor_code = 0;
        }
        rt_.bound = rt_binding_.timeline != nullptr;
    }
    rt_.guard.phase = phase;
}

Result Kernel::apply(const Command& c) noexcept {
    if (parked(rt_.guard.phase) || c.guard.phase != rt_.guard.phase)
        return Result::canceled;
    if (c.guard.generation != rt_.guard.generation ||
        (c.guard.locate != kAnyLocate && c.guard.locate != rt_.guard.locate))
        return Result::stale;
    if (!rt_.bound) return Result::no_binding;
    if (rt_.fault != Fault::none) return Result::faulted;

    const auto duration = rt_binding_.timeline->duration_;
    switch (c.op) {
    case Op::play:
        if (!rt_.playing) {
            if (rt_.render_position == duration) {
                rt_.render_position = rt_.loop ? rt_.loop_begin : 0;
                rt_.guard.locate = c.id;
            }
            rt_.playing = true;
            arm_ramp();
        }
        break;
    case Op::pause:
        if (rt_.playing) {
            if (!resolve()) return Result::faulted;
            rt_.playing = false;
            arm_ramp();
        }
        break;
    case Op::seek:
        if (c.a > duration || (rt_.loop && (c.a < rt_.loop_begin || c.a >= rt_.loop_end)))
            return Result::range;
        if (!resolve()) return Result::faulted;
        if (rt_.playing) arm_ramp();
        rt_.render_position = c.a;
        rt_.playing = rt_.playing && c.a < duration;
        rt_.guard.locate = c.id;
        break;
    case Op::stop:
        if (!resolve()) return Result::faulted;
        if (rt_.playing) arm_ramp();
        rt_.playing = false;
        rt_.render_position = rt_.loop ? rt_.loop_begin : 0;
        rt_.guard.locate = c.id;
        break;
    case Op::loop_on:
        if (c.b > duration) return Result::range;
        if (!resolve()) return Result::faulted;
        if (rt_.playing) arm_ramp();
        rt_.loop = true;
        rt_.loop_begin = c.a;
        rt_.loop_end = c.b;
        if (rt_.render_position < c.a || rt_.render_position >= c.b)
            rt_.render_position = c.a;
        rt_.guard.locate = c.id;
        break;
    case Op::loop_off:
        rt_.loop = false;
        rt_.loop_begin = rt_.loop_end = 0;
        rt_.guard.locate = c.id;
        break;
    }
    return Result::applied;
}

void Kernel::update_key(Keys& keys, std::uint32_t& total, const MidiEvent& m) noexcept {
    const auto type = m.status & 0xf0u;
    auto& count = keys[(m.status & 0x0fu) * 128u + m.data1];
    if (note_start(m.status, m.data2)) {
        ++count;
        ++total;
    } else if ((type == 0x80u || type == 0x90u) && count) {
        --count;
        --total;
    }
    // Do not trust channel-mode CCs to have cleared keys: the ABI adapter
    // can legally ignore an unmapped CC. Explicit offs are still required.
}

bool Kernel::trackable(std::uint32_t count, std::uint32_t starts) noexcept {
    if (starts <= kVoiceCapacity - keyed_) return true;
    // Near the cap, count the exact peak rather than rejecting off/on pairs.
    trial_ = keys_;
    auto total = keyed_;
    for (std::uint32_t i = 0; i < count; ++i) {
        update_key(trial_, total, midi_[i]);
        if (total > kVoiceCapacity) return false;
    }
    return true;
}

void Kernel::accept_midi(std::uint32_t count) noexcept {
    for (std::uint32_t i = 0; i < count; ++i) {
        const auto& m = midi_[i];
        channels_ |= 1u << (m.status & 0x0fu);
        update_key(keys_, keyed_, m);
    }
}

void Kernel::forget_notes() noexcept {
    keys_.fill(0);
    keyed_ = channels_ = 0;
}

std::int32_t Kernel::release_notes() noexcept {
    if (!channels_) return 0;
    std::uint32_t count = 0;
    for (std::uint32_t ch = 0; ch < 16; ++ch) {
        if (!(channels_ & (1u << ch))) continue;
        const auto cc = static_cast<std::uint8_t>(0xb0u | ch);
        const auto off = static_cast<std::uint8_t>(0x80u | ch);
        midi_[count++] = {0, cc, 64, 0, 0};
        midi_[count++] = {0, cc, 66, 0, 0};
        midi_[count++] = {0, cc, 69, 0, 0};
        for (std::uint32_t pitch = 0; pitch < 128; ++pitch) {
            for (auto n = keys_[ch * 128u + pitch]; n; --n)
                midi_[count++] = {0, off, static_cast<std::uint8_t>(pitch), 0, 0};
        }
        midi_[count++] = {0, cc, 123, 0, 0};
        midi_[count++] = {0, cc, 120, 0, 0};
    }
    // One private frame, not a device frame. Never advance the transport to
    // flush. The current ABI cannot carry MIDI in a zero-frame process call.
    float scratch[2]{};
    const auto code = rt_binding_.process(rt_binding_.user, scratch, 1, midi_.data(), count,
                                          rt_.render_position, rt_binding_.timeline->bpm_, 0);
    forget_notes();
    return code;
}

bool Kernel::resolve() noexcept {
    const auto code = release_notes();
    if (!code) return true;
    trip(Fault::reset, code);
    return false;
}

void Kernel::trip(Fault fault, std::int32_t code, bool try_cleanup) noexcept {
    if (rt_.fault != Fault::none) return;
    if (try_cleanup) code = release_notes();
    else forget_notes();
    rt_.fault = fault;
    rt_.processor_code = code;
    rt_.playing = false;
    clear_ramp();
}

void Kernel::arm_ramp() noexcept {
    origin_l_ = last_l_;
    origin_r_ = last_r_;
    ramp_left_ = ramp_size_;
}

void Kernel::clear_ramp() noexcept {
    ramp_left_ = 0;
    last_l_ = last_r_ = 0.0f;
}

void Kernel::smooth(float* out, std::uint32_t frames) noexcept {
    if (!frames) return;
    for (std::uint32_t i = 0; i < frames && ramp_left_; ++i) {
        const auto p = static_cast<std::size_t>(i) * 2u;
        const float gain = static_cast<float>(ramp_size_ - ramp_left_ + 1) /
                           static_cast<float>(ramp_size_);
        out[p] = (1.0f - gain) * origin_l_ + gain * out[p];
        out[p + 1] = (1.0f - gain) * origin_r_ + gain * out[p + 1];
        --ramp_left_;
    }
    const auto end = static_cast<std::size_t>(frames) * 2u;
    last_l_ = out[end - 2];
    last_r_ = out[end - 1];
}

void Kernel::render_output(float* out, std::uint32_t frames) noexcept {
    std::uint32_t written = 0, calls = 0, music_events = 0;
    while (written < frames && rt_.playing) {
        if (calls == kMaxMusicCalls) {
            trip(Fault::work_budget, 0, true);
            break;
        }
        const auto& timeline = *rt_binding_.timeline;
        const auto p = rt_.render_position;
        const auto end = rt_.loop ? rt_.loop_end : timeline.duration_;
        const auto limit = std::min(frames - written, rt_binding_.max_frames);
        const auto n = static_cast<std::uint32_t>(std::min<Sample>(end - p, limit));
        const auto next = p + n;  // end-p was bounded before the addition

        auto first = std::lower_bound(timeline.events_.begin(), timeline.events_.end(), p, earlier);
        const auto last = std::lower_bound(first, timeline.events_.end(), next, earlier);
        const auto count = static_cast<std::uint32_t>(last - first);
        if (count > kMidiCapacity) {
            trip(Fault::event_capacity, 0, true);
            break;
        }
        if (count > kMaxMusicEvents - music_events) {
            trip(Fault::work_budget, 0, true);
            break;
        }
        std::uint32_t starts = 0;
        for (std::uint32_t i = 0; i < count; ++i, ++first) {
            const auto& e = *first;
            midi_[i] = {static_cast<std::uint32_t>(e.sample - p), e.status, e.data1, e.data2, 0};
            if (note_start(e.status, e.data2)) ++starts;
        }
        if (!trackable(count, starts)) {
            trip(Fault::voice_capacity, 0, true);
            break;
        }

        auto* target = out + static_cast<std::size_t>(written) * 2u;
        const auto code = rt_binding_.process(rt_binding_.user, target, n,
                                              count ? midi_.data() : nullptr, count,
                                              p, timeline.bpm_, 1);
        if (code) {
            // A failing processor may have written output or consumed some MIDI.
            // Don't try to infer what it accepted; quarantine the binding.
            std::fill_n(target, static_cast<std::size_t>(n) * 2u, 0.0f);
            trip(Fault::processor, code);
            break;
        }
        accept_midi(count);
        rt_.render_position = next;
        add_count(rt_.music_frames, n);
        ++calls;
        music_events += count;
        smooth(target, n);
        written += n;

        if (next == end) {
            if (!resolve()) break;
            if (rt_.loop) rt_.render_position = rt_.loop_begin;
            else {
                rt_.playing = false;
                arm_ramp();
            }
        }
    }
    if (written < frames)
        smooth(out + static_cast<std::size_t>(written) * 2u, frames - written);
}

void Kernel::render(float* out, std::uint32_t frames) noexcept {
    // Read the command frontier FIRST: publishing any command for a resumed
    // phase follows the UI's resume store. The phase acquire below therefore
    // cannot see an older phase than that command (it may see a later park).
    const auto frontier = submitted_.load(std::memory_order_acquire);
    const auto phase = requested_phase_.load(std::memory_order_acquire);
    const bool changed = phase != rt_.guard.phase;
    if (changed) enter_phase(phase);

    if (out && frames) {
        std::fill_n(out, static_cast<std::size_t>(frames) * 2u, 0.0f);
        add_count(rt_.output_frames, frames);
    }
    const bool usable = frames <= kMaxCallbackFrames && (out || frames == 0);
    if (extension_) {
        if (!parked(phase)) extension_->render(rt_, out, frames, usable);
    } else {
        if (!parked(phase) && rt_.bound && !usable)
            trip(Fault::callback_buffer, 0, true);
        while (rt_finished_ < frontier) {
            auto& slot = slots_[slot_index(rt_finished_)];
            const auto command = slot.command;
            const auto result = apply(command);
            slot.receipt = {command.id, result, rt_.guard, rt_.render_position};
            ++rt_finished_;
        }
        if (!parked(phase) && usable && frames) render_output(out, frames);
    }

    add_count(rt_.callbacks, 1);
    publish_snapshot();
    finished_.store(rt_finished_, std::memory_order_release);
    if (changed && parked(phase))
        acked_phase_.store(phase, std::memory_order_release);
}

} // namespace astra::rt
