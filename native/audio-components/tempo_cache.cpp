#include "tempo_cache.h"
#include "tempo_processor.h"
#include <atomic>
#include <thread>
#include <mutex>
#include <memory>
#include <cstring>
#include <chrono>
namespace{
constexpr uint64_t BLOCK=4096,SLOTS=16;
struct Slot{std::atomic<int>pins{0};uint64_t key=UINT64_MAX;float pcm[BLOCK*2]{};};
struct Bank{std::atomic<bool>available{false};Slot slots[SLOTS];std::unique_ptr<astra::TempoProcessor>dsp;uint64_t next_key=0,input_at=0;bool valid=false;unsigned discard=0;std::vector<float>fifo,part,input;size_t fifo_offset=0;};
struct Tempo{
 void*input;AstraTempoRead read;AstraTempoRequest request,hint;AstraTempoRef release;
 uint64_t source_frames,frames;uint32_t rate;double ratio;
 Bank banks[2];std::atomic<unsigned>refs{1};std::atomic<bool>stop{false},failed{false},closed{false};std::thread worker;
 std::atomic<uint64_t>seq{0},target{0},generation{0},landing{UINT64_MAX},landing_gen{UINT64_MAX},published_gen{UINT64_MAX};std::atomic<int>published_bank{-1},requested_bank{-1};
 std::atomic<uint64_t>rendered{0},misses{0},resets{0};uint64_t input_gen=0;int selected=-1;uint64_t selected_gen=UINT64_MAX;
 std::mutex error_mutex;std::string error;
 struct Request{uint64_t frame,gen,land,landgen;int bank;};
 Tempo(void*p,AstraTempoRead r,AstraTempoRequest q,AstraTempoRequest h,AstraTempoRef retain,AstraTempoRef rel,uint64_t n,uint32_t hz,double t):input(p),read(r),request(q),hint(h),release(rel),source_frames(n),frames(uint64_t(std::llround(n/t))),rate(hz),ratio(t){retain(input);try{for(auto&b:banks)b.dsp=std::make_unique<astra::TempoProcessor>(rate,ratio);worker=std::thread([this]{run();});}catch(...){release(input);throw;}}
 ~Tempo(){stop=true;if(worker.joinable())worker.join();release(input);}
 Request latest(){for(;;){auto s=seq.load(std::memory_order_acquire);if(s&1)continue;Request r{target.load(),generation.load(),landing.load(),landing_gen.load(),requested_bank.load()};if(seq.load(std::memory_order_acquire)==s)return r;}}
 std::chrono::steady_clock::time_point speculative_deadline;
 bool aborted(Request r,bool speculative=false){return stop||generation.load()!=r.gen||(speculative&&(landing.load()!=r.land||landing_gen.load()!=r.landgen||std::chrono::steady_clock::now()>=speculative_deadline));}
 bool contains(int bank,uint64_t key){if(!banks[bank].available.load(std::memory_order_acquire))return false;for(auto&s:banks[bank].slots){int p=s.pins.load();if(p<0||!s.pins.compare_exchange_strong(p,p+1,std::memory_order_acquire))continue;bool found=s.key==key;s.pins.fetch_sub(1,std::memory_order_release);if(found)return true;}return false;}
 // Worker decisions must distinguish absent PCM from transient reader-pin CAS
 // contention. A false negative here would reset a continuous DSP history.
 bool worker_contains(int bank,uint64_t key){
  if(!banks[bank].available.load(std::memory_order_acquire))return false;
  auto&s=banks[bank].slots[key%SLOTS];
  for(;;){int p=s.pins.load();if(p>=0&&s.pins.compare_exchange_weak(p,p+1,std::memory_order_acquire)){bool found=s.key==key;s.pins.fetch_sub(1,std::memory_order_release);return found;}if(stop)return false;std::this_thread::yield();}
 }
 // Non-RT source read. Deadline is cancellation, never holding up the device callback.
 bool source(Bank&b,uint64_t at,unsigned n,Request req,bool speculative=false){if(aborted(req,speculative))return false;b.input.assign(size_t(n)*2,0);if(at>=source_frames)return true;unsigned count=unsigned(std::min<uint64_t>(n,source_frames-at));
  for(unsigned done=0;done<count;){unsigned part=std::min(4096u,count-done);auto gen=req.gen;request(input,0,at+done,gen);if(hint)hint(input,0,req.landgen==req.gen&&req.land<frames?uint64_t(std::llround(req.land*ratio)):UINT64_MAX,gen);
   for(;;){if(aborted(req,speculative))return false;int ok=read(input,0,at+done,part,b.input.data()+done*2);if(ok==1)break;if(ok<0)throw std::runtime_error("tempo source provider failed");if(speculative)return false;std::this_thread::sleep_for(std::chrono::milliseconds(1));}done+=part;
  }return true;
 }
 bool reset(int i,uint64_t key,Request req,bool speculative=false){auto&b=banks[i];b.available.store(false,std::memory_order_release);b.valid=false;for(auto&s:b.slots){for(;;){int p=0;if(s.pins.compare_exchange_strong(p,-1,std::memory_order_acquire)){s.key=UINT64_MAX;s.pins.store(0,std::memory_order_release);break;}if(aborted(req,speculative))return false;std::this_thread::yield();}}
  b.dsp->reset();b.fifo.clear();b.fifo_offset=0;b.next_key=key;auto at=uint64_t(std::llround(key*BLOCK*ratio));unsigned prime=b.dsp->inputLatency();if(!source(b,at,prime,req,speculative))return false;b.dsp->prime(b.input.data(),prime);b.input_at=at+prime;b.discard=b.dsp->outputLatency();b.valid=true;resets++;return true;
 }
 bool produce(int i,Request req,bool speculative=false){auto&b=banks[i];auto key=b.next_key;if(key*BLOCK>=frames)return true;unsigned need=unsigned(std::min<uint64_t>(BLOCK,frames-key*BLOCK));
  while((b.fifo.size()-b.fifo_offset)/2<need){if(!source(b,b.input_at,1024,req,speculative))return false;b.dsp->process(b.input.data(),1024,b.part);b.input_at+=1024;size_t discard=std::min<size_t>(b.discard,b.part.size()/2);b.discard-=unsigned(discard);if(b.fifo_offset){b.fifo.erase(b.fifo.begin(),b.fifo.begin()+b.fifo_offset);b.fifo_offset=0;}b.fifo.insert(b.fifo.end(),b.part.begin()+discard*2,b.part.end());}
  auto&s=b.slots[key%SLOTS];for(;;){int p=0;if(s.pins.compare_exchange_strong(p,-1,std::memory_order_acquire))break;if(aborted(req,speculative))return false;std::this_thread::yield();}
  std::memcpy(s.pcm,b.fifo.data()+b.fifo_offset,need*2*sizeof(float));std::fill(s.pcm+need*2,s.pcm+BLOCK*2,0.f);s.key=key;s.pins.store(0,std::memory_order_release);b.fifo_offset+=need*2;b.next_key++;b.available.store(true,std::memory_order_release);rendered++;return true;
 }
 void run()noexcept{try{int active=0;uint64_t prior=UINT64_MAX;while(!stop){auto req=latest();uint64_t key=req.frame/BLOCK;if(req.frame>=frames){std::this_thread::sleep_for(std::chrono::milliseconds(2));continue;}
   if(req.gen!=prior){int ready=req.bank>=0&&worker_contains(req.bank,key)?req.bank:worker_contains(active,key)?active:worker_contains(1-active,key)?1-active:-1;if(ready>=0)active=ready;else if(!reset(active,key,req))continue;prior=req.gen;}
   auto&main=banks[active];if(!main.valid||(!worker_contains(active,key)&&(key<main.next_key||key>main.next_key))){if(!reset(active,key,req))continue;}
   bool work=false;
   if(!worker_contains(active,key)){if(!produce(active,req))continue;work=true;}
   if(worker_contains(active,key)){published_bank.store(active,std::memory_order_relaxed);published_gen.store(req.gen,std::memory_order_release);}
   // Main work ALWAYS precedes speculative landing. Landing never waits on a
   // source miss; one bounded 2ms work slice yields back to main, and a same-gen
   // hint change/cancel invalidates the old slice without stopping main playback.
   if(main.next_key<=key+SLOTS-2&&main.next_key*BLOCK<frames){if(!produce(active,req))continue;work=true;}
   bool hinted=req.landgen==req.gen&&req.land<frames;int other=1-active;
   if(hinted && main.next_key>=key+3){
    speculative_deadline=std::chrono::steady_clock::now()+std::chrono::milliseconds(2);
    uint64_t lk=req.land/BLOCK;auto&b=banks[other];bool ready=true;
    if(!b.valid||(!worker_contains(other,lk)&&(lk<b.next_key||lk>b.next_key)))ready=reset(other,lk,req,true);
    if(ready&&b.next_key<=lk+1&&b.next_key*BLOCK<frames){if(produce(other,req,true))work=true;}
   }
   if(!work)std::this_thread::sleep_for(std::chrono::milliseconds(1));
  }}catch(const std::exception&e){{std::lock_guard<std::mutex>l(error_mutex);error=e.what();}failed=true;}catch(...){{std::lock_guard<std::mutex>l(error_mutex);error="tempo worker exception";}failed=true;}closed=true;}
};
void info(Tempo*c,AstraTempoInfo*i){auto size=i->size;*i={};i->size=size;i->version=1;i->state=c->failed?3:c->closed?2:1;i->sample_rate=c->rate;i->source_frames=c->source_frames;i->output_frames=c->frames;i->allocated_slots=2*SLOTS*BLOCK*2*sizeof(float);i->rendered_blocks=c->rendered;i->misses=c->misses;i->resets=c->resets;i->tempo=c->ratio;std::lock_guard<std::mutex>l(c->error_mutex);strncpy_s(i->error,c->error.c_str(),_TRUNCATE);}
}
TC_API void* astra_tempo_create(void*p,AstraTempoRead r,AstraTempoRequest q,AstraTempoRequest h,AstraTempoRef retain,AstraTempoRef rel,uint64_t n,uint32_t hz,double t,AstraTempoInfo*i){if(!i||i->size!=sizeof(*i)||i->version!=1)return nullptr;try{if(!p||!r||!q||!retain||!rel||!n||!std::isfinite(t)||t<.25||t>2||hz<8000||hz>384000||n/t>double(UINT64_MAX/2))throw std::invalid_argument("invalid tempo provider");auto c=std::make_unique<Tempo>(p,r,q,h,retain,rel,n,hz,t);info(c.get(),i);return c.release();}catch(const std::exception&e){i->state=3;strncpy_s(i->error,e.what(),_TRUNCATE);return nullptr;}}
TC_API int astra_tempo_request(void*p,uint32_t source,uint64_t frame,uint64_t gen){if(!p||source)return 0;auto*c=(Tempo*)p;if(c->stop||c->failed)return 0;int chosen=c->requested_bank.load();if(c->generation.load()!=gen){int first=c->published_bank.load();if(first<0)first=0;chosen=c->contains(first,frame/BLOCK)?first:c->contains(1-first,frame/BLOCK)?1-first:-1;}c->seq.fetch_add(1);c->requested_bank=chosen;c->target=std::min(frame,c->frames);c->generation=gen;c->seq.fetch_add(1,std::memory_order_release);return 1;}
TC_API int astra_tempo_hint(void*p,uint32_t source,uint64_t frame,uint64_t gen){if(!p||source)return 0;auto*c=(Tempo*)p;if(c->stop||c->failed)return 0;c->seq.fetch_add(1);c->landing=frame<c->frames?frame:UINT64_MAX;c->landing_gen=gen;c->seq.fetch_add(1,std::memory_order_release);return 1;}
TC_API int astra_tempo_read(void*p,uint32_t source,uint64_t frame,uint32_t count,float*out){if(!p||!out||source||count>256)return -1;auto*c=(Tempo*)p;if(c->failed||c->stop){std::fill_n(out,count*2,0.f);return -1;}uint64_t gen=c->generation.load();if(c->selected_gen!=gen){c->selected_gen=gen;c->selected=c->requested_bank.load();}if(c->published_gen.load(std::memory_order_acquire)==gen)c->selected=c->published_bank.load();
 unsigned done=0;while(done<count){if(frame>=c->frames){std::fill_n(out+done*2,(count-done)*2,0.f);return 1;}bool found=false;if(c->selected>=0){auto&s=c->banks[c->selected].slots[(frame/BLOCK)%SLOTS];int pin=s.pins.load();if(pin>=0&&s.pins.compare_exchange_strong(pin,pin+1,std::memory_order_acquire)){if(s.key==frame/BLOCK){unsigned n=unsigned(std::min<uint64_t>({count-done,BLOCK-frame%BLOCK,c->frames-frame}));std::memcpy(out+done*2,s.pcm+(frame%BLOCK)*2,n*2*sizeof(float));done+=n;frame+=n;found=true;}s.pins.fetch_sub(1,std::memory_order_release);}}if(!found){c->misses++;std::fill_n(out,count*2,0.f);return 0;}}return 1;}
TC_API void astra_tempo_retain(void*p){if(p)((Tempo*)p)->refs++;}TC_API void astra_tempo_release(void*p){if(p&&((Tempo*)p)->refs.fetch_sub(1)==1)delete(Tempo*)p;}TC_API void astra_tempo_close(void*p){if(p)((Tempo*)p)->stop=true;}
TC_API int astra_tempo_info(void*p,AstraTempoInfo*i){if(!p||!i||i->size!=sizeof(*i)||i->version!=1)return 0;info((Tempo*)p,i);return 1;}
