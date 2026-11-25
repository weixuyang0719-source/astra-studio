#pragma once
#include "portaudio_host.h"
struct AstraWasapiInfo { uint32_t size,version,rate,channels,bits,is_float,buffer_frames,raw; int32_t fallback_error; uint32_t valid_bits,channel_mask,reserved; double clock_frequency; wchar_t endpoint[512]; char name[256]; };
AH_API int astra_wasapi_probe(const wchar_t* endpoint,AstraWasapiInfo*);
AH_API void* astra_wasapi_create_owned(AstraPaCallback,void*,AstraHostReference,AstraHostReference);
AH_API int astra_wasapi_open(void*,const wchar_t* endpoint,uint32_t expected_rate,uint32_t period_frames,int raw_preferred);
AH_API int astra_wasapi_snapshot(void*,AstraHostSnapshot*);
AH_API int astra_wasapi_info(void*,AstraWasapiInfo*);
AH_API int astra_wasapi_route(void*,AstraHostRoute*);
AH_API int astra_wasapi_close(void*);
AH_API int astra_wasapi_destroy(void*);
