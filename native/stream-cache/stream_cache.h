#pragma once
#include <stdint.h>
#ifdef _WIN32
#define SC_API extern "C" __declspec(dllexport)
#else
#define SC_API extern "C"
#endif
struct AstraStreamCacheInfo {uint32_t size,version,state,sample_rate,source_rate,channels,block_frames,slots;uint64_t frames,allocated_bytes,decoded_blocks,misses,discarded;char error[256];};
// Non realtime creation. target PCM = interleaved stereo float32. Allocated bytes
// includes fixed worker scratch and a 4MiB reserve for decoder/resampler internals,
// not an OS memory quota. Supports source 0; rejects other source IDs.
SC_API void* astra_cache_create(const wchar_t* sndfile_dll,const wchar_t* samplerate_dll,const wchar_t* file,uint32_t target_rate,uint64_t budget,AstraStreamCacheInfo*);
// RT: single publisher, normally core callback. Latest request wins; generation
// must change on discontinuous seeks. Only atomic stores, no lock/IO/notification.
SC_API int astra_cache_request(void*,uint32_t source,uint64_t frame,uint64_t generation);
// RT optional secondary landing hint; UINT64_MAX clears it. Same publisher as request.
// Only hints matching the primary generation are retained by the worker.
SC_API int astra_cache_hint(void*,uint32_t source,uint64_t frame,uint64_t generation);
// RT independent pending-seek landing; UINT64_MAX clears it.
SC_API int astra_cache_prefetch(void*,uint32_t source,uint64_t frame,uint64_t generation);
// RT: bounded atomic pin/copy/unpin, no waits/allocations. 1 ready, 0 miss, -1 closed/error.
// On miss entire destination is zero. EOF zero-padding is successful. Never retain slot pointers.
SC_API int astra_cache_read(void*,uint32_t source,uint64_t frame,uint32_t count,float* stereo);
// Check a bounded read-ahead window without copying its PCM or counting a miss.
SC_API int astra_cache_ready(void*,uint32_t source,uint64_t frame,uint32_t count);
SC_API int astra_cache_info(void*,AstraStreamCacheInfo*);
SC_API void astra_cache_close(void*);
// Non realtime references. Final release requests close and joins the worker.
// The engine must retain throughout all callbacks, release only after callback ownership ends.
SC_API void astra_cache_retain(void*);
SC_API void astra_cache_release(void*);
