// Native callback-owned transport. Immutable interleaved stereo PCM, 48kHz.
// No Python, locks, allocation, file I/O or driver calls inside process().
#ifdef _WIN32
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#endif
#include <atomic>
#include <cstdint>
#include <cstdlib>
#include <cmath>
#include <algorithm>
#include <vector>
#include <new>
#include <chrono>
#include <cstring>
#include <memory>
#include <mutex>
#include <unordered_map>
#include "midi_scheduler.h"
#ifdef _WIN32
#define API extern "C" __declspec(dllexport)
#else
#define API extern "C"
#endif
static_assert(std::atomic<uint64_t>::is_always_lock_free,"native callback requires lock-free 64-bit atomics");
struct Command { int kind; double a,b; };
static int64_t clock_ns(){return std::chrono::duration_cast<std::chrono::nanoseconds>(std::chrono::steady_clock::now().time_since_epoch()).count();}
struct Mapping {std::atomic<uint64_t> version{0},first{0},last{0},epoch{0},transition{0};std::atomic<uint32_t> source{0},transport_active{0};std::atomic<int64_t> began{0},ended{0};};
using ProviderRead=int(*)(void*,uint32_t,uint64_t,uint32_t,float*);
using ProviderRequest=int(*)(void*,uint32_t,uint64_t,uint64_t);
using ProviderReference=void(*)(void*);
using ProviderReady=int(*)(void*,uint32_t,uint64_t,uint32_t);
struct AuditionProvider {
 void* handle;
 ProviderRead read;
 ProviderRequest request, hint, prefetch;
 ProviderReference retain, release;
 ProviderReady ready;
};
struct AuditionTransition {
 uint64_t sequence=0, frame=UINT64_MAX;
 uint32_t source=0;
};
struct DacClockAnchor {
 bool calibrated=false;double host_anchor=0,last_host=0;int64_t steady_anchor=0;
 std::atomic<uint64_t> resets{0};double qpc_anchor=0;int64_t qpc_steady=0;bool qpc_valid=false;
 DacClockAnchor(){
#ifdef _WIN32
  LARGE_INTEGER q,f;auto a=clock_ns();if(QueryPerformanceFrequency(&f)&&QueryPerformanceCounter(&q)&&f.QuadPart>0){auto z=clock_ns();qpc_anchor=double(q.QuadPart)/double(f.QuadPart);qpc_steady=a+(z-a)/2;qpc_valid=true;}
#endif
 }
 int64_t map(double current,double dac,int64_t observed) noexcept {
  double latency=dac-current;
  if(!std::isfinite(current)||!std::isfinite(dac)||current<0||!std::isfinite(latency)||latency<0||latency>=5){if(calibrated)resets.fetch_add(1,std::memory_order_relaxed);calibrated=false;return observed;}
  // Audio callbacks are a single producer. A non-advancing/backwards host clock
  // denotes a restarted or invalid clock domain, not ordinary callback jitter.
  if(!calibrated||current<=last_host){if(calibrated)resets.fetch_add(1,std::memory_order_relaxed);host_anchor=current;steady_anchor=observed;calibrated=true;}
  last_host=current;double delta=(dac-host_anchor)*1e9;
  if(!std::isfinite(delta)||std::abs(delta)>double(INT64_MAX)/4){calibrated=false;resets.fetch_add(1,std::memory_order_relaxed);return observed;}
  return steady_anchor+int64_t(std::llround(delta));
 }
 int64_t map_qpc(double current,double dac,int64_t observed) noexcept {
  if(!qpc_valid||!std::isfinite(current)||!std::isfinite(dac)||dac<current||dac-current>=5)return map(current,dac,observed);
  double delta=(dac-qpc_anchor)*1e9;if(!std::isfinite(delta)||std::abs(delta)>double(INT64_MAX)/4)return observed;
  return qpc_steady+int64_t(std::llround(delta));
 }
};
struct WebControls {
 uint64_t serial=0, command=0, seek=0, ingress_ns=0;
 double target=0;
 uint32_t source=0, playing=0;
};
struct Core {
 DacClockAnchor dac_clock;
 Mapping maps[512];std::atomic<uint64_t> map_count{0};uint64_t epoch=0;double rate=48000;unsigned fade_frames=240;double tempo_ratio=1;uint64_t original_frames=0;
 void* provider=nullptr;ProviderRead provider_read=nullptr;ProviderRequest provider_request=nullptr,provider_hint=nullptr;ProviderReference provider_release=nullptr;
 std::vector<AuditionProvider> sources;
 AuditionTransition pending;
 uint32_t source=0;
 uint64_t transition=0, peer_window=UINT64_MAX, peer_epoch=UINT64_MAX;
 std::vector<float> old_tail, destination;
 unsigned audition_blend=0;
 float peak=0;
 uint64_t overrange=0;
 std::atomic<uint64_t> published_overrange{0};
 std::atomic<float> published_peak{0};
 float block[512]{};uint64_t block_first=0;uint32_t block_count=0;bool provider_missed=false;
 float last_output[2]{},stall_from[2]{},recover_from[2]{};unsigned stall_remaining=0,recover_remaining=0;
 std::atomic<uint64_t> cache_misses{0};std::atomic<int> cache_error{0};
 std::vector<float> pcm; uint64_t frames; std::atomic<unsigned> refs{1};
 std::mutex control_mutex;  // WebChannel and the lifecycle worker are control producers.
 WebControls web;
 bool control_playing=false;
 uint32_t control_source=0;
 Command commands[256]{}; std::atomic<unsigned> head{0},tail{0};
 uint64_t cursor=0; bool playing=false; double gain=1,target_gain=1,level=0;
 uint64_t left=0,right=0; bool looping=false;
 float last[2]{}, old[2]{}; unsigned blend=0;
 std::atomic<uint64_t> pos{0}, rendered{0},applied{0},published_epoch{0}; std::atomic<int> running{0};
 explicit Core(const float* p,uint64_t n):pcm(p,p+n*2),frames(n){}
 Core(void* u,ProviderRead r,ProviderRequest q,ProviderReference retain,ProviderReference release,uint64_t n,double hz):provider(u),provider_read(r),provider_request(q),provider_release(release),frames(n){rate=hz;fade_frames=std::max(1u,unsigned(std::llround(rate*.005)));retain(provider);}
 Core(const AuditionProvider* inputs,uint32_t count,uint32_t initial,uint64_t n,double hz)
   :sources(inputs,inputs+count),source(initial),frames(n) {
  rate=hz;
  fade_frames=std::max(1u,unsigned(std::llround(rate*.005)));
  old_tail.resize(fade_frames*2);
  destination.resize((fade_frames+256)*2);
  for(auto& input:sources) input.retain(input.handle);
  select_provider(initial);control_source=initial;
 }
 ~Core(){
  if(sources.empty()){if(provider_release)provider_release(provider);}
  else for(auto& input:sources)input.release(input.handle);
 }
 void select_provider(uint32_t index) noexcept {
  const auto& input=sources[index];
  source=index;
  provider=input.handle;provider_read=input.read;
  provider_request=input.request;provider_hint=input.hint;
  block_count=0;
 }
 void stage_transition(uint64_t sequence,double next_source,double frame) noexcept {
  const auto index=next_source<0?(pending.sequence?pending.source:source):uint32_t(next_source);
  const auto target=frame<0?(pending.sequence?pending.frame:UINT64_MAX):bound(frame);
  if(pending.sequence && pending.source!=index)
   sources[pending.source].prefetch(sources[pending.source].handle,0,UINT64_MAX,0);
  pending={sequence,target,index};
 }
 bool tail_ready() noexcept {
  // Follow the old source clock, including a loop boundary inside the fade.
  auto at=cursor;
  unsigned copied=0;
  while(copied<fade_frames){
   if(at>=frames){
    std::fill(old_tail.begin()+copied*2,old_tail.end(),0.f);
    break;
   }
   if(looping && ((left<right && at>=right) || (left>right && at>=right && at<left)))at=left;
   const auto end=looping && at<right?right:frames;
   const auto count=uint32_t(std::min<uint64_t>(fade_frames-copied,end-at));
   if(provider_read(provider,0,at,count,old_tail.data()+copied*2)!=1)return false;
   if(!looping || left>right){
    for(unsigned j=0;j<count;++j){
     const auto scale=float(std::min(1.,(frames-at-j)/double(fade_frames)));
     old_tail[2*(copied+j)]*=scale;
     old_tail[2*(copied+j)+1]*=scale;
    }
   }
   at+=count;copied+=count;
  }
  return true;
 }
 void commit_transition() noexcept {
  if(!pending.sequence || audition_blend || (!playing && level>0))return;
  auto& target=sources[pending.source];
  const auto frame=pending.frame==UINT64_MAX?cursor:pending.frame;
  if(frame!=cursor || pending.source!=source){
   target.prefetch(target.handle,0,frame,epoch);
   // Verify a quarter second of decoded read-ahead (at most two cache
   // blocks) before exchanging sources. This never queues extra DAC frames.
   const auto ahead=unsigned(std::min(16384.,rate*.25));
   const auto available=target.ready(target.handle,0,frame,ahead);
   if(available<0)cache_error.store(available,std::memory_order_relaxed);
   if(available!=1)return;
   const auto count=unsigned(std::min<uint64_t>(fade_frames+256,frames-frame));
   const int result=count?target.read(target.handle,0,frame,count,destination.data()):1;
   if(result<0)cache_error.store(result,std::memory_order_relaxed);
   if(result!=1 || (level>0 && !tail_ready()))return;
   audition_blend=level>0?fade_frames:0;
   cursor=frame;++epoch;
   select_provider(pending.source);
   provider_request(provider,0,cursor,epoch);
   block_first=cursor;
   block_count=std::min(count,256u);
   std::copy_n(destination.data(),block_count*2,block);
   blend=0;
  }
  target.prefetch(target.handle,0,UINT64_MAX,epoch);
  transition=pending.sequence;pending={};
 }
 void prefetch_aligned_sources() noexcept {
  if(!playing || sources.size()<2)return;
  const auto window=cursor/std::max(1u,unsigned(rate*.125));
  if(window==peer_window && epoch==peer_epoch)return;
  peer_window=window;peer_epoch=epoch;
  for(uint32_t index=0;index<sources.size();++index){
   if(index==source || (pending.sequence && index==pending.source))continue;
   const auto& peer=sources[index];
   // Reuse the bounded read-ahead lane. Only the active source controls
   // transport and active playback retains its own cache priority.
   peer.prefetch(peer.handle,0,cursor,epoch);
  }
 }
 bool fetch(uint64_t frame,unsigned& retry_budget) noexcept {
  if(!provider)return true;
  if(block_count && frame>=block_first && frame<block_first+block_count)return true;
  if(!retry_budget)return false;--retry_budget;
  provider_request(provider,0,frame,epoch);
  auto count=static_cast<uint32_t>(std::min<uint64_t>(256,frames-frame));
  int result=provider_read(provider,0,frame,count,block);
  if(result!=1){block_count=0;cache_misses.fetch_add(1,std::memory_order_relaxed);if(result<0)cache_error.store(result,std::memory_order_relaxed);retry_budget=0;return false;}
  block_first=frame;block_count=count;return true;
 }
 bool push(Command c) { auto h=head.load(std::memory_order_relaxed); if(h-tail.load(std::memory_order_acquire)>=256)return false; commands[h%256]=c;head.store(h+1,std::memory_order_release);return true; }
 // Call only with control_mutex held. The realtime consumer still reads
 // the existing single ordered ring and never takes this mutex.
 uint64_t submit_control(Command command) noexcept {
  if(!push(command))return 0;
  if(command.kind==1)control_playing=command.a!=0;
  if(command.kind>=6){
   if(command.a>=0)control_source=uint32_t(command.a);
   if(command.kind==7)control_playing=false;
   if(command.kind==8)control_playing=true;
  }
  return head.load(std::memory_order_acquire);
 }
 int web_command(int kind,int source,double seconds,int64_t ingress) noexcept {
  std::lock_guard<std::mutex> lock(control_mutex);
  if(sources.empty() || !std::isfinite(seconds) || seconds<0 || kind<1 || kind>4)return 0;
  if(kind>=3 && (source<0 || static_cast<size_t>(source)>=sources.size()))return 0;
  const auto written=head.load(std::memory_order_relaxed);
  if(written>=INT32_MAX-1)return 0;
  const auto frame=std::round(std::min(seconds,double(frames)/rate)*rate);
  const bool at_end=applied.load(std::memory_order_acquire)>=written
    && !running.load(std::memory_order_acquire) && pos.load(std::memory_order_acquire)>=frames;
  Command command{};
  switch(kind){
   case 1:command={1,(!control_playing||at_end)?1.0:0.0,0};break;
   case 2:command={6,-1,frame};break;
   case 3:command={6,double(source),-1};break;
   case 4:command={8,double(source),frame};break;
  }
  const auto sequence=submit_control(command);
  if(!sequence)return 0;
  ++web.serial;
  web.command=sequence;
  web.ingress_ns=static_cast<uint64_t>(ingress);
  web.source=control_source;
  web.playing=control_playing;
  if(kind==2 || kind==4){web.seek=sequence;web.target=frame/rate;}
  return static_cast<int>(sequence);
 }
 void hint_landing() noexcept {if(provider_hint)provider_hint(provider,0,looping?left:UINT64_MAX,epoch);}
 void jump(uint64_t p) { cursor=std::min(p,frames);++epoch;if(provider)provider_request(provider,0,cursor,epoch);hint_landing();old[0]=last[0];old[1]=last[1];blend=fade_frames; }
 uint64_t source_frame(uint64_t output) const noexcept {return tempo_ratio==1?output:std::min(original_frames,static_cast<uint64_t>(std::llround(output*tempo_ratio)));}
 uint64_t bound(double x) { x/=tempo_ratio;return x<=0?0:x>=static_cast<double>(frames)?frames:(tempo_ratio==1?static_cast<uint64_t>(x):static_cast<uint64_t>(std::llround(x))); }
 void record(int64_t start,int64_t end,uint64_t first,uint64_t last,uint64_t ep,
             uint64_t sequence,uint32_t index,bool transport_active) noexcept {
  auto k=map_count.load(std::memory_order_relaxed);auto& m=maps[k%512];m.version.store(k*2+1,std::memory_order_release);
  m.began.store(start);m.ended.store(end);m.first.store(source_frame(first));m.last.store(source_frame(last));m.epoch.store(ep);m.transition.store(sequence);m.source.store(index);m.transport_active.store(transport_active);
  m.version.store(k*2+2,std::memory_order_release);map_count.store(k+1,std::memory_order_release);
 }
 uint64_t audible(int64_t now,uint64_t* epoch_out=nullptr,uint32_t* active_out=nullptr,
                  uint64_t* sequence_out=nullptr,uint32_t* source_out=nullptr,
                  uint32_t* transport_out=nullptr) const noexcept {
  if(epoch_out)*epoch_out=0;
  if(active_out)*active_out=0;
  if(sequence_out)*sequence_out=0;
  if(source_out)*source_out=0;
  if(transport_out)*transport_out=0;
  const auto count=map_count.load(std::memory_order_acquire);
  uint64_t previous=0;
  uint32_t previous_transport=0;
  for(auto k=count>512?count-512:0;k<count;++k){
   const auto& m=maps[k%512];
   const auto v=m.version.load(std::memory_order_acquire);
   if(v!=k*2+2)continue;
   const auto a=m.began.load(),z=m.ended.load();
   const auto f=m.first.load(),l=m.last.load(),ep=m.epoch.load(),seq=m.transition.load();
   const auto index=m.source.load(),transport=m.transport_active.load();
   if(m.version.load(std::memory_order_acquire)!=v)continue;
   if(now<a)break;
   if(epoch_out)*epoch_out=ep;
   if(sequence_out)*sequence_out=seq;
   if(source_out)*source_out=index;
   if(now<z && z>a){
    if(active_out)*active_out=l>f;
    if(transport_out)*transport_out=transport;
    return f+static_cast<uint64_t>((l-f)*double(now-a)/double(z-a));
   }
   previous=l;
   previous_transport=transport;
  }
  // A timestamp gap has no frame coverage. Hold the last heard frame and
  // transport state until the next mapped boundary, including a pause tail.
  if(transport_out)*transport_out=previous<source_frame(frames)?previous_transport:0;
  return previous;
 }
 void process(float* out,unsigned n,int64_t start_ns=0) noexcept {
  if(!start_ns)start_ns=clock_ns();
  auto t=tail.load(std::memory_order_relaxed),h=head.load(std::memory_order_acquire);
  bool restart=false;
  for(;t!=h;++t){auto c=commands[t%256];switch(c.kind){
   case 1:playing=c.a!=0;restart=playing && cursor>=frames;
    if(restart && sources.empty())jump(0);break; case 2:jump(bound(c.a));break;
   case 3:target_gain=c.a;break;case 4:left=bound(c.a);right=bound(c.b);looping=left!=right;break;
   case 5:looping=false;break;
   case 7:playing=false;[[fallthrough]];
   case 6:stage_transition(uint64_t(t)+1,c.a,c.b);restart=false;break;
   case 8:playing=true;stage_transition(uint64_t(t)+1,c.a,c.b);restart=false;break;
  }}tail.store(t,std::memory_order_release);
  if(!sources.empty()){
   restart=restart && !pending.sequence;
   commit_transition();
   if(restart && playing && !pending.sequence && cursor>=frames)jump(0);
   prefetch_aligned_sources();
  }
  hint_landing();
  unsigned section=0,retry_budget=n;uint64_t first=cursor,section_epoch=epoch,section_end=cursor,section_transition=transition;uint32_t section_source=source;bool was_moving=false,was_transporting=false;
  auto boundary=[&](unsigned i,bool moving,bool transporting){
   if(i==0){first=cursor;section_epoch=epoch;section_end=cursor;was_moving=moving;was_transporting=transporting;return;}
   if(epoch!=section_epoch || moving!=was_moving || transporting!=was_transporting){
    record(start_ns+int64_t(section*1e9/rate),start_ns+int64_t(i*1e9/rate),first,section_end,section_epoch,section_transition,section_source,was_transporting);
    section=i;first=cursor;section_epoch=epoch;was_moving=moving;was_transporting=transporting;
   }
  };
  for(unsigned i=0;i<n;++i){
   const double target=playing && !(pending.sequence && level==0)?1:0;
   gain+=std::clamp(target_gain-gain,-1.0/fade_frames,1.0/fade_frames);
   level+=std::clamp(target-level,-1.0/fade_frames,1.0/fade_frames);
   if(level<1e-12)level=0;
   if(looping && level>0){if(left<right && cursor>=right)jump(left);else if(left>right && cursor>=right && cursor<left)jump(left);}
   if(cursor>=frames && !pending.sequence){playing=false;level=0;}
   bool ready=level>0 && cursor<frames && fetch(cursor,retry_budget);
   if(provider && level>0 && cursor<frames && !ready){
    if(!provider_missed){stall_from[0]=last_output[0];stall_from[1]=last_output[1];stall_remaining=fade_frames;provider_missed=true;}
    level=0;
   }else if(ready && provider_missed){
    // Continue from the frozen source frame, crossfading from the last audible
    // starvation-tail sample; no sample skip and no abrupt recovery.
    level=playing?1:0;recover_from[0]=last_output[0];recover_from[1]=last_output[1];recover_remaining=fade_frames;blend=0;
    provider_missed=false;stall_remaining=0;ready=level>0;
   }
   bool moving=ready && level>0;boundary(i,moving,playing||moving);
   for(unsigned ch=0;ch<2;++ch){float sample=0;
    if(moving){sample=provider?block[(cursor-block_first)*2+ch]:pcm[cursor*2+ch];if(blend){double f=(fade_frames-blend)/double(fade_frames);sample=static_cast<float>(old[ch]*(1-f)+sample*f);}last[ch]=sample;sample=static_cast<float>(sample*gain*level*((!looping||left>right)?std::min(1.0,(frames-cursor)/double(fade_frames)):1.0));}
    else if(provider_missed && stall_remaining)sample=stall_from[ch]*(stall_remaining-1)/float(fade_frames);
    else last[ch]=0;
    if(moving && recover_remaining){const float w=recover_remaining/float(fade_frames);sample=recover_from[ch]*w+sample*(1-w);}
    if(!sources.empty()){
     if(audition_blend && moving){
      const auto at=fade_frames-audition_blend;
      const float weight=float(at)/float(std::max(1u,fade_frames-1));
      sample=old_tail[at*2+ch]*float(gain*level)*(1-weight)+sample*weight;
     }
     last_output[ch]=sample;
     peak=std::max(peak,std::abs(sample));
     // One gain for all lanes; a convex crossfade preserves their relative level.
     // Preserve overload evidence instead of hard clipping in the transport.
     sample*=0.7071067812f;
     if(std::abs(sample)>1.f)++overrange;
    }
    out[i*2+ch]=sample;
    if(sources.empty())last_output[ch]=sample;
   }
   if(stall_remaining)--stall_remaining;if(moving && recover_remaining)--recover_remaining;
   if(moving)++cursor;
   if(blend && moving)--blend;
   if(audition_blend){
    if(!level)audition_blend=0;
    else if(moving)--audition_blend;
   }
   section_end=cursor;
  }
  if(n>section)record(start_ns+int64_t(section*1e9/rate),start_ns+int64_t(n*1e9/rate),first,cursor,section_epoch,section_transition,section_source,was_transporting);
  if(!sources.empty()){
   published_peak.store(peak,std::memory_order_relaxed);
   published_overrange.store(overrange,std::memory_order_relaxed);
  }
  pos.store(source_frame(cursor),std::memory_order_release);published_epoch.store(epoch,std::memory_order_release);applied.store(t,std::memory_order_release);running.store(playing||level>0,std::memory_order_release);rendered.fetch_add(n,std::memory_order_relaxed);
 }
};
// Snapshot v2's reserved word carries optional flags; layout stays ABI v2.
enum AstraTransportFlags : uint32_t { transport_state_present=1, transport_is_playing=2 };
struct AstraCoreSnapshot { uint32_t size,version; uint64_t applied_commands,rendered_frames,rendered_source_frame; uint32_t active,reserved; uint64_t rendered_epoch,audible_source_frame,audible_epoch; uint32_t audible_active,pad; uint64_t sampled_ns; };
struct AstraAuditionSnapshot {
 AstraCoreSnapshot clock;
 uint64_t heard_transition, output_overrange;
 uint32_t heard_source;
 float pre_headroom_peak;
};
static int snapshot(void* p,AstraCoreSnapshot* out,AstraAuditionSnapshot* audition){
 if(!p||!out||out->size<sizeof(AstraCoreSnapshot))return -1;
 auto c=static_cast<Core*>(p);out->version=2;out->applied_commands=c->applied.load(std::memory_order_acquire);
 out->rendered_frames=c->rendered.load(std::memory_order_acquire);out->rendered_source_frame=c->pos.load(std::memory_order_acquire);
 out->active=c->running.load(std::memory_order_acquire);out->rendered_epoch=c->published_epoch.load(std::memory_order_acquire);out->sampled_ns=clock_ns();
 uint32_t transport_active=0;
 out->audible_source_frame=c->audible(out->sampled_ns,&out->audible_epoch,&out->audible_active,
   audition?&audition->heard_transition:nullptr,audition?&audition->heard_source:nullptr,&transport_active);
 out->reserved=transport_state_present|(transport_active?transport_is_playing:0u);
 out->pad=0;
 if(audition){
  audition->output_overrange=c->published_overrange.load(std::memory_order_relaxed);
  audition->pre_headroom_peak=c->published_peak.load(std::memory_order_relaxed);
 }
 return 0;
}
API int astra_core_snapshot(void* p,AstraCoreSnapshot* out){return snapshot(p,out,nullptr);}
API int astra_core_audition_snapshot(void* p,AstraAuditionSnapshot* out,uint32_t size){
 if(!p||!out||size!=sizeof(*out)||static_cast<Core*>(p)->sources.empty())return -1;
 return snapshot(p,&out->clock,out);
}
API void* astra_core_create_audition(const AuditionProvider* sources,uint32_t count,uint32_t initial,
 uint64_t frames,double rate){
 if(!sources||!count||count>32||initial>=count||!frames||!std::isfinite(rate)||rate<8000||rate>384000)return nullptr;
 for(uint32_t i=0;i<count;++i){
  const auto& source=sources[i];
  if(!source.handle||!source.read||!source.request||!source.hint||!source.prefetch||!source.retain||!source.release||!source.ready)return nullptr;
 }
 try{return new Core(sources,count,initial,frames,rate);}catch(...){return nullptr;}
}
API uint32_t astra_core_abi_version(){return 2;}
API void* astra_core_create(const float* p,uint64_t n){if(!p||!n||n>UINT64_MAX/2)return nullptr;try{return new Core(p,n);}catch(...){return nullptr;}}
API void* astra_core_create_rate(const float* p,uint64_t n,double rate){if(!std::isfinite(rate)||rate<8000||rate>384000)return nullptr;auto c=static_cast<Core*>(astra_core_create(p,n));if(c){c->rate=rate;c->fade_frames=std::max(1u,unsigned(std::llround(rate*.005)));}return c;}
API void* astra_core_create_provider(void* user,ProviderRead read,ProviderRequest request,ProviderReference retain,ProviderReference release,uint64_t frames,double rate){
 if(!user||!read||!request||!retain||!release||!frames||!std::isfinite(rate)||rate<8000||rate>384000)return nullptr;
 try{return new Core(user,read,request,retain,release,frames,rate);}catch(...){return nullptr;}
}
// Optional cache landing prefetch; existing provider ABI remains valid.
API void* astra_core_create_provider_hint(void* user,ProviderRead read,ProviderRequest request,ProviderReference retain,ProviderReference release,uint64_t frames,double rate,ProviderRequest hint){
 auto c=static_cast<Core*>(astra_core_create_provider(user,read,request,retain,release,frames,rate));if(c)c->provider_hint=hint;return c;
}
// Fixed-session pitch-preserving provider: provider coordinates are stretched output
// frames; public command 2/4 and snapshots remain ORIGINAL source frames at rate Hz.
// Tempo change requires a new provider/core session, never changes DAC sample rate.
API void* astra_core_create_tempo_provider(void* user,ProviderRead read,ProviderRequest request,ProviderReference retain,ProviderReference release,uint64_t source_frames,double rate,ProviderRequest hint,double tempo){
 if(!source_frames||!std::isfinite(tempo)||tempo<.25||tempo>2||double(source_frames)/tempo>double(UINT64_MAX/2))return nullptr;
 auto output_frames=static_cast<uint64_t>(std::llround(source_frames/tempo));
 auto c=static_cast<Core*>(astra_core_create_provider_hint(user,read,request,retain,release,output_frames,rate,hint));
 if(c){c->tempo_ratio=tempo;c->original_frames=source_frames;}return c;
}
API uint64_t astra_core_provider_misses(void* p){return p?static_cast<Core*>(p)->cache_misses.load(std::memory_order_relaxed):0;}
API int astra_core_provider_error(void* p){return p?static_cast<Core*>(p)->cache_error.load(std::memory_order_relaxed):-1;}
API uint64_t astra_core_audible_position(void* p){return p?static_cast<Core*>(p)->audible(clock_ns()):0;}
// Caller must stop/join the driver callback before destroy.
API void astra_core_retain(void* p){if(p)static_cast<Core*>(p)->refs.fetch_add(1,std::memory_order_relaxed);}
API void astra_core_release(void* p){if(p && static_cast<Core*>(p)->refs.fetch_sub(1,std::memory_order_acq_rel)==1)delete static_cast<Core*>(p);}
API void astra_core_destroy(void* p){astra_core_release(p);}
// Single control producer; returns 0 on full queue/invalid argument, never drops silently.
API int astra_core_command(void* p,int k,double a,double b){if(!p||!std::isfinite(a)||!std::isfinite(b)||k<1||k>5||(k==3&&(a<0||a>1)))return 0;auto c=static_cast<Core*>(p);std::lock_guard<std::mutex> lock(c->control_mutex);return c->submit_control({k,a,b})?1:0;}
API int64_t astra_core_submit(void* p,int k,double a,double b){if(!p||!std::isfinite(a)||!std::isfinite(b)||k<1||k>8||(k==3&&(a<0||a>1)))return -1;
 auto c=static_cast<Core*>(p);
 if(k>=6 && (c->sources.empty()||a < -1||a>=double(c->sources.size())||std::floor(a)!=a||b < -1))return -1;std::lock_guard<std::mutex> lock(c->control_mutex);
 const auto sequence=c->submit_control({k,a,b});
 return sequence?int64_t(sequence):-2;
}

// Window setup registers only a live Tracks PCM core. JS gets a process-local
// generation number, never a pointer. Retiring a route invalidates stale calls.
static std::mutex web_routes_mutex;
static std::unordered_map<int,Core*> web_routes;
static int next_web_route=0;
API int astra_core_bind_web_route(void* user){
 if(!user)return 0;
 auto core=static_cast<Core*>(user);
 if(core->sources.empty())return 0;
 std::lock_guard<std::mutex> lock(web_routes_mutex);
 if(next_web_route>=INT32_MAX-1)return 0;
 try{
  const auto route=++next_web_route;
  web_routes.emplace(route,core);
  astra_core_retain(core);
  return route;
 }catch(...){return 0;}
}
API void astra_core_unbind_web_route(int route){
 Core* core=nullptr;
 {
  std::lock_guard<std::mutex> lock(web_routes_mutex);
  const auto found=web_routes.find(route);
  if(found==web_routes.end())return;
  core=found->second;
  web_routes.erase(found);
 }
 astra_core_release(core);
}
API int astra_core_web_command(int route,int kind,int source,double seconds){
 const auto arrived=clock_ns();
 std::lock_guard<std::mutex> lock(web_routes_mutex);
 const auto found=web_routes.find(route);
 return found==web_routes.end()?0:found->second->web_command(kind,source,seconds,arrived);
}
API int astra_core_web_intent(void* user,WebControls* out,uint32_t size){
 if(!user||!out||size!=sizeof(*out))return 0;
 auto core=static_cast<Core*>(user);
 std::lock_guard<std::mutex> lock(core->control_mutex);
 *out=core->web;
 out->playing=core->control_playing;
 out->source=core->control_source;
 return 1;
}
API void astra_core_process(void* p,float* out,unsigned n){if(p&&out)static_cast<Core*>(p)->process(out,n);}
API uint64_t astra_core_position(void* p){return static_cast<Core*>(p)->pos.load(std::memory_order_acquire);}
API int astra_core_playing(void* p){return static_cast<Core*>(p)->running.load(std::memory_order_acquire);}
// PortAudio PaStreamCallback ABI, passed as a native function pointer; never enters Python.
struct PaClock {double input_adc,current,output_dac;};
API int astra_core_pa_callback(const void*,void* out,unsigned long frames,const void* times,unsigned long,void* user){if(!user||!out)return 2;auto c=static_cast<Core*>(user);auto now=clock_ns(),start=now;if(times){auto t=static_cast<const PaClock*>(times);start=c->dac_clock.map(t->current,t->output_dac,now);}c->process(static_cast<float*>(out),static_cast<unsigned>(frames),start);return 0;}
// Direct WASAPI host supplies absolute Windows QPC seconds. The anchor is sampled
// before callbacks start, so even the first callback's dispatch delay is excluded.
API int astra_core_qpc_callback(const void*,void* out,unsigned long frames,const void* times,unsigned long,void* user){if(!user||!out)return 2;auto c=static_cast<Core*>(user);auto now=clock_ns(),start=now;if(times){auto t=static_cast<const PaClock*>(times);start=c->dac_clock.map_qpc(t->current,t->output_dac,now);}c->process(static_cast<float*>(out),static_cast<unsigned>(frames),start);return 0;}
API int64_t astra_core_clock_ns(){return clock_ns();}
API uint64_t astra_core_clock_resets(void* p){return p?static_cast<Core*>(p)->dac_clock.resets.load(std::memory_order_relaxed):0;}
// Additive native API for hosts already calibrated to the core steady-clock domain.
API void astra_core_process_at_ns(void* p,float* out,unsigned n,int64_t dac_ns){if(p&&out&&dac_ns>0)static_cast<Core*>(p)->process(out,n,dac_ns);}


// Native follower ABI: map DAC playback time, not submitted position.
API int astra_core_midi_clock(void* p,AstraMidiClock* out){if(!p||!out||out->size!=sizeof(*out)||out->version!=1)return -1;auto c=static_cast<Core*>(p);uint32_t active=0;uint64_t epoch=0;auto frame=c->audible(clock_ns(),&epoch,&active);out->seconds=frame/c->rate;out->epoch=epoch;out->playing=active;out->reserved=0;return 0;}
