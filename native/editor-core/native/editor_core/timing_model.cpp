#include "lossless_json.h"
#include "timing_internal.h"
#include "timing_identity.h"

#include <algorithm>
#include <charconv>
#include <limits>
#include <utility>

namespace astra::editor::timing_detail {
namespace {

constexpr double two63 = 0x1p63;
constexpr double two64 = 0x1p64;
constexpr const char* clock_value_error = "时间必须是非负有限数";
constexpr const char* boundary_meter_error = "边界小节长度或周围拍号无效";

bool digit(char c) { return '0' <= c && c <= '9'; }
bool ascii_space(char c) { return c == ' ' || ('\t' <= c && c <= '\r'); }

// Python's finite decimal-string coercion. Not strtod: hexadecimal numbers,
// locale decimal separators and partially parsed strings are not accepted.
std::string decimal_text(std::string_view input, std::string_view reason) {
    static constexpr char32_t zeroes[] = {
        0x0660,0x06f0,0x07c0,0x0966,0x09e6,0x0a66,0x0ae6,0x0b66,0x0be6,0x0c66,0x0ce6,0x0d66,
        0x0de6,0x0e50,0x0ed0,0x0f20,0x1040,0x1090,0x17e0,0x1810,0x1946,0x19d0,0x1a80,0x1a90,
        0x1b50,0x1bb0,0x1c40,0x1c50,0xa620,0xa8d0,0xa900,0xa9d0,0xa9f0,0xaa50,0xabf0,0xff10,
        0x104a0,0x10d30,0x11066,0x110f0,0x11136,0x111d0,0x112f0,0x11450,0x114d0,0x11650,
        0x116c0,0x11730,0x118e0,0x11950,0x11c50,0x11d50,0x11da0,0x11f50,0x16a60,0x16ac0,
        0x16b50,0x1e140,0x1e2f0,0x1e4f0,0x1e950,0x1fbf0
    };
    std::string ascii;
    ascii.reserve(input.size());
    for (std::size_t i=0; i<input.size();) {
        const auto c = static_cast<unsigned char>(input[i++]);
        if (c < 0x80) { ascii += static_cast<char>(c); continue; }
        unsigned count;
        char32_t cp, minimum;
        if (c >= 0xc2 && c <= 0xdf) { count=1; cp=c&31u; minimum=0x80; }
        else if (c <= 0xef && c >= 0xe0) { count=2; cp=c&15u; minimum=0x800; }
        else if (c <= 0xf4 && c >= 0xf0) { count=3; cp=c&7u; minimum=0x10000; }
        else invalid(reason);
        for (unsigned j=0; j<count; ++j) {
            if (i==input.size()) invalid(reason);
            const auto b=static_cast<unsigned char>(input[i++]);
            if ((b&0xc0u)!=0x80u) invalid(reason);
            cp=(cp<<6)|(b&63u);
        }
        if (cp<minimum || cp>0x10ffff || (cp>=0xd800 && cp<=0xdfff)) invalid(reason);
        if (cp==0x85 || cp==0xa0 || cp==0x1680 || (cp>=0x2000 && cp<=0x200a) ||
            cp==0x2028 || cp==0x2029 || cp==0x202f || cp==0x205f || cp==0x3000) { ascii+=' '; continue; }
        if (cp>=0x1d7ce && cp<=0x1d7ff) { ascii+=static_cast<char>('0'+(cp-0x1d7ce)%10); continue; }
        const auto z=std::upper_bound(std::begin(zeroes),std::end(zeroes),cp);
        if (z==std::begin(zeroes) || cp-*(z-1)>9) invalid(reason);
        ascii+=static_cast<char>('0'+cp-*(z-1));
    }
    std::size_t left=0, right=ascii.size();
    while (left<right && ascii_space(ascii[left])) ++left;
    while (left<right && ascii_space(ascii[right-1])) --right;
    std::string out;
    out.reserve(right-left);
    for (auto i=left; i<right; ++i) {
        if (ascii[i]!='_') out+=ascii[i];
        else if (i==left || i+1==right || !digit(ascii[i-1]) || !digit(ascii[i+1])) invalid(reason);
    }
    if (out.empty()) invalid(reason);
    return out;
}

double parse_float(std::string_view s, std::string_view reason) {
    const std::string clean=decimal_text(s,reason);
    const char* p=clean.data(), *end=p+clean.size();
    if (*p=='+') { ++p; if (p==end || *p=='-' || *p=='+') invalid(reason); }
    double out=0.;
    const auto r=std::from_chars(p,end,out,std::chars_format::general);
    if (r.ptr!=end || r.ec==std::errc::invalid_argument) invalid(reason);
    if (r.ec==std::errc::result_out_of_range) {
        // from_chars reports both overflow and rounding-to-zero underflow as
        // out_of_range. The latter is accepted by Python float().
        const bool negative=*p=='-';
        if (negative) ++p;
        double leading=0., whole=0., exponent=0.;
        bool decimal=false, seen_nonzero=false;
        for (; p!=end && *p!='e' && *p!='E'; ++p) {
            if (*p=='.') { decimal=true; continue; }
            if (!digit(*p)) invalid(reason);
            if (!decimal) ++whole;
            if (*p!='0') seen_nonzero=true;
            if (!seen_nonzero) ++leading;
        }
        if (p!=end) {
            ++p;
            const bool exponent_negative=p!=end && *p=='-';
            if (p!=end && (*p=='-' || *p=='+')) ++p;
            for (; p!=end; ++p) if (exponent<1e20) exponent=exponent*10+(*p-'0');
            if (exponent_negative) exponent=-exponent;
        }
        if (!seen_nonzero || whole-leading-1+exponent<0.) return negative ? -0. : 0.;
        invalid(reason);
    }
    if (r.ec!=std::errc{}) invalid(reason);
    return finite(out,reason);
}

Integer parse_integer(std::string_view s, std::string_view reason) {
    const auto t=decimal_text(s,reason);
    const bool neg=t.front()=='-';
    const auto* start=t.data()+(neg || t.front()=='+');
    const auto* end=t.data()+t.size();
    std::uint64_t magnitude=0;
    const auto r=std::from_chars(start,end,magnitude,10);
    if (r.ec!=std::errc{} || r.ptr!=end) invalid(reason);
    return {neg && magnitude!=0,magnitude};
}

std::string float_chars(double x, std::chars_format format, int precision) {
    finite(x);
    char buffer[672];
    const auto r=precision<0 ? std::to_chars(buffer,buffer+sizeof(buffer),x,format)
                             : std::to_chars(buffer,buffer+sizeof(buffer),x,format,precision);
    if (r.ec!=std::errc{}) invalid("无法表示数值");
    return std::string(buffer,r.ptr);
}

int denominator(const Json& v, bool strict, std::string_view reason) {
    if (strict && !v.is_number_integer()) invalid(reason);
    const double x=number(v,Num::arithmetic,reason);
    for (const int d : denominators) if (x==d) return d;
    invalid(reason);
}

std::string meter_label(Meter m, const Json& raw_denominator) {
    std::string suffix;
    if (raw_denominator.is_boolean()) suffix=raw_denominator.get<bool>() ? "True" : "False";
    else if (raw_denominator.is_number_float()) suffix=float_repr(raw_denominator.get<double>());
    else suffix=std::to_string(m.d);
    return std::to_string(m.n)+"/"+suffix;
}

Meter short_meter(double span, int preferred) {
    if (!std::isfinite(span) || span<=0.) invalid(boundary_meter_error);
    for (int d : denominators) {
        if (d<preferred) continue;
        const double scaled=span*d/4.;
        if (!std::isfinite(scaled)) continue;
        const double n=round_even(scaled);
        if (1<=n && n<=255 && std::abs(n*4./d-span)<=1e-9) return {static_cast<int>(n),d};
    }
    invalid("边界小节长度无法精确写成 MIDI 拍号；请明确二分音符细分边界，不会自动舍入或改变速度");
}
Json meter_json(Meter m) { return {{"numerator",m.n},{"denominator",m.d}}; }

void write_canonical(Sha256& sha, const Json& value) {
    if (lossless::is_integer(value)) { sha.write(lossless::integer_text(value)); }
    else if (value.is_object()) {
        std::vector<Json::const_iterator> fields;
        fields.reserve(value.size());
        for (auto i=value.begin(); i!=value.end(); ++i) fields.push_back(i);
        std::sort(fields.begin(),fields.end(),[](auto a,auto b){ return a.key()<b.key(); });
        sha.write("{");
        bool first=true;
        for (auto i : fields) {
            if (!first) sha.write(",");
            first=false;
            write_canonical(sha,Json(i.key()));
            sha.write(":");
            write_canonical(sha,*i);
        }
        sha.write("}");
    } else if (value.is_array()) {
        sha.write("[");
        bool first=true;
        for (const auto& v : value) {
            if (!first) sha.write(",");
            first=false;
            write_canonical(sha,v);
        }
        sha.write("]");
    } else if (value.is_number_float()) {
        sha.write(float_repr(value.get<double>()));
    } else if (value.is_string() || value.is_number_integer() || value.is_boolean() || value.is_null()) {
        try { sha.write(value.dump(-1,' ',true)); }
        catch (const Json::exception&) { invalid("速度图包含无效 JSON 字符串"); }
    } else invalid("速度图包含非 JSON 数据");
}

} // namespace

double number(const Json& v, Num policy, std::string_view reason) {
    if (lossless::is_integer(v)) {
        try { return finite(lossless::as_double(v),reason); }
        catch (const std::invalid_argument&) { invalid(reason); }
    }
    if (v.is_number()) return finite(v.get<double>(),reason);
    if (v.is_boolean() && (policy==Num::arithmetic || policy==Num::floating)) return v.get<bool>() ? 1. : 0.;
    if (v.is_string() && (policy==Num::edit || policy==Num::floating))
        return parse_float(v.get_ref<const std::string&>(),reason);
    invalid(reason);
}
int bounded_int(const Json& v, int lo, int hi, std::string_view reason) {
    if (!v.is_number_integer()) invalid(reason);
    const double x=v.get<double>();
    if (x<lo || x>hi) invalid(reason);
    return static_cast<int>(x);
}
double Integer::as_double() const {
    const auto x=static_cast<double>(magnitude);
    return negative ? -x : x;
}
Json Integer::json() const {
    if (negative && magnitude) {
        if (magnitude==(std::uint64_t{1}<<63)) return std::numeric_limits<std::int64_t>::min();
        if (magnitude>(std::uint64_t{1}<<63)) invalid("整数超出 JSON 整数范围");
        return -static_cast<std::int64_t>(magnitude);
    }
    if (magnitude<(std::uint64_t{1}<<63)) return static_cast<std::int64_t>(magnitude);
    return magnitude;
}
Integer integer_double(double value) {
    if (!std::isfinite(value) || std::abs(value)>=two64) invalid("整数超出 JSON 整数范围");
    const auto m=static_cast<std::uint64_t>(std::abs(value));
    return {value<0. && m!=0,m};
}
Integer integer_value(const Json& v, bool coerce) {
    if (v.is_number_unsigned()) return {false,v.get<std::uint64_t>()};
    if (v.is_number_integer()) {
        const auto x=v.get<std::int64_t>();
        return x<0 ? Integer{true,static_cast<std::uint64_t>(-(x+1))+1} : Integer{false,static_cast<std::uint64_t>(x)};
    }
    if (coerce) {
        if (v.is_boolean()) return {false,v.get<bool>() ? 1u : 0u};
        if (v.is_number_float()) return integer_double(v.get<double>());
        if (v.is_string()) return parse_integer(v.get_ref<const std::string&>(),"需要整数");
    }
    invalid("需要整数");
}
bool numeric_less(const Json& a, const Json& b) {
    const double x=number(a,Num::arithmetic), y=number(b,Num::arithmetic);
    if (x!=y) return x<y;
    if (x==0. || (a.is_number_float() && b.is_number_float())) return false;
    // Preserve Python's exact int comparisons when converting an integer to
    // double rounded it onto another integer or onto the compared float.
    const auto magnitude=[&](const Json& v) -> std::pair<bool,std::uint64_t> {
        if (v.is_number_float() && std::abs(x)>=two64) return {true,0};
        return {false,integer_value(v,true).magnitude};
    };
    return x<0. ? magnitude(a)>magnitude(b) : magnitude(a)<magnitude(b);
}
double round_even(double x) {
    finite(x);
    const double a=std::abs(x), base=std::floor(a), tail=a-base;
    const double r=base+((tail>.5 || (tail==.5 && std::fmod(base,2.)!=0.)) ? 1. : 0.);
    return r==0. ? 0. : std::copysign(r,x);
}
double positive_mod(double x, double d) {
    const double r=std::fmod(x,d);
    return r==0. ? 0. : (r<0. ? r+d : r);
}
std::string format_g(double x, bool sign) {
    auto out=float_chars(x,std::chars_format::general,6);
    if (sign && out.front()!='-') out.insert(out.begin(),'+');
    return out;
}
std::string format_3f(double x) { return float_chars(x,std::chars_format::fixed,3); }
std::string float_repr(double x) {
    // Shortest correctly round-tripping digits, then Python repr's decimal
    // vs scientific cutoff. Formatting a float as an integer would change IDs.
    const auto sci=float_chars(x,std::chars_format::scientific,-1);
    const auto e=sci.find('e');
    if (e==std::string::npos) invalid("无法表示浮点数");
    int exp=0;
    for (auto i=e+2; i<sci.size(); ++i) exp=exp*10+sci[i]-'0';
    if (sci[e+1]=='-') exp=-exp;
    if (exp< -4 || exp>=16) return sci;
    const bool negative=sci.front()=='-';
    std::string digits;
    for (std::size_t i=negative ? 1u : 0u; i<e; ++i) if (sci[i]!='.') digits+=sci[i];
    const int point=exp+1;
    if (point<=0) digits="0."+std::string(static_cast<std::size_t>(-point),'0')+digits;
    else if (static_cast<std::size_t>(point)>=digits.size())
        digits+=std::string(static_cast<std::size_t>(point)-digits.size(),'0')+".0";
    else digits.insert(static_cast<std::size_t>(point),1,'.');
    return negative ? "-"+digits : digits;
}

Meter meter_of(const Json& row, bool strict_denominator, std::string_view reason) {
    object(row,reason);
    return {bounded_int(timing_detail::get(row,"numerator"),1,32,reason),
            denominator(timing_detail::get(row,"denominator"),strict_denominator,reason)};
}
const Json* Layout::find(const char* key) const {
    if (patch) if (const auto* v=timing_detail::find(*patch,key)) return v;
    return timing_detail::find(base,key);
}
const Json& Layout::get(const char* key, const Json& fallback) const {
    if (const auto* v=find(key)) return *v;
    return fallback;
}
const Json& Layout::rows() const {
    if (const auto* v=find("time_signatures")) return array(*v,"拍号列表必须是数组");
    static const Json defaults=Json::array({Json{{"beat",0},{"numerator",4},{"denominator",4}}});
    return defaults;
}
void ensure_default_meter(Json& tempo) {
    if (!find(tempo,"time_signatures")) tempo["time_signatures"]=Layout(tempo).rows();
}
void validate_layout(Layout layout) {
    const auto& rows=layout.rows();
    if (rows.empty() || number(required(rows.front(),"beat"),Num::arithmetic)!=0.)
        invalid("拍号列表必须从 beat=0 开始");
    const Json* previous=nullptr;
    for (const auto& row : rows) {
        const auto& beat=required(row,"beat");
        number(beat,Num::arithmetic,"无效拍号或拍号位置");
        if (previous && !numeric_less(*previous,beat)) invalid("无效拍号或拍号位置");
        meter_of(row);
        previous=&beat;
    }
    const double pickup=number(layout.get("pickup_beats",0),Num::floating);
    if (pickup<0. || pickup>=meter_of(rows.front()).length())
        invalid("pickup_beats 必须为首小节长度以内的非负数");
    const double origin=number(layout.get("grid_origin",pickup),Num::floating);
    if (origin<0.) invalid("grid_origin 必须是非负有限数");
}
Curve::Curve(const Json& anchors) {
    array(anchors,"锚点必须是数组");
    seconds.reserve(anchors.size()); beats.reserve(anchors.size());
    for (const auto& a : anchors) {
        seconds.push_back(number(required(a,"seconds"),Num::floating,"锚点必须有限且严格递增"));
        beats.push_back(number(required(a,"beat"),Num::floating,"锚点必须有限且严格递增"));
    }
    validate();
}
Curve::Curve(std::vector<double> s, std::vector<double> q) : seconds(std::move(s)), beats(std::move(q)) { validate(); }
void Curve::validate() const {
    if (seconds.size()!=beats.size() || seconds.size()<2) invalid("Tempo Map 至少需要两个锚点");
    if (seconds.front()!=0. || beats.front()!=0.) invalid("首个锚点必须是 seconds=0, beat=0，以保留音频起点");
    for (std::size_t i=1; i<seconds.size(); ++i) {
        if (!std::isfinite(seconds[i]) || !std::isfinite(beats[i]) ||
            !(seconds[i]>seconds[i-1]) || !(beats[i]>beats[i-1])) invalid("锚点必须有限且严格递增");
        const double us=(seconds[i]-seconds[i-1])/(beats[i]-beats[i-1])*1e6;
        if (!(1.<=us && us<=16777215.)) invalid("速度超出 MIDI 可表达范围");
    }
}
Curve checked_curve(const Json& tempo) {
    const Layout layout(tempo);
    Curve curve(required(tempo,"anchors"));
    validate_layout(layout);
    return curve;
}
double interpolate_coordinates(const std::vector<double>& xs,const std::vector<double>& ys,double value) {
    const auto upper=std::upper_bound(xs.begin(),xs.end(),value);
    const auto i=upper==xs.begin() ? std::size_t(0) : std::min(static_cast<std::size_t>(upper-xs.begin()-1),xs.size()-2);
    return finite(ys[i]+(value-xs[i])*(ys[i+1]-ys[i])/(xs[i+1]-xs[i]),"nonfinite interpolation");
}
double timing_query(const Curve& curve, InputAxis axis, double value, bool signed_edge) {
    if (!std::isfinite(value) || (!signed_edge && value<0.)) invalid(clock_value_error);
    const bool from_seconds=axis==InputAxis::seconds;
    const auto& xs=from_seconds ? curve.seconds : curve.beats;
    const auto& ys=from_seconds ? curve.beats : curve.seconds;
    return interpolate_coordinates(xs,ys,value);
}
std::size_t signature_index(const Json& rows, double beat, double epsilon) {
    const Json limit=beat+epsilon;
    const auto next=std::upper_bound(rows.begin(),rows.end(),limit,[](const Json& q,const Json& row){
        return numeric_less(q,required(row,"beat"));
    });
    if (next==rows.begin()) invalid("找不到此处的拍号");
    return static_cast<std::size_t>(next-rows.begin()-1);
}

std::optional<double> confirmed_end(Layout layout, const Curve& curve) {
    const auto& end=layout.get("score_end");
    if (end.is_null()) return std::nullopt;
    if (!end.is_object() || !text_is(get(end,"origin"),"editor_score_end"))
        invalid("谱面终点缺少明确确认来源，请重新设置或取消终点");
    constexpr auto reason="谱面终点必须在当前音频范围内且晚于起点";
    const double seconds=number(get(end,"seconds"),Num::strict,reason);
    if (!(0.<seconds && seconds<=curve.seconds.back())) invalid(reason);
    const double beat=timing_query(curve,InputAxis::seconds,seconds);
    if (beat<=number(layout.get("first_full_bar_beat",0),Num::floating)+edit_eps)
        invalid("谱面终点必须晚于首个完整小节起点");
    return beat;
}

namespace {
struct ShortRange { double start, end; std::size_t order; };

bool revised_opening(Layout layout) {
    const auto& policy=layout.get("opening_policy");
    const auto* keep=find(policy,"keep_before_bar1");
    return keep && keep->is_boolean();
}

bool explicit_opening_meter(const Json& row) {
    return text_is(get(row,"origin"),"editor_intent") || get(row,"smart_measure").is_object();
}

Json opening_meter_sections(Layout layout) {
    const double full=number(layout.get("first_full_bar_beat",0),Num::floating);
    const auto& raw=layout.rows();
    if (!revised_opening(layout) || full<=edit_eps) return raw;
    Json sections=Json::array();
    const auto& saved=get(layout.get("opening_policy"),"bar1_meter");
    auto inherited=raw.at(signature_index(raw,full,edit_eps));
    // Keep the underlying model/analyzer rows intact. The default before
    // bar 1 is one free span; actual authored and existing smart labels can
    // define its sub-spans. Raw meter on/after bar 1 always wins over a fallback.
    if (!explicit_opening_meter(raw.front())) {
        sections.push_back(Json{{"beat",0.},{"numerator",raw.front().at("numerator")},
            {"denominator",raw.front().at("denominator")},{"free",true},{"opening_prefix",true}});
    }
    bool boundary=false;
    for (const auto& src:raw) {
        const double q=number(src.at("beat"),Num::floating);
        if (q<full-edit_eps) {
            if (!explicit_opening_meter(src)) continue;
            auto row=src;
            row["opening_prefix"]=true;
            sections.push_back(std::move(row));
        } else {
            if (!boundary && q>full+edit_eps) {
                const auto& meter=saved.is_object()?saved:inherited;
                Json first{{"beat",full},{"numerator",meter.at("numerator")},
                    {"denominator",meter.at("denominator")}};
                if (truth(get(meter,"free"))) first["free"]=true;
                sections.push_back(std::move(first));
            }
            if (std::abs(q-full)<=edit_eps) {
                auto row=src;row["beat"]=full;
                sections.push_back(std::move(row));
            } else sections.push_back(src);
            boundary=true;
        }
    }
    if (!boundary) {
        const auto& meter=saved.is_object()?saved:inherited;
        Json first{{"beat",full},{"numerator",meter.at("numerator")},{"denominator",meter.at("denominator")}};
        if (truth(get(meter,"free"))) first["free"]=true;
        sections.push_back(std::move(first));
    }
    return sections;
}


template<class Emit>
void walk_bars(Layout layout, const Curve& curve, double last, std::optional<double> end, bool local, Emit emit) {
    if (end && last>*end+bar_eps)
        invalid("记谱音符跨过已确认谱面终点；请后移或取消终点，或明确编辑该音符，不会自动裁尾");
    const auto rows=opening_meter_sections(layout);
    const double pickup=number(layout.get("pickup_beats",0),Num::floating);
    const double first_full=number(layout.get("first_full_bar_beat",0),Num::floating);
    double position=0.;
    if (text_is(layout.get("opening_mode"),"normal") && !layout.get("score_start_seconds").is_null())
        position=timing_query(curve,InputAxis::seconds,number(layout.get("score_start_seconds"),Num::floating));
    std::vector<ShortRange> shorts;
    std::vector<double> boundaries;
    for (const auto& a : array(layout.get("short_bar_annotations",empty_array()))) {
        const double x=timing_query(curve,InputAxis::seconds,number(required(a,"start_seconds"),Num::floating));
        const double y=timing_query(curve,InputAxis::seconds,number(required(a,"end_seconds"),Num::floating));
        shorts.push_back({x,y,shorts.size()}); boundaries.push_back(x); boundaries.push_back(y);
    }
    std::sort(shorts.begin(),shorts.end(),[](const ShortRange& a,const ShortRange& b){ return a.start<b.start; });
    std::sort(boundaries.begin(),boundaries.end());
    boundaries.erase(std::unique(boundaries.begin(),boundaries.end()),boundaries.end());

    std::size_t sig=0, index=0;
    bool first=true;
    const double inf=std::numeric_limits<double>::infinity();
    while (end ? position<*end-edit_eps : (position<=last+edit_eps || (local && first))) {
        if (local && !first && position>last+edit_eps) break;
        const Json limit=position+edit_eps;
        while (sig+1<rows.size() && !numeric_less(limit,required(rows[sig+1],"beat"))) ++sig;
        const auto& row=rows[sig];
        const bool free=truth(get(row,"free"));
        if (free && position>=curve.beats.back()-edit_eps) break;
        Meter meter=meter_of(row);
        const auto& raw_d=required(row,"denominator");
        const double next_signature=sig+1<rows.size() ? number(required(rows[sig+1],"beat"),Num::arithmetic) : inf;
        double length=first && pickup!=0. ? pickup : meter.length();
        double change=position<first_full-edit_eps ? std::min(next_signature,first_full) : next_signature;
        // Match the first annotation in document order, even if several
        // starts fall within the reference tolerance.
        const auto from=std::lower_bound(shorts.begin(),shorts.end(),position,[](const ShortRange& r,double p){
            return p-r.start>=bar_eps;
        });
        const ShortRange* match=nullptr;
        for (auto i=from; i!=shorts.end() && std::abs(position-i->start)<bar_eps; ++i)
            if (!match || i->order<match->order) match=&*i;
        if (match) length=match->end-match->start;
        const auto next_short=std::upper_bound(boundaries.begin(),boundaries.end(),position+bar_eps);
        if (next_short!=boundaries.end()) change=std::min(change,*next_short);
        if (change<position+length-bar_eps) length=change-position;
        if (free) {
            const double free_end=std::min(sig+1<rows.size() ? next_signature : curve.beats.back(),
                                           end.value_or(curve.beats.back()));
            length=free_end-position;
            if (length<=edit_eps) break;
        }
        bool integer_label=raw_d.is_number_integer();
        std::string label;
        if (end && position+length>*end+edit_eps) {
            length=*end-position;
            meter=short_meter(length,denominator(raw_d,true,boundary_meter_error));
            label=std::to_string(meter.n)+"/"+std::to_string(meter.d);
            integer_label=true;
        } else label=meter_label(meter,raw_d);
        const double next=position+length;
        if (!(next>position) || !std::isfinite(next)) invalid("小节边界必须有限且向前推进");
        emit(index++,Bar{position,length,meter,std::move(label),integer_label});
        position=next;
        first=false;
    }
}

void check_strict_bars(Layout layout, const Curve& curve, const Bars& geometry) {
    const double pickup=number(layout.get("pickup_beats",0),Num::floating);
    const double first=number(layout.get("first_full_bar_beat",pickup),Num::floating);
    const auto& permitted=array(layout.get("explicit_partial_measures",empty_array()));
    for (std::size_t i=0; i<geometry.items.size(); ++i) {
        const auto& b=geometry.items[i];
        // rebar_edit parses the displayed meter as two integers. TempoMap
        // itself intentionally accepts e.g. a denominator stored as 4.0.
        if (!b.integer_label) invalid("小节拍号必须可以解析为整数拍号");
        const bool opening=(i==0 && pickup>0. && std::abs(b.length-pickup)<bar_eps) ||
                           (first>0. && std::abs(b.end()-first)<bar_eps && b.start<first);
        const bool prelude=revised_opening(layout) && b.start<first-bar_eps && b.end()<=first+bar_eps;
        if (std::abs(b.length-b.meter.length())<=bar_eps || opening || prelude || b.end()>=geometry.last_beat-bar_eps) continue;
        const double start=timing_query(curve,InputAxis::beat,b.start);
        const double stop=timing_query(curve,InputAxis::beat,b.end());
        const bool explicit_partial=std::any_of(permitted.begin(),permitted.end(),[&](const Json& r){
            object(r,"不完整小节标注必须是对象");
            return std::abs(start-number(get(r,"start_seconds",-1),Num::arithmetic))<bar_eps &&
                   std::abs(stop-number(get(r,"end_seconds",-1),Num::arithmetic))<bar_eps;
        });
        if (!explicit_partial) invalid("现有第 "+std::to_string(i+1)+" 小节在 "+format_3f(stop)+" 秒形成 "+
            format_g(b.length)+" 个四分音符单位的不完整普通小节；请返回合法版本后重划，不会自动覆盖旧稿");
    }
}

double flag_phase(Layout layout, const Json& rows, std::size_t i) {
    const auto& start=required(rows[i],"beat");
    return number(i ? start : layout.get("first_full_bar_beat",layout.get("grid_origin",layout.get("pickup_beats",start))),Num::floating);
}
} // namespace

Bars timing_query(Layout layout, const Curve& curve, BarsRequest request) {
    if (request.strict && !request.ignore_score_end) {
        if (const auto* e=layout.find("score_end"); e && !e->is_object()) invalid("谱面终点必须是对象");
    }
    const auto end=request.ignore_score_end ? std::optional<double>{} : confirmed_end(layout,curve);
    const double last=request.last_beat.value_or(end.value_or(curve.beats.back()));
    Bars result{end.value_or(last),{}};
    walk_bars(layout,curve,last,end,false,[&](std::size_t,Bar b){ result.items.push_back(std::move(b)); });
    if (request.strict) check_strict_bars(layout,curve,result);
    return result;
}
Json timing_query(Layout layout, const Curve& curve, EndingMeasure) {
    const auto end=confirmed_end(layout,curve);
    if (!end) return nullptr;
    std::optional<Bar> previous;
    walk_bars(layout,curve,*end,std::nullopt,false,[&](std::size_t,Bar b){
        if (b.start<*end-edit_eps) previous=std::move(b);
    });
    if (!previous) invalid("谱面终点之前没有可表示的小节");
    const auto& rows=layout.rows();
    const auto& surrounding=rows[signature_index(rows,previous->start,edit_eps)];
    const double length=*end-previous->start;
    const bool partial=length<previous->length-edit_eps;
    const auto& raw_d=required(surrounding,"denominator");
    Json meter=partial ? meter_json(short_meter(length,denominator(raw_d,true,boundary_meter_error))) :
        Json{{"numerator",required(surrounding,"numerator")},{"denominator",raw_d}};
    return {{"start",previous->start},{"end",*end},{"length",length},{"partial",partial},
            {"meter",std::move(meter)},{"surrounding",surrounding}};
}

std::vector<Line> boundary_lines(Layout layout, const Curve& curve, const Bars& geometry) {
    const double first=number(layout.get("first_full_bar_beat",layout.get("pickup_beats",0)),Num::floating);
    const auto& bars=geometry.items;
    const auto found=std::find_if(bars.begin(),bars.end(),[&](const Bar& b){ return std::abs(b.start-first)<bar_eps; });
    const auto first_index=found==bars.end() ? std::size_t{0} : static_cast<std::size_t>(found-bars.begin());
    std::vector<Line> lines;
    lines.reserve(bars.size());
    bool has_last=false;
    for (std::size_t i=0; i<bars.size(); ++i) {
        const auto& b=bars[i];
        if (b.start>geometry.last_beat+bar_eps) break;
        lines.push_back({b.start,timing_query(curve,InputAxis::beat,b.start),b.label,i,
            static_cast<std::int64_t>(i)-static_cast<std::int64_t>(first_index)+1,i<first_index,false});
        has_last=has_last || std::abs(b.start-geometry.last_beat)<bar_eps;
    }
    if (!bars.empty() && !has_last && std::abs(bars.back().end()-geometry.last_beat)<bar_eps) {
        const auto i=bars.size();
        lines.push_back({geometry.last_beat,timing_query(curve,InputAxis::beat,geometry.last_beat),bars.back().label,i,
            static_cast<std::int64_t>(i)-static_cast<std::int64_t>(first_index)+1,false,true});
    }
    return lines;
}
std::string tempo_sha(const Json& tempo) { Sha256 sha; write_canonical(sha,tempo); return sha.finish(); }
std::string line_id(const std::string& revision,const std::string& sha,const Line& line) {
    return tempo_sha(Json::array({revision,sha,line.index,line.beat}));
}
const std::string& revision_value(const Json& v) {
    const auto& revision=text(v,"缺少当前编辑版本");
    if (revision.empty()) invalid("缺少当前编辑版本");
    return revision;
}
bool on_flag_grid(Layout layout, double beat) {
    const auto& rows=layout.rows();
    const auto i=signature_index(rows,beat,bar_eps);
    const double units=(beat-flag_phase(layout,rows,i))/meter_of(rows[i]).unit();
    return std::abs(units-round_even(units))<=bar_eps;
}
FlagNeighbors flag_neighbors(Layout layout, const Curve& curve, double beat) {
    if (!(curve.beats.front()+bar_eps<beat && beat<curve.beats.back()-bar_eps)) invalid("不能拖动音频首尾边界。");
    double left=curve.beats.front(), right=curve.beats.back();
    const auto include=[&](double q){
        if (q<beat-bar_eps) left=std::max(left,q);
        if (q>beat+bar_eps) right=std::min(right,q);
    };
    const auto& rows=layout.rows();
    for (std::size_t i=0; i<rows.size(); ++i) {
        const double start=number(required(rows[i],"beat"),Num::arithmetic);
        const double stop=i+1<rows.size() ? number(required(rows[i+1],"beat"),Num::arithmetic) : curve.beats.back();
        const double phase=flag_phase(layout,rows,i), step=meter_of(rows[i]).unit();
        const double k=std::floor(finite((beat-phase)/step));
        for (const double j : {k-1.,k,k+1.,k+2.}) {
            const double q=phase+j*step;
            if (start-bar_eps<=q && q<=stop+bar_eps) include(q);
        }
        include(start);
    }
    return {left,right,timing_query(curve,InputAxis::beat,left),timing_query(curve,InputAxis::beat,right)};
}

namespace {

bool switch_arg(const Json& args, const char* key, bool fallback=false) {
    if (const auto* v=find(args,key)) {
        if (!v->is_boolean()) invalid(std::string(key)+" 必须是布尔值");
        return v->get<bool>();
    }
    return fallback;
}
Json bar_arrays(const Bars& bars) {
    Json starts=Json::array(), lengths=Json::array(), meters=Json::array();
    starts.get_ref<Json::array_t&>().reserve(bars.items.size());
    lengths.get_ref<Json::array_t&>().reserve(bars.items.size());
    meters.get_ref<Json::array_t&>().reserve(bars.items.size());
    for (const auto& b : bars.items) { starts.push_back(b.start); lengths.push_back(b.length); meters.push_back(b.label); }
    return Json::array({std::move(starts),std::move(lengths),std::move(meters)});
}
Json get_free_ranges(Layout layout,const Curve& curve) {
    const auto& rows=layout.rows();
    Json result=Json::array();
    const Json last=curve.beats.back();
    for (std::size_t i=0; i<rows.size(); ++i) {
        if (!truth(get(rows[i],"free"))) continue;
        const auto& a=required(rows[i],"beat");
        const auto& b=i+1<rows.size() ? required(rows[i+1],"beat") : last;
        const double lo=number(a,Num::arithmetic), hi=number(b,Num::arithmetic);
        if (hi>lo+edit_eps) result.push_back({{"start_seconds",timing_query(curve,InputAxis::beat,lo)},
            {"end_seconds",timing_query(curve,InputAxis::beat,hi)},{"start_beat",a},{"end_beat",b},{"source","user"}});
    }
    return result;
}

Meter meter_string(const Json& v) {
    constexpr auto reason="拍号格式应为 4/4 或 6/8";
    const std::string_view s=text(v,reason);
    const auto slash=s.find('/');
    if (slash==std::string_view::npos || s.find('/',slash+1)!=std::string_view::npos) invalid(reason);
    const Json row={{"numerator",parse_integer(s.substr(0,slash),reason).json()},
                    {"denominator",parse_integer(s.substr(slash+1),reason).json()}};
    return meter_of(row,true);
}
Json scaled_value(const Json& v, const Json& factor) {
    if (!factor.is_number_float() && v.is_string()) {
        const auto& s=v.get_ref<const std::string&>();
        return s+s;
    }
    if (!factor.is_number_float() && (v.is_number_integer() || v.is_boolean())) {
        auto i=integer_value(v,true);
        if (i.magnitude>std::numeric_limits<std::uint64_t>::max()/2) invalid("整数超出 JSON 整数范围");
        i.magnitude*=2;
        return i.json();
    }
    return finite(number(v,Num::arithmetic)*number(factor,Num::arithmetic));
}
Json tempo_factory(std::string_view name, const Json& args) {
    if (name=="fixed_tempo") {
        const double bpm=number(get(args,"bpm"),Num::floating,"BPM 需要在 10–600 之间");
        if (bpm<10. || bpm>600.) invalid("BPM 需要在 10–600 之间");
        const auto m=meter_string(get(args,"meter","4/4"));
        const auto& duration=required(args,"duration");
        const double seconds=std::max(1.,number(duration,Num::arithmetic));
        Json result={{"version",1},{"source","manual"},
            {"anchors",Json::array({Json{{"seconds",0},{"beat",0}},Json{{"seconds",seconds>1. ? duration : Json(1.)},{"beat",finite(seconds*bpm/60.)}}})},
            {"time_signatures",Json::array({Json{{"beat",0},{"numerator",m.n},{"denominator",m.d}}})},{"warnings",Json::array()}};
        checked_curve(result);
        return result;
    }
    Json result=object(required(args,"tempo"),"速度图必须是对象。");
    if (name=="lock_meter") {
        const auto m=meter_string(get(args,"meter"));
        Json previous=get(result,"time_signatures",empty_array());
        result["time_signatures"]=Json::array({Json{{"beat",0.},{"numerator",m.n},{"denominator",m.d}}});
        result["meter_policy"]={{"mode","locked"},{"meter",std::to_string(m.n)+"/"+std::to_string(m.d)},
                                {"previous_time_signatures",std::move(previous)}};
    } else {
        const auto& factor=get(args,"factor");
        const double f=number(factor,Num::arithmetic,"节拍倍率仅支持 0.5 / 1 / 2");
        if (f!=.5 && f!=1. && f!=2.) invalid("节拍倍率仅支持 0.5 / 1 / 2");
        if (f!=1.) {
            const auto scale=[&](Json& o,const char* k){ o[k]=scaled_value(required(o,k),factor); };
            if (find(result,"first_full_bar_beat")) scale(result,"first_full_bar_beat");
            if (get(result,"opening_phase").is_object() && find(result["opening_phase"],"beat")) scale(result["opening_phase"],"beat");
            if (get(result,"pulse_projection").is_object()) {
                auto& report=result["pulse_projection"];
                const Json applied=scaled_value(get(report,"post_projection_beat_scale",1.),factor);
                report["post_projection_beat_scale"]=applied;
                report["coordinate_scope"]="original_decoder_projection_before_explicit_beat_scale";
                if (const auto* span=find(report,"quarter_span_per_pulse"))
                    report["effective_quarter_span_per_pulse"]=finite(number(*span,Num::arithmetic)*number(applied,Num::arithmetic));
                report["original_grouping_is_current"]=false;
            }
            array(required(result,"anchors"));
            array(required(result,"time_signatures"));
            for (auto& a : result["anchors"]) scale(a,"beat");
            auto& rows=result["time_signatures"];
            for (auto& row : rows) scale(row,"beat");
            if (rows.empty()) invalid("拍号列表必须从 beat=0 开始");
            const double size=meter_of(rows.front()).length();
            const Json opening=scaled_value(get(result,"pickup_beats",0),factor);
            const double q=number(opening,Num::arithmetic);
            result["pickup_beats"]=positive_mod(q,size);
            if (q!=0.) {
                if (!find(result,"first_full_bar_beat")) result["first_full_bar_beat"]=opening;
                if (q>=size) {
                    // Reacquire the row array after updating the ordered object.
                    auto& signatures=result["time_signatures"];
                    const auto equal=[](const Json& a,const Json& b){ return !numeric_less(a,b) && !numeric_less(b,a); };
                    if (std::none_of(signatures.begin(),signatures.end(),[&](const Json& r){ return equal(required(r,"beat"),opening); })) {
                        Json row=signatures.front(); row["beat"]=opening; signatures.push_back(std::move(row));
                    }
                    std::stable_sort(signatures.begin(),signatures.end(),[](const Json& a,const Json& b){
                        return numeric_less(required(a,"beat"),required(b,"beat"));
                    });
                    Json merged=Json::array();
                    for (auto& r : signatures) {
                        if (!merged.empty() && equal(required(merged.back(),"beat"),required(r,"beat"))) merged.back()=std::move(r);
                        else merged.push_back(std::move(r));
                    }
                    signatures=std::move(merged);
                }
            }
            result["grid_origin"]=scaled_value(get(result,"grid_origin",0),factor);
            result["allow_partial_measures"]=true;
            if (!find(result,"warnings")) result["warnings"]=Json::array();
            array(result["warnings"]);
            result["warnings"].push_back("已改变节拍解释；拍号与演奏秒时间保留，可能产生不完整边界小节。");
        }
    }
    checked_curve(result);
    ensure_default_meter(result);
    return result;
}

} // namespace
} // namespace astra::editor::timing_detail

namespace astra::editor {

Json timing_query(std::string_view name,const Json& args) {
    using namespace timing_detail;
    object(args);
    if (name=="convert_many") {
        const Curve curve(required(args,"anchors"));
        Json beats=Json::array(),seconds=Json::array();
        for(const auto& v:array(required(args,"seconds"))) beats.push_back(timing_detail::timing_query(curve,InputAxis::seconds,number(v,Num::floating)));
        for(const auto& v:array(required(args,"beats"))) seconds.push_back(timing_detail::timing_query(curve,InputAxis::beat,number(v,Num::floating)));
        return {{"beats",std::move(beats)},{"seconds",std::move(seconds)}};
    }
    if (name=="interpolate_many") {
        const auto& x=array(required(args,"x"));const auto& y=array(required(args,"y"));
        if(x.size()!=y.size()||x.size()<2) invalid("interpolation needs equal arrays of at least two coordinates");
        std::vector<double> xs,ys;xs.reserve(x.size());ys.reserve(y.size());
        for(std::size_t i=0;i<x.size();++i){
            xs.push_back(number(x[i],Num::floating));ys.push_back(number(y[i],Num::floating));
            if(i&&!(xs[i]>xs[i-1]))invalid("interpolation x must increase");
        }
        Json values=Json::array();
        for(const auto& v:array(required(args,"values"))){
            double q=number(v,Num::floating);if(q<0.)invalid("negative interpolation coordinate");
            values.push_back(interpolate_coordinates(xs,ys,q));
        }
        return values;
    }
    if (name=="round_even") {
        const auto& value=required(args,"value");
        if (value.is_number_integer()) return value;
        return integer_double(round_even(number(value,Num::arithmetic))).json();
    }
    if (name=="round_decimal") {
        const auto& x=required(args,"value");
        const auto& digits=required(args,"digits");
        if (!digits.is_number_integer() || number(digits)<0.) invalid("digits 必须是非负整数");
        if (x.is_number_integer() || x.is_boolean()) return integer_value(x,true).json();
        const double v=number(x);
        const double n=number(digits);
        if (n>323.) return v;
        return parse_float(float_chars(v,std::chars_format::fixed,static_cast<int>(n)),"无法表示舍入结果");
    }
    if (name=="boundary_time_signature") {
        const auto surrounding=array(get(args,"surrounding_denominators",Json::array({4})),boundary_meter_error);
        if (surrounding.empty()) invalid(boundary_meter_error);
        int preferred=128;
        for (const auto& d : surrounding) preferred=std::min(preferred,denominator(d,true,boundary_meter_error));
        return meter_json(short_meter(number(get(args,"span_quarters"),Num::edit,boundary_meter_error),preferred));
    }
    if (name=="preview_meter_edit" || name=="preview_roll_edit" || name=="preview_constant_tempo" || name=="reference_grid_points")
        return structural_preview_query(name,args);
    if (name=="fixed_tempo" || name=="lock_meter" || name=="scale_tempo") return tempo_factory(name,args);

    if (name=="editor_clock" || name=="editor_axis") {
        const auto& projectionTempo=get(args,"tempo");
        const auto& anchors=get(projectionTempo,"anchors");
        std::optional<Curve> projectionCurve;
        if(anchors.is_array() && anchors.size()>1) projectionCurve.emplace(anchors);
        const auto& map=array(get(args,"tempo_map",empty_array()));
        // Read-only MIDI uses the decoder's piecewise tempo rows, not a
        // fabricated beat extent. No metadata/anchor validation is invented.
        if(!projectionCurve)for(std::size_t i=0;i<map.size();++i){
            const auto& r=object(map[i]);
            const double seconds=number(required(r,"seconds")),beat=number(required(r,"beat"));
            if(number(required(r,"tempo"))<=0)invalid("nonpositive MIDI tempo");
            if(i && (seconds<number(map[i-1].at("seconds")) || beat<number(map[i-1].at("beat"))))invalid("unordered MIDI tempo map");
        }
        auto convert=[&](InputAxis axis,double v){
            if(projectionCurve)return timing_detail::timing_query(*projectionCurve,axis,v,true);
            const bool seconds=axis==InputAxis::seconds;
            const char* key=seconds?"seconds":"beat";
            std::size_t lo=0,hi=map.size();
            while(lo+1<hi){const auto mid=(lo+hi)/2;if(number(map[mid].at(key))<=v)lo=mid;else hi=mid;}
            const double t=map.empty()?0.:number(map[lo].at("seconds"));
            const double q=map.empty()?0.:number(map[lo].at("beat"));
            const double us=map.empty()?500000.:number(map[lo].at("tempo"));
            // MIDI tempo rows encode slopes (microseconds per quarter), not
            // interpolation anchors. Preserve decoder discontinuities and JS
            // arithmetic without synthesizing rounded endpoint coordinates.
            return finite(seconds ? q+(v-t)*1e6/us : t+(v-q)*us/1e6);
        };
    if (name=="editor_clock") {
        const bool seconds=find(args,"seconds")!=nullptr;
        return convert(seconds?InputAxis::seconds:InputAxis::beat, number(required(args,seconds?"seconds":"beat")));
    }
    if (name=="editor_axis") {
        const auto& axis=object(required(args,"axis"));
        const double width=number(required(axis,"width"));
        const double duration=number(required(axis,"seconds_extent"));
        if(width<=0 || duration<=0) invalid("axis width and duration must be positive");
        const auto& mode=text(required(axis,"mode"),"axis mode required");
        if(mode!="seconds" && mode!="bars") invalid("unknown axis mode");
        const bool linear=mode=="seconds";
        const double extent=linear?duration:std::max(std::numeric_limits<double>::epsilon(),
            convert(InputAxis::seconds,duration));
        if(const auto* x=find(args,"x")) {
            const double unit=std::clamp(number(*x)*extent/std::max(1.,width),0.,extent);
            return linear?unit:convert(InputAxis::beat,unit);
        }
        const double seconds=number(required(args,"seconds"));
        return (linear?seconds:convert(InputAxis::seconds,seconds))*width/extent;
    }
    }
    const auto& tempo=required(args,"tempo");
    const std::string* revision=nullptr;
    if (name=="boundaries") revision=&revision_value(get(args,"revision"));
    const auto curve=checked_curve(tempo);
    const Layout layout(tempo);
    if (name=="opening_meter_sections") return opening_meter_sections(layout);
    if (name=="editor_bar_at" || name=="editor_grid_sections") {
        const auto meters=opening_meter_sections(layout);
        Json sections=meters;
        const auto first=meters.at(0);
        const double pickup=number(layout.get("pickup_beats",0.));
        const double full=number(layout.get("first_full_bar_beat",0.));
        auto beat=[](const Json& row){return number(required(row,"beat"));};
        auto sort=[&]{std::stable_sort(sections.begin(),sections.end(),[&](const Json&a,const Json&b){return beat(a)<beat(b);});};
        auto at=[&](double b,double eps){Json row=first;for(const auto&r:sections)if(beat(r)<=b+eps)row=r;return row;};
        if(pickup>0) {
            Json fullMeter=first;for(const auto&r:meters)if(beat(r)<=pickup+1e-7)fullMeter=r;
            Json shortMeter=first;shortMeter["beat"]=0.;shortMeter["numerator"]=pickup*number(first.at("denominator"))/4.;shortMeter["pickup"]=true;
            fullMeter["beat"]=pickup;fullMeter["pickup"]=false;
            sections=Json::array({shortMeter,fullMeter});
            for(std::size_t i=1;i<meters.size();++i)if(beat(meters[i])>pickup+1e-7)sections.push_back(meters[i]);
        }
        if(full>0) {
            bool exists=false;for(const auto&r:sections)if(std::abs(beat(r)-full)<1e-7)exists=true;
            if(!exists){auto row=first;for(const auto&r:sections)if(beat(r)<full)row=r;if(truth(get(row,"pickup"))){row["numerator"]=first.at("numerator");row["denominator"]=first.at("denominator");}row["beat"]=full;row["pickup"]=false;sections.push_back(row);sort();}
        }
        for(const auto& mark:layout.get("short_bar_annotations",empty_array())) {
            const double a=timing_detail::timing_query(curve,InputAxis::seconds,number(mark.at("start_seconds")),true);
            const double b=timing_detail::timing_query(curve,InputAxis::seconds,number(mark.at("end_seconds")),true);
            if(!(b>a))continue;
            auto source=at(a,1e-7),restore=source,shortMeter=source;
            restore["beat"]=b;restore["pickup"]=false;restore["short"]=false;
            shortMeter["beat"]=a;shortMeter["numerator"]=(b-a)*number(source.at("denominator"))/4.;shortMeter["short"]=true;shortMeter["pickup"]=false;
            bool hasEnd=false;for(const auto&r:sections)if(std::abs(beat(r)-b)<1e-7)hasEnd=true;
            Json kept=Json::array();for(const auto&r:sections)if(beat(r)<a-1e-7||beat(r)>=b-1e-7)kept.push_back(r);
            sections=std::move(kept);sections.push_back(shortMeter);if(!hasEnd)sections.push_back(restore);sort();
        }
        for(std::size_t i=0;i<sections.size();++i)if(truth(get(sections[i],"free"))) {
            const double end=i+1<sections.size()?beat(sections[i+1]):curve.beats.back();
            if(end>beat(sections[i]))sections[i]["numerator"]=(end-beat(sections[i]))*number(sections[i].at("denominator"))/4.;
        }
        // Publish the same structural origins once for the interactive view.
        // The live command always resolves its own endpoint on the native head.
        if (name=="editor_grid_sections") return sections;
        const double q=number(required(args,"beat"));
        std::size_t i=0;while(i+1<sections.size()&&beat(sections[i+1])<=q+1e-8)++i;
        const auto& m=sections[i];const double length=number(m.at("numerator"))*4/number(m.at("denominator"));
        if(!(length>0))invalid("invalid editor meter span");
        const double start=beat(m)+std::max(0.,std::floor((q-beat(m)+1e-8)/length))*length;
        const double end=std::min(start+length,i+1<sections.size()?beat(sections[i+1]):std::numeric_limits<double>::infinity());
        return {{"start",start},{"end",end},{"index",i},{"meter",m}};
    }
    if (name=="validate_tempo") { Json out=tempo; ensure_default_meter(out); return out; }
    if (name=="to_beat") return timing_detail::timing_query(curve,InputAxis::seconds,number(get(args,"seconds"),Num::arithmetic,clock_value_error));
    if (name=="to_seconds") return timing_detail::timing_query(curve,InputAxis::beat,number(get(args,"beat"),Num::arithmetic,clock_value_error));
    if (name=="to_beats" || name=="to_seconds_many") {
        const auto axis=name=="to_beats" ? InputAxis::seconds : InputAxis::beat;
        const auto& values=array(required(args,axis==InputAxis::seconds ? "seconds" : "beats"));
        Json result=Json::array(); result.get_ref<Json::array_t&>().reserve(values.size());
        for (const auto& v : values) result.push_back(timing_detail::timing_query(curve,axis,number(v,Num::arithmetic,clock_value_error)));
        return result;
    }
    if (name=="reference_bars" || name=="geometry") {
        BarsRequest request;
        request.ignore_score_end=switch_arg(args,"ignore_score_end");
        request.strict=name=="geometry" && switch_arg(args,"strict",true);
        if (name=="reference_bars") if (const auto* last=find(args,"last_beat")) request.last_beat=number(*last,Num::arithmetic);
        const auto bars=timing_detail::timing_query(layout,curve,request);
        auto columns=bar_arrays(bars);
        if (name=="reference_bars") return columns;
        return {{"starts",std::move(columns[0])},{"lengths",std::move(columns[1])},{"meters",std::move(columns[2])},
                {"last_beat",bars.last_beat},{"audio_end_beat",curve.beats.back()},{"audio_end_seconds",curve.seconds.back()}};
    }
    if (name=="boundaries") {
        const auto geometry=timing_detail::timing_query(layout,curve,BarsRequest{std::nullopt,false,true});
        const auto lines=boundary_lines(layout,curve,geometry);
        const auto sha=tempo_sha(tempo);
        Json out=Json::array(); out.get_ref<Json::array_t&>().reserve(lines.size());
        for (const auto& l : lines) {
            const std::string label=l.terminal ? "已确认曲尾小节线" : (l.opening ? "引子／弱起开始" : "第 "+std::to_string(l.bar_number)+" 小节开始");
            out.push_back({{"id",line_id(*revision,sha,l)},{"revision",*revision},{"tempo_sha256",sha},{"index",l.index},
                {"bar_number",l.bar_number},{"label",label},{"beat",l.beat},{"seconds",l.seconds},{"meter",l.meter},
                {"opening_boundary",l.opening}});
        }
        return out;
    }
    if (name=="bar_at") {
        const double beat=number(get(args,"beat"),Num::arithmetic,clock_value_error);
        if (beat<0.) invalid(clock_value_error);
        const auto end=switch_arg(args,"ignore_score_end") ? std::optional<double>{} : confirmed_end(layout,curve);
        std::optional<Bar> bar;
        std::size_t index=0;
        walk_bars(layout,curve,beat,end,true,[&](std::size_t i,Bar b){ index=i; bar=std::move(b); });
        if (!bar) invalid("没有可用的小节");
        return {{"start",bar->start},{"end",bar->end()},{"numerator",bar->meter.n},{"denominator",bar->meter.d},{"index",index}};
    }
    if (name=="free_ranges") return get_free_ranges(layout,curve);
    if (name=="confirmed_end_beat") { const auto end=confirmed_end(layout,curve); return end ? Json(*end) : Json(); }
    if (name=="ending_measure") return timing_detail::timing_query(layout,curve,EndingMeasure{});
    if (name=="validate_flag") {
        constexpr auto error="拍旗帜的位置无效。";
        const double seconds=number(get(args,"seconds"),Num::strict,error), beat=number(get(args,"beat"),Num::strict,error);
        if (!(curve.beats.front()-bar_eps<=beat && beat<=curve.beats.back()+bar_eps) ||
            std::abs(timing_detail::timing_query(curve,InputAxis::beat,beat)-seconds)>bar_eps)
            invalid("拍旗帜已变化，请重新选择。");
        if (!on_flag_grid(layout,beat)) invalid("请选择当前拍号下的完整拍位旗帜。");
        return nullptr;
    }
    if (name=="beat_flag_neighbors") {
        const auto n=flag_neighbors(layout,curve,number(get(args,"beat")));
        return {{"left_beat",n.left},{"right_beat",n.right},{"start_seconds",n.before},{"end_seconds",n.after}};
    }
    invalid("未知时间查询");
}

} // namespace astra::editor
