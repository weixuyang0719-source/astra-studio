#pragma once
#include <algorithm>
#include <array>
#include <cmath>
#include <cstdint>
#include <stdexcept>
#include <vector>

namespace astra::rt {
struct ClickPoint { std::int64_t sample; bool accent; };
// Immutable control-thread preparation. Callback only binary-searches and mixes
// existing samples. Times are authoritative native project positions, not a clock.
class ClickBus {
    struct Pulse {std::int64_t start,end; unsigned accent;};
    std::vector<Pulse> pulses_;
    std::array<std::vector<float>,2> waves_;
public:
    ClickBus(double rate, std::int64_t duration, const std::vector<ClickPoint>& points) {
        if (!std::isfinite(rate)||rate<8000||rate>384000||duration<0||points.size()>100000)
            throw std::invalid_argument("invalid click plan");
        const auto length=static_cast<std::int64_t>(std::llround(.024*rate));
        for(unsigned a=0;a<2;++a) {
            auto& wave=waves_[a];wave.resize(static_cast<size_t>(length));
            const double hz=a?1300.:850., level=a?.32:.20;
            for(std::int64_t i=0;i<length;++i) {
                const double t=i/rate;
                const double env=std::min(t/.0015,1.)*std::exp(-t/.006)*std::min(((length-1-i)/rate)/.003,1.);
                wave[static_cast<size_t>(i)]=static_cast<float>(level*env*(.8*std::sin(6.283185307179586*hz*t)+.2*std::sin(6.283185307179586*1.7*hz*t)));
            }
        }
        pulses_.reserve(points.size());
        for(const auto& point:points) {
            if(point.sample<0||point.sample>=duration||(!pulses_.empty()&&point.sample<pulses_.back().start))
                throw std::invalid_argument("click points must be sorted and inside duration");
            if(!pulses_.empty()&&point.sample==pulses_.back().start){pulses_.back().accent|=point.accent?1u:0u;continue;}
            if(!pulses_.empty())pulses_.back().end=std::min(pulses_.back().end,point.sample);
            pulses_.push_back({point.sample,point.sample+std::min(length,duration-point.sample),point.accent?1u:0u});
        }
    }
    // Stateless lookup makes pause/seek/loop use exactly the host's sample span.
    // Caller supplies the existing gain ramp; no second timer/output owner.
    std::uint32_t add_to(float* out,std::int64_t position,std::uint32_t frames,float gain) const noexcept {
        if(!frames||gain==0)return 0;
        const auto end=position+frames;
        auto it=std::lower_bound(pulses_.begin(),pulses_.end(),position,[](const Pulse& p,std::int64_t at){return p.end<=at;});
        std::uint32_t onsets=0;
        for(;it!=pulses_.end()&&it->start<end;++it) {
            if(it->start>=position)++onsets;
            const auto begin=std::max(position,it->start),stop=std::min(end,it->end);
            const auto& wave=waves_[it->accent];
            for(auto at=begin;at<stop;++at){const auto j=static_cast<size_t>(at-position)*2;const auto v=gain*wave[static_cast<size_t>(at-it->start)];out[j]+=v;out[j+1]+=v;}
        }
        return onsets;
    }
    size_t size() const noexcept {return pulses_.size();}
};
}
