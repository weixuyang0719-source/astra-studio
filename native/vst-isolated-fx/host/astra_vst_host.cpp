#include "insert_chain.hpp"
#include "remote_effect.hpp"
#include "astra_vst_host.h"
#include "param_changes.hpp"
#include "component_handler.hpp"
#include "editor_frame.hpp"
#include "visual_feedback.hpp"
#include <type_traits>
#include "public.sdk/source/vst/hosting/module.h"
#include "public.sdk/source/vst/hosting/plugprovider.h"
#include "public.sdk/source/vst/hosting/hostclasses.h"
#include "public.sdk/source/vst/hosting/processdata.h"
#include "public.sdk/source/vst/hosting/eventlist.h"
#include "public.sdk/source/common/memorystream.h"
#include "pluginterfaces/vst/ivsteditcontroller.h"
#include "pluginterfaces/vst/ivstparameterchanges.h"
#include "pluginterfaces/vst/ivstmidicontrollers.h"
#include "pluginterfaces/vst/ivstprocesscontext.h"
#include "pluginterfaces/gui/iplugview.h"
#include <windows.h>
#include <algorithm>
#include <array>
#include <atomic>
#include <cmath>
#include <cstring>
#include <memory>
#include <sstream>
#include <stdexcept>
#include <thread>
#include <vector>
using namespace Steinberg;
using namespace Steinberg::Vst;
namespace {
thread_local std::string error;
void fail(const char* s){ throw std::runtime_error(s); }
std::string quote(const std::string& s){ std::string r="\""; for(unsigned char c:s){ if(c=='"'||c=='\\'){r+='\\';r+=c;} else if(c<32){char b[7];sprintf_s(b,"\\u%04x",c);r+=b;} else r+=c;} return r+'"'; }
bool same(const TUID a,const TUID b){return FUnknownPrivate::iidEqual(a,b);}
#define STATIC_REF uint32 PLUGIN_API addRef() override{return 1000;} uint32 PLUGIN_API release() override{return 1000;}
constexpr uint32_t MAX_EVENTS=4096;
using astra::vst_params::Changes;
constexpr auto kStructuralRestart = kIoChanged | kReloadComponent | kParamIDMappingChanged;
constexpr auto kRefreshRestart = kLatencyChanged | kMidiCCAssignmentChanged | kParamValuesChanged;
using astra::vst_host::Edit;
using astra::vst_host::Handler;
using astra::vst_host::FeedbackContext;
struct Host {
 std::thread::id thread=std::this_thread::get_id();
 IPtr<FeedbackContext> context;
 VST3::Hosting::Module::Ptr module;
 std::unique_ptr<PlugProvider> provider;
 IPtr<IComponent> component;IPtr<IEditController> controller;FUnknownPtr<IAudioProcessor> processor;
 IPtr<IPlugView> view;astra::vst_host::EditorFrame frame;Handler handler;
 unsigned editorChanges=0;
 struct EditorChange {
  unsigned& depth;
  explicit EditorChange(unsigned& nesting):depth(nesting){++depth;}
  ~EditorChange(){--depth;}
 };
 HostProcessData data;EventList input{AV_RT_MIDI_LIMIT},output{MAX_EVENTS};Changes params,outputParams;
 ProcessContext pc{};std::array<std::array<ParamID,130>,16> midiMap{};
 std::vector<uint8_t> state;std::string classId;uint32_t maxBlock=0,latency=0;double rate=0;bool active=false,processing=false;
 bool effect=false;uint32_t refs=1;InsertChains inserts;
 std::unique_ptr<astra_remote_fx::Client> remote;
 const int64_t ticksPerSecond=[](){LARGE_INTEGER f{};QueryPerformanceFrequency(&f);return f.QuadPart;}();
 std::array<uint64_t,8> openMicros{};
 explicit Host(bool withEffectFeedback=false):context(owned(new FeedbackContext(withEffectFeedback))){
  for(auto& ch:midiMap)ch.fill(kNoParamId);
 }
 void ui(){if(thread!=std::this_thread::get_id())fail("Control operation must run on the creating UI thread");}
 void detach(){
  EditorChange change(editorChanges);
  context->showEditor(false);
  if(view){if(frame.attached)view->removed();view->setFrame(nullptr);frame.reset();view=nullptr;}
 }
 ~Host(){
  detach();
  if(processor&&processing)processor->setProcessing(false);
  context->beforeDeactivate();
  if(component&&active)component->setActive(false);
  if(controller)controller->setComponentHandler(nullptr);
  data.unprepare();processor=nullptr;controller=nullptr;component=nullptr;
  provider.reset();context->clearAfterTerminate();module.reset();
 }
 void mapping(){for(auto& ch:midiMap)ch.fill(kNoParamId);if(!controller)return;FUnknownPtr<IMidiMapping> m(controller);if(m)for(int ch=0;ch<16;ch++)for(int cc=0;cc<130;cc++){ParamID id;if(m->getMidiControllerAssignment(0,ch,(CtrlNumber)cc,id)==kResultOk)midiMap[ch][cc]=id;}}
};
Host& host(void* h){if(!h)fail("Null host handle");return *static_cast<Host*>(h);}
void put64(std::vector<uint8_t>& v,uint64_t n){for(int i=0;i<8;i++)v.push_back(uint8_t(n>>(8*i)));}
uint64_t get64(const uint8_t* v){uint64_t n=0;for(int i=0;i<8;i++)n|=uint64_t(v[i])<<(8*i);return n;}
}
uint32_t astra_vst_abi_version(){return 1;}
uint64_t astra_vst_state_generation(void* h){return h?static_cast<Host*>(h)->handler.state_generation.load(std::memory_order_acquire):0;}
const char* astra_vst_last_error(){return error.c_str();}
uint64_t astra_vst_scan(const char* path,char* buffer,uint64_t cap){try{
 error.clear();if(!path)fail("Missing plugin path");std::string why;auto m=VST3::Hosting::Module::create(path,why);if(!m)throw std::runtime_error(why);
 std::ostringstream s;s<<"{\"architecture\":\"x64\",\"classes\":[";bool first=true;
 for(auto& c:m->getFactory().classInfos()){if(c.category()!=kVstAudioEffectClass)continue;if(!first)s<<',';first=false;s<<"{\"id\":"<<quote(c.ID().toString())<<",\"name\":"<<quote(c.name())<<",\"vendor\":"<<quote(c.vendor())<<",\"category\":"<<quote(c.subCategoriesString())<<",\"version\":"<<quote(c.version())<<",\"sdk_version\":"<<quote(c.sdkVersion())<<"}";}s<<"]}";auto value=s.str();if(buffer&&cap>=value.size()+1)memcpy(buffer,value.c_str(),value.size()+1);return value.size()+1;
 }catch(const std::exception& e){error=e.what();return 0;}catch(...){error="Plugin scan failed";return 0;}}
static void configure_effect_buses(Host& h){
 // Some fixed-layout effects reject setBusArrangements even though their
 // advertised mono or stereo main buses work. Preserve optional sidechains.
 auto usable=[&]{
  for(auto dir:{kInput,kOutput}){
   BusInfo bus{};
   if(h.component->getBusInfo(kAudio,dir,0,bus)!=kResultOk ||
      bus.busType!=kMain || bus.channelCount<1 || bus.channelCount>2)return false;
  }
  return true;
 };
 auto negotiate=[&](SpeakerArrangement input,SpeakerArrangement output){
  return h.processor->setBusArrangements(&input,1,&output,1)==kResultOk && usable();
 };
 if(negotiate(SpeakerArr::kStereo,SpeakerArr::kStereo)||usable())return;
 if(negotiate(SpeakerArr::kMono,SpeakerArr::kMono) ||
    negotiate(SpeakerArr::kMono,SpeakerArr::kStereo) ||
    negotiate(SpeakerArr::kStereo,SpeakerArr::kMono))return;
 fail("Effect has no compatible mono or stereo main bus arrangement");
}
static void* open_role(const char* path,const char* id,double rate,uint32_t block,bool effect){try{
 error.clear();if(!path||!id||!std::isfinite(rate)||rate<8000||rate>384000||block<1||block>32768)fail("Invalid host configuration");auto h=std::make_unique<Host>(effect);h->effect=effect;
 LARGE_INTEGER previous{};QueryPerformanceCounter(&previous);
 auto mark=[&](size_t stage){
  LARGE_INTEGER now{};QueryPerformanceCounter(&now);
  h->openMicros[stage]=uint64_t((now.QuadPart-previous.QuadPart)*1000000/h->ticksPerSecond);
  previous=now;
 };
 std::string why;h->module=VST3::Hosting::Module::create(path,why);if(!h->module)throw std::runtime_error(why);mark(0);
 bool found=false;for(auto& c:h->module->getFactory().classInfos())if(c.ID().toString()==id&&c.category()==kVstAudioEffectClass){
 h->module->getFactory().setHostContext(static_cast<IHostApplication*>(h->context.get()));PluginContextFactory::instance().setPluginContext(static_cast<IHostApplication*>(h->context.get()));h->provider=std::make_unique<PlugProvider>(h->module->getFactory(),c,true);
 const astra_legacy_ui::FactoryScope relayBootstrap(h->context->toController,h->context->toProcessor);
 if(!h->provider->initialize())fail("Plugin initialization failed");found=true;break;}
 if(!found)fail("Requested VST3 class was not found");h->classId=id;h->component=h->provider->getComponentPtr();h->controller=h->provider->getControllerPtr();h->processor=h->component;mark(1);
 if(!h->processor||h->processor->canProcessSampleSize(kSample32)!=kResultOk)fail("Plugin does not support float32 audio processing");
 h->context->bind(h->processor,h->controller);
 if(h->component->getBusCount(kAudio,kOutput)<1||(!effect&&h->component->getBusCount(kEvent,kInput)<1)||(effect&&h->component->getBusCount(kAudio,kInput)<1))fail("Plugin buses do not match the requested role");
 if(effect)configure_effect_buses(*h);
 BusInfo info{};if(h->component->getBusInfo(kAudio,kOutput,0,info)!=kResultOk||info.channelCount<1||info.channelCount>2)fail("First output must be mono or stereo in this host version");
 for(auto dir:{kInput,kOutput}){for(int i=0;i<h->component->getBusCount(kAudio,dir);i++)h->component->activateBus(kAudio,dir,i,i==0&&(dir==kOutput||effect));for(int i=0;i<h->component->getBusCount(kEvent,dir);i++)h->component->activateBus(kEvent,dir,i,!effect&&dir==kInput&&i==0);}
 mark(2);
 if(h->controller){h->controller->setComponentHandler(&h->handler);MemoryStream initial;if(h->component->getState(&initial)==kResultOk){initial.seek(0,IBStream::kIBSeekSet,nullptr);h->controller->setComponentState(&initial);}}
 mark(3);h->mapping();mark(4);
 ProcessSetup setup{kRealtime,kSample32,(int32)block,rate};if(h->processor->setupProcessing(setup)!=kResultOk)fail("Plugin setupProcessing failed");
 mark(5);
 if(!h->data.prepare(*h->component,block,kSample32))fail("Could not allocate plugin audio buses");
 mark(6);
 h->maxBlock=block;h->rate=rate;h->data.inputEvents=&h->input;h->data.outputEvents=&h->output;h->data.inputParameterChanges=&h->params;h->data.outputParameterChanges=&h->outputParams;h->data.processContext=&h->pc;h->data.processMode=kRealtime;
 if(h->component->setActive(true)!=kResultOk)fail("Plugin activation failed");h->active=true;h->context->afterActivate();auto r=h->processor->setProcessing(true);if(r!=kResultOk&&r!=kNotImplemented)fail("Plugin setProcessing failed");h->processing=true;h->latency=h->processor->getLatencySamples();h->handler.restart.store(0);mark(7);return h.release();
 }catch(const std::exception&e){error=e.what();return nullptr;}catch(...){error="Plugin load failed";return nullptr;}}
void* astra_vst_open(const char* p,const char* id,double r,uint32_t b){return open_role(p,id,r,b,false);}
void* astra_vst_open_effect(const char* p,const char* id,double r,uint32_t b){return open_role(p,id,r,b,true);}
void astra_vst_open_times_us_v1(void* handle,uint64_t* stages,uint32_t count){
 if(handle&&stages&&count==8)std::copy_n(host(handle).openMicros.data(),count,stages);
}
namespace {
void retain_insert(void* p){++static_cast<Host*>(p)->refs;}
void release_insert(void* p){auto* h=static_cast<Host*>(p);if(!--h->refs)delete h;}
template<class Midi>
int32_t process_block(void* handle,float* out,uint32_t frames,const Midi* events,
                      uint32_t count,int64_t pos,double tempo,uint32_t playing,const AvRtContext* x,const float* audioIn=nullptr) {
 constexpr bool extended=std::is_same_v<Midi,AvRtMidiEvent>;
 if(!handle)return -1;
 auto& h=*static_cast<Host*>(handle);
 if(!out||frames>h.maxBlock||count>(extended?AV_RT_MIDI_LIMIT:MAX_EVENTS)||
    (count&&!events)||!h.active||!h.processing)return -1;
 std::fill_n(out,frames*2,0.f);
 try {
  const auto restart=h.handler.restart.load(std::memory_order_acquire);
  // There is no host-side parameter-value display to invalidate on the live path.
  if(restart&(kStructuralRestart|kLatencyChanged|kMidiCCAssignmentChanged|
              (extended?0:kParamValuesChanged)))return -6;
  h.input.clear();h.output.clear();h.params.clear();h.outputParams.clear();
  // Snapshot a finite producer frontier. New UI edits are for a later call.
  const auto w=h.handler.write.load(std::memory_order_acquire);
  const auto r=h.handler.read.load(std::memory_order_relaxed);
  auto budget=(w+h.handler.edits.size()-r)%h.handler.edits.size();
  Edit edit;
  for(;budget&&h.handler.pop(edit);--budget)if(!h.params.setInitial(edit.id,edit.value))return -3;
  uint32_t previous=0;
  for(uint32_t i=0;i<count;i++){
   const auto& m=events[i];
   if(m.sample_offset>=frames||(i&&m.sample_offset<previous)||m.data1>127||m.data2>127)return -2;
   previous=m.sample_offset;
   const int type=m.status&0xf0,ch=m.status&15;
   Event e{};e.busIndex=0;e.sampleOffset=static_cast<int32>(m.sample_offset);e.flags=Event::kIsLive;
   int32 noteId=-1;
   if constexpr(extended){
    noteId=m.note_id;e.flags=m.live?Event::kIsLive:0;
    if(x->flags&AV_RT_CLOCK)e.ppqPosition=x->beat+(playing?m.sample_offset*x->bpm/(h.rate*60.):0.);
   }
   if(type==0x90&&m.data2){
    e.type=Event::kNoteOnEvent;e.noteOn.channel=ch;e.noteOn.pitch=m.data1;e.noteOn.velocity=m.data2/127.f;e.noteOn.noteId=noteId;
   }else if(type==0x80||type==0x90){
    e.type=Event::kNoteOffEvent;e.noteOff.channel=ch;e.noteOff.pitch=m.data1;e.noteOff.velocity=m.data2/127.f;e.noteOff.noteId=noteId;
   }else if(type==0xa0){
    e.type=Event::kPolyPressureEvent;e.polyPressure.channel=ch;e.polyPressure.pitch=m.data1;e.polyPressure.pressure=m.data2/127.f;e.polyPressure.noteId=noteId;
   }else{
    int cc=-1;double v=0;
    if(type==0xb0){cc=m.data1;v=m.data2/127.;}
    else if(type==0xe0){cc=kPitchBend;v=(m.data1+128*m.data2)/16383.;}
    else if(type==0xd0){cc=kAfterTouch;v=m.data1/127.;}
    if(cc>=0&&cc<130&&h.midiMap[ch][cc]!=kNoParamId&&!h.params.add(h.midiMap[ch][cc],m.sample_offset,v))return -3;
    continue;
   }
   if(h.input.addEvent(e)!=kResultOk)return -3;
  }
  const bool feedMain=h.effect&&audioIn&&h.data.numInputs>0;
  for(int b=feedMain?1:0;b<h.data.numInputs;b++){auto& bus=h.data.inputs[b];bus.silenceFlags=~uint64(0);for(int c=0;c<bus.numChannels;c++)std::fill_n(bus.channelBuffers32[c],frames,0.f);}
  if(feedMain){
   auto& bus=h.data.inputs[0];bus.silenceFlags=0;
   float* left=bus.channelBuffers32[0];
   if(bus.numChannels==1){
    for(uint32_t i=0;i<frames;++i)left[i]=.5f*audioIn[2*i]+.5f*audioIn[2*i+1];
   }else{
    float* right=bus.channelBuffers32[1];
    for(uint32_t i=0;i<frames;++i){left[i]=audioIn[2*i];right[i]=audioIn[2*i+1];}
   }
  }
  for(int b=0;b<h.data.numOutputs;b++){auto& bus=h.data.outputs[b];bus.silenceFlags=0;for(int c=0;c<bus.numChannels;c++)std::fill_n(bus.channelBuffers32[c],frames,0.f);}
  h.pc={};h.pc.sampleRate=h.rate;h.pc.projectTimeSamples=pos;
  h.pc.state=playing?ProcessContext::kPlaying:0;
  if constexpr(extended){
   if(x->flags&AV_RT_CLOCK){
    h.pc.projectTimeMusic=x->beat;h.pc.tempo=x->bpm;h.pc.barPositionMusic=x->bar_start_beat;
    h.pc.timeSigNumerator=x->numerator;h.pc.timeSigDenominator=x->denominator;
    h.pc.state|=ProcessContext::kTempoValid|ProcessContext::kProjectTimeMusicValid;
    if(x->flags&AV_RT_METER)h.pc.state|=ProcessContext::kBarPositionValid|ProcessContext::kTimeSigValid;
   }
   if(x->flags&AV_RT_CYCLE){
    h.pc.cycleStartMusic=x->cycle_start_beat;h.pc.cycleEndMusic=x->cycle_end_beat;
    h.pc.state|=ProcessContext::kCycleValid|ProcessContext::kCycleActive;
   }
  }else{
   h.pc.tempo=tempo;h.pc.projectTimeMusic=pos/h.rate*tempo/60.;
   h.pc.state|=ProcessContext::kTempoValid|ProcessContext::kProjectTimeMusicValid;
  }
  h.data.numSamples=frames;
  const auto code=[&]{
   if(!h.effect)return h.processor->process(h.data);
   // Mark this call only. Legacy notify short-circuits before any queue
   // locks, allocations, references or peer calls on the DSP thread.
   const astra_legacy_ui::ProcessScope processScope;
   return h.processor->process(h.data);
  }();
  if(code!=kResultOk)return -4;
  if(h.params.overflow||h.outputParams.overflow)return -3;
  auto& bus=h.data.outputs[0];
  for(uint32_t i=0;i<frames;i++){
   const float l=bus.channelBuffers32[0][i],r=bus.channelBuffers32[bus.numChannels>1?1:0][i];
   if(!std::isfinite(l)||!std::isfinite(r)){std::fill_n(out,frames*2,0.f);return -5;}
   out[2*i]=l;out[2*i+1]=r;
  }
  if(h.context->meters)h.context->meters->publish(h.outputParams);
  return 0;
 }catch(...){std::fill_n(out,frames*2,0.f);return -9;}
}
void refresh_state_config(Host& h){
 h.handler.restart.fetch_and(~kRefreshRestart);
 h.mapping();h.latency=h.processor->getLatencySamples();
}
}
int32_t astra_vst_process(void* h,float* out,uint32_t frames,const AvMidiEvent* events,uint32_t count,int64_t pos,double tempo,uint32_t playing){
 if(!std::isfinite(tempo)||tempo<=0)return -1;
 return process_block(h,out,frames,events,count,pos,tempo,playing,nullptr);
}
uint32_t astra_vst_rt_version(){return AV_RT_VERSION;}
int32_t astra_vst_rt_configuration(void* handle,double* rate,uint32_t* block){try{
 auto& h=host(handle);h.ui();if(!rate||!block||!h.active||!h.processing)fail("VST processing setup unavailable");
 *rate=h.rate;*block=h.maxBlock;return 0;
 }catch(const std::exception& e){error=e.what();return -1;}catch(...){error="VST processing setup unavailable";return -1;}}
int32_t astra_vst_process_rt(void* h,float* out,uint32_t frames,const AvRtMidiEvent* events,uint32_t count,const AvRtContext* x){
 if(!x||x->sample_position<0||x->reserved||(x->flags&~(AV_RT_PLAYING|AV_RT_CLOCK|AV_RT_CYCLE|AV_RT_METER)))return -1;
 if((x->flags&AV_RT_CLOCK)&&(!std::isfinite(x->beat)||!std::isfinite(x->bpm)||x->bpm<=0||
    !std::isfinite(x->bar_start_beat)))return -1;
 if((x->flags&AV_RT_METER)&&(!(x->flags&AV_RT_CLOCK)||x->numerator<=0||x->denominator<=0))return -1;
 if((x->flags&AV_RT_CYCLE)&&(!(x->flags&AV_RT_CLOCK)||!std::isfinite(x->cycle_start_beat)||
    !std::isfinite(x->cycle_end_beat)||x->cycle_end_beat<=x->cycle_start_beat))return -1;
 auto& instrument=host(h);
 if(frames)instrument.inserts.boundary();
 auto rc=process_block(h,out,frames,events,count,x->sample_position,x->bpm,x->flags&AV_RT_PLAYING,x);
 if(rc||!frames||!instrument.inserts.active)return rc;
 auto& chain=*instrument.inserts.active;
 LARGE_INTEGER begin{};QueryPerformanceCounter(&begin);
 const auto budget=static_cast<int64_t>(instrument.ticksPerSecond*double(frames)/instrument.rate*.75);
 for(auto slot:chain.slots){if(slot.bypass)continue;
  if(auto* remote=host(slot.handle).remote.get()){
   remote->process(out,frames,*x,begin.QuadPart+budget);continue;
  }
  rc=process_block<AvRtMidiEvent>(slot.handle,chain.scratch.data(),frames,nullptr,0,x->sample_position,x->bpm,x->flags&AV_RT_PLAYING,x,out);
  if(rc){std::fill_n(out,frames*2,0.f);return rc;}
  std::copy_n(chain.scratch.data(),frames*2,out);
 }
 return 0;
}
int32_t astra_vst_rt_rearm(void* handle){try{
 auto& h=host(handle);h.ui();
 if(h.handler.restart.load()&kStructuralRestart)fail("Plugin requested structural reconfiguration; reopen it");
 if(h.processing){auto r=h.processor->setProcessing(false);if(r!=kResultOk&&r!=kNotImplemented)fail("Plugin did not stop processing");h.processing=false;}
 h.context->beforeDeactivate();
 if(h.active){if(h.component->setActive(false)!=kResultOk)fail("Plugin did not deactivate");h.active=false;}
 if(h.component->setActive(true)!=kResultOk)fail("Plugin did not reactivate");h.active=true;
 h.context->afterActivate();
 auto r=h.processor->setProcessing(true);if(r!=kResultOk&&r!=kNotImplemented)fail("Plugin did not restart processing");h.processing=true;
 refresh_state_config(h);return 0;
 }catch(const std::exception& e){error=e.what();return -1;}catch(...){error="Plugin reactivation failed";return -1;}}
uint64_t astra_vst_get_state(void* handle,uint8_t* out,uint64_t capacity){try{auto&h=host(handle);h.ui();if(!out){MemoryStream component,controller;if(h.component->getState(&component)!=kResultOk)fail("Component state unavailable");if(h.controller)h.controller->getState(&controller);h.state.assign({'A','V','S','T',1,0,0,0});h.state.insert(h.state.end(),h.classId.begin(),h.classId.end());put64(h.state,component.getSize());put64(h.state,controller.getSize());if(component.getSize())h.state.insert(h.state.end(),component.getData(),component.getData()+component.getSize());if(controller.getSize())h.state.insert(h.state.end(),controller.getData(),controller.getData()+controller.getSize());}if(out&&capacity>=h.state.size())memcpy(out,h.state.data(),h.state.size());return h.state.size();}catch(const std::exception&e){error=e.what();return 0;}catch(...){error="State save failed";return 0;}}
int32_t astra_vst_set_state(void* handle,const uint8_t* bytes,uint64_t size){try{auto&h=host(handle);h.ui();if(!bytes||size<56||memcmp(bytes,"AVST\1\0\0\0",8)||memcmp(bytes+8,h.classId.data(),32))fail("Invalid state envelope or plugin class mismatch");auto a=get64(bytes+40),b=get64(bytes+48);if(a>size-56||b!=size-56-a)fail("Invalid state lengths");MemoryStream component((void*)(bytes+56),a),controller((void*)(bytes+56+a),b);if(h.component->setState(&component)!=kResultOk)fail("Component rejected state");component.seek(0,IBStream::kIBSeekSet,nullptr);if(h.controller){h.controller->setComponentState(&component);if(b&&h.controller->setState(&controller)!=kResultOk)fail("Controller rejected state");}refresh_state_config(h);return 0;}catch(const std::exception&e){error=e.what();return -1;}catch(...){error="State load failed";return -1;}}
uint32_t astra_vst_latency(void* handle){try{return host(handle).latency;}catch(...){return 0;}}
// Called only on the owner thread of a graph with no audio endpoint.
int32_t astra_vst_service_restart_offline_v1(void* handle,uint32_t* observed){
 try{
  auto& h=host(handle);h.ui();
  auto flags=h.handler.restart.load();
  if(observed)*observed=static_cast<uint32_t>(flags);
  if(!(flags & kStructuralRestart)){
   if(flags & kRefreshRestart)refresh_state_config(h);
   return 0;
  }
  if(flags & kReloadComponent)fail("Restored plugin requested a full component reload");
  auto stop=h.processor->setProcessing(false);
  if(stop!=kResultOk && stop!=kNotImplemented)fail("Restored plugin could not stop processing");
  h.processing=false;
  h.context->beforeDeactivate();
  if(h.component->setActive(false)!=kResultOk)fail("Restored plugin could not deactivate");
  h.active=false;
  h.handler.restart.fetch_and(~(kIoChanged|kParamIDMappingChanged));
  auto nout=h.component->getBusCount(kAudio,kOutput);
  if(nout<1||h.component->getBusCount(h.effect?kAudio:kEvent,kInput)<1)
   fail("Restored plugin state has no compatible main buses");
  if(h.effect)configure_effect_buses(h);
  BusInfo output{};
  if(h.component->getBusInfo(kAudio,kOutput,0,output)!=kResultOk||output.channelCount<1||
     output.channelCount>2)
   fail("Restored plugin output is incompatible with this route");
  for(auto dir:{kInput,kOutput}){
   for(int i=0;i<h.component->getBusCount(kAudio,dir);++i)
    h.component->activateBus(kAudio,dir,i,i==0&&(dir==kOutput||h.effect));
   for(int i=0;i<h.component->getBusCount(kEvent,dir);++i)
    h.component->activateBus(kEvent,dir,i,!h.effect&&dir==kInput&&i==0);
  }
  ProcessSetup setup{kRealtime,kSample32,(int32)h.maxBlock,h.rate};
  if(h.processor->setupProcessing(setup)!=kResultOk)fail("Restored plugin rejected processing setup");
  h.data.unprepare();
  if(!h.data.prepare(*h.component,h.maxBlock,kSample32))fail("Restored bus allocation failed");
  h.data.inputEvents=&h.input;h.data.outputEvents=&h.output;
  h.data.inputParameterChanges=&h.params;h.data.outputParameterChanges=&h.outputParams;
  h.data.processContext=&h.pc;h.data.processMode=kRealtime;
  // Discard old controller edits that referenced IDs from the pre-restore image.
  if(flags&kParamIDMappingChanged){Edit edit{};while(h.handler.pop(edit)){}}
  if(h.component->setActive(true)!=kResultOk)fail("Restored plugin could not reactivate");
  h.active=true;
  h.context->afterActivate();
  auto start=h.processor->setProcessing(true);
  if(start!=kResultOk&&start!=kNotImplemented)fail("Restored plugin could not restart");
  h.processing=true;
  refresh_state_config(h);
  if(h.handler.restart.load()&kStructuralRestart)fail("Restored plugin requested another structural restart");
  return 0;
 }catch(const std::exception& e){error=e.what();return -1;}
 catch(...){error="Restored plugin reconfiguration failed";return -1;}
}
int32_t astra_vst_set_state_offline(void* handle,const uint8_t* data,uint64_t size){
 if(astra_vst_set_state(handle,data,size)!=0)return -1;
 return astra_vst_service_restart_offline_v1(handle,nullptr);
}
int32_t astra_vst_editor_attach(void* handle,void* parent,int32_t* width,int32_t* height){
 try{
  auto& h=host(handle);h.ui();
  if(!parent||!IsWindow(static_cast<HWND>(parent)))fail("A valid parent HWND is required");
  if(!h.controller)return 1;
  if(h.view)fail("Editor already attached");
  Host::EditorChange change(h.editorChanges);
  h.view=owned(h.controller->createView(ViewType::kEditor));
  if(!h.view)return 1;
  if(h.view->isPlatformTypeSupported(kPlatformTypeHWND)!=kResultOk){h.view=nullptr;return 1;}
  try{
   if(!h.frame.install(static_cast<HWND>(parent),h.view))fail("Cannot install VST editor frame");
   h.view->setFrame(&h.frame);
   h.frame.notify_scale();
   // Some editors call resizeView during attached(); others report a new size
   // only after attaching. Never re-apply a stale pre-attach rectangle.
   h.frame.fit_reported();
   const auto initialGeneration=h.frame.generation;
   if(h.view->attached(parent,kPlatformTypeHWND)!=kResultOk)fail("Plugin editor attach failed");
   h.frame.attached=true;
   if(h.frame.generation==initialGeneration){
    if(const auto reported=h.frame.reported_size();reported.valid())h.frame.desired=reported;
   }
   if(!h.frame.fit_reported())fail("Plugin editor did not provide a usable client size");
   if(width)*width=h.frame.desired.width;
   if(height)*height=h.frame.desired.height;
   h.context->showEditor(true);
   return 0;
  }catch(...){h.detach();throw;}
 }catch(const std::exception&e){error=e.what();return -1;}
 catch(...){error="Editor attach failed";return -1;}
}
int32_t astra_vst_feedback_idle_v1(void* handle,int service){
 try{
  auto& h=host(handle);h.ui();
  const bool editor=bool(h.view);
  if(!h.context->needsUi(editor))return 0;
  if(service&&!h.editorChanges&&!h.frame.busy)h.context->serviceUi(editor);
  return 1;
 }catch(...){return -1;}
}
int32_t astra_vst_editor_resize(void* handle,int32_t w,int32_t hgt){
 try{
  auto& h=host(handle);h.ui();
  if(!h.view||w<1||hgt<1)return -1;
  Host::EditorChange change(h.editorChanges);
  if(h.view->canResize()!=kResultTrue)return 1;
  ViewRect r{0,0,w,hgt};
  if(h.view->checkSizeConstraint(&r)!=kResultOk)return -1;
  return h.frame.resizeView(h.view,&r)==kResultOk?0:-1;
 }catch(...){return -1;}
}
int32_t astra_vst_editor_remove(void* handle){try{auto&h=host(handle);h.ui();h.detach();return 0;}catch(...){return -1;}}
void astra_vst_close(void* handle){try{if(handle){host(handle).ui();release_insert(handle);}}catch(const std::exception&e){error=e.what();}}

extern "C" __declspec(dllexport) int astra_fx_reset_history(void* handle){
 try{
  auto& h=host(handle);h.ui();
  if(!h.effect||!h.maxBlock||astra_vst_rt_rearm(handle))return -1;
  const uint64_t tail=h.processor->getTailSamples();
  const uint64_t samples=tail+h.processor->getLatencySamples();
  // Do not carry an unknown / infinite tail into a new logical instance.
  // Discard that spare and use a new instance on its next selection.
  if(samples>h.rate*4)return -2;
  std::vector<float> zeros(size_t(h.maxBlock)*2),out(size_t(h.maxBlock)*2);
  AvRtContext ctx{};ctx.bpm=120;ctx.numerator=4;ctx.denominator=4;
  for(uint64_t pos=0;pos<samples;){
   auto n=static_cast<uint32_t>(std::min<uint64_t>(h.maxBlock,samples-pos));
   if(process_block<AvRtMidiEvent>(handle,out.data(),n,nullptr,0,pos,120,0,&ctx,zeros.data()))return -1;
   pos+=n;
  }
  return 0;
 }catch(...){return -1;}
}

extern "C" __declspec(dllexport) void* astra_fx_server(void* handle,const wchar_t* name){
 try{
  auto& h=host(handle);h.ui();if(!h.effect)return nullptr;
  auto s=std::make_unique<astra_remote_fx::Server>();
  if(!s->open(name,true))return nullptr;
  s->plugin=handle;
  s->render=[](void* plugin,const float* in,float* out,uint32_t n,const AvRtContext* x){
   return process_block<AvRtMidiEvent>(plugin,out,n,nullptr,0,x->sample_position,x->bpm,
                                       x->flags&AV_RT_PLAYING,x,in);
  };
  s->thread=std::thread([p=s.get()]{p->run();});return s.release();
 }catch(...){return nullptr;}
}
extern "C" __declspec(dllexport) void astra_fx_stop(void* handle){
 delete static_cast<astra_remote_fx::Server*>(handle);
}
extern "C" __declspec(dllexport) int astra_fx_control(void* handle,int acquire){
 auto& s=*static_cast<astra_remote_fx::Server*>(handle);
 if(acquire)return s.vendor.try_lock_for(std::chrono::milliseconds(1500))?1:0;
 s.vendor.unlock();return 1;
}
extern "C" __declspec(dllexport) void* astra_fx_client(void* instrument,const wchar_t* name,uint32_t latency){
 try{
  auto& h=host(instrument);h.ui();
  auto p=std::make_unique<Host>();
  p->remote=std::make_unique<astra_remote_fx::Client>();
  if(latency>h.rate*10||!p->remote->open(name,false)||!p->remote->configure(latency,h.maxBlock))return nullptr;
  p->effect=true;p->latency=latency;p->rate=h.rate;p->maxBlock=h.maxBlock;return p.release();
 }catch(...){return nullptr;}
}
extern "C" __declspec(dllexport) int astra_fx_fault(void* handle){
 try{auto& h=host(handle);h.ui();return h.remote?h.remote->fault.load():0;}catch(...){return 3;}
}
extern "C" __declspec(dllexport) int astra_fx_probe(void* handle){
 try{
  auto& h=host(handle);h.ui();if(!h.remote)return -1;
  LARGE_INTEGER now{},f{};QueryPerformanceCounter(&now);QueryPerformanceFrequency(&f);
  float silence[512]{};AvRtContext context{};context.bpm=120;
  h.remote->process(silence,std::min<uint32_t>(256,h.maxBlock),context,now.QuadPart+f.QuadPart);
  return h.remote->wet?0:-1;
 }catch(...){return -1;}
}




uint64_t astra_vst_insert_clock_ms(void){return GetTickCount64();}
static void check_insert_deadline(uint64_t deadline){
 if(deadline && astra_vst_insert_clock_ms()>=deadline)fail("Insert preparation expired");
}
void* astra_vst_stage_insert_v1(void* instrument,const char* path,const char* class_id,
 const uint8_t* state,uint64_t state_size,uint64_t deadline){try{
 auto& h=host(instrument);h.ui();
 if(h.effect||!path||!class_id||(state==nullptr)!=(state_size==0))fail("Invalid insert preparation");
 check_insert_deadline(deadline);
 auto* effect=astra_vst_open_effect(path,class_id,h.rate,h.maxBlock);
 if(!effect)return nullptr;
 std::unique_ptr<void,decltype(&astra_vst_close)> candidate(effect,&astra_vst_close);
 check_insert_deadline(deadline);
 if(state && astra_vst_set_state_offline(effect,state,state_size)!=0)return nullptr;
 check_insert_deadline(deadline);
 return candidate.release();
 }catch(const std::exception& e){error=e.what();return nullptr;}catch(...){error="Insert preparation failed";return nullptr;}}
uint64_t astra_vst_set_inserts_before_v1(void* handle,void* const* effects,const uint32_t* bypass,
 uint32_t count,uint64_t revision,uint64_t deadline){try{
 check_insert_deadline(deadline);
 auto& h=host(handle);h.ui();if(h.effect||!revision||revision<=h.inserts.submitted.load()||count>32||(count&&(!effects||!bypass)))fail("Invalid insert chain");
 auto chain=std::make_unique<InsertChain>();chain->revision=revision;chain->slots.reserve(count);chain->scratch.resize(size_t(h.maxBlock)*2);chain->release=release_insert;
 for(uint32_t i=0;i<count;++i){auto& fx=host(effects[i]);fx.ui();if(!fx.effect||fx.rate!=h.rate||fx.maxBlock<h.maxBlock||bypass[i]>1)fail("Incompatible effect configuration");for(uint32_t j=0;j<i;++j)if(effects[j]==effects[i])fail("An effect instance cannot occupy two slots");retain_insert(&fx);chain->slots.push_back({&fx,bypass[i]!=0});if(!bypass[i]){if(UINT32_MAX-chain->latency<fx.latency)fail("Insert latency overflow");chain->latency+=fx.latency;}}
 if(!h.inserts.publish_if(chain.get(),[&]{check_insert_deadline(deadline);return true;}))
  fail("Insert change pending; retry after its acknowledgement");
 chain.release();return revision;
 }catch(const std::exception& e){error=e.what();return 0;}catch(...){error="Insert publication failed";return 0;}}
uint64_t astra_vst_set_inserts(void* handle,void* const* effects,const uint32_t* bypass,uint32_t count,uint64_t revision){
 return astra_vst_set_inserts_before_v1(handle,effects,bypass,count,revision,0);
}
int32_t astra_vst_inserts_status(void* handle,AvInsertStatus* value){try{auto& h=host(handle);h.ui();if(!value||value->size<sizeof(*value))fail("Invalid insert status");h.inserts.collect();value->version=1;for(;;){auto stamp=h.inserts.stamp.load(std::memory_order_acquire);if(stamp&1)continue;value->applied_revision=h.inserts.applied.load(std::memory_order_acquire);value->latency_samples=h.inserts.latency.load();value->count=h.inserts.count.load();if(stamp==h.inserts.stamp.load(std::memory_order_acquire))break;}const auto submitted=h.inserts.submitted.load();value->pending_revision=submitted>value->applied_revision?submitted:0;return 0;}catch(...){return -1;}}
