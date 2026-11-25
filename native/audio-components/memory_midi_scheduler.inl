// Scheduler members. The producer owns the pending payload until its receipt;
// swapping vectors returns the old storage to that payload, not to the timing worker.
 void choose_click_channel(){
  int ch=-1;
  if(!channels[9])ch=9;
  else for(int i=15;i>=0;--i)if(!channels[i]){ch=i;break;}
  click_channel.store(ch,std::memory_order_relaxed);
 }
 struct StaleMemory {};
 struct SendGuard {
  Scheduler& s;bool previous;
  SendGuard(Scheduler& owner,bool on):s(owner),previous(s.guard_sends){s.guard_sends=on&&s.memory_mode;}
  ~SendGuard(){s.guard_sends=previous;}
 };
 bool memory_revoked() const {
  return closing.load(std::memory_order_acquire)||memory_floor.load(std::memory_order_acquire)!=memory_gate;
 }
 void check_memory_fence() const {if(guard_sends&&memory_revoked())throw StaleMemory{};}
 void hold_memory(){
  const auto p=nowpos();resolve();run=false;at=p;click_lane_ready=false;
 }
 int install_memory(MemoryCommand& cmd){
  const auto& p=cmd.plan;
  if(closing.load()||p.gate!=memory_floor.load(std::memory_order_acquire))return 2;
  const bool swap_score=p.flags&ASTRA_MIDI_MEMORY_REPLACE;
  if((!swap_score&&(!memory_have_score||p.score!=memory_score||p.duration!=duration))||
      (memory_have_score&&p.score<memory_score))throw std::logic_error("memory MIDI score order");
  const bool want=p.flags&ASTRA_MIDI_MEMORY_PLAY;
  const bool click=p.flags&ASTRA_MIDI_MEMORY_CLICK;
  const bool new_loop=(p.flags&ASTRA_MIDI_MEMORY_LOOP)&&p.left!=p.right;
  const bool move=!memory_have_locator||p.locator!=memory_locator;
  const auto sampled=nowpos();
  double x=std::clamp(move?p.position:sampled,0.0,p.duration);
  if(move&&want&&(p.flags&ASTRA_MIDI_MEMORY_RESTART)&&x>=p.duration)x=0;
  if(new_loop&&((p.right<p.left&&p.right<=x&&x<p.left)||
      (want&&p.left<p.right&&x>=p.right)))x=p.left;
  if(run&&(swap_score||move||!want||p.gate!=memory_gate||x!=sampled)){
   // The old channel/voice and click trackers are still authoritative here.
   resolve();run=false;
  }
  if(swap_score){
   events.swap(cmd.events);memory_clicks.swap(cmd.clicks);
   click_track=&memory_clicks;click_index=0;click_lane_ready=false;
   click_beats.store(uint32_t(memory_clicks.size()),std::memory_order_relaxed);
   channels=cmd.channels;choose_click_channel();
   duration=p.duration;memory_score=p.score;memory_have_score=true;
  }
  if(move){memory_locator=p.locator;memory_have_locator=true;memory_end=false;}
  if(!run||p.rate!=rate){at=x;anchor=Clock::now();}
  rate=p.rate;left=p.left;right=p.right;loop=new_loop;
  memory_gate=p.gate;
  SendGuard permission(*this,true);
  check_memory_fence();
  if(swap_score||click!=bool(click_enabled.load(std::memory_order_relaxed))){
   if(!click)check_send(stop_click());
   gate_clicks(click);
  }
  if(volume!=p.gain){
   volume=p.gain;
   const int gain=int(std::lround(100*volume));
   click_gain_cc7.store(gain,std::memory_order_relaxed);
   if(run)for(int ch=0;ch<16;++ch)if(channels[ch])raw(msg(0xb0,ch,7,int(std::lround(volumes[ch]*volume))));
   if(click_lane_ready)check_send(transmit(msg(0xb0,click_channel,7,gain)));
  }
  if(want&&!memory_end&&!run){
   if(at<duration){chase(at);run=true;anchor=Clock::now();}
   else memory_end=true;
  }
  check_memory_fence();
  return 1;
 }
 void apply_memory(Cmd c){
  int status;
  try{status=install_memory(*c.memory);}catch(const StaleMemory&){hold_memory();status=2;}
  if(status==2&&run&&memory_revoked())hold_memory();
  position.store(nowpos());playing.store(run);
  // No dereference of c.memory after this publication. In particular a stale
  // receipt is also an ownership receipt, even if it cut a partial chase.
  memory_done.store(c.seq,std::memory_order_release);
  receipt(c.seq,status);applied.store(c.seq,std::memory_order_release);
 }
