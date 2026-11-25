#pragma once
#include <cstdint>
#include <vector>

namespace astra::rt {
// Native stream_cache ABI functions only. No Python trampolines.
struct PcmSource {
    void* handle=nullptr;
    int (*request)(void*,uint32_t,uint64_t,uint64_t)=nullptr;
    int (*read)(void*,uint32_t,uint64_t,uint32_t,float*)=nullptr;
    int (*hint)(void*,uint32_t,uint64_t,uint64_t)=nullptr;
};
enum class PcmRead { inactive, ready, missing, failed, invalid };
class PcmBus {
public:
    // Allocate once on control thread; device callbacks must not exceed max_frames.
    explicit PcmBus(uint32_t max_frames);
    // Control thread only, under actual callback exclusion. Caller owns source,
    // holds its native retain until final callback return, then releases off RT.
    void bind(PcmSource source) noexcept;
    // Single RT callback lane only. position is the SAME project/device-rate
    // sample as the VST span; stopped transport must pass playing=false.
    // Adds source bus only: caller applies master/mute and stop/seek ramps.
    PcmRead add_to(float* output, uint64_t position, uint32_t frames,
                   bool playing, float source_gain, uint64_t landing=UINT64_MAX) noexcept;
private:
    PcmSource source_{};
    std::vector<float> scratch_;
    uint64_t generation_=1, expected_=0, requested_=0;
    bool continuous_=false;
};
}
