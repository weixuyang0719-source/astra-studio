namespace {
struct PcmFake {uint64_t last=UINT64_MAX;unsigned reads=0;};
int pcm_req(void*,uint32_t,uint64_t,uint64_t){return 1;}
int pcm_hint(void*,uint32_t,uint64_t,uint64_t){return 1;}
int pcm_read(void* x,uint32_t,uint64_t at,uint32_t n,float* out){auto& f=*static_cast<PcmFake*>(x);f.last=at;++f.reads;std::fill_n(out,2*n,.5f);return 1;}
void live_pcm_grant_and_bus(){
 auto f=std::make_unique<LiveFake>();auto c=live_record<AstraLiveConfig>();c.sample_rate=48000;c.max_frames=64;c.gain=.19;c.bus_mode=1;c.midi_gain=0;c.audio_gain=.8;c.click_gain=1;
 AstraRtBridge* b=nullptr;CHECK(astra_rt_live_create(&c,f.get(),live_fake_process,&b)==0);FakeOwner owner(b);PcmFake pcm;
 AstraLivePcmSource source{sizeof(AstraLivePcmSource),1,48000,10000,&pcm,pcm_req,pcm_read,pcm_hint};
 CHECK(astra_rt_live_bind_pcm(b,1,&source)!=0);const auto ticket=park(b);
 CHECK(astra_rt_live_bind_pcm(b,ticket,&source)==ASTRA_RT_PENDING);owner.pump(0,nullptr);
 source.sample_rate=44100;CHECK(astra_rt_live_bind_pcm(b,ticket,&source)==ASTRA_RT_INVALID);source.sample_rate=48000;
 CHECK(astra_rt_live_bind_pcm(b,ticket,&source)==0);CHECK(astra_rt_resume(b,ticket)==0);
 auto spec=live_record<AstraLiveSpec>();spec.id=1;spec.epoch=1;spec.sequence=1;spec.duration=10000;spec.sample_rate=48000;spec.clock_count=1;AstraLiveClock clock{0,0,120,0,4,4};uint64_t old=0;uint32_t unsupported=0;
 CHECK(astra_rt_live_publish(b,&spec,nullptr,&clock,&old,&unsupported)==0);
 auto cmd=live_record<AstraLiveCommand>();cmd.id=2;cmd.op=ASTRA_LIVE_PLAY;cmd.epoch=1;cmd.sequence=1;cmd.intent=1;cmd.a=0;CHECK(astra_rt_live_submit(b,&cmd)==0);
 std::array<float,128> out{};owner.pump(64,out.data());CHECK(pcm.last==0&&pcm.reads==1);for(float v:out)CHECK(std::abs(v-.076f)<1e-7);
 CHECK(astra_rt_live_bind_pcm(b,ticket,nullptr)==ASTRA_RT_STALE_TICKET);
 auto t=park(b);owner.pump(0,nullptr);CHECK(astra_rt_live_bind_pcm(b,t,nullptr)==0);CHECK(astra_rt_resume(b,t)==0);owner.pump(64,out.data());for(float v:out)CHECK(v==0);
 owner.request_retire();owner.join();CHECK(astra_rt_destroy(b)==0);
}
}
