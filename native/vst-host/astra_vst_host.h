#pragma once
#include <stdint.h>
#ifdef _WIN32
#define AV_API __declspec(dllexport)
#else
#define AV_API
#endif
#ifdef __cplusplus
extern "C" {
#endif
/* ABI v1. All calls except process run on the creating/UI thread. Caller must
   serialize process with state changes and destruction. One process call at a time.
   This module owns no audio device. Never load untrusted plugins in the UI process. */
typedef struct AvMidiEvent {
    uint32_t sample_offset;
    uint8_t status, data1, data2, reserved;
} AvMidiEvent;
AV_API uint32_t astra_vst_abi_version(void);
AV_API const char* astra_vst_last_error(void);
/* Returns required UTF-8 JSON bytes INCLUDING NUL; 0 on failure. A too-small
   buffer is untouched. Metadata only: does not instantiate a plugin class. */
AV_API uint64_t astra_vst_scan(const char* module_path, char* json, uint64_t capacity);
AV_API void* astra_vst_open(const char* module_path, const char* class_id,
                           double sample_rate, uint32_t max_block);
/* Returns 0 on success, negative on error. Output is stereo interleaved f32.
   MIDI offsets must be < frames and sorted. Supported notes, CC, pitchbend,
   poly pressure/channel pressure. CC maps through IMidiMapping, never guessed. */
AV_API int32_t astra_vst_process(void* handle, float* stereo_output, uint32_t frames,
    const AvMidiEvent* events, uint32_t count, int64_t sample_position,
    double tempo, uint32_t playing);
/* State is an Astra envelope of component + controller opaque byte streams.
   get_state returns required bytes, 0 on failure. set_state validates envelope. */
AV_API uint64_t astra_vst_get_state(void* handle, uint8_t* output, uint64_t capacity);
AV_API int32_t astra_vst_set_state(void* handle, const uint8_t* data, uint64_t size);
AV_API uint32_t astra_vst_latency(void* handle);
/* UI must be attached/removed on the creation thread. Parent is HWND on Windows.
   0 success, 1 no editor, negative error. Closing editor does NOT unload plugin. */
AV_API int32_t astra_vst_editor_attach(void* handle, void* parent_hwnd, int32_t* width, int32_t* height);
AV_API int32_t astra_vst_editor_resize(void* handle, int32_t width, int32_t height);
AV_API int32_t astra_vst_editor_remove(void* handle);
AV_API void astra_vst_close(void* handle);
/* Atomic handler notification generation, read only while handle is alive. */
AV_API uint64_t astra_vst_state_generation(void* handle);

/* Additive native extension. ABI 1 records, exports and version are unchanged.
   These records are local to the worker, not IPC. */
#define AV_RT_VERSION 2u
#define AV_RT_MIDI_LIMIT 16384u
enum AvRtFlags { AV_RT_PLAYING = 1, AV_RT_CLOCK = 2, AV_RT_CYCLE = 4, AV_RT_METER = 8 };
typedef struct AvRtMidiEvent {
    uint32_t sample_offset;
    uint8_t status, data1, data2, live;
    int32_t note_id; /* -1 if there is no unambiguous identity */
} AvRtMidiEvent;
typedef struct AvRtContext {
    int64_t sample_position;
    double beat, bpm, bar_start_beat, cycle_start_beat, cycle_end_beat;
    int32_t numerator, denominator;
    uint32_t flags, reserved;
} AvRtContext;
AV_API uint32_t astra_vst_rt_version(void);
AV_API int32_t astra_vst_rt_configuration(void*, double* rate, uint32_t* max_block);
AV_API int32_t astra_vst_process_rt(void*, float*, uint32_t, const AvRtMidiEvent*,
                                   uint32_t, const AvRtContext*);
/* Control thread only, AFTER definitive device retirement. Reactivates the
   existing instance to discard stream DSP state, without replacing its state
   envelope or editor. Failure requires an explicit plugin reopen. */
AV_API int32_t astra_vst_rt_rearm(void*);
#ifdef __cplusplus
}
#endif

#ifdef __cplusplus
extern "C" {
#endif
/* Ordered stereo instrument inserts. Control calls remain on creating thread. */
typedef struct AvInsertStatus {uint32_t size,version;uint64_t applied_revision,pending_revision;uint32_t latency_samples,count;} AvInsertStatus;
AV_API void* astra_vst_open_effect(const char*,const char*,double,uint32_t);
AV_API uint64_t astra_vst_set_inserts(void*,void* const*,const uint32_t*,uint32_t,uint64_t);
AV_API int32_t astra_vst_inserts_status(void*,AvInsertStatus*);

#ifdef __cplusplus
}
#endif
