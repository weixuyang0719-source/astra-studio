#pragma once
#include "param_changes.hpp"
#include "legacy_ui_relay.hpp"
#include "public.sdk/source/vst/hosting/hostclasses.h"
#include "pluginterfaces/vst/ivstdataexchange.h"
#include "pluginterfaces/vst/ivsteditcontroller.h"
#include <algorithm>
#include <array>
#include <atomic>
#include <chrono>
#include <cmath>
#include <cstddef>
#include <cstring>
#include <malloc.h>
#include <memory>
#include <mutex>
#include <thread>

namespace astra::vst_host {
using namespace Steinberg;
using namespace Steinberg::Vst;
static_assert(std::atomic<uint64_t>::is_always_lock_free && std::atomic<bool>::is_always_lock_free);

// The processor publishes the newest value per ID; the UI coalesces all audio
// blocks between paints. There are no controller calls, allocations, waits,
// IPC or locks added to the existing process path.
struct MeterFeedback {
 static constexpr unsigned slots=8192;
 struct Cell {
  std::atomic<uint64_t> key{0}, bits{0}, pendingEpoch{0};
 };
 std::array<Cell, slots> cells;
 std::atomic<bool> any{false};
 std::atomic<uint64_t> editorEpoch{0};

 void show(bool enabled) noexcept {
  const auto epoch=editorEpoch.load(std::memory_order_relaxed);
  if(bool(epoch&1)!=enabled)editorEpoch.store(epoch+1,std::memory_order_release);
 }
 void publish(astra::vst_params::Changes& changes) noexcept {
  const auto epoch=editorEpoch.load(std::memory_order_acquire);
  if(!(epoch&1)||!changes.count)return;
  bool wrote=false;
  for(unsigned i=0;i<changes.count;++i){
   auto& q=changes.queues[i];
   if(!q.count)continue;
   const auto value=q.point(q.count-1).value;
   if(!std::isfinite(value)||value<0.||value>1.)continue;
   const uint64_t key=uint64_t(q.id)+1;
   auto id=q.id;
   id^=id>>16;id*=0x7feb352du;id^=id>>15;
   // Bounded probing. A bad or over-capacity UI producer never stalls DSP.
   for(unsigned probe=0;probe<64;++probe){
    auto& slot=cells[(id+probe)&(slots-1)];
    auto found=slot.key.load(std::memory_order_relaxed);
    if(found&&found!=key)continue;
    if(!found)slot.key.store(key,std::memory_order_relaxed);
    uint64_t bits;std::memcpy(&bits,&value,sizeof(bits));
    slot.bits.store(bits,std::memory_order_relaxed);
    slot.pendingEpoch.store(epoch,std::memory_order_release);
    wrote=true;break;
   }
  }
  if(wrote)any.store(true,std::memory_order_release);
 }
 void drain(IEditController& controller) {
  if(!any.exchange(false,std::memory_order_acq_rel))return;
  const auto epoch=editorEpoch.load(std::memory_order_relaxed);
  for(auto& slot:cells){
   if(!(epoch&1)||!slot.pendingEpoch.load(std::memory_order_acquire)||
      slot.pendingEpoch.exchange(0,std::memory_order_acq_rel)!=epoch)continue;
   auto bits=slot.bits.load(std::memory_order_relaxed);
   double value;std::memcpy(&value,&bits,sizeof(value));
   controller.setParamNormalized(static_cast<ParamID>(slot.key.load(std::memory_order_relaxed)-1),value);
  }
 }
};

// VST3 3.7.9 data exchange: packet contents are authored by the vendor DSP,
// not synthetic host samples. All memory is reserved on the owner thread.
// The RT producer never calls the edit controller, even for main-thread queues.
class FeedbackContext final : public HostApplication, public IDataExchangeHandler {
 static constexpr uint32_t kQueues=16, kBlocks=128;
 static constexpr size_t kBytes=16*1024*1024;
 struct Indices {
  std::array<uint32_t,kBlocks> values{};
  std::atomic<uint32_t> read{0},write{0};
  uint32_t ready() const noexcept {return write.load(std::memory_order_acquire)-read.load(std::memory_order_relaxed);}
  bool push(uint32_t value) noexcept {
   const auto w=write.load(std::memory_order_relaxed);
   if(w-read.load(std::memory_order_acquire)==kBlocks)return false;
   values[w%kBlocks]=value;write.store(w+1,std::memory_order_release);return true;
  }
  bool pop(uint32_t& value) noexcept {
   const auto r=read.load(std::memory_order_relaxed);
   if(r==write.load(std::memory_order_acquire))return false;
   value=values[r%kBlocks];read.store(r+1,std::memory_order_release);return true;
  }
 };
 struct AlignedFree { void operator()(void* p) const noexcept {_aligned_free(p);} };
 struct Packets {
  IPtr<IDataExchangeReceiver> receiver;
  const DataExchangeUserContextID context;
  const uint32_t size, count, stride;
  const std::unique_ptr<void,AlignedFree> memory;
  TBool background=false;
  Indices freeToRt,pending;
  std::array<DataExchangeBlock,kBlocks> delivery{};
  std::array<uint32_t,kBlocks> originalIds{};
  std::array<uint32_t,kBlocks> returned{};
  std::array<bool,kBlocks> locked{};
  uint32_t returnedCount=0;
  Packets(IDataExchangeReceiver* r,DataExchangeUserContextID ctx,uint32_t bytes,uint32_t n,uint32_t align)
   :receiver(r),context(ctx),size(bytes),count(n),stride((bytes+align-1)/align*align),
    memory(_aligned_malloc(size_t(stride)*n,align)) {
   for(uint32_t id=0;id<n;++id)freeToRt.push(id);
  }
  DataExchangeBlock block(uint32_t id) const noexcept {
   return {static_cast<char*>(memory.get())+size_t(stride)*id,size,id};
  }
  void drain() {
   uint32_t n=0,id=0;
   auto budget=pending.ready(); // never let continuous DSP monopolize this dispatch
   while(budget--&&pending.pop(id)){
    originalIds[n]=id;delivery[n++]=block(id);
   }
   if(!n)return;
   try {receiver->onDataExchangeBlocksReceived(context,n,delivery.data(),background);}
   catch(...) {} // never lose the block pool to a vendor exception
   for(uint32_t i=0;i<n;++i)freeToRt.push(originalIds[i]);
  }
 };
 const bool supported;
 const std::thread::id ui=std::this_thread::get_id();
 std::array<std::shared_ptr<Packets>,kQueues> queues;
 std::mutex topology; // optional background consumer vs. control only
 std::thread backgroundThread;
 std::atomic<bool> stop{false};
 bool configurable=true, servicing=false;
 uint32_t uiQueues=0;
 size_t bytes=0;
 IAudioProcessor* processor=nullptr;
 IEditController* controller=nullptr;

 void backgroundLoop() noexcept {
  for(;;){
   const bool last=stop.load(std::memory_order_acquire);
   std::array<std::shared_ptr<Packets>,kQueues> snapshot;
   {
    std::lock_guard<std::mutex> guard(topology);
    snapshot=queues;
   }
   for(auto& q:snapshot)if(q&&q->background)q->drain();
   if(last)return;
   std::this_thread::sleep_for(std::chrono::milliseconds(6));
  }
 }
 void stopBackground(){
  stop.store(true,std::memory_order_release);
  if(backgroundThread.joinable())backgroundThread.join();
 }
public:
 std::unique_ptr<MeterFeedback> meters;
 std::shared_ptr<astra_legacy_ui::Relay> toController,toProcessor;
 explicit FeedbackContext(bool effect):supported(effect){
  if(supported){
   getPlugInterfaceSupport()->addPlugInterfaceSupported(IDataExchangeHandler::iid);
   // Independent caps: an analyzer frame cannot crowd out its own request.
   toController=std::make_shared<astra_legacy_ui::Relay>();
   toProcessor=std::make_shared<astra_legacy_ui::Relay>();
  }
 }
 tresult PLUGIN_API createInstance(TUID cid,TUID iid,void** out) override {
  if(supported&&FUnknownPrivate::iidEqual(cid,IMessage::iid)&&FUnknownPrivate::iidEqual(iid,IMessage::iid)){
   if(!out)return kInvalidArgument;
   try{*out=static_cast<IMessage*>(new astra_legacy_ui::HostMessage());return kResultOk;}
   catch(...){*out=nullptr;return kOutOfMemory;}
  }
  return HostApplication::createInstance(cid,iid,out);
 }
 ~FeedbackContext() override { stopBackground(); }
 void bind(IAudioProcessor* p,IEditController* c) {
  processor=p;controller=c;
  if(supported&&controller)meters=std::make_unique<MeterFeedback>();
 }
 bool needsUi(bool editor) const noexcept {return uiQueues||(editor&&meters);}
 void serviceUi(bool editor) {
  if(servicing)return; // no nested service during vendor message loops
  servicing=true;
  struct Reset {bool& flag;~Reset(){flag=false;}} reset{servicing};
  auto snapshot=queues; // topology writes are UI-thread-only
  for(auto& q:snapshot)if(q&&!q->background)q->drain();
  if(editor&&meters)meters->drain(*controller);
  // Both lanes execute on the VST owner, requests before replies.
  if(editor&&toProcessor)toProcessor->drain();
  if(editor&&toController)toController->drain();
 }
 void showEditor(bool open) noexcept {
  if(meters)meters->show(open);
  if(toController)toController->show(open);
  if(toProcessor)toProcessor->show(open);
 }
 void beforeDeactivate() {
  stopBackground();serviceUi(false);configurable=true;
 }
 void afterActivate() {
  configurable=false;
  // Until activation completes the main thread may still open/close queues.
  if(std::any_of(queues.begin(),queues.end(),[](const auto& q){return q&&q->background;})){
   stop.store(false,std::memory_order_release);
   backgroundThread=std::thread([this]{backgroundLoop();});
  }
 }
 void clearAfterTerminate() {
  stopBackground();
  for(auto& q:queues)q.reset(); // before module unload; receiver callbacks are no longer valid
  uiQueues=0;bytes=0;processor=nullptr;controller=nullptr;
 }
 tresult PLUGIN_API queryInterface(const TUID iid,void** out) override {
  if(!out)return kInvalidArgument;
  if(supported&&FUnknownPrivate::iidEqual(iid,IDataExchangeHandler::iid)){
   *out=static_cast<IDataExchangeHandler*>(this);return kResultOk;
  }
  return HostApplication::queryInterface(iid,out);
 }
 uint32 PLUGIN_API addRef() override {return HostApplication::addRef();}
 uint32 PLUGIN_API release() override {return HostApplication::release();}

 tresult PLUGIN_API openQueue(IAudioProcessor* owner,uint32_t size,uint32_t count,uint32_t alignment,
                             DataExchangeUserContextID id,DataExchangeQueueID* out) override {
  if(std::this_thread::get_id()!=ui||!configurable)return kResultFalse;
  if(!out||owner!=processor||!controller||!size||!count||count>kBlocks||size>kBytes||
     alignment>4096||(alignment&&(alignment&(alignment-1))))return kInvalidArgument;
  const auto align=std::max<uint32_t>(alignment, alignof(std::max_align_t));
  const auto cost=size_t((size+align-1)/align*align)*count;
  if(cost>kBytes-bytes)return kOutOfMemory;
  FUnknownPtr<IDataExchangeReceiver> receiver(controller);
  if(!receiver)return kNoInterface;
  for(uint32_t i=0;i<kQueues;++i)if(!queues[i]){
   try{
    auto queue=std::make_shared<Packets>(receiver,id,size,count,align);
    if(!queue->memory)return kOutOfMemory;
    receiver->queueOpened(id,size,queue->background);
    if(!queue->background)++uiQueues;
    {
     std::lock_guard<std::mutex> guard(topology);
     queues[i]=std::move(queue);
    }
    bytes+=cost;*out=i;return kResultOk;
   }catch(...){return kOutOfMemory;}
  }
  return kOutOfMemory;
 }
 tresult PLUGIN_API closeQueue(DataExchangeQueueID id) override {
  if(std::this_thread::get_id()!=ui||!configurable)return kResultFalse;
  if(id>=kQueues||!queues[id])return kInvalidArgument;
  std::shared_ptr<Packets> q;
  {
   std::lock_guard<std::mutex> guard(topology);
   q=std::move(queues[id]);
  }
  if(!q->background)--uiQueues;
  bytes-=size_t(q->stride)*q->count;
  q->receiver->queueClosed(q->context);
  return kResultOk;
 }
 tresult PLUGIN_API lockBlock(DataExchangeQueueID id,DataExchangeBlock* out) override {
  if(!out||id>=kQueues||!queues[id])return kInvalidArgument;
  // Per VST3, configuration/removal is legal only with DSP inactive. No
  // shared_ptr copies, topology lock or device calls on either RT method.
  auto* q=queues[id].get();
  uint32_t slot;
  if(q->returnedCount)slot=q->returned[--q->returnedCount];
  else if(!q->freeToRt.pop(slot))return kOutOfMemory;
  q->locked[slot]=true;*out=q->block(slot);return kResultOk;
 }
 tresult PLUGIN_API freeBlock(DataExchangeQueueID id,DataExchangeBlockID slot,TBool send) override {
  if(id>=kQueues||!queues[id])return kInvalidArgument;
  auto* q=queues[id].get();
  if(slot>=q->count||!q->locked[slot])return kInvalidArgument;
  q->locked[slot]=false;
  if(send&&q->pending.push(slot))return kResultOk;
  q->returned[q->returnedCount++]=slot;
  return send?kOutOfMemory:kResultOk;
 }
};
}
