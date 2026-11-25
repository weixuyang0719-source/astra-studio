#include "astra_document.h"
#include "native_json.h"
#include "lossless_json.h"
#include "timing_internal.h"
#include "pulse_commands.h"
#include "persistent_table.h"
#include "range_projection.h"

#include <algorithm>
#include <array>
#include <atomic>
#include <charconv>
#include <cstring>
#include <limits>
#include <map>
#include <memory>
#include <set>
#include <system_error>

namespace astra::editor::document_detail {
namespace t = timing_detail;
using J = Json;
using K = uint64_t;
constexpr K limit = UINT64_MAX;
constexpr const char* groups[] = {"notes", "controls"};
constexpr char lane_revision[] = "event-projection";
constexpr auto stale_message = "工程版本已变化，请使用当前版本重新操作；旧修改未覆盖新内容。";
using t::get;
using t::required;
using t::text_is;

struct Fault { int32_t code; const char* detail; };
void need(bool ok, const char* why, int32_t code = AEC_INVALID) {
    if (!ok) throw Fault{code, why};
}
void core(int32_t rc) {
    if (rc != AEC_OK) throw Fault{rc, "事件索引拒绝了此操作。"};
}
std::string_view span(aec_bytes b) {
    need(b.count <= static_cast<size_t>(PTRDIFF_MAX), "输入过大。", AEC_LIMIT);
    need(b.count == 0 || b.data, "无效的输入缓冲区。");
    return {b.count ? b.data : "", b.count};
}
aec_bytes bytes(std::string_view s) { return {s.data(), s.size()}; }

// The public SAX codec preserves overflowing integer tokens as private atoms.
J parse(aec_bytes b) {
    const auto s = span(b);
    need(!s.empty() && s.find('\0') == s.npos, "Expected UTF-8 JSON");
    return lossless::parse(s);
}
void finite_tree(const J& j) {
    if (j.is_number_float()) need(std::isfinite(j.get<double>()), "不能输出非有限数值。");
    if (j.is_structured()) for (const auto& v : j) finite_tree(v);
}
void emit(aed_buffer& out, const J& j) {
    finite_tree(j);
    const auto data = lossless::dump(j);
    auto copy = std::make_unique<char[]>(data.size());
    std::memcpy(copy.get(), data.data(), data.size());
    out = {copy.release(), data.size()};
}
int32_t error(aed_buffer& out, int32_t code, const char* what) noexcept {
    delete[] out.data;
    out = {};
    try {
        const auto data = J{{"error", what}}.dump(-1, ' ', false, J::error_handler_t::replace);
        auto copy = std::make_unique<char[]>(data.size());
        std::memcpy(copy.get(), data.data(), data.size());
        out = {copy.release(), data.size()};
    } catch (...) {}
    return code;
}
template<class F> int32_t boundary(aed_buffer* out, F f) noexcept {
    if (!out) return AEC_INVALID;
    *out = {};
    try { f(*out); return AEC_OK; }
    catch (const Fault& e) { return error(*out, e.code, e.detail); }
    catch (const std::bad_alloc&) { delete[] out->data; *out = {}; return AEC_NOMEM; }
    catch (const J::exception& e) { return error(*out, AEC_INVALID, e.what()); }
    catch (const std::invalid_argument& e) { return error(*out, AEC_INVALID, e.what()); }
    catch (const std::length_error& e) { return error(*out, AEC_LIMIT, e.what()); }
    catch (...) { return error(*out, AEC_INTERNAL, "Native document error"); }
}

bool scalar_id(const J& v) { return lossless::is_integer(v) || v.is_null() || v.is_string() || v.is_number() || v.is_boolean(); }
std::string id_key(const J& v) {
    if (lossless::is_integer(v)) return lossless::integer_text(v);
    need(scalar_id(v), "无效的事件标识。");
    if (v.is_boolean()) return v.get<bool>() ? "1" : "0";
    if (v.is_number_float()) {
        const auto x = v.get<double>();
        if (std::trunc(x) == x && x >= -0x1p63 && x < 0x1p64) {
            if (x < 0) return std::to_string(static_cast<int64_t>(x));
            return std::to_string(static_cast<K>(x));
        }
    }
    if (v.is_number_float() && std::trunc(v.get<double>())==v.get<double>()) return lossless::float_integer_text(v.get<double>());
    return v.dump(); // strings are quoted; no alias with numeric IDs
}
bool py_equal(const J& a, const J& b) {
    if (lossless::is_integer(a) || lossless::is_integer(b)) return lossless::numeric_equal(a,b);
    if (a.is_object() && b.is_object()) {
        if (a.size() != b.size()) return false;
        for (auto i = a.begin(); i != a.end(); ++i) {
            const auto j = b.find(i.key());
            if (j == b.end() || !py_equal(*i, *j)) return false;
        }
        return true;
    }
    if (a.is_array() && b.is_array()) {
        if (a.size() != b.size()) return false;
        for (size_t i = 0; i < a.size(); ++i) if (!py_equal(a[i], b[i])) return false;
        return true;
    }
    if (!scalar_id(a) || !scalar_id(b)) return false;
    if ((a.is_number() || a.is_boolean()) && (b.is_number() || b.is_boolean())) {
        if (a.is_number_float() && b.is_number_float()) return a.get<double>() == b.get<double>();
        return id_key(a) == id_key(b);
    }
    return a.type() == b.type() && a == b;
}
const std::string& revision(const J& j) {
    const auto& r = t::text(j, "无效的编辑版本标识。");
    need(!r.empty() && r != "." && r != ".." && r.find_first_of("/\\:") == r.npos &&
         r.find('\0') == r.npos, "无效的编辑版本标识。");
    return r;
}
std::string fresh_revision(const J& packet) {
    auto r = revision(required(packet, "proposed_revision"));
    need(r.size() == 32 && r.find_first_not_of("0123456789abcdef") == r.npos,
         "新版本需要 32 位小写十六进制标识。");
    return r;
}
double sec(const J& v) {
    const double x = t::number(v, t::Num::edit, "请输入有效数值。");
    need(x >= 0, "时间和数值必须有限且不小于允许的最小值。");
    return x;
}
int midi(const J& v, int lo, int hi, const char* why) {
    need(v.is_number_integer(), why);
    return t::bounded_int(v, lo, hi, why);
}
int ppq(const J& v) { return midi(v, 9600, 9600, "无效的 MIDI PPQ。"); }
const J& list(const J& j, const char* k) { return t::array(get(j, k, t::empty_array())); }

// The header's key order is durable. Copying it shares immutable values,
// including arbitrarily large tempo, non-note streams and vendor metadata.
// Three array positions are occupied by the event store and tombstone log.
struct Header {
    std::vector<std::pair<std::string, std::shared_ptr<const J>>> fields;
    const J& at(std::string_view k) const {
        for (const auto& f : fields) if (f.first == k) return f.second ? *f.second : t::nil();
        return t::nil();
    }
    bool has(std::string_view k) const {
        for (const auto& f : fields) if (f.first == k) return true;
        return false;
    }
    void set(const char* k, J v) {
        auto value = std::make_shared<const J>(std::move(v));
        for (auto& f : fields) if (f.first == k) { f.second = std::move(value); return; }
        fields.emplace_back(k, std::move(value));
    }
};
void retire(J& body) {
    const J old = get(body, "anchors");
    if (t::truth(old)) {
        t::array(old);
        if (!body.contains("legacy_fixed_points")) body["legacy_fixed_points"] = J::array();
        auto& archive = body.at("legacy_fixed_points");
        t::array(archive);
        for (const auto& p : old) {
            if (std::none_of(archive.begin(), archive.end(), [&](const J& q) { return py_equal(p,q); }))
                archive.push_back(p);
        }
    }
    body["anchors"] = J::array();
}
bool view_key(std::string_view k) {
    for (const char* key : {"can_undo", "can_redo", "dirty", "score_stale", "exported_revision", "path",
                            "preview_path", "preview_revision", "preview_pending", "preview_error"})
        if (k == key) return true;
    return false;
}
struct Slot { J json; std::string ident; bool cc_tag = false; };
struct Group {
    Table<K,Slot> rows; // monotonically assigned key; iteration is performance order
    Table<std::string,K> ids;
    K next = 1;
};
K history_u64(const J& value) {
    need(value.is_number_unsigned(),"Invalid unsigned history integer");
    return value.get<K>();
}
bool disk_equal(const J& a,const J& b) {
    // Use the durable codec, not Python numeric equality: integer versus
    // floating tokens, negative zero and object key order are observable here.
    return &a==&b || lossless::dump(a)==lossless::dump(b);
}
void put_slot(Group& g,K key,J row,bool tag=false) {
    t::object(row);
    const auto id=id_key(required(row,"id"));
    need(!g.ids.count(id),"事件标识不能重复。",AEC_DUPLICATE);
    g.ids.set(id,key);
    g.rows.set(key,Slot{std::move(row),id,tag});
}
Group group_from(J rows,const Group* prior=nullptr,const J* layout=nullptr) {
    t::array(rows);
    if (layout) {
        t::array(*layout);
        need(layout->size()==2,"Invalid head row layout");
        Group g;
        g.next=history_u64(layout->at(0));
        const auto& keys=t::array(layout->at(1));
        need(g.next!=0 && keys.size()==rows.size(),"Invalid head row layout size");
        K previous=0;
        size_t i=0;
        for (auto& row:rows) {
            const auto& item=t::array(keys.at(i++));
            need(item.size()==2 && item.at(1).is_boolean(),"Invalid head row key");
            const K key=history_u64(item.at(0));
            need(key>previous && key<g.next,"Unordered head row keys");
            put_slot(g,key,std::move(row),item.at(1).get<bool>());
            previous=key;
        }
        return g;
    }
    if (prior) {
        // Legacy versions are streamed oldest to newest. Ordinary updates,
        // deletes and appends keep keys and share rows. A reordered list is a
        // legitimate replacement and is instead keyed in its supplied order.
        std::vector<K> keys;
        keys.reserve(rows.size());
        K next=prior->next, previous=0;
        for (const auto& row:rows) {
            t::object(row);
            const auto id=id_key(required(row,"id"));
            const auto* old=prior->ids.find(id);
            if (!old && next==limit) break;
            const K key=old?*old:next++;
            if (key<=previous) break;
            keys.push_back(key);
            previous=key;
        }
        if (keys.size()==rows.size()) {
            auto g=*prior;
            for (const auto& old:prior->rows) {
                if (std::binary_search(keys.begin(),keys.end(),old.first)) continue;
                g.ids.erase(old.second.ident);
                g.rows.erase(old.first);
            }
            for (size_t i=0;i<keys.size();++i) {
                const auto* old=g.rows.find(keys[i]);
                if (old && disk_equal(old->json,rows.at(i))) continue;
                if (old) g.ids.erase(old->ident);
                put_slot(g,keys[i],std::move(rows.at(i)));
            }
            g.next=next;
            return g;
        }
    }
    Group g;
    for (auto& row:rows) {
        need(g.next!=limit,"事件过多。",AEC_LIMIT);
        const auto key=g.next++;
        put_slot(g,key,std::move(row));
    }
    return g;
}

struct Lane {
    std::unique_ptr<aec_session, decltype(&aec_free_session)> h{nullptr,aec_free_session};
    K attempt = 0; // private aec fence, not a Document request, ID or revision
};
struct Env {
    t::Curve curve;
    double duration;
    aec_bounds bounds;
    std::optional<std::string> sha; // session-thread cache; snapshot readers never access it
};
struct Document {
    Header header;
    std::array<Group,2> events;
    Table<K,J> deleted;
    Table<std::string,K> tombs; // multiplicity, including duplicate legacy tombstones
    std::shared_ptr<Env> env;
    std::shared_ptr<Lane> lane;
};
K tick_label(const t::Curve& c, double seconds, int resolution) {
    // A bucket label is only tested for equality by aec. Encoding the rounded
    // binary64 instead of converting to uint64 also handles signed/large ticks.
    static_assert(sizeof(double) == sizeof(K) && std::numeric_limits<double>::is_iec559);
    double x = t::round_even(t::finite(t::timing_query(c,t::InputAxis::seconds,seconds)*resolution));
    if (x == 0.) x = 0.;
    K label;
    std::memcpy(&label,&x,sizeof(label));
    return label;
}
aec_row row_for(const Slot& p, K key, unsigned g, const Env& e, int resolution) {
    const auto& r = p.json;
    aec_row out{};
    out.key = key;
    out.channel = midi(get(r,"channel",J(0)),0,15,"MIDI 通道应为 0–15。");
    if (g == AEC_NOTES) {
        out.time = sec(required(r,"start"));
        out.end = sec(required(r,"end"));
        out.data1 = midi(required(r,"pitch"),0,127,"音符需要有效音高与正时长。");
        out.data2 = midi(required(r,"velocity"),1,127,"音符力度应为 1–127。");
        need(out.end > out.time,"音符需要有效音高与正时长。");
        need(!(e.bounds.flags&AEC_OPENING) || ((required(r,"start").is_number() || lossless::is_integer(required(r,"start"))) && out.time >= e.bounds.opening),
             "首完整小节前存在音符；请改用弱起/前奏或调整开头位置，未丢弃任何音符");
        need(!(e.bounds.flags&AEC_CLOSING) || out.time < e.bounds.closing,
             "谱面终点处或之后仍有音符起音，请后移或取消终点；不会隐藏后续音符。");
    } else {
        out.time = sec(required(r,"seconds"));
        out.data1 = midi(required(r,"number"),0,127,"控制器编号和数值应为 0–127。");
        out.data2 = midi(required(r,"value"),0,127,"控制器编号和数值应为 0–127。");
        if (out.data1 == 64) {
            out.tick = tick_label(e.curve,out.time,resolution);
            // aec only needs the equality transition of the value, not its
            // payload. This tag changes iff Python's incoming predicate does.
            // In particular "2.0" -> 2.0 must normalize even at equal doubles.
            out.data2 = p.cc_tag ? 1 : 0;
        }
    }
    need((g == AEC_NOTES ? out.end : out.time) <= e.bounds.extent,
         "编辑事件超出曲目终点；请缩短音符或控制器位置。");
    return out;
}
void append_deleted(Document& d,J id) {
    need(d.deleted.size()!=limit,"Too many tombstones",AEC_LIMIT);
    if (scalar_id(id)) {
        const auto key=id_key(id);
        const auto* n=d.tombs.find(key);
        d.tombs.set(key,n?*n+1:1);
    }
    d.deleted.set(d.deleted.size(),std::move(id));
}
void truncate_deleted(Document& d,size_t size) {
    while (d.deleted.size()>size) {
        const K last=d.deleted.size()-1;
        const auto& id=d.deleted.at(last);
        if (scalar_id(id)) {
            const auto key=id_key(id);
            const auto n=d.tombs.at(key);
            if (n==1) d.tombs.erase(key);
            else d.tombs.set(key,n-1);
        }
        d.deleted.erase(last);
    }
}
size_t deleted_prefix(const Document& a,const Document& b) {
    auto i=a.deleted.begin(), j=b.deleted.begin();
    const auto ae=a.deleted.end(), be=b.deleted.end();
    size_t n=0;
    while (i!=ae && j!=be && disk_equal((*i).second,(*j).second)) { ++i; ++j; ++n; }
    return n;
}

std::shared_ptr<Env> clock_environment(const Header& h) {
    J tempo = t::object(h.at("tempo"));
    t::ensure_default_meter(tempo); // only on this validation copy
    auto curve = t::checked_curve(tempo);
    t::validate_layout(t::Layout(tempo));
    if (tempo.contains("pulse_points")) (void)pulse_state(tempo);
    const auto end = t::confirmed_end(t::Layout(tempo),curve);
    const double duration = h.has("duration") ? sec(h.at("duration")) : curve.seconds.back();
    need(duration <= curve.seconds.back()+1e-6,"速度图未覆盖完整曲目，请延长速度图后再使用。");
    aec_bounds b{};
    b.extent = duration+1e-6;
    if (!get(tempo,"score_start_seconds").is_null() && get(get(tempo,"opening_policy"),"score_only")!=J(true)) {
        b.flags |= AEC_OPENING;
        b.opening = t::number(tempo.at("score_start_seconds"),t::Num::arithmetic)-1e-8;
    }
    if (end) {
        b.flags |= AEC_CLOSING;
        b.closing = t::number(tempo.at("score_end").at("seconds"),t::Num::arithmetic)-1e-8;
    }
    return std::make_shared<Env>(Env{std::move(curve),duration,b,std::nullopt});
}
bool same_clock(const Document& from,const Header& h) {
    return &from.header.at("tempo")==&h.at("tempo") &&
        (h.has("duration")?sec(h.at("duration")):from.env->curve.seconds.back())==from.env->duration;
}
void validate_rows(const std::array<Group,2>& events,const Env& env,int resolution) {
    for (unsigned g=0;g<2;++g) for (const auto& item:events[g].rows)
        (void)row_for(item.second,item.first,g,env,resolution);
}
std::shared_ptr<Lane> make_lane(const std::array<Group,2>& events,const Env& env,int resolution) {
    auto projection=std::make_shared<Lane>();
    std::array<std::vector<aec_row>,2> rows;
    for (unsigned g=0;g<2;++g) {
        rows[g].reserve(events[g].rows.size());
        for (const auto& item:events[g].rows) rows[g].push_back(row_for(item.second,item.first,g,env,resolution));
    }
    const aec_seed seed{sizeof(aec_seed),AEC_ABI,bytes(lane_revision),env.bounds,
                        rows[0].data(),rows[0].size(),rows[1].data(),rows[1].size()};
    aec_session* hdl=nullptr;
    core(aec_open(&seed,&hdl));
    projection->h.reset(hdl);
    return projection;
}
std::shared_ptr<Env> environment(const Header& h,const std::array<Group,2>& events,int resolution,
                                 std::shared_ptr<Lane>& lane) {
    auto env=clock_environment(h);
    lane=make_lane(events,*env,resolution);
    return env;
}
std::unique_ptr<Document> load(J body,int resolution,const J* layout=nullptr,
                               const Document* prior=nullptr,bool with_lane=true) {
    t::object(body);
    (void)revision(required(body,"revision"));
    t::array(required(body,"notes")); t::array(required(body,"controls"));
    if (!layout) {
        retire(body);
        if (!body.contains("deleted_ids")) body["deleted_ids"]=J::array();
    }
    auto& deleted=t::array(body.at("deleted_ids"));
    auto d=std::make_unique<Document>();
    for (unsigned g=0;g<2;++g)
        d->events[g]=group_from(std::move(body.at(groups[g])),prior?&prior->events[g]:nullptr,
                                layout?&layout->at(g):nullptr);
    size_t prefix=0;
    if (prior) {
        d->tombs=prior->tombs;
        d->deleted=prior->deleted;
        for (const auto& old:prior->deleted) {
            if (prefix==deleted.size() || !disk_equal(old.second,deleted.at(prefix))) break;
            ++prefix;
        }
        truncate_deleted(*d,prefix);
    }
    for (size_t i=prefix;i<deleted.size();++i) append_deleted(*d,std::move(body.at("deleted_ids").at(i)));
    for (auto i=body.begin();i!=body.end();++i) {
        if (view_key(i.key())) continue;
        std::shared_ptr<const J> value;
        if (i.key()!="notes" && i.key()!="controls" && i.key()!="deleted_ids") {
            if (prior) for (const auto& old:prior->header.fields) {
                if (old.first==i.key() && old.second && disk_equal(*old.second,i.value())) {
                    value=old.second;
                    break;
                }
            }
            if (!value) value=std::make_shared<const J>(std::move(i.value()));
        }
        d->header.fields.emplace_back(i.key(),std::move(value));
    }
    d->env=prior && same_clock(*prior,d->header)?prior->env:clock_environment(d->header);
    if (with_lane) d->lane=make_lane(d->events,*d->env,resolution);
    else validate_rows(d->events,*d->env,resolution);
    return d;
}

using BundleRows = std::array<J,2>;
BundleRows import_rows(const J& bundle,const std::string& key,bool explicit_adoption,const J* cc_override) {
    t::object(bundle);
    auto notes = t::array(required(bundle,"notes"));
    J controls = J::array();
    if (cc_override) controls = t::array(*cc_override);
    else if (explicit_adoption ? bundle.contains("controls") : t::truth(get(bundle,"controls")))
        controls = t::array(bundle.at("controls"));
    else for (const auto& p : list(bundle,"pedals")) {
        for (const bool down : {true,false}) controls.push_back(J{{"seconds",required(p,down?"start":"end")},
            {"number",required(p,"number")},{"value",down?get(p,"value",J(127)):J(0)},
            {"channel",get(p,"channel",J(0))}});
    }
    for (size_t i=0;i<notes.size();++i) {
        auto& row = notes[i];
        t::object(row);
        for (const auto* k : {"start_beat","end_beat","start_tick","end_tick"}) row.erase(k);
        J ch = get(row,"channel",J(0));
        if (!explicit_adoption) ch = t::integer_value(ch,true).json();
        row["id"] = "auto-"+key+"-"+std::to_string(i);
        row["origin"] = "transcription";
        row["channel"] = std::move(ch);
    }
    for (size_t i=0;i<controls.size();++i) {
        auto& row = controls[i];
        t::object(row);
        row["id"] = "auto-cc-"+key+"-"+std::to_string(i);
        row["origin"] = "transcription";
        if (explicit_adoption && !row.contains("channel")) row["channel"] = 0;
    }
    return {std::move(notes),std::move(controls)};
}
J initial(const J& a) {
    const auto key = t::text(required(a,"source_key"),"缺少转录来源。");
    auto rows = import_rows(required(a,"bundle"),key,false,t::find(a,"midi_controls"));
    const auto& duration = required(a,"duration");
    const J end = sec(duration)<10. && duration.is_number() ? duration : J(std::min(10.,sec(duration)));
    return J{{"version",1},{"notes",std::move(rows[0])},{"controls",std::move(rows[1])},
             {"other_events",get(a,"other_events",t::empty_array())},{"tempo",required(a,"tempo")},
             {"anchors",J::array()},{"duration",duration},{"source_key",key},{"source_export",get(a,"source_export")},
             {"deleted_ids",J::array()},{"range",J{{"start",0.},{"end",end},{"loop",false}}},
             {"operation",J{{"type","initialize"}}},{"manual",false}};
}

struct CorePatch {
    std::shared_ptr<Lane> lane;
    aec_token token{};
    std::unique_ptr<aec_prepared,decltype(&aec_free_prepared)> result{nullptr,aec_free_prepared};
    bool armed = false;
    explicit CorePatch(const std::shared_ptr<Lane>& l):lane(std::make_shared<Lane>()) {
        aec_session* fork=nullptr;
        core(aec_fork(l->h.get(),&fork));
        lane->h.reset(fork);
    }
    ~CorePatch() { if (armed) (void)aec_discard(lane->h.get(),&token); }
    aec_result prepare(unsigned group,const std::vector<K>& deletes,
                       const std::vector<aec_update>& updates,const std::vector<aec_row>& adds,
                       const RangeProjection* range=nullptr) {
        need(lane->attempt != limit,"投影请求超出范围。",AEC_LIMIT);
        core(aec_get_stamp(lane->h.get(),&token.base));
        token.request = lane->attempt+1;
        const aec_command c{sizeof(aec_command),AEC_ABI,bytes(lane_revision),token,group,
                            deletes.data(),deletes.size(),updates.data(),updates.size(),adds.data(),adds.size()};
        aec_prepared* p = nullptr;
        core(range ? prepare_range_controls(lane->h.get(),&c,*range,&p) : aec_prepare(lane->h.get(),&c,&p));
        armed = true; lane->attempt = token.request; result.reset(p);
        aec_result r{}; r.size = sizeof(r); r.abi = AEC_ABI;
        core(aec_get_result(result.get(),&r));
        return r;
    }
    void bind(bool changed) {
        if (changed) core(aec_bind_revision(lane->h.get(),&token,bytes(lane_revision)));
    }
    void accept() { core(aec_accept(lane->h.get(),&token)); armed = false; }
};
struct Patch {
    unsigned group;
    K next;
    std::map<K,Slot> put;
    std::set<K> remove;
    std::map<std::string,K> new_ids;
    J::array_t deleted;
    std::set<std::string> new_tombs;
    CorePatch core_patch;
    Patch(unsigned g,K n,std::shared_ptr<Lane> lane):group(g),next(n),core_patch(std::move(lane)) {}
    void tomb(const J& id) { deleted.push_back(id); new_tombs.insert(id_key(id)); }
    void install(Document& d) {
        auto& g=d.events[group];
        for (const auto key:remove) {
            g.ids.erase(g.rows.at(key).ident);
            g.rows.erase(key);
        }
        for (auto& it:put) g.rows.set(it.first,std::move(it.second));
        for (const auto& id:new_ids) g.ids.set(id.first,id.second);
        for (auto& id:deleted) append_deleted(d,std::move(id));
        g.next=next;
        d.lane=core_patch.lane;
    }
};
J exported_group(const Group& g) {
    J out=J::array(); out.get_ref<J::array_t&>().reserve(g.rows.size());
    for (const auto& it:g.rows) out.push_back(it.second.json);
    return out;
}
J snapshot(const Document& d,const Header& h) {
    J j=J::object();
    for (const auto& f:h.fields) {
        if (f.first=="notes") j[f.first]=exported_group(d.events[0]);
        else if (f.first=="controls") j[f.first]=exported_group(d.events[1]);
        else if (f.first=="deleted_ids") {
            J ids=J::array(); ids.get_ref<J::array_t&>().reserve(d.deleted.size());
            for (const auto& entry:d.deleted) ids.push_back(entry.second);
            j[f.first]=std::move(ids);
        } else j[f.first]=*f.second;
    }
    return j;
}
std::string next_id(std::string& cursor,const Document& d,const Patch& p) {
    constexpr std::string_view digits = "0123456789abcdef";
    for (;;) {
        size_t i = cursor.size();
        for (;;) {
            need(i != 0,"事件标识空间不足。",AEC_LIMIT);
            --i;
            const auto n = digits.find(cursor[i]);
            cursor[i] = digits[(n+1)%16];
            if (n!=15) break;
        }
        const auto key = J(cursor).dump();
        if (!d.events[0].ids.count(key) && !d.events[1].ids.count(key) &&
            !d.tombs.count(key) && !p.new_tombs.count(key) && !p.new_ids.count(key)) return cursor;
    }
}
std::unique_ptr<Patch> event_prepare(const Document& d,const std::shared_ptr<Lane>& lane,
                                     const J& op,unsigned g,int resolution,bool preview,std::string mint,J& delta,
                                     const RangeProjection* range=nullptr,std::string* batch_mint=nullptr) {
    const auto& src = d.events[g];
    auto p = std::make_unique<Patch>(g,src.next,lane);
    const bool notes = g==AEC_NOTES;
    const auto& env = *d.env;
    const auto lookup = [&](const J& id) {
        const auto i = src.ids.find(id_key(id));
        need(i && !p->remove.count(*i),"所选事件不存在。",AEC_MISSING);
        const auto k = *i;
        need(notes || range || py_equal(src.rows.at(k).json.at("number"),J(64)),"仅开放 CC64 编辑。");
        return k;
    };
    const auto allowed = [notes](std::string_view f) {
        return notes ? f=="start" || f=="end" || f=="pitch" || f=="velocity" : f=="seconds" || f=="value";
    };
    std::vector<K> deletes;
    for (const auto& id : list(op,"delete")) {
        const auto k = lookup(id);
        deletes.push_back(k); p->remove.insert(k); p->tomb(id);
    }
    for (const auto& u : list(op,"updates")) {
        t::object(u);
        const auto k = lookup(get(u,"id"));
        auto& r = p->put.try_emplace(k,src.rows.at(k)).first->second.json;
        for (auto it=u.begin();it!=u.end();++it) {
            if (it.key()=="id") continue;
            need(allowed(it.key()),"不支持的事件编辑字段。");
            r[it.key()] = it.value();
        }
    }
    std::vector<aec_update> updates;
    bool raw_changed = false;
    const uint32_t mask = notes ? AEC_TIME|AEC_END|AEC_DATA1|AEC_DATA2 : AEC_TIME|AEC_DATA2;
    for (auto& item : p->put) {
        const auto& old = src.rows.at(item.first);
        auto& s = item.second;
        bool changed = false;
        for (auto it=s.json.begin();it!=s.json.end();++it)
            if (allowed(it.key()) && !py_equal(it.value(),old.json.at(it.key()))) changed = true;
        if (changed) s.json["origin"] = "manual";
        if (!notes && changed) s.cc_tag = !old.cc_tag;
        raw_changed = raw_changed || changed;
        updates.push_back({row_for(s,item.first,g,env,resolution),mask});
    }
    std::vector<aec_row> adds;
    J additions = J::array();
    for (const auto& a : list(op,"add")) {
        J r = t::object(a);
        need(p->next != limit,"事件过多。",AEC_LIMIT);
        const K key = p->next++;
        const J id = preview ? J() : J(next_id(batch_mint?*batch_mint:mint,d,*p));
        r["id"] = id; r["origin"] = "manual";
        if (!r.contains("channel")) r["channel"] = 0;
        if (!notes && !r.contains("number")) r["number"] = 64;
        if (!notes && !range) need(py_equal(r.at("number"),J(64)),"仅开放 CC64 编辑。");
        Slot slot{std::move(r),id_key(id),false};
        adds.push_back(row_for(slot,key,g,env,resolution));
        if (!preview) p->new_ids.emplace(slot.ident,key);
        p->put.emplace(key,std::move(slot));
        additions.push_back(J{{"index",additions.size()},{"id",id},{"retained",true}});
    }
    const auto native = p->core_patch.prepare(g,deletes,updates,adds,range);
    if (!native.changed && !raw_changed && adds.empty() && deletes.empty()) return nullptr;
    J normalized = J::array();
    for (size_t i=0;i<native.normalized_count;++i) {
        const K k = native.normalized[i];
        const bool added = k>=src.next;
        const auto& slot = added?p->put.at(k):src.rows.at(k);
        if (preview && added) normalized.push_back(J{{"add_index",k-src.next}});
        else {
            const auto& id = slot.json.at("id");
            normalized.push_back(J{{"id",id}});
            if (!d.tombs.count(slot.ident) && !p->new_tombs.count(slot.ident)) p->tomb(id);
        }
        if (added) {
            additions[static_cast<size_t>(k-src.next)]["retained"] = false;
            if (!preview) p->new_ids.erase(slot.ident);
        } else p->remove.insert(k);
        p->put.erase(k);
    }
    J upsert=J::array(), removed=J::array();
    for (const auto k : p->remove) removed.push_back(src.rows.at(k).json.at("id"));
    for (const auto& item : p->put) {
        J row=item.second.json;
        const bool added=item.first>=src.next;
        if (preview && added) row.erase("id");
        J change{{"row",std::move(row)}};
        if (added) change["add_index"]=item.first-src.next;
        upsert.push_back(std::move(change));
    }
    delta["events"] = J{{"group",groups[g]},{"upsert",std::move(upsert)},{"remove",std::move(removed)},
                        {"normalized",std::move(normalized)},{"additions",std::move(additions)},
                        {"append_deleted_ids",p->deleted}};
    if (!preview || batch_mint) p->core_patch.bind(native.changed!=0); // a batch advances only private forks during preview
    return p;
}

struct RangeHistory {
    // Selection geometry is live view state, not part of the on-disk document.
    J before,after;
};
struct History {
    std::shared_ptr<const Document> before,after;
    std::optional<RangeHistory> range_view;
    std::array<std::vector<K>,2> keys;
    size_t deleted_prefix=0;
    int group=-1;
    bool replace=false;
};
struct HistoryCapture {
    std::vector<std::shared_ptr<const History>> undo,redo;
};
struct LegacyImport {
    std::vector<std::string> labels;
    size_t position=0,cursor=0;
    std::shared_ptr<const Document> center;
};
struct Plan {
    aec_token token{};
    std::unique_ptr<Patch> events;
    std::unique_ptr<Header> header;
    std::shared_ptr<Env> env;
    std::shared_ptr<Lane> lane;
    std::unique_ptr<Document> replacement;
    std::shared_ptr<const Document> target;
    std::shared_ptr<const History> edge;
    Table<std::string,bool> revisions;
    int step=0;
    J index, delta=J::object(), view=J::object();
    std::string observed;
    bool body=false, content=false, clear_draft=false;
    bool selection_op=false;
    bool metadata_noop=false;
    std::optional<J> selection; // engaged null clears the live selection
    std::array<std::set<K>,2> batch_keys;
};
std::atomic<K> epochs{0};
K epoch() {
    K n=epochs.load(std::memory_order_relaxed);
    do { need(n!=limit,"会话编号用尽。",AEC_LIMIT); }
    while (!epochs.compare_exchange_weak(n,n+1,std::memory_order_relaxed));
    return n+1;
}
} // namespace astra::editor::document_detail

struct aed_session {
    using J = astra::editor::Json;
    using Document = astra::editor::document_detail::Document;
    using Plan = astra::editor::document_detail::Plan;
    std::shared_ptr<const Document> doc;
    std::vector<std::shared_ptr<const astra::editor::document_detail::History>> undo,redo;
    astra::editor::document_detail::Table<std::string,bool> revisions;
    std::unique_ptr<astra::editor::document_detail::LegacyImport> legacy_import;
    std::shared_ptr<astra::editor::document_detail::Lane> reopened_lane; // owner-only cache for a cold historical root
    std::unique_ptr<J> planner_view;
    std::unique_ptr<Plan> pending;
    std::string project;
    J index, draft, selection;
    aec_stamp stamp{};
    uint64_t last_request=0, applied_sequence=0, content_sequence=0, draft_base=0, draft_serial=0;
    int resolution=0;
    bool closed=false;
};

struct aed_snapshot {
    std::shared_ptr<const astra::editor::document_detail::Document> doc;
    astra::editor::Json head;
    int resolution;
    std::unique_ptr<astra::editor::document_detail::HistoryCapture> history;
};

namespace astra::editor::document_detail {
void live(const aed_session* s) {
    need(s,"缺少编辑器会话。");
    need(!s->closed,"编辑器已关闭。",AEC_CLOSED);
    need(!s->legacy_import,"Legacy history import is incomplete",AEC_BUSY);
}
bool stamp_is(const aec_stamp& a,const aec_stamp& b) { return a.epoch==b.epoch && a.sequence==b.sequence; }
const std::string& rev(const aed_session& s) { return revision(s.doc->header.at("revision")); }
const Plan& staged(const aed_session* s,const aec_token* tok) {
    live(s);
    need(tok && s->pending && stamp_is(s->stamp,tok->base) && s->pending->token.request==tok->request,
         stale_message,AEC_STALE);
    return *s->pending;
}
#include "document_history.inc"
#include "document_memory.inc"
J context(aed_session& s,bool hash=false,bool with_tempo=false) {
    auto& d=*s.doc;
    J ctx{{"revision",rev(s)},{"project_id",s.project},{"duration",d.env->duration},
          {"source_key",d.header.at("source_key")}};
    if (hash) {
        if (!d.env->sha) d.env->sha=t::tempo_sha(d.header.at("tempo"));
        ctx["tempo_sha256"]=*d.env->sha;
    }
    if (with_tempo) ctx["tempo"]=d.header.at("tempo");
    return ctx;
}
// Presentation values are not authority for document identity, clocks or evidence.
J semantic_context(aed_session& s,const J& op,const J& trusted,bool object_clipboard=true) {
    J ctx=context(s);
    const J view=t::object(get(op,"context",J::object()));
    if (view.contains("axis")) {
        const auto& a=t::object(view.at("axis"));
        const auto mode=t::text(required(a,"mode"),"invalid axis mode");
        need(mode=="bars" || mode=="seconds","invalid axis mode");
        J axis{{"mode",mode}};
        for(const char* k:{"width","seconds_extent","beat_extent"}) {
            const double v=t::number(required(a,k)); need(v>0,"invalid axis extent"); axis[k]=v;
        }
        // Musical extent is derived from the authoritative curve, never supplied by UI.
        axis["beat_extent"]=t::timing_query(s.doc->env->curve,t::InputAxis::seconds,
                                            axis.at("seconds_extent").get<double>());
        need(axis.at("beat_extent").get<double>()>0,"invalid projected beat extent");
        ctx["axis"]=std::move(axis);
    }
    if(view.contains("cursor_seconds")) ctx["cursor_seconds"]=sec(view.at("cursor_seconds"));
    // Locator range is current owner view state rather than a stale document snapshot.
    ctx["locators"]=get(s.index,"range",s.doc->header.at("range"));
    if(object_clipboard && view.contains("clipboard")) {
        const auto& wire=view.at("clipboard");
        // WebChannel round trips through JS Number would change unknown
        // integer tokens, 1.0 and negative zero. The native-issued text and
        // the existing object form both go through the same validation below.
        J decoded;
        if (wire.is_string()) decoded=parse(bytes(wire.get_ref<const std::string&>()));
        const auto& cb=t::object(wire.is_string()?decoded:wire);
        const auto kind=t::text(required(cb,"type"),"invalid clipboard type");
        need(kind=="notes" || kind=="controls","invalid clipboard type");
        const auto& rows=t::array(required(cb,"rows"));
        for(const auto& row:rows) {
            t::object(row);
            (void)midi(get(row,"channel",J(0)),0,15,"invalid clipboard channel");
            if(kind=="notes") {
                need(sec(required(row,"end"))>sec(required(row,"start")),"invalid clipboard note span");
                (void)midi(required(row,"pitch"),0,127,"invalid clipboard pitch");
                (void)midi(required(row,"velocity"),1,127,"invalid clipboard velocity");
            } else {
                (void)sec(required(row,"seconds"));
                (void)midi(required(row,"number"),64,64,"clipboard only supports CC64");
                (void)midi(required(row,"value"),0,127,"invalid clipboard CC value");
            }
        }
        ctx["clipboard"]=J{{"type",kind},{"rows",rows}};
    }
    // These fields originate in a host-owned gesture/evidence cache, not raw UI input.
    const J owned=t::object(get(trusted,"semantic_context",J::object()));
    if(owned.contains("cc_display")) ctx["cc_display"]=t::object(owned.at("cc_display"));
    if(owned.contains("detected_seconds")) {
        const auto& xs=t::array(owned.at("detected_seconds"));
        for(const auto& x:xs) (void)sec(x);
        ctx["detected_seconds"]=xs;
    }
    return ctx;
}
#include "document_selection_range.inc"

void current_draft(const aed_session& s,const J& req) {
    need(s.draft.is_object() && py_equal(get(req,"token"),s.draft.at("token")),
         "结构草稿已失效。",AEC_STALE);
    const auto& version= get(req,"draft_version");
    need(version.is_number_integer() && py_equal(version,s.draft.at("draft_version")),
         "草稿已有更新，请使用最新草稿。",AEC_STALE);
    if (!text_is(get(req,"action"),"discard"))
        need(s.content_sequence==s.draft_base && text_is(get(req,"base_revision"),rev(s)),
             "结构草稿版本已过期。",AEC_STALE);
}
void draft_step(aed_session& s,const J& op,const aec_token& token,aed_buffer& out) {
    t::object(op);
    const bool begin=text_is(get(op,"action"),"begin");
    need(!text_is(get(op,"action"),"apply"),"应用草稿必须由主编辑事务准备。");
    if (!begin) current_draft(s,op);
    auto ctx=context(s,true,true);
    if (begin) {
        need(s.draft_serial!=limit,"草稿编号用尽。",AEC_LIMIT);
        ctx["new_draft_token"]=std::to_string(s.stamp.epoch)+":"+std::to_string(s.draft_serial+1);
    }
    auto reply=draft_command(s.draft,op,ctx);
    const bool install=reply.contains("state");
    auto& response=reply.at("response");
    const auto& state=install?reply.at("state"):s.draft;
    if (state.is_object()) {
        J ops=J::array();
        for (const auto& row:state.at("history")) ops.push_back(row.at("operation"));
        response["_recovery_operations"]=std::move(ops);
    }
    emit(out,response);
    if (install) s.draft.swap(reply.at("state"));
    if (begin) { ++s.draft_serial; s.draft_base=s.content_sequence; }
    s.last_request=token.request;
}
void validate_batch(const J& op) {
    const auto& operations=t::array(required(op,"operations"));
    need(!operations.empty() && operations.size()<=512,"结构草稿需要1–512项明确操作。");
    bool edit=false;
    for (const auto& row:operations) {
        t::object(row,"结构操作必须是对象。");
        if (!text_is(get(row,"type"),"preserve_positions")) edit=true;
    }
    need(edit,"尚未修改拍位或拍号；锁定开关仅作用于当前草稿。");
    need(get(op,"preserve_unedited_positions",J(true)).is_boolean(),"结构锁定设置必须明确。");
}
void install_tempo(Header& h,J& delta,const J& r,bool warnings,bool impact) {
    h.set("tempo",r.at("tempo")); delta["tempo"]=r.at("tempo");
    h.set("anchors",r.at("fixed_anchors")); delta["anchors"]=r.at("fixed_anchors");
    h.set("summary",r.at("summary")); delta["summary"]=r.at("summary");
    if (warnings) {
        h.set("edit_warnings",get(r,"warnings",t::empty_array()));
        delta["edit_warnings"]=h.at("edit_warnings");
    }
    if (impact) { h.set("edit_impact",r.at("impact")); delta["edit_impact"]=r.at("impact"); }
}
bool match_group(const J& rows,const Group& g) {
    if (rows.size()!=g.rows.size()) return false;
    size_t i=0;
    for (const auto& row:g.rows) if (!py_equal(rows[i++],row.second.json)) return false;
    return true;
}
void bundle_change(aed_session& s,Plan& p,const J& cap,bool adopt,J& operation) {
    const auto& d=*s.doc;
    const auto& h=d.header;
    const auto& key=t::text(required(cap,"source_key"),"缺少转录来源。");
    if (!adopt && t::truth(h.at("manual"))) return;
    if (adopt) need(!text_is(h.at("source_key"),key),"当前工程已经基于此转录候选。");
    J retained;
    const bool keep_cc=!adopt && t::truth(get(cap,"retain_midi_controls"));
    if (keep_cc) retained=exported_group(d.events[1]);
    auto rows=import_rows(required(cap,"bundle"),key,adopt,keep_cc?&retained:nullptr);
    if (!adopt && py_equal(h.at("tempo"),cap.at("tempo")) && py_equal(h.at("duration"),cap.at("duration")) &&
        text_is(h.at("source_key"),key) && match_group(rows[0],d.events[0]) && match_group(rows[1],d.events[1])) return;
    auto n=std::make_unique<Document>();
    n->header=h;
    n->events[0]=group_from(std::move(rows[0])); n->events[1]=group_from(std::move(rows[1]));
    if (adopt) {
        n->header.set("source_key",key);
        n->header.set("source_export",get(cap,"source_export"));
        n->header.set("manual",true);
        operation=J{{"type","adopt_transcription_candidate"},{"candidate_key",key},
                    {"candidate_sha256",required(cap,"candidate_sha256")},{"previous_source_key",h.at("source_key")}};
        n->header.set("operation",operation);
        n->header.set("summary","已采用新转录音符与 CC；生效速度图、拍号、固定锚点及选区保留，可撤销。");
        n->header.set("edit_warnings",J::array());
    } else {
        n->header.set("tempo",cap.at("tempo")); n->header.set("source_key",key); n->header.set("duration",cap.at("duration"));
        n->deleted=d.deleted; n->tombs=d.tombs;
    }
    n->env=environment(n->header,n->events,s.resolution,n->lane);
    const auto& end=get(h.at("tempo"),"score_end");
    if (adopt && t::truth(end)) {
        const double last=t::number(end.at("seconds"),t::Num::arithmetic)+1e-8;
        for (const auto& row:n->events[0].rows)
            need(sec(row.second.json.at("end"))<=last,
                 "新候选有音符延续到已确认谱面终点之后；请先核对或后移谱面终点，再采用。不会自动裁切。");
    }
    p.delta["replacement"]=J{{"notes",n->events[0].rows.size()},{"controls",n->events[1].rows.size()},
                             {"source_key",key},{"source_export",n->header.at("source_export")}};
    p.replacement=std::move(n);
}
J read_query(aed_session& s,const J& req) {
    const auto name=t::text(required(req,"query"),"缺少查询名。");
    const auto& d=*s.doc;
    auto args=t::object(get(req,"args",J::object()));
    if (name=="history_target") return history_target(s,args);
    if (name=="pulse_state") return pulse_state(d.header.at("tempo"));
    if (name=="mapped_cc_tick") return t::round_even(t::finite(t::timing_query(d.env->curve,t::InputAxis::seconds,
                                               sec(required(args,"seconds")))*s.resolution));
    if (name=="info") return head(s);
    args["tempo"]=d.header.at("tempo"); args["revision"]=rev(s); args["fixed_anchors"]=d.header.at("anchors");
    if (name=="tempo_reference") {
        // Both parts of the reply use the same revision-checked owner clock.
        auto result=timing_query("preview_constant_tempo",args);
        J view=get(args,"viewport");
        if (!t::truth(view)) {
            const auto& op=required(args,"request");
            view=J{{"start",required(op,"start")},{"end",required(op,"end")}};
        }
        t::object(view,"参考显示范围无效。");
        result["points"]=timing_query("reference_grid_points",J{
            {"reference_grid",result.at("reference_grid")},
            {"start_seconds",get(view,"start")},{"end_seconds",get(view,"end")},
            {"max_points",4096}});
        return result;
    }
    if(name=="convert_many") args["anchors"]=d.header.at("tempo").at("anchors");
    return timing_query(name,args);
}

void validate_instrument_state(const J& state) {
    if (state.is_null()) return; // Explicit disabled, distinct from a legacy missing field.
    need(state.is_object() && state.size()==5,"Invalid instrument state reference");
    const auto& version=required(state,"version");
    need(version.is_number_integer() && version==1,"Invalid instrument state version");
    const auto& name=t::text(required(state,"instrument"),"Invalid instrument identity");
    const auto chars=std::count_if(name.begin(),name.end(),[](unsigned char c){return (c&0xc0)!=0x80;});
    need(!name.empty() && chars<=256,"Invalid instrument identity");
    const auto hex=[&](const char* key,size_t length) {
        const auto& value=t::text(required(state,key),"Invalid instrument state digest");
        need(value.size()==length && std::all_of(value.begin(),value.end(),[](char c){
            return (c>='0' && c<='9') || (c>='a' && c<='f');
        }),"Invalid instrument state digest");
    };
    hex("class_id",32); hex("sha256",64);
    const auto& size=required(state,"size");
    need(size.is_number_integer() && size>=1 && size<=67108864,"Invalid instrument state size");
}

std::unique_ptr<Plan> plan(aed_session& s,const J& packet,const J& trusted,bool preview) {
    J op=t::object(required(packet,"op"));
    auto kind=t::text(required(op,"type"),"未知工程编辑操作。");
    auto p=std::make_unique<Plan>();
    auto& d=*s.doc;
    const auto& h=d.header;
    if (kind=="history") {
        need(!preview,"历史查询使用 history_target。");
        p->index=history_target(s,op);
        if (!p->index.is_null()) {
            p->observed=revision(p->index.at("revision"));
            p->step=direction(op);
            p->edge=history_from(s,p->step).back();
            p->target=p->step<0?p->edge->before:p->edge->after;
            p->content=true;
        }
        return p;
    }
    if (kind=="range") {
        const double a=sec(required(op,"start")), b=sec(required(op,"end"));
        need(std::max(a,b)<=d.env->duration+1e-6,"定位点必须位于项目范围内。");
        J range{{"start",a},{"end",b},{"loop",t::truth(get(op,"loop"))}};
        if (!py_equal(get(s.index,"range"),range)) {
            p->delta["range"]=range;
            if (!preview) { p->index=s.index; p->index["range"]=std::move(range); p->observed=rev(s); }
        }
        return p;
    }
    if (kind=="instrument_state") {
        const auto& state=required(op,"state");
        validate_instrument_state(state);
        if (s.index.contains("instrument_state") && py_equal(s.index.at("instrument_state"),state)) {
            p->metadata_noop=true;
        } else {
            p->delta["instrument_state"]=state;
            if (!preview) {
                p->index=s.index;
                p->index["instrument_state"]=state;
                p->observed=rev(s);
            }
        }
        return p;
    }
    const auto proposed=preview?std::string():fresh_revision(packet);
    if (!preview) {
        need(proposed!=rev(s),"不可复用当前版本标识。",AEC_DUPLICATE);
        need(!s.revisions.count(proposed),"不可复用历史版本标识。",AEC_DUPLICATE);
    }
    bool pedal_length=false;
    if (kind=="note_semantic") {
        // The planner's current shared signature takes a JSON document. Cache
        // one private owned projection per content installation, not per drag
        // sample. The reducer below still validates and stages only touched rows.
        if (!s.planner_view) s.planner_view=std::make_unique<J>(snapshot(d,h));
        const auto& native_command=required(op,"command");
        std::optional<J> range_command;
        const auto* view=t::find(op,"context");
        if (const auto* token=view?t::find(*view,"range_token"):nullptr) {
            need(!native_command.contains("ids") && !native_command.contains("all_notes"),
                 "Range and object or whole-part targets cannot be combined.");
            const auto action=t::text(required(native_command,"action"),"Invalid note action.");
            bool whole=action=="transpose";
            if(action=="quantize"){
                const auto target=t::text(get(native_command,"target",J("start")),"Invalid quantize target.");
                need(target=="start" || target=="end" || target=="length" || target=="both",
                     "Invalid range quantize target.");
                // Start quantize translates the tail too to preserve musical length.
                whole=target=="start" || target=="both";
            }else need(action=="transpose" || action=="length_scale" || action=="legato" ||
                       action=="fixed_lengths" || action=="delete_overlaps",
                       "Unsupported range note action.");
            const auto& selection=active_selection(s,J{{"token",*token}});
            const auto scope=selection_scope(selection.at("scope"));
            need(scope.notes(),"所选范围不包含音符。");
            const auto span=selection_span(selection);
            need(span.a<span.b,stale_message,AEC_STALE);
            J ids=J::array();
            // Authoritative current head only. Require both endpoints for
            // commands that operate on a whole note; require a selected tail
            // for commands that leave the start fixed. Never split notes here.
            for (const auto& item:d.events[0].rows) {
                const auto& row=item.second.json;
                const double a=sec(row.at("start")),b=sec(row.at("end"));
                if (a<b && span.a<b && b<=span.b && (!whole || span.a<=a)) ids.push_back(row.at("id"));
            }
            // An empty ID list stays empty; never fall back to all notes.
            range_command=native_command;
            (*range_command)["ids"]=std::move(ids);
        }
        auto r=note_command(*s.planner_view,range_command?*range_command:native_command,semantic_context(s,op,trusted));
        auto& meta=r.at("preview");
        if (preview) {
            if (const auto* clip=t::find(meta,"clipboard")) {
                auto wire=lossless::dump(*clip);
                meta["clipboard_json"]=std::move(wire);
            }
        }
        p->delta["semantic_preview"]=std::move(meta);
        p->delta["semantic_warnings"]=std::move(r.at("warnings"));
        if (r.at("operation").is_null()) return p;
        const bool pedal_action=text_is(get(native_command,"action"),"pedals_to_note_length");
        op=std::move(r.at("operation"));
        kind=t::text(required(op,"type"),"无效的语义编辑结果。");
        pedal_length=kind=="semantic_pedal_length" && pedal_action;
        need(kind=="notes" || kind=="controls" || pedal_length,"语义编辑必须返回事件命令。");
    }
    const bool refresh=kind=="refresh_candidate";
    bool clock_edit=false;
    if (kind=="selection_range") {
        p->selection_op=true;
        plan_selection_range(s,*p,op,trusted,preview,proposed);
        if (!p->replacement) return p;
        // Neither browser evidence nor the lossless clipboard belongs in a revision.
        J record{{"type",kind},{"action",op.at("command").at("action")}};
        op=std::move(record);
    } else if (pedal_length) {
        const auto note_count=p->delta.at("semantic_preview").at("updates").size();
        const auto control_count=p->delta.at("semantic_preview").at("deleted_control_ids").size();
        SelectionBatch batch{s,*p,preview,{}};
        batch.stage(0,required(op,"notes"));
        batch.stage(1,required(op,"controls"));
        const auto& deltas=p->delta.at("event_batches");
        need(deltas.size()==2 && deltas.at(0).at("upsert").size()==note_count &&
             deltas.at(0).at("remove").empty() && deltas.at(1).at("upsert").empty() &&
             deltas.at(1).at("remove").size()==control_count &&
             deltas.at(0).at("normalized").empty() && deltas.at(1).at("normalized").empty(),
             "Pedal conversion did not preserve the exact sparse plan.");
        op=J{{"type","note_semantic"},{"action","pedals_to_note_length"}};
    } else if (kind=="notes" || kind=="controls") {
        if (!d.lane && !s.reopened_lane) s.reopened_lane=make_lane(d.events,*d.env,s.resolution);
        p->events=event_prepare(d,d.lane?d.lane:s.reopened_lane,op,kind=="notes"?0u:1u,
                                s.resolution,preview,proposed,p->delta);
        if (!p->events) return p;
        p->header=std::make_unique<Header>(h);
    } else if (refresh || kind=="adopt_transcription_candidate") {
        bundle_change(s,*p,t::object(required(trusted,kind.c_str())),!refresh,op);
        if (!p->replacement) return p;
    } else {
        p->header=std::make_unique<Header>(h);
        auto& next=*p->header;
        const auto& tempo=h.at("tempo");
        const auto& anchors=h.at("anchors");
        clock_edit=true;
        if (kind=="tempo") {
            need(!text_is(get(op,"action"),"rhythm_span_anchors"),"Internal rhythm span is not an editor command.");
            auto r=structural_command(tempo,op,anchors,context(s,true));
            install_tempo(next,p->delta,r,true,false);
        } else if (kind=="rebar") {
            auto r=structural_command(tempo,op,anchors,context(s,true));
            install_tempo(next,p->delta,r,true,true);
        } else if (kind=="rhythm_intent") {
            op.erase("_note_onsets"); op.erase("_leading_note_count");
            auto ctx=context(s);
            if (text_is(get(op,"action"),"divide_beats") || text_is(get(op,"action"),"record_beats")) {
                J onsets=J::array();
                for (const auto& it:d.events[0].rows) onsets.push_back(it.second.json.at("start"));
                ctx["note_onsets"]=std::move(onsets);
            }
            auto r=pulse_command(tempo,op,anchors,ctx);
            if (text_is(get(op,"action"),"detect_trends")) {
                p->header.reset(); p->delta["analysis"]=std::move(r); return p;
            }
            install_tempo(next,p->delta,r,true,true);
        } else if (kind=="rhythm_structure" || kind=="draft_apply") {
            J r;
            if (kind=="draft_apply") {
                const auto& request=t::object(required(op,"request"));
                need(text_is(get(request,"action"),"apply"),"缺少草稿应用请求。");
                current_draft(s,request);
                r=draft_command(s.draft,request,context(s,true,true));
                op=J{{"type","rhythm_structure"},{"operations",r.at("commit").at("operations")},
                     {"scope",s.draft.at("scope")},{"preserve_unedited_positions",s.draft.at("initial_preserve")}};
                p->clear_draft=true;
            } else {
                validate_batch(op);
                J batch=op; batch.erase("action");
                r=draft_command(J(),batch,context(s,true,true));
            }
            auto& commit=r.at("commit");
            commit["summary"]="已应用 "+std::to_string(op.at("operations").size())+" 项拍位／拍号修改；原演奏秒数不变，可一次撤销。";
            install_tempo(next,p->delta,commit,true,false);
        } else if (kind=="bar_phase_candidate") {
            op=t::object(required(trusted,"bar_phase"));
            need(text_is(get(op,"type"),"bar_phase_candidate"),"未经确认的局部分组候选。");
            J candidate=tempo;
            candidate["time_signatures"]=t::array(required(op,"time_signatures"));
            candidate["source"]="manual_editor";
            candidate["meter_policy"]=J{{"mode","confirmed_local_bar_decode"},{"protected",true}};
            candidate["local_bar_decode"]=get(op,"analysis",J::object());
            next.set("tempo",std::move(candidate));
            next.set("summary","已更新局部分组；速度、音符和踏板实际位置不变。");
        } else if (kind=="adopt_rhythm_candidate") {
            if (preview && trusted.contains("rhythm_inspection")) {
                // Pre-artifact applicability check, NOT an authorization to
                // adopt. Real adoption preview/prepare both take the cap below.
                const auto& cap=t::object(trusted.at("rhythm_inspection"));
                J candidate=t::object(cap.at("tempo"));
                candidate["candidate_only"]=false;
                candidate["adopted_candidate"]=t::object(cap.at("identity"));
                if (t::truth(get(tempo,"score_end"))) candidate["score_end"]=tempo.at("score_end");
                next.set("tempo",std::move(candidate));
            } else {
                const auto& cap=t::object(required(trusted,"rhythm_candidate"));
                const auto& row=t::object(cap.at("row"));
                auto ctx=context(s); ctx["rhythm_candidate"]=cap;
                J action{{"type","adopt_rhythm_candidate"},{"candidate_id",required(op,"candidate_id")}};
                auto r=pulse_command(tempo,action,anchors,ctx);
                op=J{{"type","adopt_rhythm_candidate"},{"tempo",row.at("tempo")},{"identity",row.at("identity")}};
                install_tempo(next,p->delta,r,false,false);
            }
        } else if (kind=="replace_tempo") next.set("tempo",t::object(required(op,"tempo")));
        else if (kind=="anchors" || kind=="beat_flag_lock")
            need(false,"内部固定点功能已移除；请直接编辑拍点或使用选区两端。");
        else need(false,"未知工程编辑操作。");
        p->env=environment(next,d.events,s.resolution,p->lane);
    }
    p->body=true; p->content=true;
    auto& next=p->replacement?p->replacement->header:*p->header;
    const auto& env=p->replacement?*p->replacement->env:p->env?*p->env:*d.env;
    if (clock_edit || refresh) p->delta["tempo"]=next.at("tempo");
    if (next.has("summary") && (clock_edit || p->replacement)) p->delta["summary"]=next.at("summary");
    if (preview) return p;
    next.set("operation",std::move(op)); next.set("manual",!refresh);
    next.set("duration",env.duration);
    next.set("range",get(s.index,"range",h.at("range")));
    next.set("revision",proposed);
    p->observed=proposed;
    p->index=s.index;
    p->index["revision"]=proposed;
    return p;
}
} // namespace astra::editor::document_detail

using namespace astra::editor::document_detail;
extern "C" {
uint32_t AEC_CALL aed_version(void) noexcept { return AED_ABI; }
int32_t AEC_CALL aed_timing_query_v1(aec_bytes input,aed_buffer* out) noexcept {
    return boundary(out,[&](aed_buffer& b) {
        const auto a=t::object(parse(input));
        const auto name=t::text(required(a,"query"),"invalid projection query");
        const bool meter_preview=name=="preview_meter_edit" || name=="preview_roll_edit";
        need(name=="editor_clock" || name=="editor_axis" || name=="editor_grid_sections" || name=="opening_meter_sections" || name=="to_seconds" || meter_preview,
             "unsupported read-only projection query");
        auto result=astra::editor::timing_query(name,t::object(required(a,"args")));
        if (meter_preview) {
            // The quick-preview model has already checked the clock and barlines.
            // Keep the old export-preview wire shape without rebuilding the map
            // or converting any opaque fields to doubles.
            auto& tempo=result.at("data");
            if (const auto* warnings=t::find(tempo,"warnings"))
                need(warnings->is_array() && std::all_of(warnings->begin(),warnings->end(),
                     [](const J& w){ return w.is_string(); }),"warnings 必须是文字列表。");
            for (auto& row:tempo.at("anchors")) {
                row.at("seconds")=t::number(row.at("seconds"),t::Num::floating);
                row.at("beat")=t::number(row.at("beat"),t::Num::floating);
            }
        }
        emit(b,result);
    });
}
int32_t AEC_CALL aed_seed_v1(aec_bytes input,aed_buffer* out) noexcept {
    return boundary(out,[&](aed_buffer& b) {
        const auto a=t::object(parse(input));
        const bool initialize=a.contains("initialize");
        J body=initialize?initial(a.at("initialize")):a.at("validate");
        t::object(body);
        if (!body.contains("revision")) body["revision"]="seed-validation";
        auto d=load(initialize?body:std::move(body),ppq(a.at("ppq")));
        if (initialize) {
            body.erase("revision"); body["duration"]=d->env->duration;
            emit(b,body);
        } else emit(b,J{{"duration",d->env->duration}});
    });
}
int32_t AEC_CALL aed_open_v1(aec_bytes input,aed_session** result,aed_buffer* out) noexcept {
    if (result) *result=nullptr;
    return boundary(out,[&](aed_buffer&) {
        need(result,"缺少会话输出。");
        auto a=parse(input); t::object(a);
        auto s=std::make_unique<aed_session>();
        s->project=t::text(required(a,"project_id"),"缺少工程身份。");
        need(!s->project.empty(),"缺少工程身份。");
        s->resolution=ppq(a.at("ppq"));
        open_history(*s,a);
        if (s->index.contains("instrument_state")) validate_instrument_state(s->index.at("instrument_state"));
        if (a.contains("saved_sequence")) {
            need(a.at("saved_sequence").is_number_unsigned(),"无效的保存序列。");
            s->applied_sequence=a.at("saved_sequence").get<K>();
        }
        s->stamp={epoch(),s->applied_sequence};
        *result=s.release();
    });
}
int32_t AEC_CALL aed_append_legacy_v2(aed_session* s,aec_bytes input,aed_buffer* out) noexcept {
    return boundary(out,[&](aed_buffer&) {
        need(s && !s->closed,"Closed legacy import",AEC_CLOSED);
        need(static_cast<bool>(s->legacy_import),"No legacy history import is pending");
        append_legacy(*s,parse(input));
    });
}
int32_t AEC_CALL aed_stamp_v1(const aed_session* s,aec_stamp* stamp,aed_buffer* out) noexcept {
    return boundary(out,[&](aed_buffer&) { live(s); need(stamp,"缺少版本输出。"); *stamp=s->stamp; });
}
int32_t AEC_CALL aed_run_v1(aed_session* s,const aec_token* tok,uint32_t mode,aec_bytes packet,
                            aec_bytes trusted,aed_buffer* out) noexcept {
    return boundary(out,[&](aed_buffer& b) {
        live(s); need(tok && mode<=AED_APPLY,"无效的命令包。");
        need(stamp_is(s->stamp,tok->base),stale_message,AEC_STALE);
        const bool mutate=mode==AED_PREPARE || mode==AED_DRAFT || mode==AED_APPLY;
        need(mutate?tok->request!=0:tok->request==0,"无效的请求编号。");
        if (mutate) need(tok->request>s->last_request,"编辑请求已处理。",AEC_DUPLICATE);
        need(!s->pending,"另一项编辑正在准备或保存。",AEC_BUSY);
        auto req=parse(packet); t::object(req);
        need(text_is(get(req,"base_revision"),rev(*s)),stale_message,AEC_STALE);
        if (mode==AED_QUERY) { emit(b,read_query(*s,req)); return; }
        if (mode==AED_DRAFT) { draft_step(*s,required(req,"op"),*tok,b); return; }
        const bool preview=mode==AED_PREVIEW;
        auto auth=trusted.count?parse(trusted):J::object(); t::object(auth);
        auto p=plan(*s,req,auth,preview);
        p->token=*tok;
        if (!preview) {
            need(p->metadata_noop || (p->selection_op && !p->content) || s->stamp.sequence!=limit,"编辑序列用尽。",AEC_LIMIT);
            prepare_memory(*s,*p);
            prepare_selection_view(*s,*p);
        }
        J reply{{"changed",p->content || !p->index.is_null() || p->delta.contains("range") || p->delta.contains("instrument_state")},
                {"preview",preview},{"write_body",mode==AED_PREPARE && p->body},
                {"index",p->index},{"delta",std::move(p->delta)}};
        if (mode==AED_APPLY) {
            reply["head"]=head(*s,p.get());
            reply["view_delta"]=std::move(p->view);
        }
        emit(b,reply);
        if (!preview) s->last_request=tok->request;
        if (mode==AED_APPLY) publish_memory(*s,*p);
        else if (mode==AED_PREPARE) s->pending=std::move(p);
    });
}
int32_t AEC_CALL aed_export_v1(const aed_session* s,const aec_token* tok,aed_buffer* out) noexcept {
    return boundary(out,[&](aed_buffer& b) {
        const auto& p=staged(s,tok); need(p.body,"该事务没有新版本正文。");
        const auto& d=*p.target;
        emit(b,snapshot(d,d.header));
    });
}
int32_t AEC_CALL aed_finish_v1(aed_session* s,const aec_token* tok,uint32_t accept,aec_bytes observed,
                               aed_buffer* out) noexcept {
    return boundary(out,[&](aed_buffer&) {
        (void)staged(s,tok); need(accept<=1,"无效的事务处置。");
        auto& p=*s->pending;
        need(span(observed)==(accept?std::string_view(p.observed):std::string_view()),
             "未观察到预期的编辑索引。",AEC_STALE);
        if (accept) publish_memory(*s,p);
        s->pending.reset();
    });
}
int32_t AEC_CALL aed_head_v2(const aed_session* s,aed_buffer* out) noexcept {
    return boundary(out,[&](aed_buffer& b){live(s); emit(b,head(*s));});
}
int32_t AEC_CALL aed_capture_v2(const aed_session* s,aed_snapshot** result,aed_buffer* out) noexcept {
    if (result) *result=nullptr;
    return boundary(out,[&](aed_buffer&){
        live(s); need(result,"Missing snapshot output");
        auto p=std::make_unique<aed_snapshot>(aed_snapshot{s->doc,head(*s),s->resolution,nullptr});
        *result=p.release();
    });
}
int32_t AEC_CALL aed_capture_checkpoint_v2(const aed_session* s,aed_snapshot** result,aed_buffer* out) noexcept {
    if (result) *result=nullptr;
    return boundary(out,[&](aed_buffer&) {
        live(s); need(result,"Missing checkpoint snapshot output");
        auto history=std::make_unique<HistoryCapture>(HistoryCapture{s->undo,s->redo});
        auto saved_head=head(*s);
        saved_head.erase("selection_range");
        auto p=std::make_unique<aed_snapshot>(aed_snapshot{s->doc,std::move(saved_head),s->resolution,std::move(history)});
        *result=p.release();
    });
}
int32_t AEC_CALL aed_read_snapshot_v2(const aed_snapshot* f,uint32_t mode,aed_buffer* out) noexcept {
    return boundary(out,[&](aed_buffer& b){
        need(f && mode<=AED_HISTORY,"Invalid snapshot query");
        if (mode==AED_HISTORY) emit(b,encode_history(*f));
        else emit(b,mode==AED_PLAYBACK?playback(*f):snapshot(*f->doc,f->doc->header));
    });
}
int32_t AEC_CALL aed_read_history_body_v2(const aed_snapshot* f,uint32_t side,size_t index,aed_buffer* out) noexcept {
    return boundary(out,[&](aed_buffer& b) {
        need(f && f->history && side<=1,"Invalid history body query");
        const auto& stack=side?f->history->redo:f->history->undo;
        need(index<stack.size(),"Missing history body",AEC_MISSING);
        const auto& d=*(side?stack[index]->after:stack[index]->before);
        emit(b,snapshot(d,d.header));
    });
}
void AEC_CALL aed_free_snapshot(aed_snapshot* s) noexcept { delete s; }
int32_t AEC_CALL aed_close_v1(aed_session* s) noexcept {
    if (!s) return AEC_INVALID;
    s->closed=true; s->pending.reset(); s->doc.reset();
    s->planner_view.reset(); s->undo.clear(); s->redo.clear();
    s->reopened_lane.reset(); s->legacy_import.reset();
    Table<std::string,bool> empty_revisions;
    s->revisions.swap(empty_revisions);
    astra::editor::Json empty; s->draft.swap(empty);
    return AEC_OK;
}
void AEC_CALL aed_free_session(aed_session* s) noexcept { delete s; }
void AEC_CALL aed_free_buffer(char* p) noexcept { delete[] p; }
} // extern C
