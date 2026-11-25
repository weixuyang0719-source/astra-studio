namespace {
void live_click_same_callback() {
 auto f=std::make_unique<LiveFake>();auto c=live_record<AstraLiveConfig>();
 c.sample_rate=48000;c.max_frames=64;c.gain=.19;c.bus_mode=1;c.midi_gain=0;c.audio_gain=1;c.click_gain=1;c.metronome=1;
 AstraRtBridge* b=nullptr;CHECK(astra_rt_live_create(&c,f.get(),live_fake_process,&b)==0);FakeOwner owner(b);
 auto spec=live_record<AstraLiveSpec>();spec.id=1;spec.epoch=1;spec.sequence=1;spec.duration=10000;spec.sample_rate=48000;spec.clock_count=1;
 AstraLiveClock clock{0,0,120,0,0,0}; // free: explicit pulses, never invented meter
 AstraLiveClick clicks[]={{0,0,0},{901,1,0}};uint64_t old=0;uint32_t unsupported=0;
 CHECK(astra_rt_live_publish_clicks(b,&spec,nullptr,&clock,clicks,2,&old,&unsupported)==0);
 auto cmd=live_record<AstraLiveCommand>();cmd.id=2;cmd.op=ASTRA_LIVE_PLAY;cmd.epoch=1;cmd.sequence=1;cmd.intent=1;cmd.a=0;CHECK(astra_rt_live_submit(b,&cmd)==0);
 std::array<float,128> out{};owner.pump(64,out.data());
 CHECK(out[0]==0.f);float peak=0;for(float v:out)peak=std::max(peak,std::abs(v));CHECK(peak>0&&peak<.19*.2);
 CHECK(f->active==0); // click is PCM, not a plugin MIDI drum voice
 auto state=live_record<AstraLiveSnapshot>();CHECK(astra_rt_live_snapshot(b,&state)==0);CHECK(state.metronome&&state.midi_gain==0&&state.gain==.19&&state.position==64);
 const auto initial=out;
 const float before_seek=out[126];
 cmd.id=3;cmd.op=ASTRA_LIVE_SEEK;cmd.intent=2;cmd.a=0;CHECK(astra_rt_live_submit(b,&cmd)==0);owner.pump(64,out.data());CHECK(out[0]==before_seek);CHECK(astra_rt_live_snapshot(b,&state)==0&&state.position==64);
 cmd.id=4;cmd.op=ASTRA_LIVE_LOOP_ON;cmd.intent=3;cmd.a=0;cmd.b=64;CHECK(astra_rt_live_submit(b,&cmd)==0);
 owner.pump(64,out.data()); // moves to loop origin by existing loop logic
 const float before_loop=out[126];owner.pump(64,out.data());CHECK(out[0]==before_loop);CHECK(astra_rt_live_snapshot(b,&state)==0&&state.position==0);
 auto mix=live_record<AstraLiveCommand>();mix.id=6;mix.op=ASTRA_LIVE_MIX;mix.gain=.19;mix.bus_mode=1;mix.midi_gain=0;mix.audio_gain=1;mix.click_gain=1;mix.metronome=0;
 CHECK(astra_rt_live_submit(b,&mix)==0);for(unsigned i=0;i<8;++i)owner.pump(64,out.data());for(float v:out)CHECK(v==0);
 CHECK(astra_rt_live_snapshot(b,&state)==0);CHECK(!state.metronome&&state.state==ASTRA_LIVE_PLAYING);
 mix.id=7;mix.metronome=1;mix.muted=1;CHECK(astra_rt_live_submit(b,&mix)==0);for(unsigned i=0;i<5;++i)owner.pump(64,out.data());for(float v:out)CHECK(v==0);
 owner.request_retire();owner.join();CHECK(astra_rt_destroy(b)==0);
}
}
