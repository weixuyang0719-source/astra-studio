#include "midi_scheduler.h"
#define NOMINMAX
#include <windows.h>
#include <atomic>
#include <algorithm>
#include <array>
#include <vector>
#include <deque>
#include <thread>
#include <mutex>
#include <condition_variable>
#include <chrono>
#include <cmath>
#include <stdexcept>
#include <memory>
using Clock=std::chrono::steady_clock;
struct NativeWait {
 HANDLE event=nullptr,timer=nullptr;int kind=0;
 NativeWait(){event=CreateEventW(nullptr,FALSE,FALSE,nullptr);timer=CreateWaitableTimerExW(nullptr,nullptr,2,TIMER_ALL_ACCESS);if(timer)kind=2;else{timer=CreateWaitableTimerW(nullptr,FALSE,nullptr);kind=1;}if(!event||!timer){if(event)CloseHandle(event);if(timer)CloseHandle(timer);throw std::runtime_error("native timer initialization");}}
 ~NativeWait(){if(timer)CloseHandle(timer);if(event)CloseHandle(event);}
 void wake() noexcept {SetEvent(event);}
 bool wait(){LARGE_INTEGER due;due.QuadPart=-10000;if(!SetWaitableTimer(timer,&due,0,nullptr,nullptr,FALSE))return false;HANDLE h[]={event,timer};auto r=WaitForMultipleObjects(2,h,FALSE,INFINITE);return r==WAIT_OBJECT_0||r==WAIT_OBJECT_0+1;}
};
constexpr uint32_t ClickGate=9,ReplaceClicks=10,ApplyMemory=11;
using ClickTrack=std::vector<AstraMidiClick>;
struct MemoryCommand {
 AstraMidiMemoryPlan plan{};
 std::vector<AstraMidiEvent> events;
 ClickTrack clicks;
 std::array<bool,16> channels{};
};
struct Receipt {std::atomic<uint64_t> value{0};};
struct Cmd{uint64_t seq;uint32_t kind;double a,b;const ClickTrack* clicks=nullptr;MemoryCommand* memory=nullptr;};
struct Scheduler {
 std::vector<AstraMidiEvent> events;std::vector<AstraMidiNote> notes;double duration;
 AstraMidiClockFn clock;void* clock_user;AstraMidiSendFn send;void* send_user;AstraMidiLifetimeFn clock_release,send_release;
 const bool memory_mode;
 static_assert(std::atomic<uint64_t>::is_always_lock_free,"memory MIDI dispatch fence must be lock-free");
 std::atomic<uint64_t> memory_floor{0},memory_done{0};
 std::unique_ptr<MemoryCommand> memory_slot;
 uint64_t memory_queued=0,memory_gate=0,memory_score=0,memory_locator=0;
 bool memory_have_score=false,memory_have_locator=false,memory_end=false,guard_sends=false;
 ClickTrack memory_clicks;
 NativeWait timer;std::thread worker;std::mutex mu;std::condition_variable cv;std::deque<Cmd> queue;
 std::atomic<uint64_t> master_version{0},observed_epoch{0};std::atomic<double> observed_seconds{0};std::atomic<uint32_t> observed_playing{0};
 Receipt receipts[512];std::atomic<uint64_t> superseded{0};
 void receipt(uint64_t seq,int status){receipts[seq%512].value.store((seq<<3)|uint64_t(status),std::memory_order_release);}
 // Caller holds mu. The worker cannot take the command until its payload is owned.
 int64_t enqueue(uint32_t kind,double a,double b,const ClickTrack* clicks=nullptr,MemoryCommand* memory=nullptr){
  if(queue.size()>=256)return -2;
  const auto seq=accepted.load()+1;if(seq>(UINT64_MAX>>3))return -4;
  try{queue.push_back({seq,kind,a,b,clicks,memory});}catch(...){return -4;}
  accepted.store(seq);timer.wake();return int64_t(seq);
 }
 // The submitter owns both slots. Only one replacement may be in flight; once
 // installed, the preceding slot is safe to reuse. Worker never frees a table.
 std::array<std::unique_ptr<const ClickTrack>,2> click_slots;
 size_t click_slot=0;uint64_t click_queued=0;
 std::atomic<uint64_t> click_installed{0};
 const ClickTrack* click_track=nullptr;
 size_t click_index=0;std::atomic<int> click_channel{-1};int click_note=-1;
 bool click_lane_ready=false,click_realign=false;double click_end=0;
 std::atomic<uint32_t> click_enabled{0},click_beats{0},click_gain_cc7{100};
 std::atomic<uint64_t> click_note_ons{0},click_note_offs{0},click_late_skipped{0};
 std::atomic<bool> closing{false},closed{false};std::atomic<uint64_t> accepted{0},applied{0},sent{0};std::atomic<double> position{0};std::atomic<bool> playing{false};std::atomic<int> error{0};
 std::array<bool,16> channels{};std::array<std::array<uint32_t,128>,16> active{};std::array<std::array<int,128>,16> controls{},poly{};std::array<int,16> pitch{},pressure{};std::array<int,16> volumes{};
 double at=0,rate=1,volume=1,left=0,right=0;bool run=false,loop=false;size_t index=0;Clock::time_point anchor=Clock::now();uint64_t master_epoch=0;bool have_master=false;
 Scheduler(const AstraMidiEvent*e,uint64_t ne,const AstraMidiNote*n,uint64_t nn,double d,AstraMidiClockFn c,void*cu,AstraMidiSendFn s,void*su,AstraMidiLifetimeFn cr=nullptr,AstraMidiLifetimeFn cf=nullptr,AstraMidiLifetimeFn sr=nullptr,AstraMidiLifetimeFn sf=nullptr,bool live=false):duration(d),clock(c),clock_user(cu),send(s),send_user(su),clock_release(cf),send_release(sf),memory_mode(live){
 if(ne)events.assign(e,e+ne);if(nn)notes.assign(n,n+nn);
 for(auto e:events)channels[e.packed&15]=true;for(auto n:notes)channels[n.channel]=true;
 // MIDI 1.0 has no independent note-off for two sources on the same key.
 choose_click_channel();
 volumes.fill(100);for(auto&a:controls)a.fill(-1);for(auto&a:poly)a.fill(-1);pitch.fill(-1);pressure.fill(-1);
 if(cr)cr(cu);if(sr)sr(su);try{worker=std::thread([this]{work();});}catch(...){if(cf)cf(cu);if(sf)sf(su);throw;}}
 static uint32_t msg(int status,int ch,int a,int b=0){return uint32_t(status|ch)|(uint32_t(a)<<8)|(uint32_t(b)<<16);}
 static int ccDefault(int c){if(c>=33&&c<=63)return 0;if(c>=70&&c<=79)return 64;switch(c){case 8:case 10:return 64;case 11:return 127;case 91:return 40;case 98:case 99:case 100:case 101:return 127;case 0:case 1:case 2:case 4:case 5:case 12:case 13:case 16:case 17:case 18:case 19:case 32:case 64:case 65:case 66:case 67:case 68:case 69:case 80:case 81:case 82:case 83:case 84:case 92:case 93:case 94:case 95:return 0;default:return -1;}}
 bool transmit(uint32_t m){check_memory_fence();int err=send(send_user,m);if(err){error.store(err);return false;}sent.fetch_add(1,std::memory_order_relaxed);return true;}
 struct SendFailure {};
 static void check_send(bool ok){if(!ok)throw SendFailure{};}
 bool try_raw(uint32_t m){
  if(!transmit(m))return false;
  int type=m&240,ch=m&15,a=(m>>8)&127,b=(m>>16)&127;
  if(type==0x90&&b)++active[ch][a];else if(type==0x80||type==0x90){if(active[ch][a])--active[ch][a];}
  else if(type==0xb0){if(a==120||a==123)active[ch].fill(0);else if(a==121){for(int c=0;c<128;c++)if(ccDefault(c)>=0&&c!=0&&c!=7&&c!=10&&c!=32&&c!=91&&c!=93)controls[ch][c]=-1;pitch[ch]=pressure[ch]=-1;poly[ch].fill(-1);}else controls[ch][a]=b;}
  else if(type==0xe0)pitch[ch]=a+(b<<7);else if(type==0xd0)pressure[ch]=a;else if(type==0xa0)poly[ch][a]=b;
  return true;
 }
 void raw(uint32_t m){check_send(try_raw(m));}
 bool try_emit(uint32_t m){int type=m&0xf0,ch=m&15,a=(m>>8)&127,b=(m>>16)&127;
  if(type==0xb0&&a==7){volumes[ch]=b;return try_raw(msg(0xb0,ch,7,int(std::lround(b*volume))));}
  if(!try_raw(m))return false;
  return type!=0xb0||a!=121||try_raw(msg(0xb0,ch,7,int(std::lround(volumes[ch]*volume))));
 }
 void emit(uint32_t m){check_send(try_emit(m));}
 bool stop_click(){
  if(click_note<0)return true;
  if(!transmit(msg(0x80,click_channel,click_note)))return false;
  click_note=-1;click_note_offs.fetch_add(1,std::memory_order_relaxed);return true;
 }
 bool init_click_lane(){
  if(click_lane_ready)return true;
  const int ch=click_channel;
  if(!transmit(msg(0xb0,ch,121,0))||!transmit(msg(0xb0,ch,0,0))||
     !transmit(msg(0xb0,ch,32,0))||!transmit(msg(0xc0,ch,ch==9?0:115)))return false;
  const int cc[][2]={{64,0},{66,0},{69,0},{10,64},{11,127},{7,int(click_gain_cc7.load(std::memory_order_relaxed))}};
  for(const auto& v:cc)if(!transmit(msg(0xb0,ch,v[0],v[1])))return false;
  click_lane_ready=true;return true;
 }
 void gate_clicks(bool on){click_realign=on;click_enabled.store(on,std::memory_order_relaxed);}
 void land_clicks(double p){
  click_realign=false;click_index=0;
  if(click_track&&click_channel>=0&&click_enabled.load(std::memory_order_relaxed))
   click_index=std::lower_bound(click_track->begin(),click_track->end(),p,[](const AstraMidiClick& e,double t){return e.seconds<t;})-click_track->begin();
 }
 size_t after_click(double p,size_t first=0) const{
  return std::upper_bound(click_track->begin()+first,click_track->end(),p,[](double t,const AstraMidiClick& e){return t<e.seconds;})-click_track->begin();
 }
 // Same sampled score position as performance. No heap work, locks or history
 // replay here. In a late wake use at most the newest unexpired onset.
 bool dispatch_click(double p){
  if(click_note>=0&&p>=click_end&&!stop_click())return false;
  if(!click_track||click_channel<0||!click_enabled.load(std::memory_order_relaxed))return true;
  // A live arm starts strictly after the sampled position, even if a master
  // keeps reporting the same beat. A transport landing includes its exact beat.
  if(click_realign){click_index=after_click(p);click_realign=false;}
  if(click_index==click_track->size()||(*click_track)[click_index].seconds>p)return true;
  const auto next=after_click(p,click_index);
  const auto& beat=(*click_track)[next-1];
  const auto end=std::min(duration,beat.seconds+.045);
  const bool timely=p<end;
  click_late_skipped.fetch_add(next-click_index-(timely?1:0),std::memory_order_relaxed);
  click_index=next;
  if(!timely)return true;
  if(!stop_click()||!init_click_lane())return false;
  const int note=beat.accent?76:77;
  if(!transmit(msg(0x90,click_channel,note,beat.accent?95:60)))return false;
  click_note=note;click_end=end;click_note_ons.fetch_add(1,std::memory_order_relaxed);return true;
 }
 void resolve(){SendGuard release_messages(*this,false);check_send(stop_click());for(int ch=0;ch<16;ch++){for(int n=0;n<128;n++){auto count=active[ch][n];for(uint32_t i=0;i<count;i++)raw(msg(0x80,ch,n,64));}if(channels[ch]){for(int pedal:{64,66,69})if(pedal==64||controls[ch][pedal]>=0)raw(msg(0xb0,ch,pedal,0));raw(msg(0xb0,ch,123,0));}}}
 void resetStrays(size_t targetIndex){std::array<std::array<bool,128>,16> target{};std::array<bool,16> targetPitch{},targetPressure{};
  for(int ch=0;ch<16;ch++)if(channels[ch]){for(int cc:{64,66,0,32,10,11,91,93})target[ch][cc]=true;targetPitch[ch]=true;}
  for(size_t i=0;i<targetIndex;i++){auto m=events[i].packed;int type=m&240,ch=m&15,a=(m>>8)&127;if(type==0xb0)target[ch][a]=true;else if(type==0xe0)targetPitch[ch]=true;else if(type==0xd0)targetPressure[ch]=true;}
  for(int ch=0;ch<16;ch++)for(int cc=0;cc<128;cc++){int def=ccDefault(cc);if(controls[ch][cc]>=0&&!target[ch][cc]&&cc!=7&&def>=0&&controls[ch][cc]!=def)raw(msg(0xb0,ch,cc,def));}
  for(int ch=0;ch<16;ch++)if(pitch[ch]>=0&&pitch[ch]!=8192&&!targetPitch[ch])raw(msg(0xe0,ch,0,64));
  for(int ch=0;ch<16;ch++)if(pressure[ch]>0&&!targetPressure[ch])raw(msg(0xd0,ch,0));
  for(int ch=0;ch<16;ch++)for(int n=0;n<128;n++)if(poly[ch][n]>0)raw(msg(0xa0,ch,n,0));
 }
 // Reconstruct live MIDI voices from raw events, not CC64-only note spans.
 // FIFO note-off semantics match the parser; device envelopes/phases are not recoverable.
 struct Voice {int velocity;bool key=true,captured=false;};
 using Voices=std::array<std::array<std::vector<Voice>,128>,16>;
 void chaseVoices(size_t limit){
  Voices voices;std::array<int,16> damper{},sostenuto{};
  auto collect=[&](int ch){for(auto&v:voices[ch])v.erase(std::remove_if(v.begin(),v.end(),[&](const Voice&n){return !n.key&&damper[ch]<64&&!n.captured;}),v.end());};
  for(size_t i=0;i<limit;i++){
   if(!(i&255))check_memory_fence();
   auto m=events[i].packed;int type=m&240,ch=m&15,a=(m>>8)&127,b=(m>>16)&127;
   if(type==0x90&&b)voices[ch][a].push_back({b,true,false});
   else if(type==0x80||type==0x90){auto&v=voices[ch][a];auto n=std::find_if(v.begin(),v.end(),[](const Voice&n){return n.key;});if(n!=v.end())n->key=false;collect(ch);}
   else if(type==0xb0){
    if(a==64){damper[ch]=b;collect(ch);}
    else if(a==66){bool rise=sostenuto[ch]<64&&b>=64;sostenuto[ch]=b;for(auto&v:voices[ch])for(auto&n:v){if(b<64)n.captured=false;else if(rise&&n.key)n.captured=true;}collect(ch);}
    else if(a==120){for(auto&v:voices[ch])v.clear();}
    else if(a==123){for(auto&v:voices[ch])for(auto&n:v)n.key=false;collect(ch);}
    else if(a==121){damper[ch]=sostenuto[ch]=0;for(auto&v:voices[ch])for(auto&n:v)n.captured=false;collect(ch);}
   }
  }
  for(int ch=0;ch<16;ch++)if(channels[ch]){
   // Target damper first. Non-captured released voices must be recreated before
   // any captured keyed voices, otherwise same-pitch FIFO note-off hits the latter.
   if(controls[ch][64]!=damper[ch])emit(msg(0xb0,ch,64,damper[ch]));
   for(int p=0;p<128;p++)for(auto n:voices[ch][p])if(!n.captured&&!n.key){emit(msg(0x90,ch,p,n.velocity));emit(msg(0x80,ch,p,64));}
   for(bool keyed:{false,true})for(int p=0;p<128;p++)for(auto n:voices[ch][p])if(n.captured&&n.key==keyed)emit(msg(0x90,ch,p,n.velocity));
   // Captures only reconstructed captured voices; late arrivals are sent below.
   if(sostenuto[ch]!=0)emit(msg(0xb0,ch,66,sostenuto[ch]));
   for(int p=0;p<128;p++)for(auto n:voices[ch][p])if(n.captured&&!n.key)emit(msg(0x80,ch,p,64));
   for(int p=0;p<128;p++)for(auto n:voices[ch][p])if(!n.captured&&n.key)emit(msg(0x90,ch,p,n.velocity));
  }
 }
 void chase(double p){
 SendGuard permission(*this,true);check_memory_fence();
 index=std::lower_bound(events.begin(),events.end(),p,[](const auto&e,double t){return e.seconds<t;})-events.begin();resetStrays(index);
 volumes.fill(100);const int cc[][2]={{64,0},{66,0},{0,0},{32,0},{10,64},{11,127},{91,40},{93,0}};
 for(int ch=0;ch<16;ch++)if(channels[ch]){for(auto&v:cc)raw(msg(0xb0,ch,v[0],v[1]));raw(msg(0xc0,ch,0));raw(msg(0xe0,ch,0,64));raw(msg(0xb0,ch,7,int(std::lround(100*volume))));}
 index=std::lower_bound(events.begin(),events.end(),p,[](const auto&e,double t){return e.seconds<t;})-events.begin();
 // Keep bank/program/RPN/NRPN/reset order; collapse ordinary controls to last value.
 std::array<std::array<int64_t,128>,16> ccLast{},polyLast{};std::array<int64_t,16> bend{},pressure{};
 for(auto&a:ccLast)a.fill(-1);for(auto&a:polyLast)a.fill(-1);bend.fill(-1);pressure.fill(-1);std::vector<size_t> restore;
 for(size_t i=0;i<index;i++){auto m=events[i].packed;int type=m&0xf0,ch=m&15,a=(m>>8)&127;
  if(type==0x80||type==0x90||(type==0xb0&&a==66))continue;
  bool ordered=type==0xc0||(type==0xb0&&(a==0||a==6||a==32||a==38||a==96||a==97||a==98||a==99||a==100||a==101||a>=120));
  if(ordered)restore.push_back(i);else if(type==0xb0)ccLast[ch][a]=i;else if(type==0xe0)bend[ch]=i;else if(type==0xd0)pressure[ch]=i;else if(type==0xa0)polyLast[ch][a]=i;
 }
 for(int ch=0;ch<16;ch++){for(auto i:ccLast[ch])if(i>=0)restore.push_back(size_t(i));for(auto i:polyLast[ch])if(i>=0)restore.push_back(size_t(i));if(bend[ch]>=0)restore.push_back(size_t(bend[ch]));if(pressure[ch]>=0)restore.push_back(size_t(pressure[ch]));}
 std::sort(restore.begin(),restore.end());for(auto i:restore)emit(events[i].packed);
 chaseVoices(index);
 land_clicks(p); // reset the onset cursor, never chase a click voice
 }
 double nowpos(){return run?std::min(duration,at+std::chrono::duration<double>(Clock::now()-anchor).count()*rate):at;}
 double land(double p){p=std::clamp(p,0.0,duration);if(loop&&right<left&&p>=right&&p<left)return left;return p;}
 void locate(double p,bool resume){if(run)resolve();run=false;at=land(p);if(resume&&at<duration){chase(at);run=true;anchor=Clock::now();}}
#include "memory_midi_scheduler.inl"
 void apply(Cmd c){if(c.kind==ApplyMemory){apply_memory(c);return;}switch(c.kind){case 1:if(!run){at=land(at>=duration?0:at);chase(at);run=true;anchor=Clock::now();}break;
 case 2:locate(nowpos(),false);break;case 3:locate(c.a,run);break;case 4:locate(0,false);break;
 case 5:at=nowpos();anchor=Clock::now();rate=c.a;break;
 case 6:left=c.a;right=c.b;loop=left!=right;locate(nowpos(),run);break;case 8:loop=false;break;
 case 7:{
  volume=c.a;for(int ch=0;ch<16;ch++)if(channels[ch])raw(msg(0xb0,ch,7,int(std::lround(volumes[ch]*volume))));
  const int gain=int(std::lround(100*volume));click_gain_cc7.store(gain,std::memory_order_relaxed);
  if(click_lane_ready)check_send(transmit(msg(0xb0,click_channel,7,gain)));
  break;
 }
 case ClickGate:
  if(bool(click_enabled.load(std::memory_order_relaxed))!=(c.a!=0)){
   if(c.a==0)check_send(stop_click());
   gate_clicks(c.a!=0);
  }
  break;
 case ReplaceClicks:
  check_send(stop_click());click_track=c.clicks;
  click_beats.store(click_track?uint32_t(click_track->size()):0,std::memory_order_relaxed);
  gate_clicks(c.a!=0);
  click_installed.store(c.seq,std::memory_order_release);
  break;
 }
 receipt(c.seq,1);applied.store(c.seq,std::memory_order_release);}
 void work() noexcept {try{std::deque<Cmd> tasks;while(!closing.load()){
  tasks.clear();{std::lock_guard<std::mutex> l(mu);tasks.swap(queue);}for(size_t j=0;j<tasks.size();++j){
   if(closing){for(;j<tasks.size();++j)receipt(tasks[j].seq,3);break;}
   // Only adjacent pure seek commands may supersede: never cross play/pause/rate/loop/gain.
   if(tasks[j].kind==3)while(j+1<tasks.size()&&tasks[j+1].kind==3){receipt(tasks[j].seq,2);superseded.fetch_add(1);++j;}
   try{apply(tasks[j]);}catch(...){receipt(tasks[j].seq,4);for(++j;j<tasks.size();++j)receipt(tasks[j].seq,3);throw;}
  }
  if(closing.load())break;
  try{
  if(memory_mode&&run&&memory_revoked())hold_memory();
  double p=nowpos();if(clock){AstraMidiClock c{sizeof(AstraMidiClock),1,0,0,0,0};if(clock(clock_user,&c)||!std::isfinite(c.seconds)||c.seconds<0||c.size<sizeof(c)||c.version!=1){error=-100;break;}
   p=std::clamp(c.seconds,0.0,duration);bool changed=have_master&&(c.epoch!=master_epoch||p<at-1e-8);bool want=c.playing!=0&&(p<duration||run);
   if(run&&(!want||changed)){resolve();run=false;}if(want&&!run){chase(p);run=true;}at=p;have_master=true;master_epoch=c.epoch;
  }else if(run&&loop&&((left<right&&p>=right)||(right<left&&p>=right&&p<left))){locate(left,true);p=at;}
  if(run){
   SendGuard permission(*this,true);
   if(!dispatch_click(p))break;
   const size_t end=std::min(events.size(),index+256);
   for(;index<end&&events[index].seconds<=p;++index)if(!try_emit(events[index].packed))break;
   if(error.load())break;
   if(p>=duration&&index==events.size()){resolve();run=false;at=duration;if(memory_mode)memory_end=true;}
  }
  if(clock&&(!run||index==events.size()||events[index].seconds>p)){
   master_version.fetch_add(1,std::memory_order_acq_rel);observed_epoch.store(master_epoch);observed_seconds.store(p);observed_playing.store(run);master_version.fetch_add(1,std::memory_order_release);
  }
  position.store(p);playing.store(run);
  }catch(const StaleMemory&){hold_memory();position.store(at);playing.store(false);}
  if(!timer.wait()){error=-102;break;}
 }}catch(...){if(!error)error=-101;}
 closing.store(true); // reject submissions before draining, including on driver failure
 try{if(sent.load()&&!error.load())resolve();}catch(...){}
 run=false;playing=false;
 {std::lock_guard<std::mutex> l(mu);for(auto c:queue)receipt(c.seq,3);queue.clear();}
 if(clock_release)clock_release(clock_user);if(send_release)send_release(send_user);
 {std::lock_guard<std::mutex> l(mu);closed.store(true);}
 cv.notify_all();}
};
static bool validMessage(uint32_t m){auto status=m&255;auto type=status&0xf0;return status>=0x80&&status<=0xef&&!(m&0xff808000u)&&((type!=0xc0&&type!=0xd0)||!(m&0xff0000u));}
MIDI_API uint32_t astra_midi_abi_version(){return 1;}
static void* create_impl(const AstraMidiEvent*e,uint64_t ne,const AstraMidiNote*n,uint64_t nn,double d,AstraMidiClockFn c,void*cu,AstraMidiSendFn s,void*su,AstraMidiLifetimeFn cr,AstraMidiLifetimeFn cf,AstraMidiLifetimeFn sr,AstraMidiLifetimeFn sf){
 if(bool(cr)!=bool(cf)||bool(sr)!=bool(sf)||(!c&&(cr||cf))||!s||(!e&&ne)||(!n&&nn)||ne>10000000||nn>10000000||!std::isfinite(d)||d<=0)return nullptr;
 for(uint64_t i=0;i<ne;i++)if(!std::isfinite(e[i].seconds)||e[i].seconds<0||e[i].seconds>d||!validMessage(e[i].packed)||(i&&e[i].seconds<e[i-1].seconds))return nullptr;
 for(uint64_t i=0;i<nn;i++)if(!std::isfinite(n[i].start)||!std::isfinite(n[i].end)||!std::isfinite(n[i].sounding_end)||n[i].start<0||n[i].start>n[i].end||n[i].end>n[i].sounding_end||n[i].sounding_end>d||n[i].channel>15||n[i].pitch>127||n[i].velocity<1||n[i].velocity>127)return nullptr;
 try{return new Scheduler(e,ne,n,nn,d,c,cu,s,su,cr,cf,sr,sf);}catch(...){return nullptr;}}
MIDI_API void* astra_midi_create_memory_owned(AstraMidiSendFn send,void* user,AstraMidiLifetimeFn retain,AstraMidiLifetimeFn release){
 if(!send||bool(retain)!=bool(release))return nullptr;
 try{return new Scheduler(nullptr,0,nullptr,0,0.,nullptr,nullptr,send,user,nullptr,nullptr,retain,release,true);}catch(...){return nullptr;}
}
MIDI_API int astra_midi_memory_fence(void*p,uint64_t gate){
 if(!p||!gate)return -1;
 auto s=static_cast<Scheduler*>(p);if(!s->memory_mode)return -1;
 auto previous=s->memory_floor.load(std::memory_order_relaxed);
 while(previous<gate&&!s->memory_floor.compare_exchange_weak(previous,gate,std::memory_order_release,std::memory_order_relaxed)){}
 s->timer.wake();return 0;
}
MIDI_API int64_t astra_midi_memory_submit(void*p,const AstraMidiMemoryPlan* plan,const AstraMidiEvent* events,uint64_t ne,const AstraMidiClick* beats,uint64_t nb){
 if(!p||!plan||plan->size<sizeof(*plan)||plan->version!=1||plan->reserved||!plan->gate||
    (plan->flags&~31u)||ne>10000000||nb>100000||(!events&&ne)||(!beats&&nb)||
    (!(plan->flags&ASTRA_MIDI_MEMORY_REPLACE)&&(ne||nb)))return -1;
 auto s=static_cast<Scheduler*>(p);if(!s->memory_mode)return -1;
 const auto& c=*plan;
 if(!std::isfinite(c.position)||c.position<0||!std::isfinite(c.duration)||c.duration<0||
    !std::isfinite(c.rate)||c.rate<.25||c.rate>2||!std::isfinite(c.gain)||c.gain<0||c.gain>1||
    !std::isfinite(c.left)||!std::isfinite(c.right)||c.left<0||c.right<0||c.left>c.duration||c.right>c.duration)return -1;
 std::unique_ptr<MemoryCommand> payload;
 try{
  payload=std::make_unique<MemoryCommand>();payload->plan=c;
  for(uint64_t i=0;i<ne;++i){const auto& e=events[i];
   if(!std::isfinite(e.seconds)||e.seconds<0||e.seconds>c.duration||e.reserved||!validMessage(e.packed)||
      (i&&e.seconds<events[i-1].seconds))return -1;
   payload->channels[e.packed&15]=true;
  }
  for(uint64_t i=0;i<nb;++i){const auto& e=beats[i];
   if(!std::isfinite(e.seconds)||e.seconds<0||e.seconds>=c.duration||e.accent>1||e.reserved||
      (i&&e.seconds<beats[i-1].seconds))return -1;
  }
  if(ne)payload->events.assign(events,events+ne);
  if(nb)payload->clicks.assign(beats,beats+nb);
 }catch(...){return -4;}
 std::lock_guard<std::mutex> l(s->mu);
 if(s->closing||s->closed)return -3;
 if(s->memory_queued!=s->memory_done.load(std::memory_order_acquire))return -2;
 const auto seq=s->enqueue(ApplyMemory,0,0,nullptr,payload.get());
 if(seq>0){s->memory_slot.swap(payload);s->memory_queued=uint64_t(seq);}
 return seq;
}
MIDI_API int64_t astra_midi_command(void*p,uint32_t k,double a,double b){if(!p||k<1||k>ClickGate||!std::isfinite(a)||!std::isfinite(b))return -1;auto s=static_cast<Scheduler*>(p);
 if(s->memory_mode)return -3;
 if((k==5&&(a<.25||a>2))||(k==7&&(a<0||a>1))||(k==3&&(a<0||a>s->duration))||(k==6&&(a<0||b<0||a>s->duration||b>s->duration))||(k==ClickGate&&((a!=0&&a!=1)||b!=0)))return -1;
 std::lock_guard<std::mutex>l(s->mu);if(s->closing||s->closed||(s->clock&&k!=7&&k!=ClickGate))return -3;
 return s->enqueue(k,a,b);
}
MIDI_API int64_t astra_midi_set_clicks(void*p,const AstraMidiClick* beats,uint64_t count,uint32_t enabled){
 if(!p||enabled>1||count>100000||(!beats&&count))return -1;
 auto s=static_cast<Scheduler*>(p);
 if(s->memory_mode)return -3;
 for(uint64_t i=0;i<count;++i){const auto& e=beats[i];
  if(!std::isfinite(e.seconds)||e.seconds<0||e.seconds>=s->duration||e.accent>1||e.reserved||(i&&e.seconds<beats[i-1].seconds))return -1;
 }
 std::unique_ptr<const ClickTrack> table;
 try{if(count)table=std::make_unique<ClickTrack>(beats,beats+count);}catch(...){return -4;}
 std::lock_guard<std::mutex>l(s->mu);
 if(s->closing||s->closed)return -3;
 if(s->click_installed.load(std::memory_order_acquire)!=s->click_queued)return -2;
 const auto seq=s->enqueue(ReplaceClicks,double(enabled),0,table.get());
 if(seq>0){
  // The worker cannot dequeue until we unlock. The displaced table goes back
  // to the submitter's local and is destroyed after releasing mu.
  s->click_slots[s->click_slot].swap(table);
  s->click_slot^=1;s->click_queued=uint64_t(seq);
 }
 return seq;
}
MIDI_API int astra_midi_metronome_status(void*p,AstraMidiMetronomeStatus*out){
 if(!p||!out||out->size<sizeof(*out))return -1;
 auto s=static_cast<Scheduler*>(p);out->version=1;
 out->note_ons=s->click_note_ons.load(std::memory_order_relaxed);out->note_offs=s->click_note_offs.load(std::memory_order_relaxed);
 out->late_skipped=s->click_late_skipped.load(std::memory_order_relaxed);
 out->beats=s->click_beats.load(std::memory_order_relaxed);out->enabled=s->click_enabled.load(std::memory_order_relaxed);
 out->channel=s->click_channel;out->gain_cc7=s->click_gain_cc7.load(std::memory_order_relaxed);return 0;
}
MIDI_API int astra_midi_snapshot(void*p,AstraMidiStatus*out){if(!p||!out||out->size<sizeof(*out))return -1;auto s=static_cast<Scheduler*>(p);out->version=1;out->seconds=s->position;out->accepted=s->accepted;out->applied=s->applied;out->sent=s->sent;out->playing=s->playing;out->closed=s->closed;out->error=s->error;out->reserved=s->timer.kind;return 0;}
MIDI_API void astra_midi_close(void*p){if(p){auto s=static_cast<Scheduler*>(p);s->closing=true;s->timer.wake();s->cv.notify_all();}}
MIDI_API int astra_midi_wait(void*p,uint32_t ms){if(!p)return 0;auto s=static_cast<Scheduler*>(p);std::unique_lock<std::mutex>l(s->mu);return s->cv.wait_for(l,std::chrono::milliseconds(ms),[&]{return s->closed.load();})?1:0;}
MIDI_API int astra_midi_destroy(void*p){if(!p)return 1;auto s=static_cast<Scheduler*>(p);if(!s->closed)return 0;if(s->worker.joinable())s->worker.join();delete s;return 1;}

MIDI_API void* astra_midi_create(const AstraMidiEvent*e,uint64_t ne,const AstraMidiNote*n,uint64_t nn,double d,AstraMidiClockFn c,void*cu,AstraMidiSendFn s,void*su){return create_impl(e,ne,n,nn,d,c,cu,s,su,nullptr,nullptr,nullptr,nullptr);}
MIDI_API void* astra_midi_create_owned(const AstraMidiEvent*e,uint64_t ne,const AstraMidiNote*n,uint64_t nn,double d,AstraMidiClockFn c,void*cu,AstraMidiSendFn s,void*su,AstraMidiLifetimeFn cr,AstraMidiLifetimeFn cf,AstraMidiLifetimeFn sr,AstraMidiLifetimeFn sf){return create_impl(e,ne,n,nn,d,c,cu,s,su,cr,cf,sr,sf);}

MIDI_API int astra_midi_receipt(void*p,uint64_t seq){
 if(!p||!seq)return -1;auto s=static_cast<Scheduler*>(p);
 // Read the watermarks before the slot: if execution/closure is visible,
 // publication of its receipt must already be visible (unless evicted).
 const auto done=s->applied.load(std::memory_order_acquire);
 const auto closed=s->closed.load(std::memory_order_acquire);
 const auto value=s->receipts[seq%512].value.load(std::memory_order_acquire);
 if((value>>3)==seq)return int(value&7);
 return !closed&&seq>done&&seq<=s->accepted.load()?0:-1;
}
MIDI_API uint64_t astra_midi_superseded(void*p){return p?static_cast<Scheduler*>(p)->superseded.load():0;}

MIDI_API int astra_midi_master_snapshot(void*p,AstraMidiClock*out){if(!p||!out||out->size<sizeof(*out))return -1;auto s=static_cast<Scheduler*>(p);if(!s->clock)return -2;for(int i=0;i<8;i++){auto v=s->master_version.load(std::memory_order_acquire);if(!v||(v&1))continue;auto epoch=s->observed_epoch.load();auto seconds=s->observed_seconds.load();auto playing=s->observed_playing.load();if(v==s->master_version.load(std::memory_order_acquire)){out->version=1;out->epoch=epoch;out->seconds=seconds;out->playing=playing;out->reserved=0;return 0;}}return 1;}

static int silent_send(void*,uint32_t){return 0;}
MIDI_API void* astra_midi_create_silent(const AstraMidiEvent* e,uint64_t ne,const AstraMidiNote* n,uint64_t nn,double duration){
 return create_impl(e,ne,n,nn,duration,nullptr,nullptr,silent_send,nullptr,nullptr,nullptr,nullptr,nullptr);
}
MIDI_API void* astra_midi_create_memory_silent(){return astra_midi_create_memory_owned(silent_send,nullptr,nullptr,nullptr);}
