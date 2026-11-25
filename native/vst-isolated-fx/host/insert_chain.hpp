#pragma once
#include <atomic>
#include <array>
#include <vector>
#include <cstdint>
#include <algorithm>
// One control producer and one audio consumer. Only control destroys snapshots.
struct InsertSlot {void* handle;bool bypass;};
struct InsertChain {
 std::vector<InsertSlot> slots;std::vector<float> scratch;
 uint64_t revision=0;uint32_t latency=0;
 void(*release)(void*)=nullptr;
 ~InsertChain(){if(release)for(auto s:slots)release(s.handle);}
};
struct InsertChains {
 std::atomic<InsertChain*> pending{nullptr};InsertChain* active=nullptr;
 std::array<InsertChain*,128> retired{};
 std::atomic<uint32_t> write{0},read{0};
 std::atomic<uint64_t> applied{0},submitted{0};
 std::atomic<uint32_t> latency{0},count{0},stamp{0};
 void collect(){auto r=read.load();const auto w=write.load(std::memory_order_acquire);while(r!=w){delete retired[r%128];++r;}read.store(r,std::memory_order_release);}
 void boundary(){
  const auto w=write.load(std::memory_order_relaxed);
  if(active&&w-read.load(std::memory_order_acquire)>=128)return;
  auto* next=pending.exchange(nullptr,std::memory_order_acq_rel);if(!next)return;
  if(active){retired[w%128]=active;write.store(w+1,std::memory_order_release);}
  active=next;stamp.fetch_add(1,std::memory_order_acq_rel);latency.store(next->latency);count.store(uint32_t(next->slots.size()));applied.store(next->revision,std::memory_order_release);stamp.fetch_add(1,std::memory_order_release);
 }
 // The guard executes on the sole control producer, after any slow
 // retirement and immediately before making the new chain visible.
 template<class Ready> bool publish_if(InsertChain* p, Ready&& ready){
  collect();
  if(!ready())return false;
  const auto revision=p->revision;
  InsertChain* empty=nullptr;
  if(!pending.compare_exchange_strong(empty,p,std::memory_order_release))return false;
  submitted.store(revision);return true;
 }
 bool publish(InsertChain* p){return publish_if(p,[]{return true;});}
 ~InsertChains(){delete pending.exchange(nullptr);delete active;collect();}
};
