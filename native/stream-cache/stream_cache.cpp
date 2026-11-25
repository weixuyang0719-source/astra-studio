#include "stream_cache.h"
#define NOMINMAX
#include <windows.h>
#include <atomic>
#include <thread>
#include <vector>
#include <string>
#include <memory>
#include <mutex>
#include <algorithm>
#include <numeric>
#include <cmath>
#include <cstring>
#include <limits>
namespace {
using Count=int64_t;struct SFInfo{Count frames;int rate,channels,format,sections,seekable;};
struct SRCData{const float* in;float* out;long in_frames,out_frames,in_used,out_generated;int end;double ratio;};
constexpr uint32_t BLOCK=16384,CONTEXT=8192;constexpr uint64_t RESERVE=4ull*1024*1024;
static_assert(std::atomic<uint64_t>::is_always_lock_free,"cache requires lock-free counters");
bool absolute(const wchar_t*p){return p&&((p[0]&&p[1]==L':'&&(p[2]==L'\\'||p[2]==L'/'))||(p[0]==L'\\'&&p[1]==L'\\'));}
struct Decoder{
 HMODULE sf=nullptr,src=nullptr;void*file=nullptr;SFInfo meta{};
 void*(*open)(const wchar_t*,int,SFInfo*);Count(*read)(void*,float*,Count);Count(*seek)(void*,Count,int);int(*close)(void*);const char*(*error)(void*);int(*simple)(SRCData*,int,int)=nullptr;
 template<class T>void get(HMODULE h,T&v,const char*n){v=reinterpret_cast<T>(GetProcAddress(h,n));if(!v)throw std::runtime_error(n);}
 Decoder(const wchar_t*d,const wchar_t*r,const wchar_t*p,uint32_t target){sf=LoadLibraryExW(d,nullptr,LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR|LOAD_LIBRARY_SEARCH_DEFAULT_DIRS);if(!sf)throw std::runtime_error("sndfile DLL load failed");try{get(sf,open,"sf_wchar_open");get(sf,read,"sf_readf_float");get(sf,seek,"sf_seek");get(sf,close,"sf_close");get(sf,error,"sf_strerror");file=open(p,0x10,&meta);if(!file)throw std::runtime_error(error(nullptr));if(meta.frames<=0||!meta.seekable||(meta.channels!=1&&meta.channels!=2)||meta.rate<8000||meta.rate>384000)throw std::runtime_error("seekable nonempty mono/stereo source required");if(target!=uint32_t(meta.rate)){src=LoadLibraryExW(r,nullptr,LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR|LOAD_LIBRARY_SEARCH_DEFAULT_DIRS);if(!src)throw std::runtime_error("samplerate DLL load failed");get(src,simple,"src_simple");}}catch(...){cleanup();throw;}}
 void cleanup(){if(file){close(file);file=nullptr;}if(src){FreeLibrary(src);src=nullptr;}if(sf){FreeLibrary(sf);sf=nullptr;}}
 ~Decoder(){cleanup();}
};
struct Slot{std::atomic<int>pins{0};uint64_t key=UINT64_MAX;std::vector<float>pcm;Slot():pcm(BLOCK*2){};};
// Readers share a published slot. Retry a bounded number of times if the
// worker pins it for inspection between our load and CAS.
static bool pin(Slot& slot) noexcept {
 auto readers=slot.pins.load(std::memory_order_relaxed);
 for(unsigned attempt=0;readers>=0 && attempt<3;++attempt)
  if(slot.pins.compare_exchange_strong(readers,readers+1,std::memory_order_acquire))return true;
 return false;
}
struct Cache{
 std::unique_ptr<Decoder>decoder;std::vector<std::unique_ptr<Slot>>slots;std::vector<float>input,stereo,resampled;
 uint32_t rate;uint64_t frames,allocated;std::atomic<unsigned>refs{1};std::atomic<bool>stop{false},closed{false},failed{false};
 std::atomic<uint64_t>seq{0},target{0},generation{0},hint{UINT64_MAX},hint_generation{UINT64_MAX},seek{UINT64_MAX},decoded{0},misses{0},discarded{0};std::thread worker;std::mutex errorMutex;std::string error;
 Cache(const wchar_t*d,const wchar_t*r,const wchar_t*p,uint32_t hz,uint64_t budget):decoder(std::make_unique<Decoder>(d,r,p,hz)),rate(hz){
  auto&m=decoder->meta;if(uint64_t(m.frames)>UINT64_MAX/rate)throw std::runtime_error("source frame count overflow");frames=uint64_t(m.frames)*rate/uint32_t(m.rate);if(!frames)throw std::runtime_error("empty target PCM");
  uint64_t maxIn=uint64_t(std::ceil(double(BLOCK)*m.rate/rate))+2*CONTEXT+2*uint64_t(m.rate/std::gcd(m.rate,int(rate)))+8;
  uint64_t maxOut=uint64_t(std::ceil(double(maxIn)*rate/m.rate))+32;
  uint64_t scratch=(maxIn*(m.channels+2)+maxOut*2)*sizeof(float),slotBytes=BLOCK*2*sizeof(float);
  if(budget<RESERVE+scratch+4*slotBytes)throw std::runtime_error("cache budget too small for scratch and four slots");
  auto count=std::min<uint64_t>(32,(budget-RESERVE-scratch)/slotBytes);allocated=RESERVE+scratch+count*slotBytes;
  input.resize(size_t(maxIn*m.channels));stereo.resize(size_t(maxIn*2));resampled.resize(size_t(maxOut*2));for(uint64_t i=0;i<count;i++)slots.push_back(std::make_unique<Slot>());
  worker=std::thread([this]{run();});
 }
 ~Cache(){stop=true;if(worker.joinable())worker.join();}
 struct Request{uint64_t frame,gen,hint,hint_gen,seek;};
 Request request(){for(;;){auto q=seq.load(std::memory_order_acquire);if(q&1)continue;Request r{target.load(std::memory_order_relaxed),generation.load(std::memory_order_relaxed),hint.load(std::memory_order_relaxed),hint_generation.load(std::memory_order_relaxed),seek.load(std::memory_order_acquire)};if(seq.load(std::memory_order_acquire)==q)return r;}}
 bool exists(uint64_t key){for(auto&s:slots){int expected=0;if(s->pins.compare_exchange_strong(expected,1,std::memory_order_acquire)){bool ok=s->key==key;s->pins.fetch_sub(1,std::memory_order_release);if(ok)return true;}}return false;}
 bool outdated(Request r, bool for_seek){
  auto now=request();
  return stop||now.gen!=r.gen||(for_seek && now.seek/BLOCK!=r.seek/BLOCK);
 }
 bool decode(uint64_t key,Request req,bool for_seek,float*&data,uint64_t&offset){
  auto&m=decoder->meta;uint64_t at=key*BLOCK;if(at>=frames)return false;
  uint64_t end=std::min(frames,at+BLOCK);double ratio=double(rate)/m.rate;
  uint64_t unit=uint64_t(m.rate/std::gcd(m.rate,int(rate)));
  uint64_t center=uint64_t(std::floor(at/ratio));uint64_t begin=center>CONTEXT?center-CONTEXT:0;begin-=begin%unit;
  uint64_t finish=std::min<uint64_t>(m.frames,uint64_t(std::ceil(end/ratio))+CONTEXT);
  auto count=finish-begin;if(count*m.channels>input.size())throw std::runtime_error("decode scratch bound exceeded");
  if(decoder->seek(decoder->file,Count(begin),0)!=Count(begin))throw std::runtime_error("source seek failed");
  uint64_t got=0;while(got<count){if(outdated(req,for_seek))return false;auto n=decoder->read(decoder->file,input.data()+got*m.channels,Count(std::min<uint64_t>(8192,count-got)));if(n<=0)throw std::runtime_error("source read failed");got+=uint64_t(n);}
  for(uint64_t i=0;i<got;i++){float a=input[i*m.channels],b=m.channels==2?input[i*2+1]:a;if(!std::isfinite(a)||!std::isfinite(b))throw std::runtime_error("nonfinite source PCM");stereo[i*2]=a;stereo[i*2+1]=b;}
  if(outdated(req,for_seek))return false;
  auto targetBegin=uint64_t(std::llround(begin*ratio));offset=at-targetBegin;
  if(m.rate==int(rate)){data=stereo.data();return true;}
  SRCData d{stereo.data(),resampled.data(),long(got),long(resampled.size()/2),0,0,1,ratio};
  if(decoder->simple(&d,0,2))throw std::runtime_error("SINC_BEST resampling failed");
  if(d.out_generated<0||uint64_t(d.out_generated)<offset+end-at)throw std::runtime_error("resampler returned too few frames");
  data=resampled.data();return true;
 }
 void run()noexcept{
  try{while(!stop){auto req=request();auto key=req.frame/BLOCK;bool work=false;
   // Pin a bounded working set: primary/current first, loop landing second.
   // A reserved victim slot permits progress while one reader temporarily pins old PCM.
   uint64_t keys[9];size_t count=0,limit=std::min<size_t>(9,slots.size()-1);
   auto add=[&](uint64_t k){if(count>=limit||k>=((frames-1)/BLOCK+1))return;for(size_t i=0;i<count;i++)if(keys[i]==k)return;keys[count++]=k;};
   bool hinted=req.hint_gen==req.gen&&req.hint<frames;
   const auto seek_key=req.seek/BLOCK;
   add(key);add(key+1);
   if(hinted){add(req.hint/BLOCK);add(req.hint/BLOCK+1);}
   if(req.seek<frames){add(seek_key);add(seek_key+1);}
   add(key+2);add(key+3);add(key+4);
   auto protected_key=[&](uint64_t k){for(size_t i=0;i<count;i++)if(keys[i]==k)return true;return false;};
   for(size_t n=0;n<count&&!stop;n++){
    auto k=keys[n];if(exists(k))continue;
    const bool for_seek=req.seek<frames && (k==seek_key || k==seek_key+1)
      && k!=key && k!=key+1 && !(hinted && (k==req.hint/BLOCK || k==req.hint/BLOCK+1));
    if(outdated(req,for_seek))break;
    float*pcm=nullptr;uint64_t offset=0;
    if(!decode(k,req,for_seek,pcm,offset) || outdated(req,for_seek)){discarded++;break;}
    Slot*chosen=nullptr;for(auto&s:slots){int expected=0;if(s->pins.compare_exchange_strong(expected,-1,std::memory_order_acquire)){if(s->key==UINT64_MAX||!protected_key(s->key)){chosen=s.get();break;}s->pins.store(0,std::memory_order_release);}}
    if(!chosen)break;
    auto samples=std::min<uint64_t>(BLOCK,frames-k*BLOCK);std::memcpy(chosen->pcm.data(),pcm+offset*2,size_t(samples*2*sizeof(float)));std::fill(chosen->pcm.begin()+samples*2,chosen->pcm.end(),0.0f);chosen->key=k;chosen->pins.store(0,std::memory_order_release);decoded++;work=true;
   }
   if(!work)std::this_thread::sleep_for(std::chrono::milliseconds(2));
  }}catch(const std::exception&e){{std::lock_guard<std::mutex>l(errorMutex);error=e.what();}failed=true;}catch(...){{std::lock_guard<std::mutex>l(errorMutex);error="cache worker exception";}failed=true;}
  closed=true;
 }
};
void fill(Cache*c,AstraStreamCacheInfo*i){auto sz=i->size;*i={};i->size=sz;i->version=1;i->state=c->failed?3:c->closed?2:1;i->sample_rate=c->rate;i->source_rate=c->decoder->meta.rate;i->channels=2;i->block_frames=BLOCK;i->slots=uint32_t(c->slots.size());i->frames=c->frames;i->allocated_bytes=c->allocated;i->decoded_blocks=c->decoded;i->misses=c->misses;i->discarded=c->discarded;std::lock_guard<std::mutex>l(c->errorMutex);strncpy_s(i->error,c->error.c_str(),_TRUNCATE);}
}
SC_API void* astra_cache_create(const wchar_t*d,const wchar_t*r,const wchar_t*p,uint32_t rate,uint64_t budget,AstraStreamCacheInfo*i){if(!i||i->size!=sizeof(*i)||i->version!=1)return nullptr;try{if(!absolute(d)||!absolute(p)||!absolute(r)||rate<8000||rate>384000)throw std::runtime_error("invalid paths or target rate");auto c=std::make_unique<Cache>(d,r,p,rate,budget);fill(c.get(),i);return c.release();}catch(const std::exception&e){i->state=3;strncpy_s(i->error,e.what(),_TRUNCATE);return nullptr;}catch(...){i->state=3;strncpy_s(i->error,"cache initialization failed",_TRUNCATE);return nullptr;}}
SC_API int astra_cache_request(void*p,uint32_t source,uint64_t frame,uint64_t gen){if(!p||source)return 0;auto c=static_cast<Cache*>(p);if(c->stop||c->failed)return 0;c->seq.fetch_add(1,std::memory_order_acq_rel);c->target.store(std::min(frame,c->frames),std::memory_order_relaxed);c->generation.store(gen,std::memory_order_relaxed);c->seq.fetch_add(1,std::memory_order_release);return 1;}
SC_API int astra_cache_hint(void*p,uint32_t source,uint64_t frame,uint64_t gen){if(!p||source)return 0;auto c=static_cast<Cache*>(p);if(c->stop||c->failed)return 0;c->seq.fetch_add(1,std::memory_order_acq_rel);c->hint.store(frame<c->frames?frame:UINT64_MAX,std::memory_order_relaxed);c->hint_generation.store(gen,std::memory_order_relaxed);c->seq.fetch_add(1,std::memory_order_release);return 1;}
// The third landing lane is independent of the current/read-ahead and loop lanes.
SC_API int astra_cache_prefetch(void*p,uint32_t source,uint64_t frame,uint64_t){
 if(!p||source)return 0;
 auto c=static_cast<Cache*>(p);
 if(c->stop||c->failed)return 0;
 c->seek.store(frame<c->frames?frame:UINT64_MAX,std::memory_order_release);
 return 1;
}
SC_API int astra_cache_read(void*p,uint32_t source,uint64_t frame,uint32_t count,float*out){if(!p||!out||source||count>BLOCK)return -1;auto c=static_cast<Cache*>(p);if(c->stop||c->failed){std::fill_n(out,size_t(count)*2,0.0f);return -1;}uint32_t copied=0;while(copied<count){if(frame>=c->frames){std::fill_n(out+size_t(copied)*2,size_t(count-copied)*2,0.0f);return 1;}auto key=frame/BLOCK;auto offset=frame%BLOCK;auto n=std::min<uint64_t>({count-copied,BLOCK-offset,c->frames-frame});bool found=false;for(auto&s:c->slots){if(!pin(*s))continue;if(s->key==key){std::memcpy(out+size_t(copied)*2,s->pcm.data()+offset*2,size_t(n)*2*sizeof(float));found=true;}s->pins.fetch_sub(1,std::memory_order_release);if(found)break;}if(!found){c->misses++;std::fill_n(out,size_t(count)*2,0.0f);return 0;}copied+=uint32_t(n);frame+=n;}return 1;}
// Bounded readiness across the next one or two resident cache blocks.
SC_API int astra_cache_ready(void*p,uint32_t source,uint64_t frame,uint32_t count){
 if(!p||source||count>BLOCK)return -1;
 auto c=static_cast<Cache*>(p);
 if(c->stop||c->failed)return -1;
 const auto end=frame<c->frames?std::min(c->frames,frame+count):frame;
 for(auto at=frame;at<end;){
  const auto key=at/BLOCK;
  bool present=false;
  for(auto& slot:c->slots){
   if(!pin(*slot))continue;
   present=slot->key==key;
   slot->pins.fetch_sub(1,std::memory_order_release);
   if(present)break;
  }
  if(!present)return 0;
  at+=std::min(end-at,BLOCK-at%BLOCK);
 }
 return 1;
}
SC_API int astra_cache_info(void*p,AstraStreamCacheInfo*i){if(!p||!i||i->size!=sizeof(*i)||i->version!=1)return 0;fill(static_cast<Cache*>(p),i);return 1;}
SC_API void astra_cache_close(void*p){if(p)static_cast<Cache*>(p)->stop=true;}
SC_API void astra_cache_retain(void*p){if(p)static_cast<Cache*>(p)->refs.fetch_add(1,std::memory_order_relaxed);}
SC_API void astra_cache_release(void*p){if(p&&static_cast<Cache*>(p)->refs.fetch_sub(1,std::memory_order_acq_rel)==1)delete static_cast<Cache*>(p);}
