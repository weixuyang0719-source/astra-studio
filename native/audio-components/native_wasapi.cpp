// WASAPI endpoint host. COM, render and clock calls remain on one native owner thread.
#define NOMINMAX
#include <windows.h>
#include <mmdeviceapi.h>
#include <audioclient.h>
#include <functiondiscoverykeys_devpkey.h>
#include <ksmedia.h>
#include <avrt.h>
#include <wrl/client.h>
#include <atomic>
#include <thread>
#include <mutex>
#include <vector>
#include <string>
#include <algorithm>
#include <cmath>
#include <cstring>
#include "native_wasapi.h"
using Microsoft::WRL::ComPtr;
namespace {
struct Timing { double input,current,output; };
static double qpc(){LARGE_INTEGER n,f;QueryPerformanceCounter(&n);QueryPerformanceFrequency(&f);return double(n.QuadPart)/f.QuadPart;}
static bool raw_unsupported(HRESULT h){return h==AUDCLNT_E_RAW_MODE_UNSUPPORTED||h==E_NOINTERFACE||h==E_NOTIMPL;}
template<class Attempt> static HRESULT initialize_policy(bool raw,AstraWasapiInfo& i,Attempt attempt){
 HRESULT h=attempt(raw);if(FAILED(h)&&raw&&raw_unsupported(h)){i.fallback_error=h;h=attempt(false);}return h;
}
static HRESULT endpoint(const wchar_t* id,ComPtr<IMMDevice>& d,AstraWasapiInfo& info){
 ComPtr<IMMDeviceEnumerator> e;HRESULT h=CoCreateInstance(__uuidof(MMDeviceEnumerator),nullptr,CLSCTX_ALL,IID_PPV_ARGS(&e));if(FAILED(h))return h;
 h=id&&*id?e->GetDevice(id,&d):e->GetDefaultAudioEndpoint(eRender,eConsole,&d);if(FAILED(h))return h;
 LPWSTR actual=nullptr;h=d->GetId(&actual);if(FAILED(h))return h;wcsncpy_s(info.endpoint,actual,_TRUNCATE);CoTaskMemFree(actual);
 ComPtr<IPropertyStore> props;if(SUCCEEDED(d->OpenPropertyStore(STGM_READ,&props))){PROPVARIANT v;PropVariantInit(&v);if(SUCCEEDED(props->GetValue(PKEY_Device_FriendlyName,&v))&&v.vt==VT_LPWSTR)WideCharToMultiByte(CP_UTF8,0,v.pwszVal,-1,info.name,256,nullptr,nullptr);PropVariantClear(&v);}return S_OK;
}
static HRESULT format_info(const WAVEFORMATEX* f,AstraWasapiInfo& i){
 uint32_t valid=f->wBitsPerSample,mask=0;bool floating=f->wFormatTag==WAVE_FORMAT_IEEE_FLOAT;bool pcm=f->wFormatTag==WAVE_FORMAT_PCM;
 if(f->wFormatTag==WAVE_FORMAT_EXTENSIBLE&&f->cbSize>=22){auto x=(const WAVEFORMATEXTENSIBLE*)f;valid=x->Samples.wValidBitsPerSample;mask=x->dwChannelMask;floating=x->SubFormat==KSDATAFORMAT_SUBTYPE_IEEE_FLOAT;pcm=x->SubFormat==KSDATAFORMAT_SUBTYPE_PCM;}
 if(!f->nChannels||f->nChannels>32||f->nSamplesPerSec<8000||f->nSamplesPerSec>384000||(!floating&&!pcm)||(floating&&f->wBitsPerSample!=32)||(pcm&&f->wBitsPerSample!=16&&f->wBitsPerSample!=24&&f->wBitsPerSample!=32))return AUDCLNT_E_UNSUPPORTED_FORMAT;
 if(!valid||valid>f->wBitsPerSample||(floating&&valid!=32))return AUDCLNT_E_UNSUPPORTED_FORMAT;
 unsigned speakers=0;for(uint32_t m=mask;m;m>>=1)speakers+=m&1;
 if(mask&&speakers!=f->nChannels)return AUDCLNT_E_UNSUPPORTED_FORMAT;
 if(f->nChannels==1&&mask&&mask!=SPEAKER_FRONT_CENTER)return AUDCLNT_E_UNSUPPORTED_FORMAT;
 if(f->nChannels>=2&&((mask&&((mask&3)!=3))||(!mask&&f->nChannels>2)))return AUDCLNT_E_UNSUPPORTED_FORMAT;
 i.valid_bits=valid;i.channel_mask=mask;i.rate=f->nSamplesPerSec;i.channels=f->nChannels;i.bits=f->wBitsPerSample;i.is_float=floating;return S_OK;
}
// A testable driver boundary. Tests inject this class, never real COM/device APIs.
struct Device {
 virtual ~Device()=default;
 virtual HRESULT initialize(const std::wstring&,uint32_t,uint32_t,bool,AstraWasapiInfo&,HANDLE)=0;
 virtual HRESULT start()=0;virtual HRESULT padding(uint32_t&)=0;
 virtual HRESULT acquire(uint32_t,BYTE*&)=0;virtual HRESULT release(uint32_t)=0;
 virtual HRESULT clock(uint64_t&,uint64_t&)=0;virtual HRESULT stop()=0;
 virtual HRESULT shutdown()=0;
};
// IAudioClient3 only permits EVENTCALLBACK stream flags. NOPERSIST is valid
// for legacy IAudioClient::Initialize, not InitializeSharedAudioStream.
template<class Client> static HRESULT initialize_shared3(Client* c,UINT32 period,const WAVEFORMATEX* format){
 return c->InitializeSharedAudioStream(AUDCLNT_STREAMFLAGS_EVENTCALLBACK,period,format,nullptr);
}
struct ComDevice:Device {
 ComPtr<IMMDevice> dev;ComPtr<IAudioClient> client;ComPtr<IAudioRenderClient> render;ComPtr<IAudioClock> clk;uint64_t freq=0;
 HRESULT initialize(const std::wstring& id,uint32_t rate,uint32_t period,bool raw,AstraWasapiInfo& i,HANDLE event)override{
  HRESULT h=endpoint(id.c_str(),dev,i);if(FAILED(h))return h;
  auto attempt=[&](bool wantRaw)->HRESULT{
   client.Reset();HRESULT r=dev->Activate(__uuidof(IAudioClient),CLSCTX_ALL,nullptr,(void**)&client);if(FAILED(r))return r;
   if(wantRaw){ComPtr<IAudioClient2> c2;r=client.As(&c2);if(FAILED(r))return r;AudioClientProperties p={};p.cbSize=sizeof(p);p.eCategory=AudioCategory_Media;p.Options=AUDCLNT_STREAMOPTIONS_RAW;r=c2->SetClientProperties(&p);if(FAILED(r))return r;}
   WAVEFORMATEX* format=nullptr;r=client->GetMixFormat(&format);if(FAILED(r))return r;
   r=format_info(format,i);if(SUCCEEDED(r)&&i.rate!=rate)r=AUDCLNT_E_UNSUPPORTED_FORMAT;
   if(SUCCEEDED(r)){
    // Engine-selected default period unless an explicit legal fundamental multiple is requested.
    ComPtr<IAudioClient3> c3;bool initialized=false;
    if(SUCCEEDED(client.As(&c3))){UINT32 def=0,fund=0,min=0,max=0;r=c3->GetSharedModeEnginePeriod(format,&def,&fund,&min,&max);
     if(SUCCEEDED(r)){UINT32 requested=period?period:def;if(requested<min||requested>max||!fund||requested%fund)r=E_INVALIDARG;
      else {r=initialize_shared3(c3.Get(),requested,format);initialized=SUCCEEDED(r);}}
     // Do not hide device errors or retry an already initialized client.
     if(FAILED(r)&&r!=E_NOINTERFACE&&r!=E_NOTIMPL){CoTaskMemFree(format);return r;}
    }
    if(!initialized)r=client->Initialize(AUDCLNT_SHAREMODE_SHARED,AUDCLNT_STREAMFLAGS_EVENTCALLBACK|AUDCLNT_STREAMFLAGS_NOPERSIST,0,0,format,nullptr);
   }
   CoTaskMemFree(format);if(FAILED(r))return r;i.raw=wantRaw;return S_OK;
  };
  h=initialize_policy(raw,i,attempt);if(FAILED(h))return h;
  h=client->GetBufferSize(&i.buffer_frames);if(FAILED(h))return h;
  h=client->SetEventHandle(event);if(FAILED(h))return h;
  h=client->GetService(IID_PPV_ARGS(&render));if(FAILED(h))return h;
  h=client->GetService(IID_PPV_ARGS(&clk));if(FAILED(h))return h;
  h=clk->GetFrequency(&freq);i.clock_frequency=double(freq);return h;
 }
 HRESULT start()override{return client->Start();}
 HRESULT padding(uint32_t& n)override{return client->GetCurrentPadding(&n);}
 HRESULT acquire(uint32_t n,BYTE*& p)override{return render->GetBuffer(n,&p);}
 HRESULT release(uint32_t n)override{return render->ReleaseBuffer(n,0);}
 HRESULT clock(uint64_t& p,uint64_t& t)override{return clk->GetPosition(&p,&t);}
 HRESULT stop()override{return client?client->Stop():S_OK;}
 HRESULT shutdown()override{clk.Reset();render.Reset();client.Reset();dev.Reset();return S_OK;}
};
static void convert(const float* in,BYTE* out,uint32_t frames,const AstraWasapiInfo& i){
 for(uint32_t f=0;f<frames;f++)for(uint32_t ch=0;ch<i.channels;ch++){
  float v=i.channels==1?(in[2*f]+in[2*f+1])*.5f:(ch<2?in[2*f+ch]:0.f);
  if(!std::isfinite(v))v=0;v=std::max(-1.f,std::min(1.f,v));
  size_t ix=size_t(f)*i.channels+ch;
  if(i.is_float)reinterpret_cast<float*>(out)[ix]=v;
  else {uint32_t valid=i.valid_bits;int64_t max=(1LL<<(valid-1))-1;int64_t val=std::llround(double(v)*max);uint64_t encoded=uint64_t(val)<<(i.bits-valid);BYTE* p=out+ix*(i.bits/8);for(uint32_t b=0;b<i.bits/8;b++)p[b]=BYTE(encoded>>(8*b));}
 }
}
struct Host {
 AstraPaCallback cb;void* user;AstraHostReference releaseRef;
 std::mutex mu;AstraHostSnapshot snap{};AstraWasapiInfo info{};std::wstring id;uint32_t rate=0,period=0;bool raw=false,opened=false;
 HANDLE wake=CreateEvent(nullptr,FALSE,FALSE,nullptr),stopEvent=CreateEvent(nullptr,TRUE,FALSE,nullptr);
 std::atomic<uint64_t> callbackCount{0};std::atomic<uint32_t> observed{0};std::atomic<double> current{0},dacTime{0},latency{0};
 std::atomic<bool> requested{false},closing{false},done{false};std::thread worker;Device* injected=nullptr;Device* unresolvedDevice=nullptr;
 Host(AstraPaCallback c,void* u,AstraHostReference r):cb(c),user(u),releaseRef(r){snap.size=sizeof(snap);snap.version=1;info.size=sizeof(info);info.version=1;snap.state=1;}
 ~Host(){if(worker.joinable())worker.join();CloseHandle(wake);CloseHandle(stopEvent);}
 void run(){
  HRESULT co=CoInitializeEx(nullptr,COINIT_MULTITHREADED),h=S_OK;bool started=false;Device* d=nullptr;DWORD task=0;HANDLE av=nullptr;
  if(FAILED(co)){h=co;goto finish;}
  WaitForSingleObject(wake,INFINITE);if(closing)goto finish;
  {std::lock_guard<std::mutex> lock(mu);snap.state=2;}
  d=injected?injected:new(std::nothrow) ComDevice;if(!d){h=E_OUTOFMEMORY;goto finish;}
  h=d->initialize(id,rate,period,raw,info,wake);if(FAILED(h)||closing)goto finish;
  if(!info.buffer_frames||info.buffer_frames>384000||!info.rate){h=E_INVALIDARG;goto finish;}
  {
   std::vector<float> pcm;try{pcm.resize(size_t(info.buffer_frames)*2);}catch(...){h=E_OUTOFMEMORY;goto finish;}uint64_t submitted=0;
   av=AvSetMmThreadCharacteristicsW(L"Pro Audio",&task);
   {std::lock_guard<std::mutex> lock(mu);snap.sample_rate=info.rate;strncpy_s(snap.device,info.name,_TRUNCATE);if(info.fallback_error)strcpy_s(snap.message,"RAW unsupported; native WASAPI shared active");}
   auto fill=[&]()->HRESULT{
    uint32_t padding=0;HRESULT r=d->padding(padding);if(FAILED(r))return r;if(padding>info.buffer_frames)return E_FAIL;
    uint32_t n=info.buffer_frames-padding;if(!n)return S_OK;
    uint64_t position=0,clockQpc=0;if(started){r=d->clock(position,clockQpc);if(FAILED(r))return r;}
    double now=qpc(),heard=info.clock_frequency>0?double(position)/info.clock_frequency:0;
    // IAudioClock QPC is already in 100ns units, not raw QueryPerformanceCounter ticks.
    double stamp=double(clockQpc)*1e-7;double queue=std::max(0.,double(submitted)/info.rate-heard);
    double dac=started?std::max(now,stamp+queue):now+double(padding)/info.rate;
    Timing timing{0,now,dac};BYTE* output=nullptr;r=d->acquire(n,output);if(FAILED(r))return r;
    cb(nullptr,pcm.data(),n,&timing,0,user);convert(pcm.data(),output,n,info);
    r=d->release(n);if(FAILED(r))return r;submitted+=n;
    callbackCount.fetch_add(1,std::memory_order_relaxed);observed.store(n,std::memory_order_relaxed);current.store(now,std::memory_order_relaxed);dacTime.store(dac,std::memory_order_relaxed);latency.store(dac-now,std::memory_order_relaxed);return S_OK;
   };
   // Prime native callback before Start so initial commands are applied and no PCM is discarded.
   h=fill();if(FAILED(h)||closing)goto finish;
   h=d->start();if(FAILED(h))goto finish;started=true;{std::lock_guard<std::mutex> lock(mu);opened=true;snap.state=3;}
   HANDLE waits[]={stopEvent,wake};
   while(!closing){DWORD w=WaitForMultipleObjects(2,waits,FALSE,2000);if(w==WAIT_OBJECT_0)break;if(w==WAIT_TIMEOUT){h=AUDCLNT_E_DEVICE_INVALIDATED;break;}if(w!=WAIT_OBJECT_0+1){h=HRESULT_FROM_WIN32(GetLastError());break;}h=fill();if(FAILED(h))break;}
  }
 finish:
  if(av)AvRevertMmThreadCharacteristics(av);
  HRESULT stopResult=S_OK,closeResult=S_OK;if(d){if(started)stopResult=d->stop();closeResult=d->shutdown();if(SUCCEEDED(closeResult))delete d;else unresolvedDevice=d;}
  // All callbacks execute synchronously on this thread. Once shutdown returns no callback can race release.
  if(SUCCEEDED(closeResult)){if(releaseRef)releaseRef(user);user=nullptr;}
  if(SUCCEEDED(co))CoUninitialize();
  {std::lock_guard<std::mutex> lock(mu);snap.released=SUCCEEDED(closeResult);snap.error=FAILED(h)?h:(FAILED(stopResult)?stopResult:closeResult);snap.state=snap.released?(snap.error?6:5):7;if(snap.error)snprintf(snap.message,sizeof(snap.message),"WASAPI error 0x%08X",uint32_t(snap.error));}
  done=true;
 }
};
}
AH_API int astra_wasapi_probe(const wchar_t* id,AstraWasapiInfo* out){
 if(!out||out->size!=sizeof(*out)||out->version!=1)return E_INVALIDARG;
 HRESULT result=E_FAIL;std::wstring endpointId=id?id:L"";
 std::thread t([&]{HRESULT co=CoInitializeEx(nullptr,COINIT_MULTITHREADED);if(FAILED(co)){result=co;return;}ComPtr<IMMDevice>d;ComPtr<IAudioClient>c;AstraWasapiInfo i{};i.size=sizeof(i);i.version=1;result=endpoint(endpointId.c_str(),d,i);if(SUCCEEDED(result))result=d->Activate(__uuidof(IAudioClient),CLSCTX_ALL,nullptr,(void**)&c);WAVEFORMATEX*f=nullptr;if(SUCCEEDED(result))result=c->GetMixFormat(&f);if(SUCCEEDED(result))result=format_info(f,i);if(f)CoTaskMemFree(f);if(SUCCEEDED(result))*out=i;c.Reset();d.Reset();CoUninitialize();});t.join();return result;
}
AH_API void* astra_wasapi_create_owned(AstraPaCallback cb,void* user,AstraHostReference retain,AstraHostReference release){
 if(!cb||!user||!retain||!release)return nullptr;Host*h=nullptr;bool held=false;
 try{h=new Host(cb,user,release);if(!h->wake||!h->stopEvent){delete h;return nullptr;}retain(user);held=true;h->worker=std::thread([h]{h->run();});return h;}catch(...){if(held)release(user);delete h;return nullptr;}
}
AH_API int astra_wasapi_open(void*p,const wchar_t* id,uint32_t rate,uint32_t period,int raw){
 if(!p||rate<8000||rate>384000||(raw!=0&&raw!=1))return 0;auto*h=(Host*)p;std::lock_guard<std::mutex>lock(h->mu);if(h->requested||h->closing||h->done)return 0;try{h->id=id?id:L"";}catch(...){return 0;}h->rate=rate;h->period=period;h->raw=raw;h->requested=true;SetEvent(h->wake);return 1;
}
AH_API int astra_wasapi_snapshot(void*p,AstraHostSnapshot*s){if(!p||!s||s->size!=sizeof(*s)||s->version!=1)return 0;auto*h=(Host*)p;std::lock_guard<std::mutex>l(h->mu);*s=h->snap;s->callbacks=h->callbackCount.load();s->observed_frames=h->observed.load();s->current_time=h->current.load();s->dac_time=h->dacTime.load();s->output_latency=h->latency.load();return 1;}
AH_API int astra_wasapi_info(void*p,AstraWasapiInfo*i){if(!p||!i||i->size!=sizeof(*i)||i->version!=1)return 0;auto*h=(Host*)p;std::lock_guard<std::mutex>l(h->mu);if(!h->opened||!h->info.rate||(h->snap.state!=3&&h->snap.state!=5))return 0;*i=h->info;return 1;}
AH_API int astra_wasapi_route(void*p,AstraHostRoute*r){if(!p||!r||r->size!=sizeof(*r)||r->version!=1)return 0;auto*h=(Host*)p;std::lock_guard<std::mutex>l(h->mu);if(!h->opened||!h->info.rate||(h->snap.state!=3&&h->snap.state!=5))return 0;r->host_type=13;r->device_index=-1;r->shared=1;r->raw=h->info.raw;strcpy_s(r->host,h->info.raw?"Native WASAPI RAW":"Native WASAPI shared");return 1;}
AH_API int astra_wasapi_close(void*p){if(!p)return 0;auto*h=(Host*)p;h->closing=true;SetEvent(h->stopEvent);SetEvent(h->wake);return 1;}
AH_API int astra_wasapi_destroy(void*p){if(!p)return 1;auto*h=(Host*)p;if(!h->done||!h->snap.released)return 0;delete h;return 1;}
