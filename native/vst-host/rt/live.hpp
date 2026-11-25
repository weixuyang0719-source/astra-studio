#pragma once
#include "kernel.hpp"
#include "live.h"

namespace astra::rt {
class Live final : public BoundaryProcessor {
public:
    Live(const AstraLiveConfig&, void*, AstraLiveProcess);
    ~Live();
    std::uint32_t max_frames() const noexcept;
    std::int32_t publish(const AstraLiveSpec&, const AstraRtEvent*, const AstraLiveClock*,
                         std::uint64_t& superseded, std::uint32_t& unsupported, const AstraLiveClick* clicks=nullptr, std::uint32_t click_count=0);
    std::int32_t submit(const AstraLiveCommand&) noexcept;
    bool poll(AstraLiveReceipt&) noexcept;
    void bind_pcm(const AstraLivePcmSource*);
    void park(Snapshot&) noexcept override;
    void render(Snapshot&, float*, std::uint32_t, bool usable) noexcept override;
private:
    struct Impl;
    std::unique_ptr<Impl> p_;
};
}
