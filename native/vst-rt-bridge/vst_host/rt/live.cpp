#include "transport_declick.hpp"
#include "live.hpp"
#include "click_bus.hpp"
#include "pcm_bus.hpp"
#include <algorithm>
#include <cmath>
#include <stdexcept>
#include <tuple>
#include <type_traits>

namespace astra::rt {
namespace {
static_assert(sizeof(AstraLiveConfig)==72 && sizeof(AstraLiveSpec)==56 && sizeof(AstraLiveClock)==40);
static_assert(sizeof(AstraLiveCommand)==136 && sizeof(AstraLiveSnapshot)==152 && sizeof(AstraLiveReceipt)==24);
static_assert(sizeof(AvRtMidiEvent)==12 && sizeof(AvRtContext)==64);
static_assert(std::is_same_v<AstraLiveProcess, decltype(&astra_vst_process_rt)>);
constexpr uint32_t kNone = UINT32_MAX, kDirty = 4, kMask = 3;
constexpr uint32_t kCredit = 128, kReleaseReserve = 16, kAuditions = 128;
constexpr unsigned kControls = 16*130;
// C++ cold-chase only. One bounded tile per hardware callback; future tiles never backdate.
// Fits with full old/new key reconciliation, ordinary authored MIDI and the previous tile's releases.
constexpr uint32_t kDamperTile=512;
// Dedicated instrument bus ceiling: 10^(+6 dB / 20). ABI and other buses unchanged.
constexpr double kVstMidiGainMax = 1.9952623149688795;
constexpr std::array<uint8_t,3> kPedals{64,66,69};
static_assert(2*kVoiceCapacity + kControls + 16*3 + kAuditions + 2*kCredit + kMidiCapacity + 2*kDamperTile <= AV_RT_MIDI_LIMIT);
constexpr uint32_t ring(uint64_t n) noexcept { return static_cast<uint32_t>(n)&(kCredit-1); }

struct Key {
    Sample start;
    uint32_t ordinal;
    uint8_t status, pitch, velocity;
    auto order() const noexcept { return std::tie(start,status,pitch,ordinal,velocity); }
};
struct Note { Key key; Sample end, max_end; uint32_t next = kNone; };
struct DamperTail {Sample off, until;uint32_t note;};
struct Message { Sample sample; uint8_t status, a, b; uint32_t note = kNone; };
constexpr unsigned channel(const Message& e) noexcept { return e.status&15u; }
constexpr unsigned type(const Message& e) noexcept { return e.status&0xf0u; }
constexpr unsigned midi_key(const Message& e) noexcept { return 128*channel(e)+e.a; }
constexpr int controller(const Message& e) noexcept {
    // Channel-mode CCs are delivered, but are actions, not chaseable values.
    return type(e)==0xb0 && e.a<120 ? e.a : type(e)==0xe0 ? 128 : type(e)==0xd0 ? 129 : -1;
}
constexpr bool earlier(const Message& e, Sample x) noexcept { return e.sample < x; }

double beat_at(const AstraLiveClock& c, Sample at, double rate) noexcept {
    return c.beat + static_cast<double>(at-c.sample)*(c.bpm/(60.*rate));
}

template<class T> bool valid_mix(const T& c) noexcept {
    return c.bus_mode<=1 && c.metronome<=1 && (!c.bus_mode ||
       (std::isfinite(c.midi_gain)&&c.midi_gain>=0&&c.midi_gain<=kVstMidiGainMax&&
        std::isfinite(c.audio_gain)&&c.audio_gain>=0&&c.audio_gain<=1&&
        std::isfinite(c.click_gain)&&c.click_gain>=0&&c.click_gain<=1));
}
std::vector<ClickPoint> click_points(const AstraLiveClick* p,uint32_t n) {
    if(n>100000 || (n&&!p))throw std::invalid_argument("invalid click count");
    std::vector<ClickPoint> out;out.reserve(n);
    for(uint32_t i=0;i<n;++i){
        const uint32_t gate=p[i].reserved, threshold=gate & 0xffffu;
        if(p[i].accent>1 || (gate && (!threshold || threshold>30000 || (gate & 0x7fff0000u))))
            throw std::invalid_argument("invalid click point");
        out.push_back({p[i].sample,p[i].accent!=0,gate});
    }
    return out;
}

// This is the plugin tempo-context compatibility range, not a timeline limit.
constexpr double kMaxPluginContextBpm = 1000.;

struct Prepared {
    AstraLiveSpec spec;
    ClickBus click;
    std::vector<Message> events;
    std::vector<Note> notes;
    // Per channel, physical releases and their common next pedal-up are both ordered.
    // Construct/allocate only in Prepared, on the timeline publisher; never in process().
    std::array<std::vector<DamperTail>,16> damper;
    std::vector<AstraLiveClock> clock;
    std::array<std::vector<uint32_t>,kControls> controls;
    uint32_t unsupported = 0;

    Prepared(const AstraLiveSpec& s, const AstraRtEvent* midi, const AstraLiveClock* spans, const AstraLiveClick* clicks, uint32_t click_count)
        : spec(s), click(s.sample_rate,s.duration,click_points(clicks,click_count)), clock(spans, spans+s.clock_count) {
        if (clock.front().sample != 0) throw std::invalid_argument("prepared clock must start at sample zero");
        for (unsigned i=0; i<clock.size(); ++i) {
            const auto& c=clock[i];
            if (c.sample<0 || c.sample>spec.duration || (i && c.sample<=clock[i-1].sample) ||
                !std::isfinite(c.beat) || std::abs(c.beat)>1.e15 || !std::isfinite(c.bar_start_beat) ||
                std::abs(c.bar_start_beat)>1.e15 || (c.numerator && c.bar_start_beat>c.beat+1.e-8) ||
                !std::isfinite(c.bpm) || c.bpm<=0 ||
                ((c.numerator!=0 || c.denominator!=0) &&
                 (c.numerator<1 || c.numerator>1024 || c.denominator<1 || c.denominator>1024)))
                throw std::invalid_argument("invalid prepared clock boundary");
            if (i) {
                const auto& prev=clock[i-1];
                const double predicted=beat_at(prev,c.sample,s.sample_rate);
                // Allow sample quantization of an authoritative boundary, not a tempo ramp.
                const double tolerance=std::max(prev.bpm,c.bpm)/(60.*s.sample_rate)+
                    8.*std::numeric_limits<double>::epsilon()*std::max(1.,std::abs(predicted));
                if (!std::isfinite(predicted) || std::abs(c.beat-predicted)>tolerance)
                    throw std::invalid_argument("clock is not representable by the supplied constant-tempo spans");
            }
        }
        events.reserve(s.event_count);
        for (unsigned i=0; i<s.event_count; ++i) {
            const auto& e=midi[i];
            if (e.sample<0 || e.sample>=s.duration || e.status<0x80 || e.data1>127 || e.data2>127 || e.reserved || e.reserved2)
                throw std::invalid_argument("MIDI event outside prepared timeline or byte range");
            if ((e.status&0xf0)==0xc0 || e.status>=0xf0) { ++unsupported; continue; }
            events.push_back({e.sample,e.status,e.data1,e.data2});
        }
        if (!std::is_sorted(events.begin(),events.end(),[](const auto& a,const auto& b){return a.sample<b.sample;}))
            std::stable_sort(events.begin(),events.end(),[](const auto& a,const auto& b){return a.sample<b.sample;});

        std::array<uint32_t,16*128> head, tail, ordinal{};
        std::array<Sample,16*128> start;
        head.fill(kNone); tail.fill(kNone); start.fill(-1);
        std::array<bool,16> damper_down{};
        std::array<size_t,16> batch_begin{};
        uint32_t held=0, density=0;
        Sample previous=-1;
        for (uint32_t i=0; i<events.size(); ++i) {
            auto& e=events[i];
            if (e.sample!=previous) { previous=e.sample; density=0; }
            if (++density>kMidiCapacity) throw std::invalid_argument("more than 4096 authored MIDI events at one sample");
            const auto k=midi_key(e),ch=channel(e);
            if(type(e)==0xb0 && (e.a==64 || e.a==120 || e.a==121 || e.a==123)) {
                // Channel-mode actions are boundaries: don't recreate voices the plugin may have silenced.
                if(e.a!=64 || e.b<64) {
                    auto& v=damper[ch];for(size_t j=batch_begin[ch];j<v.size();++j)v[j].until=e.sample;
                    batch_begin[ch]=v.size();
                }
                if(e.a==64) damper_down[ch]=e.b>=64;
                else if(e.a==121) damper_down[ch]=false;
            }
            if (type(e)==0x90 && e.b) {
                if (++held>kVoiceCapacity) throw std::invalid_argument("too many simultaneous key-down timeline notes");
                if (start[k]!=e.sample) {start[k]=e.sample;ordinal[k]=0;}
                e.note=static_cast<uint32_t>(notes.size());
                notes.push_back({{e.sample,ordinal[k]++,e.status,e.a,e.b},s.duration,s.duration});
                if (head[k]==kNone) head[k]=e.note; else notes[tail[k]].next=e.note;
                tail[k]=e.note;
            } else if (type(e)==0x80 || type(e)==0x90) {
                e.note=head[k];
                if (e.note!=kNone) {
                    auto& n=notes[e.note];n.end=e.sample;head[k]=n.next;--held;
                    if(damper_down[ch])damper[ch].push_back({e.sample,s.duration,e.note});
                }
            } else if (type(e)==0xa0 && head[k]==tail[k]) {
                e.note=head[k];
            }
            if (const int c=controller(e); c>=0) controls[channel(e)*130+static_cast<unsigned>(c)].push_back(i);
        }
        index_ends(0, static_cast<uint32_t>(notes.size()));
    }

    // An implicit balanced interval tree over start-ordered notes. A seek/edit
    // queries crossing notes instead of replaying the project on the callback.
    Sample index_ends(uint32_t a, uint32_t b) noexcept {
        if (a==b) return -1;
        const auto m=a+(b-a)/2;
        return notes[m].max_end=std::max({notes[m].end,index_ends(a,m),index_ends(m+1,b)});
    }
    template<class Hit> void crossing(uint32_t a, uint32_t b, Sample p, Hit& hit) const noexcept {
        if (a==b || notes[a].key.start>=p) return;
        const auto m=a+(b-a)/2;
        const auto& n=notes[m];
        if (n.max_end<p) return;
        crossing(a,m,p,hit);
        if (n.key.start<p && n.end>=p) hit(m);
        if (n.key.start<p) crossing(m+1,b,p,hit);
    }
    const AstraLiveClock* span(Sample at) const noexcept {
        const auto it=std::upper_bound(clock.begin(),clock.end(),at,
            [](Sample p,const auto& c){return p<c.sample;});
        return &*(it-1);
    }
    double beat(Sample at) const noexcept { return beat_at(*span(at),at,spec.sample_rate); }
};
}

struct Live::Impl {
    const AstraLiveConfig config;
    void* const plugin;
    const AstraLiveProcess process;

    // Reverse-direction triple mailbox. UI owns back; RT owns front. The
    // exchange transfers ownership of the slot and all of its storage. No RT
    // code reads the previous timeline after exchanging front into middle.
    std::array<std::unique_ptr<const Prepared>,3> timelines;
    std::atomic<uint32_t> middle{1};
    uint32_t back=2, front=0;
    const Prepared* now=nullptr;

    // One credit per unresolved or unread outcome, including a deferred Play.
    // Coalescing an unobserved publication returns its credit on UI.
    struct Outcome { uint64_t id; uint32_t result; bool audition; };
    std::array<AstraLiveCommand,kCredit> input{};
    std::array<Outcome,kCredit> output{};
    std::atomic<uint64_t> commands{0}, receipts{0};
    uint64_t ui_write=0, ui_read=0, rt_read=0, rt_write=0;
    uint32_t flight=0;
    uint64_t ui_epoch=0, ui_sequence=0;
    bool ui_timeline=false;
    AstraLiveCommand pending{};
    uint64_t intent=0;

    struct Voice {Key key; uint32_t index; int32_t id;};
    struct Aud {uint64_t token; uint8_t ch, pitch; int32_t id;};
    std::vector<uint32_t> slots = std::vector<uint32_t>(kMaxTimelineEvents,kNone);
    std::array<Voice,kVoiceCapacity> voices{}, kept{};
    std::array<uint32_t,kVoiceCapacity> wanted{};
    std::array<Aud,kAuditions> aud{};
    std::array<std::array<bool,3>,16> pedals{};
    std::array<uint32_t,kControls> chase_order{};
    struct TailOff {uint8_t status,pitch;int32_t id;};
    std::array<TailOff,kDamperTile> tail_off{};
    std::array<uint32_t,16> tail_next{},tail_end{};
    uint32_t tail_off_count=0;
    bool cold_tail=true,tail_backlog=false;
    std::array<AvRtMidiEvent,AV_RT_MIDI_LIMIT> midi{};
    uint32_t key_count=0, aud_count=0, count=0;
    uint32_t next_note_id=0;
    bool reset=false, chase=false, overflow=false;

    double gain=0., effective_gain=0., target_gain=0.;
    bool muted=false, metronome=false;
    double midi_gain=1.,audio_gain=1.,click_gain=1.,midi_effective=1.,click_effective=0.;
    uint32_t ramp_left=0, bus_ramp=0;
    std::vector<float> click_scratch;
    PcmBus pcm;
    bool has_pcm=false, click_reset=true;
    Sample loop_landing=-1;
    ClickBus::Cursor click_cursor{};
    struct Lead { uint64_t step=0; uint32_t count=0,accent=0; Sample frame=0,duration=0,tail=0; };
    Lead lead{};
    // The VST ProcessContext sample counter advances in real output frames
    // between locates; the editor cursor remains on the immutable project grid.
    Sample process_position=0;
    TransportDeclick declick;
    Impl(const AstraLiveConfig& c, void* p, AstraLiveProcess f) : config(c), plugin(p), process(f),
        gain(c.gain), effective_gain(c.muted?0.:c.gain), target_gain(effective_gain), muted(c.muted!=0), metronome(c.metronome!=0),
        midi_gain(c.bus_mode?c.midi_gain:1.),audio_gain(c.bus_mode?c.audio_gain:1.),click_gain(c.bus_mode?c.click_gain:1.),
        midi_effective(midi_gain),click_effective(metronome?click_gain:0.),click_scratch(static_cast<size_t>(c.max_frames)*2,0.f),pcm(c.max_frames),declick(c.sample_rate) {}
    void bind_pcm(const AstraLivePcmSource* s) {
        if(!s){pcm.bind({});has_pcm=false;return;}
        if(s->size!=sizeof(*s)||s->version!=1||s->sample_rate!=config.sample_rate||!s->frames||!s->handle||!s->request||!s->read||!s->hint)
            throw std::invalid_argument("invalid PCM source or sample rate");
        if (applied_rate!=ASTRA_LIVE_RATE_ONE)
            throw std::invalid_argument("VST PCM comparison requires original speed");
        pcm.bind({s->handle,s->request,s->read,s->hint});
        has_pcm=true;
    }
    uint32_t applied_rate=ASTRA_LIVE_RATE_ONE;
    static constexpr auto unit=static_cast<int64_t>(ASTRA_LIVE_RATE_ONE);

    void locate(Snapshot& s,Sample at) noexcept {
        s.render_position=at;
        s.live_subframe=0;
        loop_landing=-1;
        click_reset=true;
        // Avoid overflowing a legal int64 timeline while scaling a locate.
        const auto quotient=at/s.live_rate;
        const auto tail=(at%s.live_rate)*unit/s.live_rate;
        const auto max=std::numeric_limits<Sample>::max();
        process_position=quotient>(max-tail)/unit ? max : quotient*unit+tail;
    }

    static uint32_t before_boundary(const Snapshot& s,Sample boundary,uint32_t n) noexcept {
        const auto ticks=static_cast<int64_t>(s.live_subframe)+n*static_cast<int64_t>(s.live_rate);
        const auto frames=boundary-s.render_position;
        if (frames>ticks/unit) return n;
        return static_cast<uint32_t>((frames*unit-s.live_subframe+s.live_rate-1)/s.live_rate);
    }

    static uint32_t output_offset(const Snapshot& s,Sample at) noexcept {
        const auto ticks=(at-s.render_position)*unit-s.live_subframe;
        return ticks>0 ? static_cast<uint32_t>(ticks/s.live_rate) : 0;
    }

    void mix(Snapshot& s,float* out,uint32_t n,Sample first,Sample before) noexcept {
        std::fill_n(click_scratch.data(),static_cast<size_t>(n)*2,0.f);
        if(now&&s.playing&&(metronome||click_effective!=0.)){
            now->click.add_retimed(click_scratch.data(), n, s.render_position, s.live_subframe, s.live_rate,
                                   first, before, click_cursor, click_reset);
            click_reset=false;
        } else click_reset=true;
        for (uint32_t i=0;i<n;++i) {
            if(bus_ramp){midi_effective+=(midi_gain-midi_effective)/bus_ramp;click_effective+=((metronome?click_gain:0.)-click_effective)/bus_ramp;--bus_ramp;}
            out[2*i]=static_cast<float>(out[2*i]*midi_effective+click_scratch[2*i]*click_effective);
            out[2*i+1]=static_cast<float>(out[2*i+1]*midi_effective+click_scratch[2*i+1]*click_effective);
        }
        if(now && lead.count) {
            now->click.add_count(out,n,lead.frame,lead.step,lead.count,lead.accent,static_cast<float>(click_gain));
            lead.frame+=n;
            if(lead.frame>=std::max(lead.tail,lead.duration))lead.count=0;
        }
        // The legacy, currently hidden 1x-only comparison has no varispeed bus.
        if (has_pcm) pcm.add_to(out,static_cast<uint64_t>(s.render_position),n,s.playing,static_cast<float>(audio_gain));
        if(muted)declick.clear();
        for(uint32_t i=0;i<n;++i){
            if(!muted)declick.frame(out+2*i);
            if(ramp_left){effective_gain+=(target_gain-effective_gain)/ramp_left;--ramp_left;}
            out[2*i]*=static_cast<float>(effective_gain);out[2*i+1]*=static_cast<float>(effective_gain);
        }
        s.live_gain=gain;s.live_muted=muted;s.live_effective_gain=effective_gain;
        s.live_midi_gain=midi_gain;s.live_audio_gain=audio_gain;s.live_click_gain=click_gain;s.live_metronome=metronome;
    }

    int32_t publish(const AstraLiveSpec& s, const AstraRtEvent* e, const AstraLiveClock* c,
                    uint64_t& superseded, uint32_t& unsupported, const AstraLiveClick* clicks, uint32_t click_count) {
        if (!s.id || !s.epoch || s.duration<0 || s.sample_rate!=config.sample_rate ||
            s.event_count>kMaxTimelineEvents || (s.event_count&&!e) || !s.clock_count ||
            s.clock_count>ASTRA_LIVE_MAX_CLOCK || !c)
            throw std::invalid_argument("invalid live timeline size, duration, or sample rate");
        if (s.epoch<ui_epoch || (s.epoch==ui_epoch && ui_timeline && s.sequence<=ui_sequence)) return ASTRA_LIVE_STALE;
        if (flight>=kCredit-kReleaseReserve) return ASTRA_RT_BUSY;
        auto value=std::make_unique<const Prepared>(s,e,c,clicks,click_count);
        unsupported=value->unsupported;
        timelines[back]=std::move(value);
        ++flight;
        const auto previous=middle.exchange(back|kDirty,std::memory_order_acq_rel);
        back=previous&kMask;
        superseded=0;
        if (previous&kDirty) {superseded=timelines[back]->spec.id;--flight;}
        timelines[back].reset(); // both coalesced and rendered storage retire here
        ui_epoch=s.epoch;ui_sequence=s.sequence;ui_timeline=true;
        return ASTRA_RT_OK;
    }
    int32_t submit(const AstraLiveCommand& c) noexcept {
        if (!c.id || (c.op!=ASTRA_LIVE_MIX && !c.epoch) || c.reserved || c.reserved2 || c.reserved3 || c.op>ASTRA_LIVE_SCORE_RATE ||
            (c.op==ASTRA_LIVE_RATE && (c.a<ASTRA_LIVE_RATE_ONE/4 || c.a>2*ASTRA_LIVE_RATE_ONE || c.b)) ||
            (c.op==ASTRA_LIVE_SCORE_RATE && (c.a<ASTRA_LIVE_RATE_ONE/10 || c.a>3*ASTRA_LIVE_RATE_ONE || c.b)) ||
            (c.op==ASTRA_LIVE_MIX && (!std::isfinite(c.gain) || c.gain<0. || c.gain>1. || c.muted>1 || !valid_mix(c))) ||
            (c.op==ASTRA_LIVE_PLAY && (c.a< -1 || c.b)) ||
            (c.op==ASTRA_LIVE_COUNT_PLAY && (c.a< -1 || c.b<=0 || c.b>120*config.sample_rate ||
               !c.channel || c.channel>128 || !c.pitch || c.pitch>64 || c.velocity ||
               c.token<ASTRA_LIVE_RATE_ONE || c.token>40*config.sample_rate*ASTRA_LIVE_RATE_ONE ||
               (c.channel-1)*c.token+ASTRA_LIVE_RATE_ONE/2>=uint64_t(c.b)*ASTRA_LIVE_RATE_ONE+c.token)) ||
            (c.op==ASTRA_LIVE_SEEK && (c.a<0 || c.b)) ||
            (c.op==ASTRA_LIVE_LOOP_ON && (c.a<0 || c.b<=c.a)) ||
            ((c.op==ASTRA_LIVE_ON || c.op==ASTRA_LIVE_OFF) && !c.token) ||
            (c.op==ASTRA_LIVE_ON && (c.channel>=16 || c.pitch>=128 || !c.velocity || c.velocity>=128)))
            return ASTRA_RT_INVALID;
        if (c.op!=ASTRA_LIVE_MIX && ui_epoch && c.epoch!=ui_epoch) return ASTRA_LIVE_STALE;
        if (flight >= (c.op==ASTRA_LIVE_ON ? kCredit-kReleaseReserve : kCredit)) return ASTRA_RT_BUSY;
        if (!ui_epoch && c.op!=ASTRA_LIVE_MIX) ui_epoch=c.epoch;
        ++flight;
        input[ring(ui_write++)]=c;
        commands.store(ui_write,std::memory_order_release);
        return ASTRA_RT_OK;
    }
    bool poll(AstraLiveReceipt& r) noexcept {
        if (ui_read==receipts.load(std::memory_order_acquire)) return false;
        const auto& o=output[ring(ui_read++)];
        r={sizeof(r),ASTRA_LIVE_VERSION,o.result,0,o.id};
        --flight;
        return true;
    }
    void complete(uint64_t id, uint32_t result, bool audition=false) noexcept {
        output[ring(rt_write++)]={id,result,audition};
    }
    void cancel_play(uint32_t result) noexcept {
        if (pending.id) {complete(pending.id,result);pending={};}
    }
    static void state(Snapshot& s, uint32_t v) noexcept {s.live_state=v;s.playing=v==ASTRA_LIVE_PLAYING;}
    void cancel_lead(Snapshot& s) noexcept {
        if(s.live_state==ASTRA_LIVE_COUNTING)state(s,ASTRA_LIVE_PAUSED);
        lead={};
    }
    void fail(Snapshot& s, Fault f, int32_t code=0) noexcept {
        cancel_lead(s);
        declick.clear();
        if (s.fault==Fault::none) {s.fault=f;s.processor_code=code;}
        state(s,ASTRA_LIVE_STOPPED);
    }
    void send(uint32_t off, uint8_t status, uint8_t a, uint8_t b, int32_t id=-1, bool live=false) noexcept {
        if (count==midi.size()) {overflow=true;return;}
        midi[count++]={off,status,a,b,static_cast<uint8_t>(live),id};
        if ((status&0xf0)==0xb0)
            for (unsigned i=0;i<kPedals.size();++i) if (a==kPedals[i]) pedals[status&15u][i]=b!=0;
    }
    int32_t new_note_id() noexcept {
        if (next_note_id==INT32_MAX) {overflow=true;return -1;}
        return static_cast<int32_t>(++next_note_id);
    }
    void release_voice(const Voice& v) noexcept {send(0,static_cast<uint8_t>(v.key.status-0x10),v.key.pitch,0,v.id);}
    void finish_tail_pulses() noexcept {
        for(uint32_t i=0;i<tail_off_count;++i){const auto& v=tail_off[i];send(0,v.status,v.pitch,0,v.id);}
        tail_off_count=0;
    }
    void begin_tail_chase(Sample p) noexcept {
        tail_backlog=false;
        for(unsigned ch=0;ch<16;++ch){
            const auto& v=now->damper[ch];
            tail_next[ch]=static_cast<uint32_t>(std::upper_bound(v.begin(),v.end(),p,
                    [](Sample at,const DamperTail& e){return at<e.until;})-v.begin());
            tail_end[ch]=static_cast<uint32_t>(std::upper_bound(v.begin(),v.end(),p,
                    [](Sample at,const DamperTail& e){return at<e.off;})-v.begin());
            if(tail_next[ch]>tail_end[ch])tail_next[ch]=tail_end[ch];
            tail_backlog|=tail_next[ch]!=tail_end[ch];
        }
    }
    void emit_tail_tile(Sample p) noexcept {
        for(unsigned ch=0;ch<16 && tail_off_count<kDamperTile;++ch){
            const auto& tails=now->damper[ch];auto& i=tail_next[ch];const auto end=tail_end[ch];
            // Cursors came from a single pedal span per channel. If it expired during a huge catch-up,
            // the entire remaining range expired together. Never scan the score on the audio thread.
            if(i<end && tails[i].until<=p){i=end;continue;}
            while(i<end && tail_off_count<kDamperTile){
                const auto& t=tails[i++];const auto& k=now->notes[t.note].key;
                if(k.start>=p)continue; // leave an authored onset at the exact cursor to normal playback
                const auto id=new_note_id();send(0,k.status,k.pitch,k.velocity,id);
                tail_off[tail_off_count++]={static_cast<uint8_t>(k.status-0x10),k.pitch,id};
            }
        }
        tail_backlog=tail_next!=tail_end;
    }
    void reset_timeline_voices() noexcept {
        finish_tail_pulses();tail_next.fill(0);tail_end.fill(0);cold_tail=true;tail_backlog=false;
        for (uint32_t i=0;i<key_count;++i) {release_voice(voices[i]);slots[voices[i].index]=kNone;}
        key_count=0;
        for (uint8_t ch=0;ch<16;++ch)
            for (unsigned i=0;i<kPedals.size();++i)
                if (pedals[ch][i]) send(0,static_cast<uint8_t>(0xb0|ch),kPedals[i],0);
    }
    void release_aud(unsigned i) noexcept {
        const auto a=aud[i];
        send(0,static_cast<uint8_t>(0x80|a.ch),a.pitch,0,a.id,true);
        aud[i]=aud[--aud_count];
    }
    void clear_audition() noexcept {while (aud_count) release_aud(aud_count-1);}
    unsigned apply_audition(const AstraLiveCommand& c) noexcept {
        if (c.op==ASTRA_LIVE_CLEAR) {clear_audition();return ASTRA_LIVE_APPLIED;}
        unsigned i=0;
        while (i<aud_count && aud[i].token!=c.token) ++i;
        if (c.op==ASTRA_LIVE_ON && i==aud_count && aud_count==aud.size()) return ASTRA_LIVE_VOICES;
        if (i<aud_count) release_aud(i);
        if (c.op==ASTRA_LIVE_ON) {
            const auto id=new_note_id();
            aud[aud_count++]={c.token,static_cast<uint8_t>(c.channel),static_cast<uint8_t>(c.pitch),id};
            send(0,static_cast<uint8_t>(0x90|c.channel),static_cast<uint8_t>(c.pitch),static_cast<uint8_t>(c.velocity),id,true);
        }
        return ASTRA_LIVE_APPLIED;
    }

    void adopt(Snapshot& s) noexcept {
        if (!(middle.load(std::memory_order_acquire)&kDirty)) return;
        cancel_lead(s);
        front=middle.exchange(front,std::memory_order_acq_rel)&kMask;
        now=timelines[front].get();
        click_reset=true;
        // An edit may replace/recycle the previous Prepared. Do not reuse its indices or retrigger sounding tails.
        tail_next.fill(0);tail_end.fill(0);tail_backlog=false;
        if (s.live_epoch!=now->spec.epoch) {
            declick.clear();
            cancel_play(ASTRA_LIVE_OBSOLETE);
            clear_audition();
            s.live_rate=applied_rate=ASTRA_LIVE_RATE_ONE;
            locate(s,0);s.loop=false;s.loop_begin=s.loop_end=0;
            intent=s.live_intent=0;state(s,ASTRA_LIVE_STOPPED);reset=true;
        }
        s.live_epoch=now->spec.epoch;s.live_sequence=now->spec.sequence;s.live_timeline=true;
        if (s.loop && s.loop_end>now->spec.duration) {s.loop=false;s.loop_begin=s.loop_end=0;}
        if (s.render_position>=now->spec.duration) {
            if(s.playing)declick.arm();
            locate(s,now->spec.duration);state(s,ASTRA_LIVE_STOPPED);reset=true;
        }
        chase=true;
        complete(now->spec.id,ASTRA_LIVE_APPLIED);
    }
    bool in_range(const Snapshot& s,Sample at) const noexcept {
        return now && at<=now->spec.duration && (!s.loop || (at>=s.loop_begin && at<s.loop_end));
    }
    unsigned play(Snapshot& s,const AstraLiveCommand& c) noexcept {
        if (!now->spec.duration || (c.a>=0 && (!in_range(s,c.a) || c.a==now->spec.duration))) return ASTRA_LIVE_RANGE;
        if(!s.playing || (c.a>=0&&(s.render_position!=c.a || s.live_subframe)))declick.arm();
        if (c.a>=0 && (s.render_position!=c.a || s.live_subframe)) {locate(s,c.a);reset=true;}
        if (s.render_position==now->spec.duration) {locate(s,s.loop?s.loop_begin:0);reset=true;}
        if (!s.playing || reset) chase=true;
        if(c.op==ASTRA_LIVE_COUNT_PLAY){
            if(s.playing)reset=true;
            const auto last=((c.channel-1)*c.token+ASTRA_LIVE_RATE_ONE/2)/ASTRA_LIVE_RATE_ONE;
            lead={c.token,c.channel,c.pitch,0,c.b,static_cast<Sample>(last)+now->click.wave_frames()};
            state(s,ASTRA_LIVE_COUNTING);
        } else state(s,ASTRA_LIVE_PLAYING);
        return ASTRA_LIVE_APPLIED;
    }
    void apply(Snapshot& s,const AstraLiveCommand& c) noexcept {
        if (c.op==ASTRA_LIVE_MIX) {
            gain=c.gain;muted=c.muted!=0;target_gain=muted?0.:gain;
            midi_gain=c.bus_mode?c.midi_gain:1.;audio_gain=c.bus_mode?c.audio_gain:1.;click_gain=c.bus_mode?c.click_gain:1.;metronome=c.metronome!=0;
            bus_ramp=std::max(1u,static_cast<uint32_t>(config.sample_rate*.005));
            ramp_left=std::max(1u,static_cast<uint32_t>(config.sample_rate*.005));
            complete(c.id,ASTRA_LIVE_APPLIED);return;
        }
        if (!s.live_epoch) s.live_epoch=c.epoch;
        if (c.epoch!=s.live_epoch) {complete(c.id,ASTRA_LIVE_OBSOLETE);return;}
        if (c.op>=ASTRA_LIVE_ON && c.op<=ASTRA_LIVE_CLEAR) {complete(c.id,apply_audition(c),true);return;}
        if (c.intent<intent) {complete(c.id,ASTRA_LIVE_OBSOLETE);return;}
        if (c.op==ASTRA_LIVE_RATE || c.op==ASTRA_LIVE_SCORE_RATE) {
            if(s.live_state==ASTRA_LIVE_COUNTING)cancel_lead(s);
            if (has_pcm && c.a!=ASTRA_LIVE_RATE_ONE) {complete(c.id,ASTRA_LIVE_RANGE);return;}
            // A live rate change is not a seek, reset, new clock, or play/pause.
            s.live_rate=applied_rate=static_cast<uint32_t>(c.a);
            intent=s.live_intent=c.intent;
            complete(c.id,ASTRA_LIVE_APPLIED);return;
        }
        if (pending.id && (c.intent>pending.intent || c.op<=ASTRA_LIVE_STOP)) cancel_play(ASTRA_LIVE_OBSOLETE);
        intent=c.intent;
        const auto previous_position=s.render_position;const bool previous_playing=s.playing;
        unsigned result=ASTRA_LIVE_APPLIED;
        // Latest transport command fences pending lead-in and any unrendered
        // click tail. Mix/audition are handled earlier and do not intervene.
        cancel_lead(s);
        switch (c.op) {
        case ASTRA_LIVE_COUNT_PLAY:
        case ASTRA_LIVE_PLAY:
            if (!now || c.sequence>s.live_sequence) {pending=c;return;}
            result=c.sequence==s.live_sequence?play(s,c):ASTRA_LIVE_OBSOLETE;
            break;
        case ASTRA_LIVE_PAUSE:
            if (s.playing) reset=true;
            state(s,ASTRA_LIVE_PAUSED);break;
        case ASTRA_LIVE_STOP:
            locate(s,s.loop?s.loop_begin:0);state(s,ASTRA_LIVE_STOPPED);reset=true;break;
        case ASTRA_LIVE_SEEK:
            if (!now) {result=ASTRA_LIVE_NO_TIMELINE;break;}
            if (!in_range(s,c.a)) {result=ASTRA_LIVE_RANGE;break;}
            locate(s,c.a);reset=chase=true;
            if (s.playing && c.a==now->spec.duration) state(s,ASTRA_LIVE_STOPPED);
            break;
        case ASTRA_LIVE_LOOP_ON:
            if (!now) {result=ASTRA_LIVE_NO_TIMELINE;break;}
            if (c.b>now->spec.duration) {result=ASTRA_LIVE_RANGE;break;}
            s.loop=true;s.loop_begin=c.a;s.loop_end=c.b;
            if (s.render_position<c.a || s.render_position>=c.b) {locate(s,c.a);reset=chase=true;}
            else if (loop_landing>=0) loop_landing=std::max(loop_landing,c.a);
            break;
        case ASTRA_LIVE_LOOP_OFF:s.loop=false;s.loop_begin=s.loop_end=0;break;
        default:break;
        }
        if (result==ASTRA_LIVE_APPLIED) {
            if(previous_position!=s.render_position || previous_playing!=s.playing || (c.op==ASTRA_LIVE_SEEK&&s.playing))declick.arm();
            s.live_intent=c.intent;
        }
        complete(c.id,result);
    }
    void finish_pending_play(Snapshot& s) noexcept {
        if (!pending.id || !now || pending.sequence>s.live_sequence) return;
        const auto r=pending.sequence==s.live_sequence?play(s,pending):ASTRA_LIVE_OBSOLETE;
        if (r==ASTRA_LIVE_APPLIED) s.live_intent=std::max(s.live_intent,pending.intent);
        cancel_play(r);
    }

    void add_voice(uint32_t index,uint32_t offset) noexcept {
        if (key_count==voices.size()) {overflow=true;return;}
        const auto& k=now->notes[index].key;
        const auto id=new_note_id();
        slots[index]=key_count;voices[key_count++]={k,index,id};
        send(offset,k.status,k.pitch,k.velocity,id);
    }
    void reconcile_notes(Sample p) noexcept {
        uint32_t n=0;
        auto record=[&](uint32_t i) {if (n<wanted.size()) wanted[n++]=i;else overflow=true;};
        now->crossing(0,static_cast<uint32_t>(now->notes.size()),p,record);
        for (uint32_t i=0;i<key_count;++i) slots[voices[i].index]=kNone;
        std::sort(voices.begin(),voices.begin()+key_count,[](const Voice& a,const Voice& b){return a.key.order()<b.key.order();});
        std::sort(wanted.begin(),wanted.begin()+n,[&](uint32_t a,uint32_t b){return now->notes[a].key.order()<now->notes[b].key.order();});
        uint32_t i=0, j=0, m=0;
        for (;i<key_count;++i) {
            const auto& v=voices[i];
            while (j<n && now->notes[wanted[j]].key.order()<v.key.order()) ++j;
            if (j<n && now->notes[wanted[j]].key.order()==v.key.order()) {
                kept[m]={v.key,wanted[j],v.id};slots[wanted[j++]]=m++;
            } else release_voice(v);
        }
        std::copy_n(kept.begin(),m,voices.begin());key_count=m;
        for (j=0;j<n;++j) {
            const auto k=wanted[j];
            // At an authored off boundary preserve a delivered ID, but don't
            // manufacture an onset for a note that ends at this sample.
            if (slots[k]==kNone && now->notes[k].end>p) add_voice(k,0);
        }
    }
    void chase_controls(Sample p) noexcept {
        unsigned n=0;
        for (unsigned i=0;i<kControls;++i) {
            const auto& list=now->controls[i];
            const auto it=std::lower_bound(list.begin(),list.end(),p,
                [&](uint32_t j,Sample at){return now->events[j].sample<at;});
            if (it!=list.begin()) chase_order[n++]=*(it-1);
            else for (unsigned j=0;j<kPedals.size();++j)
                if (i%130==kPedals[j] && pedals[i/130][j]) send(0,static_cast<uint8_t>(0xb0|i/130),kPedals[j],0);
        }
        // Distinct CCs may map to one ParamID. Replay last authored values in
        // original chronological order, not controller-number order.
        std::sort(chase_order.begin(),chase_order.begin()+n);
        for (unsigned i=0;i<n;++i) {
            const auto& e=now->events[chase_order[i]];
            send(0,e.status,e.a,e.b);
        }
    }
    void authored(const Message& e,uint32_t off) noexcept {
        if (type(e)==0x90 && e.b) {add_voice(e.note,off);return;}
        const auto i=e.note==kNone?kNone:slots[e.note];
        if (type(e)==0x80 || type(e)==0x90) {
            // No anonymous off: it could release a different (audition) voice.
            if (i==kNone) return;
            send(off,e.status,e.a,e.b,voices[i].id);
            slots[e.note]=kNone;
            if (i!=--key_count) {voices[i]=voices[key_count];slots[voices[i].index]=i;}
        } else send(off,e.status,e.a,e.b,i==kNone?-1:voices[i].id);
    }

    AvRtContext context(const Snapshot& s, bool playing) const noexcept {
        AvRtContext x{};x.sample_position=process_position;
        if (playing) x.flags|=AV_RT_PLAYING;
        if (!now) return x;
        const auto& c=*now->span(s.render_position);
        // Keep the exact clock for project math, but don't feed an out-of-range
        // local editing span to arbitrary plugins as a trusted sync context.
        // Frame-timed events and clicks continue; normal flags resume next span.
        if (c.bpm > kMaxPluginContextBpm) return x;
        x.flags|=AV_RT_CLOCK;
        x.beat=beat_at(c,s.render_position,config.sample_rate)+s.live_subframe*c.bpm/(unit*60.*config.sample_rate);
        x.bpm=c.bpm*s.live_rate/unit;
        x.numerator=c.numerator;x.denominator=c.denominator;
        if (c.numerator && c.denominator) {
            x.flags|=AV_RT_METER;
            const double measure=4.*c.numerator/c.denominator;
            x.bar_start_beat=c.bar_start_beat+measure*std::floor(std::max(0.,(x.beat-c.bar_start_beat)/measure)+1.e-10);
        }
        if (s.loop) {
            x.cycle_start_beat=now->beat(s.loop_begin);x.cycle_end_beat=now->beat(s.loop_end);
            if (x.cycle_end_beat>x.cycle_start_beat) x.flags|=AV_RT_CYCLE;
        }
        return x;
    }
    int32_t dispatch(Snapshot& s,float* out,uint32_t n,bool playing) noexcept {
        if (s.process_calls!=UINT64_MAX) ++s.process_calls;
        const auto x=context(s,playing);
        try {return process(plugin,out,n,count?midi.data():nullptr,count,&x);}
        catch (...) {return ASTRA_RT_PROCESS_EXCEPTION;}
    }
    void park(Snapshot& s) noexcept {
        cancel_lead(s);
        declick.clear();
        count=0;overflow=false;
        if (s.fault==Fault::none) {
            reset_timeline_voices();clear_audition();
            if (count) {
                float discarded[2]{};
                if (const auto rc=dispatch(s,discarded,1,false)) fail(s,Fault::reset,rc);
            }
        }
        chase=true;reset=false;click_reset=true;
        // Kernel publishes the exact acknowledgement after this returns. Do
        // not change the wanted transport state merely because DSP is parked.
    }
    void render(Snapshot& s,float* out,uint32_t frames,bool usable) noexcept {
        if (!usable) {fail(s,Fault::callback_buffer);return;}
        if (!frames || s.fault!=Fault::none) return;
        count=0;overflow=false;
        const auto first_outcome=rt_write;
        // A command submitted after a publish cannot overtake that mailbox
        // publication. It can observe a later, coalesced timeline instead.
        const auto frontier=commands.load(std::memory_order_acquire);
        adopt(s);
        while (rt_read!=frontier) {const auto c=input[ring(rt_read++)];apply(s,c);}
        finish_pending_play(s);

        uint32_t written=0,calls=0,budget=4*AV_RT_MIDI_LIMIT;
        bool tile_available=true;
        while (written<frames) {
            if (++calls>kMaxMusicCalls) {fail(s,Fault::work_budget);break;}
            finish_tail_pulses(); // previous packet was rendered. Pair each ID before any new chase/ons.
            if (reset) {reset_timeline_voices();reset=false;}
            const Sample p=s.render_position;
            // At fractional positions, an authored event at the preceding
            // integer sample was already delivered. After a wrap, events at L
            // still belong to this first frame, even with a fractional carry.
            const Sample begin=loop_landing>=0?loop_landing:p+(s.live_subframe!=0);
            if (chase && s.playing) {
                chase_controls(begin);
                if(cold_tail){begin_tail_chase(begin);cold_tail=false;}
            }
            if(s.playing && tile_available && tail_backlog){emit_tail_tile(begin);tile_available=false;}
            if(chase && s.playing)reconcile_notes(begin);
            chase=false;
            const bool counting=s.live_state==ASTRA_LIVE_COUNTING;
            auto n=std::min(frames-written,config.max_frames);
            if(counting)n=static_cast<uint32_t>(std::min<Sample>(n,lead.duration-lead.frame));
            if(tail_off_count)n=1; // physical key is already up: defer its paired release by exactly one sample
            Sample end=0,before=0;
            int64_t advance=0;
            if (s.playing) {
                end=s.loop?s.loop_end:now->spec.duration;
                auto boundary=end;
                const auto* next=now->span(p)+1;
                if (next!=now->clock.data()+now->clock.size()) boundary=std::min(boundary,next->sample);
                if (boundary<=p) {fail(s,Fault::work_budget);break;}
                n=before_boundary(s,boundary,n);
                const auto limit=[&]() {
                    advance=static_cast<int64_t>(n)*s.live_rate+s.live_subframe;
                    return p+std::min<Sample>(end-p,(advance+unit-1)/unit);
                };
                before=limit();
                const auto first=std::lower_bound(now->events.begin(),now->events.end(),begin,earlier);
                auto last=std::lower_bound(first,now->events.end(),before,earlier);
                if (last-first>kMidiCapacity) {
                    // Split on an output frame. Events sharing that frame
                    // must remain together in authored order.
                    const Sample split=(first+kMidiCapacity)->sample;
                    n=output_offset(s,split);
                    if (!n) {fail(s,Fault::event_capacity);break;}
                    before=limit();
                    last=std::lower_bound(first,last,before,earlier);
                }
                for (auto it=first;it!=last;++it) authored(*it,output_offset(s,it->sample));
            }
            if (overflow) {fail(s,Fault::voice_capacity);break;}
            if (count>budget) {fail(s,Fault::work_budget);break;}
            budget-=count;
            auto* target=out+static_cast<std::size_t>(written)*2;
            if (const auto rc=dispatch(s,target,n,s.playing)) {
                std::fill_n(target,static_cast<std::size_t>(n)*2,0.f);
                fail(s,Fault::processor,rc);break;
            }
            mix(s,target,n,begin,before);
            count=0;written+=n;
            if(counting && lead.frame>=lead.duration) {
                state(s,ASTRA_LIVE_PLAYING);
                chase=true;
                // Next sub-block of this same device callback is music frame
                // zero. The last click's finite tail remains in the mix.
                continue;
            }
            if (s.playing) {
                s.music_frames+=std::min<uint64_t>(n,UINT64_MAX-s.music_frames);
                process_position+=std::min<Sample>(n,std::numeric_limits<Sample>::max()-process_position);
                loop_landing=-1;
                if (advance/unit>=end-p) {
                    declick.arm();
                    reset=chase=click_reset=true;
                    if (s.loop) {
                        auto carry=advance-(end-p)*unit;
                        const auto length=s.loop_end-s.loop_begin;
                        if (carry/unit>=length) carry%=length*unit;
                        locate(s,s.loop_begin);
                        s.render_position+=carry/unit;
                        s.live_subframe=static_cast<uint32_t>(carry%unit);
                        loop_landing=s.loop_begin;
                    } else {
                        s.render_position=end;s.live_subframe=0;
                        state(s,ASTRA_LIVE_STOPPED);
                    }
                } else {
                    s.render_position=p+advance/unit;
                    s.live_subframe=static_cast<uint32_t>(advance%unit);
                }
            }
        }
        if (!written) {
            // The first block carries the audition prefix. Do not report
            // successful delivery if that block was rejected by the processor.
            for (auto i=first_outcome;i!=rt_write;++i) {
                auto& o=output[ring(i)];
                if (o.audition && o.result==ASTRA_LIVE_APPLIED) o.result=ASTRA_LIVE_FAULTED;
            }
        }
        receipts.store(rt_write,std::memory_order_release);
    }
};

Live::Live(const AstraLiveConfig& c,void* p,AstraLiveProcess f) {
    if (!std::isfinite(c.sample_rate) || c.sample_rate<8000 || c.sample_rate>384000 ||
        !c.max_frames || c.max_frames>kMaxPluginFrames || c.reserved || c.reserved2 ||
        !std::isfinite(c.gain) || c.gain<0. || c.gain>1. || c.muted>1 || !valid_mix(c))
        throw std::invalid_argument("invalid live processing configuration");
    p_=std::make_unique<Impl>(c,p,f);
}
Live::~Live()=default;
uint32_t Live::max_frames() const noexcept {return p_->config.max_frames;}
int32_t Live::publish(const AstraLiveSpec& s,const AstraRtEvent* e,const AstraLiveClock* c,uint64_t& id,uint32_t& skipped,const AstraLiveClick* clicks,uint32_t click_count) {
    return p_->publish(s,e,c,id,skipped,clicks,click_count);
}
int32_t Live::submit(const AstraLiveCommand& c) noexcept {return p_->submit(c);}
bool Live::poll(AstraLiveReceipt& r) noexcept {return p_->poll(r);}
void Live::bind_pcm(const AstraLivePcmSource* s) {p_->bind_pcm(s);}
void Live::park(Snapshot& s) noexcept {p_->park(s);}
void Live::render(Snapshot& s,float* out,uint32_t n,bool usable) noexcept {p_->render(s,out,n,usable);}
}
