#include "note_semantic_commands.h"
#include "timing_internal.h"

#include <algorithm>
#include <array>
#include <iterator>
#include <cmath>
#include <cstddef>
#include <limits>
#include <optional>
#include <stdexcept>
#include <string>
#include <string_view>
#include <unordered_map>
#include <unordered_set>
#include <utility>
#include <vector>

namespace astra::editor {
namespace {

constexpr double tail_eps = 1e-6;
constexpr double beat_eps = 1e-9;
constexpr double update_eps = 1e-7;
constexpr double ppq = 9600.0;
constexpr std::size_t copy_limit = 20000;
constexpr std::size_t sweep_limit = 2048;

[[noreturn]] void invalid(const std::string& message) {
    throw std::invalid_argument("note semantic: " + message);
}

const Json& object(const Json& value, const char* label) {
    if (!value.is_object()) invalid(std::string(label) + " must be an object");
    return value;
}

const Json& array(const Json& value, const char* label) {
    if (!value.is_array()) invalid(std::string(label) + " must be an array");
    return value;
}

const Json* find(const Json& row, const char* key) {
    object(row, "record");
    const auto it = row.find(key);
    return it == row.end() ? nullptr : &*it;
}

const Json& field(const Json& row, const char* key) {
    const auto* v = find(row, key);
    if (!v) invalid(std::string("missing ") + key);
    return *v;
}

const Json& array_field(const Json& row, const char* key) {
    return array(field(row, key), key);
}

const Json& samples(const Json& cmd) {
    static const Json empty = Json::array();
    const auto* v = find(cmd, "samples");
    return v ? array(*v, "samples") : empty;
}

std::string_view string(const Json& value, const char* label) {
    if (!value.is_string()) invalid(std::string(label) + " must be a string");
    return value.get_ref<const std::string&>();
}

std::string_view text(const Json& row, const char* key, std::string_view fallback) {
    const auto* v = find(row, key);
    return v ? string(*v, key) : fallback;
}

std::string_view id_of(const Json& value) {
    const auto id = string(value, "event ID");
    if (id.empty()) invalid("empty event ID");
    return id;
}

std::string_view row_id(const Json& row) { return id_of(field(row, "id")); }

double finite(double value, const char* label) {
    if (!std::isfinite(value)) invalid(std::string(label) + " must be finite");
    return value;
}

double number(const Json& value, const char* label) {
    if (!value.is_number()) invalid(std::string(label) + " must be a number");
    return finite(value.get<double>(), label);
}

double num(const Json& row, const char* key) { return number(field(row, key), key); }

double num(const Json& row, const char* key, double fallback) {
    const auto* v = find(row, key);
    return v ? number(*v, key) : fallback;
}

bool flag(const Json& row, const char* key, bool fallback = false) {
    const auto* v = find(row, key);
    if (!v) return fallback;
    if (!v->is_boolean()) invalid(std::string(key) + " must be boolean");
    return v->get<bool>();
}

double positive(double value, const char* label) {
    if (!(value > 0)) invalid(std::string(label) + " must be positive");
    return value;
}

int integer(double value, int lo, int hi, const char* label) {
    if (!std::isfinite(value) || value < lo || value > hi || std::trunc(value) != value)
        invalid(std::string(label) + " must be an integer in range");
    return static_cast<int>(value);
}

// JS Math.max(lo, Math.min(hi, v)). Unlike std::clamp this also has defined
// behavior for the reversed limits possible in a legacy CC drag projection.
double clip(double v, double lo, double hi) { return std::max(lo, std::min(hi, v)); }

// Math.round, not C++ round, Python round, or floor(x + .5). The latter
// changes the answer for a representable double just below some half ties.
double js_round(double x) {
    const double base = std::floor(x);
    return x - base < .5 ? base : base + 1.0;
}

bool span(double a, double b) { return std::isfinite(a) && std::isfinite(b) && b > a; }

bool frames_equal(double a, double b) { return a == b; }

enum class Kind { notes, controls };
const char* key_of(Kind k) { return k == Kind::notes ? "notes" : "controls"; }
Kind kind_of(std::string_view s) {
    if (s == "notes") return Kind::notes;
    if (s == "controls") return Kind::controls;
    invalid("domain must be notes or controls");
}
Kind domain(const Json& cmd) { return kind_of(text(cmd, "domain", "notes")); }
int channel(const Json& row, int fallback = 0) {
    const auto* v = find(row, "channel");
    return v && !v->is_null() ? integer(number(*v, "channel"), 0, 15, "channel") : fallback;
}

using Ids = std::unordered_set<std::string_view>;
Ids id_set(const Json& values) {
    Ids out;
    out.reserve(values.size());
    for (const auto& v : values) out.insert(id_of(v));
    return out;
}

struct Ref {
    const Json* row;
    std::size_t index;
};
using Refs = std::vector<Ref>;

struct Model {
    const Json& doc;
    const Json& rows(Kind k) const { return array_field(doc, key_of(k)); }
    const Json& tempo() const { return object(field(doc, "tempo"), "tempo"); }
    double duration() const {
        const double v = num(doc, "duration");
        if (v < 0) invalid("negative document duration");
        return v == 0 ? 1.0 : v; // active project_editor.duration(); not transaction validation
    }

    // Canonical active publication in. Look up IDs, never accept a caller's
    // selection rows. Ordinary note selections take document order; the lane
    // takes stable seconds order. Only references are sorted or collected.
    Refs select(const Ids& ids, Kind k, int ch) const {
        Refs out;
        if (ids.empty()) return out;
        out.reserve(ids.size());
        std::size_t index = 0;
        for (const auto& r : rows(k)) {
            if (ids.count(row_id(r))) {
                if (k == Kind::controls && (num(r, "number") != 64 || channel(r) != ch))
                    invalid("event is not CC64 in the active channel");
                out.push_back({&r, index});
                if (out.size() == ids.size()) break;
            }
            ++index;
        }
        if (out.size() != ids.size()) invalid("selection event not found in document");
        if (k == Kind::controls) sort_cc(out);
        return out;
    }

    Refs select(const Json& cmd, Kind k) const {
        return select(id_set(array_field(cmd, "ids")), k, k == Kind::controls ? channel(cmd) : 0);
    }

    Refs lane(int ch) const {
        Refs out;
        std::size_t index = 0;
        for (const auto& c : rows(Kind::controls)) {
            if (num(c, "number") == 64 && channel(c) == ch) out.push_back({&c, index});
            ++index;
        }
        sort_cc(out);
        return out;
    }

    static void sort_cc(Refs& out) {
        std::stable_sort(out.begin(), out.end(), [](Ref a, Ref b) {
            return num(*a.row, "seconds") < num(*b.row, "seconds");
        });
    }
};

struct Plan {
    Json op = nullptr;
    Json preview = Json::object();
    Json warnings = Json::array();

    void warn(const char* code, std::size_t count = 0) {
        Json w = {{"code", code}};
        if (count) w["count"] = count;
        warnings.push_back(std::move(w));
    }
    void refuse(const char* code, std::size_t count = 0) {
        preview["blocked"] = true;
        warn(code, count);
    }
    void events(Kind k, const char* name, Json values) {
        if (!values.empty()) op = {{"type", key_of(k)}, {name, std::move(values)}};
    }
    Json result() && {
        return {{"operation", std::move(op)}, {"preview", std::move(preview)},
                {"warnings", std::move(warnings)}};
    }
};

// One lazily constructed query envelope per invocation. No tempo validation,
// anchor interpolation, meter section construction, or viewport clock here.
class Clock {
public:
    Clock(Model model, const Json& context) : model_(model), context_(context) {}

    double beat(double seconds) {
        return cached(beats_, finite(seconds, "seconds"), [&] {
            return scalar(seconds < 0 ? "editor_clock" : "to_beat", "seconds", seconds);
        });
    }
    double seconds(double q) {
        return cached(times_, finite(q, "beat"), [&] {
            return scalar(q < 0 ? "editor_clock" : "to_seconds", "beat", q);
        });
    }
    double grid(double q, double unit) {
        const auto bar = query("editor_bar_at", "beat", q);
        const double a = num(bar, "start"), b = num(bar, "end");
        return std::max(0.0, std::min(b, a + js_round((q - a) / unit) * unit));
    }
    double x(double t) {
        return cached(xs_, finite(t, "projection time"), [&] {
            return scalar("editor_axis", "seconds", t, &axis());
        });
    }
    // Captured pointers can leave either display edge. Match rollAxis.seconds
    // without clamping the raw evidence or changing the owner clock.
    double at(double x) { return scalar("editor_axis", "x", clip(x, 0, num(axis(), "width")), &axis()); }
    Json tick(double t) {
        return timing_query("round_even", Json{{"value", finite(beat(t) * ppq, "PPQ position")}});
    }
    double tick_key(double t) {
        return cached(ticks_, finite(t, "CC64 seconds"), [&] { return number(tick(t), "MIDI tick"); });
    }
    double unit(const Json& settings) const {
        const auto* u = find(settings, "unit");
        if (u && u->is_number()) return positive(number(*u, "unit"), "unit");
        if (u && string(*u, "unit") != "auto") invalid("unit must be positive or auto");
        const auto& a = axis();
        const double pixels = positive(num(a, "width"), "axis width") /
                              positive(num(a, "beat_extent"), "axis beat_extent");
        double step = 1.0 / 32.0;
        while (step < 64 && step * pixels < 12) step *= 2;
        return step;
    }

private:
    const Json& axis() const { return object(field(context_, "axis"), "axis"); }
    using Memo = std::unordered_map<double, double>;
    template<class F> static double cached(Memo& m, double key, F compute) {
        const auto it = m.find(key);
        if (it != m.end()) return it->second;
        const double value = compute();
        m.emplace(key, value);
        return value;
    }
    Json query(std::string_view name, const char* input, double value, const Json* axis_data = nullptr) {
        if (args_.is_null()) args_ = {{"tempo", model_.tempo()}};
        args_[input] = finite(value, input);
        if (axis_data) args_["axis"] = *axis_data;
        auto result = timing_query(name, args_);
        args_.erase(input);
        if (axis_data) args_.erase("axis");
        return result;
    }
    double scalar(std::string_view name, const char* input, double value, const Json* axis_data = nullptr) {
        return number(query(name, input, value, axis_data), "timing result");
    }
    Model model_;
    const Json& context_;
    Json args_ = nullptr;
    Memo beats_, times_, xs_, ticks_;
};

const Json& settings(const Json& cmd) {
    static const Json defaults = Json::object();
    const auto* v = find(cmd, "snap");
    return v ? object(*v, "snap") : defaults;
}

const Json& live_settings(const Json& cmd, const Json& sample) {
    const auto* v = find(sample, "snap");
    return v ? object(*v, "sample snap") : settings(cmd);
}

double preset(const Json& cmd) { return positive(num(cmd, "preset", .25), "preset"); }
double length_unit(const Json& cmd) {
    const auto* len = find(cmd, "length");
    if (!len || (len->is_string() && string(*len, "length") == "link")) return preset(cmd);
    return positive(number(*len, "length"), "length");
}

struct Aim { double seconds; const char* source; };

class Snap {
public:
    Snap(Model model, Clock& clock, const Json& context, Ids exclude = {})
        : model_(model), clock_(clock), context_(context), exclude_(std::move(exclude)) {}

    static bool on(const Json& s, const Json& sample, bool latched_free = false) {
        return flag(s, "enabled", true) && !latched_free && !flag(sample, "bypass");
    }

    Aim get(double t, const Json& sample, const Json& s,
            std::optional<double> ref = {}, bool latched_free = false) {
        const Aim raw{std::max(0.0, t), "raw"};
        if (!on(s, sample, latched_free)) return raw;
        const auto mode = text(s, "mode", "grid");
        if (mode == "detected") {
            const auto p = nearest(detected(), t);
            return p && distance(*p, t) <= 10 ? Aim{*p, "detected"} : raw;
        }
        if (mode == "relative" && ref) {
            const double step = clock_.unit(s), q = clock_.beat(*ref);
            return {clock_.seconds(std::max(0.0, q + js_round((clock_.beat(t) - q) / step) * step)), "relative"};
        }
        const auto grid = [&] { return Aim{clock_.seconds(clock_.grid(clock_.beat(t), clock_.unit(s))), "grid"}; };
        if (mode == "grid" || mode == "relative") return grid();
        if (mode != "events" && mode != "cursor" && mode != "grid-events-cursor") invalid("unknown snap mode");
        auto p = mode == "cursor" ? std::optional<double>{} : nearest(event_edges(), t);
        const char* source = "event";
        if (mode != "events") {
            const auto* c = find(sample, "cursor_seconds");
            if (!c) c = find(context_, "cursor_seconds");
            const double cursor = c && !c->is_null() ? std::max(0.0, number(*c, "cursor_seconds")) : 0;
            if (!p || distance(cursor, t) < distance(*p, t)) { p = cursor; source = "cursor"; }
        }
        if (p && distance(*p, t) <= 10) return {*p, source};
        return mode == "grid-events-cursor" ? grid() : raw;
    }

private:
    double distance(double a, double b) { return std::abs(clock_.x(a) - clock_.x(b)); }
    std::optional<double> nearest(const std::vector<double>& points, double t) {
        const auto hi = std::lower_bound(points.begin(), points.end(), t);
        if (hi == points.end()) return points.empty() ? std::optional<double>{} : std::optional<double>{points.back()};
        if (hi == points.begin()) return *hi;
        return distance(*(hi - 1), t) <= distance(*hi, t) ? *(hi - 1) : *hi;
    }
    static void order(std::vector<double>& a) {
        std::sort(a.begin(), a.end());
        a.erase(std::unique(a.begin(), a.end()), a.end());
    }
    const std::vector<double>& event_edges() {
        if (!events_) {
            events_.emplace();
            for (const auto& n : model_.rows(Kind::notes)) if (!exclude_.count(row_id(n))) {
                events_->push_back(num(n, "start"));
                events_->push_back(num(n, "end"));
            }
            order(*events_);
        }
        return *events_;
    }
    const std::vector<double>& detected() {
        if (!detected_) {
            detected_.emplace();
            const auto* v = find(context_, "detected_seconds");
            if (v && !v->is_null()) for (const auto& p : array(*v, "detected_seconds")) {
                const double t = number(p, "detected time");
                if (t < 0 || t > model_.duration() + .001) invalid("detected time outside evidence range");
                detected_->push_back(t);
            }
            order(*detected_);
        }
        return *detected_;
    }
    Model model_;
    Clock& clock_;
    const Json& context_;
    Ids exclude_;
    std::optional<std::vector<double>> events_, detected_;
};

Ids magnet_exclusions(const Json& cmd, Ids fallback = {}) {
    const auto* p = find(cmd, "exclude_note_ids");
    return p ? id_set(array(*p, "exclude_note_ids")) : std::move(fallback);
}

const Json& grabbed(const Refs& rows, const Json& id) {
    const auto wanted = id_of(id);
    for (const auto& r : rows) if (row_id(*r.row) == wanted) return *r.row;
    invalid("grabbed event must be in selection");
}

Json patch(const Json& r) { return {{"id", field(r, "id")}}; }

void quantize(Plan& p, Model m, Clock& clock, const Json& cmd) {
    const auto target = text(cmd, "target", "start");
    if (target != "start" && target != "end" && target != "length" && target != "both")
        invalid("unknown quantize target");
    const auto length_mode = text(cmd, "length_mode", "multiple");
    if (length_mode != "multiple" && (length_mode != "fixed" || target != "length"))
        invalid("unknown quantize length mode");
    const double step = target == "length" ? length_unit(cmd) : preset(cmd);
    const double strength = clip(num(cmd, "strength", 100), 0, 100);
    const double s = strength / 100, limit = m.duration();
    const auto* opening = find(m.tempo(), "score_start_seconds");
    const auto* policy = find(m.tempo(), "opening_policy");
    const auto* score_only = policy && policy->is_object() ? find(*policy, "score_only") : nullptr;
    const double floor = opening && !opening->is_null() && !(score_only && *score_only==true) ? std::max(0.0, number(*opening, "score_start_seconds")) : 0;
    const auto toward = [s](double a, double b) { return a + (b - a) * s; };
    const auto rows = m.select(cmd, Kind::notes);
    Json up = Json::array();
    std::size_t short_count = 0, edge = 0;
    for (const auto& ref : rows) {
        const auto& n = *ref.row;
        const double a = num(n, "start"), b = num(n, "end"), len0 = b - a, sb = clock.beat(a);
        double start = a, end = b;
        const double first = target == "start" || target == "both" ? toward(sb, clock.grid(sb, step)) : sb;
        if (first != sb) start = clock.seconds(first);
        if (target == "start") {
            // A note's duration is musical time. Reproject both edges when
            // the moved span lies at a different tempo rather than carrying seconds.
            if (first != sb) end = clock.seconds(first + (clock.beat(b) - sb));
        }
        else {
            const double eb = clock.beat(b);
            if (target == "length") {
                const double len = eb - sb, goal = length_mode == "fixed" ? step : js_round(len / step) * step;
                if (goal < beat_eps) ++short_count;
                else {
                    const double next = clock.seconds(sb + toward(len, goal));
                    if (span(start, next)) end = next; else ++short_count;
                }
            } else {
                const double from = target == "both" ? clock.beat(start) : sb;
                const double goal = clock.grid(eb, step);
                const std::optional<double> next = goal <= from + beat_eps ? std::nullopt :
                    std::optional<double>{clock.seconds(toward(eb, goal))};
                if (!next || !span(start, *next)) {
                    ++short_count;
                    end = target == "both" ? start + len0 : b;
                } else end = *next;
            }
        }
        if (start < floor - beat_eps || end > limit + tail_eps || !span(start, end)) { ++edge; continue; }
        const bool move_start = std::abs(start - a) > update_eps;
        const bool move_end = std::abs(end - b) > update_eps;
        if (!move_start && !move_end) continue;
        // Onset-only quantize moves a musical span as one unit. Also keep both
        // edges of any other landing if per-edge pruning would collapse it;
        // the reducer must see the same positive span as the planner.
        const bool pair = target == "start" || !span(move_start ? start : a, move_end ? end : b);
        Json u = patch(n);
        if (move_start || (pair && start != a)) u["start"] = start;
        if (move_end || (pair && end != b)) u["end"] = end;
        up.push_back(std::move(u));
    }
    p.preview = {{"total", rows.size()}, {"short", short_count}, {"edge", edge}, {"changed", up.size()},
                 {"target", target}, {"unit", step}, {"strength", strength}};
    if (target == "length" && length_mode == "fixed") p.preview["length_mode"] = length_mode;
    if (short_count) p.warn("quantize_short", short_count);
    if (edge) p.warn("quantize_edge", edge);
    p.events(Kind::notes, "updates", std::move(up));
}

// The inspector's overlap is musical (480 ticks / quarter). It is intentionally
// separate from the legacy seconds-based legato API and SMF export PPQ.
constexpr int length_ticks_per_quarter = 480;
constexpr int legato_tick_limit = 10 * length_ticks_per_quarter;

struct LengthClock {
    timing_detail::Curve curve;
    explicit LengthClock(const Model& m) : curve(timing_detail::checked_curve(m.tempo())) {}
    double beat(double s) const {
        return timing_detail::timing_query(curve, timing_detail::InputAxis::seconds, s, s < 0);
    }
    double seconds(double q) const {
        return timing_detail::timing_query(curve, timing_detail::InputAxis::beat, q, q < 0);
    }
};

void length_updates(Plan& p, Json updates) {
    p.preview["changed"] = updates.size();
    // Preview and commit use this one sparse, immutable recipe. Only the owner
    // reducer can apply it; slider sampling never publishes a revision.
    p.preview["updates"] = updates;
    p.events(Kind::notes, "updates", std::move(updates));
}

void fixed_lengths(Plan& p, Model m, const Json& cmd) {
    if (domain(cmd) != Kind::notes || flag(cmd, "all_notes"))
        invalid("fixed lengths requires explicit note targets");
    const auto rows = m.select(cmd, Kind::notes);
    const double unit = length_unit(cmd);
    p.preview = {{"total", rows.size()}, {"unit", unit}, {"edge", 0}};
    Json updates = Json::array();
    if (rows.empty()) { length_updates(p, std::move(updates)); return; }
    const LengthClock clock(m);
    const double limit = m.duration(), max_beat = clock.beat(limit);
    std::size_t edge = 0;
    for (const auto& ref : rows) {
        const auto& n = *ref.row;
        const double start = num(n, "start"), old = num(n, "end");
        const double target = clock.beat(start) + unit;
        // Fixed means exact: skip a note whose requested duration does not fit,
        // rather than truncate it to an unrelated length at the project tail.
        if (target > max_beat + beat_eps) { ++edge; continue; }
        const double end = target >= max_beat ? limit : std::min(limit, clock.seconds(target));
        if (!span(start, end)) { ++edge; continue; }
        if (end != old) { auto u = patch(n); u["end"] = end; updates.push_back(std::move(u)); }
    }
    p.preview["edge"] = edge;
    if (edge) p.warn("fixed_length_boundary", edge);
    length_updates(p, std::move(updates));
}

// Pedal conversion is deliberately stricter than a playback threshold: leave
// half-pedal and repeated/uncertain state alone. Use the actual 9600-PPQ event
// clock to disambiguate ordering, but keep the release's exact seconds.
struct PedalRow {
    const Json* row;
    double time, tick;
    int value;
};
struct PedalNote {
    const Json* row;
    double start, end;
    int pitch;
    bool selected;
};
struct PedalPitchLane {
    std::vector<const PedalNote*> ordered;
    std::vector<double> prior_max_tail;

    void prepare() {
        std::stable_sort(ordered.begin(), ordered.end(), [](auto* a, auto* b){return a->start < b->start;});
        double prior = -1;
        prior_max_tail.reserve(ordered.size());
        for (const auto* n : ordered) {
            prior_max_tail.push_back(prior); prior = std::max(prior, n->end);
        }
    }

    bool ambiguous(const PedalNote& n, double release) const {
        const auto i = std::lower_bound(ordered.begin(), ordered.end(), n.start,
            [](const PedalNote* other, double start){return other->start < start;});
        const auto position = static_cast<std::size_t>(i - ordered.begin());
        if (i == ordered.end() || *i != &n || prior_max_tail[position] >= n.start) return true;
        return std::next(i) != ordered.end() && (*std::next(i))->start <= release;
    }
};

void pedals_to_note_length(Plan& p, Model m, const Json& cmd) {
    if (domain(cmd) != Kind::notes || find(cmd, "all_notes"))
        invalid("pedal conversion requires explicit note IDs");
    const auto selected = m.select(cmd, Kind::notes);
    p.preview = {{"total", selected.size()}, {"changed", 0}, {"interval_count", 0}, {"blocked", false},
                 {"updates", Json::array()}, {"deleted_control_ids", Json::array()}};
    if (selected.empty()) return;

    std::array<bool, 16> active{};
    const auto ids = id_set(array_field(cmd, "ids"));
    for (const auto& n : selected) active[channel(*n.row)] = true;
    const LengthClock clock(m);
    const auto tick = [&clock](double t) { return timing_detail::round_even(clock.beat(t) * ppq); };
    std::array<std::vector<PedalRow>, 16> pedals;
    std::array<std::vector<double>, 16> mode_resets;
    for (const auto& c : m.rows(Kind::controls)) {
        const int ch = channel(c);
        if (!active[ch]) continue;
        const int type = integer(num(c, "number"), 0, 127, "controller");
        const double at = num(c, "seconds");
        if (type == 64) pedals[ch].push_back({&c, at, tick(at), integer(num(c, "value"), 0, 127, "pedal value")});
        else if (type == 120 || type == 121 || type >= 123) mode_resets[ch].push_back(tick(at));
    }
    std::array<std::vector<PedalNote>, 16> notes;
    for (const auto& n : m.rows(Kind::notes)) {
        const int ch = channel(n);
        if (!pedals[ch].empty()) notes[ch].push_back({&n, tick(num(n, "start")), tick(num(n, "end")),
            integer(num(n, "pitch"), 0, 127, "pitch"), ids.count(row_id(n)) != 0});
    }

    std::size_t count = 0, shared = 0, edge = 0, retrigger = 0, nonbinary = 0, incomplete = 0, reset = 0;
    Json updates = Json::array(), deletes = Json::array();
    for (std::size_t ch = 0; ch < pedals.size(); ++ch) {
        auto& cc = pedals[ch];
        auto& channel_notes = notes[ch];
        if (cc.empty() || channel_notes.empty()) continue;
        std::stable_sort(cc.begin(), cc.end(), [](const auto& a, const auto& b){return a.tick < b.tick;});
        std::stable_sort(channel_notes.begin(), channel_notes.end(), [](const auto& a, const auto& b){return a.end < b.end;});
        auto& resets = mode_resets[ch];
        std::sort(resets.begin(), resets.end());
        std::array<PedalPitchLane, 128> pitches;
        for (const auto& n : channel_notes) pitches[n.pitch].ordered.push_back(&n);
        for (auto& pitch : pitches) pitch.prepare();
        const PedalRow* down = nullptr;
        bool dirty_level = false, duplicate_on = false, collision = false;
        const auto proposed = [&](const PedalRow* release) {
            if (!down) return;
            const double finish = release ? release->tick : std::numeric_limits<double>::infinity();
            const auto first = std::lower_bound(channel_notes.begin(), channel_notes.end(), down->tick,
                [](const auto& n, double at){return n.end < at;});
            const auto last = std::upper_bound(first, channel_notes.end(), finish,
                [](double at, const auto& n){return at < n.end;});
            if (std::none_of(first, last, [](const auto& n){return n.selected;})) return;
            if (!release || !(release->time <= m.duration())) { ++incomplete; return; }
            bool bad = false;
            if (dirty_level) { ++nonbinary; bad = true; }
            if (duplicate_on) { ++retrigger; bad = true; }
            const auto mode = std::lower_bound(resets.begin(), resets.end(), down->tick);
            if (mode != resets.end() && *mode <= finish) { ++reset; bad = true; }
            if (collision || std::any_of(first, last, [&](const auto& n){return n.end == down->tick || n.end == finish;})) {
                ++edge; bad = true;
            }
            if (std::any_of(first, last, [](const auto& n){return !n.selected;})) { ++shared; bad = true; }
            if (std::any_of(first, last, [&](const auto& n){return pitches[n.pitch].ambiguous(n, finish);})){
                ++retrigger; bad = true;
            }
            if (bad) return;
            for (auto i = first; i != last; ++i) {
                auto u = patch(*i->row);
                u["end"] = release->time;
                updates.push_back(std::move(u));
            }
            deletes.push_back(field(*down->row, "id")); deletes.push_back(field(*release->row, "id"));
            ++count;
        };
        for (auto group = cc.begin(); group != cc.end();) {
            const auto next = std::find_if(std::next(group), cc.end(), [&](const auto& c){return c.tick != group->tick;});
            const auto on = std::find_if(group, next, [](const auto& c){return c.value > 0;});
            const auto off = std::find_if(group, next, [](const auto& c){return c.value == 0;});
            const bool tied = std::next(group) != next, has_on = on != next, has_off = off != next;
            if (!down && has_on) {
                down = &*on;
                dirty_level = false; duplicate_on = false; collision = false;
            } else if (down && has_on) duplicate_on = true;
            if (down) {
                collision = collision || tied;
                dirty_level = dirty_level || std::any_of(group, next, [](const auto& c){return c.value != 0 && c.value != 127;});
                // Opposite edges on the same exported tick are unordered.
                // Keep the entire uncertain segment through the next definitive zero.
                if (has_off && !has_on) { proposed(&*off); down = nullptr; }
            }
            group = next;
        }
        if (down) proposed(nullptr);
    }
    if (shared) p.warn("pedal_length_shared", shared);
    if (edge) p.warn("pedal_length_edge_tie", edge);
    if (retrigger) p.warn("pedal_length_retrigger", retrigger);
    if (nonbinary) p.warn("pedal_length_nonbinary", nonbinary);
    if (incomplete) p.warn("pedal_length_incomplete", incomplete);
    if (reset) p.warn("pedal_length_channel_mode", reset);
    if (shared || edge || retrigger || nonbinary || incomplete || reset) {
        p.preview["blocked"] = true;
        return; // no partial conversion even when other intervals were safe
    }
    if (updates.empty()) { p.warn("pedal_length_no_pairs"); return; }
    p.preview["changed"] = updates.size();
    p.preview["interval_count"] = count;
    p.preview["updates"] = updates;
    p.preview["deleted_control_ids"] = deletes;
    // Internal planner output only. The public edit dispatcher cannot submit
    // a raw two-domain patch as an operation.
    p.op = {{"type", "semantic_pedal_length"}, {"notes", {{"type", "notes"}, {"updates", std::move(updates)}}},
            {"controls", {{"type", "controls"}, {"delete", std::move(deletes)}}}};
}

void adjusted_legato(Plan& p, Model m, const Json& cmd) {
    if (domain(cmd) != Kind::notes || find(cmd, "overlap_ms"))
        invalid("adjusted legato uses notes and musical overlap_ticks only");
    const auto mode = text(cmd, "mode", "selected");
    if (mode != "any" && mode != "selected") invalid("invalid legato mode");
    const double percent = num(cmd, "percent", 100);
    if (percent < -100 || percent > 100) invalid("invalid legato percent");
    const int ticks = integer(num(cmd, "overlap_ticks", 0), -legato_tick_limit, legato_tick_limit,
                              "legato overlap ticks");
    const bool whole = flag(cmd, "all_notes");
    if (whole && find(cmd, "ids")) invalid("all_notes and note IDs cannot be combined");
    Refs rows;
    const auto& notes = m.rows(Kind::notes);
    if (whole) {
        rows.reserve(notes.size());
        for (std::size_t i = 0; i < notes.size(); ++i) rows.push_back({&notes[i], i});
    } else {
        rows = m.select(cmd, Kind::notes);
    }
    p.preview = {{"total", rows.size()}, {"mode", mode}, {"percent", percent},
                 {"overlap_ticks", ticks}, {"ticks_per_quarter", length_ticks_per_quarter},
                 {"last", 0}, {"too_short", 0}, {"edge", 0}};
    Json updates = Json::array();
    if (rows.empty() || percent == 0) { length_updates(p, std::move(updates)); return; }
    if (percent > 0 && mode == "selected" && rows.size() < 2) {
        p.warn("legato_need_two");
        length_updates(p, std::move(updates)); return;
    }

    std::vector<double> onsets;
    if (percent > 0) {
        if (mode == "any") {
            onsets.reserve(notes.size());
            for (const auto& n : notes) onsets.push_back(num(n, "start"));
        } else {
            onsets.reserve(rows.size());
            for (const auto& n : rows) onsets.push_back(num(*n.row, "start"));
        }
        std::sort(onsets.begin(), onsets.end());
    }
    const LengthClock clock(m);
    const double limit = m.duration(), overlap = double(ticks) / length_ticks_per_quarter;
    std::size_t last = 0, short_count = 0, edge = 0;
    for (const auto& ref : rows) {
        const auto& n = *ref.row;
        const double start = num(n, "start"), old = num(n, "end");
        double end;
        if (percent < 0) {
            // Astra's negative slider scales down the current musical
            // duration. Floor at one UI tick or the existing shorter length.
            const double first = clock.beat(start), old_beat = clock.beat(old);
            const double len = old_beat - first;
            const double next_len = std::max(std::min(len, 1.0 / length_ticks_per_quarter),
                                             len * (1 + percent / 100));
            if (!(next_len < len)) continue;
            end = std::min(old, clock.seconds(first + next_len));
        } else {
            // Preserve the existing near-simultaneous onset/chord grouping.
            const auto next = std::lower_bound(onsets.begin(), onsets.end(), start + 1e-6);
            if (next == onsets.end()) { ++last; continue; }
            const double target = ticks == 0 ? *next : clock.seconds(clock.beat(*next) + overlap);
            if (target - start <= beat_eps) { ++short_count; continue; }
            const double goal = std::min(limit, target);
            if (target > limit) ++edge;
            if (percent == 100) end = goal; // zero ticks meets next onset exactly, without a clock round-trip
            else {
                const double old_beat = clock.beat(old);
                end = clock.seconds(old_beat + (clock.beat(goal) - old_beat) * percent / 100);
            }
        }
        if (!span(start, end)) { ++short_count; continue; }
        if (end > limit) { ++edge; continue; }
        if (end != old) { auto u = patch(n); u["end"] = end; updates.push_back(std::move(u)); }
    }
    p.preview["last"] = last;
    p.preview["too_short"] = short_count;
    p.preview["edge"] = edge;
    if (last) p.warn("legato_no_next", last);
    if (short_count) p.warn("legato_too_short", short_count);
    if (edge) p.warn("legato_boundary", edge);
    length_updates(p, std::move(updates));
}

void legato(Plan& p, Model m, const Json& cmd) {
    const auto mode = text(cmd, "mode", "selected");
    if (mode != "any" && mode != "selected") invalid("invalid legato mode");
    auto selected = m.select(cmd, Kind::notes);
    const double ms = clip(num(cmd, "overlap_ms", 0), -200, 50);
    p.preview = {{"total", selected.size()}, {"last", 0}, {"too_short", 0}, {"changed", 0}, {"overlap_ms", ms}};
    if (selected.empty()) return;
    if (mode == "selected" && selected.size() < 2) { p.warn("legato_need_two"); return; }
    std::stable_sort(selected.begin(), selected.end(), [](Ref a, Ref b) {
        const double sa = num(*a.row, "start"), sb = num(*b.row, "start");
        return sa != sb ? sa < sb : num(*a.row, "pitch") < num(*b.row, "pitch");
    });
    std::vector<double> pool;
    if (mode == "any") {
        const auto& notes = m.rows(Kind::notes);
        pool.reserve(notes.size());
        for (const auto& n : notes) pool.push_back(num(n, "start"));
        std::sort(pool.begin(), pool.end());
    } else {
        pool.reserve(selected.size());
        for (const auto& n : selected) pool.push_back(num(*n.row, "start"));
    }
    const double limit = m.duration(), overlap = ms / 1000;
    std::size_t last = 0, short_count = 0;
    Json up = Json::array();
    for (const auto& ref : selected) {
        const auto& n = *ref.row;
        const double start = num(n, "start");
        const auto next = std::lower_bound(pool.begin(), pool.end(), start + 1e-6);
        if (next == pool.end()) { ++last; continue; }
        const double end = std::min(limit, *next + overlap);
        if (end - start <= beat_eps) { ++short_count; continue; }
        if (std::abs(end - num(n, "end")) > 1e-6) {
            auto u = patch(n); u["end"] = end; up.push_back(std::move(u));
        }
    }
    p.preview["last"] = last; p.preview["too_short"] = short_count; p.preview["changed"] = up.size();
    if (last) p.warn("legato_no_next", last);
    if (short_count) p.warn("legato_too_short", short_count);
    p.events(Kind::notes, "updates", std::move(up));
}

void length_scale(Plan& p, Model m, const Json& cmd) {
    const auto rows = m.select(cmd, Kind::notes);
    const double pct = clip(num(cmd, "percent", 100), 25, 200), k = pct / 100, limit = m.duration();
    Json up = Json::array();
    std::size_t outside = 0;
    for (const auto& r : rows) {
        const auto& n = *r.row;
        const double a = num(n, "start"), b = num(n, "end"), end = a + (b - a) * k;
        if (end > limit + tail_eps || !span(a, end)) { ++outside; continue; }
        if (end != b) { auto u = patch(n); u["end"] = end; up.push_back(std::move(u)); }
    }
    p.preview = {{"total", rows.size()}, {"percent", pct}, {"outside", outside}, {"changed", outside ? 0 : up.size()}};
    if (outside) p.refuse("length_scale_boundary", outside);
    else p.events(Kind::notes, "updates", std::move(up));
}

// Steinberg Cubase's historically named mono command operates per pitch;
// poly also resolves different-pitch overlaps. In Astra MIDI channels are
// independent and equal-onset chords are not shortened to zero/deleted.
void delete_overlaps(Plan& p, Model m, const Json& cmd) {
    if (domain(cmd) != Kind::notes) invalid("overlap cleanup is a note command");
    const auto mode = text(cmd, "mode", "");
    if (mode != "mono" && mode != "poly") invalid("unknown overlap cleanup mode");
    const bool same_pitch = mode == "mono";
    auto rows = m.select(cmd, Kind::notes);
    const auto group = [&](const Ref& r) {
        const auto& n = *r.row;
        return channel(n) * 128 + (same_pitch ? static_cast<int>(num(n, "pitch")) : 0);
    };
    std::stable_sort(rows.begin(), rows.end(), [&](const Ref& a, const Ref& b) {
        const int ga = group(a), gb = group(b);
        return ga != gb ? ga < gb : num(*a.row, "start") < num(*b.row, "start");
    });
    std::size_t ties = 0;
    Json updates = Json::array();
    for (std::size_t i = 0; i < rows.size();) {
        const int lane = group(rows[i]);
        const double at = num(*rows[i].row, "start");
        std::size_t j = i + 1;
        while (j < rows.size() && group(rows[j]) == lane && num(*rows[j].row, "start") == at) ++j;
        if (same_pitch && j > i + 1) ties += j-i;
        if (j < rows.size() && group(rows[j]) == lane) {
            const double next = num(*rows[j].row, "start");
            for (std::size_t k = i; k < j; ++k) {
                const auto& n = *rows[k].row;
                if (next < num(n, "end")) {
                    auto u = patch(n);
                    u["end"] = field(*rows[j].row, "start");
                    updates.push_back(std::move(u));
                }
            }
        }
        i = j;
    }
    p.preview = {{"total", rows.size()}, {"mode", mode}, {"changed", updates.size()},
                 {"equal_onset_preserved", ties}};
    p.events(Kind::notes, "updates", std::move(updates));
}

void transpose(Plan& p, Model m, const Json& cmd) {
    const auto policy = text(cmd, "policy", "reject");
    if (policy != "reject" && policy != "clamp") invalid("unknown transpose policy");
    const double amount = num(cmd, "semitones");
    if (amount != std::trunc(amount)) invalid("transpose requires integer semitones");
    const auto rows = m.select(cmd, Kind::notes);
    p.preview = {{"total", rows.size()}, {"requested", amount}, {"changed", 0}};
    if (rows.empty()) return;
    double lo = 127, hi = 0;
    for (auto r : rows) { const double v = num(*r.row, "pitch"); lo = std::min(lo, v); hi = std::max(hi, v); }
    p.preview["min"] = lo; p.preview["max"] = hi;
    const double delta = clip(amount, -lo, 127 - hi);
    if (policy == "reject" && amount != delta) { p.refuse("transpose_pitch_range"); return; }
    p.preview["delta"] = delta;
    p.preview["first_pitch"] = num(*rows.front().row, "pitch");
    p.preview["first_target_pitch"] = num(*rows.front().row, "pitch") + delta;
    if (delta == 0) return;
    Json up = Json::array();
    for (auto r : rows) {
        auto u = patch(*r.row); u["pitch"] = integer(num(*r.row, "pitch") + delta, 0, 127, "pitch");
        up.push_back(std::move(u));
    }
    p.preview["changed"] = up.size();
    p.events(Kind::notes, "updates", std::move(up));
}

int lane_value(double y, double bottom, double size, int low) {
    return static_cast<int>(clip(js_round((bottom - y) / size * 127), low, 127));
}

void velocity(Plan& p, Model m, Clock& clock, const Json& cmd) {
    const auto mode = text(cmd, "mode", "drag");
    const auto ids = id_set(array_field(cmd, "ids"));
    auto rows = m.select(ids, Kind::notes, 0);
    Json up = Json::array();
    if (mode == "paint") {
        const auto& segments = array_field(cmd, "segments");
        if (ids.empty() && !segments.empty()) {
            const auto& notes = m.rows(Kind::notes);
            rows.reserve(notes.size());
            std::size_t i = 0;
            for (const auto& n : notes) rows.push_back({&n, i++});
        }
        std::stable_sort(rows.begin(), rows.end(), [](Ref a, Ref b) { return num(*a.row, "start") < num(*b.row, "start"); });
        std::unordered_map<const Json*, int> values;
        std::vector<const Json*> paint_order;
        Refs touched;
        for (const auto& segment : segments) {
            const double x0 = num(segment, "x0"), y0 = num(segment, "y0"), x1 = num(segment, "x1"), y1 = num(segment, "y1");
            const double offset = num(segment, "offset"), bottom = num(segment, "bottom");
            const double height = positive(num(segment, "span"), "velocity span");
            const double lo = std::min(x0, x1) - 3, hi = std::max(x0, x1) + 3;
            const auto x = [&](Ref r) { return clock.x(num(*r.row, "start")) - offset; };
            const auto first = std::lower_bound(rows.begin(), rows.end(), lo, [&](Ref r, double at) { return x(r) < at; });
            touched.clear();
            for (auto it = first; it != rows.end() && x(*it) <= hi; ++it) touched.push_back(*it);
            // Map iteration on release is first-touch order, with document
            // order within each individual stroke segment.
            std::sort(touched.begin(), touched.end(), [](Ref a, Ref b) { return a.index < b.index; });
            for (Ref r : touched) {
                const double t = x1 == x0 ? 1 : clip((x(r) - x0) / (x1 - x0), 0, 1);
                const int v = lane_value(y0 + (y1 - y0) * t, bottom, height, 1);
                auto slot = values.emplace(r.row, v);
                if (slot.second) paint_order.push_back(r.row); else slot.first->second = v;
            }
            p.preview["pointer_velocity"] = lane_value(y1, bottom, height, 1);
        }
        for (const auto* n : paint_order) {
            const auto v = values.at(n);
            if (v != num(*n, "velocity")) { auto u = patch(*n); u["velocity"] = v; up.push_back(std::move(u)); }
        }
        p.preview["touched"] = paint_order.size();
        p.preview["only_selected"] = !ids.empty();
    } else {
        if (mode != "drag" && mode != "dynamics") invalid("unknown velocity mode");
        double mean = 0, k = 1, delta = 0;
        if (mode == "drag") {
            delta = js_round(num(cmd, "up_pixels")); p.preview["delta"] = delta;
        } else {
            const double pct = clip(num(cmd, "percent", 100), 25, 200);
            k = pct / 100;
            double lo = 127, hi = 1;
            for (auto r : rows) {
                const double v = num(*r.row, "velocity"); mean += v;
                lo = std::min(lo, v); hi = std::max(hi, v);
            }
            if (!rows.empty()) {
                mean /= static_cast<double>(rows.size());
                p.preview["min"] = lo; p.preview["max"] = hi;
            }
            p.preview["mean"] = mean; p.preview["display_mean"] = js_round(mean); p.preview["percent"] = pct;
        }
        std::size_t clipped = 0;
        for (auto r : rows) {
            const double old = num(*r.row, "velocity");
            const double raw = mode == "drag" ? old + delta : js_round(mean + (old - mean) * k);
            const int v = static_cast<int>(clip(raw, 1, 127));
            if (raw != v) ++clipped;
            if (v != old) { auto u = patch(*r.row); u["velocity"] = v; up.push_back(std::move(u)); }
        }
        p.preview["clipped"] = clipped;
        if (clipped && mode == "dynamics") p.warn("velocity_clipped", clipped);
    }
    p.preview["total"] = rows.size(); p.preview["changed"] = up.size();
    p.events(Kind::notes, "updates", std::move(up));
}

std::string_view part(const Json& cmd) {
    const auto s = text(cmd, "part", "move");
    if (s != "move" && s != "start" && s != "end") invalid("unknown move part");
    return s;
}

void nudge(Plan& p, Model m, Clock& clock, const Json& cmd) {
    const auto k = domain(cmd);
    const auto which = part(cmd);
    const auto rows = m.select(cmd, k);
    const auto dir = integer(num(cmd, "direction"), -1, 1, "direction");
    if (dir == 0) invalid("direction must be -1 or 1");
    p.preview["total"] = rows.size();
    if (rows.empty() || (k == Kind::controls && which != "move")) return;
    const double step = dir * clock.unit(settings(cmd)); // independent of Snap enabled/bypass/mode
    const auto move = [&](double t) { return clock.seconds(clock.beat(t) + step); };
    double limit = m.duration();
    if (k == Kind::notes) for (auto r : rows) limit = std::max(limit, num(*r.row, "end"));
    Json up = Json::array();
    std::size_t outside = 0;
    for (const auto& r : rows) {
        const auto& row = *r.row;
        auto u = patch(row);
        if (k == Kind::notes) {
            const double a = num(row, "start"), b = num(row, "end");
            const double start = which == "end" ? a : move(a), end = which == "start" ? b : move(b);
            if (start < -beat_eps || end > limit + tail_eps || !span(start, end)) { ++outside; continue; }
            if (start == a && end == b) continue;
            u["start"] = start == a ? field(row, "start") : Json(start);
            u["end"] = end == b ? field(row, "end") : Json(end);
        } else {
            const double old = num(row, "seconds"), t = move(old);
            if (t < -beat_eps || t > limit + tail_eps) { ++outside; continue; }
            if (t == old) continue;
            u["seconds"] = t;
        }
        up.push_back(std::move(u));
    }
    p.preview["step_quarters"] = step; p.preview["changed"] = outside ? 0 : up.size();
    if (outside) p.refuse("nudge_boundary", outside); else p.events(k, "updates", std::move(up));
}

double read_info(const Json& row, std::string_view f) {
    if (f == "length") return num(row, "end") - num(row, "start");
    const std::string key(f);
    return num(row, key.c_str());
}

void info(Plan& p, Model m, const Json& cmd, bool state_only) {
    const auto k = state_only ? Kind::controls : domain(cmd);
    const auto f = state_only ? std::string_view("value") : string(field(cmd, "field"), "field");
    const bool notes = k == Kind::notes;
    if (notes ? f != "start" && f != "end" && f != "length" && f != "pitch" && f != "velocity"
              : f != "seconds" && f != "value") invalid("unsupported info field");
    const auto rows = m.select(cmd, k);
    const double v = num(cmd, "value");
    if (!notes && f == "value" && v != 0 && v != 127) invalid("CC64 state must be 0 or 127");
    p.preview["total"] = rows.size(); p.preview["changed"] = 0;
    if (rows.empty()) return;
    const bool absolute = (!notes && f == "value") || rows.size() == 1 || flag(cmd, "absolute");
    const auto* shown = find(cmd, "shown");
    const double delta = v - (shown && !shown->is_null() ? number(*shown, "shown") : read_info(*rows.front().row, f));
    p.preview["absolute"] = absolute;
    if (!absolute) p.preview["delta"] = finite(delta, "relative difference");
    if (!absolute && std::abs(delta) < beat_eps) return;
    const double limit = state_only ? 0 : m.duration() + tail_eps;
    std::size_t bad = 0;
    Json up = Json::array();
    for (auto r : rows) {
        const auto& row = *r.row;
        auto u = patch(row);
        if (notes) {
            const double a = num(row, "start"), b = num(row, "end");
            const double pitch0 = num(row, "pitch"), vel0 = num(row, "velocity");
            double start = a, end = b, pitch = pitch0, vel = vel0;
            if (f == "start") { start = absolute ? v : a + delta; end = start + (b - a); }
            else if (f == "end") end = absolute ? v : b + delta;
            else if (f == "length") end = a + (absolute ? v : b - a + delta);
            else if (f == "pitch") pitch = js_round(absolute ? v : pitch0 + delta);
            else vel = js_round(absolute ? v : vel0 + delta);
            if (start < 0 || !span(start, end) || end > limit || !(pitch >= 0 && pitch <= 127 && vel >= 1 && vel <= 127)) {
                ++bad; continue;
            }
            if (start != a) u["start"] = start;
            if (end != b) u["end"] = end;
            if (pitch != pitch0) u["pitch"] = static_cast<int>(pitch);
            if (vel != vel0) u["velocity"] = static_cast<int>(vel);
        } else {
            const double t0 = num(row, "seconds"), v0 = num(row, "value");
            const double t = f == "seconds" ? (absolute ? v : t0 + delta) : t0;
            const double level = f == "value" ? v : v0;
            if (!state_only && !(t >= 0 && t <= limit && level >= 0 && level <= 127)) { ++bad; continue; }
            if (t != t0) u["seconds"] = t;
            if (level != v0) u["value"] = static_cast<int>(level);
        }
        if (u.size() > 1) up.push_back(std::move(u));
    }
    p.preview["outside"] = bad; p.preview["changed"] = bad ? 0 : up.size();
    if (bad) p.refuse("info_boundary", bad); else p.events(k, "updates", std::move(up));
}

void form(Plan& p, Model m, const Json& cmd) {
    const auto k = domain(cmd);
    const auto id = id_of(field(cmd, "id"));
    const auto rows = m.select(Ids{id}, k, k == Kind::controls ? channel(cmd) : 0);
    const auto& old = *rows.front().row;
    const auto& f = object(field(cmd, "fields"), "fields");
    auto u = patch(old);
    if (k == Kind::controls) {
        const double t = num(f, "seconds"), v = num(f, "value");
        if (t < 0 || t > m.duration() || (v != num(old, "value") && v != 0 && v != 127)) {
            p.refuse("cc_form_boundary"); return;
        }
        if (t != num(old, "seconds")) u["seconds"] = t;
        if (v != num(old, "value")) u["value"] = integer(v, 0, 127, "value");
    } else {
        // The older note form supplies four literal fields. Leave global song
        // and score boundary checks to the common transaction validator.
        const double a = num(f, "start"), b = num(f, "end");
        const int pitch = integer(num(f, "pitch"), 0, 127, "pitch");
        const int vel = integer(num(f, "velocity"), 1, 127, "velocity");
        if (a < 0 || !span(a, b)) { p.refuse("note_form_span"); return; }
        if (a != num(old, "start")) u["start"] = a;
        if (b != num(old, "end")) u["end"] = b;
        if (pitch != num(old, "pitch")) u["pitch"] = pitch;
        if (vel != num(old, "velocity")) u["velocity"] = vel;
    }
    if (u.size() > 1) p.events(k, "updates", Json::array({std::move(u)}));
}

Json cc_add(const Json& seconds, int ch, const Json& value) {
    return {{"seconds", seconds}, {"number", 64}, {"channel", ch}, {"value", value}};
}

Json copy_at(const Json& row, Kind k, int ch, double delta, bool shift) {
    if (k == Kind::controls) {
        return cc_add(shift ? Json(finite(num(row, "seconds") + delta, "CC64 position")) : field(row, "seconds"),
                      channel(row, ch), field(row, "value"));
    }
    Json n = row;
    n.erase("id");
    if (shift) {
        n["start"] = finite(num(row, "start") + delta, "copy start");
        n["end"] = finite(num(row, "end") + delta, "copy end");
    }
    return n;
}

struct Extent { double lo; double hi; };
Extent extent(const Refs& rows, Kind k) {
    Extent e{std::numeric_limits<double>::infinity(), -std::numeric_limits<double>::infinity()};
    for (auto r : rows) {
        e.lo = std::min(e.lo, num(*r.row, k == Kind::notes ? "start" : "seconds"));
        e.hi = std::max(e.hi, num(*r.row, k == Kind::notes ? "end" : "seconds"));
    }
    return e;
}

void copy_selection(Plan& p, Model m, const Json& cmd) {
    const auto k = domain(cmd);
    const auto rows = m.select(cmd, k);
    Json snapshot = Json::array();
    for (auto r : rows) snapshot.push_back(*r.row);
    p.preview = {{"clipboard", {{"type", key_of(k)}, {"rows", std::move(snapshot)}}}, {"total", rows.size()}};
}

void repeats(Plan& p, Model m, const Json& cmd, const Json& context, std::string_view action) {
    const auto k = domain(cmd);
    const int ch = k == Kind::controls ? channel(cmd) : 0;
    const auto rows = m.select(cmd, k);
    p.preview["selection_count"] = rows.size();
    if (rows.empty()) return;
    const auto e = extent(rows, k);
    p.preview["lo"] = e.lo; p.preview["hi"] = e.hi;
    const double len = e.hi - e.lo;
    if (action == "repeat_defaults" || action == "selection_extent") {
        if (action == "selection_extent") return;
        const auto* r = find(context, "locators");
        if (!r) r = find(m.doc, "range");
        double step = len;
        bool from_range = false;
        if (k == Kind::controls && r && !r->is_null()) {
            const double a = num(*r, "start"), b = num(*r, "end");
            from_range = b > a + .00001 && a <= e.lo && b >= e.hi;
            if (from_range) step = b - a;
        }
        p.preview["default_step_seconds"] = step; p.preview["from_range"] = from_range;
        return;
    }
    const bool repeat = action == "repeat";
    const int count = repeat ? integer(num(cmd, "count"), 1, 999, "count") : 1;
    const double step = repeat ? positive(num(cmd, "step_seconds"), "repeat step") : len;
    if (!repeat && k == Kind::controls && len < 1e-6) { p.refuse("cc_duplicate_no_span"); return; }
    if (repeat && rows.size() > copy_limit / static_cast<std::size_t>(count)) { p.refuse("repeat_event_limit"); return; }
    const double end = e.hi + step * count;
    if (!std::isfinite(end) || end > m.duration() + tail_eps) { p.refuse("repeat_past_end"); return; }
    Json add = Json::array();
    for (int i = 1; i <= count; ++i) for (auto r : rows) add.push_back(copy_at(*r.row, k, ch, step * i, true));
    p.preview["count"] = count; p.preview["end"] = end; p.preview["total"] = add.size();
    p.events(k, "add", std::move(add));
}

void paste(Plan& p, Model m, const Json& cmd, const Json& context) {
    const auto& clipboard = field(context, "clipboard");
    const auto k = kind_of(string(field(clipboard, "type"), "clipboard type"));
    const auto& rows = array_field(clipboard, "rows");
    const auto where = text(cmd, "at", "cursor");
    if (where != "cursor" && where != "origin") invalid("paste at must be cursor or origin");
    const bool original = where == "origin";
    if (rows.empty()) { p.preview["total"] = 0; return; }
    if (original && rows.size() > copy_limit) { p.refuse("paste_origin_event_limit"); return; }
    double lo = std::numeric_limits<double>::infinity(), hi = -std::numeric_limits<double>::infinity();
    for (const auto& row : rows) {
        const bool notes = k == Kind::notes;
        const double a = num(row, notes ? "start" : "seconds"), b = notes ? num(row, "end") : a;
        if (notes && !span(a, b)) invalid("invalid note in clipboard");
        if (!notes && num(row, "number") != 64) invalid("clipboard controller is not CC64");
        lo = std::min(lo, a); hi = std::max(hi, b);
    }
    const double limit = m.duration();
    // The Document view envelope owns the displayed cursor. Keep the
    // command spelling for existing direct note_command callers.
    const auto* cursor = find(context, "cursor_seconds");
    if (!cursor) cursor = find(cmd, "cursor_seconds");
    const double at = !original && cursor && !cursor->is_null() ? std::max(0.0, number(*cursor, "cursor_seconds")) : 0;
    const double offset = original ? 0 : at - lo;
    const double end = hi + offset, upper = !original && k == Kind::notes ? std::max(limit, hi) : limit;
    if ((original && lo < 0) || !std::isfinite(end) || end > upper + tail_eps) {
        p.refuse(original ? "paste_origin_boundary" : "paste_past_end"); return;
    }
    Json add = Json::array();
    const int ch = k == Kind::controls ? channel(cmd) : 0;
    for (const auto& row : rows) add.push_back(copy_at(row, k, ch, offset, !original));
    p.preview = {{"total", add.size()}, {"offset_seconds", offset}, {"end", end}};
    p.events(k, "add", std::move(add));
}

void erase(Plan& p, Model m, const Json& cmd) {
    const auto k = domain(cmd);
    const auto& ids = array_field(cmd, "ids");
    const auto wanted = id_set(ids);
    const auto rows = m.select(wanted, k, k == Kind::controls ? channel(cmd) : 0);
    Json del = Json::array();
    if (k == Kind::controls) for (auto r : rows) del.push_back(field(*r.row, "id"));
    else {
        Ids seen;
        for (const auto& id : ids) if (seen.insert(id_of(id)).second) del.push_back(id);
    }
    p.preview["total"] = del.size();
    p.events(k, "delete", std::move(del));
}

double drag_seconds(Clock& clock, double world_x, double t, double pressed, double anchor) {
    return frames_equal(t, pressed) ? anchor : clock.at(clock.x(anchor) + (world_x - clock.x(pressed)));
}

void create_note(Plan& p, Model m, Clock& clock, const Json& cmd, const Json& context) {
    const auto& press = object(field(cmd, "press"), "press");
    const auto& s = settings(cmd);
    const double x0 = num(press, "x"), y = num(press, "y");
    const double row = positive(num(cmd, "row_height"), "pitch row height"), unit = length_unit(cmd);
    const bool latched = flag(press, "bypass");
    Snap snap(m, clock, context, magnet_exclusions(cmd));
    const auto a = snap.get(clock.at(x0), press, s, {}, latched);
    const double q0 = clock.beat(a.seconds);
    double end = clock.seconds(q0 + unit);
    if (const auto* last = find(cmd, "current")) {
        const double x = num(*last, "x");
        if (flag(*last, "dragged") && std::abs(x - x0) > .5) {
            const double t = clock.at(x);
            if (Snap::on(s, *last, latched)) {
                const double delta = clock.beat(t) - q0;
                end = clock.seconds(q0 + std::max(unit, js_round(delta / unit) * unit));
            } else if (t > a.seconds) end = t;
        }
    }
    const auto pitch = static_cast<int>(clip(num(cmd, "high_pitch") - std::floor(y / row), 0, 127));
    const int velocity = integer(num(cmd, "velocity", 80), 1, 127, "insert velocity");
    Json n = {{"start", a.seconds}, {"end", end}, {"pitch", pitch}, {"velocity", velocity}, {"channel", 0}};
    p.preview = {{"note", n}, {"start_source", a.source}, {"length_unit", unit}};
    // No extra song-end clamp here: the visible held note used to be allowed
    // to overhang. The common transaction decides whether it can be published.
    if (a.seconds < 0 || !span(a.seconds, end)) { p.refuse("note_creation_span"); return; }
    p.events(Kind::notes, "add", Json::array({std::move(n)}));
}

struct Move { double delta; int pitch; };

void drag_notes(Plan& p, Model m, Clock& clock, const Json& cmd, const Json& context) {
    auto ids = id_set(array_field(cmd, "ids"));
    const auto rows = m.select(ids, Kind::notes, 0);
    const auto& anchor = grabbed(rows, field(cmd, "grabbed_id"));
    const auto edge = part(cmd);
    const bool copy = flag(cmd, "copy");
    if (copy && edge != "move") invalid("copy drag is not a resize");
    const auto& press = object(field(cmd, "press"), "press");
    const auto& s = settings(cmd);
    const double origin = clock.at(num(press, "x"));
    const double row = positive(num(cmd, "row_height"), "pitch row height"), y0 = num(press, "y");
    const bool latched = flag(press, "bypass");
    const double ref = num(anchor, edge == "end" ? "end" : "start");
    const auto e = extent(rows, Kind::notes);
    const double right = std::max(m.duration(), e.hi) - e.hi;
    double minlen = std::numeric_limits<double>::infinity(), low = 127, high = 0;
    for (auto r : rows) {
        const auto& n = *r.row;
        minlen = std::min(minlen, num(n, "end") - num(n, "start"));
        low = std::min(low, num(n, "pitch")); high = std::max(high, num(n, "pitch"));
    }
    const auto start = [&](const Json& n, Move move) {
        const double a = num(n, "start");
        return edge == "end" ? a : std::max(0.0, a + move.delta);
    };
    const auto end = [&](const Json& n, Move move) {
        const double b = num(n, "end");
        return edge == "start" ? b : b + move.delta;
    };
    Snap snap(m, clock, context, magnet_exclusions(cmd, std::move(ids)));
    std::optional<Move> last;
    p.preview = {{"total", rows.size()}, {"move", nullptr}, {"snap_aim", nullptr}, {"at", ref}};
    for (const auto& sample : samples(cmd)) {
        if (!flag(sample, "dragged")) continue;
        const double x = num(sample, "x"), raw = drag_seconds(clock, x, clock.at(x), origin, ref);
        const Aim aim = raw == ref ? Aim{ref, "raw"} : snap.get(raw, sample, s, ref, latched);
        const double shrink = std::max(0.0, minlen), d = aim.seconds - ref;
        const double delta = edge == "move" ? clip(d, -e.lo, right) :
                             edge == "start" ? clip(d, -e.lo, shrink) : clip(d, -shrink, right);
        const double requested = edge == "move" ? js_round((y0 - num(sample, "y")) / row) : 0;
        const Move next{delta, static_cast<int>(clip(requested, -low, 127 - high))};
        if (!last || last->delta != delta || last->pitch != next.pitch) {
            const double margin = minlen + (edge == "start" ? -delta : edge == "end" ? delta : 0);
            const double scale = std::max(1.0, e.hi + std::abs(delta));
            const bool valid = margin > 4 * std::numeric_limits<double>::epsilon() * scale ||
                std::all_of(rows.begin(), rows.end(), [&](Ref r) { return span(start(*r.row, next), end(*r.row, next)); });
            if (valid) last = next;
            else if (edge == "move") last.reset(); // an invalid resize retains its last legal sample
        }
        const double at = ref + (last ? last->delta : 0);
        p.preview["at"] = at;
        p.preview["detected_target"] = std::string_view(aim.source) == "detected" ? Json(aim.seconds) : Json(nullptr);
        p.preview["snap_aim"] = last && last->delta == delta && Snap::on(s, sample, latched) && aim.seconds != raw &&
            std::abs(clock.x(at) - clock.x(raw)) > .5 ? Json(raw) : Json(nullptr);
    }
    if (!last) return;
    p.preview["move"] = {{"delta_seconds", last->delta}, {"pitch_delta", last->pitch}, {"part", edge}};
    Json result = Json::array();
    for (auto r : rows) {
        const auto& n = *r.row;
        const double a = start(n, *last), b = end(n, *last), old_pitch = num(n, "pitch");
        const int pitch = integer(old_pitch + last->pitch, 0, 127, "pitch");
        const auto time = [&](double value, const char* key) { return value == num(n, key) ? field(n, key) : Json(value); };
        if (copy || a != num(n, "start") || b != num(n, "end") || pitch != old_pitch) {
            Json u = copy ? n : patch(n);
            if (copy) u.erase("id");
            u["start"] = time(a, "start"); u["end"] = time(b, "end");
            u["pitch"] = pitch == old_pitch ? field(n, "pitch") : Json(pitch);
            result.push_back(std::move(u));
        }
    }
    p.preview["changed"] = result.size();
    p.events(Kind::notes, copy ? "add" : "updates", std::move(result));
}

const Json* cc_at_tick(const Refs& lane, Clock& clock, double t) {
    const double tick = clock.tick_key(t);
    const auto first = std::lower_bound(lane.begin(), lane.end(), tick, [&](Ref r, double key) {
        return clock.tick_key(num(*r.row, "seconds")) < key;
    });
    return first != lane.end() && clock.tick_key(num(*first->row, "seconds")) == tick ? first->row : nullptr;
}

void insert_cc(Plan& p, Model m, Clock& clock, const Json& cmd, const Json& context) {
    const int ch = channel(cmd);
    const auto& point = object(field(cmd, "point"), "point");
    Snap snap(m, clock, context, magnet_exclusions(cmd));
    const auto aim = snap.get(clock.at(num(point, "x")), point, live_settings(cmd, point));
    const double t = aim.seconds;
    const auto lane = m.lane(ch);
    const Json* hit = nullptr;
    if (flag(cmd, "match_seconds")) {
        const auto first = std::find_if(lane.begin(), lane.end(), [&](Ref r) { return std::abs(num(*r.row, "seconds") - t) < 1e-7; });
        if (first != lane.end()) hit = first->row;
    }
    if (!hit) hit = cc_at_tick(lane, clock, t);
    p.preview = {{"seconds", t}, {"source", aim.source}, {"tick", clock.tick(t)}};
    if (hit) { p.preview["select_id"] = field(*hit, "id"); return; }
    const int v = lane_value(num(point, "y"), num(cmd, "bottom"), positive(num(cmd, "span"), "CC64 span"), 0) >= 64 ? 127 : 0;
    p.events(Kind::controls, "add", Json::array({cc_add(t, ch, v)}));
}

struct DrawPoint { double seconds; double tick; int value; std::size_t sample_index; };

void sweep_cc(Plan& p, Model m, Clock& clock, const Json& cmd, const Json& context) {
    const int ch = channel(cmd);
    const double bottom = num(cmd, "bottom"), height = positive(num(cmd, "span"), "CC64 span");
    const auto& input = samples(cmd);
    if (input.empty()) invalid("CC64 draw needs its press sample");
    Snap snap(m, clock, context, magnet_exclusions(cmd));
    std::vector<DrawPoint> points;
    std::unordered_map<double, std::size_t> positions;
    DrawPoint ghost{};
    std::size_t sample_index = 0;
    for (const auto& sample : input) {
        const double t = snap.get(clock.at(num(sample, "x")), sample, live_settings(cmd, sample)).seconds;
        ghost = {t, clock.tick_key(t), lane_value(num(sample, "y"), bottom, height, 0) >= 64 ? 127 : 0, sample_index++};
        const auto i = positions.find(ghost.tick);
        if (i != positions.end()) points[i->second] = ghost;
        else if (points.size() < sweep_limit) { positions.emplace(ghost.tick, points.size()); points.push_back(ghost); }
    }
    auto ordered = points;
    std::stable_sort(ordered.begin(), ordered.end(), [](DrawPoint a, DrawPoint b) { return a.seconds < b.seconds; });
    const bool swept = flag(cmd, "swept");
    const auto lane = m.lane(ch);
    const auto tick = [&](Ref r) { return clock.tick_key(num(*r.row, "seconds")); };
    auto it = std::lower_bound(lane.begin(), lane.end(), ordered.front().tick,
                               [&](Ref r, double k) { return tick(r) < k; });
    double level = it == lane.begin() ? 0 : num(*(it - 1)->row, "value");
    Json del = Json::array(), essential = Json::array(), sources = Json::array();
    if (swept) {
        // Tick equality extends both inclusive seconds edges of the sweep.
        while (it != lane.end() && tick(*it) <= ordered.back().tick) {
            del.push_back(field(*it->row, "id")); ++it;
        }
    }
    for (const auto& q : ordered) {
        // In a still hover/draw, same-tick existing rows count as the level
        // that would hold if this candidate is omitted. No collision policy
        // for incoming units is implemented here.
        while (it != lane.end() && tick(*it) <= q.tick) { level = num(*it->row, "value"); ++it; }
        if (q.value != level) {
            essential.push_back(cc_add(q.seconds, ch, q.value));
            if (swept) sources.push_back(q.sample_index);
        }
        level = q.value;
    }
    const bool retained = positions.count(ghost.tick) != 0;
    p.preview = {{"ghost", cc_add(ghost.seconds, ch, ghost.value)}, {"overwrite_count", del.size()},
                 {"sampled_points", points.size()}, {"display_add", essential}, {"final_retained", retained}};
    if (!retained) p.warn("cc_sweep_limit");
    Json add = Json::array();
    if (swept) add = std::move(essential);
    else for (const auto& q : points) {
        add.push_back(cc_add(q.seconds, ch, q.value)); sources.push_back(q.sample_index);
    }
    // Index into the received evidence, not into an independently coalesced
    // UI point list. Position in this array is the event reducer's add index.
    p.preview["add_sources"] = std::move(sources);
    if (!add.empty() || !del.empty()) {
        p.op = {{"type", "controls"}, {"add", std::move(add)}};
        if (!del.empty()) p.op["delete"] = std::move(del);
    }
}

struct LiveCC {
    const Json* original;
    std::size_t index;
    double t;
    double v;
};

void drag_cc(Plan& p, Model m, Clock& clock, const Json& cmd, const Json& context) {
    const auto* line = find(cmd, "line_id");
    const bool creating = line != nullptr, copy = flag(cmd, "copy");
    if (creating && copy) invalid("CC64 line creation is not a copy gesture");
    const int ch = channel(cmd);
    const auto& press = object(field(cmd, "press"), "press");
    const double origin = clock.at(num(press, "x")), y0 = num(press, "y");
    const double height = positive(num(cmd, "span"), "CC64 span");
    const auto ids = creating ? Ids{id_of(*line)} : id_set(array_field(cmd, "ids"));
    const auto selected = m.select(ids, Kind::controls, ch);
    const auto& anchor = creating ? *selected.front().row : grabbed(selected, field(cmd, "grabbed_id"));
    std::vector<LiveCC> rows;
    if (creating) rows.push_back({nullptr, 0, origin, num(anchor, "value") >= 64 ? 127.0 : 0.0});
    else {
        // Optional native-owned presentation origin from an in-flight settle.
        // It never supplies identity, channel, or the value to keep on release.
        const auto* display = find(context, "cc_display");
        if (display) object(*display, "cc_display");
        for (auto r : selected) {
            const auto& n = *r.row;
            double t = num(n, "seconds"), v = num(n, "value");
            if (display) {
                auto it = display->find(field(n, "id").get_ref<const std::string&>());
                if (it != display->end()) {
                    t = num(*it, "seconds"); v = num(*it, "value");
                    if (t < 0 || v < 0 || v > 127) invalid("invalid CC64 settle origin");
                }
            }
            rows.push_back({&n, r.index, t, v});
        }
        std::sort(rows.begin(), rows.end(), [](LiveCC a, LiveCC b) { return a.t != b.t ? a.t < b.t : a.index < b.index; });
    }
    const auto a = creating ? rows.begin() : std::find_if(rows.begin(), rows.end(), [&](const LiveCC& r) { return r.original == &anchor; });
    const double minimum = -rows.front().t, maximum = m.duration() - rows.back().t;
    double delta = 0, commit_delta = 0, pv = a->v;
    bool time_active = creating, value_active = false, moved = false;
    std::optional<int> target;
    Snap snap(m, clock, context, magnet_exclusions(cmd));
    const auto& stream = samples(cmd);
    if (creating && stream.empty()) commit_delta = snap.get(origin, press, live_settings(cmd, press)).seconds - origin;
    for (const auto& sample : stream) {
        const double x = num(sample, "x"), dx = num(sample, "client_dx"), dy = num(sample, "y") - y0;
        const auto& cfg = live_settings(cmd, sample);
        delta = clip(drag_seconds(clock, x, clock.at(x), origin, a->t) - a->t, minimum, maximum);
        time_active = creating || std::abs(dx) >= (Snap::on(cfg, sample) ? 3.0 : .5);
        commit_delta = time_active ? clip(snap.get(a->t + delta, sample, cfg, creating ? std::nullopt :
            std::optional<double>{a->t}).seconds - a->t, minimum, maximum) : 0;
        value_active = std::abs(dy) >= 4;
        pv = a->v;
        if (value_active) {
            const double blend = std::min(1.0, (std::abs(dy) - 4) / 4);
            const double travel = dy * blend * blend * (3 - 2 * blend);
            pv = clip(a->v - travel / height * 127, 0, 127);
            if (!target) target = a->v >= 64 ? 127 : 0;
            const double hysteresis = 3 / height * 127;
            if (pv > 63.5 + hysteresis) target = 127;
            else if (pv < 63.5 - hysteresis) target = 0;
        }
        moved = delta != 0 || value_active;
    }
    bool changed = creating;
    Json ops = Json::array(), ghosts = Json::array(), targets = Json::array();
    std::size_t index = 0;
    double insert_seconds = 0;
    for (const auto& r : rows) {
        const double display_t = finite(r.t + delta, "CC64 ghost seconds");
        const double display_v = value_active ? clip(r.v + (pv - a->v), 0, 127) : r.v;
        const double old_t = r.original ? num(*r.original, "seconds") : r.t;
        const double old_v = r.original ? num(*r.original, "value") : r.v;
        const double t = finite(r.original && !time_active ? old_t : display_t + commit_delta - delta, "CC64 landing time");
        const int v = integer(value_active ? static_cast<double>(*target) : old_v, 0, 127, "CC64 value");
        const Json saved_t = r.original && t == old_t ? field(*r.original, "seconds") : Json(t);
        Json g = cc_add(display_t, ch, display_v), out = cc_add(saved_t, ch, v);
        if (r.original) { g["id"] = field(*r.original, "id"); out["id"] = field(*r.original, "id"); }
        if (creating || copy) {
            g["add_index"] = index; out["add_index"] = index;
            ops.push_back(cc_add(saved_t, ch, v));
        } else ops.push_back({{"id", field(*r.original, "id")}, {"seconds", saved_t}, {"value", v}});
        ghosts.push_back(std::move(g)); targets.push_back(std::move(out));
        changed = changed || t != old_t || v != old_v;
        insert_seconds = t; ++index;
    }
    p.preview = {{"ghosts", std::move(ghosts)}, {"targets", std::move(targets)}, {"time_active", time_active},
                 {"value_active", value_active}, {"moved", moved}, {"preview_delta_seconds", delta},
                 {"commit_delta_seconds", commit_delta}, {"binary_target", value_active ? Json(*target) : Json(nullptr)}};
    if (creating && !moved) {
        if (const auto* hit = cc_at_tick(m.lane(ch), clock, insert_seconds)) {
            p.preview["select_id"] = field(*hit, "id"); return;
        }
    }
    if (changed) p.events(Kind::controls, creating || copy ? "add" : "updates", std::move(ops));
}

void snap_query(Plan& p, Model m, Clock& clock, const Json& cmd, const Json& context) {
    const auto& sample = object(field(cmd, "point"), "point");
    const auto* x = find(sample, "x");
    const double raw = x ? clock.at(number(*x, "x")) : num(sample, "seconds");
    const auto* ref = find(cmd, "reference_seconds");
    Snap snap(m, clock, context, magnet_exclusions(cmd));
    const auto aim = snap.get(raw, sample, live_settings(cmd, sample),
        ref && !ref->is_null() ? std::optional<double>{number(*ref, "reference_seconds")} : std::nullopt,
        flag(cmd, "latched_free"));
    p.preview = {{"seconds", aim.seconds}, {"source", aim.source}};
}

} // namespace

Json note_command(const Json& document, const Json& cmd, const Json& context) {
    const Model m{object(document, "document")};
    object(cmd, "semantic command"); object(context, "context");
    const auto action = string(field(cmd, "action"), "action");
    Clock clock(m, context);
    Plan p;
    if (action == "quantize") quantize(p, m, clock, cmd);
    else if (action == "fixed_lengths") fixed_lengths(p, m, cmd);
    else if (action == "pedals_to_note_length") pedals_to_note_length(p, m, cmd);
    else if (action == "legato") {
        if (find(cmd, "percent") || find(cmd, "overlap_ticks") || flag(cmd, "all_notes"))
            adjusted_legato(p, m, cmd);
        else legato(p, m, cmd);
    }
    else if (action == "delete_overlaps") delete_overlaps(p, m, cmd);
    else if (action == "transpose") transpose(p, m, cmd);
    else if (action == "length_scale") length_scale(p, m, cmd);
    else if (action == "velocity") velocity(p, m, clock, cmd);
    else if (action == "nudge") nudge(p, m, clock, cmd);
    else if (action == "info_assign" || action == "cc_state") info(p, m, cmd, action == "cc_state");
    else if (action == "form_assign") form(p, m, cmd);
    else if (action == "copy") copy_selection(p, m, cmd);
    else if (action == "paste") paste(p, m, cmd, context);
    else if (action == "repeat" || action == "repeat_defaults" || action == "duplicate" || action == "selection_extent")
        repeats(p, m, cmd, context, action);
    else if (action == "delete") erase(p, m, cmd);
    else if (action == "note_create") create_note(p, m, clock, cmd, context);
    else if (action == "note_drag") drag_notes(p, m, clock, cmd, context);
    else if (action == "cc_insert") insert_cc(p, m, clock, cmd, context);
    else if (action == "cc_sweep") sweep_cc(p, m, clock, cmd, context);
    else if (action == "cc_drag") drag_cc(p, m, clock, cmd, context);
    else if (action == "snap_time") snap_query(p, m, clock, cmd, context);
    else if (action == "grid_unit") p.preview["unit"] = clock.unit(settings(cmd));
    else if (action == "cc_tick") p.preview["tick"] = clock.tick(num(cmd, "seconds"));
    else invalid("unknown action");
    return std::move(p).result();
}

} // namespace astra::editor
