#include "midi_winmm.h"
#define NOMINMAX
#include <windows.h>
#include <mmsystem.h>
#include <mutex>
#include <atomic>
#include <string>
#include <cwctype>
#include <climits>
#include <cstring>
struct WinmmApi{
 HMODULE dll=nullptr;
 decltype(&midiOutGetNumDevs) count=nullptr;decltype(&midiOutGetDevCapsW) caps=nullptr;
 decltype(&midiOutOpen) open=nullptr;decltype(&midiOutShortMsg) send=nullptr;decltype(&midiOutClose) close=nullptr;decltype(&midiOutReset) reset=nullptr;
 bool system(){dll=LoadLibraryExW(L"winmm.dll",nullptr,LOAD_LIBRARY_SEARCH_SYSTEM32);if(!dll)return false;
 count=reinterpret_cast<decltype(count)>(GetProcAddress(dll,"midiOutGetNumDevs"));caps=reinterpret_cast<decltype(caps)>(GetProcAddress(dll,"midiOutGetDevCapsW"));open=reinterpret_cast<decltype(open)>(GetProcAddress(dll,"midiOutOpen"));send=reinterpret_cast<decltype(send)>(GetProcAddress(dll,"midiOutShortMsg"));close=reinterpret_cast<decltype(close)>(GetProcAddress(dll,"midiOutClose"));reset=reinterpret_cast<decltype(reset)>(GetProcAddress(dll,"midiOutReset"));return count&&caps&&open&&send&&close&&reset;}
 ~WinmmApi(){if(dll)FreeLibrary(dll);}
};
static_assert(std::atomic<uint64_t>::is_always_lock_free,"diagnostic counters must be lock-free");
struct DriverMetric {
 std::atomic<uint64_t> calls{0},completed{0},errors{0},total_ns{0},max_ns{0};
 void finish(uint64_t ns,bool error){total_ns.fetch_add(ns,std::memory_order_relaxed);auto old=max_ns.load(std::memory_order_relaxed);while(old<ns&&!max_ns.compare_exchange_weak(old,ns,std::memory_order_relaxed)){}if(error)errors.fetch_add(1,std::memory_order_relaxed);completed.fetch_add(1,std::memory_order_release);}
};
struct WinmmSender{
 DriverMetric send_metric,reset_metric;LARGE_INTEGER qpc_hz{};
 WinmmSender(){QueryPerformanceFrequency(&qpc_hz);}
 uint64_t elapsed(LARGE_INTEGER start){LARGE_INTEGER end;QueryPerformanceCounter(&end);return uint64_t(double(end.QuadPart-start.QuadPart)*1e9/double(qpc_hz.QuadPart));}
 WinmmApi api;std::mutex lock;HMIDIOUT handle=nullptr;unsigned borrowers=0;int error=0;bool needs_panic=false;std::wstring name;
 static int rank(const MIDIOUTCAPSW&c){
  if(c.wTechnology!=7)return -1;std::wstring n=c.szPname;const std::wstring prefix=L"VirtualMIDISynth #";
  if(c.wMid==65535&&n.compare(0,prefix.size(),prefix)==0){auto tail=n.substr(prefix.size());if(tail.empty()||tail[0]<L'1'||tail[0]>L'9')return -1;int id=0;for(wchar_t x:tail){if(x<L'0'||x>L'9'||id>1000000)return -1;id=id*10+x-L'0';}return id;}
  for(auto&x:n)x=std::towlower(x);
  if(c.wMid==1&&(n==L"microsoft gs wavetable synth"||n==L"microsoft gs \u6ce2\u8868\u5408\u6210\u5668"))return INT_MAX-1;return -1;
 }
 int open(const wchar_t*requested){std::lock_guard<std::mutex>l(lock);if(handle)return 0;if(borrowers)return -2;
  UINT selected=UINT_MAX;int best=INT_MAX;std::wstring selected_name;
  for(UINT i=0;i<api.count();i++){MIDIOUTCAPSW c{};if(api.caps(i,&c,sizeof(c)))continue;int r=rank(c);if(r<0||(requested&&std::wstring(requested)!=c.szPname))continue;if(r<best){best=r;selected=i;selected_name=c.szPname;}}
  if(selected==UINT_MAX)return error=-3;
  HMIDIOUT h=nullptr;auto e=api.open(&h,selected,0,0,0);if(e)return error=e;
  handle=h;name=selected_name;MIDIOUTCAPSW actual{};
  if(api.caps(reinterpret_cast<UINT_PTR>(handle),&actual,sizeof(actual))||rank(actual)<0||selected_name!=actual.szPname){auto closed=api.close(handle);if(!closed){handle=nullptr;name.clear();}return error=closed?int(closed):-4;}
  error=0;return 0;
 }
 int emit(uint32_t word){std::lock_guard<std::mutex>l(lock);if(needs_panic)return error?error:-8;if(!handle)return error=-5;
  unsigned status=word&255,type=status&240;if(status<128||status>239||(word&0xff808000u)||((type==0xc0||type==0xd0)&&(word&0xff0000u)))return error=-6;
  LARGE_INTEGER began;send_metric.calls.fetch_add(1,std::memory_order_relaxed);QueryPerformanceCounter(&began);auto e=api.send(handle,word);send_metric.finish(elapsed(began),e!=0);if(e){error=e;needs_panic=true;}return e;}
 int panic(){std::lock_guard<std::mutex>l(lock);if(borrowers)return -2;if(!handle)return 0;needs_panic=true;LARGE_INTEGER began;reset_metric.calls.fetch_add(1,std::memory_order_relaxed);QueryPerformanceCounter(&began);auto e=api.reset?api.reset(handle):MMRESULT(1);reset_metric.finish(elapsed(began),e!=0);if(e)return error=e;needs_panic=false;error=0;return 0;}
 int stop(){std::lock_guard<std::mutex>l(lock);if(borrowers)return -2;if(needs_panic)return -8;if(!handle)return 0;auto e=api.close(handle);if(e)return error=e;handle=nullptr;name.clear();error=0;return 0;}
};
WM_API void* astra_winmm_create(){try{auto p=new WinmmSender;if(!p->api.system()){delete p;return nullptr;}return p;}catch(...){return nullptr;}}
WM_API int astra_winmm_open(void*p,const wchar_t*n){if(!p)return -1;try{return static_cast<WinmmSender*>(p)->open(n);}catch(...){return -7;}}
WM_API int astra_winmm_send(void*p,uint32_t m){if(!p)return -1;return static_cast<WinmmSender*>(p)->emit(m);}
WM_API void astra_winmm_retain(void*p){if(p){auto s=static_cast<WinmmSender*>(p);std::lock_guard<std::mutex>l(s->lock);++s->borrowers;}}
WM_API void astra_winmm_release(void*p){if(p){auto s=static_cast<WinmmSender*>(p);std::lock_guard<std::mutex>l(s->lock);if(s->borrowers)--s->borrowers;}}
WM_API int astra_winmm_panic(void*p){return p?static_cast<WinmmSender*>(p)->panic():-1;}
WM_API int astra_winmm_close(void*p){return p?static_cast<WinmmSender*>(p)->stop():-1;}
WM_API int astra_winmm_snapshot(void*p,AstraWinmmStatus*out){if(!p||!out||out->size<sizeof(*out))return -1;auto s=static_cast<WinmmSender*>(p);std::lock_guard<std::mutex>l(s->lock);out->version=1;out->open=s->handle!=nullptr;out->borrowers=s->borrowers;out->error=s->error;out->reserved=0;wcsncpy_s(out->name,s->name.c_str(),_TRUNCATE);return 0;}
WM_API int astra_winmm_destroy(void*p){if(!p)return 1;auto s=static_cast<WinmmSender*>(p);{std::lock_guard<std::mutex>l(s->lock);if(s->handle||s->borrowers)return 0;}delete s;return 1;}

WM_API int astra_winmm_diagnostics(void*p,AstraWinmmDiagnostics*out){if(!p||!out||out->size<sizeof(*out))return -1;auto s=static_cast<WinmmSender*>(p);out->version=1;
#define METRIC(prefix, metric) out->prefix##_calls=s->metric.calls.load(std::memory_order_relaxed);out->prefix##_completed=s->metric.completed.load(std::memory_order_acquire);out->prefix##_errors=s->metric.errors.load(std::memory_order_relaxed);out->prefix##_total_ns=s->metric.total_ns.load(std::memory_order_relaxed);out->prefix##_max_ns=s->metric.max_ns.load(std::memory_order_relaxed);
 METRIC(send,send_metric) METRIC(reset,reset_metric)
#undef METRIC
 return 0;}
