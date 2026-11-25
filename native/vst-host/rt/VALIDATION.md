# Independent validation: native VST RT kernel and bridge (2026-11-18)

Implemented by Sol Max Ultrafast, reviewed and tested locally before integration.
No active application playback route was changed. The plugin worker remains offline-only until explicit native-device integration and validation.

Verified:
- MSVC C++17 /O2 /W4 standalone kernel tests passed, with and without NDEBUG.
- 20 consecutive NDEBUG runs passed (includes concurrent command/snapshot and in-flight quiesce cases).
- Separate real official mda Piano offline adapter: note at offset 17 first sounded at sample 17; peak 0.359862; pause produced silence; exact quiesce allowed detach and plugin close; later render did not access closed plugin.
- Bridge fake-device tests passed, including owner thread affinity, counted device leases, blocked processor retirement, exact park tickets, replacement, invalid inputs.
- Project CMake Release targets built: astra_vst_rt_kernel, astra_vst_rt_bridge.dll, test executables.
- Final project CTest: astra_vst_rt_kernel and astra_vst_rt_bridge both passed (2/2).
- Actual built DLL loaded with ctypes: ABI version 1, empty bridge creation, 128-byte snapshot and destruction passed. No Python audio callback was used.

Independent review correction:
Sol's bridge originally returned paContinue for invalid/oversized buffers without filling output. Such a buffer must not be submitted. Corrected to paAbort (2), including absent live lease and null positive-frame output. The fake owner now honors abort and regression verifies it.

Not yet verified / deliberately not claimed:
- No real audio device was opened by this work. No live GUI workflow, audible latency, dropout, click, or perceived responsiveness claim.
- Worker owns native device and bridge in same process: still to be wired; never share pointers across processes.
- Complete MIDI/controller/voice chase, plugin tails, tempo-map process context, per-device audible clock and cross-process route ownership remain integration work.
- Do not use a park grant or timeout as device retirement. Keep bridge/module until final native callback and release hook return, and host destroy succeeds.
- Kernel bounded no-allocation behavior does not certify arbitrary plugin DSP as real-time safe.
- Existing mda SDK 16-sample boundary fixture bug is separate; do not compensate by shifting host timestamps.

Second independent audit: existing host mapped-parameter Queue has MAX_POINTS=256 versus kernel MIDI capacity4096. Dense same-CC events and UI edits can return -3 and quarantine a binding. Before live integration, add capacity regression and coalescing/bounded edit policy; kernel capacity alone is not a plugin-host capacity guarantee. Exact-park/last-release contracts showed no obvious UAF under single-owner/single-callback assumptions in this audit; no claim of exhaustive concurrency proof.
