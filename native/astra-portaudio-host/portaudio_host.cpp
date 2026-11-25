#include "portaudio_host.h"
#include <windows.h>
#include <objbase.h>
#include <atomic>
#include <thread>
#include <mutex>
#include <condition_variable>
#include <vector>
#include <string>
#include <cstring>
#include <cstdio>
#include <cmath>
#include <algorithm>
namespace {
std::atomic<bool> hostLease{false};
using Stream=void;
struct DeviceInfo {int version;const char* name;int host;int inputs,outputs;double lowIn,lowOut,highIn,highOut,rate;};
struct HostInfo {int version,type;const char* name;int count,defaultIn,defaultOut;};
struct Parameters {int device,channels;unsigned long format;double latency;void* specific;};
// The bundled sounddevice PortAudio DLL implements the v19.7 WASAPI ABI.
using WasapiHostProcessor = void(*)(void*,long,void*,long,void*);
struct WasapiStreamInfo {
 unsigned long size;
 int hostApiType;
 unsigned long version,flags,channelMask;
 WasapiHostProcessor hostProcessorOutput,hostProcessorInput;
 int threadPriority,streamCategory,streamOption;
};
static_assert(sizeof(void*)!=8 || sizeof(WasapiStreamInfo)==56);
constexpr int wasapiHostType=13;
constexpr unsigned long wasapiAutoConvert=1UL<<6;
struct StreamInfo {int version;double inputLatency,outputLatency,sampleRate;};
struct TimeInfo {double input,current,output;};
template<size_t N> void text(char(&out)[N],const char* in){strncpy_s(out,N,in?in:"",_TRUNCATE);}
struct Api {
 int(*asioSizes)(int,long*,long*,long*,long*)=nullptr;int(*asioPanel)(int,void*)=nullptr;
 HMODULE dll=nullptr;int(*init)();int(*terminate)();int(*count)();const DeviceInfo*(*device)(int);const HostInfo*(*host)(int);
 int(*open)(Stream**,const Parameters*,const Parameters*,double,unsigned long,unsigned long,AstraPaCallback,void*);
 int(*start)(Stream*);int(*active)(Stream*);int(*abort)(Stream*);int(*close)(Stream*);const StreamInfo*(*info)(Stream*);const char*(*error)(int);
 template<class T> void bind(T& p,const char* n){p=reinterpret_cast<T>(GetProcAddress(dll,n));if(!p)throw std::runtime_error(n);}
 void load(const wchar_t* path){dll=LoadLibraryExW(path,nullptr,LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR|LOAD_LIBRARY_SEARCH_DEFAULT_DIRS);if(!dll)throw std::runtime_error("PortAudio DLL load failed");
 bind(init,"Pa_Initialize");bind(terminate,"Pa_Terminate");bind(count,"Pa_GetDeviceCount");bind(device,"Pa_GetDeviceInfo");bind(host,"Pa_GetHostApiInfo");bind(open,"Pa_OpenStream");bind(start,"Pa_StartStream");bind(active,"Pa_IsStreamActive");bind(abort,"Pa_AbortStream");bind(close,"Pa_CloseStream");bind(info,"Pa_GetStreamInfo");bind(error,"Pa_GetErrorText");asioSizes=reinterpret_cast<decltype(asioSizes)>(GetProcAddress(dll,"PaAsio_GetAvailableBufferSizes"));asioPanel=reinterpret_cast<decltype(asioPanel)>(GetProcAddress(dll,"PaAsio_ShowControlPanel"));}
};
struct Owner {
 std::wstring dll;AstraPaCallback callback;void* user;AstraHostReference release=nullptr;std::thread thread;
 std::mutex mutex;std::condition_variable cv;std::vector<AstraHostDevice> devices;
 AstraHostSnapshot s{};AstraHostRoute route{sizeof(AstraHostRoute),1,0,-1,0,0,{}};std::string requested;int requestedHost=3;double rate=48000;uint32_t block=0;bool openPending=false,closePending=false;
 bool controlOnly=false,controlPending=false,showPanel=false;uintptr_t panelParent=0;AstraAsioControl control{sizeof(AstraAsioControl),1};
 std::atomic<bool> done{false};std::atomic<uint64_t> calls{0},flags{0};std::atomic<uint32_t> frames{0};std::atomic<double> now{0},dac{0};
 Owner(const wchar_t* p,AstraPaCallback cb,void* u,AstraHostReference r=nullptr,bool only=false):dll(p),callback(cb),user(u),release(r),controlOnly(only){s.size=sizeof(s);s.version=1;s.state=0;thread=std::thread([this]{run();});}
 static int process(const void* input,void* output,unsigned long n,const void* ti,unsigned long status,void* p) noexcept {
  auto o=static_cast<Owner*>(p);o->frames.store(static_cast<uint32_t>(n),std::memory_order_relaxed);o->calls.fetch_add(1,std::memory_order_relaxed);o->flags.fetch_or(status,std::memory_order_relaxed);
  if(ti){auto t=static_cast<const TimeInfo*>(ti);o->now.store(t->current,std::memory_order_relaxed);o->dac.store(t->output,std::memory_order_relaxed);}
  return o->callback(input,output,n,ti,status,o->user);
 }
 void state(unsigned st,int err=0,const char* msg=""){std::lock_guard<std::mutex> l(mutex);s.state=st;s.error=err;text(s.message,msg);}
 void run() noexcept {
  Api a;Stream* stream=nullptr;bool initialized=false,uncertain=false;
  WasapiStreamInfo sharedWasapi{};
  sharedWasapi.size=sizeof(sharedWasapi);
  sharedWasapi.hostApiType=wasapiHostType;
  sharedWasapi.version=1;
  sharedWasapi.flags=wasapiAutoConvert;
  HRESULT com=CoInitializeEx(nullptr,COINIT_APARTMENTTHREADED);bool uninit=SUCCEEDED(com);
  try{
   if(FAILED(com))throw std::runtime_error("ASIO owner COM initialization failed");
   a.load(dll.c_str());int err=a.init();if(err<0)throw std::runtime_error(a.error(err));initialized=true;
   int n=a.count();if(n<0)throw std::runtime_error(a.error(n));
   {std::lock_guard<std::mutex> l(mutex);for(int i=0;i<n;i++){auto d=a.device(i);if(!d)continue;auto h=a.host(d->host);if(!h||(h->type!=3&&h->type!=13))continue;AstraHostDevice row{};row.size=sizeof(row);row.version=1;row.index=i;row.host_type=h->type;row.outputs=d->outputs;row.inputs=d->inputs;row.default_rate=d->rate;row.low_latency=d->lowOut;text(row.name,d->name);text(row.host,h->name);devices.push_back(row);}s.state=1;}
   for(;;){
    std::string name;double hz;uint32_t bs;int hostType;bool ctl=false,show=false;uintptr_t parent=0;
    {std::unique_lock<std::mutex> l(mutex);cv.wait_for(l,std::chrono::milliseconds(10),[&]{return closePending||openPending||controlPending;});if(closePending)break;
     if(controlPending){controlPending=false;ctl=true;show=showPanel;parent=panelParent;name=requested;}
     else if(!openPending){l.unlock();if(stream){int astate=a.active(stream);if(astate<=0)throw std::runtime_error(astate<0?a.error(astate):"native stream stopped unexpectedly");}MSG m;while(PeekMessageW(&m,nullptr,0,0,PM_REMOVE)){TranslateMessage(&m);DispatchMessageW(&m);}continue;}
     else{openPending=false;name=requested;hostType=requestedHost;hz=rate;bs=block;s.state=2;}}
    if(ctl){
     int index=-1,matches=0;for(auto& d:devices)if(d.host_type==3&&name==d.name){index=d.index;matches++;}
     int error=0;long minimum=0,maximum=0,preferred=0,granularity=0;
     if(matches!=1||!a.asioSizes||(show&&!a.asioPanel))error=-1;
     else{if(show)error=a.asioPanel(index,reinterpret_cast<void*>(parent));if(!error)error=a.asioSizes(index,&minimum,&maximum,&preferred,&granularity);}
     {std::lock_guard<std::mutex> l(mutex);control.error=error;control.state=error?3:2;control.min_frames=minimum;control.max_frames=maximum;control.preferred_frames=preferred;control.granularity=granularity;control.completed_id=control.request_id;text(control.device,name.c_str());}
     continue;
    }
    int index=-1,matches=0;
    for(auto& d:devices){if(d.host_type!=hostType||d.outputs<2)continue;
     bool match=name==d.name;
     if(name.empty()){auto info=a.device(d.index);auto hinfo=info?a.host(info->host):nullptr;match=hinfo&&hinfo->defaultOut==d.index;}
     if(match){index=d.index;++matches;}
    }
    if(matches!=1){state(1,-1,matches>1?"ambiguous native device name":"native stereo device not found for requested host API");continue;}
    auto d=a.device(index);auto hi=d?a.host(d->host):nullptr;
    if(!hi||hi->type!=hostType)throw std::runtime_error("device host API changed during open");
    // Let the Windows shared mixer convert when its format differs from the
    // application/VST clock. ASIO still receives the exact requested parameters.
    if(hostType==3&&a.asioSizes){long mn=0,mx=0,pref=0,gran=0;int e=a.asioSizes(index,&mn,&mx,&pref,&gran);std::lock_guard<std::mutex>l(mutex);control.state=e?3:2;control.error=e;control.min_frames=mn;control.max_frames=mx;control.preferred_frames=pref;control.granularity=gran;text(control.device,d->name);}
    Parameters out{index,2,1,d->lowOut,hostType==wasapiHostType?&sharedWasapi:nullptr};
    int e=a.open(&stream,nullptr,&out,hz,bs,0,process,this);
    if(e<0){
     if(stream){uncertain=true;throw std::runtime_error("failed open returned non-null stream; ownership unresolved");}
     char reason[256];
     std::snprintf(reason,sizeof(reason),
                   "Pa_OpenStream %s: %s (%d), requested %.0f Hz, device default %.0f Hz, buffer %u, endpoint %s",
                   hostType==wasapiHostType?"WASAPI shared":"ASIO",a.error(e),e,hz,d->rate,
                   static_cast<unsigned>(bs),d->name);
     state(1,e,reason);
     continue;
    }
    if(!stream)throw std::runtime_error("successful open returned null stream");
    auto info=a.info(stream);if(!info)throw std::runtime_error("missing opened stream parameters");
    {std::lock_guard<std::mutex> l(mutex);s.sample_rate=info->sampleRate;s.output_latency=info->outputLatency;text(s.device,d->name);route.host_type=hi->type;route.device_index=index;route.shared=hi->type==13?1:-1;route.raw=0;text(route.host,hi->name);}
    if(std::abs(info->sampleRate-hz)>0.01)throw std::runtime_error("actual rate differs from engine rate");
    e=a.start(stream);if(e<0)throw std::runtime_error(a.error(e));state(3);
   }
  }catch(const std::exception& e){state(6,-1,e.what());}catch(...){state(6,-1,"native host exception");}
  // Close runs on the same owner thread. A blocked driver leaves done=false and cannot
  // be mistaken for released. No timeout fabricates success or frees callback/core memory.
  if(stream&&!uncertain){a.abort(stream);int e=a.close(stream);if(e<0){uncertain=true;state(7,e,a.error(e));}else stream=nullptr;}
  if(initialized&&!uncertain){int e=a.terminate();if(e<0){uncertain=true;state(7,e,a.error(e));}}
  if(!uncertain){if(a.dll)FreeLibrary(a.dll);if(uninit)CoUninitialize();{std::lock_guard<std::mutex> l(mutex);s.released=1;if(s.state!=6)s.state=5;}if(release)release(user);hostLease.store(false,std::memory_order_release);done.store(true,std::memory_order_release);}
  // If close failed, retain DLL and opaque handle for process lifetime. Never permit fallback
  // based on an unproven release. Driver recovery requires a future explicit recovery API.
 }
};
}
AH_API void* astra_host_create(const wchar_t* path,AstraPaCallback cb,void* user){if(!path||!cb||!user)return nullptr;bool expected=false;if(!hostLease.compare_exchange_strong(expected,true))return nullptr;try{return new Owner(path,cb,user);}catch(...){hostLease=false;return nullptr;}}
AH_API void* astra_host_create_owned(const wchar_t* path,AstraPaCallback cb,void* user,AstraHostReference retain,AstraHostReference release){
 if(!path||!cb||!user||!retain||!release)return nullptr;
 bool expected=false;if(!hostLease.compare_exchange_strong(expected,true))return nullptr;
 retain(user);try{return new Owner(path,cb,user,release);}catch(...){release(user);hostLease=false;return nullptr;}
}
AH_API int astra_host_open_backend(void* p,int32_t hostType,const char* name,double hz,uint32_t block){if(!p||(hostType!=3&&hostType!=13)||!std::isfinite(hz)||hz<8000||hz>384000||block>1048576)return 0;auto o=static_cast<Owner*>(p);std::lock_guard<std::mutex> l(o->mutex);if(o->controlOnly||o->s.state!=1||o->openPending||o->closePending)return 0;try{o->requested=name?name:"";}catch(...){return 0;}o->requestedHost=hostType;o->rate=hz;o->block=block;o->openPending=true;o->cv.notify_one();return 1;}
AH_API int astra_host_open(void* p,const char* name,double hz,uint32_t block){if(!name||!*name)return 0;return astra_host_open_backend(p,3,name,hz,block);}
AH_API int astra_host_route(void* p,AstraHostRoute* r){if(!p||!r||r->size!=sizeof(*r)||r->version!=1)return 0;auto o=static_cast<Owner*>(p);std::lock_guard<std::mutex> l(o->mutex);*r=o->route;return 1;}
AH_API int astra_host_close(void* p){if(!p)return 0;auto o=static_cast<Owner*>(p);std::lock_guard<std::mutex> l(o->mutex);o->closePending=true;o->cv.notify_one();return 1;}
AH_API int astra_host_snapshot(void* p,AstraHostSnapshot* s){if(!p||!s||s->size!=sizeof(*s)||s->version!=1)return 0;auto o=static_cast<Owner*>(p);std::lock_guard<std::mutex> l(o->mutex);*s=o->s;s->callbacks=o->calls.load();s->status_flags=o->flags.load();s->observed_frames=o->frames.load();s->current_time=o->now.load();s->dac_time=o->dac.load();return 1;}
AH_API int astra_host_device_count(void* p){if(!p)return -1;auto o=static_cast<Owner*>(p);std::lock_guard<std::mutex> l(o->mutex);return static_cast<int>(o->devices.size());}
AH_API int astra_host_device(void* p,uint32_t i,AstraHostDevice* d){if(!p||!d||d->size!=sizeof(*d)||d->version!=1)return 0;auto o=static_cast<Owner*>(p);std::lock_guard<std::mutex> l(o->mutex);if(i>=o->devices.size())return 0;*d=o->devices[i];return 1;}
AH_API int astra_host_destroy(void* p){if(!p)return 1;auto o=static_cast<Owner*>(p);if(!o->done.load(std::memory_order_acquire))return 0;if(o->thread.joinable())o->thread.join();delete o;return 1;}

AH_API void* astra_host_control_create(const wchar_t* path){if(!path)return nullptr;bool expected=false;if(!hostLease.compare_exchange_strong(expected,true))return nullptr;try{return new Owner(path,nullptr,nullptr,nullptr,true);}catch(...){hostLease=false;return nullptr;}}
AH_API int astra_host_asio_request(void* p,const char* name,uintptr_t parent,int show){if(!p||!name||!*name||(show!=0&&show!=1))return 0;auto o=static_cast<Owner*>(p);std::lock_guard<std::mutex>l(o->mutex);if(!o->controlOnly||o->s.state!=1||o->closePending||o->control.state==1)return 0;try{o->requested=name;}catch(...){return 0;}o->panelParent=parent;o->showPanel=show;o->controlPending=true;o->control.state=1;o->control.error=0;o->control.request_id++;o->cv.notify_one();return 1;}
AH_API int astra_host_asio_control(void* p,AstraAsioControl* c){if(!p||!c||c->size!=sizeof(*c)||c->version!=1)return 0;auto o=static_cast<Owner*>(p);std::lock_guard<std::mutex>l(o->mutex);*c=o->control;return 1;}
