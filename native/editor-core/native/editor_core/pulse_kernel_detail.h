#pragma once
#include "native_json.h"
#include <algorithm>
#include <array>
#include <charconv>
#include <cmath>
#include <cstddef>
#include <cstdint>
#include <locale>
#include <sstream>
#include <stdexcept>
#include <string>
#include <string_view>
#include <system_error>
#include <utility>
#include <vector>

namespace astra::editor::pulse_detail {
using D = std::vector<double>;
inline constexpr double eps = 1e-8;
inline constexpr std::size_t none = static_cast<std::size_t>(-1);

[[noreturn]] inline void bad(std::string_view message) {
    throw std::invalid_argument(std::string(message));
}
inline const Json& object(const Json& j) {
    if (!j.is_object()) bad("Expected a JSON object");
    return j;
}
inline const Json& array(const Json& j) {
    if (!j.is_array()) bad("Expected a JSON array");
    return j;
}
inline const Json& opt(const Json& j, const char* key) {
    static const Json null = nullptr;
    const auto it = object(j).find(key);
    return it == j.end() ? null : *it;
}
inline const Json& otherwise(const Json& j, const char* key, const Json& fallback) {
    const auto it = object(j).find(key);
    return it == j.end() ? fallback : *it;
}
inline const Json& rows_or_empty(const Json& j, const char* key) {
    static const Json empty = Json::array();
    return array(otherwise(j, key, empty));
}
inline std::string_view text(const Json& j, std::string_view message = "Expected text") {
    if (!j.is_string()) bad(message);
    return j.get_ref<const std::string&>();
}
inline bool is(const Json& j, std::string_view s) {
    return j.is_string() && text(j) == s;
}
inline bool truth(const Json& j) {
    if (j.is_null()) return false;
    if (j.is_boolean()) return j.get<bool>();
    if (j.is_number()) return j.get<double>() != 0.;
    if (j.is_string()) return !text(j).empty();
    return !j.empty();
}
inline double num(const Json& j, std::string_view message = "Expected a finite number") {
    if (!j.is_number()) bad(message);
    const double v = j.get<double>();
    if (!std::isfinite(v)) bad(message);
    return v;
}
// The older rhythm_intent endpoints, unlike apply_pulses and draft endpoints,
// accept finite numeric text. Do not use the process locale to parse it.
inline double intent_num(const Json& j, std::string_view message) {
    if (!j.is_string()) return num(j, message);
    const auto s = text(j);
    std::string clean;
    clean.reserve(s.size());
    const auto digit = [](char c) { return c >= '0' && c <= '9'; };
    for (std::size_t i = 0; i < s.size(); ++i) {
        if (s[i] != '_') clean.push_back(s[i]);
        else if (!i || i + 1 == s.size() || !digit(s[i - 1]) || !digit(s[i + 1])) bad(message);
    }
    std::istringstream in(clean);
    in.imbue(std::locale::classic());
    double v = 0.;
    if (!(in >> v) || !std::isfinite(v)) bad(message);
    in >> std::ws;
    if (!in.eof()) bad(message);
    return v;
}
inline int integer(const Json& j, int lo, int hi, std::string_view message) {
    if (!j.is_number_integer()) bad(message);
    const double v = j.get<double>();
    if (v < lo || v > hi) bad(message);
    return static_cast<int>(v);
}
inline std::uint64_t count(const Json& j, std::string_view message) {
    if (j.is_number_unsigned()) return j.get<std::uint64_t>();
    if (!j.is_number_integer() || j.get<std::int64_t>() < 0) bad(message);
    return static_cast<std::uint64_t>(j.get<std::int64_t>());
}
inline std::string fmt(double v, std::chars_format mode = std::chars_format::general,
                       int precision = 6) {
    char buf[384];
    const auto r = std::to_chars(buf, buf + sizeof(buf), v, mode, precision);
    if (r.ec != std::errc{}) bad("Cannot format coordinate");
    return std::string(buf, r.ptr);
}
inline std::string fixed10(double v) { return fmt(v, std::chars_format::fixed, 10); }
inline bool same(const Json& a, const Json& b) {
    if (a.is_object() && b.is_object()) {
        if (a.size() != b.size()) return false;
        for (auto i = a.begin(); i != a.end(); ++i) {
            const auto j = b.find(i.key());
            if (j == b.end() || !same(i.value(), j.value())) return false;
        }
        return true;
    }
    if (a.is_array() && b.is_array()) {
        if (a.size() != b.size()) return false;
        for (std::size_t i = 0; i < a.size(); ++i) if (!same(a[i], b[i])) return false;
        return true;
    }
    return a == b;
}
inline double field(const Json& row, const char* key) { return num(row.at(key)); }
inline bool denominator(double d) {
    return d == 1 || d == 2 || d == 4 || d == 8 || d == 16 || d == 32 || d == 64 || d == 128;
}
struct Meter {
    int n, d;
    double unit() const { return 4. / d; }
    double length() const { return n * 4. / d; }
};
inline Meter meter(const Json& n, const Json& d, std::string_view message,
                   bool strict_denominator = true) {
    const int nn = integer(n, 1, 32, message);
    if (strict_denominator && !d.is_number_integer()) bad(message);
    const double dd = !strict_denominator && d.is_boolean() ? (d.get<bool>() ? 1. : 0.) : num(d, message);
    if (!denominator(dd)) bad(message);
    return {nn, static_cast<int>(dd)};
}
inline Meter meter_of(const Json& r) {
    return meter(r.at("numerator"), r.at("denominator"), "无效拍号", false);
}
inline Meter meter_op(const Json& op, std::string_view message, bool strict = true) {
    return meter(opt(op, "numerator"), otherwise(op, "denominator", Json(4)), message, strict);
}
inline double opening_beat(const Json& tempo) {
    return intent_num(otherwise(tempo, "first_full_bar_beat", otherwise(tempo, "pickup_beats", Json(0))),
                      "无效开头位置");
}
inline const Json& signatures(const Json& tempo) {
    static const Json fallback = Json::array({Json{{"beat", 0}, {"numerator", 4}, {"denominator", 4}}});
    const auto& r = array(otherwise(tempo, "time_signatures", fallback));
    if (r.empty()) bad("缺少起始拍号");
    return r;
}
inline const Json& effective(const Json& rows, const char* key, double v, double tolerance) {
    if (rows.empty()) bad("Missing initial row");
    const auto it = std::upper_bound(rows.begin(), rows.end(), v + tolerance,
                                    [key](double x, const Json& r) { return x < field(r, key); });
    return it == rows.begin() ? rows.front() : *std::prev(it);
}
inline std::size_t lower(const Json& rows, const char* key, double v) {
    return static_cast<std::size_t>(std::lower_bound(rows.begin(), rows.end(), v,
        [key](const Json& r, double x) { return field(r, key) < x; }) - rows.begin());
}
inline std::size_t at(const Json& rows, const char* key, double v, double tolerance = eps) {
    for (auto i = lower(rows, key, v - tolerance); i < rows.size(); ++i) {
        const double x = field(rows[i], key);
        if (std::abs(x - v) < tolerance) return i;
        if (x > v) break;
    }
    return none;
}
inline void sort_rows(Json& rows, const char* key) {
    std::stable_sort(rows.begin(), rows.end(), [key](const Json& a, const Json& b) {
        return field(a, key) < field(b, key);
    });
}
inline D column(const Json& rows, const char* key) {
    D out;
    out.reserve(array(rows).size());
    for (const auto& r : rows) out.push_back(field(r, key));
    return out;
}
inline void sort_unique(D& v) {
    std::sort(v.begin(), v.end());
    v.erase(std::unique(v.begin(), v.end()), v.end());
}
inline D joined(const D& a, const D& b) {
    D v;
    v.reserve(a.size() + b.size());
    std::set_union(a.begin(), a.end(), b.begin(), b.end(), std::back_inserter(v));
    return v;
}
inline D anchor_times(const Json& tempo) {
    D v;
    const auto& anchors = array(tempo.at("anchors"));
    v.reserve(anchors.size());
    for (const auto& a : anchors) v.push_back(intent_num(a.at("seconds"), "Invalid anchor seconds"));
    return v;
}
struct End { double seconds, beat; };
inline End extent(const Json& tempo) {
    const auto& a = array(tempo.at("anchors"));
    if (a.size() < 2) bad("Tempo Map 至少需要两个锚点");
    return {intent_num(a.back().at("seconds"), "Invalid anchor seconds"),
            intent_num(a.back().at("beat"), "Invalid anchor beat")};
}
inline Json validated(const Json& tempo) {
    return timing_query("validate_tempo", Json{{"tempo", tempo}});
}
struct Projection { D q, t; };
inline Projection project(const Json& tempo, const D& seconds, const D& beats = {}) {
    if (seconds.empty() && beats.empty()) return {};
    // Only the stored mathematical anchors are needed by a conversion. In
    // particular, don't copy a tempo's baseline, evidence and visible points
    // into each conversion request, or validate its grouping during a warp.
    const Json r = timing_query("convert_many", Json{{"anchors", tempo.at("anchors")},
                                                     {"seconds", seconds}, {"beats", beats}});
    auto q = r.at("beats").get<D>();
    auto t = r.at("seconds").get<D>();
    if (q.size() != seconds.size() || t.size() != beats.size()) bad("Invalid convert_many result");
    return {std::move(q), std::move(t)};
}
inline double beat(const Json& tempo, double t) { return project(tempo, {t}).q.front(); }
inline double seconds(const Json& tempo, double q) { return project(tempo, {}, {q}).t.front(); }
inline D interpolate(const D& xs, const D& ys, const D& values) {
    if (values.empty()) return {};
    auto r = timing_query("interpolate_many", Json{{"x", xs}, {"y", ys}, {"values", values}}).get<D>();
    if (r.size() != values.size()) bad("Invalid interpolate_many result");
    return r;
}
inline int round_even_small(double v) {
    return timing_query("round_even", Json{{"value", v}}).get<int>();
}
struct Bars { D starts, lengths; std::vector<Meter> meters; };
inline Bars bar_columns(const Json& b) {
    if (!b.is_array() || b.size() != 3) bad("Invalid reference_bars result");
    Bars v{b.at(0).get<D>(), b.at(1).get<D>(), {}};
    const auto& meters = array(b.at(2));
    if (v.starts.size() != v.lengths.size() || v.starts.size() != meters.size()) bad("Invalid bar columns");
    for (const auto& j : meters) {
        const auto s = text(j);
        const auto sep = s.find('/');
        if (sep == s.npos) bad("Invalid reference bar meter");
        int n = 0, d = 0;
        const auto a = std::from_chars(s.data(), s.data() + sep, n);
        const auto z = std::from_chars(s.data() + sep + 1, s.data() + s.size(), d);
        if (a.ec != std::errc{} || z.ec != std::errc{} || a.ptr != s.data() + sep ||
            z.ptr != s.data() + s.size() || n < 1 || n > 32 || !denominator(d)) bad("Invalid reference bar meter");
        v.meters.push_back({n, d});
    }
    return v;
}
inline Bars reference(const Json& tempo, bool performance_extent) {
    Json args{{"tempo", tempo}};
    if (performance_extent) {
        auto& clock=args.at("tempo");
        clock.erase("score_end");
        const auto& policy=opt(clock,"opening_policy");
        if (policy.is_object() && opt(policy,"keep_before_bar1").is_boolean()) clock.erase("score_start_seconds");
    }
    return bar_columns(timing_query("reference_bars", args));
}
struct Geometry { double end_q; Bars bars; };
inline Geometry geometry(const Json& tempo) {
    const auto g = timing_query("geometry", Json{{"tempo", tempo}});
    return {field(g, "last_beat"), bar_columns(Json::array({g.at("starts"),g.at("lengths"),g.at("meters")}))};
}
inline Json transport_rows(const Json& tempo, const D& old_t, const D& samples,
                           const D& new_t, const D& new_q) {
    const auto& a = tempo.at("anchors");
    Json out = Json::array();
    auto& dst = out.get_ref<Json::array_t&>();
    dst.reserve(samples.size());
    for (std::size_t i = 0, k = 0; i < samples.size(); ++i) {
        while (k < old_t.size() && old_t[k] < samples[i]) ++k;
        if (k < old_t.size() && old_t[k] == samples[i]) {
            Json r = a[k];
            if (new_t[i] != old_t[k]) r["seconds"] = new_t[i];
            if (new_q[i] != intent_num(r.at("beat"), "Invalid anchor beat")) r["beat"] = new_q[i];
            dst.push_back(std::move(r));
        } else dst.push_back(Json{{"seconds", new_t[i]}, {"beat", new_q[i]}});
    }
    return out;
}

// SHA-256 only for compatibility/opaque row labels in this module. No project
// hash, file digest, trust decision or session token is derived here. In
// particular legacy pulse IDs must match the Python ruler byte-for-byte.
inline std::string row_digest(std::string_view s) {
    constexpr std::uint32_t k[64] = {
        0x428a2f98,0x71374491,0xb5c0fbcf,0xe9b5dba5,0x3956c25b,0x59f111f1,0x923f82a4,0xab1c5ed5,
        0xd807aa98,0x12835b01,0x243185be,0x550c7dc3,0x72be5d74,0x80deb1fe,0x9bdc06a7,0xc19bf174,
        0xe49b69c1,0xefbe4786,0x0fc19dc6,0x240ca1cc,0x2de92c6f,0x4a7484aa,0x5cb0a9dc,0x76f988da,
        0x983e5152,0xa831c66d,0xb00327c8,0xbf597fc7,0xc6e00bf3,0xd5a79147,0x06ca6351,0x14292967,
        0x27b70a85,0x2e1b2138,0x4d2c6dfc,0x53380d13,0x650a7354,0x766a0abb,0x81c2c92e,0x92722c85,
        0xa2bfe8a1,0xa81a664b,0xc24b8b70,0xc76c51a3,0xd192e819,0xd6990624,0xf40e3585,0x106aa070,
        0x19a4c116,0x1e376c08,0x2748774c,0x34b0bcb5,0x391c0cb3,0x4ed8aa4a,0x5b9cca4f,0x682e6ff3,
        0x748f82ee,0x78a5636f,0x84c87814,0x8cc70208,0x90befffa,0xa4506ceb,0xbef9a3f7,0xc67178f2};
    std::uint32_t h[8] = {0x6a09e667,0xbb67ae85,0x3c6ef372,0xa54ff53a,0x510e527f,0x9b05688c,0x1f83d9ab,0x5be0cd19};
    std::vector<std::uint8_t> b(s.begin(), s.end());
    b.push_back(0x80);
    while (b.size() % 64 != 56) b.push_back(0);
    const auto bits = static_cast<std::uint64_t>(s.size()) * 8;
    for (int i = 56; i >= 0; i -= 8) b.push_back(static_cast<std::uint8_t>(bits >> i));
    const auto rr = [](std::uint32_t v, unsigned n) { return (v >> n) | (v << (32 - n)); };
    for (std::size_t off = 0; off < b.size(); off += 64) {
        std::uint32_t w[64];
        for (int i = 0; i < 16; ++i) {
            const auto j = off + 4 * i;
            w[i] = (std::uint32_t(b[j]) << 24) | (std::uint32_t(b[j+1]) << 16) |
                   (std::uint32_t(b[j+2]) << 8) | std::uint32_t(b[j+3]);
        }
        for (int i = 16; i < 64; ++i) {
            const auto a = w[i-15], z = w[i-2];
            w[i] = w[i-16] + (rr(a,7) ^ rr(a,18) ^ (a >> 3)) + w[i-7] + (rr(z,17) ^ rr(z,19) ^ (z >> 10));
        }
        auto [a,c,d,e,f,g,u,v] = std::array<std::uint32_t,8>{h[0],h[1],h[2],h[3],h[4],h[5],h[6],h[7]};
        for (int i = 0; i < 64; ++i) {
            const auto t1 = v + (rr(f,6) ^ rr(f,11) ^ rr(f,25)) + ((f & g) ^ (~f & u)) + k[i] + w[i];
            const auto t2 = (rr(a,2) ^ rr(a,13) ^ rr(a,22)) + ((a & c) ^ (a & d) ^ (c & d));
            v=u; u=g; g=f; f=e+t1; e=d; d=c; c=a; a=t1+t2;
        }
        h[0]+=a; h[1]+=c; h[2]+=d; h[3]+=e; h[4]+=f; h[5]+=g; h[6]+=u; h[7]+=v;
    }
    constexpr char hex[] = "0123456789abcdef";
    std::string out;
    out.reserve(64);
    for (const auto x : h) for (int i = 28; i >= 0; i -= 4) out.push_back(hex[(x >> i) & 15]);
    return out;
}
}
