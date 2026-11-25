#pragma once
#include <stdint.h>
#ifdef _WIN32
#define TC_API extern "C" __declspec(dllexport)
#else
#define TC_API extern "C"
#endif
using AstraTempoRead=int(*)(void*,uint32_t,uint64_t,uint32_t,float*);
using AstraTempoRequest=int(*)(void*,uint32_t,uint64_t,uint64_t);
using AstraTempoRef=void(*)(void*);
struct AstraTempoInfo{uint32_t size,version,state,sample_rate;uint64_t source_frames,output_frames,allocated_slots,rendered_blocks,misses,resets;double tempo;char error[256];};
// Worker-side creation. Input provider is immutable source-rate stereo PCM.
// Owns one input reference; final release stops and joins worker before releasing it.
// Input provider must be dedicated: only this worker publishes its requests.
TC_API void* astra_tempo_create(void* input,AstraTempoRead,AstraTempoRequest,AstraTempoRequest hint,AstraTempoRef retain,AstraTempoRef release,uint64_t source_frames,uint32_t rate,double tempo,AstraTempoInfo*);
// Callback-facing ABI matches core provider. Coordinates are OUTPUT frames.
// request/hint/read must share ONE callback thread. Every discontinuous seek
// changes generation. info is control-thread safe; final release is non-RT.
TC_API int astra_tempo_request(void*,uint32_t,uint64_t,uint64_t);
TC_API int astra_tempo_hint(void*,uint32_t,uint64_t,uint64_t);
TC_API int astra_tempo_read(void*,uint32_t,uint64_t,uint32_t,float*);
TC_API void astra_tempo_retain(void*);TC_API void astra_tempo_release(void*);TC_API void astra_tempo_close(void*);
TC_API int astra_tempo_info(void*,AstraTempoInfo*);
