#pragma once
// Compatibility for background legacy messages in an isolated effect editor.
// Standard VST3 requires notify on the owner/UI thread. Do not relay from process().
#include "connectionproxy.h"
#include "pluginterfaces/vst/ivstattributes.h"
#include <algorithm>
#include <array>
#include <atomic>
#include <chrono>
#include <cstring>
#include <map>
#include <memory>
#include <mutex>
#include <optional>
#include <string>
#include <type_traits>
#include <utility>
#include <variant>
#include <vector>

namespace astra_legacy_ui {
using namespace Steinberg;
using namespace Steinberg::Vst;
inline thread_local bool inProcess=false;
struct ProcessScope {
 const bool was=inProcess;
 ProcessScope() noexcept {inProcess=true;}
 ~ProcessScope(){inProcess=was;}
};

struct ISnapshot : FUnknown {
 inline static const FUID iid {0xC2BDD088,0xA9EF421C,0xA9DADC55,0xB6B241B3};
 virtual IMessage* PLUGIN_API snapshot() noexcept=0;
};

// Implement the standard host-allocated IMessage interface. Only this
// implementation exposes ISnapshot; arbitrary vendor objects are not retained.
// Copies share an immutable payload until either side edits its own copy.
struct HostMessage final:IMessage,IAttributeList,ISnapshot {
 using String=std::basic_string<TChar>;
 using Binary=std::vector<uint8_t>;
 using Value=std::variant<int64,double,String,Binary>;
 struct Payload {std::optional<std::string> id;std::map<std::string,Value,std::less<>> attr;};
 std::shared_ptr<Payload> body=std::make_shared<Payload>();
 std::atomic<uint32> refs{1};
 explicit HostMessage()=default;
 explicit HostMessage(const std::shared_ptr<Payload>& payload):body(payload){}
 Payload& makeWritable(){
  if(!body.unique())body=std::make_shared<Payload>(*body);
  return *body;
 }
 tresult PLUGIN_API queryInterface(const TUID iid,void** out) override {
  if(!out)return kInvalidArgument;
  if(FUnknownPrivate::iidEqual(iid,IMessage::iid)||FUnknownPrivate::iidEqual(iid,FUnknown::iid))
   *out=static_cast<IMessage*>(this);
  else if(FUnknownPrivate::iidEqual(iid,IAttributeList::iid))
   *out=static_cast<IAttributeList*>(this);
  else if(FUnknownPrivate::iidEqual(iid,ISnapshot::iid))
   *out=static_cast<ISnapshot*>(this);
  else{*out=nullptr;return kNoInterface;}
  addRef();return kResultOk;
 }
 uint32 PLUGIN_API addRef() override{return refs.fetch_add(1,std::memory_order_relaxed)+1;}
 uint32 PLUGIN_API release() override{
  auto n=refs.fetch_sub(1,std::memory_order_acq_rel)-1;
  if(!n)delete this;return n;
 }
 IMessage* PLUGIN_API snapshot() noexcept override {
  // Bound only async traffic; standard on-owner messages can be any size.
  constexpr size_t cap=512*1024;
  const auto idSize=body->id?body->id->size():0;
  if(body->attr.size()>128||idSize>1024)return nullptr;
  size_t remaining=cap-idSize;
  for(const auto& [key,value]:body->attr){
   if(key.size()>remaining)return nullptr;
   remaining-=key.size();
   size_t bytes=std::visit([](const auto& item)->size_t{
    using T=std::decay_t<decltype(item)>;
    if constexpr(std::is_same_v<T,String>)return item.size()*sizeof(TChar);
    else if constexpr(std::is_same_v<T,Binary>)return item.size();
    else return sizeof(T);
   },value);
   if(bytes>remaining)return nullptr;
   remaining-=bytes;
  }
  try{return static_cast<IMessage*>(new HostMessage(body));}
  catch(...){return nullptr;}
 }
 FIDString PLUGIN_API getMessageID() override{return body->id?body->id->c_str():nullptr;}
 void PLUGIN_API setMessageID(FIDString id) override{
  try{
   if(id)makeWritable().id=std::string(id);
   else makeWritable().id.reset();
  }catch(...){}
 }
 IAttributeList* PLUGIN_API getAttributes() override{return this;}
 template<class T> const T* value(AttrID id) const {
  if(!id)return nullptr;
  auto found=body->attr.find(id);
  return found==body->attr.end()?nullptr:std::get_if<T>(&found->second);
 }
 template<class T> tresult set(AttrID id,T&& v) {
  if(!id)return kInvalidArgument;
  try{makeWritable().attr.insert_or_assign(id,std::forward<T>(v));return kResultOk;}
  catch(...){return kOutOfMemory;}
 }
 tresult PLUGIN_API setInt(AttrID id,int64 v) override{return set(id,v);}
 tresult PLUGIN_API getInt(AttrID id,int64& out) override{
  if(!id)return kInvalidArgument;
  if(const auto* x=value<int64>(id)){out=*x;return kResultOk;}return kResultFalse;
 }
 tresult PLUGIN_API setFloat(AttrID id,double v) override{return set(id,v);}
 tresult PLUGIN_API getFloat(AttrID id,double& out) override{
  if(!id)return kInvalidArgument;
  if(const auto* x=value<double>(id)){out=*x;return kResultOk;}return kResultFalse;
 }
 tresult PLUGIN_API setString(AttrID id,const TChar* text) override{
  if(!text)return kInvalidArgument;
  try{return set(id,String(text));}catch(...){return kOutOfMemory;}
 }
 tresult PLUGIN_API getString(AttrID id,TChar* out,uint32 bytes) override{
  if(!id||(!out&&bytes))return kInvalidArgument;
  const auto* x=value<String>(id);if(!x)return kResultFalse;
  if(bytes)std::memcpy(out,x->c_str(),std::min<size_t>(bytes,(x->size()+1)*sizeof(TChar)));
  return kResultOk;
 }
 tresult PLUGIN_API setBinary(AttrID id,const void* data,uint32 bytes) override{
  if(!id||(!data&&bytes))return kInvalidArgument;
  try{
   Binary b;
   if(bytes){auto* p=static_cast<const uint8_t*>(data);b.assign(p,p+bytes);}
   return set(id,std::move(b));
  }catch(...){return kOutOfMemory;}
 }
 tresult PLUGIN_API getBinary(AttrID id,const void*& data,uint32& bytes) override{
  if(!id)return kInvalidArgument;
  if(const auto* x=value<Binary>(id)){data=x->data();bytes=static_cast<uint32>(x->size());return kResultOk;}
  return kResultFalse;
 }
};

class Relay {
 using Clock=std::chrono::steady_clock;
 struct Item {IPtr<IMessage> message;uint64_t epoch=0;Clock::time_point expires{};};
 static constexpr size_t capacity=64;
 std::mutex queueMutex; // never reached from process(); best effort on other threads
 std::array<Item,capacity> queue{};
 size_t first=0,count=0;
 std::atomic<uint64_t> viewEpoch{0};
 IConnectionPoint* dest=nullptr; // accessed only by owner, lifetime kept by ConnectionProxy
 bool draining=false;

 void take(std::array<Item,16>& batch) noexcept {
  std::unique_lock<std::mutex> lock(queueMutex,std::try_to_lock);
  if(!lock)return;
  for(auto& item:batch){
   if(!count)break;
   auto& current=queue[first];item=std::move(current);
   first=(first+1)%capacity;--count;
  }
 }
public:
 void connect(IConnectionPoint* to) noexcept {dest=to;}
 void show(bool open) noexcept {
  const auto epoch=viewEpoch.load(std::memory_order_relaxed);
  if(bool(epoch&1)!=open)viewEpoch.store(epoch+1,std::memory_order_release);
  if(!open){
   // Drop as much as is already enqueued, but never block closing. A racing
   // producer rechecks the epoch inside its brief try-lock; old epochs are
   // rejected again on any later drain.
   for(unsigned i=0;i<capacity/16;++i){std::array<Item,16> drop;take(drop);}
  }
 }
 void disconnect() noexcept {show(false);dest=nullptr;}
 tresult post(IMessage* message) noexcept {
  if(inProcess)return kResultFalse;
  const auto epoch=viewEpoch.load(std::memory_order_acquire);
  if(!(epoch&1)||!message)return kResultFalse;
  try{
   // The VST interface has no way to enumerate attributes from an arbitrary
   // IMessage; only our own standard host-allocated messages can be frozen.
   FUnknownPtr<ISnapshot> source(message);
   auto snap=source?owned(source->snapshot()):IPtr<IMessage>{};
   if(!snap)return kResultFalse;
   std::unique_lock<std::mutex> lock(queueMutex,std::try_to_lock);
   if(!lock||count==capacity||viewEpoch.load(std::memory_order_acquire)!=epoch)return kResultFalse;
   queue[(first+count)%capacity]={std::move(snap),epoch,Clock::now()+std::chrono::milliseconds(250)};
   ++count;return kResultOk; // accepted for later delivery
  }catch(...){return kResultFalse;}
 }
 void drain() noexcept {
  if(draining||!dest)return;
  draining=true;
  struct Reset {bool& value;~Reset(){value=false;}} reset{draining};
  std::array<Item,16> batch;
  take(batch);
  for(auto& item:batch){
   if(!item.message)break;
   if(viewEpoch.load(std::memory_order_relaxed)!=item.epoch||Clock::now()>item.expires||!dest)continue;
   try{
    // Vendor callbacks can re-enter the editor loop and disconnect.
    const IPtr<IConnectionPoint> target(dest);
    target->notify(item.message);
   }catch(...){}
  }
 }
};
inline thread_local const std::shared_ptr<Relay>* activeFactoryRelay=nullptr;
inline thread_local const std::shared_ptr<Relay>* activeReverseRelay=nullptr;
struct FactoryScope {
 const std::shared_ptr<Relay>* prior=activeFactoryRelay;
 const std::shared_ptr<Relay>* priorReverse=activeReverseRelay;
 FactoryScope(const std::shared_ptr<Relay>& active,const std::shared_ptr<Relay>& reverse) noexcept {
  activeFactoryRelay=&active;activeReverseRelay=&reverse;
 }
 ~FactoryScope(){activeFactoryRelay=prior;activeReverseRelay=priorReverse;}
};

class UiConnection final : public ConnectionProxy {
 std::shared_ptr<Relay> relay;
public:
 UiConnection(IConnectionPoint* source,std::shared_ptr<Relay> r):ConnectionProxy(source),relay(std::move(r)){}
 tresult PLUGIN_API connect(IConnectionPoint* to) override {
  const auto rc=ConnectionProxy::connect(to);
  if(rc==kResultOk)relay->connect(to);
  return rc;
 }
 tresult PLUGIN_API disconnect(IConnectionPoint* to) override {
  if(to&&to==dstConnection)relay->disconnect();
  return ConnectionProxy::disconnect(to);
 }
 tresult PLUGIN_API notify(IMessage* message) override {
  if(inProcess)return kResultFalse;
  if(threadChecker&&threadChecker->test())return ConnectionProxy::notify(message);
  return relay->post(message);
 }
};
inline ConnectionProxy* makeProcessorToControllerProxy(IConnectionPoint* source) {
 if(activeFactoryRelay&&*activeFactoryRelay)return new UiConnection(source,*activeFactoryRelay);
 return new ConnectionProxy(source);
}
inline ConnectionProxy* makeControllerToProcessorProxy(IConnectionPoint* source) {
 if(activeReverseRelay&&*activeReverseRelay)return new UiConnection(source,*activeReverseRelay);
 return new ConnectionProxy(source);
}
}
