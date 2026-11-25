#include "pcm_loader.h"
#include <windows.h>
#include <atomic>
#include <vector>
#include <string>
#include <limits>
#include <memory>
#include <cstring>
#include <cmath>
#include <new>
using count_t=int64_t;
struct SF_INFO_ABI { count_t frames; int samplerate,channels,format,sections,seekable; };
struct Cancel{std::atomic<bool> requested{false};};
struct Pcm{std::vector<float> data;};
struct Decoder {
 HMODULE dll=nullptr; void* file=nullptr;
 using Open=void*(*)(const wchar_t*,int,SF_INFO_ABI*);
 using Read=count_t(*)(void*,float*,count_t);
 using Close=int(*)(void*);
 using Error=const char*(*)(void*);
 Open open=nullptr;Read read=nullptr;Close close=nullptr;Error error=nullptr;
 ~Decoder(){if(file&&close)close(file);if(dll)FreeLibrary(dll);}
 bool init(const wchar_t* path){dll=LoadLibraryExW(path,nullptr,LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR|LOAD_LIBRARY_SEARCH_DEFAULT_DIRS);if(!dll)return false;
 open=reinterpret_cast<Open>(GetProcAddress(dll,"sf_wchar_open"));read=reinterpret_cast<Read>(GetProcAddress(dll,"sf_readf_float"));close=reinterpret_cast<Close>(GetProcAddress(dll,"sf_close"));error=reinterpret_cast<Error>(GetProcAddress(dll,"sf_strerror"));return open&&read&&close&&error;}
};
static bool absolute(const wchar_t* p){return p&&((p[0]&&p[1]==L':'&&(p[2]==L'\\'||p[2]==L'/'))||(p[0]==L'\\'&&p[1]==L'\\'));}
static int fail(AstraPcmInfo* info,int code,const char* message){if(info){strncpy_s(info->error,message?message:"decoder error",_TRUNCATE);}return code;}
PCM_API uint32_t astra_pcm_abi_version(){return 1;}
PCM_API void* astra_pcm_cancel_create(){return new(std::nothrow) Cancel;}
PCM_API void astra_pcm_cancel_request(void* p){if(p)static_cast<Cancel*>(p)->requested.store(true,std::memory_order_release);}
PCM_API void astra_pcm_cancel_destroy(void* p){delete static_cast<Cancel*>(p);}
PCM_API int astra_pcm_load(const wchar_t* dll,const wchar_t* file,uint64_t budget,void* cancel,void** result,AstraPcmInfo* info){
 if(result)*result=nullptr;
 if(!info||info->size<sizeof(AstraPcmInfo))return 3;
 auto size=info->size;*info={};info->size=size;info->version=1;
 if(!result||!absolute(dll)||!absolute(file))return fail(info,3,"absolute DLL/audio paths and result pointer required");
 auto canceled=[&]{return cancel&&static_cast<Cancel*>(cancel)->requested.load(std::memory_order_acquire);};
 if(canceled())return fail(info,1,"canceled");
 try{
  Decoder d;if(!d.init(dll))return fail(info,4,"cannot load libsndfile or required exports");
  SF_INFO_ABI f{};d.file=d.open(file,0x10,&f);if(!d.file)return fail(info,4,d.error(nullptr));
  if(f.frames<=0||f.samplerate<8000||f.samplerate>384000||(f.channels!=1&&f.channels!=2))return fail(info,3,"requires nonempty mono/stereo audio, sample rate 8000..384000");
  constexpr uint64_t chunk_frames=8192, scratch_bytes=chunk_frames*2*sizeof(float);
  auto frames=static_cast<uint64_t>(f.frames);
  if(frames>(std::numeric_limits<size_t>::max)()/(2*sizeof(float)))return fail(info,2,"PCM size exceeds address space");
  auto bytes=frames*2*sizeof(float);
  info->frames=frames;info->bytes=bytes;info->sample_rate=f.samplerate;info->source_channels=f.channels;info->output_channels=2;info->source_sample_rate=f.samplerate;
  if(budget<scratch_bytes||bytes>budget-scratch_bytes)return fail(info,2,"PCM plus decode scratch exceeds budget");
  if(canceled())return fail(info,1,"canceled");
  auto pcm=std::make_unique<Pcm>();pcm->data.resize(static_cast<size_t>(frames*2));
  std::vector<float> scratch(chunk_frames*f.channels);uint64_t at=0;
  while(at<frames){
   if(canceled())return fail(info,1,"canceled");
   auto count=static_cast<count_t>((std::min)(chunk_frames,frames-at));
   auto got=d.read(d.file,scratch.data(),count);
   if(got<=0||got>count)return fail(info,5,"unexpected end of file or decode error");
   for(count_t i=0;i<got;i++){auto l=scratch[static_cast<size_t>(i*f.channels)];auto r=f.channels==2?scratch[static_cast<size_t>(i*2+1)]:l;
    if(!std::isfinite(l)||!std::isfinite(r))return fail(info,5,"nonfinite PCM sample rejected");
    pcm->data[static_cast<size_t>((at+i)*2)]=l;pcm->data[static_cast<size_t>((at+i)*2+1)]=r;}
   at+=got;
  }
  if(canceled())return fail(info,1,"canceled");
  *result=pcm.release();return 0;
 }catch(const std::bad_alloc&){return fail(info,6,"PCM allocation failed");}catch(...){return fail(info,4,"unexpected native decoder error");}
}
PCM_API const float* astra_pcm_data(void* p){return p?static_cast<Pcm*>(p)->data.data():nullptr;}
PCM_API void astra_pcm_release(void* p){delete static_cast<Pcm*>(p);}
struct SRC_DATA_ABI { const float* data_in;float* data_out;long input_frames,output_frames,input_frames_used,output_frames_gen;int end_of_input;double src_ratio; };
struct Resampler {
 HMODULE dll=nullptr;void* state=nullptr;
 using New=void*(*)(int,int,int*);using Process=int(*)(void*,SRC_DATA_ABI*);using Delete=void*(*)(void*);
 New create=nullptr;Process process=nullptr;Delete destroy=nullptr;
 ~Resampler(){if(state&&destroy)destroy(state);if(dll)FreeLibrary(dll);}
 bool init(const wchar_t* path){dll=LoadLibraryExW(path,nullptr,LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR|LOAD_LIBRARY_SEARCH_DEFAULT_DIRS);if(!dll)return false;
 create=reinterpret_cast<New>(GetProcAddress(dll,"src_new"));process=reinterpret_cast<Process>(GetProcAddress(dll,"src_process"));destroy=reinterpret_cast<Delete>(GetProcAddress(dll,"src_delete"));if(!create||!process||!destroy)return false;int err=0;state=create(0,2,&err);return state&&err==0;}
};
PCM_API int astra_pcm_load_resampled(const wchar_t* sndfile,const wchar_t* samplerate,const wchar_t* file,uint32_t target,uint64_t budget,void* cancel,void** result,AstraPcmInfo* info){
 if(result)*result=nullptr;
 if(!result||!info||info->size<sizeof(AstraPcmInfo))return 3;
 if(target!=0&&(target<8000||target>384000))return fail(info,3,"target rate outside 8000..384000");
 void* raw=nullptr;int status=astra_pcm_load(sndfile,file,budget,cancel,&raw,info);if(status)return status;
 std::unique_ptr<Pcm> input(static_cast<Pcm*>(raw));
 if(!target||target==info->sample_rate){*result=input.release();return 0;}
 if(!absolute(samplerate))return fail(info,3,"absolute libsamplerate DLL path required");
 auto canceled=[&]{return cancel&&static_cast<Cancel*>(cancel)->requested.load(std::memory_order_acquire);};
 try{
  double ratio=static_cast<double>(target)/info->sample_rate;
  double predicted=std::ceil(info->frames*ratio)+256;
  if(predicted>=static_cast<double>((std::numeric_limits<size_t>::max)()/(2*sizeof(float))))return fail(info,2,"resampled PCM exceeds address space");
  uint64_t capacity=static_cast<uint64_t>(predicted),out_bytes=capacity*2*sizeof(float);
  // Additional reserve bounds decoder/filter state overhead, besides input and output arrays.
  constexpr uint64_t reserve=4*1024*1024;
  if(budget<reserve||info->bytes>budget-reserve||out_bytes>budget-reserve-info->bytes)return fail(info,2,"input plus resampled PCM exceeds budget");
  if(canceled())return fail(info,1,"canceled");
  Resampler r;if(!r.init(samplerate))return fail(info,4,"cannot initialize libsamplerate");
  auto output=std::make_unique<Pcm>();output->data.resize(static_cast<size_t>(capacity*2));
  uint64_t in_at=0,out_at=0;
  for(;;){
   if(canceled())return fail(info,1,"canceled");
   if(out_at>=capacity)return fail(info,5,"resampler output exceeds planned capacity");
   SRC_DATA_ABI d{};d.data_in=input->data.data()+in_at*2;d.data_out=output->data.data()+out_at*2;
   d.input_frames=static_cast<long>((std::min)(uint64_t(8192),info->frames-in_at));d.output_frames=static_cast<long>((std::min)(uint64_t(8192),capacity-out_at));
   d.end_of_input=in_at+d.input_frames==info->frames;d.src_ratio=ratio;
   if(r.process(r.state,&d))return fail(info,5,"libsamplerate conversion failed");
   if(d.input_frames_used<0||d.output_frames_gen<0||d.input_frames_used>d.input_frames||d.output_frames_gen>d.output_frames)return fail(info,5,"invalid resampler progress");
   in_at+=d.input_frames_used;out_at+=d.output_frames_gen;
   if(!d.input_frames_used&&!d.output_frames_gen){if(in_at==info->frames)break;return fail(info,5,"resampler stalled");}
  }
  if(canceled())return fail(info,1,"canceled");
  output->data.resize(static_cast<size_t>(out_at*2));info->frames=out_at;info->bytes=out_at*2*sizeof(float);info->source_sample_rate=info->sample_rate;info->sample_rate=target;
  *result=output.release();return 0;
 }catch(const std::bad_alloc&){return fail(info,6,"resampled PCM allocation failed");}catch(...){return fail(info,4,"unexpected native resampler error");}
}
