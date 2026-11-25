#pragma once

#include <nlohmann/json.hpp>
#include "native_json.h"

#include <cmath>
#include <cstddef>
#include <cstdint>
#include <optional>
#include <stdexcept>
#include <string>
#include <string_view>
#include <vector>
#include <utility>

namespace astra::editor::timing_detail {

inline constexpr double edit_eps = 1e-8;
inline constexpr double bar_eps = 1e-7;
inline constexpr double exact_sample_eps = 1e-10;
inline constexpr int denominators[] = {1, 2, 4, 8, 16, 32, 64, 128};

[[noreturn]] inline void invalid(std::string_view reason) {
    throw std::invalid_argument(std::string(reason));
}
inline const Json& object(const Json& j, std::string_view reason = "需要 JSON 对象") {
    if (!j.is_object()) invalid(reason);
    return j;
}
inline const Json& array(const Json& j, std::string_view reason = "需要 JSON 数组") {
    if (!j.is_array()) invalid(reason);
    return j;
}
inline const Json* find(const Json& j, const char* key) {
    const auto it = j.find(key);
    return it == j.end() ? nullptr : &*it;
}
inline const Json& nil() { static const Json value; return value; }
inline const Json& empty_array() { static const Json value = Json::array(); return value; }
inline const Json& get(const Json& j, const char* key, const Json& fallback = nil()) {
    if (const auto* p = find(j, key)) return *p;
    return fallback;
}
// A temporary fallback must not escape by reference; value ownership extends
// through a caller's bound const reference, including a missing string default.
inline Json get(const Json& j, const char* key, Json&& fallback) {
    if (const auto* p = find(j, key)) return *p;
    return std::move(fallback);
}
inline const Json& required(const Json& j, const char* key) {
    if (const auto* p = find(j, key)) return *p;
    invalid(std::string("缺少字段：") + key);
}
inline bool text_is(const Json& j, std::string_view text) {
    return j.is_string() && std::string_view(j.get_ref<const std::string&>()) == text;
}
inline const std::string& text(const Json& j, std::string_view reason) {
    if (!j.is_string()) invalid(reason);
    return j.get_ref<const std::string&>();
}
inline bool truth(const Json& j) {
    if (j.is_null()) return false;
    if (j.is_boolean()) return j.get<bool>();
    if (j.is_number()) return j.get<double>() != 0.;
    if (j.is_string()) return !j.get_ref<const std::string&>().empty();
    return !j.empty();
}
inline double finite(double x, std::string_view reason = "必须是有限数值") {
    if (!std::isfinite(x)) invalid(reason);
    return x;
}

// Distinct Python conversion sites: strict int/float, arithmetic (also bool),
// float(...) (also bool and decimal strings), and tempo_edit._number (no bool).
enum class Num { strict, arithmetic, floating, edit };
double number(const Json&, Num = Num::strict, std::string_view reason = "必须是有限数值");
bool numeric_less(const Json&, const Json&);
int bounded_int(const Json&, int lo, int hi, std::string_view reason);
struct Integer {
    bool negative;
    std::uint64_t magnitude;
    double as_double() const;
    Json json() const;
};
Integer integer_value(const Json&, bool coerce = false);
Integer integer_double(double);
double round_even(double);
double positive_mod(double, double);
std::string format_g(double, bool sign = false);
std::string format_3f(double);
std::string float_repr(double);

struct Meter {
    int n, d;
    double unit() const { return 4. / d; }
    double length() const { return n * 4. / d; }
    bool operator==(Meter other) const { return n == other.n && d == other.d; }
    bool operator!=(Meter other) const { return !(*this == other); }
};
Meter meter_of(const Json& row, bool strict_denominator = false,
               std::string_view reason = "无效拍号或拍号位置");

// The typed implementations of timing_query. Structural algorithms reuse a
// checked curve and borrow the original document (with sparse overrides for a
// prospective rebar), rather than serializing and validating on every sample.
struct Layout {
    const Json& base;
    const Json* patch = nullptr;
    Layout(const Json& b, const Json* p = nullptr) : base(object(b, "速度图必须是对象。")), patch(p) {}
    const Json* find(const char* key) const;
    const Json& get(const char* key, const Json& fallback = nil()) const;
    Json get(const char* key, Json&& fallback) const { return get(key, static_cast<const Json&>(fallback)); }
    const Json& rows() const;
};
void validate_layout(Layout);
void ensure_default_meter(Json&);
struct Curve {
    std::vector<double> seconds, beats;
    explicit Curve(const Json& anchors);
    Curve(std::vector<double> seconds, std::vector<double> beats);
private:
    void validate() const;
};
Curve checked_curve(const Json& tempo);
enum class InputAxis { seconds, beat };
double timing_query(const Curve&, InputAxis, double, bool signed_edge = false);
double interpolate_coordinates(const std::vector<double>& xs,const std::vector<double>& ys,double value);

struct Bar {
    double start, length;
    Meter meter;
    std::string label;
    bool integer_label;
    double end() const { return start + length; }
};
struct BarsRequest {
    std::optional<double> last_beat;
    bool ignore_score_end = false;
    bool strict = false;
};
struct Bars {
    double last_beat;
    std::vector<Bar> items;
};
Bars timing_query(Layout, const Curve&, BarsRequest);
struct EndingMeasure {};
Json timing_query(Layout, const Curve&, EndingMeasure);
std::optional<double> confirmed_end(Layout, const Curve&);

struct Line {
    double beat, seconds;
    std::string meter;
    std::size_t index;
    std::int64_t bar_number;
    bool opening, terminal;
};
std::vector<Line> boundary_lines(Layout, const Curve&, const Bars&);
std::string tempo_sha(const Json&);
std::string line_id(const std::string& revision, const std::string& sha, const Line&);
const std::string& revision_value(const Json&);

std::size_t signature_index(const Json& rows, double beat, double epsilon);
struct FlagNeighbors { double left, right, before, after; };
bool on_flag_grid(Layout, double beat);
FlagNeighbors flag_neighbors(Layout, const Curve&, double beat);

// JSON dispatch for the structure-dependent previews; linked with timing_model.cpp.
Json structural_preview_query(std::string_view, const Json&);

} // namespace astra::editor::timing_detail
