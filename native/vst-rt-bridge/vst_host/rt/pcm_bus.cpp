#include "pcm_bus.hpp"
#include <algorithm>
#include <cmath>
#include <stdexcept>
namespace astra::rt {
PcmBus::PcmBus(uint32_t n) {
    if(!n || n>32768) throw std::invalid_argument("invalid PCM bus block capacity");
    scratch_.resize(static_cast<size_t>(n)*2);
}
void PcmBus::bind(PcmSource s) noexcept {source_=s;continuous_=false;}
PcmRead PcmBus::add_to(float* out,uint64_t p,uint32_t n,bool playing,float gain,uint64_t landing) noexcept {
    if(!out || !n || n>scratch_.size()/2 || !std::isfinite(gain) || gain<0.f || gain>1.f || p>UINT64_MAX-n)
        return PcmRead::invalid;
    if(!playing || !source_.handle) {continuous_=false;return PcmRead::inactive;}
    if(!source_.request || !source_.read) return PcmRead::invalid;
    if(!continuous_ || (expected_!=p && requested_!=p)) {
        if(generation_==UINT64_MAX) return PcmRead::failed;
        ++generation_;
    }
    expected_=p+n;requested_=p;continuous_=true;
    if(source_.request(source_.handle,0,p,generation_)<=0) return PcmRead::failed;
    if(source_.hint && source_.hint(source_.handle,0,landing,generation_)<=0) return PcmRead::failed;
    int result=1;
    for(uint32_t copied=0;copied<n;) {
        const auto count=std::min<uint32_t>(n-copied,16384);
        result=source_.read(source_.handle,0,p+copied,count,scratch_.data()+2*copied);
        if(result<=0) break;
        copied+=count;
    }
    // Never add stale scratch if a provider misses or fails, even if it violates
    // the stream_cache promise to zero on miss. Existing VST output is preserved.
    if(result<=0) return result==0?PcmRead::missing:PcmRead::failed;
    for(size_t i=0;i<static_cast<size_t>(n)*2;++i) out[i]+=scratch_[i]*gain;
    return PcmRead::ready;
}
}
