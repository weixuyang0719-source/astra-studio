#pragma once
#include <stdint.h>

#if defined(_WIN32)
#define ASTRA_RT_CALL __cdecl
#if defined(ASTRA_RT_BRIDGE_BUILD)
#define ASTRA_RT_API __declspec(dllexport)
#elif defined(ASTRA_RT_BRIDGE_IMPORT)
#define ASTRA_RT_API __declspec(dllimport)
#else
#define ASTRA_RT_API
#endif
#else
#define ASTRA_RT_CALL
#define ASTRA_RT_API
#endif

#ifdef __cplusplus
extern "C" {
#endif

#define ASTRA_RT_ABI_VERSION 1u
#define ASTRA_RT_ANY_LOCATE UINT64_MAX
#define ASTRA_RT_MAX_EVENTS 1048576u
#define ASTRA_RT_MAX_PLUGIN_FRAMES 32768u
#define ASTRA_RT_MAX_CALLBACK_FRAMES 32768u
#define ASTRA_RT_MAX_RAMP_FRAMES 64u
#define ASTRA_RT_COMMAND_CAPACITY 32u
#define ASTRA_RT_PROCESS_BAD_DISPATCH (-2147483647)
#define ASTRA_RT_PROCESS_EXCEPTION (-2147483647 - 1)

/* Enumerators are constants, not types used in records or signatures. */
enum AstraRtStatus {
    ASTRA_RT_OK = 0, ASTRA_RT_EMPTY = 1,
    ASTRA_RT_INVALID = -1, ASTRA_RT_WRONG_THREAD = -2, ASTRA_RT_BUSY = -3,
    ASTRA_RT_STALE_TICKET = -4, ASTRA_RT_PENDING = -5, ASTRA_RT_NO_MEMORY = -6,
    ASTRA_RT_EXHAUSTED = -7, ASTRA_RT_INTERNAL = -8
};
enum AstraRtOp {
    ASTRA_RT_PLAY = 0, ASTRA_RT_PAUSE = 1, ASTRA_RT_SEEK = 2,
    ASTRA_RT_STOP = 3, ASTRA_RT_LOOP_ON = 4, ASTRA_RT_LOOP_OFF = 5
};
enum AstraRtSubmitCode {
    ASTRA_RT_SUBMIT_QUEUED = 0, ASTRA_RT_SUBMIT_BAD_COMMAND = 1,
    ASTRA_RT_SUBMIT_PARKED = 2, ASTRA_RT_SUBMIT_STALE = 3,
    ASTRA_RT_SUBMIT_FULL = 4, ASTRA_RT_SUBMIT_EXHAUSTED = 5
};
enum AstraRtResult {
    ASTRA_RT_APPLIED = 0, ASTRA_RT_CANCELED = 1, ASTRA_RT_STALE_COMMAND = 2,
    ASTRA_RT_RANGE = 3, ASTRA_RT_NO_BINDING = 4, ASTRA_RT_FAULTED = 5
};
enum AstraRtFault {
    ASTRA_RT_FAULT_NONE = 0, ASTRA_RT_FAULT_CALLBACK_BUFFER = 1,
    ASTRA_RT_FAULT_EVENT_CAPACITY = 2, ASTRA_RT_FAULT_VOICE_CAPACITY = 3,
    ASTRA_RT_FAULT_WORK_BUDGET = 4, ASTRA_RT_FAULT_PROCESSOR = 5,
    ASTRA_RT_FAULT_RESET = 6
};
enum AstraRtParkState {
    ASTRA_RT_PARK_NONE = 0, ASTRA_RT_PARK_PENDING = 1,
    ASTRA_RT_PARK_ACK = 2, ASTRA_RT_PARK_STALE = 3
};

typedef struct AstraRtBridge AstraRtBridge;
struct AvMidiEvent;
/* Native Astra VST ABI v1 process address. Never supply a foreign-language trampoline. */
typedef int32_t (ASTRA_RT_CALL *AstraRtVstProcess)(void*, float*, uint32_t,
    const struct AvMidiEvent*, uint32_t, int64_t, double, uint32_t);
typedef int (ASTRA_RT_CALL *AstraRtCallback)(const void*, void*, unsigned long,
    const void*, unsigned long, void*);
typedef void (ASTRA_RT_CALL *AstraRtDeviceReference)(void*);

/* Native layout, not packed or serialized. Set size=sizeof(record), version=1
   on each top-level input AND output record. Set input reserved fields to zero. */
typedef struct AstraRtConfig {
    uint32_t size, version, ramp_frames, reserved;
} AstraRtConfig;
typedef struct AstraRtTimelineSpec {
    uint32_t size, version;
    int64_t duration; /* exclusive, in prepared project samples */
    double bpm;
    uint32_t event_count, max_frames; /* max_frames <= the plugin's opened max block */
} AstraRtTimelineSpec;
typedef struct AstraRtEvent {
    int64_t sample;
    uint8_t status, data1, data2, reserved;
    uint32_t reserved2;
} AstraRtEvent;
typedef struct AstraRtGuard {
    uint64_t generation, phase, locate;
} AstraRtGuard;
typedef struct AstraRtCommand {
    uint32_t size, version, op, reserved;
    AstraRtGuard guard;
    int64_t a, b; /* seek: a; loop_on: [a,b); unused fields must be zero */
} AstraRtCommand;
typedef struct AstraRtSubmission {
    uint32_t size, version, code, reserved;
    uint64_t id, park_ticket; /* full/exhausted may request an emergency park */
} AstraRtSubmission;
typedef struct AstraRtReceipt {
    uint32_t size, version, result, reserved;
    uint64_t id;
    AstraRtGuard guard;
    int64_t position; /* immediately after command evaluation */
} AstraRtReceipt;
typedef struct AstraRtSnapshot {
    uint32_t size, version;
    AstraRtGuard guard; /* last published render guard; not the current UI phase */
    int64_t render_position, loop_begin, loop_end;
    uint64_t callbacks, output_frames, music_frames;
    uint64_t park_ticket, active_leases;
    uint32_t park_state, released;
    uint32_t fault;
    int32_t processor_code;
    uint32_t bound, playing, loop, bad_callback;
} AstraRtSnapshot;

ASTRA_RT_API uint32_t ASTRA_RT_CALL astra_rt_abi_version(void);

/* Control calls must run on the creating thread. Failures and EMPTY do not
   write outputs. A null spec with null events/plugin/process means unbound.
   Otherwise the events are copied, and plugin and process are borrowed. */
ASTRA_RT_API int32_t ASTRA_RT_CALL astra_rt_create(const AstraRtConfig*,
    const AstraRtTimelineSpec*, const AstraRtEvent*, void* plugin,
    AstraRtVstProcess process, AstraRtBridge** out);
ASTRA_RT_API int32_t ASTRA_RT_CALL astra_rt_destroy(AstraRtBridge*);
ASTRA_RT_API int32_t ASTRA_RT_CALL astra_rt_snapshot(AstraRtBridge*, AstraRtSnapshot*);
ASTRA_RT_API int32_t ASTRA_RT_CALL astra_rt_submit(AstraRtBridge*,
    const AstraRtCommand*, AstraRtSubmission*);
ASTRA_RT_API int32_t ASTRA_RT_CALL astra_rt_poll_receipt(AstraRtBridge*, AstraRtReceipt*);

ASTRA_RT_API int32_t ASTRA_RT_CALL astra_rt_request_park(AstraRtBridge*, uint64_t* out_ticket);
ASTRA_RT_API int32_t ASTRA_RT_CALL astra_rt_park_status(AstraRtBridge*, uint64_t ticket,
    uint32_t* out_state);
/* Change timeline and binding as one transaction. Null spec + null source fields
   detaches. Success returns the new staged generation and keeps the grant held. */
ASTRA_RT_API int32_t ASTRA_RT_CALL astra_rt_replace(AstraRtBridge*, uint64_t ticket,
    const AstraRtTimelineSpec*, const AstraRtEvent*, void* plugin,
    AstraRtVstProcess process, uint64_t* out_generation);
ASTRA_RT_API int32_t ASTRA_RT_CALL astra_rt_resume(AstraRtBridge*, uint64_t ticket);

/* Use the hooks with astra_host_create_owned only under its synchronous-retain,
   definitive-retirement contract. Only one non-reentrant render lane globally
   per bridge. These hooks never delete the bridge or call the plugin. */
ASTRA_RT_API void ASTRA_RT_CALL astra_rt_device_retain(void* user);
ASTRA_RT_API void ASTRA_RT_CALL astra_rt_device_release(void* user);
ASTRA_RT_API AstraRtCallback ASTRA_RT_CALL astra_rt_get_callback(void);
ASTRA_RT_API int ASTRA_RT_CALL astra_rt_render(const void*, void*, unsigned long,
    const void*, unsigned long, void* user);

#ifdef __cplusplus
}
#endif
