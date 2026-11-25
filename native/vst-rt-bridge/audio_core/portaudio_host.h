#pragma once
#include <stdint.h>
#ifdef _WIN32
#define AH_API extern "C" __declspec(dllexport)
#else
#define AH_API extern "C"
#endif
// ABI 1. No STL or exceptions cross this boundary. All calls except callback are non-real-time.
// core_user is borrowed until snapshot.released==1 and destroy succeeds.
// The callback MUST be a native function pointer, not a Python/ctypes trampoline.
typedef int (*AstraPaCallback)(const void*,void*,unsigned long,const void*,unsigned long,void*);
struct AstraHostSnapshot {
 uint32_t size,version,state,released;
 int32_t error;
 uint32_t observed_frames;
 uint64_t callbacks,status_flags;
 double sample_rate,output_latency,current_time,dac_time;
 char device[256],message[512];
};
struct AstraHostDevice {
 uint32_t size,version; int32_t index,host_type,outputs,inputs;
 double default_rate,low_latency; char name[256],host[128];
};
struct AstraHostRoute {
 uint32_t size,version; int32_t host_type,device_index,shared,raw;
 char host[128];
};
// create only starts the owner thread. Loading the specified trusted DLL and driver enumeration
// happens there; callers must obtain authorization before create. No implicit driver fallback.
AH_API void* astra_host_create(const wchar_t* dll,AstraPaCallback callback,void* core_user);
typedef void (*AstraHostReference)(void*);
AH_API void* astra_host_create_owned(const wchar_t* dll,AstraPaCallback callback,void* core_user,AstraHostReference retain,AstraHostReference release);
AH_API int astra_host_open(void*,const char* exact_driver,double rate,uint32_t block);
// host_type is 3=ASIO or 13=WASAPI shared. No exclusive/RAW mode is requested.
// Empty/null name selects that host API's default output. Exact duplicate names are rejected.
AH_API int astra_host_open_backend(void*,int32_t host_type,const char* exact_device,double rate,uint32_t block);
AH_API int astra_host_route(void*,AstraHostRoute*);
AH_API int astra_host_close(void*);
AH_API int astra_host_snapshot(void*,AstraHostSnapshot*);
AH_API int astra_host_device_count(void*);
AH_API int astra_host_device(void*,uint32_t,AstraHostDevice*);
// Nonblocking: returns 0 while ownership is unresolved. Never unloads a live callback.
AH_API int astra_host_destroy(void*);
struct AstraAsioControl { uint32_t size,version,state; int32_t error,min_frames,max_frames,preferred_frames,granularity; uint64_t request_id,completed_id; char device[256]; };
// No stream is opened by this owner. Caller must retire all previous ASIO owners first.
AH_API void* astra_host_control_create(const wchar_t* dll);
AH_API int astra_host_asio_request(void*,const char* exact_driver,uintptr_t parent_window,int show_panel);
AH_API int astra_host_asio_control(void*,AstraAsioControl*);
