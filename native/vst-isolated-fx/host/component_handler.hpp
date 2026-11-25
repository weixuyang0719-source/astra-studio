#pragma once
#include "param_changes.hpp"
#include "pluginterfaces/vst/ivsteditcontroller.h"
#include <atomic>
#include <array>
#include <cstdint>
namespace astra::vst_host {
using namespace Steinberg;
using namespace Steinberg::Vst;
static_assert(std::atomic<uint64_t>::is_always_lock_free);
// Component handler notifications, not a count of render automation. Plugins
// which do not notify still require an explicit state capture at save time.
struct Edit {ParamID id;double value;};
struct Handler final:IComponentHandler {
 std::array<Edit,astra::vst_params::kEditSlots> edits;std::atomic<uint32_t> read{0},write{0};std::atomic<int32> restart{0};std::atomic<uint64_t> state_generation{0};
 void changed() noexcept {auto n=state_generation.load(std::memory_order_relaxed);while(n!=UINT64_MAX && !state_generation.compare_exchange_weak(n,n+1,std::memory_order_release,std::memory_order_relaxed)) {}}
 tresult PLUGIN_API beginEdit(ParamID)override{return kResultOk;}
 tresult PLUGIN_API endEdit(ParamID)override{return kResultOk;}
 tresult PLUGIN_API performEdit(ParamID id,ParamValue value)override{changed();auto w=write.load();auto next=(w+1)%edits.size();if(next==read.load(std::memory_order_acquire))return kOutOfMemory;edits[w]={id,value};write.store((uint32_t)next,std::memory_order_release);return kResultOk;}
 tresult PLUGIN_API restartComponent(int32 flags)override{
  // Changing parameter titles/metadata is not a change to the saved preset.
  // E.g. Pianoteq emits this alone for playback of sustain (MIDI CC64).
  // Keep other notifications, including a combined value change, dirty.
  if(flags&~kParamTitlesChanged)changed();
  restart.fetch_or(flags);return kResultOk;
 }
 bool pop(Edit& e){auto r=read.load();if(r==write.load(std::memory_order_acquire))return false;e=edits[r];read.store((r+1)%edits.size(),std::memory_order_release);return true;}
 tresult PLUGIN_API queryInterface(const TUID iid,void** p)override{if(FUnknownPrivate::iidEqual(iid,IComponentHandler::iid)||FUnknownPrivate::iidEqual(iid,FUnknown::iid)){*p=this;return kResultOk;}*p=nullptr;return kNoInterface;} uint32 PLUGIN_API addRef() override{return 1000;} uint32 PLUGIN_API release() override{return 1000;}
};
}
