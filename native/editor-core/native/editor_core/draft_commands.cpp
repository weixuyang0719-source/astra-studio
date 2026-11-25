#include "draft_commands.h"
#include "pulse_kernel_detail.h"
#include <iterator>
#include <unordered_map>
#include <unordered_set>

namespace astra::editor {
namespace {
using namespace pulse_detail;
constexpr double de = 1e-7;
constexpr std::array<const char*,3> categories{"pulses","bars","labels"};
constexpr std::array<const char*,3> phase_keys{"pickup_beats","grid_origin","first_full_bar_beat"};
using Index = std::unordered_map<std::string_view,std::size_t>;

Index index(const Json& rows) {
    Index out;
    out.reserve(rows.size());
    for (std::size_t i=0;i<rows.size();++i) if (!out.emplace(text(rows[i].at("id")),i).second)
        bad("草稿身份重复");
    return out;
}
const Json& local_meter(const Json& labels, double t) { return effective(labels,"seconds",t,de); }
bool exception_bar(const Json& row) { return is(opt(row,"exception"),"opening") || is(opt(row,"exception"),"tail"); }
void touch(Json& model, const Json& row) {
    auto& ids=model.at("changed_ids");
    const auto& id=row.at("id");
    if (std::find(ids.begin(),ids.end(),id)==ids.end()) ids.push_back(id);
}
std::string new_id(std::string_view prefix, const std::string& seed) {
    return std::string(prefix)+seed.substr(0,16);
}
std::string step_seed(std::string_view prior, const Json& op) {
    return row_digest(std::string(prior)+'\n'+op.dump());
}

struct Start { Json model, scope; };
Start start_model(const Json& source, const Json& requested_scope, bool preserve) {
    const auto g=geometry(source);
    const auto& signatures= pulse_detail::signatures(source);
    const auto b=project(source,{},column(signatures,"beat")).t;
    const double end=seconds(source,g.end_q), first=opening_beat(source);
    Json scope=nullptr;
    if (!requested_scope.is_null()) {
        object(requested_scope);
        const double a=num(requested_scope.at("start")), z=num(requested_scope.at("end"));
        if (!(0<=a && a<z && z<=end+de)) bad("结构编辑选区必须位于谱面范围内");
        scope=Json{{"start",a},{"end",z}};
    }
    Json labels=Json::array(), bars=Json::array(), pulses=Json::array();
    for (std::size_t i=0;i<signatures.size();++i) labels.push_back(Json{{"id","m"+std::to_string(i)},
        {"seconds",b[i]},{"numerator",signatures[i].at("numerator")},{"denominator",signatures[i].at("denominator")}});
    const auto starts=project(source,{},g.bars.starts).t;
    D qs{0.,extent(source).beat,g.end_q};
    for (std::size_t i=0;i<starts.size();++i) {
        const double q=g.bars.starts[i];
        if (q>=g.end_q-de) continue;
        const double finish=std::min(q+g.bars.lengths[i],g.end_q);
        const auto m=meter_of(local_meter(labels,starts[i]));
        qs.push_back(q); qs.push_back(finish);
        for (std::size_t k=1;q+k*m.unit()<finish-de;++k) qs.push_back(q+k*m.unit());
        Json except=nullptr;
        if (q<first-de && std::abs(finish-first)<de) except="opening";
        else if (std::abs(finish-g.end_q)<de && std::abs(finish-q-m.length())>de) except="tail";
        bars.push_back(Json{{"id","b"+std::to_string(bars.size())},{"seconds",starts[i]},{"exception",std::move(except)}});
    }
    bars.push_back(Json{{"id","b"+std::to_string(bars.size())},{"seconds",end},{"exception","end"}});
    sort_unique(qs);
    const auto ts=project(source,{},qs).t;
    for (std::size_t i=0;i<qs.size();++i) pulses.push_back(Json{{"id","p"+std::to_string(i)},
        {"seconds",ts[i]},{"units",i ? qs[i]-qs[i-1] : 0.}});
    return {Json{{"pulses",std::move(pulses)},{"bars",std::move(bars)},{"labels",std::move(labels)},
                 {"preserve_unedited_positions",preserve},{"changed_ids",Json::array()},{"metadata_seconds",Json::object()}},
            std::move(scope)};
}
Json baseline_of(const Json& model) {
    return Json{{"pulses",model.at("pulses")},{"bars",model.at("bars")},{"labels",model.at("labels")}};
}

// A draft pulse is an incoming count constraint. It is not a replacement
// for the live pulse list (whose units are outgoing). Keep source performance
// pulse IDs and seconds, but don't leave stale quarter coordinates or a
// signature pointing at an unrelated live pulse after a draft count edit.
void project_live_points(Json& result) {
    if (!result.contains("pulse_points")) return;
    auto& pts=result.at("pulse_points");
    const auto q=project(result,column(pts,"seconds")).q;
    std::unordered_map<std::string,double> by_id;
    by_id.reserve(pts.size());
    for (std::size_t i=0;i<pts.size();++i) {
        auto& p=pts[i];
        const double before=field(p,"beat");
        if (q[i]!=before) p.at("beat")=q[i];
        if (i+1<q.size() && std::abs((q[i+1]-q[i])-(field(pts[i+1],"beat")-before))>eps)
            p["step_quarters"]=q[i+1]-q[i];
        by_id.emplace(p.at("id").dump(),q[i]);
    }
    for (auto& row : result.at("time_signatures")) if (truth(opt(row,"pulse_id"))) {
        const auto i=by_id.find(row.at("pulse_id").dump());
        if (i==by_id.end() || std::abs(i->second-field(row,"beat"))>eps) row.erase("pulse_id");
    }
}
Json candidate(const Json& source, const Json& model, const Json& baseline) {
    const auto& pulses=model.at("pulses");
    const auto t=column(pulses,"seconds");
    D q{0.};
    if (t.size()<2 || t.front()!=0) bad("拍旗时间必须严格递增，每拍计数必须为正");
    for (std::size_t i=1;i<t.size();++i) {
        const double last=q.back(), next=last+field(pulses[i],"units");
        if (t[i]-t[i-1]<=de || next-last<=de) bad("拍旗时间必须严格递增，每拍计数必须为正");
        q.push_back(next);
    }
    const auto old_t=anchor_times(source), support=joined(t,old_t);
    const auto& labels=model.at("labels");
    const auto& meta=model.at("metadata_seconds");
    D values=support;
    for (const auto& l : labels) values.push_back(field(l,"seconds"));
    std::vector<const char*> keys;
    D mq;
    for (const char* k : phase_keys) if (source.contains(k)) {
        keys.push_back(k); mq.push_back(intent_num(source.at(k),"无效开头位置"));
    }
    const auto mt=project(source,{},mq).t;
    for (std::size_t i=0;i<keys.size();++i) values.push_back(meta.contains(keys[i]) ? field(meta,keys[i]) : mt[i]);
    const auto old_q=project(source,values).q;
    D xs;
    xs.reserve(t.size());
    for (double v : t) xs.push_back(old_q[static_cast<std::size_t>(std::lower_bound(support.begin(),support.end(),v)-support.begin())]);
    const auto mapped=interpolate(xs,q,old_q);
    Json rows=Json::array();
    const auto original=index(baseline.at("labels"));
    const auto& sr=pulse_detail::signatures(source);
    for (std::size_t i=0;i<labels.size();++i) {
        const auto& l=labels[i];
        const auto j=original.find(text(l.at("id")));
        Json row=j==original.end() ? Json::object() : sr.at(j->second);
        row.update(Json{{"beat",mapped[support.size()+i]},{"numerator",l.at("numerator")},
                        {"denominator",l.at("denominator")},{"origin","editor_structure_draft"}});
        rows.push_back(std::move(row));
    }
    Json out=source;
    out.at("anchors")=transport_rows(source,old_t,support,support,mapped);
    out["time_signatures"]=std::move(rows);
    for (std::size_t i=0;i<keys.size();++i) out.at(keys[i])=mapped[support.size()+labels.size()+i];
    if (out.contains("intro_downbeat_seconds") && meta.contains("first_full_bar_beat"))
        out.at("intro_downbeat_seconds")=meta.at("first_full_bar_beat");
    project_live_points(out);
    out["source"]="manual_editor";
    out["meter_policy"]=Json{{"mode","manual_editor"},{"protected",true}};
    return out;
}

Json flags(const Json& model,const Json& clock,const D& qbars) {
    const auto& bars=model.at("bars");
    const auto& labels=model.at("labels");
    const auto& pulses=model.at("pulses");
    D q; std::vector<int> denoms;
    for (std::size_t i=1;i<bars.size();++i) {
        const auto m=meter_of(local_meter(labels,field(bars[i-1],"seconds")));
        const double start=qbars[i-1], end=qbars[i];
        for (std::size_t k=0;start+k*m.unit()<end-de;++k) {
            q.push_back(start+k*m.unit()); denoms.push_back(m.d);
        }
    }
    const auto t=project(clock,{},q).t;
    Json f=Json::array();
    for (std::size_t j=0;j<t.size();++j) f.push_back(Json{{"seconds",t[j]},{"beat",q[j]},
        {"denominator",denoms[j]},{"derived",at(pulses,"seconds",t[j],de)==none}});
    return f;
}

Json inspection(const Json& source,const Json& model,const Json& baseline,const Json& scope,
                std::size_t operation_count,bool render) {
    Json conflicts=Json::array();
    const auto issue=[&](const char* code,std::string message,Json t=nullptr,Json detail=Json::object()) {
        Json c{{"code",code},{"message",std::move(message)},{"seconds",std::move(t)}};
        c.update(detail); conflicts.push_back(std::move(c));
    };
    Json clock;
    try { clock=validated(candidate(source,model,baseline)); }
    catch (const std::invalid_argument& e) {
        issue("invalid_map",e.what(),nullptr,Json{{"suggestion","修正拍旗顺序、正拍数或起始拍号后重试"}});
    }
    if (!conflicts.empty()) {
        Json r{{"valid",false},{"conflicts",conflicts},
               {"summary","结构草稿保留了修改，但当前拍点映射不合法"},
               {"impact",Json{{"scope",scope},{"explicit_operation_count",operation_count}}}};
        if (render) {
            r["guidance"]=std::move(conflicts);
            r["display"]=baseline_of(model); r.at("display")["beat_flags"]=Json::array();
        }
        return r;
    }
    const auto& bars=model.at("bars");
    const auto& labels=model.at("labels");
    const auto t=column(bars,"seconds");
    const auto q=project(clock,t).q;
    const auto& old_bars=baseline.at("bars");
    const auto old_index=index(old_bars);
    D prior_t;
    std::vector<std::size_t> prior_idx;
    for (std::size_t i=1;i<bars.size();++i) if (exception_bar(bars[i-1])) {
        const auto a=old_index.find(text(bars[i-1].at("id"))), b=old_index.find(text(bars[i].at("id")));
        if (a!=old_index.end() && b!=old_index.end()) {
            prior_idx.push_back(i);
            prior_t.push_back(field(old_bars[a->second],"seconds"));
            prior_t.push_back(field(old_bars[b->second],"seconds"));
        }
    }
    const auto old_q=project(source,prior_t).q;
    for (std::size_t i=1;i<t.size();++i) if (t[i]-t[i-1]<=de) {
        issue("boundary_order","小节线物理时间必须严格递增；这不是小节编号顺序限制"); break;
    }
    const bool preserve=model.at("preserve_unedited_positions").get<bool>();
    for (std::size_t i=1, j=0;i<t.size();++i) {
        const double a=t[i-1], b=t[i], units=q[i]-q[i-1];
        const auto m=meter_of(local_meter(labels,a));
        const double actual=units*m.d/4.;
        if (j<prior_idx.size() && prior_idx[j]==i) {
            const double prior=old_q[2*j+1]-old_q[2*j]; ++j;
            if (std::abs(prior-units)>de)
                issue("protected_short_bar","既有弱起或曲尾短节计数需保留；请使用明确的弱起／曲尾编辑入口",a,
                      Json{{"end_seconds",b},{"suggestions",Json::array({"恢复该短节原拍数；普通段的补删拍可继续保留"})}});
            continue;
        }
        if (std::abs(actual-m.n)<=de) continue;
        Json suggestions=Json::array(), proposed=nullptr;
        const int n=(.5<=actual && actual<=32.5) ? round_even_small(actual) : 0;
        if (1<=n && n<=32 && std::abs(actual-n)<de) {
            proposed=Json{{"type","set_meter"},{"seconds",a},{"numerator",n},{"denominator",m.d}};
            suggestions.push_back("将本小节设为 "+std::to_string(n)+"/"+std::to_string(m.d)+"，在下一小节恢复原拍号");
        }
        suggestions.push_back("补回或删除拍旗，或明确修改小节线与拍号");
        if (preserve) suggestions.push_back("如要重新分组现有拍旗，可关闭保留未编辑位置，再框选完整小节执行按当前拍号重分组");
        issue("bar_count",fmt(a)+"–"+fmt(b)+" 秒有 "+fmt(actual)+" 个 "+std::to_string(m.d)+
              " 分音符单位，但标签为 "+std::to_string(m.n)+"/"+std::to_string(m.d),a,
              Json{{"end_seconds",b},{"actual_count",actual},{"denominator",m.d},
                   {"suggestions",std::move(suggestions)},{"proposed_operation",std::move(proposed)}});
    }
    for (const auto& l : labels) if (at(bars,"seconds",field(l,"seconds"),de)==none)
        issue("label_boundary","拍号标签必须在所选拍位建立小节线；不会移回旧小节开头",l.at("seconds"));
    const bool scoped=!scope.is_null();
    const double s=scoped ? field(scope,"start") : 0., e=scoped ? field(scope,"end") : 0.;
    std::unordered_set<std::string_view> changed;
    for (const auto& id : model.at("changed_ids")) changed.insert(text(id));
    for (const char* c : categories) {
        const auto& current=model.at(c);
        const auto now=index(current);
        for (const auto& old : baseline.at(c)) {
            const double x=field(old,"seconds");
            const auto id=text(old.at("id"));
            const auto i=now.find(id);
            const bool external=scoped && (x<s-de || x>e+de);
            if ((external || (preserve && !changed.count(id))) &&
                (i==now.end() || std::abs(field(current[i->second],"seconds")-x)>de))
                issue("protected_position","未编辑的拍旗或小节线位置需要保留；请明确编辑该位置或调整范围",x);
        }
    }
    if (scoped) {
        D probes{e};
        const auto& old_labels=baseline.at("labels");
        for (const Json* rows : {&old_bars,&bars,&old_labels,&labels}) {
            const auto x=column(*rows,"seconds"); probes.insert(probes.end(),x.begin(),x.end());
        }
        sort_unique(probes);
        for (double x : probes) if (x<s-de || x>=e-de) {
            const auto a=meter_of(local_meter(old_labels,x)), b=meter_of(local_meter(labels,x));
            if (a.n!=b.n || a.d!=b.d) {
                issue("scope_meter","拍号变更延伸到选区外；请在选区末端恢复原拍号",x); break;
            }
        }
        D edges{s,e};
        const auto supports=joined(joined(anchor_times(source),anchor_times(clock)),edges);
        D outside;
        for (double x : supports) if (x<=s+de || x>=e-de) outside.push_back(x);
        const auto x0=project(source,outside).q, x1=project(clock,outside).q;
        for (const bool before : {true,false}) {
            double mn=INFINITY,mx=-INFINITY,first=0.;
            for (std::size_t i=0;i<outside.size();++i) if (before ? outside[i]<=s+de : outside[i]>=e-de) {
                if (mn==INFINITY) first=outside[i];
                const double d=x1[i]-x0[i]; mn=std::min(mn,d); mx=std::max(mx,d);
            }
            if (mx-mn>de) {
                issue("scope_mapping","拍数变更会拉伸选区外的拍间映射；请将选区边缘落在保留拍旗上或扩大显式选区",first,
                      Json{{"suggestions",Json::array({"框选包含该拍间隔的完整范围，或恢复边缘附近的拍旗"})}});
                break;
            }
        }
    }
    if (conflicts.empty()) {
        try {
            const auto g=geometry(clock);
            D physical;
            for (double b : g.bars.starts) if (b<g.end_q-de) physical.push_back(b);
            physical.push_back(g.end_q); physical=project(clock,{},physical).t;
            bool match=physical.size()==t.size();
            if (match) for (std::size_t i=0;i<t.size();++i) if (std::abs(physical[i]-t[i])>de) {match=false; break;}
            if (!match) issue("boundary_roundtrip","生效图无法重现草稿的小节边界；请检查弱起、曲尾或拍号标签",nullptr,
                              Json{{"suggestions",Json::array({"保留完整结构草稿并修复边界；不会只保存一部分修改"})}});
        } catch (const std::invalid_argument& ex) { issue("boundary_roundtrip",ex.what()); }
    }
    const bool valid=conflicts.empty();
    Json result{{"valid",valid},{"conflicts",std::move(conflicts)}};
    result["impact"]=Json{{"scope",scope},{"following_bar_number_shift",static_cast<std::int64_t>(bars.size())-static_cast<std::int64_t>(old_bars.size())},
                          {"source_seconds_unchanged",true},{"explicit_operation_count",operation_count}};
    result["summary"]=valid ? std::string("结构草稿已闭合，可一次保存") :
        "结构草稿保留了修改，尚有 "+std::to_string(result.at("conflicts").size())+" 处冲突待解决";
    if (render) {
        Json guidance=Json::array();
        for (const auto& c : result.at("conflicts")) guidance.push_back(Json{{"code",c.at("code")},{"seconds",c.at("seconds")},
            {"message",c.at("message")},{"suggestions",otherwise(c,"suggestions",Json::array())},{"proposed_operation",opt(c,"proposed_operation")}});
        const double first=seconds(clock,opening_beat(clock));
        auto i0=at(bars,"seconds",first,de);
        if (i0==none) i0=0;
        Json numbered=bars;
        for (std::size_t i=0;i<numbered.size();++i) numbered[i]["bar_number"]=static_cast<std::int64_t>(i)-static_cast<std::int64_t>(i0)+1;
        result["guidance"]=std::move(guidance);
        result["display"]=Json{{"bars",std::move(numbered)},{"pulses",model.at("pulses")},{"labels",labels},
            {"beat_flags",flags(model,clock,q)},
            {"unit_explanation","拍旗计数使用拍号分母单位；内部四分音符单位保持不变，6/8 每八分音符为 0.5。"}};
    }
    if (valid) result["tempo"]=std::move(clock);
    return result;
}

void regroup(Json& model,const Json& source,const Json& baseline,const Json& scope,const Json& op,const std::string& seed) {
    if (model.at("preserve_unedited_positions").get<bool>())
        bad("按当前拍号重分组会改变未编辑的小节线；请先关闭“保留未编辑位置”");
    const double a=num(op.at("start")), b=num(op.at("end"));
    auto& bars=model.at("bars");
    const auto& labels=model.at("labels");
    if (b-a<=de || at(bars,"seconds",a,de)==none || at(bars,"seconds",b,de)==none)
        bad("重分组需明确选择两个现有小节边界");
    if (!scope.is_null() && (a<field(scope,"start")-de || b>field(scope,"end")+de))
        bad("重分组不能越过结构编辑选区");
    const auto clock=validated(candidate(source,model,baseline));
    D partition{a,b};
    for (const auto& l : labels) if (a<field(l,"seconds") && field(l,"seconds")<b) partition.push_back(field(l,"seconds"));
    for (std::size_t i=1;i<bars.size();++i) if (exception_bar(bars[i-1])) {
        for (const auto j : {i-1,i}) {
            const double t=field(bars[j],"seconds");
            if (a-de<=t && t<=b+de) partition.push_back(t);
        }
    }
    sort_unique(partition);
    const auto q=project(clock,partition).q;
    D inner;
    // This is the grouping the user requested inside these draft partitions,
    // not an alternate reference-bar generator. Round-trip is still certified
    // by the foundation's effective score geometry before apply.
    for (std::size_t i=1;i<q.size();++i) {
        const double step=meter_of(local_meter(labels,partition[i-1])).length();
        for (std::size_t k=1;q[i-1]+k*step<q[i]-de;++k) inner.push_back(q[i-1]+k*step);
    }
    const auto inside=project(clock,{},inner).t;
    D desired=partition; desired.insert(desired.end(),inside.begin(),inside.end()); sort_unique(desired);
    Json next=Json::array();
    for (const auto& row : bars) if (field(row,"seconds")<a-de || field(row,"seconds")>b+de) next.push_back(row);
    for (double t : desired) {
        const auto i=at(bars,"seconds",t,de);
        if (i!=none) next.push_back(bars[i]);
        else next.push_back(Json{{"id",new_id("regroup",row_digest(seed+Json(t).dump()))},{"seconds",t},{"exception",nullptr}});
    }
    for (const auto& row : bars) {
        const double x=field(row,"seconds");
        if (!(a<x && x<b)) continue;
        const auto i=std::lower_bound(desired.begin(),desired.end(),x);
        if ((i==desired.end() || std::abs(*i-x)>=de) && (i==desired.begin() || std::abs(*std::prev(i)-x)>=de)) touch(model,row);
    }
    sort_rows(next,"seconds"); bars=std::move(next);
}
void edit_model(Json& model,const Json& source,const Json& base,const Json& scope,const Json& op,const std::string& seed) {
    const auto kind=text(opt(op,"type"),"未知结构草稿操作");
    if (kind=="preserve_positions") {
        const auto& value=opt(op,"enabled");
        if (!value.is_boolean()) bad("保留位置开关必须为布尔值");
        model.at("preserve_unedited_positions")=value;
        return;
    }
    if (kind=="regroup_bars") { regroup(model,source,base,scope,op,seed); return; }
    auto& bars=model.at("bars"); auto& pulses=model.at("pulses"); auto& labels=model.at("labels");
    const double end=field(bars.back(),"seconds"), t=num(op.at("seconds"));
    const bool scoped=!scope.is_null();
    const double a=scoped ? field(scope,"start") : 0., b=scoped ? field(scope,"end") : end;
    if (t<0 || t>end+de || (scoped && (t<a-de || t>b+de))) bad("此位置不在结构编辑选区内");
    auto p=at(pulses,"seconds",t,de);
    const auto bar=at(bars,"seconds",t,de);
    if (p==none && (kind=="set_meter" || kind=="add_meter_label" || kind=="delete_pulse" || kind=="calibrate_pulse")) {
        // Only materialize an actual derived flag; adding a label at a
        // derived subdivision splits counts, it does not add a new count.
        const auto clock=validated(candidate(source,model,base));
        const auto displayed=flags(model,clock,project(clock,column(bars,"seconds")).q);
        if (std::any_of(displayed.begin(),displayed.end(),[t](const Json& f){return std::abs(field(f,"seconds")-t)<de;})) {
            p=lower(pulses,"seconds",t);
            if (!p || p==pulses.size()) bad("派生拍旗不在可分割的拍间隔内");
            const auto q=project(clock,{field(pulses[p-1],"seconds"),t}).q;
            const double u=q[1]-q[0];
            pulses[p].at("units")=field(pulses[p],"units")-u;
            pulses.insert(pulses.begin()+static_cast<std::ptrdiff_t>(p),Json{{"id",new_id("derived",seed)},{"seconds",t},{"units",u}});
        }
    }
    if (kind=="add_pulse") {
        if (p!=none) bad("此处已有拍旗；校准位置请用 calibrate_pulse");
        const auto& def=local_meter(labels,t).at("denominator");
        const auto m=meter(Json(1),otherwise(op,"denominator",def),"请选择明确计数单位，例如 6/8 的八分音符");
        pulses.push_back(Json{{"id",new_id("p",seed)},{"seconds",t},{"units",m.unit()}});
    } else if (kind=="delete_pulse" || kind=="calibrate_pulse") {
        if (p==none) bad("没有选中现有拍旗");
        if (t==0 || t==end) bad("不能删除或移动首尾边界");
        if (kind=="delete_pulse") {
            if (bar!=none) bad("该拍旗也是小节线；如需合并，请先删除同位拍号标签（若有），再删除此小节线，最后删拍；合并后的拍数仍需与拍号一致");
            if (op.contains("units") && std::abs(num(op.at("units"))-field(pulses[p],"units"))>de)
                bad("该拍旗当前实际计数为 "+fmt(field(pulses[p],"units"))+" 个四分音符单位；请刷新选中位置，不会按新拍号偷偷重解释");
            touch(model,pulses[p]); pulses.erase(p);
        } else {
            const double target=num(op.at("to_seconds"));
            if (!p || p+1==pulses.size() || !(field(pulses[p-1],"seconds")+de<target && target<field(pulses[p+1],"seconds")-de))
                bad("校准不能越过相邻拍旗");
            if (scoped && !(a+de<t && t<b-de && a+de<target && target<b-de)) bad("不能移动选区边界或移出编辑选区");
            touch(model,pulses[p]);
            if (bar!=none) {
                touch(model,bars[bar]); bars[bar].at("seconds")=target;
                const auto label=at(labels,"seconds",t,de);
                if (label!=none) {touch(model,labels[label]); labels[label].at("seconds")=target;}
                auto& meta=model.at("metadata_seconds");
                for (const char* k : phase_keys) if (source.contains(k)) {
                    const double old=meta.contains(k) ? field(meta,k) : seconds(source,intent_num(source.at(k),"无效开头位置"));
                    if (std::abs(old-t)<de) meta[k]=target;
                }
            }
            pulses[p].at("seconds")=target;
        }
    } else if (kind=="set_meter" || kind=="add_meter_label") {
        const auto m=meter_op(op,"拍号无效");
        if (p==none) bad("请将拍号放在明确拍旗上，或先补拍；不会自动吸附到旧小节线");
        if (bar==none) {
            bars.push_back(Json{{"id",new_id("b",seed)},{"seconds",t},{"exception",nullptr}}); sort_rows(bars,"seconds");
        }
        if (kind=="set_meter") {
            const auto i=std::upper_bound(bars.begin(),bars.end(),t+de,[](double x,const Json& r){return x<field(r,"seconds");});
            if (i!=bars.end()) {
                const double x=field(*i,"seconds");
                if (at(labels,"seconds",x,de)==none) {
                    const auto& r=local_meter(labels,x);
                    Json restore{{"id",new_id("restore",seed)},{"seconds",x},{"numerator",r.at("numerator")},{"denominator",r.at("denominator")}};
                    labels.push_back(std::move(restore)); sort_rows(labels,"seconds");
                }
            }
        }
        const auto i=at(labels,"seconds",t,de);
        if (i!=none) {touch(model,labels[i]); labels[i].update(Json{{"numerator",m.n},{"denominator",m.d}});}
        else labels.push_back(Json{{"id",new_id("m",seed)},{"seconds",t},{"numerator",m.n},{"denominator",m.d}});
    } else if (kind=="delete_meter_label") {
        const auto i=at(labels,"seconds",t,de);
        if (i==none || t==0) bad("请选择非起始拍号标签");
        touch(model,labels[i]); labels.erase(i);
    } else if (kind=="add_bar") {
        if (p==none || bar!=none) bad("请在尚无小节线的现有拍旗处添加小节线");
        bars.push_back(Json{{"id",new_id("b",seed)},{"seconds",t},{"exception",nullptr}});
    } else if (kind=="delete_bar") {
        if (bar==none || truth(opt(bars[bar],"exception")) || t==0) bad("首尾及弱起边界不能隐式删除");
        touch(model,bars[bar]); bars.erase(bar);
    } else bad("未知结构草稿操作");
    for (const char* key : categories) sort_rows(model.at(key),"seconds");
}

// Private per-edit deltas. Changed rows carry their positions on both sides
// so undo and redo keep order even in a recoverable boundary conflict. Neither
// history stack contains copies of the source song or rendered candidates.
Json delta(const Json& before,const Json& after) {
    Json changes=Json::object();
    for (const char* k : categories) {
        const auto& a=before.at(k); const auto& b=after.at(k);
        const auto ia=index(a), ib=index(b);
        Json left=Json::array(), right=Json::array();
        for (std::size_t i=0;i<a.size();++i) {
            const auto j=ib.find(text(a[i].at("id")));
            if (j==ib.end() || a[i]!=b[j->second]) left.push_back(Json::array({i,a[i]}));
        }
        for (std::size_t i=0;i<b.size();++i) {
            const auto j=ia.find(text(b[i].at("id")));
            if (j==ia.end() || b[i]!=a[j->second]) right.push_back(Json::array({i,b[i]}));
        }
        if (!left.empty() || !right.empty()) changes[k]=Json::array({std::move(left),std::move(right)});
    }
    for (const char* k : {"preserve_unedited_positions","metadata_seconds"})
        if (before.at(k)!=after.at(k)) changes[k]=Json::array({before.at(k),after.at(k)});
    const auto& ids=after.at("changed_ids");
    const auto n=before.at("changed_ids").size();
    if (n<ids.size()) {
        Json appended=Json::array();
        for (std::size_t i=n;i<ids.size();++i) appended.push_back(ids[i]);
        changes["touch"]=std::move(appended);
    }
    return changes;
}
void patch(Json& model,const Json& changes,bool forward) {
    const std::size_t to=forward ? 1 : 0;
    for (const char* k : categories) if (changes.contains(k)) {
        const auto& from=changes.at(k).at(1-to);
        const auto& want=changes.at(k).at(to);
        std::unordered_set<std::string_view> removed;
        for (const auto& p : from) removed.insert(text(p.at(1).at("id")));
        auto& rows=model.at(k);
        Json next=Json::array();
        if (from.size()>rows.size()) bad("结构草稿历史不一致");
        const auto size=rows.size()-from.size()+want.size();
        next.get_ref<Json::array_t&>().reserve(size);
        for (std::size_t j=0,i=0,n=0;j<size;++j) {
            if (n<want.size() && want[n].at(0).get<std::size_t>()==j) next.push_back(want[n++].at(1));
            else {
                while (i<rows.size() && removed.count(text(rows[i].at("id")))) ++i;
                if (i==rows.size()) bad("结构草稿历史不一致");
                next.push_back(std::move(rows[i++]));
            }
        }
        rows=std::move(next);
    }
    for (const char* k : {"preserve_unedited_positions","metadata_seconds"})
        if (changes.contains(k)) model.at(k)=changes.at(k).at(to);
    if (changes.contains("touch")) {
        auto& ids=model.at("changed_ids"); const auto& more=changes.at("touch");
        if (forward) for (const auto& id : more) ids.push_back(id);
        else {
            if (more.size()>ids.size()) bad("结构草稿历史不一致");
            ids.erase(ids.end()-static_cast<std::ptrdiff_t>(more.size()),ids.end());
        }
    }
}

bool preserve_option(const Json& request) {
    const Json setting=otherwise(request,"preserve_unedited_positions",Json(true));
    if (!setting.is_boolean()) bad("请明确是否保留未编辑位置。");
    return setting.get<bool>();
}
std::size_t edit_count(const Json& history) {
    return static_cast<std::size_t>(std::count_if(history.begin(),history.end(),[](const Json& h){
        return !is(opt(h.at("operation"),"type"),"preserve_positions");
    }));
}
Json response(const Json& state,const Json& source) {
    const auto& history=state.at("history");
    const auto& scope=state.at("scope");
    const auto& m=state.at("model");
    auto report=inspection(source,m,state.at("baseline"),scope,history.size(),true);
    const bool valid=report.at("valid").get<bool>();
    const auto n=edit_count(history);
    Json view{{"scope",scope},{"preserve_unedited_positions",m.at("preserve_unedited_positions")},
              {"pulses",m.at("pulses")},{"bars",m.at("bars")},{"labels",m.at("labels")}};
    for (const char* k : {"valid","conflicts","tempo","display","guidance","summary","impact"})
        if (report.contains(k)) view[k]=std::move(report.at(k));
    return Json{{"ok",true},{"token",state.at("token")},{"draft_version",state.at("draft_version")},
                {"base_revision",state.at("base_revision")},{"operation_count",n},
                {"can_undo",!history.empty()},{"can_redo",!state.at("redo").empty()},
                {"can_apply",valid && n!=0},{"draft",std::move(view)}};
}
std::string_view required_text(const Json& obj,const char* k,const char* message) {
    const auto s=text(opt(obj,k),message);
    if (s.empty()) bad(message);
    return s;
}
Json replay(const Json& source,std::string_view revision,std::string_view hash,const Json& operations,
            const Json& scope,bool preserve,bool from_session) {
    auto initial=start_model(source,scope,preserve);
    const auto baseline=baseline_of(initial.model);
    std::string seed(hash);
    for (const auto& op : array(operations)) {
        seed=step_seed(seed,op);
        edit_model(initial.model,source,baseline,initial.scope,op,seed);
    }
    auto checked=inspection(source,initial.model,baseline,initial.scope,operations.size(),false);
    if (!checked.at("valid").get<bool>()) {
        if (from_session) bad("请先完成结构草稿中的冲突修正，再应用。");
        bad("结构草稿仍有冲突："+std::string(text(checked.at("conflicts").front().at("message"))));
    }
    return Json{{"tempo",std::move(checked.at("tempo"))},{"fixed_anchors",Json::array()},
                {"source_revision",std::string(revision)},{"source_sha256",std::string(hash)},
                {"operations",operations},{"summary",std::move(checked.at("summary"))},{"impact",std::move(checked.at("impact"))}};
}
Json dispatch_draft(const Json& state,const Json& op,const Json& context) {
    object(op); object(context);
    if (is(opt(op,"type"),"rhythm_structure") && !op.contains("action")) {
        const auto rev=required_text(context,"revision","缺少来源编辑版本");
        const auto sha=required_text(context,"tempo_sha256","缺少来源速度图身份");
        return Json{{"commit",replay(context.at("tempo"),rev,sha,op.at("operations"),opt(op,"scope"),preserve_option(op),false)}};
    }
    const auto action=text(opt(op,"action"),"未知结构草稿操作。");
    const auto project_id=required_text(context,"project_id","缺少草稿工程身份");
    if (action=="begin") {
        const auto rev=required_text(context,"revision","缺少来源编辑版本");
        const auto sha=required_text(context,"tempo_sha256","缺少来源速度图身份");
        const auto token=required_text(context,"new_draft_token","缺少草稿会话身份");
        if (!is(opt(op,"base_revision"),rev)) bad("草稿来源版本不符。");
        const bool preserve=preserve_option(op);
        const auto& source=context.at("tempo");
        auto start=start_model(source,opt(op,"scope"),preserve);
        auto base=baseline_of(start.model);
        Json next{{"version",1},{"project_id",std::string(project_id)},{"token",std::string(token)},{"draft_version",0},
                  {"base_revision",std::string(rev)},{"source_sha256",std::string(sha)},{"initial_preserve",preserve},
                  {"scope",std::move(start.scope)},{"baseline",std::move(base)},{"model",std::move(start.model)},
                  {"history",Json::array()},{"redo",Json::array()}};
        auto reply=response(next,source);
        return Json{{"state",std::move(next)},{"response",std::move(reply)}};
    }
    if (!state.is_object() || !is(opt(state,"project_id"),project_id) ||
        !is(opt(op,"token"),text(state.at("token"))))
        bad("结构草稿已失效，请重新进入编辑；当前工程未被覆盖。");
    constexpr auto stale="草稿已有更新，请使用最新草稿；旧操作未覆盖新内容。";
    const auto version=count(state.at("draft_version"),stale);
    if (count(opt(op,"draft_version"),stale)!=version) bad(stale);
    if (action=="discard") return Json{{"state",nullptr},{"response",Json{{"ok",true},{"discarded",true}}}};
    const auto rev=required_text(context,"revision","缺少来源编辑版本");
    const auto sha=required_text(context,"tempo_sha256","缺少来源速度图身份");
    if (!is(state.at("base_revision"),rev) || !is(state.at("source_sha256"),sha))
        bad("结构草稿版本已过期；请合并当前编辑后再保存");
    if (!is(opt(op,"base_revision"),rev)) bad("草稿来源版本不符。");
    const auto& source=context.at("tempo");
    if (action=="status") return Json{{"response",response(state,source)}};
    if (action=="apply") {
        const auto& history=state.at("history");
        if (!edit_count(history)) bad("请先完成结构草稿中的冲突修正，再应用。");
        Json operations=Json::array();
        for (const auto& h : history) operations.push_back(h.at("operation"));
        // No caller-provided candidate, pins, note arrays or patch is published.
        // Rebuild under the caller's current source identity, then return one
        // commit plan. Do not clear the session until the owner's CAS succeeds.
        return Json{{"commit",replay(source,rev,sha,operations,state.at("scope"),
                                     state.at("initial_preserve").get<bool>(),true)}};
    }
    const auto& previous=state.at("history");
    if (action=="edit") {
        if (previous.size()>=512) bad("本次草稿已达512项操作，请先应用或撤回后继续。");
        if (!opt(op,"operation").is_object()) bad("请选择具体的拍位或拍号操作。");
    } else if (action=="undo") {
        if (previous.empty()) bad("草稿中没有可撤销的操作。");
    } else if (action=="redo") {
        if (state.at("redo").empty()) bad("草稿中没有可重做的操作。");
    } else bad("未知结构草稿操作。");
    if (version==UINT64_MAX) bad(stale);
    Json next=state;
    auto& model=next.at("model"); auto& history=next.at("history"); auto& redo=next.at("redo");
    if (action=="edit") {
        const auto& change=op.at("operation");
        const auto seed=step_seed(previous.empty() ? sha : text(previous.back().at("seed")),change);
        edit_model(model,source,next.at("baseline"),next.at("scope"),change,seed);
        history.push_back(Json{{"operation",change},{"seed",seed},{"delta",delta(state.at("model"),model)}});
        redo=Json::array();
    } else {
        const bool forward=action=="redo";
        auto& from=forward ? redo : history; auto& to=forward ? history : redo;
        Json entry=std::move(from.back()); from.erase(from.size()-1);
        patch(model,entry.at("delta"),forward); to.push_back(std::move(entry));
    }
    next.at("draft_version")=version+1;
    auto reply=response(next,source);
    return Json{{"state",std::move(next)},{"response",std::move(reply)}};
}
}

Json draft_command(const Json& draft,const Json& operation,const Json& context) {
    try { return dispatch_draft(draft,operation,context); }
    catch (const Json::exception& e) { pulse_detail::bad(e.what()); }
}
}
