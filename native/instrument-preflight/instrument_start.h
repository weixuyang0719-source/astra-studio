#pragma once
#include <cstdint>
extern "C" {
using AvOpen = void* (__cdecl *)(const char*, const char*, double, uint32_t);
using AvError = const char* (__cdecl *)();
struct AvInstrumentStartV1 {
    uint32_t bytes, version, attempts, chosen, result, capacity[2], outcome[2], reserved;
    uint64_t attempt_us[2], total_us;
    char error[512];
};
__declspec(dllexport) void* __cdecl astra_instrument_start_v1(AvOpen, AvError, const char*, const char*,
    double, uint32_t wide, uint32_t requested, uint32_t observed_same_route, AvInstrumentStartV1*) noexcept;
}
