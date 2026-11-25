# Astra RT bridge — legacy ABI v1

This page describes the unchanged ABI 1 transport. For the live extension and worker facade see [LIVE.md](LIVE.md). Include `live.cpp` whenever compiling `bridge.cpp` (including the standalone commands below).

Place the four files in `native/vst_host/rt`. The bridge links the supplied kernel into the same image and does not link against PortAudio, the Astra VST host, or the VST SDK. The VST host's ABI v1 process address is injected. The test injects a fake with that signature and uses a separate native callback thread; it needs no driver or SDK. The intentionally blocked fake call tests retirement, not RT performance.

## Build

Run from `native/vst_host/rt`. Linux, with NDEBUG test checks still enabled:

```sh
c++ -std=c++17 -O2 -DNDEBUG -Wall -Wextra -Wpedantic -pthread kernel.cpp bridge.cpp live.cpp bridge_test.cpp -o bridge_test
./bridge_test
```

Windows, from an x64 MSVC developer prompt. Use cdecl (`/Gd`, matching the supplied host headers) and one CRT configuration. `/EHs` enables the defensive C++ catch in the adapter without `/EHc`'s assumption that calls with C linkage cannot throw; the processor contract is still nonthrowing.

```bat
cl /nologo /c /std:c++17 /EHs /Gd /O2 /MD /DNDEBUG kernel.cpp
lib /nologo /OUT:astra_rt_kernel.lib kernel.obj
cl /nologo /LD /std:c++17 /EHs /Gd /O2 /MD /DNDEBUG /DASTRA_RT_BRIDGE_BUILD bridge.cpp live.cpp astra_rt_kernel.lib /link /OUT:astra_rt_bridge.dll
cl /nologo /std:c++17 /EHs /Gd /O2 /MD /DNDEBUG bridge.cpp live.cpp bridge_test.cpp astra_rt_kernel.lib /Fe:bridge_test.exe
bridge_test.exe
```

## Ownership and shutdown

- Create and control the bridge on the same UI thread that opens and owns the plugin. Keep that thread alive until bridge destruction. Creation starts paused, with **zero device leases**. A null spec, events, handle and process address creates an unbound bridge. Otherwise pass a borrowed handle and a native Astra VST ABI v1 `astra_vst_process` address. The bridge never calls `astra_vst_close`, edits plugin state, or owns the handle or the VST DLL. Do not let a Python finalizer close the handle or unload its DLL. Keep the module and handle alive until successful detach/replacement under an exact grant or until the lane is definitively retired and the bridge is destroyed.
- Initiate attachment only from the bridge UI thread: call `astra_host_create_owned` with `astra_rt_get_callback()`, the bridge as `core_user`, and `astra_rt_device_retain`/`astra_rt_device_release` as the reference hooks. **No new attachment may race destruction.** The host must acquire its lease synchronously before returning from create and before permitting callbacks. On failure it must return any acquired lease only if the lane is definitively retired. Do not use the unowned create API or call the hooks from Python. Counted references protect lifetime, not render serialization: there is one non-reentrant callback lane per bridge, with a synchronized handoff if its thread changes.
- The native owner must return its lease exactly once, with the last callback's return synchronized before release and no possibility of another callback without a new retain. A close request, device error, stopped-looking callback counter, park ack, or timeout is not proof of retirement. If the host cannot prove final retirement, keep everything alive. No bridge API fabricates a release. Once the final release publishes zero leases, the hook has no further bridge access, so UI destruction can succeed even before the hook's return epilogue. **That is not permission to unload bridge code**: keep the bridge DLL and device DLL resident until the native host and its callback/reference calls have fully returned, or keep them resident for process lifetime.
- `active_leases == 0, released == 0` means never retained. `released == 1` means all acquired leases have been returned at the time of the sample; a subsequent synchronous retain clears it. The two fields come from one atomic read. The theoretical overflow sentinel `active_leases == UINT64_MAX, released == 0` means accounting saturated and destruction fails closed. `astra_rt_destroy` returns `ASTRA_RT_BUSY` without freeing anything while a lease remains; otherwise it frees only bridge-owned objects. Use the existing host close/snapshot/destroy protocol to retire the device owner, not the bridge as a device-stop API. Do not use a handle after its successful destroy, or race control calls with destroy, even calls that would be rejected as wrong-thread.
- Normal shutdown if callbacks still arrive: park, wait for the exact ack, detach, keep parked if desired, and close the old plugin on UI. Then retire the device and destroy the bridge. If callbacks have already stopped without the ack, do not close a still-borrowed plugin on the strength of a park request. Definitively retire the owner, destroy the bridge on UI, then close the plugin. A pending park can remain pending forever even after retirement; it is not required for destruction once all leases are returned.

## Control protocol

1. Pass a prepared-sample duration (positive and exclusive), constant BPM, native plugin max block, and a bounded array of `AstraRtEvent`. The bridge copies it synchronously; the kernel validates and stably sorts it. Equal-sample input order is preserved. Input arrays/specs may be discarded after the call. Use the actual session/device sample rate when preparing positions and opening the plugin; no rate conversion is done here.
2. Submit a command with a snapshot's guard. `ASTRA_RT_ANY_LOCATE` is an explicit opt-in to ignoring the locate stamp for that command. An ABI return of `ASTRA_RT_OK` means the submission reply is valid, not that the command queued or took effect; inspect `code` and then match a queued `id` to a receipt. Drain receipts to return ring capacity. `ASTRA_RT_EMPTY` is a non-error no-receipt result. On full or exhausted, handle the returned emergency park ticket exactly like an explicit park. Snapshot position is for the next musical frame, not a DAC or audible clock.
3. Request a park and poll `astra_rt_park_status` with that **exact** odd ticket on the UI thread. `ASTRA_RT_BUSY` from a second request means a ticket is already held or pending; it is in `snapshot.park_ticket`. `PENDING` and `STALE` are not grants. Only `PARK_ACK` for the current ticket authorizes plugin UI calls that must not overlap processing, such as state get/set. Make those calls synchronously on UI, before resuming; do not pump a nested event loop that allows another control path to resume while using the grant. Keep actual device callbacks coming until the ack, if possible. Neither polling nor retirement runs a substitute callback.
4. With the exact ack, `astra_rt_replace` installs a new timeline/binding or detaches with all source fields null. For timeline-only edits pass the same live plugin/address with the new timeline. On success the old timeline is freed on UI, the old plugin is no longer reachable from the bridge if replaced/detached, and the ticket remains held. Invalid input or preparation failure leaves the old staging intact. If plugin UI work fails, you may stay parked. Resume releases the grant, not Play. Wait for the snapshot's render guard to reflect the resumed phase (`ticket + 1`) and staged generation before using that guard for a new intent. The render view, park state and lease state are separate observations, not one global transaction. Use the exact-ticket API for the grant, not an inference from `bound`, the phase in an old view, or a fault.

## ctypes wiring

Run this inside the process/thread owning the plugin, e.g. the worker's Qt UI thread. Use `ctypes.CDLL` for cdecl. Keep explicit strong references to the native libraries, the plugin owner, and the live bridge/device handles through the shutdown sequence. Do not marshal any addresses over worker IPC. Use values (commands, timeline data, state bytes, and status) over IPC and route control requests onto the owning UI thread.

All public records have natural native alignment with no pointer members and no implicit packing requirement. Sizes in bytes: Config 16, TimelineSpec 32, Event 16, Guard 24, Command 56, Submission 32, Receipt 56, Snapshot 128. Set `size = ctypes.sizeof(type)` and `version = 1` on all versioned records, including outputs. Event and embedded Guard have no header. Use `c_uint64` for ticket/ID/stamp/count fields, `c_int64` for project samples, and `c_uint32` for the flags, not `c_ulong` or `c_bool`. PortAudio's *callback parameters* `frames` and flags are native `unsigned long`; Python never calls that function. Declare all `argtypes` and `restype` before use; convert Python integers with range checks, especially before constructing array counts and byte fields. As with any raw C ABI, nonnull pointers must address live, suitably aligned storage of the declared extent and access mode. Records/arrays must not overlap outputs or be mutated during a call. Version/size checks do not prove an arbitrary pointer safe.

Example of the initial binding and address wiring. `rt`, `vst`, `pa` are the loaded bridge, VST and device-host libraries; `plugin_handle` was opened on this UI thread at the selected rate with `max_block`. Check both `astra_rt_abi_version()` and `astra_vst_abi_version()` are 1 (both return `c_uint32`).

```python
import ctypes as C

class Config(C.Structure):
    _fields_ = [(n, C.c_uint32) for n in ('size', 'version', 'ramp_frames', 'reserved')]

class TimelineSpec(C.Structure):
    _fields_ = [('size', C.c_uint32), ('version', C.c_uint32),
                ('duration', C.c_int64), ('bpm', C.c_double),
                ('event_count', C.c_uint32), ('max_frames', C.c_uint32)]

class Event(C.Structure):
    _fields_ = [('sample', C.c_int64), ('status', C.c_uint8), ('data1', C.c_uint8),
                ('data2', C.c_uint8), ('reserved', C.c_uint8), ('reserved2', C.c_uint32)]

assert (C.sizeof(Config), C.sizeof(TimelineSpec), C.sizeof(Event)) == (16, 32, 16)
rt.astra_rt_create.argtypes = [C.POINTER(Config), C.POINTER(TimelineSpec), C.POINTER(Event),
                               C.c_void_p, C.c_void_p, C.POINTER(C.c_void_p)]
rt.astra_rt_create.restype = C.c_int32
rt.astra_rt_get_callback.argtypes = []
rt.astra_rt_get_callback.restype = C.c_void_p
pa.astra_host_create_owned.argtypes = [C.c_wchar_p, C.c_void_p, C.c_void_p, C.c_void_p, C.c_void_p]
pa.astra_host_create_owned.restype = C.c_void_p

config = Config(C.sizeof(Config), 1, 16, 0)
events = (Event * 2)(Event(17, 0x90, 60, 100, 0, 0), Event(2417, 0x80, 60, 0, 0, 0))
spec = TimelineSpec(C.sizeof(TimelineSpec), 1, 48000, 120.0, len(events), max_block)
bridge = C.c_void_p()
rc = rt.astra_rt_create(C.byref(config), C.byref(spec), events, plugin_handle,
                        C.cast(vst.astra_vst_process, C.c_void_p), C.byref(bridge))
if rc != 0:
    raise RuntimeError(f'bridge create: {rc}')
# Only after authorization, with the trusted configured PortAudio DLL path.
device = pa.astra_host_create_owned(portaudio_dll_path, rt.astra_rt_get_callback(), bridge,
                                   C.cast(rt.astra_rt_device_retain, C.c_void_p),
                                   C.cast(rt.astra_rt_device_release, C.c_void_p))
# Check device before requesting the explicitly selected backend/route. On
# failure keep bridge/plugin alive until astra_rt_destroy can succeed safely.
```

No `CFUNCTYPE` callback or GIL acquisition is involved. The addresses above are passed through, not called by Python. The bridge callback uses no heap allocation, locks, logging, or library loading. It expects interleaved stereo float32 output and returns `paContinue` only for valid callbacks with a live device lease. Invalid or oversized buffers and absent live leases return `paAbort` (2), so an unwritten buffer is never submitted as normal audio. For an oversized reported frame count it does not touch the output buffer; for a missing positive-frame buffer there is nothing to write. Both latch `bad_callback` for the lifetime of the bridge and still allow that real callback boundary to service control. When bound and unparked the kernel also handles these as callback-buffer faults. Treat either as a device contract failure and retire/fix the route; there is no fallback. The native processor must itself be RT-suitable, synchronous, not retain output/event pointers, and write finite stereo samples on success. The adapter copies MIDI fields into a fixed native `AvMidiEvent` array; it cannot contain native memory faults or make a blocking plugin real-time safe.

This is the constant-tempo, prepared-sample stage. There is no chase, tempo map, full audible clock, or claim of complete VST playback. The worker, device-route authorization, plugin policy, and existing stems path are unchanged by these files.


Independent review correction: invalid or oversized callback buffers and missing live leases return PortAudio paAbort (2), never paContinue with an unwritten buffer. The fake owner honors paAbort by retiring the lane, preventing garbage audio from malformed callbacks.
