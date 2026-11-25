#include "instrument_start.h"
#include <chrono>
#include <cmath>
#include <cstring>
#include <limits>

namespace {
using Clock=std::chrono::steady_clock;
uint64_t us(Clock::time_point a, Clock::time_point b){return std::chrono::duration_cast<std::chrono::microseconds>(b-a).count();}
constexpr uint32_t OK=0, INVALID=1, FORMAT=2, OTHER=3;
void text(char (&out)[512],const char* msg){if(!msg)msg="Instrument preparation failed";std::strncpy(out,msg,sizeof(out)-1);out[sizeof(out)-1]=0;}
}

// Called synchronously ONLY by the dedicated candidate's vendor-lifecycle owner, before device admission.
// The VST host owns construction/tear-down; never pass partially built objects between threads or processes.
void* __cdecl astra_instrument_start_v1(AvOpen open,AvError error,const char* module,const char* cid,
 double rate,uint32_t wide,uint32_t requested,uint32_t observed,AvInstrumentStartV1* out) noexcept {
 if(!out || out->bytes!=sizeof(*out) || out->version!=1)return nullptr;
 *out={sizeof(*out),1};out->result=INVALID;
 if(!open||!error||!module||!*module||!cid||!*cid||!std::isfinite(rate)||rate<8000||rate>384000||
    wide<1||wide>16384||requested>32768||observed>32768){
    text(out->error,"Invalid instrument preparation contract");return nullptr;}
 if(requested>wide){text(out->error,"Requested device block exceeds instrument host capacity");return nullptr;}
 uint32_t caps[2]={wide,0};
 // Only an explicit, matching fixed device request is eligible. Auto/unknown must keep headroom.
 if(requested>=1 && requested<=1024) {
    if(observed>=requested && observed<=wide){caps[0]=observed; caps[1]=(observed!=wide)?wide:0;}
    else if(!observed && requested<wide) caps[1]=requested;
 }
 const auto began=Clock::now();
 for(uint32_t i=0;i<2 && caps[i];++i){
    out->attempts=i+1;out->capacity[i]=caps[i];const auto before=Clock::now();
    void* plugin=nullptr;
    try {plugin=open(module,cid,rate,caps[i]);} catch(...) {
      out->attempt_us[i]=us(before,Clock::now());out->outcome[i]=out->result=OTHER;
      text(out->error,"Instrument preparation failed");break;
    }
    const auto after=Clock::now();out->attempt_us[i]=us(before,after);
    if(plugin){out->outcome[i]=out->result=OK;out->chosen=caps[i];out->error[0]=0;out->total_us=us(began,after);return plugin;}
    const char* reason=error();
    // Stable, exact error emitted by the pinned native host when IAudioProcessor::setupProcessing rejects.
    // No retry on crashes, startup timeouts, generic init failures, missing classes, activation, or state failures.
    out->outcome[i]=out->result=reason&&std::strcmp(reason,"Plugin setupProcessing failed")==0 ? FORMAT : OTHER;
    text(out->error,reason);
    if(out->result!=FORMAT)break;
 }
 out->total_us=us(began,Clock::now());return nullptr;
}
