#include "pulse_commands.h"
#include "pulse_kernel_detail.h"
#include <iterator>
#include <map>
#include <numeric>
#include <unordered_map>
#include <unordered_set>

namespace astra::editor {
namespace {
using namespace pulse_detail;
using IdSet = std::unordered_set<std::string>;
using IdIndex = std::unordered_map<std::string, std::size_t>;

// Only a knot explicitly introduced by a pulse edit can be retired with its
// owning pulse. An implicit ruler move carries a provisional marker until
// the model-generated pulse identities are first materialized.
constexpr auto owner_key = "pulse_constraint_id";

using KnotOwners = std::map<double,Json>;
void claim_deleted_detector_knots(Json& out, const IdSet& targets);
void update_intro_time(Json& out);
KnotOwners knot_owners(const Json& points) {
    KnotOwners out;
    for (const auto& row : points)
        out.emplace(field(row,"seconds"),row.at("id"));
    return out;
}
bool retain_knot(const Json& row, const KnotOwners& owners) {
    if (!row.contains(owner_key)) return true;
    const auto t=field(row,"seconds");
    for (auto i=owners.lower_bound(t-eps);i!=owners.end() && i->first<t+eps;++i)
        if (i->second==row.at(owner_key)) return true;
    return false;
}
void release_clock_owners(Json& out) {
    for (auto& row : out.at("anchors")) row.erase(owner_key);
}


std::string id_key(const Json& id) {
    // Historical snapshots use strings. Preserve scalar legacy IDs as data,
    // including Python's equality of integral numeric IDs.
    if (id.is_boolean()) return id.get<bool>() ? "1" : "0";
    if (id.is_number_float()) {
        const double x = num(id);
        if (std::trunc(x) == x && x >= -0x1p63 && x < 0x1p64) {
            if (x < 0) return std::to_string(static_cast<std::int64_t>(x));
            return std::to_string(static_cast<std::uint64_t>(x));
        }
    } else if (!id.is_primitive()) bad("无效拍点身份");
    return id.dump();
}
IdIndex index_ids(const Json& points) {
    IdIndex by;
    by.reserve(points.size());
    for (std::size_t i = 0; i < points.size(); ++i)
        if (!by.emplace(id_key(points[i].at("id")), i).second) bad("拍点身份重复，请先修复工程");
    return by;
}
class NewIds {
    std::string seed_;
    IdSet used_;
    std::size_t serial_ = 0;
public:
    NewIds(const Json& tempo, const Json& op, const Json& context) {
        seed_ = row_digest(Json::array({opt(context, "revision"), opt(tempo, "last_tempo_operation"), op}).dump());
        for (const auto& p : tempo.at("pulse_points")) used_.insert(id_key(p.at("id")));
    }
    std::string make() {
        for (;;) {
            auto s = row_digest(seed_ + ':' + std::to_string(serial_++)).substr(0, 32);
            if (used_.insert(id_key(Json(s))).second) return s;
        }
    }
};
Json point(const Json& id, double t, double q, double step) {
    return Json{{"id", id}, {"seconds", t}, {"beat", q}, {"step_quarters", step}};
}
void migrate_points(Json& out) {
    const auto end = extent(out);
    const auto& p = opt(out, "pulse_projection");
    double origin = 0., unit = 0.;
    std::size_t n = 0;
    const auto legacy_num = [](const Json& j) {
        return j.is_boolean() ? (j.get<bool>() ? 1. : 0.) : j.is_number() ? j.get<double>() : NAN;
    };
    if (truth(p)) {
        const auto& c = opt(p, "pulse_count");
        unit = legacy_num(otherwise(p, "effective_quarter_span_per_pulse", opt(p, "quarter_span_per_pulse")));
        origin = legacy_num(opt(p, "pulse_ordinal_quarter_origin"));
        const double scale = legacy_num(otherwise(p, "post_projection_beat_scale", Json(1.)));
        if (!std::isnan(scale)) origin *= scale;
        if (c.is_number_integer() && c.get<double>() > 0 && c.get<double>() <= 1000000 &&
            std::isfinite(unit) && unit > 0 && std::isfinite(origin) && origin >= 0)
            n = c.get<std::size_t>();
    }
    D coords, steps;
    if (n) {
        for (std::size_t i = 0; i < n; ++i) {
            const double q = origin + i * unit;
            if (q > end.beat + eps) break;
            coords.push_back(q); steps.push_back(unit);
        }
    } else {
        const auto bars = reference(out, true);
        for (std::size_t i = 0; i < bars.starts.size(); ++i) {
            const double start = bars.starts[i], step = bars.meters[i].unit();
            const double stop = std::min(start + bars.lengths[i], end.beat) + eps;
            for (std::size_t k = 0; start + k * step <= stop; ++k) {
                coords.push_back(start + k * step); steps.push_back(step);
            }
        }
    }
    const auto ts = project(out, {}, coords).t;
    std::unordered_map<std::string, Json> distinct;
    distinct.reserve(ts.size());
    for (std::size_t i = 0; i < ts.size(); ++i) {
        const auto stamp = fixed10(ts[i]);
        const auto ident = "pulse-" + row_digest(stamp + '/' + fixed10(coords[i])).substr(0, 20);
        distinct[stamp == "-0.0000000000" ? "0.0000000000" : stamp] = point(ident, ts[i], coords[i], steps[i]);
    }
    Json points = Json::array();
    points.get_ref<Json::array_t&>().reserve(distinct.size());
    for (auto& kv : distinct) points.push_back(std::move(kv.second));
    sort_rows(points, "seconds");
    for (std::size_t i = 1; i < points.size(); ++i)
        points[i-1]["step_quarters"] = field(points[i], "beat") - field(points[i-1], "beat");
    out["pulse_migration_source"] = n ? "model_projection" : "legacy_meter_grid";
    // Match in beat space: the old ruler and model paths can round-trip an
    // exact knot through seconds with insignificant float error.
    for (auto& row : out.at("anchors")) {
        if (!row.contains(owner_key) || opt(row,owner_key)!=Json(true)) continue;
        const auto q=field(row,"beat");
        const auto i=std::lower_bound(points.begin(),points.end(),q-eps,
                                      [](const Json& p,double v){return field(p,"beat")<v;});
        if (i!=points.end() && std::abs(field(*i,"beat")-q)<eps
                && std::abs(field(row,"seconds")-field(*i,"seconds"))<eps) {
            row[owner_key]=i->at("id");
        } else {
            row.erase(owner_key);
        }
    }
    out["pulse_points"] = std::move(points);
}
void check_points(Json& out, bool fill) {
    auto& points = out.at("pulse_points");
    const auto ts = column(points, "seconds"), qs = column(points, "beat");
    const auto by_id = index_ids(points);
    const double last = extent(out).seconds;
    for (std::size_t i = 0; i < ts.size(); ++i) {
        if (ts[i] < 0 || ts[i] > last + eps) bad("拍点超出音频范围");
        if (i && ts[i] <= ts[i-1] + eps) bad("拍点必须按实际秒位置严格递增");
    }
    const auto mapped = project(out, ts).q;
    for (std::size_t i = 0; i < qs.size(); ++i) {
        if (std::abs(mapped[i] - qs[i]) > 1e-6) bad("拍点与唯一速度映射不一致");
        const double def = i + 1 < qs.size() ? qs[i+1] - qs[i] : 1.;
        const double u = num(otherwise(points[i], "step_quarters", Json(def)), "拍点音乐单位无效");
        if (u <= 0) bad("拍点音乐单位无效");
        if (fill && !points[i].contains("step_quarters")) points[i]["step_quarters"] = def;
    }
    const bool ordered = std::is_sorted(qs.begin(), qs.end());
    for (auto& row : out.at("time_signatures")) {
        const double q = field(row, "beat");
        if (fill && !row.contains("pulse_id")) {
            // The normal case is monotone. Imported projections can have
            // sub-tolerance rounding in the stored beat, so keep first-match
            // behavior even if those redundant beat coordinates are unordered.
            if (ordered) {
                const auto i = at(points, "beat", q);
                if (i != none) row["pulse_id"] = points[i].at("id");
            } else {
                const auto it = std::find_if(qs.begin(), qs.end(), [q](double x) { return std::abs(x-q) < eps; });
                if (it != qs.end()) row["pulse_id"] = points[static_cast<std::size_t>(it-qs.begin())].at("id");
            }
        }
        const auto& ident = opt(row, "pulse_id");
        if (truth(ident)) {
            const auto it = by_id.find(id_key(ident));
            if (it == by_id.end()) bad("拍号所附拍点不存在，请明确重新附着");
            if (std::abs(qs[it->second] - q) > eps) bad("拍号与附属拍点位置不一致，请明确重新附着");
        }
    }
}
void materialize(Json& out) {
    if (!out.contains("pulse_points")) migrate_points(out);
    check_points(out, true);
}

double time_arg(const Json& v, End end) {
    const double t = intent_num(v, "时间必须为有限数值");
    if (t < 0 || t > end.seconds + eps) bad("时间超出音频范围");
    return std::min(t, end.seconds);
}
double exact_time(const Json& v, End end) {
    constexpr auto message = "位置必须在音频范围内";
    const double t = num(v, message);
    if (t < 0 || t > end.seconds) bad(message);
    return t;
}
std::pair<double,double> range(const Json& op, End end) {
    const double a = time_arg(otherwise(op, "start", Json(0)), end);
    const double b = time_arg(otherwise(op, "end", Json(end.seconds)), end);
    if (b-a <= eps) bad("范围右端必须晚于左端");
    return {a,b};
}
Json ordered_meters(Json rows) {
    sort_rows(rows, "beat");
    Json r = Json::array();
    for (auto& v : rows) {
        if (!r.empty() && std::abs(field(r.back(), "beat") - field(v, "beat")) < eps) r.back() = std::move(v);
        else r.push_back(std::move(v));
    }
    return r;
}
Json inherited(const Json& row, double q) {
    Json r = row;
    // Cloning a signature to restore a phase doesn't clone ownership of a
    // different pulse. Keep the association when this is the same boundary.
    if (std::abs(field(row, "beat") - q) >= eps) r.erase("pulse_id");
    r["beat"] = q;
    return r;
}
bool same_span(const Json& a, double l, double r) {
    return std::abs(field(a,"start_seconds")-l) < eps && std::abs(field(a,"end_seconds")-r) < eps;
}

IdSet deletion_ids(const Json& points, const Json& op, End end) {
    const auto& targets = opt(op, "seconds");
    if (!targets.is_array() || targets.empty()) bad("请先选择要删除的拍点");
    const bool keyed = op.contains("pulse_ids");
    const auto& ids = opt(op, "pulse_ids");
    IdIndex by;
    if (keyed) {
        if (!ids.is_array() || ids.size() != targets.size() || std::any_of(ids.begin(), ids.end(), [](const Json& j) {
            return !j.is_string() || text(j).empty();
        })) bad("拍点身份列表无效，请重新选择");
        by = index_ids(points);
    }
    IdSet remove;
    for (std::size_t j = 0; j < targets.size(); ++j) {
        const double t = exact_time(targets[j], end);
        std::size_t i;
        if (keyed) {
            const auto it = by.find(id_key(ids[j]));
            if (it == by.end() || field(points[it->second], "seconds") != t)
                bad("所选拍点身份或位置已变化，请重新选择");
            i = it->second;
        } else {
            const auto k = lower(points, "seconds", t);
            if (k < points.size() && field(points[k], "seconds") == t) i = k;
            else {
                const bool left = k && std::abs(field(points[k-1], "seconds")-t) < eps;
                const bool right = k < points.size() && std::abs(field(points[k], "seconds")-t) < eps;
                if (left == right) bad("无法唯一匹配当前拍点，请重新选择");
                i = left ? k-1 : k;
            }
        }
        remove.insert(id_key(points[i].at("id")));
    }
    return remove;
}
void resolve_meters(Json& out, const IdSet& remove, const Json& remaining, const Json& resolution) {
    const bool prev = is(resolution, "previous"), next = is(resolution, "next"), del = is(resolution, "delete");
    IdIndex by;
    IdSet attached;
    Json rows = Json::array();
    const auto& points = out.at("pulse_points");
    for (const auto& m : out.at("time_signatures")) {
        Json r = m;
        if (m.contains("pulse_id") && remove.count(id_key(m.at("pulse_id")))) {
            if (field(m,"beat") <= eps) r.erase("pulse_id");
            else if (del) continue;
            else {
                if (!prev && !next) bad("所选拍点附有拍号：请选择移到前一拍、后一拍或明确删除拍号");
                if (by.empty()) by = index_ids(points);
                const double t = field(points[by.at(id_key(m.at("pulse_id")))], "seconds");
                std::size_t i = lower(remaining, "seconds", prev ? t-eps : t+eps);
                if (prev) i = i ? i-1 : none;
                else while (i < remaining.size() && field(remaining[i],"seconds") <= t+eps) ++i;
                if (i >= remaining.size()) bad("该方向没有保留拍点，请选择另一方向或明确删除拍号");
                r["pulse_id"] = remaining[i].at("id");
            }
        }
        const auto& ident = opt(r, "pulse_id");
        if (truth(ident) && !attached.insert(id_key(ident)).second)
            bad("多个拍号将落在同一拍点；请先分别处理，未覆盖任何事件");
        rows.push_back(std::move(r));
    }
    out["time_signatures"] = std::move(rows);
}

// Pulses carry no duration of their own: the meter governing a pulse (by
// audio seconds) fixes its musical unit, 4/denominator quarters. Returns true
// when any stored step differed, so the caller can re-derive beat positions.
bool meter_owned_steps(Json& out) {
    if (!out.contains("pulse_points") || !out.contains("time_signatures")) return false;
    auto& pts = out.at("pulse_points");
    const auto& rows = out.at("time_signatures");
    if (!pts.is_array() || pts.empty() || !rows.is_array() || rows.empty()) return false;
    const auto by = index_ids(pts);
    std::vector<std::pair<double,double>> starts;
    D unbound_q, unbound_unit;
    for (const auto& r : rows) {
        const double unit = meter_of(r).unit();
        if (truth(opt(r,"pulse_id"))) {
            const auto it = by.find(id_key(r.at("pulse_id")));
            if (it != by.end()) { starts.emplace_back(field(pts[it->second],"seconds"), unit); continue; }
        }
        const double q = field(r,"beat");
        if (q <= eps) starts.emplace_back(-1., unit);
        else { unbound_q.push_back(q); unbound_unit.push_back(unit); }
    }
    if (!unbound_q.empty()) {
        const auto t = project(out, {}, unbound_q).t;
        for (std::size_t i = 0; i < t.size(); ++i) starts.emplace_back(t[i], unbound_unit[i]);
    }
    std::stable_sort(starts.begin(), starts.end(), [](const auto& a, const auto& b) { return a.first < b.first; });
    // Governing meter segment of each pulse. Only an interval that lies
    // wholly inside one segment is a regular beat; one that ends where a new
    // meter starts can be a pickup, opening sliver or short bar, and the
    // last pulse only plans the tail, so those keep their stored length.
    std::vector<std::size_t> seg(pts.size());
    std::vector<double> unit(pts.size());
    std::size_t k = 0;
    double u = starts.front().second;
    for (std::size_t i = 0; i < pts.size(); ++i) {
        const double t = field(pts[i],"seconds");
        while (k < starts.size() && starts[k].first <= t + eps) u = starts[k++].second;
        seg[i] = k; unit[i] = u;
    }
    bool changed = false;
    for (std::size_t i = 0; i + 1 < pts.size(); ++i) {
        if (seg[i] != seg[i+1]) continue;
        auto& p = pts[i];
        if (!p.contains("step_quarters") || std::abs(field(p,"step_quarters") - unit[i]) > 1e-9) {
            p["step_quarters"] = unit[i];
            changed = true;
        }
    }
    return changed;
}

using ClockSpan = std::pair<double,double>;
void rebuild(Json& out, double former_last, const ClockSpan* uniform = nullptr,
             const std::vector<ClockSpan>* uniforms = nullptr) {
    auto& pts = out.at("pulse_points");
    if (pts.empty()) {
        out["pulse_mode"] = "manual_empty";
        release_clock_owners(out);
        return;
    }
    const auto end = extent(out);
    const auto old_t = anchor_times(out);
    D t = column(pts, "seconds"), q;
    const auto original = project(out, {t.front(), std::max(t.back(), former_last)}).q;
    q.reserve(t.size()+2);
    q.push_back(t.front() < eps ? 0. : original[0]);
    for (std::size_t i = 0; i < pts.size(); ++i) {
        if (i) q.push_back(q.back() + field(pts[i-1], "step_quarters"));
        pts[i]["beat"] = q.back();
    }
    if (t.front() > eps) { t.insert(t.begin(), 0.); q.insert(q.begin(), 0.); }
    if (t.back() < end.seconds-eps) {
        double tail = end.beat-original[1];
        if (tail <= eps) tail = field(pts.back(),"step_quarters");
        t.push_back(end.seconds); q.push_back(q.back()+tail);
    }
    const auto owners=knot_owners(pts);
    // A DuET first-pass clock has exactly one knot per beat. A bare knot at
    // one of those times with no pulse there any more is the leftover of a
    // deleted beat (older cores kept it), not an independent tempo sample:
    // keeping it warps the interval in the seconds view.
    D duet_knots;
    if (const auto& h=opt(out,"rhythm_initial"); h.is_object()) {
        const auto& init=opt(h,"tempo");
        if (init.is_object() && is(opt(init,"source"),"duet_first_pass") && opt(init,"anchors").is_array())
            for (const auto& a : init.at("anchors")) duet_knots.push_back(field(a,"seconds"));
        std::sort(duet_knots.begin(),duet_knots.end());
    }
    const auto orphan=[&](const Json& row) {
        if (duet_knots.empty() || row.contains(owner_key)) return false;
        const double at=field(row,"seconds");
        if (!std::binary_search(duet_knots.begin(),duet_knots.end(),at)) return false;
        const auto near=owners.lower_bound(at-eps);
        return near==owners.end() || near->first>at+eps;
    };
    D basis_t,basis_q,retired;
    const auto& old_rows=out.at("anchors");
    for (std::size_t i=0;i<old_rows.size();++i) {
        const auto& row=old_rows[i];
        if (i==0 || i+1==old_rows.size() || (retain_knot(row,owners) && !orphan(row))) {
            basis_t.push_back(old_t[i]);
            basis_q.push_back(field(row,"beat"));
        } else {
            retired.push_back(old_t[i]);
        }
    }
    const auto samples = joined(basis_t, t);
    // Removing a row but still sampling the old warped curve would leak
    // the deleted pulse's timing kink into neighboring retained knots.
    const auto source_q = retired.empty() ? project(out,samples).q
                                           : interpolate(basis_t,basis_q,samples);
    D x;
    x.reserve(t.size());
    for (double v : t) x.push_back(source_q[static_cast<std::size_t>(std::lower_bound(samples.begin(),samples.end(),v)-samples.begin())]);
    D mapped = interpolate(x, q, source_q);
    const auto flatten = [&](const ClockSpan& span) {
        const auto l = std::lower_bound(samples.begin(),samples.end(),span.first-eps);
        const auto r = std::upper_bound(l,samples.end(),span.second+eps);
        const auto linear = interpolate(t,q,D(l,r));
        std::copy(linear.begin(),linear.end(),mapped.begin()+(l-samples.begin()));
    };
    if (uniform) flatten(*uniform);
    if (uniforms) for (const auto& span : *uniforms) flatten(span);
    // Read all unbound meter/metadata locations through the old curve before
    // installing the new anchors. Bound meters get their owner's exact beat.
    const auto& src_rows = out.at("time_signatures");
    D old_q;
    std::vector<std::size_t> unbound;
    for (std::size_t i=0;i<src_rows.size();++i) {
        const auto& r = src_rows[i];
        if (!truth(opt(r,"pulse_id")) && field(r,"beat") != 0.) {
            unbound.push_back(i); old_q.push_back(field(r,"beat"));
        }
    }
    std::vector<const char*> meta;
    for (const char* k : {"first_full_bar_beat", "grid_origin", "pickup_beats"}) {
        if (out.contains(k) && (std::string_view(k) != "pickup_beats" || truth(out.at(k)))) {
            meta.push_back(k); old_q.push_back(intent_num(out.at(k), "无效开头位置"));
        }
    }
    const auto where = project(out, {}, old_q).t;
    const auto by = index_ids(pts);
    auto anchors=transport_rows(out,old_t,samples,samples,mapped);
    for (std::size_t i=0;i<samples.size();++i) {
        auto& row=anchors[i];
        const auto seconds=samples[i];
        const auto constraint=std::lower_bound(t.begin(),t.end(),seconds);
        if (constraint!=t.end() && *constraint==seconds)
            row["beat"]=q[constraint-t.begin()];
        if (i==0 || i+1==samples.size()) row.erase(owner_key);
        else if (!std::binary_search(basis_t.begin(),basis_t.end(),seconds)) {
            const auto owner=owners.find(seconds);
            if (owner!=owners.end()) row[owner_key]=owner->second;
        }
    }
    out.at("anchors")=std::move(anchors);
    const auto new_q = project(out, where).q;
    auto& rows = out.at("time_signatures");
    for (auto& r : rows) {
        if (truth(opt(r,"pulse_id"))) {
            const auto it = by.find(id_key(r.at("pulse_id")));
            if (it == by.end()) bad("拍号所附拍点不存在，请明确重新附着");
            r["beat"] = pts[it->second].at("beat");
        }
    }
    for (std::size_t i=0;i<unbound.size();++i) rows[unbound[i]]["beat"] = new_q[i];
    sort_rows(rows,"beat");
    for (std::size_t i=1;i<rows.size();++i)
        if (std::abs(field(rows[i],"beat")-field(rows[i-1],"beat")) < eps)
            bad("拍号重排后位置冲突，请先处理附属拍号");
    for (std::size_t i=0;i<meta.size();++i) {
        const double qnew = new_q[unbound.size()+i];
        if (std::string_view(meta[i]) == "pickup_beats" && qnew >= meter_of(rows.front()).length()-eps)
            bad("本次拍数修改会让弱起达到一整小节；请先改为前奏或调整首完整小节");
        out.at(meta[i]) = qnew;
    }
    out["pulse_mode"] = "manual";
}

Json division(double start, double end, int n, const Json& context, bool snap) {
    const double step = (end-start)/n;
    if (step <= 4e-8) bad("Intervals too small to edit safely");
    const double radius = std::min(.12, step*.30);
    D onsets;
    if (snap) {
        const auto& notes = opt(context,"note_onsets");
        if (!notes.is_array()) bad("MIDI onset snapping requires current document evidence");
        for (const auto& v : notes) if (v.is_number()) {
            const double t = v.get<double>();
            if (std::isfinite(t) && start < t && t < end) onsets.push_back(t);
        }
        sort_unique(onsets);
    }
    D times{start};
    Json rows = Json::array();
    for (int i=1;i<n;++i) {
        const double t = start+step*i;
        const auto a = std::lower_bound(onsets.begin(),onsets.end(),t-radius);
        const auto b = std::upper_bound(a,onsets.end(),t+radius);
        double best = t, first = INFINITY, second = INFINITY;
        for (auto it=a;it!=b;++it) {
            const double d = std::abs(*it-t);
            if (d < first) { second=first; first=d; best=*it; }
            else if (d < second) second=d;
        }
        const auto status = b-a > 1 && second-first < .015 ? "ambiguous_onset" : a != b ? "midi_onset" :
                            snap ? "no_nearby_onset" : "uniform";
        times.push_back(best);
        rows.push_back(Json{{"index",i},{"uniform_seconds",t},{"seconds",best},{"delta_seconds",best-t},
                            {"status",status},{"nearby_onset_count",b-a}});
    }
    times.push_back(end);
    return Json{{"beat_seconds",times},{"points",std::move(rows)},{"radius_seconds",radius},
                {"source",snap ? "current_midi_note_onsets" : "uniform_seconds"},{"model_used",false},
                {"interval_count",n},{"endpoints_fixed",true}};
}

struct RecordedTapInput {
    double seconds = 0.;
    bool snap = false;
    double step_quarters = 1.;
};

struct OnsetCluster {
    double onset = 0.;
    double end = 0.;
    int count = 0;
};

inline bool parse_endpoint_inclusion(const Json& obj, const char* key, bool fallback = true) {
    if (!obj.contains(key)) return fallback;
    const auto& v = obj.at(key);
    if (!v.is_boolean()) bad("录拍覆盖区域端点开关无效");
    return v.get<bool>();
}

inline bool in_coverage_bounds(double t, double a, double b, bool include_start, bool include_end) {
    const bool lo = include_start ? (t >= a - eps) : (t > a + eps);
    const bool hi = include_end   ? (t <= b + eps) : (t < b - eps);
    return lo && hi;
}

Json snap_recorded_beats(
    const std::vector<RecordedTapInput>& taps,
    double range_start, double range_end,
    bool has_left_pulse, double left_bound,
    bool has_right_pulse, double right_bound,
    double end_seconds,
    const Json& context,
    bool global_snap,
    double snap_tolerance,
    double cluster_window,
    D& snapped_out, bool strict_coverage = false,
    bool include_start = true, bool include_end = true)
{
    const bool any_snap = std::any_of(taps.begin(), taps.end(), [](const RecordedTapInput& t){ return t.snap; });
    std::vector<OnsetCluster> clusters;
    if (any_snap) {
        const auto& notes = opt(context, "note_onsets");
        if (!notes.is_array()) bad("MIDI onset snapping requires current document evidence");
        const double search_lo = std::max(0., range_start - snap_tolerance);
        const double search_hi = std::min(end_seconds, range_end + snap_tolerance);
        D onsets;
        for (const auto& v : notes) if (v.is_number()) {
            const double t = v.get<double>();
            if (std::isfinite(t) && t >= search_lo - eps && t <= search_hi + eps) onsets.push_back(t);
        }
        sort_unique(onsets);
        const double max_span = cluster_window * 1.5;
        for (double t : onsets) {
            if (clusters.empty() || t - clusters.back().end > cluster_window || t - clusters.back().onset > max_span) {
                clusters.push_back(OnsetCluster{t, t, 1});
            } else {
                clusters.back().end = t;
                clusters.back().count += 1;
            }
        }
    }

    snapped_out.clear();
    snapped_out.reserve(taps.size());
    Json rows = Json::array();
    int snapped_count = 0, ambiguous_count = 0, fallback_count = 0;

    for (std::size_t i = 0; i < taps.size(); ++i) {
        const double raw_t = taps[i].seconds;
        const double left_ref = i > 0 ? taps[i-1].seconds : (has_left_pulse ? left_bound : 0.);
        const double right_ref = i + 1 < taps.size() ? taps[i+1].seconds : (has_right_pulse ? right_bound : end_seconds);
        const double left_clear = (i == 0 && !has_left_pulse) ? std::max(0., raw_t) : std::max(0., (raw_t - left_ref) * .35);
        const double right_clear = (i + 1 == taps.size() && !has_right_pulse) ? std::max(0., end_seconds - raw_t) : std::max(0., (right_ref - raw_t) * .35);
        const double rad_l = std::min(snap_tolerance, left_clear);
        const double rad_r = std::min(snap_tolerance, right_clear);
        double lo = std::max(0., raw_t - rad_l);
        double hi = std::min(end_seconds, raw_t + rad_r);
        if (has_left_pulse) lo = std::max(lo, left_bound + 1e-4);
        if (has_right_pulse) hi = std::min(hi, right_bound - 1e-4);
        if (strict_coverage) {
            lo = std::max(lo, range_start);
            hi = std::min(hi, range_end);
        }

        double chosen = raw_t;
        std::string status = "raw";
        int nearby = 0;
        int cluster_notes = 0;
        if (taps[i].snap && lo <= hi) {
            auto a = std::lower_bound(clusters.begin(), clusters.end(), lo, [](const OnsetCluster& c, double v){ return c.onset < v; });
            auto b = std::upper_bound(a, clusters.end(), hi, [](double v, const OnsetCluster& c){ return v < c.onset; });
            double best = raw_t, first = INFINITY, second = INFINITY;
            int best_notes = 0;
            for (auto it = a; it != b; ++it) {
                if (!include_start && it->onset <= range_start + eps) continue;
                if (!include_end && it->onset >= range_end - eps) continue;
                ++nearby;
                const double d = std::abs(it->onset - raw_t);
                if (d < first) { second = first; first = d; best = it->onset; best_notes = it->count; }
                else if (d < second) second = d;
            }
            if (nearby > 1 && second - first < .015) {
                chosen = best; status = "ambiguous_onset"; cluster_notes = best_notes; ++ambiguous_count; ++snapped_count;
            } else if (nearby > 0) {
                chosen = best; status = "midi_onset"; cluster_notes = best_notes; ++snapped_count;
            } else {
                chosen = raw_t; status = "no_nearby_onset"; ++fallback_count;
            }
        } else if (taps[i].snap) {
            status = "no_nearby_onset"; ++fallback_count;
        }
        // Prevent (0, eps) / (end - eps, end) rebuild endpoint edge collisions.
        if (chosen > 0. && chosen < eps) chosen = (i == 0 && !has_left_pulse) ? 0. : eps * 2.;
        if (chosen < end_seconds && end_seconds - chosen < eps) chosen = (i + 1 == taps.size() && !has_right_pulse) ? end_seconds : end_seconds - eps * 2.;
        snapped_out.push_back(chosen);
        rows.push_back(Json{
            {"index", static_cast<int>(i)},
            {"raw_seconds", raw_t},
            {"seconds", chosen},
            {"delta_seconds", chosen - raw_t},
            {"effective_radius", std::min(rad_l, rad_r)},
            {"status", status},
            {"nearby_cluster_count", nearby},
            {"cluster_note_count", cluster_notes}
        });
    }
    return Json{
        {"source", global_snap || any_snap ? "current_midi_note_onsets" : "raw_taps"},
        {"model_used", false},
        {"tolerance_seconds", snap_tolerance},
        {"chord_cluster_seconds", cluster_window},
        {"snapped_count", snapped_count},
        {"ambiguous_count", ambiguous_count},
        {"fallback_count", fallback_count},
        {"points", std::move(rows)}
    };
}

std::string normalize_smart_owner(const std::string& raw) {
    if (raw.size() >= 2 && raw.front() == '"' && raw.back() == '"')
        return raw.substr(1, raw.size() - 2);
    return raw;
}

bool smart_owner_matches_row(const Json& row, const std::string& owner_raw) {
    const auto norm = normalize_smart_owner(owner_raw);
    if (norm.empty()) return false;
    if (norm == "zero") return std::abs(field(row, "beat")) <= eps;
    const auto& id = opt(row, "pulse_id");
    if (!id.is_null()) {
        const auto rid = id.is_string() ? id.get<std::string>() : normalize_smart_owner(id_key(id));
        if (rid == norm) return true;
    }
    return false;
}

void scrub_orphan_smart_measures(Json& out, const IdSet& remove) {
    if (remove.empty()) return;
    IdSet norm_remove;
    for (const auto& k : remove) {
        norm_remove.insert(k);
        norm_remove.insert(normalize_smart_owner(k));
    }
    Json rows = Json::array();
    for (auto row : out.at("time_signatures")) {
        const auto& smart = opt(row, "smart_measure");
        if (smart.is_object()) {
            const auto& owner = opt(smart, "owner");
            if (owner.is_string() && (norm_remove.count(owner.get<std::string>()) ||
                                      norm_remove.count(normalize_smart_owner(owner.get<std::string>())))) {
                if (truth(opt(smart, "generated")) && field(row, "beat") > eps) continue;
                row.erase("smart_measure");
                row.erase("free");
            }
        }
        rows.push_back(std::move(row));
    }
    out["time_signatures"] = std::move(rows);
}

void meter_bars(Json& out, const Json& op, const Json& context, Json& impact) {
    const auto end = extent(out);
    const double start = exact_time(opt(op,"start"),end), stop = exact_time(opt(op,"end"),end);
    const auto m = meter_op(op,"拍号无效，请检查分子和分母");
    const auto bars = reference(out, false);
    const auto& pts = out.at("pulse_points");
    const auto& sigs = out.at("time_signatures");
    D q = bars.starts;
    for (std::size_t i=0;i<bars.starts.size();++i) q.push_back(bars.starts[i]+bars.lengths[i]);
    const auto t = project(out,{},q).t;
    struct Bar { double a,b,q,size,new_q; };
    std::vector<Bar> selected;
    const auto nbar = bars.starts.size();
    for (std::size_t i=0;i<nbar;++i) {
        const double a=t[i], b=t[i+nbar], q=bars.starts[i];
        if (a>=stop-eps || b<=start+eps) continue;
        const auto& row=effective(sigs,"beat",q,eps);
        if (truth(opt(row,"free")) || std::abs(bars.lengths[i]-meter_of(row).length())>eps)
            bad("范围包含弱起、自由拍或不完整小节，请改选完整小节");
        if (a<start-eps || b>stop+eps || (!selected.empty() && std::abs(a-selected.back().b)>eps))
            bad("请将左右定位点放在小节线上，选中连续的完整小节");
        selected.push_back({a,b,q,bars.lengths[i],(selected.empty()?q:selected.front().q)+selected.size()*m.length()});
    }
    if (selected.empty() || std::abs(selected.front().a-start)>eps || std::abs(selected.back().b-stop)>eps)
        bad("请将左右定位点放在小节线上，选中连续的完整小节");
    const double left=selected.front().a, right=selected.back().b;
    const double old_end=selected.back().q+selected.back().size, new_end=selected.back().new_q+m.length();
    const auto& following=effective(sigs,"beat",old_end,eps);
    const auto sig_t=project(out,{},column(sigs,"beat")).t;
    auto keep_inner_meter=[&](std::size_t i) {
        const auto& row=sigs[i];
        return sig_t[i]>left+eps && sig_t[i]<right-eps
            && opt(row,"origin")=="editor_intent" && !row.contains("smart_measure");
    };
    for (std::size_t i=0;i<sigs.size();++i) if (keep_inner_meter(i)) {
        const auto prior=meter_of(sigs[i]);
        if (prior.n!=m.n || prior.d!=m.d)
            bad("选区内已有手动拍号变化，请分段修改。");
    }
    D targets,boundaries;
    for (const auto& b:selected) {
        boundaries.push_back(b.a);
        for (int i=0;i<m.n;++i) targets.push_back(b.q+b.size*i/m.n);
    }
    boundaries.push_back(right);
    const auto canonical=project(out,{},targets).t;
    NewIds ids(out,op,context);
    Json replacement=Json::array(), repartitioned=Json::array();
    std::map<double,double> exact;
    exact.emplace(0.,0.);
    std::size_t preserved=0, introduced=0, removed=0, removed_meters=0;
    const auto first=lower(pts,"seconds",left-eps);
    for (std::size_t i=0;i<first;++i) replacement.push_back(pts[i]);
    if (first) replacement.back()["step_quarters"]=selected.front().q-field(replacement.back(),"beat");
    Json begin_id, end_id;
    for (std::size_t k=0;k<selected.size();++k) {
        auto& b=selected[k];
        std::vector<std::size_t> slots(m.n,none);
        const auto l=lower(pts,"seconds",b.a-eps), r=lower(pts,"seconds",b.b-eps);
        for (auto j=l;j<r;++j) {
            const double slot=(field(pts[j],"beat")-b.q)*m.n/b.size;
            const int i=static_cast<int>(std::round(slot));
            if (i>=0 && i<m.n && std::abs(slot-i)<=1e-6 && slots[i]==none)
                slots[i]=j;
        }
        std::size_t kept=0;
        for (int i=0;i<m.n;++i) {
            double t0=i==0?b.a:canonical[k*m.n+i];
            const auto j=slots[i];
            const double q0=b.new_q+i*m.unit();
            if (j!=none) {t0=field(pts[j],"seconds");++kept;}
            Json p=j==none ? point(ids.make(),t0,q0,m.unit()) : pts[j];
            p["beat"]=q0; p["step_quarters"]=m.unit();
            if (k==0 && i==0) begin_id=p.at("id");
            exact[t0]=q0;
            replacement.push_back(std::move(p));
        }
        preserved+=kept;
        introduced+=m.n-kept;
        removed+=r-l-kept;
    }
    const auto tail=at(pts,"seconds",right);
    Json boundary=tail==none ? point(ids.make(),right,new_end,1.) : pts[tail];
    boundary["beat"]=new_end;
    end_id=boundary.at("id");
    exact[field(boundary,"seconds")]=new_end;
    if (tail==none) ++introduced; else ++preserved;
    const double change=new_end-old_end;
    const auto after=std::upper_bound(pts.begin(),pts.end(),right+eps,
        [](double x,const Json& p){return x<field(p,"seconds");});
    if (after!=pts.end()) boundary["step_quarters"]=field(*after,"beat")+change-new_end;
    replacement.push_back(std::move(boundary));
    for (auto it=after;it!=pts.end();++it) {
        Json p=*it; p["beat"]=field(p,"beat")+change; replacement.push_back(std::move(p));
    }
    auto map=[&](double t,double q0) {
        const auto i=exact.find(t);
        if (i!=exact.end()) return i->second;
        if (t<left) return q0;
        if (t>=right) return q0+change;
        const auto j=std::upper_bound(boundaries.begin(),boundaries.end(),t)-boundaries.begin()-1;
        const auto& b=selected[j];
        const double f=(q0-b.q)/b.size;
        return b.new_q+m.length()*f;
    };
    std::map<double,Json> knots;
    const auto owners=knot_owners(replacement);
    std::map<double,Json> protected_knots;
    const auto& old_knots=out.at("anchors");
    for (std::size_t index=0;index<old_knots.size();++index) {
        const auto& anchor=old_knots[index];
        const auto t=field(anchor,"seconds");
        const bool owned=retain_knot(anchor,owners);
        if (!owned) {
            if (t<left-eps || t>=right-eps) continue;
            // A deleted display beat does not own a real tempo kink anymore.
            // Keep the kink as geometry; omit redundant collinear subdivisions.
            if (index>0 && index+1<old_knots.size()) {
                const auto& a=old_knots[index-1];
                const auto& b=old_knots[index+1];
                const double at=field(a,"seconds"), bt=field(b,"seconds");
                const double aq=field(a,"beat"), bq=field(b,"beat");
                if (bt>at && std::abs(field(anchor,"beat")-(aq+(bq-aq)*(t-at)/(bt-at)))<1e-9)
                    continue;
            }
        }
        if (!anchor.contains(owner_key)) protected_knots.emplace(t,anchor);
        const auto i=exact.lower_bound(t);
        if ((i!=exact.end() && std::abs(i->first-t)<eps)
                || (i!=exact.begin() && std::abs(std::prev(i)->first-t)<eps)) continue;
        Json a=anchor; a["beat"]=map(t,field(anchor,"beat"));
        if (!owned) a.erase(owner_key);
        knots.emplace(t,std::move(a));
    }
    for (const auto& [t,q0]:exact) {
        auto row=Json{{"seconds",t},{"beat",q0}};
        const auto authored=protected_knots.find(t);
        if (authored!=protected_knots.end()) {
            row=authored->second;
            row["beat"]=q0;
        } else if (!std::binary_search(boundaries.begin(),boundaries.end(),t)) {
            const auto owner=owners.find(t);
            if (owner!=owners.end()) row[owner_key]=owner->second;
        }
        knots.emplace(t,std::move(row));
    }
    Json anchors=Json::array();
    for (auto& [t,a]:knots) anchors.push_back(std::move(a));
    Json rows=Json::array();
    for (std::size_t i=0;i<sigs.size();++i) {
        const double t0=sig_t[i];
        if (t0>=left-eps && t0<right-eps && !keep_inner_meter(i)) {++removed_meters;continue;}
        Json r=sigs[i];r["beat"]=map(t0,field(r,"beat"));rows.push_back(std::move(r));
    }
    rows.push_back(Json{{"beat",selected.front().q},{"numerator",m.n},{"denominator",m.d},
                        {"pulse_id",begin_id},{"origin","editor_intent"}});
    if (std::none_of(rows.begin(),rows.end(),[new_end](const Json& r){return std::abs(field(r,"beat")-new_end)<eps;})) {
        Json r=following;r.update(Json{{"beat",new_end},{"pulse_id",end_id},{"origin","editor_intent"}});
        rows.push_back(std::move(r));
    }
    sort_rows(rows,"beat");
    for (const auto* key:{"first_full_bar_beat","grid_origin","pickup_beats"}) {
        if (out.contains(key)) {
            const double q=field(out,key);
            out[key]=map(project(out,{},D{q}).t.front(),q);
        }
    }
    out.at("time_signatures")=std::move(rows);
    out.at("pulse_points")=std::move(replacement);
    out.at("anchors")=std::move(anchors);
    out["pulse_mode"]="manual";
    impact.update(Json{{"bar_count",selected.size()},{"replaced_point_count",removed},
                       {"replaced_meter_count",removed_meters},{"bar_boundaries_seconds",boundaries},
                       {"preserved_point_count",preserved},{"inserted_point_count",introduced},
                       {"repartitioned_bars",std::move(repartitioned)}});
}


// Ordinary pulse positions and tempo stay untouched. Only the meter rows
// at the edges of this explicit edit can describe bounded residual spans.
bool revised_opening(const Json& out) {
    const auto& policy=opt(out,"opening_policy");
    return policy.is_object() && opt(policy,"keep_before_bar1").is_boolean();
}
bool explicit_opening_meter(const Json& row) {
    return is(opt(row,"origin"),"editor_intent") || opt(row,"smart_measure").is_object();
}

Json smart_meter_rows(Json& out, double q, Json desired, const std::string& owner, Json& impact) {
    auto& current=out.at("time_signatures");
    Json clean=Json::array();
    const auto owner_norm = normalize_smart_owner(owner);
    for (const auto& old:current) {
        Json r=old;
        const auto& smart=opt(r,"smart_measure");
        if (smart.is_object() && opt(smart,"owner").is_string() &&
            normalize_smart_owner(opt(smart,"owner").get<std::string>())==owner_norm) {
            if (truth(opt(smart,"generated"))) continue;
            r.erase("smart_measure");r.erase("free");
        }
        clean.push_back(std::move(r));
    }
    current=std::move(clean);
    const auto first_full=opening_beat(out);
    const bool prelude=revised_opening(out) && q<first_full-eps;
    const auto bars=reference(out,prelude);
    auto active_at=[&](const Json& meters,double location)->Json {
        if (!prelude) return effective(meters,"beat",location,eps);
        for (auto it=meters.rbegin();it!=meters.rend();++it)
            if (field(*it,"beat")<=location+eps && explicit_opening_meter(*it)) return *it;
        auto initial=meters.front();initial["free"]=true;return initial;
    };
    Json rows=Json::array();
    for (const auto& r:current) if (std::abs(field(r,"beat")-q)>eps) rows.push_back(r);
    rows.push_back(std::move(desired));sort_rows(rows,"beat");
    Json report=Json::array();
    const auto marked=rows_or_empty(out,"short_bar_annotations");
    auto protect=[&](double a,double b) {
        if (a<first_full-eps && (!prelude || b>first_full+eps)) return true;
        const auto bounds=project(out,{},D{a,b}).t;
        return std::any_of(marked.begin(),marked.end(),[&](const Json& r) {
            return field(r,"start_seconds")<bounds[1]-eps && field(r,"end_seconds")>bounds[0]+eps;
        });
    };
    auto free_span=[&](double a,double b,const char* side) {
        if (b<=a+eps || protect(a,b)) return;
        const auto i=at(rows,"beat",a);
        if (i!=none && truth(opt(rows[i],"free")) && (!prelude || explicit_opening_meter(rows[i]))) return;
        const auto active=active_at(rows,a);
        if (truth(opt(active,"free"))) return;
        Json change=i==none ? active : rows[i];
        change["beat"]=a;change["free"]=true;
        change["smart_measure"]=Json{{"owner",owner},{"generated",i==none}};
        if (i==none) {
            change.erase("pulse_id");
            // Retain a reference to an existing pulse when possible; never
            // create one, because this is just a grouping annotation.
            const auto at_time=seconds(out,a);
            const auto& points=out.at("pulse_points");
            const auto j=at(points,"seconds",at_time);
            if (j!=none) change["pulse_id"]=points[j].at("id");
            rows.push_back(std::move(change));sort_rows(rows,"beat");
        } else rows[i]=std::move(change);
        const auto bounds=project(out,{},D{a,b}).t;
        report.push_back(Json{{"start",bounds[0]},{"end",bounds[1]},{"side",side}});
    };
    const auto left=std::lower_bound(bars.starts.begin(),bars.starts.end(),q-eps);
    if (left!=bars.starts.begin()) {
        const auto i=static_cast<std::size_t>(std::prev(left)-bars.starts.begin());
        const double a=bars.starts[i], b=a+bars.lengths[i];
        const auto active=active_at(current,a);
        if (q<=b+eps && q-a<meter_of(active).length()-eps && !truth(opt(active,"free")))
            free_span(a,q,"before");
    }
    auto next=std::find_if(rows.begin(),rows.end(),[&](const Json& r){
        const double at=field(r,"beat");
        if (at<=q+eps) return false;
        const auto& sm=opt(r,"smart_measure");
        if (sm.is_object() && truth(opt(sm,"generated"))) return false;
        return !prelude || at>=first_full-eps || explicit_opening_meter(r);
    });
    const double following=next==rows.end() ? INFINITY : field(*next,"beat");
    // Same smart-measure arithmetic; a first numbered bar is a real right
    // boundary even without an authored signature row at that coordinate.
    const double fixed=prelude ? std::min(following,first_full) : following;
    if (std::isfinite(fixed)) {
        const double unit=meter_of(active_at(rows,q)).length();
        const double full=q+std::floor((fixed-q+eps)/unit)*unit;
        if (full<fixed-eps) free_span(full,fixed,"after");
    }
    impact["smart_measures"]=std::move(report);
    return rows;
}

void reconcile_smart_measures(Json& out) {
    auto& sigs = out.at("time_signatures");
    const auto has_smart = [](const Json& r) {
        return opt(r, "smart_measure").is_object();
    };
    const auto is_gen_smart = [](const Json& r) {
        const auto& sm = opt(r, "smart_measure");
        return sm.is_object() && truth(opt(sm, "generated"));
    };
    if (!std::any_of(sigs.begin(), sigs.end(), has_smart)) return;

    // 1. Collect non-generated real meter rows to verify smart owners still exist.
    std::vector<Json> real_check;
    for (const auto& r : sigs) {
        if (!is_gen_smart(r)) real_check.push_back(r);
    }
    Json surviving = Json::array();
    for (auto r : sigs) {
        if (has_smart(r)) {
            const auto& sm = r.at("smart_measure");
            const auto& ow = opt(sm, "owner");
            bool owner_alive = false;
            if (ow.is_string()) {
                const auto owner_s = ow.get<std::string>();
                owner_alive = std::any_of(real_check.begin(), real_check.end(), [&](const Json& cand) {
                    return smart_owner_matches_row(cand, owner_s);
                });
            }
            if (!owner_alive) {
                if (truth(opt(sm, "generated")) && field(r, "beat") > eps) continue;
                r.erase("smart_measure");
                r.erase("free");
            }
        }
        surviving.push_back(std::move(r));
    }
    sort_rows(surviving, "beat");
    if (!std::any_of(surviving.begin(), surviving.end(), has_smart)) {
        sigs = std::move(surviving);
        return;
    }

    // 2. Re-evaluate every section bounded by consecutive real (non-generated) meter rows.
    const double first_full = opening_beat(out);
    const bool rev_open = revised_opening(out);
    const auto marked = rows_or_empty(out, "short_bar_annotations");
    auto protect = [&](double a, double b, bool in_prelude) {
        if (a < first_full - eps && (!in_prelude || b > first_full + eps)) return true;
        const auto bounds = project(out, {}, D{a, b}).t;
        return std::any_of(marked.begin(), marked.end(), [&](const Json& r) {
            return field(r, "start_seconds") < bounds[1] - eps && field(r, "end_seconds") > bounds[0] + eps;
        });
    };

    std::vector<Json> real_rows;
    std::vector<Json> gen_rows;
    for (const auto& r : surviving) {
        if (is_gen_smart(r)) gen_rows.push_back(r);
        else real_rows.push_back(r);
    }

    Json rebuilt = Json::array();
    const auto& points = out.at("pulse_points");
    for (std::size_t m = 0; m < real_rows.size(); ++m) {
        Json base = real_rows[m];
        const double a = field(base, "beat");
        const bool in_prelude = rev_open && a < first_full - eps;
        double b = (m + 1 < real_rows.size()) ? field(real_rows[m + 1], "beat") : INFINITY;
        if (in_prelude) b = std::min(b, first_full);

        const bool base_smart = has_smart(base);
        const bool manual_free = truth(opt(base, "free")) && !base_smart;
        std::vector<const Json*> sec_gen;
        for (const auto& g : gen_rows) {
            const double gb = field(g, "beat");
            if (gb > a + eps && gb < b - eps) sec_gen.push_back(&g);
        }

        if (manual_free || (!base_smart && sec_gen.empty())) {
            rebuilt.push_back(std::move(base));
            for (const auto* g : sec_gen) rebuilt.push_back(*g);
            continue;
        }

        // Preserve smart owner string from this section.
        std::string owner_str;
        if (base_smart && opt(base.at("smart_measure"), "owner").is_string()) {
            owner_str = base.at("smart_measure").at("owner").get<std::string>();
        } else if (!sec_gen.empty() && opt(sec_gen.back()->at("smart_measure"), "owner").is_string()) {
            owner_str = sec_gen.back()->at("smart_measure").at("owner").get<std::string>();
        }
        base.erase("smart_measure");
        base.erase("free");

        if (!std::isfinite(b) || b <= a + eps) {
            rebuilt.push_back(std::move(base));
            continue;
        }

        const double unit_len = meter_of(base).length();
        const double span = b - a;
        const double nearest_bars = std::round(span / unit_len);
        const bool exact_full = (span >= unit_len - eps) && (std::abs(span - nearest_bars * unit_len) <= eps);
        if (exact_full) {
            // Reached complete regular measure(s): automatically exit temporary smart state!
            rebuilt.push_back(std::move(base));
            continue;
        }

        const double full = a + std::floor((span + eps) / unit_len) * unit_len;
        if (full <= a + eps) {
            if (!protect(a, b, in_prelude)) {
                base["free"] = true;
                base["smart_measure"] = Json{{"owner", owner_str}, {"generated", false}};
            }
            rebuilt.push_back(std::move(base));
        } else {
            rebuilt.push_back(base);
            if (full < b - eps && !protect(full, b, in_prelude)) {
                Json gen = base;
                gen["beat"] = full;
                gen["free"] = true;
                gen["smart_measure"] = Json{{"owner", owner_str}, {"generated", true}};
                gen.erase("pulse_id");
                const auto at_time = seconds(out, full);
                const auto j = at(points, "seconds", at_time);
                if (j != none) gen["pulse_id"] = points[j].at("id");
                rebuilt.push_back(std::move(gen));
            }
        }
    }
    sort_rows(rebuilt, "beat");
    sigs = std::move(rebuilt);
}



// One multi-region take uses one original pulse set and one rebuild. In
// particular, meter reassignment may not land on a pulse another region
// will delete, and a rejected region cannot publish any earlier region.
std::string record_regions(Json& out, const Json& op, const Json& context, Json& impact) {
    auto& points = out.at("pulse_points");
    const auto end = extent(out);
    const auto& input = opt(op,"regions");
    if (!input.is_array() || input.empty() || input.size()>256 ||
        op.contains("start") || op.contains("end") || op.contains("beats") ||
        op.contains("include_start") || op.contains("include_end"))
        bad("录拍覆盖区域无效");
    const auto replace_meter = otherwise(op,"meter_resolution",Json("delete"));
    if (!is(replace_meter,"delete") && !is(replace_meter,"previous") && !is(replace_meter,"next"))
        bad("录拍拍号处理方式无效");
    const double default_unit = num(otherwise(op,"pulse_unit",Json(1.)));
    const double tol = num(otherwise(op,"snap_tolerance_seconds",Json(.08)));
    const double cluster = num(otherwise(op,"chord_cluster_seconds",Json(.025)));
    if (!(default_unit>0.) || !(tol>0. && tol<=.20) || !(cluster>=0. && cluster<=.06))
        bad("录拍选项无效");
    const auto global_snap=truth(opt(op,"snap_to_midi"));

    struct Region {
        double a=0.,b=0.,left=0.,right=0.;
        bool include_start=true,include_end=true;
        bool has_left=false,has_right=false;
        std::vector<RecordedTapInput> taps;
        D snapped;
        Json removed=Json::array(),snap=Json::object();
        int bound=0,unbound=0;
        bool crosses_bar1=false;
    };
    std::vector<Region> regions;
    regions.reserve(input.size());
    std::size_t total=0;
    for (const auto& row:input) {
        object(row);
        Region r;
        r.a=exact_time(opt(row,"start"),end);
        r.b=exact_time(opt(row,"end"),end);
        r.include_start=parse_endpoint_inclusion(row,"include_start",true);
        r.include_end=parse_endpoint_inclusion(row,"include_end",true);
        if (r.b<=r.a+eps || (!regions.empty() && r.a<=regions.back().b+2*eps))
            bad("录拍覆盖区域必须按时间排列且互不相接");
        r.right=end.seconds;
        const auto& beats=opt(row,"beats");
        if (!beats.is_array() || beats.empty() || beats.size()>16384-total)
            bad("录拍覆盖区域需要有效拍点");
        total+=beats.size();
        r.taps.reserve(beats.size());
        for (const auto& item:beats) {
            RecordedTapInput tap;
            if (item.is_number()) {
                tap.seconds=exact_time(item,end); tap.snap=global_snap; tap.step_quarters=default_unit;
            } else if (item.is_object()) {
                tap.seconds=exact_time(opt(item,"seconds"),end);
                tap.snap=item.contains("snap") ? truth(item.at("snap")) : global_snap;
                tap.step_quarters=item.contains("step_quarters") ? num(item.at("step_quarters")) : default_unit;
            } else bad("录拍拍点格式无效");
            const bool tap_ok_lo=r.include_start ? (tap.seconds>=r.a) : (tap.seconds>r.a+eps);
            const bool tap_ok_hi=r.include_end ? (tap.seconds<=r.b) : (tap.seconds<r.b-eps);
            if (!tap_ok_lo || !tap_ok_hi || !(tap.step_quarters>0.) ||
                (!r.taps.empty() && tap.seconds<=r.taps.back().seconds+eps))
                bad("录拍拍点超出覆盖区域或次序无效");
            r.taps.push_back(tap);
        }
        regions.push_back(std::move(r));
    }
    const auto coverage=[&](double t) -> Region* {
        auto it=std::lower_bound(regions.begin(),regions.end(),t-eps,
            [](const Region& r,double value){return r.b<value;});
        if (it!=regions.end() && in_coverage_bounds(t,it->a,it->b,it->include_start,it->include_end))
            return &*it;
        if (it!=regions.end() && ++it!=regions.end() &&
            in_coverage_bounds(t,it->a,it->b,it->include_start,it->include_end))
            return &*it;
        return nullptr;
    };
    const bool was_empty=points.empty();
    const double former_last=was_empty ? -1. : field(points.back(),"seconds");
    const bool keep_zero=!was_empty && field(points.front(),"seconds")<eps &&
        regions.front().include_start && regions.front().a<=eps && regions.front().taps.front().seconds>.15;
    IdSet remove;
    Json surviving=Json::array(), removed=Json::array();
    for (const auto& point_:points) {
        const auto t=field(point_,"seconds");
        auto* region=coverage(t);
        if (!region || (keep_zero && t<eps)) {
            surviving.push_back(point_);
            continue;
        }
        remove.insert(id_key(point_.at("id")));
        const Json item{{"id",point_.at("id")},{"seconds",t}};
        region->removed.push_back(item); removed.push_back(item);
    }
    const auto outside=surviving.size();

    bool has_bar1=out.contains("first_full_bar_beat"), grid_matches=false,pickup_matches=false;
    double old_b1_q=0.,old_b1_t=0.;
    Region* bar1_relock=nullptr;
    if (has_bar1) {
        old_b1_q=intent_num(out.at("first_full_bar_beat"),"Invalid opening beat");
        old_b1_t=project(out,{},D{old_b1_q}).t.front();
        if (auto* r=coverage(old_b1_t)) {
            r->crosses_bar1=true;
            const auto i=at(points,"seconds",old_b1_t);
            if (i==none || remove.count(id_key(points[i].at("id")))) bar1_relock=r;
        }
        grid_matches=out.contains("grid_origin") &&
            std::abs(intent_num(out.at("grid_origin"),"Invalid grid origin")-old_b1_q)<eps;
        pickup_matches=truth(opt(out,"pickup_beats")) &&
            std::abs(intent_num(out.at("pickup_beats"),"Invalid pickup")-old_b1_q)<eps;
    }
    const auto& sigs=out.at("time_signatures");
    const auto sig_times=project(out,{},column(sigs,"beat")).t;
    for (std::size_t i=0;i<sigs.size();++i) {
        const auto& sig=sigs[i];
        if (field(sig,"beat")<=eps) continue;
        if (auto* r=coverage(sig_times[i])) {
            if (sig.contains("pulse_id") && remove.count(id_key(sig.at("pulse_id")))) ++r->bound;
            else if (!truth(opt(sig,"pulse_id"))) ++r->unbound;
        }
    }
    claim_deleted_detector_knots(out,remove);
    resolve_meters(out,remove,surviving,replace_meter);
    scrub_orphan_smart_measures(out,remove);
    D independent;
    for (const auto& row:out.at("anchors"))
        if (!row.contains(owner_key)) independent.push_back(field(row,"seconds"));
    sort_unique(independent);
    const D surviving_times=column(surviving,"seconds");
    double prev=-INFINITY;
    std::vector<ClockSpan> windows;
    windows.reserve(regions.size());
    for (auto& r:regions) {
        const auto l=r.include_start
            ? std::lower_bound(surviving_times.begin(),surviving_times.end(),r.a-eps)
            : std::upper_bound(surviving_times.begin(),surviving_times.end(),r.a+eps);
        if (l!=surviving_times.begin()) {r.has_left=true;r.left=*(l-1);}
        else if (keep_zero && r.include_start && r.a<=eps) {r.has_left=true;r.left=0.;}
        const auto h=r.include_end
            ? std::upper_bound(surviving_times.begin(),surviving_times.end(),r.b+eps)
            : std::lower_bound(surviving_times.begin(),surviving_times.end(),r.b-eps);
        if (h!=surviving_times.end()) {r.has_right=true;r.right=*h;}
        r.snap=snap_recorded_beats(r.taps,r.a,r.b,r.has_left,r.left,r.has_right,r.right,
            end.seconds,context,global_snap,tol,cluster,r.snapped,true,r.include_start,r.include_end);
        for (std::size_t i=0;i<r.snapped.size();++i) {
            auto& t=r.snapped[i];
            const auto it=std::lower_bound(independent.begin(),independent.end(),t-eps);
            if (it!=independent.end() &&
                (r.include_start ? (*it>=r.a) : (*it>r.a+eps)) &&
                (r.include_end ? (*it<=r.b) : (*it<r.b-eps)) &&
                std::abs(*it-t)<eps &&
                *it>prev+eps && (!r.has_left||*it>r.left+eps) && (!r.has_right||*it<r.right-eps) &&
                (i+1==r.snapped.size() || *it<r.snapped[i+1]-eps)) {
                t=*it;
                r.snap["points"][i]["seconds"]=t;
                r.snap["points"][i]["delta_seconds"]=t-r.taps[i].seconds;
            }
            if (!in_coverage_bounds(t,r.a,r.b,r.include_start,r.include_end) || t<=prev+eps ||
                    (r.has_left && t<=r.left+eps) || (r.has_right && t>=r.right-eps))
                bad("录拍吸附与保留边界冲突，请调整覆盖范围");
            prev=t;
        }
        if (!windows.empty() && r.left<=windows.back().second+eps) windows.back().second=std::max(windows.back().second,r.right);
        else windows.emplace_back(r.left,r.right);
    }

    NewIds ids(out,op,context);
    for (const auto& r:regions)
        for (std::size_t i=0;i<r.snapped.size();++i)
            surviving.push_back(point(ids.make(),r.snapped[i],0.,r.taps[i].step_quarters));
    sort_rows(surviving,"seconds");
    points=std::move(surviving);
    rebuild(out,former_last,nullptr,&windows);

    if (has_bar1) {
        auto target=old_b1_t;
        if (bar1_relock) {
            const auto& v=bar1_relock->snapped;
            target=v.front();
            for (const auto t:v) if (std::abs(t-old_b1_t)<std::abs(target-old_b1_t)-eps) target=t;
        }
        if (target!=old_b1_t || project(out,{},D{old_b1_q}).t.front()!=old_b1_t) {
            const double q=project(out,D{target}).q.front();
            if (pickup_matches && (q<=eps || q>=meter_of(out.at("time_signatures").front()).length()-eps))
                bad("录拍后的弱起超过拍号限制，请调整覆盖范围");
            out["first_full_bar_beat"]=q;
            if (grid_matches) out["grid_origin"]=q;
            if (pickup_matches) out["pickup_beats"]=q;
        }
    }
    update_intro_time(out);
    reconcile_smart_measures(out);

    if (was_empty && regions.front().snapped.front()>eps) impact["requires_opening_choice"]=true;
    bool crosses=false;
    int bound=0,unbound=0,ns=0,na=0,nf=0;
    auto region_results=Json::array(), flat=Json::array();
    auto source=regions.front().snap.at("source");
    for (std::size_t n=0;n<regions.size();++n) {
        auto& r=regions[n];
        crosses|=r.crosses_bar1;bound+=r.bound;unbound+=r.unbound;
        ns+=r.snap.at("snapped_count").get<int>();na+=r.snap.at("ambiguous_count").get<int>();
        nf+=r.snap.at("fallback_count").get<int>();
        if (r.snap.at("source")!="raw_taps") source=r.snap.at("source");
        for (const auto& pt:r.snap.at("points")) {
            auto row=pt;row["region_index"]=n;row["region_point_index"]=row["index"];row["index"]=flat.size();
            flat.push_back(std::move(row));
        }
        region_results.push_back(Json{{"start_seconds",r.a},{"end_seconds",r.b},
            {"include_start",r.include_start},{"include_end",r.include_end},
            {"inserted_point_count",r.snapped.size()},{"replaced_point_count",r.removed.size()},
            {"replaced_points",std::move(r.removed)},{"crosses_bar1",r.crosses_bar1},
            {"replaced_meter_count",r.bound},{"unbound_meters_in_range_count",r.unbound},
            {"snap_preview",std::move(r.snap)}});
    }
    impact.update(Json{{"start_seconds",regions.front().a},{"end_seconds",regions.back().b},
        {"inserted_point_count",total},{"replaced_point_count",remove.size()},
        {"replaced_points",std::move(removed)},{"preserved_outside_count",outside},
        {"replaced_meter_count",bound},{"unbound_meters_in_range_count",unbound},
        {"crosses_bar1",crosses},{"regions",std::move(region_results)},
        {"snap_preview",Json{{"source",source},{"model_used",false},{"tolerance_seconds",tol},
            {"chord_cluster_seconds",cluster},{"snapped_count",ns},{"ambiguous_count",na},
            {"fallback_count",nf},{"points",std::move(flat)}}}});
    return "已录拍覆盖 "+std::to_string(regions.size())+" 段，写入 "+std::to_string(total)+" 个拍点";
}

std::string edit_pulses(Json& out, const Json& op, std::string_view kind, const Json& context, Json& impact) {
    materialize(out);
    auto& pts=out.at("pulse_points");
    const auto end=extent(out);
    const double former_last=pts.empty() ? -1. : field(pts.back(),"seconds");
    impact["tempo_changed"]=true;
    auto& sigs=out.at("time_signatures");
    if (kind=="set_meter") {
        const double t=exact_time(opt(op,"seconds"),end);
        const auto i=at(pts,"seconds",t);
        const double q=i==none ? beat(out,t) : field(pts[i],"beat");
        const auto j=at(sigs,"beat",q);
        if (i==none && t!=0 && (j==none || !opt(sigs[j],"smart_measure").is_object()))
            bad("拍号需要附在原有拍点；请先选择拍点");
        const auto m=meter_op(op,"拍号无效");
        Json r=j==none ? Json::object() : sigs[j];
        const bool manual_free=truth(opt(r,"free")) && !opt(r,"smart_measure").is_object();
        r.update(Json{{"beat",q},{"numerator",m.n},{"denominator",m.d},{"origin","editor_intent"}});
        r.erase("free");r.erase("smart_measure");
        if (i!=none && truth(pts[i].at("id"))) r["pulse_id"]=pts[i].at("id");
        const bool prefix_meter=revised_opening(out) && q<opening_beat(out)-eps;
        if (manual_free && !prefix_meter) {
            Json rows=Json::array();
            for (const auto& old:sigs) if (std::abs(field(old,"beat")-q)>eps) rows.push_back(old);
            rows.push_back(std::move(r));sort_rows(rows,"beat");sigs=std::move(rows);
        } else {
            const auto owner=i==none ? (t==0 ? "zero" : fmt(t)) : id_key(pts[i].at("id"));
            sigs=smart_meter_rows(out,q,std::move(r),owner,impact);
        }
        if (q==0 && intent_num(otherwise(out,"pickup_beats",Json(0)),"无效弱起")>=m.length())
            bad("新拍号容不下当前弱起，请先调整开头解释");
        impact["tempo_changed"]=false;
        return "拍号已附着该拍点；只改变分组，物理拍点与速度映射不变";
    }
    if (kind=="move_meter") {
        const double t=exact_time(opt(op,"seconds"),end), target=exact_time(opt(op,"to_seconds"),end);
        const auto pi=at(pts,"seconds",target);
        const auto times=project(out,{},column(sigs,"beat")).t;
        const auto it=std::find_if(times.begin(),times.end(),[t](double x){return std::abs(x-t)<eps;});
        if (pi==none || it==times.end()) bad("请选择现有拍号及其目标拍点");
        const auto i=static_cast<std::size_t>(it-times.begin());
        if (field(sigs[i],"beat")==0) bad("初始拍号不能移动，只能修改");
        const double q=field(pts[pi],"beat");
        for (std::size_t j=0;j<sigs.size();++j)
            if (j!=i && std::abs(field(sigs[j],"beat")-q)<eps) bad("目标拍点已有拍号，未覆盖");
        sigs[i]["beat"]=pts[pi].at("beat"); sigs[i]["pulse_id"]=pts[pi].at("id");
        sort_rows(sigs,"beat");
        reconcile_smart_measures(out);
        impact["tempo_changed"]=false;
        return "拍号已移至目标拍点；持续到下一个拍号";
    }
    if (kind=="clear_beats") {
        if (sigs.size()>1 && !is(opt(op,"meter_resolution"),"delete"))
            bad("从零布拍将清除附属变拍拍号，请明确选择删除这些拍号");
        Json first=sigs.front(); first.erase("pulse_id");
        sigs=Json::array({std::move(first)}); pts=Json::array();
        out["pulse_mode"]="manual_empty";
        release_clock_owners(out);
        impact["tempo_changed"]=false;
        return "拍点已清空，可连续点击布拍；原始音符和速度初稿保留";
    }
    if (kind=="meter_bars") {
        meter_bars(out,op,context,impact);
        return "已按新拍号均分所选小节；小节线的实际时间保持不变";
    }
    std::string summary;
    Json supplement_owner;
    if (kind=="add_beat") {
        const double t=exact_time(opt(op,"seconds"),end);
        if (at(pts,"seconds",t)!=none) bad("该位置已有拍点");
        if (std::any_of(sigs.begin(),sigs.end(),[](const Json& r){return opt(r,"smart_measure").is_object();})) {
            const double q=beat(out,t);
            const auto& row=effective(sigs,"beat",q,eps);
            const auto& smart=opt(row,"smart_measure");
            if (smart.is_object() && q>field(row,"beat")+eps) supplement_owner=opt(smart,"owner");
        }
        constexpr auto msg="新增拍点单位必须为正数四分音符单位";
        const double unit=num(otherwise(op,"pulse_unit",Json(1.)),msg);
        if (unit<=0) bad(msg);
        if (pts.empty() && t>eps) impact["requires_opening_choice"]=true;
        NewIds ids(out,op,context);
        const auto i=lower(pts,"seconds",t);
        pts.insert(pts.begin()+static_cast<std::ptrdiff_t>(i),point(ids.make(),t,beat(out,t),unit));
        summary="已新增一拍；已有拍点秒位置不变，后续重新编号";
    } else if (kind=="set_beat_duration") {
        const auto& targets=opt(op,"seconds");
        if (!targets.is_array() || targets.empty()) bad("请先选择拍点");
        constexpr auto msg="时值必须为正数四分音符单位";
        const double unit=num(opt(op,"duration_quarters"),msg);
        if (unit<=0) bad(msg);
        bool tail_only=true;
        for (const auto& v : targets) {
            const auto i=at(pts,"seconds",exact_time(v,end));
            if (i==none) bad("所选拍点已变化，请重新选择");
            pts[i]["step_quarters"]=unit;
            tail_only=tail_only && i+1==pts.size();
        }
        if (tail_only) {
            impact["tempo_changed"]=false;
            return "Tail interval planned; source clock unchanged";
        }
        summary="已修改到下一拍点的谱面时值；原始秒位置不变";
    } else if (kind=="delete_beats") {
        const auto remove=deletion_ids(pts,op,end);
        claim_deleted_detector_knots(out,remove);
        Json remaining=Json::array();
        for (const auto& p : pts) if (!remove.count(id_key(p.at("id")))) remaining.push_back(p);
        resolve_meters(out,remove,remaining,otherwise(op,"meter_resolution",Json("delete")));
        pts=std::move(remaining);
        summary="已删除 "+std::to_string(remove.size())+" 个拍点；其余拍点及原始事件秒位置不变";
    } else if (kind=="divide_beats") {
        const double a=exact_time(opt(op,"start"),end), b=exact_time(opt(op,"end"),end);
        constexpr auto msg="请选择递增两端和 1–4096 个间隔";
        const int n=integer(opt(op,"interval_count"),1,4096,msg);
        if (b<=a) bad(msg);
        const auto i=at(pts,"seconds",a), j=at(pts,"seconds",b);
        if (i==none || j==none) bad("均分的两端必须是已有拍点，请先添加端点");
        const double q_i=field(pts[i],"beat"), q_j=field(pts[j],"beat");
        const auto times=project(out,{},column(sigs,"beat")).t;
        Json kept_sigs=Json::array();
        for (std::size_t k=0;k<sigs.size();++k) {
            if (a+eps<times[k] && times[k]<b-eps) {
                // A generated Smart row only describes the previous layout.
                // It will be reconciled from the real meter rows after division.
                const auto& smart=opt(sigs[k],"smart_measure");
                if (smart.is_object() && truth(opt(smart,"generated"))) continue;
                bad("选区内有拍号变化，请分段均分");
            }
            kept_sigs.push_back(sigs[k]);
        }
        const Json left_meter=effective(sigs,"beat",q_i,eps);
        const double beat_unit=meter_of(left_meter).unit();
        static constexpr double legal_units[]={0.125,0.25,0.5,0.75,1.0,1.5,2.0,3.0,4.0};
        const double u0=field(pts[i],"step_quarters");
        bool uniform_legal=std::any_of(std::begin(legal_units),std::end(legal_units),
                                       [u0](double v){return std::abs(u0-v)<=1e-6;});
        if (uniform_legal) {
            for (std::size_t k=i;k<j;++k) {
                if (std::abs(field(pts[k],"step_quarters")-u0)>1e-6) { uniform_legal=false; break; }
            }
        }
        const double unit=(uniform_legal && std::abs((q_j-q_i)-n*u0)<=1e-5) ? u0 : beat_unit;
        Json proposal=division(a,b,n,context,truth(opt(op,"snap_to_midi")));
        IdSet remove;
        Json remaining=Json::array(), removed=Json::array();
        for (std::size_t k=0;k<pts.size();++k) {
            const auto& p=pts[k];
            const double t=field(p,"seconds");
            if (a+eps<t && t<b-eps) {
                remove.insert(id_key(p.at("id")));
                removed.push_back(Json{{"id",p.at("id")},{"seconds",p.at("seconds")}});
            } else {
                remaining.push_back(p);
                if (k==i) remaining.back()["step_quarters"]=unit;
            }
        }
        claim_deleted_detector_knots(out,remove);
        sigs=std::move(kept_sigs);
        NewIds ids(out,op,context);
        for (int k=1;k<n;++k) remaining.push_back(point(ids.make(),num(proposal.at("beat_seconds").at(k)),0.,unit));
        sort_rows(remaining,"seconds"); pts=std::move(remaining);
        const std::pair<double,double> uniform{a,b};
        rebuild(out,former_last,&uniform);
        reconcile_smart_measures(out);
        impact.update(Json{{"division_preview",std::move(proposal)},{"replaced_points",std::move(removed)},
                           {"interval_count",n},{"point_count",n+1},{"replaced_point_count",remove.size()},
                           {"replaced_meter_count",0},{"unit_quarters",unit}});
        return "两端间已按秒均分 "+std::to_string(n)+" 个间隔，共 "+std::to_string(n+1)+" 个拍点";
    } else if (kind=="record_beats") {
        if (op.contains("regions")) return record_regions(out,op,context,impact);
        const double a=exact_time(opt(op,"start"),end), b=exact_time(opt(op,"end"),end);
        const bool include_start=parse_endpoint_inclusion(op,"include_start",true);
        const bool include_end=parse_endpoint_inclusion(op,"include_end",true);
        if (b<=a+eps) bad("录制覆盖范围无效：结束时间必须晚于开始时间");
        constexpr auto unit_msg="录制拍点单位必须为正数四分音符单位";
        const double default_unit=num(otherwise(op,"pulse_unit",Json(1.)),unit_msg);
        if (default_unit<=0.) bad(unit_msg);
        const bool global_snap=truth(opt(op,"snap_to_midi"));
        const double snap_tol=num(otherwise(op,"snap_tolerance_seconds",Json(.08)),"吸附容差无效");
        if (snap_tol<=0. || snap_tol>.20) bad("吸附容差须在 0–200 毫秒内");
        const double cluster_win=num(otherwise(op,"chord_cluster_seconds",Json(.025)),"和弦聚类窗口无效");
        if (cluster_win<0. || cluster_win>.06) bad("和弦聚类窗口须在 0–60 毫秒内");
        const auto& raw_arr=opt(op,"beats");
        if (!raw_arr.is_array() || raw_arr.empty()) bad("未录得有效拍点，无法覆盖区间");
        if (raw_arr.size()>16384) bad("单次录制拍点数量超出上限");
        std::vector<RecordedTapInput> taps;
        taps.reserve(raw_arr.size());
        for (const auto& item : raw_arr) {
            RecordedTapInput tap;
            if (item.is_number()) {
                tap.seconds=exact_time(item,end);
                tap.snap=global_snap;
                tap.step_quarters=default_unit;
            } else if (item.is_object()) {
                tap.seconds=exact_time(opt(item,"seconds"),end);
                tap.snap=item.contains("snap") ? truth(item.at("snap")) : global_snap;
                tap.step_quarters=item.contains("step_quarters") ? num(item.at("step_quarters"),unit_msg) : default_unit;
                if (tap.step_quarters<=0.) bad(unit_msg);
            } else bad("录制拍点格式无效");
            const bool tap_ok_lo=include_start ? (tap.seconds>=a-eps) : (tap.seconds>a+eps);
            const bool tap_ok_hi=include_end ? (tap.seconds<=b+eps) : (tap.seconds<b-eps);
            if (!tap_ok_lo || !tap_ok_hi) bad("录制拍点超出指定覆盖区间");
            if (!taps.empty() && tap.seconds<=taps.back().seconds+eps) bad("录制拍点必须严格按时间递增");
            taps.push_back(tap);
        }
        const bool was_empty=pts.empty();
        // Preserve a locked song-start pulse at t=0 when the first tap clearly occurs after zero
        // (>150ms), so recording near the opening never shifts the whole song onto fractional beats.
        const bool keep_zero_boundary=(!pts.empty() && field(pts.front(),"seconds")<eps && include_start && a<=eps && taps.front().seconds>0.15);
        IdSet remove;
        Json surviving=Json::array(), removed=Json::array();
        for (const auto& p : pts) {
            const double t=field(p,"seconds");
            if (in_coverage_bounds(t,a,b,include_start,include_end) && !(keep_zero_boundary && t<eps)) {
                remove.insert(id_key(p.at("id")));
                removed.push_back(Json{{"id",p.at("id")},{"seconds",p.at("seconds")}});
            } else {
                surviving.push_back(p);
            }
        }
        if (surviving.size()+taps.size()<1) bad("不能清空工程的全部拍点");
        bool has_left_pulse=false, has_right_pulse=false;
        double left_bound=0., right_bound=end.seconds;
        for (const auto& p : surviving) {
            const double t=field(p,"seconds");
            const bool before_a=include_start ? (t<a-eps) : (t<=a+eps);
            const bool after_b=include_end ? (t>b+eps) : (t>=b-eps);
            if (t<taps.front().seconds-eps && (before_a || (keep_zero_boundary && t<eps))) {
                has_left_pulse=true; left_bound=t;
            } else if (!has_right_pulse && after_b) {
                has_right_pulse=true; right_bound=t;
            }
        }
        const bool strict_bounds=!include_start || !include_end;
        D snapped;
        Json snap_preview=snap_recorded_beats(
            taps, a, b, has_left_pulse, left_bound, has_right_pulse, right_bound,
            end.seconds, context, global_snap, snap_tol, cluster_win, snapped,
            strict_bounds, include_start, include_end);
        // Check monotonicity & clearance against surviving pulses after snap.
        for (std::size_t k=0;k<snapped.size();++k) {
            if (strict_bounds && !in_coverage_bounds(snapped[k],a,b,include_start,include_end))
                bad("录拍吸附与保留边界冲突，请调整覆盖范围");
            if (has_left_pulse && snapped[k]<=left_bound+eps) bad("首拍与区间前既有拍点过近，请调整覆盖起点或重录");
            if (has_right_pulse && snapped[k]>=right_bound-eps) bad("末拍与区间后既有拍点过近，请调整覆盖终点或重录");
            if (k && snapped[k]<=snapped[k-1]+eps) bad("吸附后拍点发生重叠或反序，请重试");
        }
        // Preserve the first natural bar in time across an earlier overwrite.
        bool crosses_bar1=false, relock_bar1=false, has_bar1=false;
        bool grid_origin_matches_bar1=false, pickup_matches_bar1=false;
        double old_b1_t=0., old_b1_q=0.;
        if (out.contains("first_full_bar_beat")) {
            has_bar1=true;
            old_b1_q=intent_num(out.at("first_full_bar_beat"),"Invalid opening beat");
            old_b1_t=project(out,{},D{old_b1_q}).t.front();
            crosses_bar1=in_coverage_bounds(old_b1_t,a,b,include_start,include_end);
            const auto idx=at(pts,"seconds",old_b1_t);
            // A retained pulse, including the protected song start, stays fixed.
            relock_bar1=crosses_bar1 && (idx==none || remove.count(id_key(pts[idx].at("id")))!=0);
            grid_origin_matches_bar1=out.contains("grid_origin") &&
                std::abs(intent_num(out.at("grid_origin"),"Invalid grid origin")-old_b1_q)<eps;
            pickup_matches_bar1=out.contains("pickup_beats") && truth(out.at("pickup_beats")) &&
                std::abs(intent_num(out.at("pickup_beats"),"Invalid pickup")-old_b1_q)<eps;
        }
        // Count bound & unbound meters inside range before mutation.
        int replaced_bound_meters=0, unbound_meters_in_range=0;
        {
            const auto sig_times=project(out,{},column(sigs,"beat")).t;
            for (std::size_t k=0;k<sigs.size();++k) {
                const auto& m=sigs[k];
                if (m.contains("pulse_id") && remove.count(id_key(m.at("pulse_id")))) {
                    if (field(m,"beat")>eps) ++replaced_bound_meters;
                } else if (!truth(opt(m,"pulse_id")) && field(m,"beat")>eps) {
                    if (in_coverage_bounds(sig_times[k],a,b,include_start,include_end)) ++unbound_meters_in_range;
                }
            }
        }
        claim_deleted_detector_knots(out,remove);
        resolve_meters(out,remove,surviving,otherwise(op,"meter_resolution",Json("delete")));
        scrub_orphan_smart_measures(out,remove);
        // Guard against <eps exact-double collision with retained independent knots in basis_t.
        D indep_times;
        for (const auto& row : out.at("anchors")) {
            if (!row.contains(owner_key)) indep_times.push_back(field(row,"seconds"));
        }
        sort_unique(indep_times);
        for (std::size_t k=0;k<snapped.size();++k) {
            auto it=std::lower_bound(indep_times.begin(),indep_times.end(),snapped[k]-eps);
            if (it!=indep_times.end() && std::abs(*it-snapped[k])<eps && *it!=snapped[k]) {
                const double cand=*it;
                const bool ok_bounds=!strict_bounds || in_coverage_bounds(cand,a,b,include_start,include_end);
                const bool ok_l=(k>0 ? cand>snapped[k-1]+eps : (!has_left_pulse || cand>left_bound+eps));
                const bool ok_r=(k+1<snapped.size() ? cand<snapped[k+1]-eps : (!has_right_pulse || cand<right_bound-eps));
                if (ok_bounds && ok_l && ok_r) {
                    snapped[k]=cand;
                    snap_preview["points"][k]["seconds"]=cand;
                    snap_preview["points"][k]["delta_seconds"]=cand-taps[k].seconds;
                }
            }
        }
        if (was_empty && snapped.front()>eps) impact["requires_opening_choice"]=true;
        const std::size_t preserved_outside=surviving.size();
        NewIds ids(out,op,context);
        for (std::size_t k=0;k<snapped.size();++k)
            surviving.push_back(point(ids.make(),snapped[k],0.,taps[k].step_quarters));
        sort_rows(surviving,"seconds");
        pts=std::move(surviving);
        // Linearize the entire touched boundary span [left_bound, right_bound] so every
        // recorded beat AND both entry/exit boundary beats subdivide uniformly (no leftover kinks).
        const double uniform_l=has_left_pulse ? left_bound : 0.;
        const double uniform_r=has_right_pulse ? right_bound : end.seconds;
        const std::pair<double,double> uniform{uniform_l,uniform_r};
        rebuild(out,former_last,&uniform);
        if (has_bar1) {
            double target=old_b1_t;
            if (relock_bar1) {
                // Only the newly recorded (and natively snapped) beats are eligible.
                auto best=snapped.begin();
                for (auto it=best+1;it!=snapped.end();++it) {
                    if (std::abs(*it-old_b1_t)<std::abs(*best-old_b1_t)-eps) best=it;
                }
                target=*best;
            }
            if (target!=old_b1_t || project(out,{},D{old_b1_q}).t.front()!=old_b1_t) {
                const double q=project(out,D{target}).q.front();
                if (pickup_matches_bar1 &&
                    (q<=eps || q>=meter_of(out.at("time_signatures").front()).length()-eps))
                    bad("录拍后弱起长度超出首个拍号，请调整覆盖范围或先重设第一小节");
                out["first_full_bar_beat"]=q;
                if (grid_origin_matches_bar1) out["grid_origin"]=q;
                if (pickup_matches_bar1) out["pickup_beats"]=q;
            }
        }
        update_intro_time(out);
    reconcile_smart_measures(out);
        impact.update(Json{
            {"start_seconds",a},
            {"end_seconds",b},
            {"include_start",include_start},
            {"include_end",include_end},
            {"inserted_point_count",snapped.size()},
            {"replaced_point_count",remove.size()},
            {"replaced_points",std::move(removed)},
            {"preserved_outside_count",preserved_outside},
            {"replaced_meter_count",replaced_bound_meters},
            {"unbound_meters_in_range_count",unbound_meters_in_range},
            {"crosses_bar1",crosses_bar1},
            {"snap_preview",std::move(snap_preview)}
        });
        return "已录制覆盖 "+std::to_string(snapped.size())+" 个拍点（替换 "+std::to_string(remove.size())+" 个）";
    } else bad("未知拍点操作");
    rebuild(out,former_last);
    reconcile_smart_measures(out);
    return summary;
}

D visible_times_for_single_move(const Json& tempo) {
    const auto end=extent(tempo);
    if (tempo.contains("pulse_points")) {
        D t=column(tempo.at("pulse_points"),"seconds");
        t.push_back(0.); t.push_back(end.seconds); sort_unique(t);
        return t;
    }
    // Single legacy calibration uses the displayed ruler, not model ordinals.
    const auto b=reference(tempo,true);
    D q{0.,end.beat};
    for (std::size_t i=0;i<b.starts.size();++i) {
        const double start=b.starts[i], stop=start+b.lengths[i], unit=b.meters[i].unit();
        for (std::size_t k=0;start+k*unit<std::min(stop,end.beat)-eps;++k) q.push_back(start+k*unit);
        if (stop<=end.beat+eps) q.push_back(std::min(stop,end.beat));
    }
    sort_unique(q);
    return project(tempo,{},q).t;
}
D warp_inside(const D& x, const D& y, const D& values) {
    D at_inside;
    for (double v : values) if (x.front()<v && v<x.back()) at_inside.push_back(v);
    const auto moved=interpolate(x,y,at_inside);
    D out=values;
    for (std::size_t i=0,k=0;i<out.size();++i) if (x.front()<out[i] && out[i]<x.back()) out[i]=moved[k++];
    return out;
}
bool recognized_detector_clock(const Json& tempo) {
    // DuET first-pass maps are model clocks too: one knot per beat.
    return is(opt(tempo,"source"),"cubase_local_reconstruction")
        || is(opt(tempo,"source"),"beat_this")
        || is(opt(tempo,"source"),"duet_first_pass");
}
bool detector_knot(const Json& rows, double t, double q) {
    try {
        const auto found=std::lower_bound(rows.begin(),rows.end(),t,
            [](const Json& a,double v){return field(a,"seconds")<v;});
        return found!=rows.end() && found->size()==2
            && field(*found,"seconds")==t && field(*found,"beat")==q;
    } catch (const std::invalid_argument&) {
        return false; // Optional historical provenance must not block an edit.
    } catch (const Json::exception&) {
        return false;
    }
}
const Json* detector_origin_at(const Json& tempo, double t) {
    // Reference comes from the immutable imported detector clock, not the
    // current mutable beat numbering or a guessed near-time match.
    const auto& h=opt(tempo,"rhythm_initial");
    if (!h.is_object()) return nullptr;
    const auto& initial=opt(h,"tempo");
    if (!initial.is_object() || !recognized_detector_clock(initial)) return nullptr;
    const auto& rows=opt(initial,"anchors");
    if (!rows.is_array() || rows.size()<3) return nullptr;
    try {
        const auto found=std::lower_bound(rows.begin(),rows.end(),t,
            [](const Json& a,double v){return field(a,"seconds")<v;});
        if (found==rows.begin() || found==rows.end() || std::next(found)==rows.end()
            || found->size()!=2 || field(*found,"seconds")!=t)
            return nullptr;
        return &*found;
    } catch (const std::invalid_argument&) {
        return nullptr;
    } catch (const Json::exception&) {
        return nullptr;
    }
}
bool detector_pulse_identity(const Json& tempo, double t, const Json& owner) {
    // The digest identity below already proves the pulse was generated at
    // this exact detector knot, whichever way the pulses were materialized.
    if (!owner.is_string()) return false;
    const auto* origin=detector_origin_at(tempo,t);
    if (!origin) return false;
    try {
        const auto expected="pulse-"+row_digest(fixed10(t)+'/'+fixed10(field(*origin,"beat"))).substr(0,20);
        return owner==Json(expected);
    } catch (const std::invalid_argument&) {
        return false;
    } catch (const Json::exception&) {
        return false;
    }
}
void claim_deleted_detector_knots(Json& out, const IdSet& targets) {
    auto& rows=out.at("anchors");
    for (const auto& p : out.at("pulse_points")) {
        const auto& id=p.at("id");
        if (!targets.count(id_key(id))) continue;
        const auto t=field(p,"seconds");
        if (!detector_pulse_identity(out,t,id)) continue;
        const auto row=std::lower_bound(rows.begin(),rows.end(),t,
            [](const Json& a,double v){return field(a,"seconds")<v;});
        if (row!=rows.end() && row->size()==2 && field(*row,"seconds")==t)
            (*row)[owner_key]=id;
    }
}
void install_warp(Json& out, const D& x, const D& y, const KnotOwners& owners) {
    const auto old_t=anchor_times(out), support=joined(old_t,x);
    const auto destinations=warp_inside(x,y,support);
    const auto source_q=project(out,support).q;
    auto rows=transport_rows(out,old_t,support,destinations,source_q);
    const auto& history=opt(out,"rhythm_initial");
    const auto& initial=history.is_object() ? opt(history,"tempo") : history;
    const auto& detector=initial.is_object() && recognized_detector_clock(initial) ? initial : out;
    const auto& detector_rows=opt(detector,"anchors");
    const bool detector_known=recognized_detector_clock(detector) && detector_rows.is_array();
    for (std::size_t i=0;i<support.size();++i) {
        const auto owner=owners.find(support[i]);
        if (owner==owners.end() || rows[i].contains(owner_key)) continue;
        const bool existing=std::binary_search(old_t.begin(),old_t.end(),support[i]);
        if (!existing) {
            rows[i][owner_key]=owner->second;
        } else if (support[i]!=destinations[i] && detector_known && rows[i].size()==2) {
            // Exact coincidence with a pulse does not prove ownership for a
            // manually authored/legacy knot. Only claim a moved bare sample
            // known to be from a detector and at its original coordinates.
            if (detector_pulse_identity(out,support[i],owner->second)
                || detector_knot(detector_rows,support[i],source_q[i]))
                rows[i][owner_key]=owner->second;
        }
    }
    out.at("anchors")=std::move(rows);
}
void update_intro_time(Json& out) {
    if (out.contains("intro_downbeat_seconds") && out.contains("first_full_bar_beat")) {
        const double t=seconds(out,opening_beat(out));
        out.at("intro_downbeat_seconds")=t;
        if (is(opt(out,"opening_mode"),"normal") && out.contains("score_start_seconds")) out.at("score_start_seconds")=t;
    }
}
std::string move_single(Json& out, const Json& op, Json& impact) {
    const auto end=extent(out);
    const auto points=visible_times_for_single_move(out);
    const double requested=time_arg(opt(op,"seconds"),end);
    if (!(eps<requested && requested<end.seconds-eps)) bad("不能增删或移动曲目首尾边界");
    auto i=std::lower_bound(points.begin(),points.end(),requested-eps);
    if (i==points.end() || (std::abs(*i-requested)>=eps && (++i==points.end() || std::abs(*i-requested)>=eps)))
        bad("请选择当前网格的一面拍旗，不是速度曲线的采样点");
    // Reuse the exact existing selected coordinate; a rounded UI argument
    // is an identity lookup, not another independent timing support.
    const double t=*i;
    const auto l=std::lower_bound(points.begin(),points.end(),t-eps);
    const auto r=std::upper_bound(points.begin(),points.end(),t+eps);
    if (l==points.begin() || r==points.end()) bad("拍点缺少相邻边界");
    double a=*std::prev(l), b=*r;
    const double target=time_arg(opt(op,"to_seconds"),end);
    if (!opt(op,"scope").is_null()) {
        const auto& sc=op.at("scope");
        if (!sc.is_object()) bad("编辑范围无效，请重新框选");
        const auto [s,e]=range(sc,end);
        if (!(s+eps<t && t<e-eps)) bad("只能校准选区内拍点；选区两端作为临时固定位置");
        a=std::max(a,s); b=std::min(b,e);
    }
    if (!(a+eps<target && target<b-eps)) bad("目标越过相邻拍点或固定点；补拍/删拍请用对应工具");
    const D x{a,t,b}, y{a,target,b};
    KnotOwners owners;
    if (out.contains("pulse_points")) {
        const auto& explicit_points=out.at("pulse_points");
        const auto chosen=at(explicit_points,"seconds",t);
        if (chosen!=none && field(explicit_points[chosen],"seconds")==t)
            owners.emplace(t,explicit_points[chosen].at("id"));
    } else {
        owners.emplace(t,true);
    }
    install_warp(out,x,y,owners);
    if (out.contains("pulse_points")) {
        auto& pts=out.at("pulse_points");
        const auto moved=warp_inside(x,y,column(pts,"seconds"));
        for (std::size_t k=0;k<moved.size();++k) if (field(pts[k],"seconds")!=moved[k]) pts[k]["seconds"]=moved[k];
    }
    update_intro_time(out);
    reconcile_smart_measures(out);
    impact.update(Json{{"start_seconds",a},{"end_seconds",b},{"tempo_changed",true}});
    return "拍点已校准；相邻拍点与固定点保持，音符和音频不动";
}
std::string move_group(Json& out, const Json& op, Json& impact) {
    materialize(out);
    auto& pts=out.at("pulse_points");
    const auto end=extent(out);
    const auto& values=opt(op,"seconds");
    if (!values.is_array() || values.empty()) bad("请选择要移动的拍点");
    D sel;
    for (const auto& j : values) sel.push_back(time_arg(j,end));
    sort_unique(sel);
    const double delta=intent_num(opt(op,"delta_seconds"),"移动距离必须为有限数值");
    std::unordered_set<std::size_t> moved;
    for (double t : sel) {
        const auto i=at(pts,"seconds",t);
        if (i==none) bad("所选拍点已改变，请重新选择");
        const double p=field(pts[i],"seconds");
        if (!(eps<t && t<end.seconds-eps && 0<p && p<end.seconds))
            bad("曲目首尾边界不能整组移动；请取消选择边界");
        moved.insert(i);
    }
    D x{0.}, y{0.};
    bool prior_moved=false;
    const auto append=[&](double s,double d,bool change) {
        if (!std::isfinite(d) || d<=y.back() || ((change || prior_moved) && d<=y.back()+eps))
            bad("整组移动不能越过相邻未选拍点、固定点或曲目边界");
        x.push_back(s); y.push_back(d); prior_moved=change;
    };
    for (std::size_t i=0;i<pts.size();++i) {
        const double t=field(pts[i],"seconds");
        const bool change=moved.count(i)!=0;
        if (0<t && t<end.seconds) append(t,change ? t+delta : t,change);
    }
    append(end.seconds,end.seconds,false);
    install_warp(out,x,y,knot_owners(pts));
    for (const auto i : moved) pts[i]["seconds"]=field(pts[i],"seconds")+delta;
    update_intro_time(out);
    reconcile_smart_measures(out);
    impact.update(Json{{"tempo_changed",true},{"moved_count",sel.size()},{"delta_seconds",delta}});
    return "已整体移动 "+std::to_string(sel.size())+" 个拍点；拍号随所属拍点移动，音符和音频不动";
}

std::string opening(Json& out, const Json& op, const Json& context, Json& warnings) {
    if (op.contains("keep_before_bar1")) {
        const auto& keep=op.at("keep_before_bar1");
        if (!keep.is_boolean()) bad("请选择是否保留第 1 小节前的内容");
        const double t=num(opt(op,"seconds"),"第 1 小节起点必须为有限秒数");
        if (t<0 || t>=extent(out).seconds-eps) bad("第 1 小节必须位于音频结束之前");
        const double q=beat(out,t);
        const auto& rows=out.at("time_signatures");
        auto meter=effective(rows,"beat",q,eps);
        // A previous fallback isolates the post-marker meter when a newly
        // authored prefix signature has since replaced the inherited old row.
        const auto& previous=opt(out,"opening_policy");
        const auto old_full=opening_beat(out);
        if (revised_opening(out) && previous.is_object() && opt(previous,"bar1_meter").is_object() &&
            field(meter,"beat")<old_full-eps && std::abs(field(meter,"beat")-q)>eps)
            meter=previous.at("bar1_meter");
        Json fallback{{"numerator",meter.at("numerator")},{"denominator",meter.at("denominator")}};
        if (truth(opt(meter,"free")) && !opt(meter,"smart_measure").is_object()) fallback["free"]=true;
        out.update(Json{{"pickup_beats",0.},{"first_full_bar_beat",q},{"grid_origin",q},
            {"intro_downbeat_seconds",t},{"opening_mode",keep==true && t>eps?"intro":"normal"},
            {"opening_policy",Json{{"source","user"},{"protected",true},{"keep_before_bar1",keep},
                                   {"bar1_meter",std::move(fallback)}}}});
        if (keep==false) {
            out.at("opening_policy")["score_only"]=true;
            if(t>eps) out["score_start_seconds"]=t;
            else out.erase("score_start_seconds");
        } else out.erase("score_start_seconds");
        return std::string("第 1 小节从 ")+fmt(t)+" 秒开始；"+
               (keep==true?"保留前段谱面":"不保留前段谱面")+"，原始演奏时间不变";
    }

    const auto mode=text(opt(op,"mode"),"请选择正常、弱起或前奏开头");
    if (mode!="normal" && mode!="pickup" && mode!="intro") bad("请选择正常、弱起或前奏开头");
    const double t=num(otherwise(op,"seconds",mode=="normal" ? Json(0) : Json(nullptr)),"第一完整小节位置必须为有限秒数");
    if (t<0 || t>=extent(out).seconds-eps) bad("第一完整小节必须位于音频结束之前");
    if (mode!="normal" && t<=eps) bad("没有前段时请选择正常开头；弱起或前奏需要正的起始长度");
    const double q=beat(out,t);
    Json rows=Json::array();
    for (const auto& r : out.at("time_signatures")) {
        if (!rows.empty() && is(opt(r,"origin"),"editor_first_bar") &&
            r.at("numerator")==rows.back().at("numerator") && r.at("denominator")==rows.back().at("denominator")) continue;
        rows.push_back(r);
    }
    if (mode=="pickup") {
        for (const auto& r : rows) if (eps<field(r,"beat") && field(r,"beat")<q-eps)
            bad("弱起内部存在拍号变化；请保留为前奏，或先明确处理这些拍号事件");
        const auto& inherit=effective(rows,"beat",q,eps);
        if (!(eps<q && q<meter_of(inherit).length()-eps)) bad("弱起必须短于继承拍号的一小节；较长前段请选择前奏，不会自动截断");
        rows.front().at("numerator")=inherit.at("numerator");
        rows.front().at("denominator")=inherit.at("denominator");
    }
    out.update(Json{{"time_signatures",std::move(rows)},{"pickup_beats",mode=="pickup" ? q : 0.},
                    {"first_full_bar_beat",q},{"grid_origin",q},{"opening_mode",std::string(mode)},
                    {"intro_downbeat_seconds",t},{"opening_policy",Json{{"source","user"},{"protected",true}}}});
    if (mode=="normal") out["opening_policy"]["score_only"]=true;
    if (mode=="normal" && t>eps) out["score_start_seconds"]=t;
    else out.erase("score_start_seconds");
    if (mode=="intro" || (mode=="normal" && t>0))
        warnings.push_back("若开头长度无法精确写成标准 MIDI 短拍号，导出会保留播放时间并写入边界标记和拍号事件；不保证 Cubase 等外部 DAW 在该位置重起小节，小节网格可能需手动对齐。");
    if (mode=="intro") warnings.push_back("第一完整小节之前的音符和拍号全部保留为前奏，不会自动并入弱起");
    return std::string("开头设为")+(mode=="normal" ? "正常开头" : mode=="pickup" ? "弱起" : "前奏")+
           "；第一完整小节在 "+fmt(t)+" 秒，原始演奏时间不变";
}
void set_free(Json& out, const Json& op, Json& impact) {
    materialize(out);
    const auto end=extent(out);
    const double t=time_arg(otherwise(op,"seconds",opt(op,"start")),end);
    const auto& points=out.at("pulse_points");
    const auto i=at(points,"seconds",t);
    if (t>=end.seconds-eps) bad("Free measure must start before the project end");
    const double q=beat(out,t);
    const auto& src=out.at("time_signatures");
    const auto j=at(src,"beat",q);
    const bool smart=j!=none && opt(src[j],"smart_measure").is_object();
    if (i==none && t>eps && !smart) bad("Select an existing pulse for the free measure start");
    Json row=j==none ? effective(src,"beat",q,eps) : src[j];
    row.update(Json{{"beat",q},{"free",true},{"origin","editor_intent"}});
    row.erase("pulse_id");row.erase("smart_measure");
    if (i!=none) row["pulse_id"]=points[i].at("id");
    Json rr=Json::array();
    for (const auto& r : src) if (std::abs(field(r,"beat")-q)>eps) rr.push_back(r);
    rr.push_back(std::move(row)); rr=ordered_meters(std::move(rr));
    double stop=end.seconds;
    for (const auto& r : rr) if (field(r,"beat")>q+eps) {stop=seconds(out,field(r,"beat")); break;}
    out.at("time_signatures")=std::move(rr);
    Json marks=Json::array();
    for (const auto& a : rows_or_empty(out,"short_bar_annotations"))
        if (field(a,"end_seconds")<=t+eps || field(a,"start_seconds")>=stop-eps) marks.push_back(a);
    out["short_bar_annotations"]=std::move(marks);
    impact.update(Json{{"tempo_changed",false},{"start_seconds",t},{"end_seconds",stop}});
}
void short_bar(Json& out, const Json& op, Json& impact) {
    const auto end=extent(out);
    const double t=time_arg(opt(op,"start"),end), u=time_arg(opt(op,"end"),end);
    if (u<=t) bad("Short measure requires two ordered boundaries");
    const bool remove=truth(opt(op,"remove"));
    const auto& before=rows_or_empty(out,"short_bar_annotations");
    if (!remove) {
        const auto q=project(out,{t,u}).q;
        const auto& rows=out.at("time_signatures");
        if (q[1]-q[0]>=meter_of(effective(rows,"beat",q[0],eps)).length()-eps)
            bad("Short measure must be shorter than one local full measure");
        for (const auto& m : rows) if (q[0]+eps<field(m,"beat") && field(m,"beat")<q[1]-eps)
            bad("Short measure cannot cross an internal meter change");
        for (const auto& a : before) if (std::max(t,field(a,"start_seconds"))<std::min(u,field(a,"end_seconds"))-eps && !same_span(a,t,u))
            bad("Short measure annotations cannot overlap");
    }
    Json marks=Json::array();
    for (const auto& a : before) if (!same_span(a,t,u)) marks.push_back(a);
    if (!remove) marks.push_back(Json{{"start_seconds",t},{"end_seconds",u},{"source","user"}});
    impact.update(Json{{"tempo_changed",false},{"short_bar_count",marks.size()}});
    out["short_bar_annotations"]=std::move(marks);
}
void relax_initial_pickup(Json& out, double start, Meter m) {
    if (start==0 && intent_num(otherwise(out,"pickup_beats",Json(0)),"无效弱起")>=m.length()) {
        if (!out.contains("first_full_bar_beat")) { Json v=out.at("pickup_beats"); out["first_full_bar_beat"]=std::move(v); }
        out.at("pickup_beats")=0.;
    }
}
std::string legacy_meter(Json& out, const Json& op, Json& impact) {
    const auto end=extent(out);
    const auto m=meter_op(op,"请输入有效拍号，例如 3/4 或 6/8");
    const double t=time_arg(otherwise(op,"seconds",otherwise(op,"start",Json(0))),end), left=beat(out,t);
    const auto& src=out.at("time_signatures");
    double stop=end.seconds;
    if (!opt(op,"end").is_null()) {
        stop=time_arg(op.at("end"),end);
        if (stop-t<=eps) bad("选区右端必须晚于左端");
    } else for (const auto& r : src) if (field(r,"beat")>left+eps) {stop=seconds(out,field(r,"beat")); break;}
    if (stop-t<=eps) bad("拍号必须位于音频结束之前");
    const double right=beat(out,stop);
    const auto i=at(src,"beat",left);
    Json row=i==none ? Json::object() : src[i];
    row.update(Json{{"beat",left},{"numerator",m.n},{"denominator",m.d},{"origin","editor_intent"},{"free",false}});
    Json rows=Json::array();
    for (const auto& r : src) if (field(r,"beat")<left-eps || field(r,"beat")>=right-eps) rows.push_back(r);
    rows.push_back(std::move(row));
    if (stop<end.seconds-eps) {
        rows.push_back(inherited(effective(src,"beat",right,eps),right));
        const auto starts=reference(out,true).starts;
        const auto recovery=std::upper_bound(starts.begin(),starts.end(),right+eps);
        if (recovery!=starts.end() && std::none_of(starts.begin(),starts.end(),[right](double q){return std::abs(q-right)<eps;}) &&
            std::none_of(rows.begin(),rows.end(),[right,recovery](const Json& r){return right+eps<field(r,"beat") && field(r,"beat")<=*recovery+eps;})) {
            auto saved=inherited(effective(src,"beat",*recovery,eps),*recovery);
            saved["origin"]="editor_intent_restore"; rows.push_back(std::move(saved));
        }
    }
    out.at("time_signatures")=ordered_meters(std::move(rows));
    relax_initial_pickup(out,t,m);
    impact.update(Json{{"start_seconds",t},{"end_seconds",stop},{"tempo_changed",false}});
    return fmt(t)+"–"+fmt(stop)+" 秒设为 "+std::to_string(m.n)+"/"+std::to_string(m.d)+"；只改分组，速度和演奏不变";
}
std::string change_count(Json& out, const Json& op, bool recount, const Json& context, Json& impact) {
    const auto end=extent(out);
    const auto [a,b]=range(op,end);
    const auto lr=project(out,{a,b}).q;
    const double old_span=lr[1]-lr[0];
    if (old_span<=0) bad("范围右端必须晚于左端");
    Meter m{4,4}; int bars=0; double target;
    if (recount) {
        m=meter_op(op,"请输入有效拍号，例如 3/4 或 6/8");
        bars=integer(opt(op,"bar_count"),1,4096,"小节数应为 1–4096 的整数");
        target=bars*m.length();
    } else {
        const double f=intent_num(opt(op,"factor"),"拍值比例必须为有限数值");
        if (f!=.5 && f!=2.) bad("拍值比例应为 0.5 或 2");
        target=old_span*f;
    }
    const std::string mode(text(otherwise(op,"distribution",Json("preserve")),"请选择保留变速或均匀分布"));
    if (mode!="preserve" && mode!="uniform") bad("请选择保留变速或均匀分布");
    const auto& sigs=out.at("time_signatures");
    Json restore;
    if (recount && b<end.seconds-eps) {
        restore=inherited(effective(sigs,"beat",lr[1],eps),lr[1]);
        restore["beat"]=lr[0]+target; restore["origin"]="editor_intent_restore";
    }
    D transport=column(sigs,"beat");
    std::vector<const char*> keys;
    for (const char* k : {"grid_origin","first_full_bar_beat","pickup_beats"})
        if (out.contains(k) && (std::string_view(k)!="pickup_beats" || truth(out.at(k)))) {
            keys.push_back(k); transport.push_back(intent_num(out.at(k),"无效开头位置"));
        }
    auto at_times=project(out,{},transport).t;
    const auto& points=rows_or_empty(out,"pulse_points");
    for (const auto& p : points) at_times.push_back(field(p,"seconds"));
    // This primitive changes just the mathematical curve. Calling user rebar
    // here would incorrectly impose bar IDs, pickup and selection gates.
    auto edited=structural_command(out,Json{{"type","rhythm_span_anchors"},{"start",a},{"end",b},
                                            {"quarters",target},{"distribution",std::string(mode)}},Json::array(),context);
    out.at("anchors")=std::move(edited.at("anchors"));
    const auto q=project(out,at_times).q;
    Json rows=Json::array();
    for (std::size_t i=0;i<sigs.size();++i) if (!recount || q[i]<lr[0]-eps || q[i]>lr[0]+target+eps) {
        auto r=sigs[i]; r["beat"]=q[i]; rows.push_back(std::move(r));
    }
    // The pickup modulus uses the remapped OLD initial meter, before recount
    // installs its replacement signature.
    for (std::size_t i=0;i<keys.size();++i) {
        const double v=q[sigs.size()+i];
        if (std::string_view(keys[i])=="pickup_beats") {
            out.at(keys[i])=std::fmod(v,meter_of(sigs.front()).length());
            if (!out.contains("first_full_bar_beat")) out["first_full_bar_beat"]=v;
        } else out.at(keys[i])=v;
    }
    if (recount) {
        rows.push_back(Json{{"beat",lr[0]},{"numerator",m.n},{"denominator",m.d},{"origin","editor_intent"}});
        if (!restore.is_null()) rows.push_back(std::move(restore));
        rows=ordered_meters(std::move(rows));
    }
    const auto offset=transport.size();
    out.at("time_signatures")=std::move(rows);
    if (recount) relax_initial_pickup(out,a,m);
    if (out.contains("pulse_points")) {
        auto& p=out.at("pulse_points");
        for (std::size_t i=0;i<p.size();++i) {
            p[i]["beat"]=q[offset+i];
            if (i+1<p.size()) p[i]["step_quarters"]=q[offset+i+1]-q[offset+i];
        }
    }
    const double delta=target-old_span;
    impact.update(Json{{"start_seconds",a},{"end_seconds",b},{"tempo_changed",true},{"quarters",target},{"following_quarter_shift",delta}});
    if (recount) return "固定 "+fmt(a)+"–"+fmt(b)+" 秒为 "+std::to_string(bars)+" 小节 "+std::to_string(m.n)+"/"+std::to_string(m.d)+"；原演奏不变";
    return fmt(a)+"–"+fmt(b)+" 秒拍值乘 "+fmt(target/old_span)+"；拍号与演奏耗时不变";
}

// Bounded-window Theil–Sen evidence, not a change to the clock.
double median(D v) {
    const auto i=v.begin()+static_cast<std::ptrdiff_t>(v.size()/2);
    std::nth_element(v.begin(),i,v.end());
    return v.size()%2 ? *i : (*i+*std::max_element(v.begin(),i))/2.;
}
struct Evidence {
    double slope, change, relative, noise, agreement, threshold;
    bool accepted;
};
Evidence evidence(const D& v) {
    D pairs;
    pairs.reserve(v.size()*(v.size()-1)/2);
    for (std::size_t i=0;i<v.size();++i) for (std::size_t j=i+1;j<v.size();++j)
        pairs.push_back((v[j]-v[i])/(j-i));
    const double slope=median(pairs);
    D shifted, residual;
    for (std::size_t i=0;i<v.size();++i) shifted.push_back(v[i]-slope*i);
    const double a=median(shifted);
    for (std::size_t i=0;i<v.size();++i) residual.push_back(v[i]-(a+slope*i));
    const double med=median(residual);
    for (auto& r : residual) r=std::abs(r-med);
    const double noise=1.4826*median(std::move(residual)), base=median(v);
    std::size_t agreeing=0;
    for (std::size_t i=0;i<v.size();++i) for (std::size_t j=i+1;j<v.size();++j) if ((v[j]-v[i])*slope>0) ++agreeing;
    const double ratio=static_cast<double>(agreeing)/pairs.size();
    const double change=slope*(v.size()-1), threshold=std::max(6*noise,.06*base);
    return {slope,change,change/base,noise,ratio,threshold,std::abs(change)>=threshold && ratio>=.8};
}
struct Trends { Json candidates=Json::array(), suspicious=Json::array(), warnings=Json::array(); };
Trends trends(const Json& pts) {
    Trends r;
    if (pts.size()<7) {r.warnings.push_back("Insufficient explicit pulse points; no trend inferred."); return r;}
    D v;
    for (std::size_t i=1;i<pts.size();++i) {
        const double dt=field(pts[i],"seconds")-field(pts[i-1],"seconds");
        const double dq=field(pts[i],"beat")-field(pts[i-1],"beat");
        if (dt<=0 || dq<=0 || !std::isfinite(dt/dq)) bad("Pulse seconds and musical positions must be finite and strictly increasing.");
        v.push_back(dt/dq);
    }
    std::vector<std::size_t> blocked(v.size()+1,0);
    for (std::size_t i=1;i<v.size();++i) {
        const double ratio=v[i]/v[i-1];
        if (ratio>=1.6 || ratio<=1/1.6) {
            blocked[i]=blocked[i+1]=1;
            r.suspicious.push_back(Json{{"start_point_index",i-1},{"end_point_index",i+1},
                {"start_seconds",pts[i-1].at("seconds")},{"end_seconds",pts[i+1].at("seconds")},{"interval_ratio",ratio},
                {"reason","Abrupt tempo ratio: possible missing/extra pulse, half/double interpretation, or intentional jump."}});
        }
    }
    std::partial_sum(blocked.begin(),blocked.end(),blocked.begin());
    struct Hit {std::size_t a,b; bool accel;};
    std::vector<Hit> merged;
    const auto sample=[&v](std::size_t a,std::size_t b,std::size_t stride) {
        D s; for (auto i=a;i<b;i+=stride) s.push_back(v[i]); return s;
    };
    for (std::size_t a=0;a+6<=v.size();++a) {
        const auto max_n=std::min(std::size_t(16),v.size()-a);
        for (std::size_t n : std::array<std::size_t,2>{6,max_n}) {
            const auto b=a+n;
            if (blocked[b]==blocked[a]) {
                const auto fit=evidence(sample(a,b,1));
                if (fit.accepted) {
                    const bool accel=fit.relative<0;
                    bool extended=false;
                    if (!merged.empty() && accel==merged.back().accel && a<merged.back().b) {
                        auto& h=merged.back();
                        const auto stop=std::max(b,h.b), len=stop-h.a;
                        D s;
                        if (len<=64) s=sample(h.a,stop,1);
                        else for (std::size_t k=0;k<64;++k) {
                            const auto i=timing_query("round_even",Json{{"value",static_cast<double>(k)*(len-1)/63.}}).get<std::size_t>();
                            s.push_back(v[h.a+i]);
                        }
                        if (evidence(s).accepted) {h.b=stop; extended=true;}
                    }
                    if (!extended) merged.push_back({a,b,accel});
                }
            }
            if (max_n==6) break;
        }
    }
    for (const auto& h : merged) {
        const auto n=h.b-h.a, stride=(n+63)/64;
        const auto fit=evidence(sample(h.a,h.b,stride));
        const double confidence=std::min(.99,.5*fit.agreement+.5*std::min(1.,std::abs(fit.change)/(2*fit.threshold)));
        Json e{{"slope_seconds_per_quarter_per_interval",fit.slope},{"fitted_change_seconds_per_quarter",fit.change},
               {"relative_change",fit.relative},{"residual_mad_seconds_per_quarter",fit.noise},
               {"pairwise_direction_agreement",fit.agreement},{"required_change_seconds_per_quarter",fit.threshold},
               {"interval_count",n},{"accepted",fit.accepted},{"sample_stride_intervals",stride}};
        r.candidates.push_back(Json{{"start_point_index",h.a},{"end_point_index",h.b},
            {"start_seconds",pts[h.a].at("seconds")},{"end_seconds",pts[h.b].at("seconds")},
            {"start_pulse_id",pts[h.a].at("id")},{"end_pulse_id",pts[h.b].at("id")},
            {"direction",h.accel ? "accelerando" : "ritardando"},{"confidence",confidence},
            {"confidence_kind","heuristic_evidence_score"},{"evidence",std::move(e)}});
    }
    return r;
}

void finish_partials(Json& tempo, Json& warnings) {
    // reference_bars validates the completed musical map. The score end is
    // omitted only in this editable-ruler query, never from the saved tempo.
    const auto bars=reference(tempo,true);
    D q;
    for (std::size_t i=0;i<bars.starts.size();++i) if (std::abs(bars.lengths[i]-bars.meters[i].length())>eps) {
        q.push_back(bars.starts[i]); q.push_back(bars.starts[i]+bars.lengths[i]);
    }
    const auto t=project(tempo,{},q).t;
    Json marks=Json::array();
    for (std::size_t i=0;i<t.size();i+=2) marks.push_back(Json{{"start_seconds",t[i]},{"end_seconds",t[i+1]}});
    const auto& annotations=rows_or_empty(tempo,"short_bar_annotations");
    for (const auto& a : annotations)
        if (std::none_of(marks.begin(),marks.end(),[&a](const Json& m){return same(m,a);})) marks.push_back(a);
    if (!marks.empty()) {
        const auto spans=timing_query("free_ranges",Json{{"tempo",tempo}});
        const double end=extent(tempo).seconds;
        for (const auto& m : marks) {
            const double a=field(m,"start_seconds"), b=field(m,"end_seconds");
            if (b>=end-eps) continue;
            if (std::any_of(spans.begin(),spans.end(),[a,b](const Json& s){return field(s,"start_seconds")-eps<=a && b<=field(s,"end_seconds")+eps;})) continue;
            if (std::any_of(annotations.begin(),annotations.end(),[a,b](const Json& r){return same_span(r,a,b);})) continue;
            warnings.push_back("边界处保留不完整小节，不会拉伸音符来凑拍；如需完整小节，请使用“选区设为 N 小节”。");
            break;
        }
    }
    tempo["explicit_partial_measures"]=std::move(marks);
}
Json output(Json tempo, std::string summary, Json warnings, Json impact) {
    return Json{{"tempo",std::move(tempo)},{"fixed_anchors",Json::array()},{"summary",std::move(summary)},
                {"warnings",std::move(warnings)},{"impact",std::move(impact)}};
}
Json adopt(const Json& tempo, const Json& op, const Json& context) {
    const auto& cap=object(context.at("rhythm_candidate"));
    const auto& row=object(cap.at("row"));
    const auto& binding=object(cap.at("binding"));
    const auto& id=object(row.at("identity"));
    const auto wanted=text(otherwise(op,"candidate_id",opt(object(otherwise(op,"identity",Json::object())),"id")),"请选择有效拍点候选");
    if (wanted.empty() || !is(opt(row,"id"),wanted) || !is(opt(id,"id"),wanted) || !truth(opt(row,"valid")))
        bad("请选择有效拍点候选。");
    const auto revision=text(context.at("revision"),"缺少当前编辑版本");
    if (revision.empty() || !is(opt(binding,"base_revision"),revision) || !is(opt(id,"base_revision"),revision))
        bad("拍点候选版本已过期，请重新分析。");
    for (const char* k : {"source_sha256","input_sha256","normalized_sha256","provider","decoder","original_reverified"})
        if (!same(id.at(k),binding.at(k))) bad("原曲或候选分析身份已变化，请重新分析候选。");
    const auto actual=text(cap.at("observed_input_sha256"),"缺少已复核的输入身份");
    if (actual.empty() || !is(id.at("input_sha256"),actual) || !id.at("original_reverified").is_boolean() ||
        (id.at("original_reverified").get<bool>() && !is(id.at("source_sha256"),actual)))
        bad("原曲或导入缓存内容与工程身份不符；当前图保持。");
    const auto registered=text(cap.at("registered_candidate_sha256"),"缺少候选文件身份");
    if (registered.empty() || !is(cap.at("observed_candidate_sha256"),registered)) bad("候选文件已变化，请重新分析。");
    const auto& candidate=object(row.at("tempo"));
    const auto& evidence_ref=opt(candidate,"pulse_evidence");
    if (!same(evidence_ref,opt(id,"pulse_evidence")) ||
        (!evidence_ref.is_null() && !same(evidence_ref,opt(cap,"resolved_pulse_evidence"))))
        bad("候选拍点证据与已复核证据不符。");
    Json replacement=candidate;
    replacement["candidate_only"]=false;
    replacement["adopted_candidate"]=id;
    if (truth(opt(tempo,"score_end"))) replacement["score_end"]=tempo.at("score_end");
    replacement=validated(replacement);
    if (replacement.contains("pulse_points")) check_points(replacement,false);
    return output(std::move(replacement),"已采用拍点候选；音符和CC64实际时间保持不变，可一次撤销。",
                  Json::array(),Json{{"source_seconds_unchanged",true},{"tempo_changed",true}});
}

Json dispatch(const Json& tempo, const Json& operation, const Json& context) {
    object(tempo); object(operation); object(context);
    if (is(opt(operation,"type"),"adopt_rhythm_candidate")) return adopt(tempo,operation,context);
    if (operation.contains("type") && !is(operation.at("type"),"rhythm_intent")) bad("未知音乐结构操作");
    Json op=operation;
    // These names are reserved; never consume or persist UI-supplied evidence.
    op.erase("_leading_note_count"); op.erase("_note_onsets");
    std::string kind(text(opt(op,"action"),"未知音乐结构操作"));
    if (kind=="fix_position" || kind=="unfix_position") bad("内部固定点功能已移除；拍点可直接移动。");
    Json out=validated(tempo);
    Json impact{{"source_seconds_unchanged",true}}, warnings=Json::array();
    std::string summary;
    if (kind=="detect_trends" || kind=="apply_trend") {
        Trends r;
        if (kind=="detect_trends") {
            materialize(out); r=trends(out.at("pulse_points"));
            impact.update(Json{{"tempo_changed",false},{"candidates",std::move(r.candidates)},{"suspicious_intervals",std::move(r.suspicious)}});
            return output(tempo,"Tempo-expression candidates only; source timing unchanged",std::move(r.warnings),std::move(impact));
        }
        if (out.contains("pulse_points")) r=trends(out.at("pulse_points"));
        else { Json projection=out; materialize(projection); r=trends(projection.at("pulse_points")); }
        if (out.contains("pulse_points")) check_points(out,false);
        const double a=num(otherwise(op,"start",Json(-1))), b=num(otherwise(op,"end",Json(-1)));
        const auto it=std::find_if(r.candidates.begin(),r.candidates.end(),[&](const Json& c){
            return same_span(c,a,b) && c.at("direction")==opt(op,"direction");
        });
        if (it==r.candidates.end()) bad("Trend candidate changed; preview again");
        Json annotations=Json::array();
        for (const auto& c : rows_or_empty(out,"tempo_expression_annotations"))
            if (!same_span(c,field(*it,"start_seconds"),field(*it,"end_seconds"))) annotations.push_back(c);
        Json saved=*it; saved["source"]="user_confirmed_detection"; annotations.push_back(std::move(saved));
        out["tempo_expression_annotations"]=std::move(annotations);
        kind="trend_annotation_saved";
    }
    if (kind=="delete_beat") {
        Json target=opt(op,"seconds"); kind="delete_beats";
        op["action"]=kind; op["seconds"]=Json::array({std::move(target)});
    }
    if (kind=="add_beat" || kind=="delete_beats" || kind=="divide_beats" || kind=="record_beats" || kind=="clear_beats" ||
        kind=="set_meter" || kind=="move_meter" || kind=="set_beat_duration" || kind=="meter_bars") {
        summary=edit_pulses(out,op,kind,context,impact);
    } else if (kind=="trend_annotation_saved") {
        summary="Tempo expression annotation saved; performance and tempo map unchanged";
        impact["tempo_changed"]=false;
    } else if (kind=="set_free_meter") {
        set_free(out,op,impact); summary="Free measure until the next meter event; performance and tempo unchanged";
    } else if (kind=="mark_short_bar") {
        short_bar(out,op,impact); summary="Short measure annotation saved; notes and durations unchanged";
    } else if (kind=="set_opening") {
        summary=opening(out,op,context,warnings); impact["tempo_changed"]=false;
    } else if (kind=="restore_initial") {
        const auto& initial=opt(tempo,"rhythm_initial");
        if (!initial.is_object() || !truth(opt(initial,"available")) || !opt(initial,"tempo").is_object())
            bad("此工程没有可恢复的初稿；当前用户编辑未改变");
        out=initial.at("tempo"); out["rhythm_initial"]=initial;
        summary="已恢复保存的初稿结构；原始音符、CC 和音频秒位置不变"; impact["tempo_changed"]=true;
    } else if (kind=="move_beats") summary=move_group(out,op,impact);
    else if (kind=="move_beat") summary=move_single(out,op,impact);
    else if (kind=="delete_meter") {
        const double t=time_arg(opt(op,"seconds"),extent(out));
        auto& rows=out.at("time_signatures");
        const auto i=at(rows,"beat",beat(out,t));
        if (i==none) bad("此处没有拍号，请重新选择");
        if (i==0) bad("开头拍号不能删除，可双击修改");
        rows.erase(i);
        reconcile_smart_measures(out);
        impact.update(Json{{"tempo_changed",false},{"start_seconds",t}});
        summary="已删除拍号标记，沿用前一个拍号；拍点及演奏时间不变";
    } else if (kind=="meter") summary=legacy_meter(out,op,impact);
    else if (kind=="recount" || kind=="interpret") summary=change_count(out,op,kind=="recount",context,impact);
    else bad("未知音乐结构操作");

    if (kind!="restore_initial" && kind!="trend_annotation_saved" && meter_owned_steps(out)) {
        const auto& pts=out.at("pulse_points");
        rebuild(out,field(pts.back(),"seconds"));
        reconcile_smart_measures(out);
        impact["tempo_changed"]=true;
        impact["meter_owned_steps"]=true;
    }
    finish_partials(out,warnings);
    if (!out.contains("rhythm_initial"))
        out["rhythm_initial"]=Json{{"available",true},{"source","before_first_rhythm_edit"},{"tempo",tempo}};
    out["rhythm_operation_version"]=1;
    out["source"]="manual_editor";
    out["meter_policy"]=Json{{"mode","manual_editor"},{"protected",true}};
    out["last_tempo_operation"]=std::move(op);
    return output(std::move(out),std::move(summary),std::move(warnings),std::move(impact));
}
}

Json pulse_state(const Json& tempo) {
    try { Json result=validated(tempo); materialize(result); return result; }
    catch (const Json::exception& e) { pulse_detail::bad(e.what()); }
}
Json pulse_command(const Json& tempo,const Json& operation,const Json& fixed_anchors,const Json& context) {
    (void)fixed_anchors; // Retired editor pins are not mathematical supports.
    try { return dispatch(tempo,operation,context); }
    catch (const Json::exception& e) { pulse_detail::bad(e.what()); }
}
}
