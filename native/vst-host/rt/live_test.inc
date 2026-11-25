// Included by bridge_test.cpp to reuse its real native fake-device owner.
namespace {
struct LiveFake {
    struct Block {AvRtContext clock;uint32_t n,first,count;};
    std::array<Block,64> blocks{};
    std::array<AvRtMidiEvent,16000> events{};
    std::array<int32_t,16> voices{};
    uint32_t calls=0,seen=0,active=0;
    bool pedal=false;
};
extern "C" int32_t ASTRA_RT_CALL live_fake_process(void* p,float* out,uint32_t n,
    const AvRtMidiEvent* e,uint32_t count,const AvRtContext* x) {
    auto& f=*static_cast<LiveFake*>(p);
    CHECK(n && out && x && n<=64 && f.calls<f.blocks.size() && f.seen+count<=f.events.size());
    CHECK((count!=0)==(e!=nullptr));
    f.blocks[f.calls++]={*x,n,f.seen,count};
    for (uint32_t i=0;i<count;++i) {
        CHECK(e[i].sample_offset<n && (!i || e[i-1].sample_offset<=e[i].sample_offset));
        f.events[f.seen++]=e[i];
    }
    uint32_t j=0;
    for (uint32_t i=0;i<n;++i) {
        while (j<count && e[j].sample_offset==i) {
            const auto& m=e[j++]; const auto kind=m.status&0xf0;
            if (kind==0x90 && m.data2) {
                CHECK(m.note_id>=0 && f.active<f.voices.size());
                CHECK(std::find(f.voices.begin(),f.voices.begin()+f.active,m.note_id)==f.voices.begin()+f.active);
                f.voices[f.active++]=m.note_id;
            } else if (kind==0x80 || kind==0x90) {
                auto it=std::find(f.voices.begin(),f.voices.begin()+f.active,m.note_id);
                CHECK(it!=f.voices.begin()+f.active);*it=f.voices[--f.active];
            } else if (m.status==0xb2 && m.data1==64) f.pedal=m.data2>=64;
        }
        out[2*i]=static_cast<float>(f.active);out[2*i+1]=-out[2*i];
    }
    return 0;
}

template<class T> T live_record() {T r{};r.size=sizeof(T);r.version=ASTRA_LIVE_VERSION;return r;}

void live_timeline_and_audition() {
    CHECK(astra_rt_live_version()==ASTRA_LIVE_VERSION && astra_rt_abi_version()==1);
    auto fake=std::make_unique<LiveFake>();
    auto c=live_record<AstraLiveConfig>();c.sample_rate=48000;c.max_frames=64;c.gain=1.;
    AstraRtBridge* b=nullptr;
    CHECK(astra_rt_live_create(&c,fake.get(),live_fake_process,&b)==ASTRA_RT_OK);
    FakeOwner owner(b,4); // paOutputUnderflow is a count, not an OR of flags
    AstraLiveClock clocks[]={{0,0.,120.,-1.,4,4},{32,32./24000.,60.,0.,3,4}};
    std::vector<AstraRtEvent> midi{{17,0x92,60,96,0,0},{17,0xb2,64,127,0,0},{40,0x82,60,12,0,0}};
    auto spec=live_record<AstraLiveSpec>();spec.sample_rate=48000;spec.duration=160;spec.epoch=1;spec.clock_count=2;
    auto publish=[&](uint64_t id,uint64_t seq,uint64_t replaced,uint32_t unsupported=0) {
        spec.id=id;spec.sequence=seq;spec.event_count=static_cast<uint32_t>(midi.size());
        uint64_t old=0;uint32_t skipped=0;
        CHECK(astra_rt_live_publish(b,&spec,midi.data(),clocks,&old,&skipped)==ASTRA_RT_OK);
        CHECK(old==replaced && skipped==unsupported);
    };
    auto cmd=[&](uint64_t id,uint32_t op,uint64_t intent,uint64_t seq=0,int64_t a=0,int64_t tail=0,uint64_t token=0) {
        auto v=live_record<AstraLiveCommand>();v.id=id;v.op=op;v.epoch=1;v.intent=intent;v.sequence=seq;
        v.a=a;v.b=tail;v.token=token;
        if (op==ASTRA_LIVE_ON) {v.channel=2;v.pitch=60;v.velocity=80;}
        CHECK(astra_rt_live_submit(b,&v)==ASTRA_RT_OK);
    };
    auto status=[&] {auto s=live_record<AstraLiveSnapshot>();CHECK(astra_rt_live_snapshot(b,&s)==0);return s;};
    auto receipt=[&](uint64_t id,uint32_t result=ASTRA_LIVE_APPLIED) {
        auto r=live_record<AstraLiveReceipt>();CHECK(astra_rt_live_poll(b,&r)==0 && r.id==id && r.result==result);
    };
    auto out=std::array<float,130>{};out.front()=123.f;out.back()=456.f;
    publish(1,1,0);cmd(2,ASTRA_LIVE_PLAY,4,1,-1);cmd(3,ASTRA_LIVE_ON,0,0,0,0,90);
    CHECK(!status().has_timeline);
    owner.pump(24,out.data()+1);
    receipt(1);receipt(2);receipt(3);
    CHECK(fake->calls==1 && fake->seen==3 && fake->active==2);
    CHECK(out[1]==1.f && out[1+2*16]==1.f && out[1+2*17]==2.f);
    CHECK(fake->events[0].live && fake->events[0].status==0x92 && fake->events[0].data2==80);
    CHECK(!fake->events[1].live && fake->events[1].sample_offset==17 && fake->events[1].data2==96);
    CHECK(fake->events[2].sample_offset==17 && fake->events[2].status==0xb2 && fake->events[2].data2==127);
    const auto timeline_note=fake->events[1].note_id;
    CHECK(timeline_note!=fake->events[0].note_id && fake->blocks[0].clock.bar_start_beat==-1.);

    midi[2].sample=90; // extending a held note doesn't retrigger it
    publish(4,2,0);publish(5,3,4);cmd(6,ASTRA_LIVE_OFF,0,0,0,0,90);
    CHECK(status().sequence==1 && status().position==24);
    owner.pump(24,out.data()+1);
    receipt(5);receipt(6);
    CHECK(status().position==48 && status().sequence==3 && status().underruns==2);
    CHECK(fake->active==1 && fake->voices[0]==timeline_note && fake->pedal);
    CHECK(fake->blocks[1].n==8 && fake->blocks[1].clock.bpm==120.);
    const auto& x=fake->blocks[2].clock;
    CHECK(x.sample_position==32 && x.beat==32./24000. && x.bpm==60. && x.numerator==3 && x.bar_start_beat==0.);
    CHECK(out[1]==1.f && out[1+2*23]==1.f);

    midi.push_back({48,0xc2,5,0,0,0});
    for (unsigned i=0;i<4096;++i) midi.push_back({48,static_cast<uint8_t>(0xb0|(i/128)%16),
                                                 static_cast<uint8_t>(i%120),static_cast<uint8_t>(i%128),0,0});
    publish(7,4,0,1);
    owner.pump(8,out.data()+1);receipt(7);
    CHECK(fake->blocks[fake->calls-1].count==4097); // full authored batch plus chase
    CHECK(fake->active==1 && fake->voices[0]==timeline_note);

    cmd(8,ASTRA_LIVE_PLAY,5,99,-1);
    owner.pump(1,out.data()+1);
    CHECK(status().position==57 && status().state==ASTRA_LIVE_PLAYING);
    auto r=live_record<AstraLiveReceipt>();CHECK(astra_rt_live_poll(b,&r)==ASTRA_RT_EMPTY);
    cmd(9,ASTRA_LIVE_PAUSE,6);cmd(10,ASTRA_LIVE_SEEK,7,0,60);owner.pump(8,out.data()+1);
    receipt(8,ASTRA_LIVE_OBSOLETE);receipt(9);receipt(10);
    CHECK(status().state==ASTRA_LIVE_PAUSED && status().position==60 && !fake->active && !fake->pedal);
    cmd(11,ASTRA_LIVE_LOOP_ON,7,0,60,64);cmd(12,ASTRA_LIVE_PLAY,8,4,-1);
    owner.pump(6,out.data()+1);receipt(11);receipt(12);
    CHECK(status().position==62 && status().loop && fake->active==1);
    CHECK(fake->blocks[fake->calls-1].clock.flags&AV_RT_CYCLE);

    const auto ticket=park(b);
    CHECK(park_state(b,ticket)==ASTRA_RT_PARK_PENDING && astra_rt_destroy(b)==ASTRA_RT_BUSY);
    owner.pump(0,nullptr);
    CHECK(park_state(b,ticket)==ASTRA_RT_PARK_ACK && !fake->active);
    const auto calls=fake->calls;
    owner.pump(8,out.data()+1);
    CHECK(calls==fake->calls && out[1]==0.f && status().parked);
    CHECK(astra_rt_resume(b,ticket)==0);
    owner.pump(1,out.data()+1);
    CHECK(status().state==ASTRA_LIVE_PLAYING && status().position==63 && fake->active==1);
    cmd(13,ASTRA_LIVE_STOP,9);owner.pump(2,out.data()+1);receipt(13);
    CHECK(status().state==ASTRA_LIVE_STOPPED && status().position==60 && !fake->active);
    CHECK(out.front()==123.f && out.back()==456.f);

    clocks[1].beat=2.;spec.sequence=5;spec.id=14;
    uint64_t old=0;uint32_t skipped=0;
    CHECK(astra_rt_live_publish(b,&spec,midi.data(),clocks,&old,&skipped)==ASTRA_RT_INVALID);
    CHECK(status().sequence==4);
    auto stale=live_record<AstraLiveCommand>();stale.op=ASTRA_LIVE_STOP;stale.id=15;stale.epoch=2;
    CHECK(astra_rt_live_submit(b,&stale)==ASTRA_LIVE_STALE);
    std::array<float,2> sentinel{7.f,8.f};owner.pump(65,sentinel.data());
    CHECK(owner.last_result()==2 && sentinel[0]==7.f && sentinel[1]==8.f);
    CHECK(status().fault==ASTRA_RT_FAULT_CALLBACK_BUFFER);
    owner.join();CHECK(astra_rt_destroy(b)==0);
}
}

namespace {
AvRtContext mix_context{};
extern "C" int32_t ASTRA_RT_CALL live_constant_process(void*,float* out,uint32_t n,
    const AvRtMidiEvent*,uint32_t,const AvRtContext* context) {
    mix_context=*context;std::fill_n(out,2*n,1.f);return 0;
}
void live_master_mix() {
    auto c=live_record<AstraLiveConfig>();c.sample_rate=48000;c.max_frames=64;c.gain=.19;
    AstraRtBridge* b=nullptr;
    CHECK(astra_rt_live_create(&c,&c,live_constant_process,&b)==ASTRA_RT_OK);
    FakeOwner owner(b);std::array<float,128> out{};
    owner.pump(64,out.data());
    for (auto v:out) CHECK(std::abs(v-.19f)<1.e-6f); // no full-volume first callback
    auto mix=live_record<AstraLiveCommand>();mix.id=900;mix.op=ASTRA_LIVE_MIX;mix.gain=.19;mix.muted=1;
    CHECK(astra_rt_live_submit(b,&mix)==ASTRA_RT_OK); // no project/epoch needed
    float previous=.19f;
    for (unsigned j=0;j<4;++j) {owner.pump(64,out.data());for (unsigned i=0;i<64;++i){CHECK(out[2*i]<=previous && out[2*i]>=0.f);CHECK(out[2*i]==out[2*i+1]);previous=out[2*i];}}
    CHECK(previous==0.f);
    auto view=live_record<AstraLiveSnapshot>();CHECK(astra_rt_live_snapshot(b,&view)==0);
    CHECK(view.gain==.19 && view.muted && view.effective_gain==0. && view.epoch==0);
    auto receipt=live_record<AstraLiveReceipt>();CHECK(astra_rt_live_poll(b,&receipt)==0 && receipt.id==900 && receipt.result==0);
    mix.id=901;mix.muted=0;mix.gain=.4;CHECK(astra_rt_live_submit(b,&mix)==0);
    for (unsigned j=0;j<4;++j) owner.pump(64,out.data());
    CHECK(std::abs(out.back()-.4f)<1.e-6f);
    auto spec=live_record<AstraLiveSpec>();spec.id=903;spec.epoch=1;spec.sequence=1;spec.duration=1000;spec.sample_rate=48000;spec.clock_count=1;
    AstraLiveClock free_clock{0,-1.,120.,0.,0,0};uint64_t superseded=0;uint32_t skipped=0;
    CHECK(astra_rt_live_publish(b,&spec,nullptr,&free_clock,&superseded,&skipped)==0);
    auto play=live_record<AstraLiveCommand>();play.id=904;play.epoch=1;play.sequence=1;play.intent=1;play.op=ASTRA_LIVE_PLAY;
    CHECK(astra_rt_live_submit(b,&play)==0);owner.pump(64,out.data());
    CHECK((mix_context.flags&AV_RT_CLOCK) && !(mix_context.flags&AV_RT_METER));
    CHECK(mix_context.numerator==0 && mix_context.denominator==0 && mix_context.bpm==120.);
    mix.id=902;mix.gain=std::numeric_limits<double>::quiet_NaN();CHECK(astra_rt_live_submit(b,&mix)==ASTRA_RT_INVALID);
    owner.request_retire();owner.join();CHECK(astra_rt_destroy(b)==0);
    c.muted=1;CHECK(astra_rt_live_create(&c,&c,live_constant_process,&b)==0);
    FakeOwner muted_owner(b);muted_owner.pump(64,out.data());for(auto v:out)CHECK(v==0.f);
    muted_owner.request_retire();muted_owner.join();CHECK(astra_rt_destroy(b)==0);
}
}
