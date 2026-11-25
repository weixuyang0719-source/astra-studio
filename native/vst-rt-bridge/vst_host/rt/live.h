#pragma once
#include "bridge.h"
#include "../astra_vst_host.h"

#ifdef __cplusplus
extern "C" {
#endif
#define ASTRA_LIVE_VERSION 3u
#define ASTRA_LIVE_MAX_CLOCK 262144u
#define ASTRA_LIVE_RATE_ONE 10000u /* fixed-point project samples per output frame */
#define ASTRA_LIVE_STALE (-9)

enum AstraLiveOp {
    ASTRA_LIVE_PLAY, ASTRA_LIVE_PAUSE, ASTRA_LIVE_SEEK, ASTRA_LIVE_STOP,
    ASTRA_LIVE_LOOP_ON, ASTRA_LIVE_LOOP_OFF, ASTRA_LIVE_ON, ASTRA_LIVE_OFF, ASTRA_LIVE_CLEAR, ASTRA_LIVE_MIX, ASTRA_LIVE_RATE, ASTRA_LIVE_COUNT_PLAY, ASTRA_LIVE_SCORE_RATE
};
enum AstraLiveResult {
    ASTRA_LIVE_APPLIED, ASTRA_LIVE_OBSOLETE, ASTRA_LIVE_RANGE, ASTRA_LIVE_NO_TIMELINE,
    ASTRA_LIVE_VOICES, ASTRA_LIVE_FAULTED
};
enum AstraLiveState { ASTRA_LIVE_STOPPED, ASTRA_LIVE_PAUSED, ASTRA_LIVE_PLAYING, ASTRA_LIVE_COUNTING };

typedef int32_t (ASTRA_RT_CALL *AstraLiveProcess)(void*, float*, uint32_t,
    const AvRtMidiEvent*, uint32_t, const AvRtContext*);
typedef struct AstraLiveConfig {
    uint32_t size, version, max_frames, reserved;
    double sample_rate, gain;
    uint32_t muted, reserved2;
    double midi_gain, audio_gain, click_gain;
    uint32_t metronome, bus_mode;
} AstraLiveConfig;
typedef struct AstraLiveSpec {
    uint32_t size, version, event_count, clock_count;
    uint64_t id, epoch, sequence;
    int64_t duration;
    double sample_rate;
} AstraLiveSpec;
typedef struct AstraLivePcmSource {
    uint32_t size,version; double sample_rate; uint64_t frames;
    void* handle;
    int (*request)(void*,uint32_t,uint64_t,uint64_t);
    int (*read)(void*,uint32_t,uint64_t,uint32_t,float*);
    int (*hint)(void*,uint32_t,uint64_t,uint64_t);
} AstraLivePcmSource;

/* reserved = 0 for all rates, or low 16 bits are a fixed-point step
   threshold; bit 31 inverts it to strictly below. ABI layout unchanged. */
typedef struct AstraLiveClick { int64_t sample; uint32_t accent, reserved; } AstraLiveClick;
ASTRA_RT_API uint32_t ASTRA_RT_CALL astra_rt_live_score_click_gates(void);
ASTRA_RT_API uint32_t ASTRA_RT_CALL astra_rt_live_score_count_in(void);
ASTRA_RT_API uint32_t ASTRA_RT_CALL astra_rt_live_score_rate(void);

typedef struct AstraLiveClock {
    int64_t sample;
    double beat, bpm, bar_start_beat;
    int32_t numerator, denominator;
} AstraLiveClock;
typedef struct AstraLiveCommand {
    uint32_t size, version, op, reserved;
    uint64_t id, epoch, sequence, intent, token;
    int64_t a, b; /* Play: -1 resumes, otherwise locate; Seek: a; Loop: [a,b); Rate: a in basis points */
    // COUNT_PLAY: a=start (-1 resumes), b=physical lead frames,
    // token=step in units of 1/ASTRA_LIVE_RATE_ONE output frame,
    // channel=pulse count, pitch=accent period (pulses).
    uint32_t channel, pitch, velocity, reserved2;
    double gain;
    uint32_t muted, reserved3;
    double midi_gain, audio_gain, click_gain;
    uint32_t metronome, bus_mode;
} AstraLiveCommand;
typedef struct AstraLiveReceipt {
    uint32_t size, version, result, reserved;
    uint64_t id;
} AstraLiveReceipt;
typedef struct AstraLiveSnapshot {
    uint32_t size, version, state, loop, has_timeline, fault;
    int32_t processor_code;
    uint32_t parked;
    uint64_t epoch, sequence, last_intent, callbacks, output_frames, underruns, process_calls;
    int64_t position;
    double gain, effective_gain;
    uint32_t muted, position_fraction; /* units of 1/ASTRA_LIVE_RATE_ONE project frames */
    double midi_gain, audio_gain, click_gain;
    uint32_t metronome, rate_basis; /* formerly reserved; zero from older bridges */
} AstraLiveSnapshot;

/* By default control calls use the creating thread. An opt-in one-time
   dispatcher handoff moves only publish, publish_clicks, submit, poll and
   live_snapshot. Lifecycle and exact park/resume/PCM binding retain the
   original owner. Use ABI 1 retain/render/release, park,
   resume and destroy. No ABI 1 submit/replace on this kind of bridge. BUSY
   returns backpressure, never a park. IDs of accepted commands get one outcome;
   if publish coalesces a still-pending publication its ID is returned directly.
   Poll is single-consumer; the facade journals results before exposing status. */
ASTRA_RT_API uint32_t ASTRA_RT_CALL astra_rt_live_version(void);
ASTRA_RT_API const char* ASTRA_RT_CALL astra_rt_live_error(void);
ASTRA_RT_API int32_t ASTRA_RT_CALL astra_rt_live_create(const AstraLiveConfig*, void*,
    AstraLiveProcess, AstraRtBridge**);
/* Creating owner calls prepare to stop old transport submissions, then hands
   the lane-scoped epoch to the designated dispatcher thread in this process.
   There it claims, services the sole producer/consumer and releases as its
   final bridge access. Stop admitting dispatch work and resolve or invalidate
   buffered commands before release; the native functions do not own that
   application queue. Do not pass a raw bridge address across processes.
   When dispatch startup fails, cancel_unclaimed is allowed only on the
   creating owner after ensuring no thread can enter claim. Destroy requires
   both dispatcher retirement and device-lease release. The creating owner
   may take a final live snapshot after dispatcher release (no submit/poll).
   A claimed dispatcher must stay alive through release; the caller should
   join it before using or freeing any other resources it borrowed. */
ASTRA_RT_API int32_t ASTRA_RT_CALL astra_rt_live_dispatch_prepare(AstraRtBridge*, uint64_t* epoch);
ASTRA_RT_API int32_t ASTRA_RT_CALL astra_rt_live_dispatch_claim(AstraRtBridge*, uint64_t epoch);
ASTRA_RT_API int32_t ASTRA_RT_CALL astra_rt_live_dispatch_release(AstraRtBridge*, uint64_t epoch);
ASTRA_RT_API int32_t ASTRA_RT_CALL astra_rt_live_dispatch_cancel_unclaimed(AstraRtBridge*, uint64_t epoch);
/* Creating owner reclaims only after joining the former dispatcher. */
ASTRA_RT_API int32_t ASTRA_RT_CALL astra_rt_live_dispatch_reclaim(AstraRtBridge*, uint64_t epoch);
ASTRA_RT_API int32_t ASTRA_RT_CALL astra_rt_live_publish(AstraRtBridge*, const AstraLiveSpec*,
    const AstraRtEvent*, const AstraLiveClock*, uint64_t* superseded, uint32_t* unsupported);
ASTRA_RT_API int32_t ASTRA_RT_CALL astra_rt_live_publish_clicks(AstraRtBridge*, const AstraLiveSpec*,
    const AstraRtEvent*, const AstraLiveClock*, const AstraLiveClick*, uint32_t, uint64_t*, uint32_t*);
ASTRA_RT_API int32_t ASTRA_RT_CALL astra_rt_live_bind_pcm(AstraRtBridge*, uint64_t park_ticket, const AstraLivePcmSource*);
ASTRA_RT_API int32_t ASTRA_RT_CALL astra_rt_live_submit(AstraRtBridge*, const AstraLiveCommand*);
ASTRA_RT_API int32_t ASTRA_RT_CALL astra_rt_live_poll(AstraRtBridge*, AstraLiveReceipt*);
ASTRA_RT_API int32_t ASTRA_RT_CALL astra_rt_live_snapshot(AstraRtBridge*, AstraLiveSnapshot*);
// Rendering thread only. Invoke immediately after its native callback returns.
ASTRA_RT_API int32_t ASTRA_RT_CALL astra_rt_render_snapshot(AstraRtBridge*, AstraLiveSnapshot*);
#ifdef __cplusplus
}
#endif
