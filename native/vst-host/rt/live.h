#pragma once
#include "bridge.h"
#include "../astra_vst_host.h"

#ifdef __cplusplus
extern "C" {
#endif
#define ASTRA_LIVE_VERSION 3u
#define ASTRA_LIVE_MAX_CLOCK 262144u
#define ASTRA_LIVE_STALE (-9)

enum AstraLiveOp {
    ASTRA_LIVE_PLAY, ASTRA_LIVE_PAUSE, ASTRA_LIVE_SEEK, ASTRA_LIVE_STOP,
    ASTRA_LIVE_LOOP_ON, ASTRA_LIVE_LOOP_OFF, ASTRA_LIVE_ON, ASTRA_LIVE_OFF, ASTRA_LIVE_CLEAR, ASTRA_LIVE_MIX
};
enum AstraLiveResult {
    ASTRA_LIVE_APPLIED, ASTRA_LIVE_OBSOLETE, ASTRA_LIVE_RANGE, ASTRA_LIVE_NO_TIMELINE,
    ASTRA_LIVE_VOICES, ASTRA_LIVE_FAULTED
};
enum AstraLiveState { ASTRA_LIVE_STOPPED, ASTRA_LIVE_PAUSED, ASTRA_LIVE_PLAYING };

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

typedef struct AstraLiveClick { int64_t sample; uint32_t accent, reserved; } AstraLiveClick;

typedef struct AstraLiveClock {
    int64_t sample;
    double beat, bpm, bar_start_beat;
    int32_t numerator, denominator;
} AstraLiveClock;
typedef struct AstraLiveCommand {
    uint32_t size, version, op, reserved;
    uint64_t id, epoch, sequence, intent, token;
    int64_t a, b; /* Play: -1 resumes, otherwise locate; Seek: a; Loop: [a,b) */
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
    uint32_t muted, reserved2;
    double midi_gain, audio_gain, click_gain;
    uint32_t metronome, reserved3;
} AstraLiveSnapshot;

/* Control calls on creating thread. Use ABI 1 retain/render/release, park,
   resume and destroy. No ABI 1 submit/replace on this kind of bridge. BUSY
   returns backpressure, never a park. IDs of accepted commands get one outcome;
   if publish coalesces a still-pending publication its ID is returned directly.
   Poll is single-consumer; the facade journals results before exposing status. */
ASTRA_RT_API uint32_t ASTRA_RT_CALL astra_rt_live_version(void);
ASTRA_RT_API const char* ASTRA_RT_CALL astra_rt_live_error(void);
ASTRA_RT_API int32_t ASTRA_RT_CALL astra_rt_live_create(const AstraLiveConfig*, void*,
    AstraLiveProcess, AstraRtBridge**);
ASTRA_RT_API int32_t ASTRA_RT_CALL astra_rt_live_publish(AstraRtBridge*, const AstraLiveSpec*,
    const AstraRtEvent*, const AstraLiveClock*, uint64_t* superseded, uint32_t* unsupported);
ASTRA_RT_API int32_t ASTRA_RT_CALL astra_rt_live_publish_clicks(AstraRtBridge*, const AstraLiveSpec*,
    const AstraRtEvent*, const AstraLiveClock*, const AstraLiveClick*, uint32_t, uint64_t*, uint32_t*);
ASTRA_RT_API int32_t ASTRA_RT_CALL astra_rt_live_bind_pcm(AstraRtBridge*, uint64_t park_ticket, const AstraLivePcmSource*);
ASTRA_RT_API int32_t ASTRA_RT_CALL astra_rt_live_submit(AstraRtBridge*, const AstraLiveCommand*);
ASTRA_RT_API int32_t ASTRA_RT_CALL astra_rt_live_poll(AstraRtBridge*, AstraLiveReceipt*);
ASTRA_RT_API int32_t ASTRA_RT_CALL astra_rt_live_snapshot(AstraRtBridge*, AstraLiveSnapshot*);
#ifdef __cplusplus
}
#endif
