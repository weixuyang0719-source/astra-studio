#pragma once
#include "live.h"
#include <algorithm>
#include <array>
#include <cmath>
#include <cstdint>
#include <stdexcept>
#include <vector>

namespace astra::rt {
struct ClickPoint { std::int64_t sample; bool accent; uint32_t rate_gate=0; };
// Immutable control-thread preparation. Callback only binary-searches and mixes
// existing samples. Times are authoritative native project positions, not a clock.
class ClickBus {
    struct Pulse {
        std::int64_t start,end; unsigned accent; uint32_t gate;
        bool enabled(uint32_t step) const noexcept {
            if(!gate)return true;
            return (gate & 0x80000000u) ? step<(gate & 0xffffu) : step>=(gate & 0xffffu);
        }
    };
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
            if(!pulses_.empty()&&point.sample==pulses_.back().start&&point.rate_gate==pulses_.back().gate){
                pulses_.back().accent|=point.accent?1u:0u;continue;
            }
            if(!pulses_.empty())pulses_.back().end=std::min(pulses_.back().end,point.sample);
            pulses_.push_back({point.sample,point.sample+std::min(length,duration-point.sample),point.accent?1u:0u,point.rate_gate});
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
            if(!it->enabled(ASTRA_LIVE_RATE_ONE))continue;
            if(it->start>=position)++onsets;
            const auto begin=std::max(position,it->start),stop=std::min(end,it->end);
            const auto& wave=waves_[it->accent];
            for(auto at=begin;at<stop;++at){const auto j=static_cast<size_t>(at-position)*2;const auto v=gain*wave[static_cast<size_t>(at-it->start)];out[j]+=v;out[j+1]+=v;}
        }
        return onsets;
    }
    struct Cursor { uint32_t offset=UINT32_MAX, accent=0; };

    // Pulse times follow the project clock; the sound always advances one
    // waveform sample per hardware frame, even across a live rate change.
    void add_retimed(float* out, uint32_t n, std::int64_t p, uint32_t phase, uint32_t step,
                     std::int64_t first, std::int64_t before, Cursor& cursor, bool reset) const noexcept {
        constexpr auto unit=static_cast<std::int64_t>(ASTRA_LIVE_RATE_ONE);
        auto it=std::lower_bound(pulses_.begin(),pulses_.end(),first,[](const Pulse& pulse,std::int64_t at){return pulse.start<at;});
        const auto length=static_cast<uint32_t>(waves_[0].size());
        if(reset){
            cursor.offset=length;
            auto candidate=it;
            while(candidate!=pulses_.begin()){
                const auto& prior=*--candidate;
                const auto distance=p-prior.start;
                if(distance>3*static_cast<int64_t>(length) || distance*unit+phase>=static_cast<int64_t>(length)*step)break;
                if(!prior.enabled(step))continue;
                cursor.offset=static_cast<uint32_t>((distance*unit+phase)/step);
                cursor.accent=prior.accent;
                break;
            }
        }
        uint32_t written=0;
        const auto mix_until=[&](uint32_t end){
            const auto& wave=waves_[cursor.accent];
            for(;written<end&&cursor.offset<length;++written,++cursor.offset){
                const auto value=wave[cursor.offset];
                out[2*written]+=value;out[2*written+1]+=value;
            }
            written=end;
        };
        for(;it!=pulses_.end()&&it->start<before;++it){
            if(!it->enabled(step))continue;
            const auto ticks=(it->start-p)*unit-phase;
            mix_until(ticks>0?static_cast<uint32_t>(ticks/step):0);
            cursor={0,it->accent};
        }
        mix_until(n);
    }

    // Lead-in is physically timed even while the project cursor is frozen.
    // Counted pulses and their tails use the exact same prepared waveform.
    uint32_t wave_frames() const noexcept {return static_cast<uint32_t>(waves_[0].size());}
    void add_count(float* out,uint32_t n,int64_t played,uint64_t step,uint32_t count,uint32_t accent,
                   float gain) const noexcept {
        for(uint32_t i=0;i<count;++i){
            const auto onset=static_cast<int64_t>((i*step+ASTRA_LIVE_RATE_ONE/2)/ASTRA_LIVE_RATE_ONE);
            const auto& wave=waves_[i%accent?0:1];
            const auto end=onset+static_cast<int64_t>(wave.size());
            if(end<=played)continue;
            if(onset>=played+n)break;
            for(auto at=std::max(played,onset);at<std::min(end,played+n);++at){
                const auto x=gain*wave[static_cast<size_t>(at-onset)];
                const auto j=static_cast<size_t>(at-played)*2;
                out[j]+=x;out[j+1]+=x;
            }
        }
    }
    size_t size() const noexcept {return pulses_.size();}
};
}
