#include "transport_declick.hpp"
namespace {
void declick_sample_contract(){
 for(unsigned period:{64u,240u}){astra::rt::TransportDeclick d(48000);float v[2]={1,-1};d.frame(v);d.arm();
  for(unsigned i=0;i<320;++i){float prior=v[0];if(i%period==0)d.arm();v[0]=v[1]=0;d.frame(v);
   if(i%period==0)CHECK(v[0]==prior);if(i>=240)CHECK(v[0]==0&&v[1]==0);
  }
 }

 for(unsigned block:{1u,17u,255u,256u,1024u}){
  astra::rt::TransportDeclick d(48000);float v[2]={.75f,-.5f};d.frame(v);d.arm();
  unsigned at=0;
  while(at<480){for(unsigned i=0;i<block&&at<480;++i,++at){
   v[0]=-.25f;v[1]=.25f;d.frame(v);
   float a=at<240?float(239-at)/239:0.f;
   CHECK(std::abs(v[0]-(.75f*a-.25f*(1-a)))<1e-6);
   CHECK(std::abs(v[1]-(-.5f*a+.25f*(1-a)))<1e-6);
  }}
  d.arm();v[0]=.1f;v[1]=.1f;d.frame(v);CHECK(v[0]==-.25f); // continues current, not unity
  d.clear();d.arm();v[0]=v[1]=0;d.frame(v);CHECK(v[0]==0&&v[1]==0);
 }
}
struct FadeFake {bool audition=false,fail=false,miss=false,silent=false;float level=.4f;unsigned calls=0;};
int32_t ASTRA_RT_CALL fade_process(void* p,float* out,uint32_t n,const AvRtMidiEvent* e,uint32_t count,const AvRtContext* x){
 auto& f=*static_cast<FadeFake*>(p);++f.calls;if(f.fail)return -1;
 for(unsigned j=0;j<count;++j){if((e[j].status&0xf0)==0x90&&e[j].data2)f.audition=true;if((e[j].status&0xf0)==0x80)f.audition=false;}
 float v=(x->flags&AV_RT_PLAYING)?(x->sample_position<1000?f.level:-f.level):(f.audition?.2f:0.f);
 std::fill_n(out,2*n,f.silent?0.f:v);return 0;
}
int fade_pcm_read(void* p,uint32_t,uint64_t at,uint32_t n,float* out){
 auto& f=*static_cast<FadeFake*>(p);if(f.miss)return 0;
 std::fill_n(out,2*n,at>=9000?0.f:(at<1000?.25f:-.25f));return 1;
}
void declick_live_buses(){
 for(unsigned bus=0;bus<4;++bus){
  FadeFake f;auto c=live_record<AstraLiveConfig>();c.sample_rate=48000;c.max_frames=1024;c.gain=.19;c.bus_mode=1;
  c.midi_gain=bus==0||bus==3?1:0;c.audio_gain=bus==1||bus==3?1:0;c.click_gain=1;c.metronome=bus==2||bus==3;
  AstraRtBridge* b=nullptr;CHECK(astra_rt_live_create(&c,&f,fade_process,&b)==0);FakeOwner owner(b);
  AstraLivePcmSource source{sizeof(AstraLivePcmSource),1,48000,10000,&f,pcm_req,fade_pcm_read,pcm_hint};auto ticket=park(b);owner.pump(0,nullptr);CHECK(astra_rt_live_bind_pcm(b,ticket,&source)==0);CHECK(astra_rt_resume(b,ticket)==0);
  auto spec=live_record<AstraLiveSpec>();spec.id=1;spec.epoch=1;spec.sequence=1;spec.duration=10000;spec.sample_rate=48000;spec.clock_count=1;AstraLiveClock clock{0,0,120,0,4,4};AstraLiveClick clicks[]={{0,1,0},{1000,0,0}};uint64_t old=0;uint32_t skip=0;
  CHECK(astra_rt_live_publish_clicks(b,&spec,nullptr,&clock,clicks,2,&old,&skip)==0);
  uint64_t id=1,intent=0;
  auto cmd=[&](unsigned op,int64_t a=0,int64_t end=0){auto q=live_record<AstraLiveCommand>();q.id=++id;q.epoch=1;q.sequence=1;q.intent=++intent;q.op=op;q.a=a;q.b=end;q.token=99;q.channel=0;q.pitch=60;q.velocity=80;CHECK(astra_rt_live_submit(b,&q)==0);};
  auto status=[&](){auto q=live_record<AstraLiveSnapshot>();CHECK(astra_rt_live_snapshot(b,&q)==0);return q;};
  std::array<float,2048> out{};cmd(ASTRA_LIVE_PLAY);owner.pump(64,out.data());float last=out[126];CHECK(last!=0);
  cmd(ASTRA_LIVE_PAUSE);owner.pump(0,nullptr);CHECK(status().state==ASTRA_LIVE_PLAYING&&status().position==64);
  owner.pump(1,out.data());CHECK(out[0]==last&&status().position==64&&status().state==ASTRA_LIVE_PAUSED);
  owner.pump(255,out.data());CHECK(out[508]==0&&out[509]==0); // finite, no permanently gated plugin
  cmd(ASTRA_LIVE_ON);owner.pump(17,out.data());CHECK(std::abs(out[0]-.2f*float(c.midi_gain)*.19f)<1e-6);
  cmd(ASTRA_LIVE_OFF);owner.pump(17,out.data());CHECK(out[0]==0);
  cmd(ASTRA_LIVE_PLAY,1000);owner.pump(17,out.data());CHECK(out[0]==0&&status().position==1017);last=out[32];
  cmd(ASTRA_LIVE_SEEK,2000);owner.pump(1,out.data());CHECK(out[0]==last&&status().position==2001);last=out[0];
  cmd(ASTRA_LIVE_PAUSE);cmd(ASTRA_LIVE_SEEK,3000);cmd(ASTRA_LIVE_PLAY,-1);owner.pump(17,out.data());CHECK(out[0]==last&&status().position==3017&&status().last_intent==intent);
  for(float v:out)CHECK(std::isfinite(v)&&std::abs(v)<=.19f*(.4f+.25f+.32f)+1e-6);
  // All accepted commands still produce ordered immediate receipts.
  auto receipt=live_record<AstraLiveReceipt>();uint64_t previous=0;while(astra_rt_live_poll(b,&receipt)==0){CHECK(receipt.id>previous&&receipt.result==ASTRA_LIVE_APPLIED);previous=receipt.id;}CHECK(previous==id);
  // Same-epoch note/timeline replacement does not trigger a fresh bridge.
  owner.pump(256,out.data());last=out[510];f.level=.1f;spec.id=++id;spec.sequence=2;CHECK(astra_rt_live_publish_clicks(b,&spec,nullptr,&clock,clicks,2,&old,&skip)==0);owner.pump(1,out.data());CHECK(std::abs(out[0]-float((-.1*c.midi_gain-.25*c.audio_gain)*.19))<1e-6);if(bus==0)CHECK(out[0]!=last);
  // Non-muted park removes historical output instead of relying on master=0.
  ticket=park(b);owner.pump(17,out.data());CHECK(astra_rt_live_bind_pcm(b,ticket,nullptr)==0);f.silent=true;CHECK(astra_rt_resume(b,ticket)==0);owner.pump(17,out.data());for(unsigned j=0;j<34;++j)CHECK(out[j]==0);
  ticket=park(b);owner.pump(0,nullptr);CHECK(astra_rt_live_bind_pcm(b,ticket,&source)==0);f.silent=false;CHECK(astra_rt_resume(b,ticket)==0);owner.pump(256,out.data());
  // Mute never resurrects history, and park/fault clear saved samples.
  auto m=live_record<AstraLiveCommand>();m.id=++id;m.op=ASTRA_LIVE_MIX;m.gain=.19;m.muted=1;m.bus_mode=1;m.midi_gain=c.midi_gain;m.audio_gain=c.audio_gain;m.click_gain=1;m.metronome=c.metronome;CHECK(astra_rt_live_submit(b,&m)==0);owner.pump(256,out.data());for(unsigned i=480;i<512;++i)CHECK(out[i]==0);
  cmd(ASTRA_LIVE_SEEK,0);owner.pump(17,out.data());for(unsigned i=0;i<34;++i)CHECK(out[i]==0);
  ticket=park(b);owner.pump(17,out.data());for(unsigned i=0;i<34;++i)CHECK(out[i]==0);CHECK(astra_rt_resume(b,ticket)==0);
  m.id=++id;m.muted=0;CHECK(astra_rt_live_submit(b,&m)==0);owner.pump(256,out.data());
  f.fail=true;owner.pump(17,out.data());for(unsigned i=0;i<34;++i)CHECK(out[i]==0);
  owner.request_retire();owner.join();CHECK(astra_rt_destroy(b)==0);
 }
}
}
