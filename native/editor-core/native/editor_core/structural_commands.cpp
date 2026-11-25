#include "timing_internal.h"

#include <algorithm>
#include <limits>
#include <tuple>
#include <utility>

namespace astra::editor::timing_detail {
namespace {

constexpr auto meter_flag_error="请输入有效拍号，例如 3/4 或 6/8";
constexpr auto meter_range_error="请输入有效拍号，例如 4/4 或 6/8";
constexpr auto count_error="小节数必须是 1–4096 的整数";
constexpr auto stale_rebar="所选小节线已过期，请在当前版本重新选择";
constexpr auto missing_lines="请选择当前版本中两条已有小节线";
constexpr double constant_tolerance=1e-6;

inline double beat(const Curve& c,double t) { return timing_query(c,InputAxis::seconds,t); }
inline double seconds(const Curve& c,double q) { return timing_query(c,InputAxis::beat,q); }
inline double raw_number(const Json& v) { return number(v,Num::arithmetic); }
inline double edit_number(const Json& v,std::string_view label) {
    return number(v,Num::edit,std::string(label)+" 必须是有限数值");
}
inline double strict_number(const Json& v,std::string_view label) {
    return number(v,Num::strict,std::string(label)+" 必须是有限数值");
}
inline double coord(const Json& row,const char* key) { return raw_number(required(row,key)); }
inline void erase_key(Json& j,const char* key) { j.erase(key); }
inline void sort_records(Json& rows,const char* key) {
    std::stable_sort(rows.begin(),rows.end(),[=](const Json& a,const Json& b){
        return numeric_less(required(a,key),required(b,key));
    });
}
inline void sort_samples(std::vector<double>& samples) {
    std::stable_sort(samples.begin(),samples.end());
    samples.erase(std::unique(samples.begin(),samples.end()),samples.end());
}
inline bool quick_same(double a,double b) { return std::abs(a-b)<=bar_eps; }
inline bool quick_meter(const Json& n) {
    return n.is_number_integer() && n.get<double>()>=2. && n.get<double>()<=4.;
}
inline Meter op_meter(const Json& op,std::string_view error,bool default_denominator) {
    return meter_of(Json{{"numerator",get(op,"numerator")},{"denominator",get(op,"denominator",default_denominator ? Json(4) : Json())}},true,error);
}
inline Json signature(double q,Meter m) { return {{"beat",q},{"numerator",m.n},{"denominator",m.d}}; }

// Old records stay detached; a sample which survives at exactly the same
// source second keeps its unrecognized fields. Known coordinates use the
// same types as newly computed Python coordinates.
Json sample_record(const Json& source,const Curve& old,double t,double q) {
    const auto it=std::lower_bound(old.seconds.begin(),old.seconds.end(),t);
    if (it==old.seconds.end() || *it!=t) return {{"seconds",t},{"beat",q}};
    Json row=required(source,"anchors")[static_cast<std::size_t>(it-old.seconds.begin())];
    row["seconds"]=t; row["beat"]=q;
    return row;
}
Json samples_at(const Json& source,const Curve& old,const Curve& next) {
    Json rows=Json::array(); rows.get_ref<Json::array_t&>().reserve(next.seconds.size());
    auto i=old.seconds.begin();
    const auto& original=required(source,"anchors");
    for (std::size_t j=0; j<next.seconds.size(); ++j) {
        const double t=next.seconds[j], q=next.beats[j];
        while (i!=old.seconds.end() && *i<t) ++i;
        Json row=i!=old.seconds.end() && *i==t ? original[static_cast<std::size_t>(i-old.seconds.begin())] : Json::object();
        row["seconds"]=t; row["beat"]=q;
        rows.push_back(std::move(row));
    }
    return rows;
}

double edit_time(const Json& v,const Curve& curve) {
    const double x=edit_number(v,"时间");
    if (x<0. || x>curve.seconds.back()+edit_eps) invalid("时间超出当前音频范围");
    return std::min(x,curve.seconds.back());
}
std::pair<double,double> edit_range(const Json& op,const Curve& curve) {
    const double start=edit_time(get(op,"start",0),curve), end=edit_time(get(op,"end",curve.seconds.back()),curve);
    if (end-start<=edit_eps) invalid("请指定有正时长的左右定位点");
    return {start,end};
}

std::string_view canonical_kind(std::string_view kind) {
    if (kind=="regroup") return "meter";
    if (kind=="move") return "move_point";
    if (kind=="insert") return "add_point";
    if (kind=="delete") return "remove_point";
    return kind;
}

Json normalize_rows(Json rows,bool quick) {
    sort_records(rows,"beat");
    Json out=Json::array(); out.get_ref<Json::array_t&>().reserve(rows.size());
    for (auto& row : rows) {
        if (!out.empty()) {
            const auto& a=required(out.back(),"beat");
            const auto& b=required(row,"beat");
            const double distance=std::abs(raw_number(a)-raw_number(b));
            const bool integer_difference=!a.is_number_float() && !b.is_number_float() &&
                                          (numeric_less(a,b) || numeric_less(b,a));
            if (!integer_difference && (quick ? distance<=bar_eps : distance<edit_eps)) {
                out.back()=std::move(row);
                continue;
            }
            if (quick && meter_of(out.back())==meter_of(row)) continue;
        }
        out.push_back(std::move(row));
    }
    return out;
}

void remap_structure(Json& result,const Curve& old,const Curve& next) {
    const auto remap=[&](const Json& q){ return beat(next,seconds(old,raw_number(q))); };
    for (auto& row : result["time_signatures"]) row["beat"]=remap(required(row,"beat"));
    for (const char* key : {"grid_origin","first_full_bar_beat"})
        if (const auto* q=find(result,key)) result[key]=remap(*q);
    if (const auto& q=get(result,"pickup_beats"); truth(q)) {
        const double full=remap(q);
        result["pickup_beats"]=positive_mod(full,meter_of(result["time_signatures"].front()).length());
        if (!find(result,"first_full_bar_beat")) result["first_full_bar_beat"]=full;
    }
}

std::string edit_meter_flag(Json& out,const Json& op,const Curve& clock,std::string_view kind) {
    const double at=edit_time(get(op,"seconds"),clock), q=beat(clock,at);
    auto& rows=out["time_signatures"];
    const auto locate=[&]{
        return std::find_if(rows.begin(),rows.end(),[&](const Json& r){ return std::abs(coord(r,"beat")-q)<edit_eps; });
    };
    auto it=locate();
    if (kind=="remove_meter_event") {
        if (it==rows.end()) invalid("此处没有拍号标记，请明确选中已有标记；范围变拍请使用选区操作");
        if (it==rows.begin()) invalid("不能删除起始拍号；可以修改它。");
        rows.erase(it);
        Json merged=Json::array();
        for (auto& row : rows) {
            if (!merged.empty() && !text_is(get(row,"origin"),"editor_first_bar") && meter_of(row)==meter_of(merged.back())) continue;
            merged.push_back(std::move(row));
        }
        rows=std::move(merged);
        return "已移除拍号标签并合并重复恢复标签；拍点、音符和踏板秒时间不变。";
    }
    if (kind=="add_meter_event") {
        if (it!=rows.end()) invalid("此小节已有拍号标签，请修改现有标签。");
    } else if (it==rows.end()) invalid("此处没有拍号标记，请明确选中已有标记；范围变拍请使用选区操作");
    const Meter m=op_meter(op,meter_flag_error,true);
    if (kind=="add_meter_event") {
        rows.push_back(signature(q,m));
        rows=normalize_rows(std::move(rows),false);
        it=locate();
        if (it==rows.end()) invalid("找不到新增拍号标记");
    }
    if (it==rows.begin() && raw_number(get(out,"pickup_beats",0))>=m.length())
        invalid("现有弱起长于新小节，请先调整弱起或使用完整引子模式");
    (*it)["numerator"]=m.n; (*it)["denominator"]=m.d;
    const double end=it+1==rows.end() ? clock.seconds.back() : seconds(clock,coord(*(it+1),"beat"));
    return "将 "+format_g(at)+" 秒小节首拍的标签设为 "+std::to_string(m.n)+"/"+std::to_string(m.d)+
        "，持续至 "+format_g(end)+" 秒的下一标签或曲尾；速度与演奏秒数不变";
}

std::string edit_meter_range(Json& out,const Json& op,const Curve& clock) {
    const auto [start,end]=edit_range(op,clock);
    const Meter m=op_meter(op,meter_range_error,true);
    const double left=beat(clock,start), right=beat(clock,end);
    auto& signatures=out["time_signatures"];
    if (!truth(get(op,"replace_changes",false)) && std::any_of(signatures.begin(),signatures.end(),[&](const Json& row){
        const double q=coord(row,"beat"); return left+edit_eps<q && q<right-edit_eps;
    })) invalid("范围内已有变拍；请缩小范围，或明确选择替换范围内拍号");
    Json restore=signatures[signature_index(signatures,right,edit_eps)];
    restore["beat"]=right;
    Json rows=Json::array();
    for (auto& r : signatures) {
        const double q=coord(r,"beat");
        if (q<left-edit_eps || q>=right-edit_eps) rows.push_back(std::move(r));
    }
    rows.push_back(signature(left,m));
    if (end<clock.seconds.back()-edit_eps) rows.push_back(std::move(restore));
    signatures=normalize_rows(std::move(rows),false);
    if (start==0. && raw_number(get(out,"pickup_beats",0))>=m.length())
        invalid("现有弱起长于新小节，请先将弱起改为完整引子或调整范围");
    out["allow_partial_measures"]=true;
    return "仅重分组为 "+std::to_string(m.n)+"/"+std::to_string(m.d)+"；速度及演奏秒数不变，范围结束恢复原拍号";
}

void meter_constraints(const Json& before,const Json& after,const Json& op,std::string_view kind,const Curve& clock) {
    Bars updated;
    try {
        validate_layout(after);
        updated=timing_query(Layout(after),clock,BarsRequest{std::nullopt,false,true});
    } catch (const std::invalid_argument& e) {
        invalid(std::string("只改拍号无法在当前拍点上组成完整小节；请选择小节线，或使用“从这里重新定拍”明确小节数。")+e.what());
    }
    const auto& scope=get(op,"scope");
    if (scope.is_null()) return;
    const auto prior=timing_query(Layout(before),clock,BarsRequest{std::nullopt,false,true});
    if (!scope.is_object() || !find(scope,"start") || !find(scope,"end")) invalid("编辑选区已失效，请重新框选。");
    const auto [left,right]=edit_range(scope,clock);
    if (kind!="meter") invalid("框内拍号修改须限定范围并在末端恢复，不能直接修改全局标签。");
    const auto [start,end]=edit_range(op,clock);
    if (start<left-edit_eps || end>right+edit_eps) invalid("拍号动作越过了编辑选区；框外内容保持不变。");
    const auto a=boundary_lines(before,clock,prior), b=boundary_lines(after,clock,updated);
    const auto exterior=[&](const Line& l){ return l.seconds<left-edit_eps || l.seconds>=right-edit_eps; };
    auto i=a.begin(), j=b.begin();
    for (;;) {
        i=std::find_if(i,a.end(),exterior); j=std::find_if(j,b.end(),exterior);
        if (i==a.end() && j==b.end()) return;
        if (i==a.end() || j==b.end() || i->meter!=j->meter || std::abs(i->seconds-j->seconds)>edit_eps)
            invalid("拍号修改会移动框外小节线或改变框外拍号；请调整选区内的划分。小节重新编号不受限制。");
        ++i; ++j;
    }
}

std::string edit_count(const Json& source,Json& out,const Json& op,const Curve& old,bool interpret) {
    const auto [start,end]=edit_range(op,old);
    const double left=beat(old,start), right=beat(old,end), span=right-left;
    double target;
    if (interpret) {
        const double f=edit_number(get(op,"factor"),"节拍解释倍率");
        if (f!=.5 && f!=1. && f!=2.) invalid("节拍解释仅支持 ½、1、×2");
        target=span*f;
    } else target=edit_number(get(op,"quarters",get(op,"beats")),"四分音符等值拍数");
    if (!(target>0.) || !(span>0.)) invalid("拍数跨度必须大于零");
    const double factor=target/span, delta=target-span;
    const auto& distribution=get(op,"distribution","preserve");
    const bool uniform=text_is(distribution,"uniform");
    if (!uniform && !text_is(distribution,"preserve")) invalid("段内分布应为 preserve 或 uniform");
    std::vector<double> samples;
    samples.reserve(old.seconds.size()+2);
    for (double t : old.seconds) if (!uniform || t<=start || t>=end) samples.push_back(t);
    samples.push_back(start); samples.push_back(end); sort_samples(samples);
    std::vector<double> beats; beats.reserve(samples.size());
    for (const double t : samples) {
        const double q=beat(old,t);
        beats.push_back(t<=start ? q : (t>=end ? q+delta : left+(q-left)*factor));
    }
    const Curve next(std::move(samples),std::move(beats));
    out["anchors"]=samples_at(source,old,next);
    remap_structure(out,old,next);
    out["allow_partial_measures"]=true;
    return "固定 "+format_g(start)+"–"+format_g(end)+" 秒，解释为 "+format_g(target)+" 个四分音符单位；后续音乐编号平移 "+
        format_g(delta,true)+"，音频位置不变";
}

std::string edit_pulse_times(const Json& source,Json& out,const Json& op,const Curve& old,Json& warnings) {
    const auto [start,end]=edit_range(op,old);
    const auto& raw=get(op,"beat_seconds");
    if (!raw.is_array() || raw.size()<2 || raw.size()>65) invalid("拍点候选需包含 1–64 个间隔及两端");
    std::vector<double> ts;
    for (const auto& v : raw) ts.push_back(edit_number(v,"拍点秒数"));
    if (std::abs(ts.front()-start)>edit_eps || std::abs(ts.back()-end)>edit_eps ||
        std::adjacent_find(ts.begin(),ts.end(),[](double a,double b){ return b<=a; })!=ts.end())
        invalid("候选拍点必须严格递增且保持选区精确两端");
    const double left=beat(old,start), right=beat(old,end), delta=static_cast<double>(ts.size()-1)-(right-left);
    const auto& original=required(source,"anchors");
    Json rows=Json::array();
    for (const auto& a : original) if (coord(a,"seconds")<start) rows.push_back(a);
    for (std::size_t i=0; i<ts.size(); ++i) rows.push_back(sample_record(source,old,ts[i],left+static_cast<double>(i)));
    for (const auto& a : original) if (coord(a,"seconds")>end) {
        Json copy=a; copy["beat"]=coord(a,"beat")+delta; rows.push_back(std::move(copy));
    }
    // Test the concatenated order, as the source does before its redundant sort.
    // Do not replace the accepted candidate endpoint seconds with start/end.
    const Curve next(rows);
    out["anchors"]=std::move(rows);
    remap_structure(out,old,next);
    out["allow_partial_measures"]=true;
    out["local_pulse_decode"]=get(op,"analysis",Json::object());
    warnings.push_back("拍号与第一拍不由本操作重新识别；原分组标记保留音频位置，必要时再复核分组。");
    return "依据缓存重选 "+format_g(start)+"–"+format_g(end)+" 秒内的 "+std::to_string(ts.size()-1)+
        " 个四分音符间隔；后续音乐编号平移 "+format_g(delta,true)+"，音符、踏板和范围外拍点秒位置不变";
}

std::string edit_point(Json& out,const Json& op,const Curve& old,std::string_view kind) {
    const double at=edit_time(get(op,"seconds"),old);
    auto& rows=out["anchors"];
    auto it=std::find_if(rows.begin(),rows.end(),[&](const Json& a){ return std::abs(coord(a,"seconds")-at)<edit_eps; });
    if (kind=="add_point") {
        if (it!=rows.end()) invalid("此处已有拍点，请使用移动动作");
        const double default_beat=beat(old,at);
        rows.push_back({{"seconds",at},{"beat",edit_number(get(op,"beat",default_beat),"拍位")},
                        {"origin","editor_tempo_point"}});
    } else {
        if (it==rows.end()) invalid("此处没有速度映射点，请先选择已有拍点");
        if (it==rows.begin() || it+1==rows.end()) invalid("不能移动或删除音频端点／固定锚点，请先解除固定");
        if (kind=="move_point") {
            const double to=edit_time(get(op,"to_seconds",at),old);
            const double q=edit_number(get(op,"beat",required(*it,"beat")),"拍位");
            (*it)["seconds"]=to; (*it)["beat"]=q;
            // Direct conductor point edits independently own this timing
            // knot, including when it started life as a pulse constraint.
            (*it)["origin"]="editor_tempo_point";
            it->erase("pulse_constraint_id");
        } else rows.erase(it);
    }
    sort_records(rows,"seconds");
    const Curve next(rows);
    remap_structure(out,old,next);
    out["allow_partial_measures"]=true;
    return "局部拍点已调整；音符、踏板及音频秒时间不变";
}

std::string edit_flag(const Json& source,Json& out,const Json& op,const Curve& old) {
    constexpr auto bad_value="拍点位置必须是有限数值。";
    const double from=number(get(op,"seconds"),Num::strict,bad_value);
    const double target=number(get(op,"to_seconds"),Num::strict,bad_value);
    const double q=number(get(op,"beat"),Num::strict,bad_value);
    if (!(old.beats.front()+bar_eps<q && q<old.beats.back()-bar_eps)) invalid("不能拖动音频首尾边界。");
    if (std::abs(seconds(old,q)-from)>bar_eps) invalid("拍点位置已变化，请重新选择旗帜。");
    const Layout layout(source);
    if (!on_flag_grid(layout,q)) invalid("请选择当前拍号下的完整拍位旗帜；不会把任意小数拍当成新拍。");
    auto support=flag_neighbors(layout,old,q);
    const auto& scope=get(op,"scope");
    if (!scope.is_null()) {
        if (!scope.is_object()) invalid("选区已失效，请重新框选。");
        constexpr auto range_error="选区边界必须在曲目内且严格递增。";
        const double start=number(get(scope,"start"),Num::strict,range_error), end=number(get(scope,"end"),Num::strict,range_error);
        if (start<old.seconds.front()-bar_eps || end>old.seconds.back()+bar_eps || end-start<=bar_eps) invalid(range_error);
        if (!(start+bar_eps<from && from<end-bar_eps)) invalid("只能移动框内旗帜；选区两端保持固定。");
        if (!(start+bar_eps<target && target<end-bar_eps)) invalid("旗帜不能越过选区边界；框外节拍不会移动。");
        support.before=std::max(support.before,start); support.after=std::min(support.after,end);
        support.left=beat(old,support.before); support.right=beat(old,support.after);
    }
    if (!(support.before+bar_eps<target && target<support.after-bar_eps))
        invalid("拍点不能越过相邻旗帜；补拍或删拍请使用拍数修正。");
    std::optional<double> score_end;
    if (const auto& e=get(source,"score_end"); truth(e)) {
        score_end=number(required(e,"seconds"),Num::floating);
        if (std::abs(*score_end-from)<bar_eps) invalid("这面旗帜已固定，请先解除锁定。");
    }
    Json rows=Json::array();
    Json moved=Json::object();
    for (const auto& a : required(source,"anchors")) {
        const double old_q=coord(a,"beat");
        if (std::abs(old_q-q)>bar_eps) rows.push_back(a);
        else if (old_q==q) moved=a;
    }
    const auto add_support=[&](double t,double b){
        if (std::none_of(rows.begin(),rows.end(),[&](const Json& a){ return std::abs(coord(a,"seconds")-t)<bar_eps; }))
            rows.push_back(sample_record(source,old,t,b));
    };
    add_support(support.before,support.left); add_support(support.after,support.right);
    if (score_end) add_support(*score_end,beat(old,*score_end));
    moved["seconds"]=target; moved["beat"]=q; rows.push_back(std::move(moved));
    sort_records(rows,"seconds");
    if (std::adjacent_find(rows.begin(),rows.end(),[](const Json& a,const Json& b){
        return coord(b,"seconds")-coord(a,"seconds")<=bar_eps || coord(b,"beat")-coord(a,"beat")<=bar_eps;
    })!=rows.end()) invalid("拖动与相邻映射点或固定旗帜冲突；请缩小移动范围。");
    const Curve next(rows);
    out["anchors"]=std::move(rows);
    if (find(out,"intro_downbeat_seconds") && find(out,"first_full_bar_beat"))
        out["intro_downbeat_seconds"]=seconds(next,coord(out,"first_full_bar_beat"));
    return "拍点位置从 "+format_g(from)+" 秒移至 "+format_g(target)+" 秒；影响 "+format_g(support.before)+"–"+
        format_g(support.after)+" 秒，拍数与原演奏不变";
}

std::string edit_first_bar(Json& out,const Json& op,const Curve& clock) {
    const double at=edit_time(get(op,"seconds"),clock), q=beat(clock,at);
    Json rows=Json::array();
    for (auto& row : out["time_signatures"]) if (!text_is(get(row,"origin"),"editor_first_bar")) rows.push_back(std::move(row));
    const Json& prior=rows[signature_index(rows,q,edit_eps)];
    const auto& mode=get(op,"mode","intro");
    const bool pickup=text_is(mode,"pickup");
    if (!pickup && !text_is(mode,"intro")) invalid("请选择完整引子或弱起");
    if (pickup && (q>=meter_of(prior).length()-edit_eps || std::any_of(rows.begin(),rows.end(),[&](const Json& r){
        const double p=coord(r,"beat"); return p>0. && p<q;
    }))) invalid("弱起必须短于一个小节且不跨变拍；完整引子请使用引子模式");
    if (!pickup && q>edit_eps && std::none_of(rows.begin(),rows.end(),[&](const Json& r){ return std::abs(coord(r,"beat")-q)<edit_eps; })) {
        Json marker=prior;
        marker["beat"]=q; marker["origin"]="editor_first_bar";
        rows.push_back(std::move(marker));
        rows=normalize_rows(std::move(rows),false);
    }
    out["time_signatures"]=std::move(rows);
    out["pickup_beats"]=pickup ? q : 0.;
    out["first_full_bar_beat"]=q;
    out["grid_origin"]=q;
    out["opening_mode"]=mode;
    out["intro_downbeat_seconds"]=at;
    out["allow_partial_measures"]=true;
    return pickup ? "首个完整小节已指定；前段保留为弱起" : "首个完整小节已指定；前段保留为引子，允许不完整边界小节";
}

void manual_editor(Json& out,Json operation) {
    out["source"]="manual_editor";
    out["meter_policy"]={{"mode","manual_editor"},{"protected",true}};
    out["last_tempo_operation"]=std::move(operation);
}

Json apply_tempo(const Json& source,Json op) {
    const auto kind=canonical_kind(text(get(op,"type"),"未知速度编辑操作"));
    const Curve old=checked_curve(source);
    const bool score=kind=="score_end" || kind=="clear_score_end";
    const bool flag_meter=kind=="meter_event" || kind=="add_meter_event" || kind=="remove_meter_event";
    const bool meter=flag_meter || kind=="meter";
    if (!score) required(source,"time_signatures");
    Json out=source, warnings=Json::array();
    std::string summary;
    if (flag_meter) summary=edit_meter_flag(out,op,old,kind);
    else if (kind=="meter") summary=edit_meter_range(out,op,old);
    else if (kind=="recount" || kind=="interpret") summary=edit_count(source,out,op,old,kind=="interpret");
    else if (kind=="pulse_times") summary=edit_pulse_times(source,out,op,old,warnings);
    else if (kind=="move_beat") summary=edit_flag(source,out,op,old);
    else if (kind=="move_point" || kind=="add_point" || kind=="remove_point") summary=edit_point(out,op,old,kind);
    else if (score) {
        if (kind=="clear_score_end") {
            erase_key(out,"score_end");
            summary="已取消谱面终点；恢复按记谱内容结束，演奏与音频不变";
        } else {
            const double at=edit_time(get(op,"seconds"),old);
            out["score_end"]={{"seconds",at},{"origin","editor_score_end"}};
            ensure_default_meter(out);
            timing_query(Layout(out),old,EndingMeasure{});
            summary="谱面终点设为 "+format_g(at)+" 秒；只表示末小节，不裁剪原始演奏或音频余音";
            warnings.push_back("若记谱音仍跨过终点，生成时会明确提示；不会自动截断长音来凑齐。");
        }
        out["allow_partial_measures"]=true;
        ensure_default_meter(out);
    } else if (kind=="first_bar") summary=edit_first_bar(out,op,old);
    else invalid("未知速度编辑操作");
    if (meter) meter_constraints(source,out,op,kind,old);
    else if (!score && kind!="move_beat") validate_layout(out);
    manual_editor(out,std::move(op));
    return {{"tempo",std::move(out)},{"fixed_anchors",Json::array()},{"summary",std::move(summary)},{"warnings",std::move(warnings)}};
}

struct RebarSite {
    const Json& tempo;
    const Curve& clock;
    std::string revision, sha;
    std::vector<Line> lines;
    std::vector<std::string> ids;
    RebarSite(const Json& data,const Curve& c,std::string rev,std::string identity) :
        tempo(data),clock(c),revision(std::move(rev)),sha(std::move(identity)) {
        const auto geometry=timing_query(Layout(tempo),clock,BarsRequest{std::nullopt,false,true});
        lines=boundary_lines(tempo,clock,geometry);
        ids.reserve(lines.size());
        for (const auto& line : lines) ids.push_back(line_id(revision,sha,line));
    }
    const Line* lookup(const Json& v) const {
        if (!v.is_string()) return nullptr;
        const auto it=std::find(ids.begin(),ids.end(),v.get_ref<const std::string&>());
        return it==ids.end() ? nullptr : &lines[static_cast<std::size_t>(it-ids.begin())];
    }
};

struct RebarPlan {
    Curve next;
    Json patch, impact;
    std::string summary;
};

RebarPlan plan_rebar(const RebarSite& site,const Json& op) {
    const auto* left=site.lookup(get(op,"left_boundary")), *right=site.lookup(get(op,"right_boundary"));
    if (!left || !right) invalid(missing_lines);
    if (left->index>=right->index) invalid("右侧小节线必须位于左侧之后");
    const Meter m=op_meter(op,meter_range_error,false);
    const int bars=bounded_int(get(op,"bar_count"),1,4096,count_error);
    const auto selected=static_cast<std::int64_t>(right->index-left->index);
    const auto& distribution=get(op,"distribution","preserve");
    const bool uniform=text_is(distribution,"uniform");
    if (!uniform && !text_is(distribution,"preserve")) invalid("重划分布必须是保留原形状或明确的选区定速");
    const double req_start=strict_number(get(op,"start"),"左定位秒数"), req_end=strict_number(get(op,"end"),"右定位秒数");
    if (req_start<0. || req_end>site.clock.seconds.back()+bar_eps || req_end-req_start<=bar_eps)
        invalid("左右定位必须在音频内且严格递增");
    if (const auto& ending=get(get(site.tempo,"score_end"),"seconds"); !ending.is_null() && req_end>number(ending,Num::floating)+bar_eps)
        invalid("重划不能越过已确认谱面终点；请先调整或取消终点");
    const double start=left->seconds, end=right->seconds, qa=left->beat, qb=right->beat;
    if (std::abs(req_start-start)>bar_eps || std::abs(req_end-end)>bar_eps)
        invalid("框选边界必须是所选已有小节线，不能移动框外节拍；请明确重新选择边界");
    const auto& rows=required(site.tempo,"time_signatures");
    if (!truth(get(op,"replace_changes",false)) && std::any_of(rows.begin(),rows.end(),[&](const Json& r){
        const double q=coord(r,"beat"); return qa+bar_eps<q && q<qb-bar_eps;
    })) invalid("范围内已有变拍；请明确选择替换范围内拍号");
    const double first=number(get(site.tempo,"first_full_bar_beat",get(site.tempo,"pickup_beats",0)),Num::floating);
    if (qa<first-bar_eps) invalid("请从首个完整小节开始重划；弱起／引子使用专用操作");
    const double target=bars*m.n*4./m.d, delta=target-(qb-qa), factor=target/(qb-qa);
    const auto map=[&](double q){ return q<=qa ? q : (q>=qb ? q+delta : qa+(q-qa)*factor); };
    const auto mapped_field=[&](const Json& value){
        const double q=raw_number(value);
        return q<=qa ? value : Json(map(q));
    };
    std::vector<double> samples=site.clock.seconds;
    samples.push_back(start); samples.push_back(end);
    for (const auto& r : rows) {
        const auto& q=required(r,"beat");
        if (!numeric_less(Json(site.clock.beats.back()),q)) samples.push_back(seconds(site.clock,raw_number(q)));
    }
    const auto& exact=site.clock.seconds;
    for (auto& t : samples) {
        // Match the first original sample with abs(t-x)<1e-10; don't use
        // tolerance-based sorting or rounding of the selected bar lines.
        const auto i=std::lower_bound(exact.begin(),exact.end(),t,[](double x,double t0){ return t0-x>=exact_sample_eps; });
        if (i!=exact.end() && std::abs(t-*i)<exact_sample_eps) t=*i;
    }
    sort_samples(samples);
    std::vector<double> beats; beats.reserve(samples.size());
    for (const double t : samples) beats.push_back(uniform && start<=t && t<=end ?
        qa+(t-start)*target/(end-start) : map(beat(site.clock,t)));
    Curve next(std::move(samples),std::move(beats));
    Json new_rows=Json::array();
    for (const auto& r : rows) {
        const auto& pos=required(r,"beat");
        const double q=raw_number(pos);
        if (q<qa-bar_eps || q>qb+bar_eps) {
            Json row=r; row["beat"]=mapped_field(pos); new_rows.push_back(std::move(row));
        }
    }
    Json replacement=signature(qa,m); replacement["origin"]="editor_rebar";
    Json restore=rows[signature_index(rows,qb,bar_eps)]; restore["beat"]=qa+target; restore["origin"]="editor_rebar_restore";
    new_rows.push_back(std::move(replacement)); new_rows.push_back(std::move(restore));
    sort_records(new_rows,"beat");
    Json patch={{"time_signatures",std::move(new_rows)}};
    for (const char* key : {"grid_origin","first_full_bar_beat","pickup_beats"})
        if (const auto* q=find(site.tempo,key)) patch[key]=mapped_field(*q);
    const Layout layout(site.tempo,&patch);
    validate_layout(layout);
    timing_query(layout,next,BarsRequest{std::nullopt,false,true});

    const auto shift=bars-selected;
    Json impact={{"scope","selected_range"},{"start_seconds",start},{"end_seconds",end},
        {"selected_start_seconds",start},{"selected_end_seconds",end},
        {"old_boundary_seconds",Json::array({start,end})},{"new_boundary_seconds",Json::array({start,end})},
        {"old_bar_numbers",Json::array({left->bar_number,right->bar_number})},{"new_bar_count",bars},{"affected_bar_count",bars},
        {"quarters",target},{"following_quarter_shift",delta},
        {"distribution",uniform ? "uniform_selected_range" : "preserve_piecewise_shape"},
        {"source_note_seconds_modified",false},{"source_control_seconds_modified",false},
        {"outer_clock_seconds_unchanged",true},{"outside_bar_numbers_unchanged",shift==0},
        {"following_bar_number_shift",shift},{"outside_physical_beats_and_bars_unchanged",true}};
    std::string summary="第 "+std::to_string(left->bar_number)+"–"+std::to_string(right->bar_number)+" 小节线重划为 "+
        std::to_string(bars)+" 小节 "+std::to_string(m.n)+"/"+std::to_string(m.d)+"；";
    if (uniform) summary+="选区按确认拍数定速，";
    summary+="仅修改框内 "+format_g(start)+"–"+format_g(end)+" 秒；框外拍点、小节线时刻与演奏秒数不变";
    if (shift!=0) summary+="，后方小节编号顺延 "+std::string(shift>0 ? "+" : "")+std::to_string(shift);
    return {std::move(next),std::move(patch),std::move(impact),std::move(summary)};
}

Json apply_rebar(const Json& tempo,const Json& op,const std::string& revision) {
    if (!text_is(get(op,"boundary_revision"),revision)) invalid(stale_rebar);
    auto sha=tempo_sha(tempo);
    if (!text_is(get(op,"tempo_sha256"),sha)) invalid(stale_rebar);
    const auto old=checked_curve(tempo);
    const RebarSite site(tempo,old,revision,std::move(sha));
    auto plan=plan_rebar(site,op);
    Json out=Json::object();
    for (auto i=tempo.begin(); i!=tempo.end(); ++i) {
        const auto& key=i.key();
        if (key=="anchors") out[key]=samples_at(tempo,old,plan.next);
        else if (auto p=plan.patch.find(key); p!=plan.patch.end()) out[key]=std::move(*p);
        else out[key]=*i;
    }
    Json inner=op; inner["type"]="rebar";
    manual_editor(out,std::move(inner));
    out["rebar_edit"]={{"version",2},{"impact",plan.impact}};
    return {{"tempo",std::move(out)},{"fixed_anchors",Json::array()},{"impact",std::move(plan.impact)},
            {"warnings",Json::array()},{"summary",std::move(plan.summary)}};
}

// Quick meter preview: these tolerances and rules are deliberately distinct
// from range-meter and integer rebar transactions.
void validate_barlines(const Json& rows,double pickup) {
    Meter current=meter_of(rows.front());
    double origin=pickup;
    for (std::size_t i=1; i<rows.size(); ++i) {
        const auto m=meter_of(rows[i]);
        if (m==current) continue;
        const double q=coord(rows[i],"beat"), bars=(q-origin)/current.length();
        if (bars< -bar_eps || !quick_same(bars,round_even(bars)))
            invalid("后续 "+format_g(q)+" 拍的拍号将不在小节线上。请先核对该变拍点。");
        origin=q; current=m;
    }
}
struct QuickCandidate {
    double distance, left, target;
    Meter meter;
    auto key() const { return std::tie(distance,left,target,meter.n,meter.d); }
};
QuickCandidate quick_candidate(double q,const Json& rows,double pickup,const Json& previous) {
    std::optional<QuickCandidate> best;
    const auto add=[&](QuickCandidate c){ if (!best || c.key()<best->key()) best=c; };
    for (std::size_t i=0; i<rows.size(); ++i) {
        const auto m=meter_of(rows[i]);
        const double length=m.length(), start=i ? coord(rows[i],"beat") : pickup;
        const double limit=i+1<rows.size() ? coord(rows[i+1],"beat") : std::numeric_limits<double>::infinity();
        const double requested=previous.is_null() ? length : number(previous);
        const double guess=std::floor(finite((q-start-requested)/length));
        for (const double k : {guess,guess+1.}) {
            if (k<0.) continue;
            const double left=start+k*length, target=left+requested;
            if (left<limit-bar_eps && target<=limit+bar_eps) add({std::abs(q-target),left,target,m});
        }
    }
    if (previous.is_null() && pickup>0.) add({std::abs(q-pickup),0.,pickup,meter_of(rows.front())});
    if (best && best->distance<=std::min(.28,2./best->meter.d)) return *best;
    invalid("这里离现有小节线较远。若前一小节实际为 2、3 或 4 拍，请在「前一小节」明确选择；也可调整时间再预览。");
}

struct OpeningPreview { double pickup, bars; };
Json quick_preview(const Json& source,Json result,const Curve& old,const Json& at,const Json& numerator,
                   const Json& previous,std::optional<OpeningPreview> opening) {
    const double t=number(at);
    Json rows=normalize_rows(result["time_signatures"],true);
    double pickup=number(get(result,"pickup_beats",0),Num::floating);
    validate_barlines(rows,pickup);
    const double q=beat(old,t);
    double target=q;
    std::optional<double> previous_start;
    std::size_t desired_row=0;
    double unit=1.;
    const bool pickup_choice=text_is(previous,"pickup");
    if (quick_same(t,0.)) {
        if (!previous.is_null()) invalid("曲首没有前一小节。");
        target=0.;
    } else if (pickup_choice) {
        const double size=meter_of(rows.front()).length();
        if (q<=bar_eps || q>=size || std::any_of(rows.begin()+1,rows.end(),[&](const Json& r){ return coord(r,"beat")<q-bar_eps; }))
            invalid("弱起须在曲首，且短于一个完整小节。");
        target=round_even(q*4.)/4.;
        if (target<=0. || target>=size) invalid("这个位置太接近曲首或完整小节，无法确定一个短弱起。若没有弱起，请保留曲首；否则请定位真正的首个完整小节第一拍。");
        if (std::abs(target-q)>.125+bar_eps) invalid("所选位置与常见弱起长度相差较大，请重新定位首个完整小节第一拍。");
        previous_start=0.; pickup=target; result["grid_origin"]=pickup;
    } else {
        const auto candidate=quick_candidate(q,rows,pickup,previous);
        previous_start=candidate.left; target=candidate.target;
        desired_row=signature_index(rows,target,bar_eps);
        if (previous.is_null()) unit=candidate.meter.unit();
    }
    const Json desired_n=numerator.is_null() ? required(rows[desired_row],"numerator") : numerator;
    const Json desired_d=numerator.is_null() ? required(rows[desired_row],"denominator") : Json(4);
    bool anchors_changed=false;
    if (!quick_same(target,q)) {
        if (!previous_start) invalid("修正会压缩掉前一拍。请选择更准确的时间。");
        const double left=std::max(*previous_start,target-unit);
        if (q<=left+bar_eps) invalid("修正会压缩掉前一拍。请选择更准确的时间。");
        const double left_seconds=seconds(old,left), offset=target-q;
        const auto shift=[&](const Json& value) -> Json {
            const double position=raw_number(value);
            if (position>=q) return position+offset;
            if (position<=left) return value;
            return left+(position-left)*(target-left)/(q-left);
        };
        Json anchors=Json::array();
        for (auto& a : result["anchors"]) {
            const double ts=coord(a,"seconds");
            if (quick_same(ts,left_seconds) || quick_same(ts,t)) continue;
            a["beat"]=shift(required(a,"beat")); anchors.push_back(std::move(a));
        }
        if (left_seconds!=t) anchors.push_back(sample_record(source,old,left_seconds,left));
        Json point=sample_record(source,old,t,target); point["seconds"]=at; anchors.push_back(std::move(point));
        sort_records(anchors,"seconds");
        result["anchors"]=std::move(anchors);
        erase_key(result,"midi_tempo_events");
        Json shifted=Json::array();
        for (auto& row : rows) {
            if (quick_same(coord(row,"beat"),target)) continue;
            row["beat"]=shift(required(row,"beat")); shifted.push_back(std::move(row));
        }
        rows=std::move(shifted);
        anchors_changed=true;
    }
    if (quick_meter(previous)) rows.push_back({{"beat",*previous_start},{"numerator",previous},{"denominator",4}});
    auto& anchors=result["anchors"];
    if (!quick_same(t,0.) && std::none_of(anchors.begin(),anchors.end(),[&](const Json& a){ return quick_same(coord(a,"seconds"),t); })) {
        anchors.push_back({{"seconds",at},{"beat",target}});
        sort_records(anchors,"seconds");
        anchors_changed=true;
    }
    rows.push_back({{"beat",target},{"numerator",desired_n},{"denominator",desired_d}});
    result["time_signatures"]=normalize_rows(std::move(rows),true);
    result["pickup_beats"]=pickup;
    validate_barlines(result["time_signatures"],pickup);
    std::optional<Curve> new_curve;
    if (anchors_changed) new_curve.emplace(result["anchors"]);
    validate_layout(result);
    const Curve& next=new_curve ? *new_curve : old;
    if (truth(get(result,"notation_tempo_marks"))) {
        array(result["notation_tempo_marks"]);
        for (auto& mark : result["notation_tempo_marks"])
            mark["beat"]=beat(next,seconds(old,number(required(mark,"beat"),Num::floating)));
    }
    Json edit={{"seconds",at},{"beat",target},{"previous_beat",q},{"numerator",desired_n},{"denominator",desired_d},
               {"previous",previous},{"beat_adjustment",target-q}};
    if (pickup_choice) { edit["pickup_beats"]=pickup; edit["pickup_snap_step"]=.25; }
    if (opening) {
        edit["opening"]=true; edit["pickup_beats"]=opening->pickup;
        edit["intro_full_bars"]=integer_double(opening->bars).json();
    }
    if (!find(result,"manual_meter_edits")) result["manual_meter_edits"]=Json::array();
    array(result["manual_meter_edits"]);
    result["manual_meter_edits"].push_back(edit);
    result["source"]="manual";
    erase_key(result,"constant_bpm"); erase_key(result,"fit");
    if (opening) result["intro_downbeat_seconds"]=at;
    return {{"data",std::move(result)},{"edit",std::move(edit)}};
}

Json preview_meter(const Json& args,bool roll) {
    const bool opening=roll && truth(get(args,"opening",false));
    const auto& at=get(args,"seconds"), &numerator=get(args,"numerator");
    const auto& previous=roll ? nil() : get(args,"previous");
    const auto time_error=opening ? "请选择有效的音符或标尺位置。" : "请输入有效的时间（秒）。";
    const double t=number(at,Num::strict,time_error);
    if (t<0.) invalid(time_error);
    if (!numerator.is_null() && !quick_meter(numerator)) invalid(opening ? "请选择 2/4、3/4 或 4/4。" : "快捷修正支持 2/4、3/4 和 4/4。");
    if (!previous.is_null() && !text_is(previous,"pickup") && !quick_meter(previous)) invalid("请选择有效的前一小节拍号。");
    const auto& source=object(required(args,"tempo"),"速度图必须是对象。");
    const auto old=checked_curve(source);
    if (t>=old.seconds.back()) invalid(opening ? "请选择曲目结束前的位置。" : "请输入速度图范围内且早于结尾的时间。");
    Json draft=source;
    ensure_default_meter(draft);
    if (!opening) return quick_preview(source,std::move(draft),old,at,numerator,previous,std::nullopt);
    const double target=round_even(beat(old,t)*4.)/4.;
    if (t>0. && target<=0.) invalid("这里太接近起点，请选择首个完整小节的第一拍。");
    double size;
    {
        auto& first=draft["time_signatures"].front();
        if (!numerator.is_null()) { first["numerator"]=numerator; first["denominator"]=4; }
        size=meter_of(first).length();
    }
    const double pickup=positive_mod(target,size);
    const double intro_count=round_even((target-pickup)/size);
    draft["pickup_beats"]=pickup; draft["grid_origin"]=pickup;
    return quick_preview(source,std::move(draft),old,at,nil(),nil(),OpeningPreview{pickup,intro_count});
}

Json preview_constant(const Json& args) {
    const auto& req=object(required(args,"request"));
    const double bpm=strict_number(get(req,"reference_bpm"),"参考 BPM");
    if (bpm<10. || bpm>600.) invalid("参考 BPM 需要在 10–600 之间");
    const double phase=strict_number(get(req,"phase_seconds"),"参考第一拍秒数");
    const auto& tempo=required(args,"tempo");
    const Curve old=checked_curve(tempo);
    if (std::abs(phase)>std::max(86400.,old.seconds.back()*4.)) invalid("参考相位超出可显示范围");
    const auto& revision=revision_value(get(args,"revision"));
    const RebarSite site(tempo,old,revision,tempo_sha(tempo));
    if (!text_is(get(req,"boundary_revision"),revision) || !text_is(get(req,"tempo_sha256"),site.sha))
        invalid("参考的小节线已过期，请重新选择");
    if (!site.lookup(get(req,"left_boundary")) || !site.lookup(get(req,"right_boundary")))
        invalid("请选择当前版本的两条已有小节线");
    const Meter m=op_meter(req,"请输入有效拍号",false);
    const int bars=bounded_int(get(req,"bar_count"),1,4096,count_error);
    const double left=strict_number(get(req,"start"),"左定位秒数"), right=strict_number(get(req,"end"),"右定位秒数");
    if (!(0.<=left && left<right && right<=old.seconds.back())) invalid("参考选区必须在音频内且严格递增");
    const double quarter=60./bpm, unit=m.unit(), bar_quarters=m.n*unit, target=bars*bar_quarters;
    const double actual=right-left, expected=target*quarter;
    const double left_index=round_even((left-phase)/(quarter*bar_quarters))*m.n;
    const double aligned_left=finite(phase+left_index*quarter*unit), aligned_right=finite(aligned_left+expected);
    const double phase_error=finite(left-aligned_left), span_error=actual-expected;
    const double actual_quarters=finite(actual/quarter), implied=finite(actual_quarters/bar_quarters);
    const bool phases_ok=std::abs(phase_error)<=constant_tolerance, count_ok=std::abs(span_error)<=constant_tolerance;
    Json operation=Json::object();
    for (const char* key : {"boundary_revision","tempo_sha256","left_boundary","right_boundary","start","end",
                             "numerator","denominator","bar_count","replace_changes"})
        if (const auto* v=find(req,key)) operation[key]=*v;
    Json candidates=Json::array();
    bool requested_feasible=false;
    const auto add=[&](const char* id,const char* kind,Json op,bool constant,Json changes) {
        Json candidate={{"id",id},{"kind",kind},{"constant_reference_satisfied",constant},
                        {"operation",std::move(op)},{"changes",std::move(changes)},{"applicable",false}};
        try {
            const auto& candidate_op=candidate["operation"];
            auto plan=plan_rebar(site,candidate_op);
            if (constant) {
                const double begin=number(required(candidate_op,"start")), finish=number(required(candidate_op,"end"));
                const auto verify=[&](double a,double b){
                    if (std::abs((beat(plan.next,b)-beat(plan.next,a))/(b-a)*60.-bpm)>constant_tolerance)
                        invalid("该候选未能形成要求的局部定速图；不会把保留原速度形状冒充定速");
                };
                double a=begin;
                for (auto i=std::upper_bound(plan.next.seconds.begin(),plan.next.seconds.end(),begin);
                     i!=plan.next.seconds.end() && *i<finish; ++i) { verify(a,*i); a=*i; }
                verify(a,finish);
            }
            candidate["applicable"]=true;
            candidate["impact"]=std::move(plan.impact);
            candidate["summary"]=std::move(plan.summary);
            if (constant) candidate["effective_quarter_bpm"]=bpm;
            if (std::string_view(id)=="fixed_requested") requested_feasible=true;
        } catch (const std::invalid_argument& e) {
            candidate["reason"]=e.what();
            erase_key(candidate,"operation");
        }
        candidate["playback_rate"]=1;
        candidate["performance_export_purpose"]="mapped";
        candidates.push_back(std::move(candidate));
    };
    Json fixed=operation; fixed["distribution"]="uniform";
    if (phases_ok && count_ok) add("fixed_requested","keep_requested_constraints",std::move(fixed),true,Json::array());
    else {
        Json moved=fixed; moved["start"]=aligned_left; moved["end"]=aligned_right;
        add("fixed_move_endpoints","adjust_endpoints",std::move(moved),true,Json::array({
            Json{{"field","start"},{"before",left},{"after",aligned_left},{"delta_seconds",aligned_left-left}},
            Json{{"field","end"},{"before",right},{"after",aligned_right},{"delta_seconds",aligned_right-right}}
        }));
        const double nearest=round_even(implied);
        if (phases_ok && 1.<=nearest && nearest<=4096. && std::abs(actual-nearest*bar_quarters*quarter)<=constant_tolerance) {
            const int n=static_cast<int>(nearest);
            fixed["bar_count"]=n;
            add("fixed_change_count","adjust_bar_count",std::move(fixed),true,Json::array({Json{{"field","bar_count"},{"before",bars},{"after",n}}}));
        }
    }
    operation["distribution"]="preserve";
    add("release_constant","release_constant_constraint",std::move(operation),false,Json::array());
    Json warnings=Json::array();
    if (!requested_feasible) warnings.push_back(phases_ok && count_ok ? "边界或固定锚点约束阻止采用；请查看候选的具体原因。" :
        "参考速度、相位、选区时长与整数小节数不能同时满足；请明确选择调整方案。");
    return {{"version",1},{"mode","reference_only"},{"revision",revision},{"tempo_sha256",site.sha},
        {"reference_grid",Json{{"bpm",bpm},{"phase_seconds",phase},{"quarter_seconds",quarter},{"beat_unit_quarters",unit},
            {"beat_unit_seconds",unit*quarter},{"numerator",m.n},{"denominator",m.d},{"start_seconds",0},
            {"end_seconds",old.seconds.back()},{"zero_index_at_phase",true},{"phase_kind","bar_start"}}},
        {"constraints",Json{{"start_seconds",left},{"end_seconds",right},{"bar_count",bars},{"target_quarters",target},
            {"actual_duration_seconds",actual},{"required_duration_seconds",expected},{"duration_error_seconds",span_error},
            {"quarter_count_at_reference_bpm",actual_quarters},{"quarter_count_error",actual_quarters-target},
            {"left_phase_error_seconds",phase_error},{"implied_bar_count",implied},{"phase_consistent",phases_ok},{"span_consistent",count_ok}}},
        {"requested_constraints_feasible",requested_feasible},{"candidates",std::move(candidates)},
        {"playback",Json{{"rate",1},{"source","original_unquantized_seconds"},{"reference_grid_drives_playback",false},
            {"active_clock_changed",false},{"source_note_or_cc_seconds_changed",false},
            {"adoption_requires_mapped_tempo_and_note_ticks_together",true},{"quantized_output_must_be_separate",true}}},
        {"warnings",std::move(warnings)}};
}

Json grid_points(const Json& args) {
    constexpr auto bad_range="参考网格范围或数量无效";
    const double start=strict_number(get(args,"start_seconds"),"显示开始秒数"), end=strict_number(get(args,"end_seconds"),"显示结束秒数");
    if (end<start) invalid(bad_range);
    const int max_points=bounded_int(get(args,"max_points",4096),1,10000,bad_range);
    const auto& ref=required(args,"reference_grid");
    const double phase=number(required(ref,"phase_seconds"),Num::floating), step=number(required(ref,"beat_unit_seconds"),Num::floating,"参考拍长无效");
    if (step<=0.) invalid("参考拍长无效");
    const double first=std::ceil(finite((start-phase)/step-1e-9)), last=std::floor(finite((end-phase)/step+1e-9));
    if (last-first+1.>max_points) invalid("参考网格过密，请缩小可见范围");
    const auto n=integer_value(required(ref,"numerator"),true);
    Json result=Json::array();
    if (last<first) return result;
    if (!n.magnitude) invalid("参考拍号无效");
    auto index=integer_double(first);
    const auto count=static_cast<unsigned>(last-first+1.);
    result.get_ref<Json::array_t&>().reserve(count);
    for (unsigned i=0; i<count; ++i) {
        const bool opposite=index.negative!=n.negative;
        auto quotient=index.magnitude/n.magnitude, remainder=index.magnitude%n.magnitude;
        if (opposite && remainder) { ++quotient; remainder=n.magnitude-remainder; }
        const Integer bar{opposite,quotient};
        const Integer in_bar=n.negative && remainder ? Integer{true,remainder-1} : Integer{false,remainder+1};
        result.push_back({{"seconds",finite(phase+index.as_double()*step)},{"index",index.json()},
                          {"beat_in_bar",in_bar.json()},{"bar_offset",bar.json()}});
        if (i+1<count) {
            if (index.negative) { if (--index.magnitude==0) index.negative=false; }
            else {
                if (index.magnitude==std::numeric_limits<std::uint64_t>::max()) invalid("整数超出 JSON 整数范围");
                ++index.magnitude;
            }
        }
    }
    return result;
}

} // namespace

Json structural_preview_query(std::string_view name,const Json& args) {
    if (name=="preview_meter_edit" || name=="preview_roll_edit") return preview_meter(args,name=="preview_roll_edit");
    if (name=="preview_constant_tempo") return preview_constant(args);
    if (name=="reference_grid_points") return grid_points(args);
    invalid("未知结构预览查询");
}
} // namespace astra::editor::timing_detail

namespace astra::editor {

Json structural_command(const Json& tempo,const Json& operation,const Json& fixed_anchors,const Json& context) {
    using namespace timing_detail;
    (void)fixed_anchors; // Retired compatibility input, not a constraint list.
    object(operation);
    const auto& outer=get(operation,"type");
    if (text_is(outer,"rhythm_span_anchors")) {
        const Curve old(required(tempo,"anchors"));
        const double start=edit_time(required(operation,"start"),old),end=edit_time(required(operation,"end"),old);
        const double target=number(required(operation,"quarters"));
        if(start<0.||!(end>start)||end>old.seconds.back()+edit_eps||!(target>0.)) invalid("invalid rhythm span");
        const Json distribution=get(operation,"distribution","preserve");
        const bool uniform=text_is(distribution,"uniform");
        if(!uniform&&!text_is(distribution,"preserve"))invalid("invalid rhythm span distribution");
        const double left=beat(old,start),right=beat(old,end),span=right-left,delta=target-span;
        if(!(span>0.))invalid("invalid rhythm quarter span");
        std::vector<double> samples=old.seconds;samples.push_back(start);samples.push_back(end);sort_samples(samples);
        std::vector<double> beats;beats.reserve(samples.size());
        for(double t:samples){
            const double q=beat(old,t);
            beats.push_back(t<=start?q:t>=end?q+delta:
                uniform?left+(t-start)*target/(end-start):left+(q-left)*(target/span));
        }
        const Curve next(std::move(samples),std::move(beats));
        return {{"anchors",samples_at(tempo,old,next)}};
    }
    if (text_is(outer,"tempo")) {
        const auto& action=text(get(operation,"action"),"未知速度编辑操作");
        Json inner=operation;
        inner.erase("action");
        inner["type"]=action;
        return apply_tempo(tempo,std::move(inner));
    }
    if (text_is(outer,"rebar")) return apply_rebar(tempo,operation,revision_value(get(context,"revision")));
    invalid("未知结构编辑操作");
}

} // namespace astra::editor
