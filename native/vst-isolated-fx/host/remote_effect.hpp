#pragma once
#include <windows.h>
#include <algorithm>
#include <atomic>
#include <cmath>
#include <cstdint>
#include <cstring>
#include <memory>
#include <mutex>
#include <new>
#include <string>
#include <thread>
#include <vector>
#include "astra_vst_host.h"

namespace astra_remote_fx {
constexpr uint32_t maxFrames=32768;
using Render=int(*)(void*,const float*,float*,uint32_t,const AvRtContext*);
struct Shared {
 std::atomic<uint64_t> request{0},done{0};
 std::atomic<int> failed{0};
 bool skipped=false;
 uint32_t frames=0;
 AvRtContext context{};
 float input[maxFrames*2],output[maxFrames*2];
};
struct Link {
 HANDLE mapping=nullptr, work=nullptr, completed=nullptr;
 Shared* data=nullptr;
 ~Link(){
  if(data)UnmapViewOfFile(data);
  if(completed)CloseHandle(completed);
  if(work)CloseHandle(work);
  if(mapping)CloseHandle(mapping);
 }
 bool open(const wchar_t* name,bool owner){
  if(!name||!name[0])return false;
  mapping=owner?CreateFileMappingW(INVALID_HANDLE_VALUE,nullptr,PAGE_READWRITE,0,sizeof(Shared),name):
                OpenFileMappingW(FILE_MAP_ALL_ACCESS,FALSE,name);
  if(!mapping||(owner&&GetLastError()==ERROR_ALREADY_EXISTS))return false;
  data=static_cast<Shared*>(MapViewOfFile(mapping,FILE_MAP_ALL_ACCESS,0,0,sizeof(Shared)));
  if(!data)return false;
  if(owner)new(data)Shared{};
  auto prefix=std::wstring(name);
  auto event=[&](const wchar_t* suffix){
   auto path=prefix+suffix;
   return owner?CreateEventW(nullptr,FALSE,FALSE,path.c_str()):
                OpenEventW(SYNCHRONIZE|EVENT_MODIFY_STATE,FALSE,path.c_str());
  };
  work=event(L".request");completed=event(L".done");return work&&completed;
 }
};
struct Client : Link {
 uint64_t serial=0;
 int64_t missStart=0, frequency=0;
 uint32_t latency=0,write=0,valid=0;
 int64_t nextPosition=0;
 bool wasPlaying=false;
 std::vector<float> history,dry;
 HANDLE timer=nullptr;
 std::atomic<int> fault{0};
 bool wet=false;
 float last[2]{};
 ~Client(){if(timer)CloseHandle(timer);}
 bool configure(uint32_t delay,uint32_t maxFrames){
  latency=delay;history.resize(size_t(delay)*2);dry.resize(size_t(maxFrames)*2);
  LARGE_INTEGER f{};QueryPerformanceFrequency(&f);frequency=f.QuadPart;
  timer=CreateWaitableTimerExW(nullptr,nullptr,CREATE_WAITABLE_TIMER_HIGH_RESOLUTION,TIMER_ALL_ACCESS);
  return timer!=nullptr;
 }
 const float* alignedDry(const float* src,uint32_t n,const AvRtContext& x){
  if(!latency)return src;
  const bool playing=(x.flags&AV_RT_PLAYING)!=0;
  if(playing!=wasPlaying||(playing&&x.sample_position!=nextPosition)){write=0;valid=0;}
  nextPosition=x.sample_position+(playing?n:0);wasPlaying=playing;
  for(uint32_t i=0;i<n;++i){
   for(uint32_t c=0;c<2;++c){
    const float current=src[2*i+c];float& sample=history[2*write+c];
    dry[2*i+c]=valid==latency?sample:current;
    sample=current;
   }
   valid=std::min(valid+1,latency);if(++write==latency)write=0;
  }
  return dry.data();
 }
 void mix(float* dst,const float* src,uint32_t frames,bool active){
  if(active!=wet){
   const uint32_t fade=std::min<uint32_t>(frames,32);
   for(uint32_t i=0;i<fade;++i){
    float t=float(i+1)/fade;
    for(uint32_t c=0;c<2;++c)dst[2*i+c]=last[c]*(1-t)+src[2*i+c]*t;
   }
   if(dst!=src)std::copy(src+2*fade,src+2*frames,dst+2*fade);
  }else if(dst!=src)std::copy_n(src,2*frames,dst);
  last[0]=dst[2*frames-2];last[1]=dst[2*frames-1];wet=active;
 }
 void process(float* pcm,uint32_t n,const AvRtContext& x,int64_t deadline){
  auto& s=*data;
  bool success=false,skipped=false;
  const auto fallback=alignedDry(pcm,n,x);
  if(!fault.load(std::memory_order_relaxed)&&n<=maxFrames){
   if(s.failed.load(std::memory_order_acquire))fault.store(2);
   else if(s.done.load(std::memory_order_acquire)==serial){
    s.context=x;s.frames=n;std::copy_n(pcm,2*n,s.input);
    s.request.store(++serial,std::memory_order_release);SetEvent(work);
    // All slots share this one block deadline. This code executes on the
    // ahead-of-device render producer. The ASIO callback does not wait.
    LARGE_INTEGER now{},due{};QueryPerformanceCounter(&now);
    due.QuadPart=-std::max<int64_t>(1,(deadline-now.QuadPart)*10000000/frequency);
    HANDLE events[2]={completed,timer};
    const bool armed=SetWaitableTimer(timer,&due,0,nullptr,nullptr,FALSE);
    for(;;){
     if(s.done.load(std::memory_order_acquire)==serial){
      skipped=s.skipped;
      success=!skipped&&!s.failed.load(std::memory_order_acquire);break;
     }
     QueryPerformanceCounter(&now);
     if(now.QuadPart>=deadline||!armed)break;
     if(WaitForMultipleObjects(2,events,FALSE,100)!=WAIT_OBJECT_0)break;
    }
   }
   if(success||skipped)missStart=0;
   else{
    LARGE_INTEGER clock{};QueryPerformanceCounter(&clock);
    if(!missStart)missStart=clock.QuadPart;
    else if(clock.QuadPart-missStart>frequency/5)fault.store(1);
   }
  }
  mix(pcm,success?s.output:fallback,n,success);
 }
};
struct Server : Link {
 void* plugin=nullptr;Render render=nullptr;
 std::timed_mutex vendor;
 std::atomic<bool> stop{false};std::thread thread;
 void run(){
  SetThreadPriority(GetCurrentThread(),THREAD_PRIORITY_HIGHEST);
  auto& s=*data;
  while(!stop.load()){
   if(WaitForSingleObject(work,100)!=WAIT_OBJECT_0)continue;
   if(stop.load())break;
   auto id=s.request.load(std::memory_order_acquire);
   if(id==s.done.load())continue;
   auto n=s.frames;
   std::unique_lock<std::timed_mutex> access(vendor,std::try_to_lock);
   s.skipped=!access.owns_lock();
   if(!s.skipped&&(!n||n>maxFrames||render(plugin,s.input,s.output,n,&s.context)||
           !std::all_of(s.output,s.output+2*n,[](float v){return std::isfinite(v);})))s.failed.store(1);
   s.done.store(id,std::memory_order_release);SetEvent(completed);
  }
 }
 ~Server(){stop.store(true);if(work)SetEvent(work);if(thread.joinable())thread.join();}
};
}
