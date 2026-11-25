#pragma once
#include <stdint.h>
#define WM_API extern "C" __declspec(dllexport)
struct AstraWinmmStatus{uint32_t size,version,open,borrowers;int32_t error,reserved;wchar_t name[128];};
// create loads only Windows system WinMM function table; does NOT enumerate/open.
WM_API void* astra_winmm_create();
// Explicit open: only verified local VirtualMIDISynth / Microsoft GS software synth.
// requested_name null selects best known candidate. Never MIDI_MAPPER/hardware.
WM_API int astra_winmm_open(void*,const wchar_t* requested_name);
WM_API int astra_winmm_send(void*,uint32_t packed); // compatible scheduler send callback
// Borrow for scheduler lifetime via create_owned. Base owner must call destroy,
// not release. Close refuses while borrowed; no midiOutReset or CC120 hard kill.
WM_API void astra_winmm_retain(void*);
WM_API void astra_winmm_release(void*);
// Error-only panic, after scheduler stopped/released borrower. Failure retains handle.
WM_API int astra_winmm_panic(void*);
WM_API int astra_winmm_close(void*);
WM_API int astra_winmm_snapshot(void*,AstraWinmmStatus*);
WM_API int astra_winmm_destroy(void*); // refuses open handle/borrowers

// Cumulative QPC driver-call timings only (not queue/lock wait or sound latency).
// Lock-free reads: fields can advance independently while a call is in flight.
struct AstraWinmmDiagnostics {
 uint32_t size,version;
 uint64_t send_calls,send_completed,send_errors,send_total_ns,send_max_ns;
 uint64_t reset_calls,reset_completed,reset_errors,reset_total_ns,reset_max_ns;
};
WM_API int astra_winmm_diagnostics(void*,AstraWinmmDiagnostics*);
