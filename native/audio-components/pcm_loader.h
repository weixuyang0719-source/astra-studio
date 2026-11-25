#pragma once
#include <stdint.h>
#ifdef _WIN32
#define PCM_API extern "C" __declspec(dllexport)
#else
#define PCM_API extern "C"
#endif
// All functions are NON realtime. Caller runs load on a worker, never callback.
// Input DLL/audio paths must be absolute. Output PCM is immutable stereo float32,
// at the file's ORIGINAL sample rate: no hidden resampling or normalization.
struct AstraPcmInfo {
 uint32_t size,version; uint64_t frames,bytes; uint32_t sample_rate,source_channels,output_channels,source_sample_rate;
 char error[256];
};
// Status 0 success, 1 canceled, 2 budget exceeded, 3 invalid input/format,
// 4 decoder/DLL failure, 5 read failure, 6 allocation failure.
PCM_API uint32_t astra_pcm_abi_version();
PCM_API void* astra_pcm_cancel_create();
PCM_API void astra_pcm_cancel_request(void* token);
// Destroy token only after every load using it has returned.
PCM_API void astra_pcm_cancel_destroy(void* token);
PCM_API int astra_pcm_load(const wchar_t* dll,const wchar_t* file,uint64_t budget_bytes,void* cancel,void** result,AstraPcmInfo* info);
// Borrowed pointer: valid until astra_pcm_release. Do not mutate/free or release
// while another thread/core still reads it. Core may copy then release safely.
PCM_API const float* astra_pcm_data(void* result);
PCM_API void astra_pcm_release(void* result);
// Optional high-quality NON realtime conversion through libsamplerate SRC_SINC_BEST_QUALITY.
// Budget accounts for simultaneous input/output PCM plus a fixed decoder reserve;
// it is not an OS process-memory quota for DLL internals. Cancellation is checked between blocks.
PCM_API int astra_pcm_load_resampled(const wchar_t* sndfile_dll,const wchar_t* samplerate_dll,const wchar_t* file,uint32_t target_rate,uint64_t budget_bytes,void* cancel,void** result,AstraPcmInfo* info);
