// Production adaptation of the kept-device prototype's generation-tagged SPSC rings,
// extended with a wait-free in-process direct PCM lane so Tracks, Notes, and Score
// share one physical ASIO stream with zero extra ring latency for Tracks.
// The physical callback has one consumer and never calls a vendor or waits for a worker.
#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <windows.h>
#include <avrt.h>
#pragma comment(lib,"avrt.lib")
#include <algorithm>
#include <atomic>
#include <cmath>
#include <cstdint>
#include <cstring>
#include <new>
#include <thread>
using U=uint32_t; using Q=uint64_t; using S=int64_t;
using Callback=int(*)(const void*,void*,unsigned long,const void*,unsigned long,void*);
using Reference=void(*)(void*);
struct View {
 U size,version,state,loop,has_timeline,fault; int processor_code; U parked;
 Q epoch,sequence,last_intent,callbacks,output_frames,underruns,process_calls;
 S position; double gain,effective_gain; U muted,reserved; double midi_gain,audio_gain,click_gain; U metronome,reserved3;
};
using Snapshot=int(*)(void*,View*);
constexpr U maxFrames=16384, depth=4;
constexpr Q directAudioTag=~Q(0);
static_assert(std::atomic<Q>::is_always_lock_free);
struct Block { Q token=0,at=0,sequence=0; S position=0; U state=0,valid=0; float pcm[maxFrames*2]; };
struct Ring { std::atomic<Q> head{0},tail{0}; Block rows[depth]; };
struct Shared {
 Ring ring[2];
 std::atomic<Q> cursor{0},selected{0},request{0},drops{0},switches{0},wrongSize{0};
 std::atomic<Q> audibleFrames{0},silentFrames{0},peakMillionths{0},cutCursor{0};
 std::atomic<U> frames{0},refVersion{0},refState{0};
 std::atomic<Q> refCursor{0}; std::atomic<S> refPosition{0};
 std::atomic<Q> rejected{0},trialAudible{0};
 U rate=48000,goodFrames=0;
 Q candidate=0,lastAudio=0; float lastSample[2]{};
};
struct Mapping {
 HANDLE file;
 Shared* shared;
 // Process-local wait-free direct PCM lane (Tracks NativeAuditionCore).
 // direct_state: 0=EMPTY, 1=ATTACHED, 2=RUNNING, 3=DETACHING_WAIT, 4=DETACHED_READY, 5=FINISHING
 Callback direct_cb{nullptr};
 void* direct_user{nullptr};
 Reference direct_release{nullptr};
 std::atomic<int> direct_state{0};
 std::atomic<U> direct_mode{0}; // 0=VST ring owns output, 1=direct PCM owns output
 float scratch[maxFrames*2]{};
};
#define API extern "C" __declspec(dllexport)
API Mapping* kept_map(const wchar_t* name,int owner){
 HANDLE h=owner?CreateFileMappingW(INVALID_HANDLE_VALUE,nullptr,PAGE_READWRITE,0,sizeof(Shared),name):OpenFileMappingW(FILE_MAP_ALL_ACCESS,FALSE,name);
 if(!h)return nullptr;
 if(owner&&GetLastError()==ERROR_ALREADY_EXISTS){CloseHandle(h);return nullptr;}
 void* address=MapViewOfFile(h,FILE_MAP_ALL_ACCESS,0,0,sizeof(Shared));
 if(!address){CloseHandle(h);return nullptr;}
 if(owner)new(address)Shared{};
 auto* m=new(std::nothrow)Mapping{};
 if(!m){UnmapViewOfFile(address);CloseHandle(h);return nullptr;}
 m->file=h; m->shared=static_cast<Shared*>(address);
 return m;
}
API int kept_direct_detach(Mapping* m){
 if(!m)return 1;
 for(;;){
  int st=m->direct_state.load(std::memory_order_acquire);
  if(st==0)return 1;
  if(st==3||st==5)return 0;
  if(st==2){
   if(m->direct_state.compare_exchange_weak(st,3,std::memory_order_acq_rel,std::memory_order_acquire))return 0;
   continue;
  }
  if(st==1||st==4){
   if(m->direct_state.compare_exchange_weak(st,5,std::memory_order_acq_rel,std::memory_order_acquire)){
    auto rel=m->direct_release; auto* u=m->direct_user;
    m->direct_cb=nullptr; m->direct_user=nullptr; m->direct_release=nullptr;
    m->direct_state.store(0,std::memory_order_release);
    if(rel&&u)rel(u);
    return 1;
   }
  }
 }
}
API int kept_direct_attach(Mapping* m,Callback cb,void* user,Reference retain,Reference release){
 if(!m||!cb||!user||!retain||!release)return 0;
 int expected=0;
 if(!m->direct_state.compare_exchange_strong(expected,5,std::memory_order_acq_rel,std::memory_order_acquire))return 0;
 retain(user);
 m->direct_cb=cb; m->direct_user=user; m->direct_release=release;
 m->direct_state.store(1,std::memory_order_release);
 return 1;
}
API void kept_direct_mode(Mapping* m,U mode){
 if(m)m->direct_mode.store(mode?1u:0u,std::memory_order_release);
}
API U kept_direct_mode_get(Mapping* m){
 return m?m->direct_mode.load(std::memory_order_acquire):0u;
}
API void kept_unmap(Mapping* m){
 if(m){
  kept_direct_detach(m);
  UnmapViewOfFile(m->shared);CloseHandle(m->file);delete m;
 }
}
API void kept_rate(Mapping* m,U rate){m->shared->rate=rate;} // owner, before device creation
API Q kept_stat(Mapping* m,U which){
 auto& s=*m->shared;
 switch(which){case 0:return s.selected.load();case 1:return s.cursor.load();case 2:return s.drops.load();
 case 3:return s.switches.load();case 4:return s.frames.load();case 5:return s.wrongSize.load();
 case 6:return s.audibleFrames.load();case 7:return s.silentFrames.load();case 8:return s.peakMillionths.load();case 9:return s.cutCursor.load();
 case 10:return s.rejected.load();case 11:return s.trialAudible.load();default:return 0;}
}
// One control word owns the cutover. Clearing it races safely with callback CAS.
constexpr Q switching=~Q(0);
API Q kept_request(Mapping* m,Q wanted){
 auto& request=m->shared->request;auto current=request.load();
 for(;;){
  if(current==switching){SwitchToThread();current=request.load();continue;}
  if(request.compare_exchange_weak(current,wanted))return current;
 }
}
API int kept_callback(const void* input,void* output,unsigned long n,const void* timeInfo,unsigned long status,void* user){
 auto* m=static_cast<Mapping*>(user); auto& s=*m->shared; auto* dst=static_cast<float*>(output);
 if(!dst||!n||n>maxFrames)return 2;
 auto known=s.frames.load();
 if(!known)s.frames.store(n);
 else if(known!=n){++s.wrongSize;std::fill_n(dst,2*n,0.f);return 2;}
 auto at=s.cursor.load();auto active=s.selected.load();auto want=s.request.load();
 Block* rows[2]{};Q heads[2]{};
 for(U i=0;i<2;++i){
  auto& r=s.ring[i];auto head=r.head.load();const auto tail=r.tail.load(std::memory_order_acquire);
  while(head<tail&&r.rows[head%depth].at<at)++head;
  heads[i]=head;
  if(head<tail&&r.rows[head%depth].at==at)rows[i]=&r.rows[head%depth];
 }
 auto find=[&](Q id){auto* b=rows[id&1];return id&&b&&b->token==id?b:nullptr;};
 auto* old=find(active);auto* next=find(want);auto* audio=old;
 if(want!=s.candidate){s.goodFrames=0;s.candidate=want;s.trialAudible.store(0);}
 bool aligned=next&&(!active||(old&&old->valid&&next->valid&&next->sequence==old->sequence&&
       next->position==old->position&&(next->state==2)==(old->state==2)));
 if(aligned){
  s.goodFrames+=n;
  // Observe two complete blocks before exposing a new graph, then retain
  // the live old graph for 80 ms while the new one is actually audible.
  const U pre=std::max<U>(n*2,s.rate/40);
  if(!active||s.goodFrames>=pre){audio=next;s.trialAudible.store(want);}
  if((!active||s.goodFrames>=pre+std::max<U>(n,s.rate*8/100))&&s.request.compare_exchange_strong(want,switching)){
   s.cutCursor.store(at);s.selected.store(next->token);++s.switches;s.request.store(0);s.trialAudible.store(0);
  }
 }else if(want&&s.goodFrames){
  if(s.trialAudible.load()==want&&s.request.compare_exchange_strong(want,0)){s.rejected.store(s.candidate);s.trialAudible.store(0);}
  s.goodFrames=0;
 }
 const bool wantDirect=(m->direct_mode.load(std::memory_order_acquire)!=0);
 bool haveDirect=false;
 int expected=1;
 if(m->direct_state.compare_exchange_strong(expected,2,std::memory_order_acq_rel,std::memory_order_acquire)){
  haveDirect=true;
 }
 if(wantDirect){
  if(haveDirect){
   m->direct_cb(input,dst,n,timeInfo,status,m->direct_user);
   if(s.lastAudio&&s.lastAudio!=directAudioTag){
    auto* previous=find(s.lastAudio);const auto length=std::min<U>(n,64);
    for(U i=0;i<length;++i){
     const float t=float(i+1)/length;
     for(U c=0;c<2;++c)dst[2*i+c]=(previous?previous->pcm[2*i+c]:s.lastSample[c])*(1-t)+dst[2*i+c]*t;
    }
   }
   s.lastAudio=directAudioTag;
  }else{
   std::fill_n(dst,2*n,0.f);
   s.lastAudio=0;
  }
 }else{
  // Keep direct PCM lane clocked into scratch while VST owns output so any
  // pause fade-out and DAC epoch receipts settle immediately without audible bleed.
  if(haveDirect){
   m->direct_cb(input,m->scratch,n,timeInfo,status,m->direct_user);
  }
  if(audio){
   std::memcpy(dst,audio->pcm,n*2*sizeof(float));
   if(s.lastAudio&&s.lastAudio!=audio->token){auto* previous=find(s.lastAudio);const auto length=std::min<U>(n,64);for(U i=0;i<length;++i){
    const float t=float(i+1)/length;for(U c=0;c<2;++c)dst[2*i+c]=(previous?previous->pcm[2*i+c]:s.lastSample[c])*(1-t)+dst[2*i+c]*t;
   }}
   ++s.refVersion;s.refPosition.store(audio->position);s.refState.store(audio->state);s.refCursor.store(at+n);++s.refVersion;
  }else{std::fill_n(dst,2*n,0.f);if(active)++s.drops;}
  s.lastAudio=audio?audio->token:0;
 }
 if(haveDirect){
  int cur=2;
  if(!m->direct_state.compare_exchange_strong(cur,1,std::memory_order_acq_rel,std::memory_order_acquire)){
   m->direct_state.store(4,std::memory_order_release);
  }
 }
 s.lastSample[0]=dst[2*n-2];s.lastSample[1]=dst[2*n-1];
 float peak=0;Q count=0,silent=s.silentFrames.load();
 for(U i=0;i<n;++i){
  float a=std::max(std::abs(dst[2*i]),std::abs(dst[2*i+1]));peak=std::max(peak,a);
  if(a>0.00001f){++count;silent=0;}else ++silent;
 }
 s.audibleFrames.fetch_add(count);s.silentFrames.store(silent);
 s.peakMillionths.store(static_cast<Q>(std::min(peak,1000.f)*1000000));
 for(U i=0;i<2;++i)s.ring[i].head.store(heads[i]+(rows[i]!=nullptr),std::memory_order_release);
 s.cursor.store(at+n,std::memory_order_release);return 0;
}
struct Producer{
 Mapping* m;Q token;Callback cb;void* user;Reference release;Snapshot snap;U capacity;
 std::atomic<bool> stop{false},done{false};std::atomic<int> fault{0};std::thread thread;
 bool render(float* out,U frames,View& view){
  for(U offset=0;offset<frames;){
   const U count=std::min(capacity,frames-offset);
   if(cb(nullptr,out+2*offset,count,nullptr,0,user)||snap(user,&view)||view.fault)return false;
   offset+=count;
  }
  return true;
 }
 void run(){
  auto timer=CreateWaitableTimerExW(nullptr,nullptr,CREATE_WAITABLE_TIMER_HIGH_RESOLUTION,TIMER_ALL_ACCESS);
  if(!timer){fault.store(1);done.store(true);return;}
  DWORD taskIndex=0;
  HANDLE scheduling=AvSetMmThreadCharacteristicsW(L"Pro Audio",&taskIndex);
  if(scheduling)AvSetMmThreadPriority(scheduling,AVRT_PRIORITY_HIGH);
  else SetThreadPriority(GetCurrentThread(),THREAD_PRIORITY_HIGHEST);
  auto& s=*m->shared;auto& r=s.ring[token&1];auto n=s.frames.load();Q next=s.cursor.load()+n;
  View view{};view.size=sizeof(View);view.version=3;
  auto wait=[&](){LARGE_INTEGER d{};d.QuadPart=-2500;return SetWaitableTimer(timer,&d,0,nullptr,nullptr,FALSE)&&WaitForSingleObject(timer,100)==WAIT_OBJECT_0;};
  while(!stop.load()){
   auto tail=r.tail.load();auto at=s.cursor.load();
   if(tail-r.head.load()>=2||next>at+2*n){if(!wait()){fault.store(2);break;}continue;}
   auto& b=r.rows[tail%depth];
   if(next<at)next=at;
   if(s.selected.load()!=token&&view.has_timeline&&view.state==2){
    auto v=s.refVersion.load();auto anchor=s.refCursor.load();auto pos=s.refPosition.load();auto state=s.refState.load();
    if(!(v&1)&&v==s.refVersion.load()&&state==2&&anchor&&next>=anchor){
     S delta=pos+S(next-anchor)-view.position;
     if(delta>0&&delta<96000){
      auto step=U(std::min<S>(delta,n));if(!render(b.pcm,step,view)){fault.store(3);break;}
      continue;
     }
     // Allow queued transport/seek commands to progress when ahead: the next blocks
     // are rendered early, then the physical clock catches up without an audio cut.
     if(delta<0&&delta>-96000){if(!wait()){fault.store(2);break;}next=std::max(next+n,s.cursor.load());continue;}
    }
   }
   if(!render(b.pcm,n,view)){fault.store(3);break;}
   if(!std::all_of(b.pcm,b.pcm+2*n,[](float x){return std::isfinite(x);})) {fault.store(4);break;}
   b.token=token;b.at=next;b.sequence=view.sequence;b.position=view.position;b.state=view.state;
   b.valid=view.has_timeline&&!view.parked&&!view.fault;next+=n;
   if(b.at>=s.cursor.load())r.tail.store(tail+1,std::memory_order_release);
  }
  if(scheduling)AvRevertMmThreadCharacteristics(scheduling);
  CloseHandle(timer);done.store(true);
 }
};
API Producer* kept_producer_v2(Mapping*m,Q token,Callback cb,void* user,Reference retain,Reference release,Snapshot snapshot,U capacity){
 if(!m||!token||!cb||!user||!retain||!release||!snapshot||!m->shared->frames.load()||!capacity||capacity>maxFrames)return nullptr;
 auto* p=new(std::nothrow)Producer{m,token,cb,user,release,snapshot,capacity};if(!p)return nullptr;
 retain(user);try{p->thread=std::thread(&Producer::run,p);}catch(...){release(user);delete p;return nullptr;}return p;
}
API void kept_stop(Producer*p){p->stop.store(true);}
API U kept_producer_state(Producer*p){return p->fault.load()?2:p->done.load()?1:0;}
API int kept_destroy(Producer*p){if(!p->done.load())return 0;p->thread.join();p->release(p->user);delete p;return 1;}
