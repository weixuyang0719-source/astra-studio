#pragma once
#include <array>
#include <algorithm>
#include <cstdint>
namespace astra::rt {
// Finite last-sample bridge, not a natural tail or a delayed transport.
// Callback-owned, fixed storage. The current musical time/receipts never wait.
class TransportDeclick {
    std::array<float,2> last_{},from_{};
    uint32_t length_,segment_=0,left_=0;
    bool valid_=false;
public:
    explicit TransportDeclick(double rate):length_(std::max(1u,static_cast<uint32_t>(rate*.005))){}
    void clear() noexcept {last_={};from_={};left_=0;valid_=false;}
    void arm() noexcept {if(valid_){from_=last_;if(!left_)left_=length_;segment_=left_;}}
    void frame(float* sample) noexcept {
        if(left_){
            const double old=segment_>1?static_cast<double>(left_-1)/(segment_-1):0.;
            for(unsigned c=0;c<2;++c)sample[c]=static_cast<float>(from_[c]*old+sample[c]*(1.-old));
            --left_;
        }
        last_={sample[0],sample[1]};valid_=true;
    }
};
}
