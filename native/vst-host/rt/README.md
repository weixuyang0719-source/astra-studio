# Astra native VST transport kernel

This is a standalone C++17 slice. It borrows one instrument binding, owns sample-time transport in the native callback, and does not open a device, load a DLL, or implement IPC. No WinMM or timer scheduler participates. `kernel_test.cpp` uses fake instruments, including one that turns MIDI into impulses at the actual received offsets. Checks stay enabled under `NDEBUG`. The harness traps C++ `new`/`delete` (including aligned variants) on the render lane; that is not instrumentation of vendor DLLs or raw CRT allocation.

## Build the tests

From an x64 MSVC developer prompt:

```bat
cl /nologo /std:c++17 /EHsc /W4 /O2 /DNDEBUG kernel.cpp kernel_test.cpp /Fe:kernel_test.exe
.\kernel_test.exe
```

Or with a C++17 toolchain on POSIX:

```sh
c++ -std=c++17 -O2 -DNDEBUG -Wall -Wextra -pedantic -pthread kernel.cpp kernel_test.cpp -o kernel_test
./kernel_test
```

## Transport contract

- `Timeline::prepare` copies integer project-sample events and stable-sorts them. Convert SMF time/tempo maps and quantize to the actual stream rate on control, preserving desired input order for quantized ties. Choose an exclusive duration greater than the last event sample. The process context has one finite BPM in `(0,1000]`; tempo-map process context and rate changes are not implemented. Accepted messages: notes, CC, poly/channel pressure (channel pressure has zero `data2`), pitch bend. Note-on velocity zero is canonicalized to note-off on the same channel. Program changes and system messages are rejected rather than silently promising support the supplied ABI does not have.
- Music uses `[position,position+frames)`. A process call cannot cross the configured plugin max, an enabled loop's exclusive end, or the project end. Offsets are relative to that call, even after a wrap inside one device callback. There is no mda-specific timestamp adjustment. Stable ordering is at the kernel/ABI MIDI list boundary; ABI v1 separates mapped CC/pressure/bend parameters from VST events, and cannot promise the plugin's cross-list tie ordering or CC support.
- Loop enable confines the cursor to `[begin,end)` (lands at `begin` if outside); seeks outside an enabled loop are rejected. Stop rewinds to loop begin or zero. Seek preserves play/pause except that seeking to the non-loop project end stops. Play from project end rewinds. Multiple commands at one callback boundary apply in FIFO order before music. Use the observed `Guard`; its phase and generation are mandatory. Seek, Stop, loop edits and Play's implicit rewind stamp `locate` with the command ID. Automatic wraps do not invalidate explicit intents. For a dependent Play, wait for the Seek receipt or submit with the Seek ID as the expected `locate`. Use `kAnyLocate` only for intents that may act at any cursor under that generation and phase.
- Discontinuities resolve every outstanding delivered key-down, including stacked same-pitch notes on a channel. For each used channel the cleanup list starts with CC64/66/69 off, sends explicit note-offs, then CC123/120. Cleanup is a single discarded one-frame process call, with Playing clear and project time pinned at the old head or crossed boundary. This may tick internal DSP by one frame; it consumes no device or musical frames. Actual pedal/mode reset requires compatible plugin mapping/behavior. Validate it with the instrument; reload a plugin that cannot be reliably reset. No voice, controller or envelope chase on seek/resume/loop entry. A note already held at the target will not be retriggered. Non-pedal controller values may remain set. Release/reverb tails are not rendered during pause or park and internal tails may reappear on Play. The optional endpoint ramp blends from the last emitted stereo sample over 0–64 output frames for transport commands and project end. It is not a tail crossfade; wraps aren't separately smoothed. Park and faults hard-mute the unrendered output. No timestamp shifts or musical time spent on ramps to silence.
- A slot reserves space for both a command and its receipt until UI consumes the receipt. Queue full (including unread receipts) explicitly rejects the new command and returns an independent emergency park ticket. It never silently loses a Pause/Stop. A callback already past its phase read may finish. At the next boundary observing the park, cleanup runs and stale-phase commands are canceled, not replayed on resume. A publication racing that boundary may get its cancellation on the following callback. Drain the receipts even when parked. Receipts report command evaluation, not later process success or DAC presentation.
- Fixed callback limits: 32 outstanding commands, 32,768 device frames, up to 64 musical calls, 4,096 MIDI entries per process call and 8,192 musical MIDI entries per callback. An extremely short loop or tiny plugin quantum can exhaust the call budget. Key-down capacity reserves room for a single complete cleanup (4,016 explicit offs plus up to 80 CCs). Density, voice or work overflow faults before submitting the offending segment. Already successful segments stand; the rest is silent. Cleanup is attempted once for previously delivered voices. An invalid device buffer faults an active binding; oversized valid buffers are still zeroed in full. A failing process may have partially altered DSP, so the kernel discards its output and quarantines it without calling it again. An error during cleanup is also sticky (`processor_code` records it); silence and safe retirement are possible, but clean plugin voice state is not guaranteed. A new generation requires a fresh or independently reinitialized binding; do not simply republish a poisoned handle.

## Threads, memory and lifetime

One UI producer/receipt consumer/snapshot reader, one serial callback lane. Any callback-thread handoff must itself synchronize; no concurrent/reentrant `render`. The kernel does not allocate, free, lock, wait, or perform I/O in `render`. The plugin and adapter must also satisfy the RT contract. The callback takes one command frontier and one phase at entry. Release/acquire publication gives it command slots; publishing completion gives slots back to UI for reading and eventual reuse. Snapshot slots have exclusive front/back owners and an atomic middle-slot exchange; UI gets a coherent value copy, not a possibly torn set of telemetry scalars. No retry loop on either lane.

`request_quiesce()` (or overflow) hands UI a unique odd phase ticket. Poll `quiesced(ticket)` asynchronously, including when already paused. A park grant is not the same as a paused snapshot, command receipt, timeout, or old acknowledgement. Before granting, the callback has returned from any cleanup call and discarded its borrowed binding. While parked it does not read staging or call a processor. UI can save/load state or safely detach the old binding with `replace_binding(ticket, {})`, then close the plugin and free its adapter/timeline on the owning thread. Stage a replacement under the same grant if needed. `resume(ticket)` publishes staging and invalidates the grant immediately; the callback can use it at its next active boundary. Same generation preserves the paused cursor/loop and any fault. New generation starts at zero without a loop or fault. Neither resumes music: observe the new even phase and issue an explicit Play. A new park can overtake an unobserved resume; the previous grant never authorizes it.

An acknowledgement is **only** processor exclusivity, not cleanliness, stopped driver, or kernel destruction permission. If callbacks stop before ack, do not manufacture it or call `render` from control. Wait for independently proven device retirement or retain the binding. To destroy the kernel, require no callback in flight and no possible future entry, including into adapter code. After successful device retirement (or proof it never started), UI may close the plugin and destroy its objects even if no quiesce ack arrived. If the driver hangs or release is uncertain, retain all potentially referenced resources and report unresolved shutdown. The kernel destructor performs no plugin or device operations.

`render_position` is the next project sample, not playback heard by the user. `output_frames` includes silence; `music_frames` counts only successful musical rendering. Cleanup and pauses do not move musical time. Park/seek cannot recall already submitted device audio. This slice has **no audible clock**. To add one, use callback DAC timestamps, per-output-range project mapping with loop/seek invalidation, and measured/reported plugin and route latency. Do not label the render head audible, or subtract a scalar latency across discontinuities and call it exact.

## Windows worker integration (follow-up, not linked by these files)

Build a native bridge in the existing dedicated plugin worker process. The worker UI thread opens the VST, prepares its timeline for the chosen rate, and owns the adapter and kernel. Send values and commands across IPC, never pointers. Resolve DLL symbols and create/touch all storage off the callback. Below is the ABI translation; no struct aliasing or Python callback is needed. The Windows callback is an `AstraPaCallback` (use `device_callback` as a native function pointer).

```cpp
#include "kernel.hpp"
#include "astra_vst_host.h"
#include "portaudio_host.h"

struct VstAdapter {
    void* handle = nullptr;
    decltype(&astra_vst_process) dispatch = nullptr; // resolve and pin DLL on control
    std::array<AvMidiEvent, astra::rt::kMidiCapacity> events{};

    static std::int32_t process(void* user, float* out, std::uint32_t frames,
                                const astra::rt::MidiEvent* midi, std::uint32_t count,
                                astra::rt::Sample pos, double bpm, std::uint32_t playing) noexcept {
        auto& v = *static_cast<VstAdapter*>(user);
        if (!v.handle || !v.dispatch || count > v.events.size()) return -1;
        for (std::uint32_t i = 0; i < count; ++i) {
            const auto& m = midi[i];
            v.events[i] = {m.sample_offset, m.status, m.data1, m.data2, 0};
        }
        return v.dispatch(v.handle, out, frames, count ? v.events.data() : nullptr,
                          count, pos, bpm, playing);
    }
};

static int device_callback(const void*, void* out, unsigned long n,
                           const void*, unsigned long, void* user) noexcept {
    static_assert(sizeof(unsigned long) <= sizeof(std::uint32_t), "Windows callback ABI");
    static_cast<astra::rt::Kernel*>(user)->render(static_cast<float*>(out),
                                                 static_cast<std::uint32_t>(n));
    return 0; // paContinue; UI handles fault/retirement, parked callbacks output silence
}
```

1. Bind `{timeline.get(), &adapter, &VstAdapter::process, actual_plugin_max_block}`. Pass the native callback and `Kernel*` to the PortAudio owner **in the worker**. Use `astra_host_create` with lifetime retained by the worker, or an owned bundle whose retain/release does not close UI-owned VST state from the device owner thread. Open the explicitly selected ASIO or WASAPI shared route, verify actual sample rate, format, bounds and callback progress. Do not advertise realtime ready from plugin load or `astra_host_open_backend` request acceptance. Distinguish device started, callbacks advancing, and successful musical processing (`music_frames`); surface sticky faults and device failures. Retain the offline-only worker's existing `realtime_ready:false` until the native wiring and qualification are in place.
2. Serialize state, explicit editor attach/remove/resize and close requests through the worker's owner UI thread; park and await the exact grant before exclusive VST calls. Include window-close/shutdown paths, not just IPC. Do not call Python `render_offline` on a device-owned instance. Qualify editor/controller concurrency specific to the plugin. Handle restart, mapping, latency and bus changes as explicit reconfiguration/reopen; ABI v1 may reject processing rather than support them in place. Audit/bound the host's edit-queue drain and parameter capacity under concurrent edits and qualify vendor allocation/locking behavior before production. Kernel limits do not make arbitrary VST code realtime safe.
3. For worker shutdown, request park while callbacks are available. After proof of exclusivity, detach and close on UI or keep parked until device close. Request `astra_host_close`; keep kernel, callback code and borrowed device user reachable through `released==1` **and successful `astra_host_destroy`**. If no ack arrives, only that authoritative device retirement substitutes for it. Never free or switch to a competing device based on timeout. No driver fallback hides failure.
4. Leave stems on their proven RAW/PauseGuard path. Piano roll uses this independent device/VST transport. Page switches may pause and hand off the selected route; coordinate device exclusion across processes explicitly (the supplied host lease is process-local). Do not mirror transport/source state by force and do not send these events through the WinMM scheduler. Treat the SDK mda <=16 boundary fixture issue separately; never shift MIDI offsets to compensate.
