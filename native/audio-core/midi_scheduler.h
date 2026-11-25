#pragma once
#include <stdint.h>
#define MIDI_API extern "C" __declspec(dllexport)
struct AstraMidiEvent { double seconds; uint32_t packed,reserved; };
struct AstraMidiNote { double start,end,sounding_end; uint32_t channel,pitch,velocity,reserved; };
struct AstraMidiClock { uint32_t size,version; double seconds; uint64_t epoch; uint32_t playing,reserved; };
// Native callbacks only, never Python trampolines. Return 0 on success.
using AstraMidiClockFn=int(*)(void*,AstraMidiClock*);
using AstraMidiSendFn=int(*)(void*,uint32_t);
struct AstraMidiStatus {uint32_t size,version;double seconds;uint64_t accepted,applied,sent;uint32_t playing,closed;int32_t error,reserved; /* reserved reports timer kind: 2 high-resolution, 1 ordinary waitable */};
// Copies events/spans. Raw events are authoritative for seek voice/pedal state;
// note spans are validated metadata, not a CC66 or sustain lifetime oracle.
// Duplicate-note matching follows FIFO; synth-specific envelope phase is not restored.
// clock==nullptr selects standalone steady-clock master.
// With external clock, that clock alone controls play/pause/seek/loop/rate.
// Volume, click gate and click table updates are still allowed. Clock epoch MUST
// change on seek, loop or source change.
MIDI_API void* astra_midi_create(const AstraMidiEvent*,uint64_t,const AstraMidiNote*,uint64_t,double duration,AstraMidiClockFn,void* clock_user,AstraMidiSendFn,void* send_user);
// Commands: 1 play,2 pause,3 seek(a),4 stop,5 rate(a),6 loop(a,b),7 volume(a 0..1),8 disable loop,9 click gate(a 0 or 1,b=0).
// Returns sequence >0, -1 invalid, -2 queue full, -3 externally controlled/closing, -4 resource failure.
MIDI_API int64_t astra_midi_command(void*,uint32_t,double,double);
MIDI_API int astra_midi_snapshot(void*,AstraMidiStatus*);
MIDI_API void astra_midi_close(void*); // asynchronous; resolves sounding notes, cancels queue
MIDI_API int astra_midi_wait(void*,uint32_t timeout_ms); // non realtime, 1 if closed
// Succeeds only after closed. Keep callback functions/users alive until success.
MIDI_API int astra_midi_destroy(void*);
MIDI_API uint32_t astra_midi_abi_version();

using AstraMidiLifetimeFn=void(*)(void*);
// Retains callback users before starting worker, releases exactly once after its
// last callback and before closed=true. Each retain/release pair must both exist or both be null.
MIDI_API void* astra_midi_create_owned(const AstraMidiEvent*,uint64_t,const AstraMidiNote*,uint64_t,double,AstraMidiClockFn,void*,AstraMidiSendFn,void*,AstraMidiLifetimeFn clock_retain,AstraMidiLifetimeFn clock_release,AstraMidiLifetimeFn send_retain,AstraMidiLifetimeFn send_release);


// Recent 512 command receipts: 0 pending, 1 executed, 2 superseded by a later
// adjacent seek, 3 canceled on close/failure, 4 failed, -1 unknown/evicted.
MIDI_API int astra_midi_receipt(void*,uint64_t sequence);
MIDI_API uint64_t astra_midi_superseded(void*);

// Coherent last fully-dispatched external master token; 1 means not yet observed/busy.
MIDI_API int astra_midi_master_snapshot(void*,AstraMidiClock*);

// Additive ABI-1 exports. Clicks start disabled with an empty table. These are
// score-time onsets, separate from performance events and seek voice chase.
struct AstraMidiClick {double seconds;uint32_t accent,reserved;};
// Control thread only. Copies <=100000 sorted onsets in [0,duration), accent
// 0 or 1, reserved 0. Installs the table and gate together in command order.
// Same receipt/error convention as command; -2 also means one replacement is
// already pending. Wait for that receipt before retrying. count=0 clears the table.
MIDI_API int64_t astra_midi_set_clicks(void*,const AstraMidiClick*,uint64_t count,uint32_t enabled);
struct AstraMidiMetronomeStatus {
 uint32_t size,version;
 uint64_t note_ons,note_offs,late_skipped;
 uint32_t beats,enabled;
 int32_t channel; // -1: no unused MIDI channel; never borrow a performance channel
 uint32_t gain_cc7;
};
// Sampled diagnostics, not a transport receipt or confirmation of audible sound.
MIDI_API int astra_midi_metronome_status(void*,AstraMidiMetronomeStatus*);
