#include "astra_editor_core.h"
#include "range_projection.h"

#include <algorithm>
#include <array>
#include <atomic>
#include <cmath>
#include <cstring>
#include <limits>
#include <map>
#include <memory>
#include <new>
#include <optional>
#include <set>
#include <string>
#include <tuple>
#include <utility>
#include <vector>

namespace {
using Key = uint64_t;
constexpr Key max_key = std::numeric_limits<Key>::max();

struct Failure { int32_t status; };
void need(bool condition, int32_t status = AEC_INVALID) {
    if (!condition) throw Failure{status};
}
template<class F> int32_t boundary(F &&f) noexcept {
    try { f(); return AEC_OK; }
    catch (const Failure &e) { return e.status; }
    catch (const std::bad_alloc &) { return AEC_NOMEM; }
    catch (...) { return AEC_INTERNAL; }
}
template<class T> void envelope(const T *p) {
    need(p && p->abi == AEC_ABI && p->size == sizeof(T));
}
template<class T> void array_input(const T *p, size_t n) {
    need(n == 0 || p != nullptr);
    need(n <= static_cast<size_t>(std::numeric_limits<ptrdiff_t>::max()) / sizeof(T), AEC_LIMIT);
}
void revision_input(aec_bytes b) { array_input(b.data, b.count); need(b.count != 0); }

// Immutable AVL nodes: a candidate allocates only paths it changes. No
// iterator or pointer into one index is used as an ID or returned over the ABI.
template<class K, class V> struct Index {
    struct Node;
    using Root = std::shared_ptr<const Node>;
    struct Node {
        K key;
        V value;
        Root left, right;
        int height;
        Node(const K &k, const V &v, const Root &l, const Root &r)
            : key(k), value(v), left(l), right(r), height(1 + std::max(h(l), h(r))) {}
    };
    static int h(const Root &p) { return p ? p->height : 0; }
    static Root node(const K &k, const V &v, const Root &l, const Root &r) {
        return std::make_shared<const Node>(k, v, l, r);
    }
    static Root balance(const K &k, const V &v, const Root &l, const Root &r) {
        if (h(l) > h(r) + 1) {
            if (h(l->left) >= h(l->right))
                return node(l->key, l->value, l->left, node(k, v, l->right, r));
            const auto &p = l->right;
            return node(p->key, p->value, node(l->key, l->value, l->left, p->left),
                        node(k, v, p->right, r));
        }
        if (h(r) > h(l) + 1) {
            if (h(r->right) >= h(r->left))
                return node(r->key, r->value, node(k, v, l, r->left), r->right);
            const auto &p = r->left;
            return node(p->key, p->value, node(k, v, l, p->left),
                        node(r->key, r->value, p->right, r->right));
        }
        return node(k, v, l, r);
    }
    static const V *find(const Root &p, const K &k) {
        auto n = p.get();
        while (n) {
            if (k < n->key) n = n->left.get();
            else if (n->key < k) n = n->right.get();
            else return &n->value;
        }
        return nullptr;
    }
    static Root put(const Root &p, const K &k, const V &v) {
        if (!p) return node(k, v, {}, {});
        if (k < p->key) return balance(p->key, p->value, put(p->left, k, v), p->right);
        if (p->key < k) return balance(p->key, p->value, p->left, put(p->right, k, v));
        return node(k, v, p->left, p->right);
    }
    static Root erase(const Root &p, const K &k) {
        if (!p) return {};
        if (k < p->key) return balance(p->key, p->value, erase(p->left, k), p->right);
        if (p->key < k) return balance(p->key, p->value, p->left, erase(p->right, k));
        if (!p->left) return p->right;
        if (!p->right) return p->left;
        auto n = p->right.get();
        while (n->left) n = n->left.get();
        return balance(n->key, n->value, p->left, erase(p->right, n->key));
    }
    static Root build(const std::vector<std::pair<K, V>> &sorted, size_t first, size_t last) {
        if (first == last) return {};
        const size_t mid = first + (last - first) / 2;
        return node(sorted[mid].first, sorted[mid].second, build(sorted, first, mid),
                    build(sorted, mid + 1, last));
    }
    template<class F> static void visit(const Root &p, const K &lo, const K &hi, F &f) {
        if (!p) return;
        if (lo < p->key) visit(p->left, lo, hi, f);
        if (!(p->key < lo) && !(hi < p->key)) f(p->key, p->value);
        if (p->key < hi) visit(p->right, lo, hi, f);
    }
};

using Rows = Index<Key, aec_row>;
using Target = std::pair<int32_t, Key>;
using BucketKey = std::tuple<int32_t, Key, Key>; // channel, mapped tick, list order
using Buckets = Index<BucketKey, Key>;
struct Group { Rows::Root rows; Key next_order = 0; };
struct Model { std::array<Group, 2> groups{}; Buckets::Root cc; };
struct Pending {
    Model model;
    aec_token token{};
    bool changed = false;
    std::string revision;
};

Target target(const aec_row &r) { return {r.channel, r.tick}; }
BucketKey bucket(const aec_row &r) { return {r.channel, r.tick, r.order}; }
template<class F> void members(const Buckets::Root &root, const Target &at, F f) {
    Buckets::visit(root, {at.first, at.second, 0}, {at.first, at.second, max_key}, f);
}
bool midi(int32_t v) { return v >= 0 && v <= 127; }
bool seconds(double v) { return std::isfinite(v) && v >= 0; }
void valid_row(uint32_t group, const aec_bounds &b, const aec_row &r) {
    need(seconds(r.time) && midi(r.data1) && midi(r.data2) && r.channel >= 0 && r.channel < 16);
    if (group == AEC_NOTES) {
        need(seconds(r.end) && r.end > r.time && r.data2 > 0 && r.end <= b.extent);
        need(!(b.flags & AEC_OPENING) || r.time >= b.opening);
        need(!(b.flags & AEC_CLOSING) || r.time < b.closing);
    } else {
        need(r.time <= b.extent);
    }
}
bool equal_row(uint32_t group, const aec_row &a, const aec_row &b) {
    return a.time == b.time && a.data1 == b.data1 && a.data2 == b.data2 &&
        a.channel == b.channel && (group == AEC_CONTROLS || a.end == b.end);
}

std::atomic<Key> epoch_source{0};
Key new_epoch() {
    Key n = epoch_source.load(std::memory_order_relaxed);
    for (;;) {
        need(n != max_key, AEC_LIMIT);
        if (epoch_source.compare_exchange_weak(n, n + 1, std::memory_order_relaxed)) return n + 1;
    }
}
} // namespace

struct aec_session {
    Model model;
    aec_bounds bounds{};
    std::string revision;
    aec_stamp stamp{};
    Key last_request = 0;
    std::unique_ptr<Pending> pending;
    bool closed = false;
};
struct aec_prepared {
    aec_token token{};
    uint32_t group = AEC_NOTES;
    bool changed = false;
    std::vector<aec_change> changes;
    std::vector<Key> normalized;
};

namespace {
void active(const aec_session *s) { need(s != nullptr); need(!s->closed, AEC_CLOSED); }
bool same_stamp(const aec_stamp &a, const aec_stamp &b) {
    return a.epoch == b.epoch && a.sequence == b.sequence;
}
Pending &pending_for(aec_session *s, const aec_token *t) {
    active(s);
    need(t != nullptr);
    need(s->pending && same_stamp(s->stamp, t->base) &&
         s->pending->token.request == t->request, AEC_STALE);
    return *s->pending;
}

Model seed_model(const aec_seed &seed) {
    Model out;
    std::vector<std::pair<BucketKey, Key>> cc;
    for (const uint32_t g : {AEC_NOTES, AEC_CONTROLS}) {
        const auto *input = g == AEC_NOTES ? seed.notes : seed.controls;
        const size_t count = g == AEC_NOTES ? seed.note_count : seed.control_count;
        array_input(input, count);
        need(count < max_key, AEC_LIMIT);
        std::vector<std::pair<Key, aec_row>> rows;
        rows.reserve(count);
        for (size_t i = 0; i < count; ++i) {
            auto row = input[i];
            row.order = static_cast<Key>(i);
            valid_row(g, seed.bounds, row);
            rows.emplace_back(row.key, row);
            if (g == AEC_CONTROLS && row.data1 == 64) cc.emplace_back(bucket(row), row.key);
        }
        std::sort(rows.begin(), rows.end(), [](const auto &a, const auto &b) { return a.first < b.first; });
        for (size_t i = 1; i < rows.size(); ++i) need(rows[i - 1].first != rows[i].first, AEC_DUPLICATE);
        out.groups[g] = {Rows::build(rows, 0, rows.size()), static_cast<Key>(count)};
    }
    std::sort(cc.begin(), cc.end());
    out.cc = Buckets::build(cc, 0, cc.size());
    return out;
}

struct Edit { std::optional<aec_row> before, after; };
using Overlay = std::map<Key, Edit>;
Edit &touch(Overlay &overlay, const Rows::Root &base, Key key) {
    auto insertion = overlay.try_emplace(key);
    auto &edit = insertion.first->second;
    if (insertion.second) {
        if (const auto *old = Rows::find(base, key)) edit.before = edit.after = *old;
    }
    return edit;
}
bool incoming(const Edit &e) {
    return e.after && e.after->data1 == 64 &&
        (!e.before || e.before->time != e.after->time || e.before->data2 != e.after->data2);
}
struct Rank {
    bool added;
    double time;
    Key order;
    bool operator<(const Rank &other) const {
        return std::tie(added, time, order) < std::tie(other.added, other.time, other.order);
    }
};
struct Unit { Key leader; Rank rank; };

void normalize_cc(const Model &base, Overlay &overlay, Buckets::Root &projected,
                  std::vector<Key> &normalized, const astra::editor::RangeProjection* range) {
    // A guard is a physical edge transition, not an incoming replacement.
    if (range && range->start == range->end) return;
    std::set<Target> sources;
    for (const auto &entry : overlay)
        if (incoming(entry.second) && entry.second.before) sources.insert(target(*entry.second.before));

    // Index only affected source clusters and target buckets. Historical
    // duplicates are not queried or normalized simply because they exist.
    std::map<Key, Unit> clustered;
    for (const auto &source : sources) {
        std::vector<Key> ids;
        std::optional<Target> dest;
        bool intact = true;
        Rank rank{false, 0., 0};
        members(base.cc, source, [&](const BucketKey &, Key id) {
            if (!intact) return;
            const auto it = overlay.find(id);
            if (it == overlay.end() || !incoming(it->second)) { intact = false; return; }
            const auto &e = it->second;
            const auto next = target(*e.after);
            if (dest && *dest != next) { intact = false; return; }
            dest = next;
            ids.push_back(id);
            // These maxima are independent, just as in the disk editor.
            rank.time = std::max(rank.time, e.before->time);
            rank.order = std::max(rank.order, e.before->order);
        });
        if (intact && ids.size() > 1)
            for (const auto id : ids) clustered.emplace(id, Unit{ids.front(), rank});
    }

    std::map<Target, Unit> winners;
    for (const auto &entry : overlay) {
        const auto id = entry.first;
        const auto &e = entry.second;
        if (!incoming(e)) continue;
        const auto c = clustered.find(id);
        const Unit unit = c != clustered.end() ? c->second : Unit{id, e.before ?
            Rank{false, e.before->time, e.before->order} : Rank{true, 0., e.after->order}};
        const auto insertion = winners.emplace(target(*e.after), unit);
        if (!insertion.second && insertion.first->second.rank < unit.rank) insertion.first->second = unit;
    }

    std::vector<std::pair<BucketKey, Key>> losers;
    for (const auto &winner : winners) {
        members(projected, winner.first, [&](const BucketKey &at, Key id) {
            if (range) {
                const auto i = overlay.find(id);
                const auto* row = i == overlay.end() ? Rows::find(base.groups[AEC_CONTROLS].rows,id) :
                    (i->second.after ? &*i->second.after : nullptr);
                need(row != nullptr, AEC_INTERNAL);
                // A MIDI tick can straddle the physical selection edge. Its
                // outside rows aren't eligible to lose a range collision.
                if (row->time < range->start || row->time >= range->end) return;
            }
            const auto c = clustered.find(id);
            const auto leader = c == clustered.end() ? id : c->second.leader;
            if (leader != winner.second.leader) losers.emplace_back(at, id);
        });
    }
    std::sort(losers.begin(), losers.end(), [](const auto &a, const auto &b) {
        return std::get<2>(a.first) < std::get<2>(b.first);
    });
    for (const auto &loser : losers) {
        touch(overlay, base.groups[AEC_CONTROLS].rows, loser.second).after.reset();
        projected = Buckets::erase(projected, loser.first);
        normalized.push_back(loser.second);
    }
}

void prepare_group(const aec_session &s, const aec_command &c, Pending &p, aec_prepared &out,
                   const astra::editor::RangeProjection* range) {
    const auto &original = s.model.groups[c.group];
    auto &candidate = p.model.groups[c.group];
    Overlay edits;
    const bool notes = c.group == AEC_NOTES;
    const auto allowed = notes ? uint32_t(AEC_TIME | AEC_END | AEC_DATA1 | AEC_DATA2)
                               : uint32_t(AEC_TIME | AEC_DATA2);
    for (size_t i = 0; i < c.delete_count; ++i) {
        auto &edit = touch(edits, original.rows, c.deletes[i]);
        need(edit.after.has_value(), AEC_MISSING);
        need(notes || range || edit.after->data1 == 64);
        edit.after.reset();
    }
    for (size_t i = 0; i < c.update_count; ++i) {
        const auto &u = c.updates[i];
        auto &edit = touch(edits, original.rows, u.row.key);
        need(edit.after.has_value(), AEC_MISSING);
        need(notes || range || edit.after->data1 == 64);
        need((u.mask & ~allowed) == 0);
        auto &r = *edit.after;
        if (u.mask & AEC_TIME) { r.time = u.row.time; if (!notes) r.tick = u.row.tick; }
        if (u.mask & AEC_END) r.end = u.row.end;
        if (u.mask & AEC_DATA1) r.data1 = u.row.data1;
        if (u.mask & AEC_DATA2) r.data2 = u.row.data2;
    }
    for (size_t i = 0; i < c.add_count; ++i) {
        auto r = c.adds[i];
        need(edits.find(r.key) == edits.end() && !Rows::find(original.rows, r.key), AEC_DUPLICATE);
        need(candidate.next_order != max_key, AEC_LIMIT);
        need(notes || range || r.data1 == 64);
        r.order = candidate.next_order++;
        edits.emplace(r.key, Edit{std::nullopt, r});
    }

    // Validate the final assignments, not intermediate ones, and do it before
    // normalization can displace an invalid row. Only the private overlay has
    // been edited; even the candidate index roots still refer to the base.
    for (const auto &entry : edits) {
        const auto &e = entry.second;
        if (!e.after) continue;
        valid_row(c.group, s.bounds, *e.after);
        if (!notes && e.before && e.before->time == e.after->time)
            need(e.before->tick == e.after->tick); // host mapping contract
    }
    if (!notes) {
        for (const auto &entry : edits) {
            const auto &e = entry.second;
            if (e.before && e.after && bucket(*e.before) == bucket(*e.after)) continue;
            if (e.before) p.model.cc = Buckets::erase(p.model.cc, bucket(*e.before));
            if (e.after) p.model.cc = Buckets::put(p.model.cc, bucket(*e.after), entry.first);
        }
        normalize_cc(s.model, edits, p.model.cc, out.normalized, range);
    }
    const auto changed = [&](const Edit &e) {
        return e.before.has_value() != e.after.has_value() ||
            (e.before && e.after && !equal_row(c.group, *e.before, *e.after));
    };
    out.changed = p.changed = std::any_of(edits.begin(), edits.end(), [&](const auto &x) { return changed(x.second); });
    if (!out.changed) { p.model = s.model; return; }
    out.changes.reserve(edits.size());
    for (const auto &entry : edits) {
        const auto &e = entry.second;
        if (!e.before && !e.after) continue;
        aec_change change{};
        change.key = entry.first;
        if (e.before) { change.flags |= AEC_BEFORE; change.before = *e.before; }
        if (e.after) {
            change.flags |= AEC_AFTER;
            if (changed(e)) change.flags |= AEC_MANUAL;
            change.after = *e.after;
            candidate.rows = Rows::put(candidate.rows, entry.first, *e.after);
        } else {
            candidate.rows = Rows::erase(candidate.rows, entry.first);
        }
        out.changes.push_back(change);
    }
}
} // namespace

extern "C" {
uint32_t AEC_CALL aec_version(void) noexcept { return AEC_ABI; }

int32_t AEC_CALL aec_open(const aec_seed *seed, aec_session **out) noexcept {
    if (out) *out = nullptr;
    return boundary([&] {
        need(out != nullptr);
        envelope(seed);
        revision_input(seed->revision);
        const auto &b = seed->bounds;
        need((b.flags & ~uint32_t(AEC_OPENING | AEC_CLOSING)) == 0 && seconds(b.extent));
        need(!(b.flags & AEC_OPENING) || !std::isnan(b.opening));
        need(!(b.flags & AEC_CLOSING) || !std::isnan(b.closing));
        auto s = std::make_unique<aec_session>();
        s->bounds = b;
        s->model = seed_model(*seed);
        s->revision.assign(seed->revision.data, seed->revision.count);
        s->stamp = {new_epoch(), 0};
        *out = s.release();
    });
}
int32_t AEC_CALL aec_fork(const aec_session *s,aec_session **out) noexcept {
    if (out) *out=nullptr;
    return boundary([&] {
        active(s); need(out!=nullptr); need(!s->pending,AEC_BUSY);
        auto next=std::make_unique<aec_session>();
        next->model=s->model;
        next->bounds=s->bounds;
        next->revision=s->revision;
        next->stamp={new_epoch(),0};
        *out=next.release();
    });
}

int32_t AEC_CALL aec_get_stamp(const aec_session *s, aec_stamp *out) noexcept {
    return boundary([&] { active(s); need(out != nullptr); *out = s->stamp; });
}
} // extern C

namespace {
int32_t prepare_projection(aec_session *s, const aec_command *c, aec_prepared **out,
                           const astra::editor::RangeProjection* range) noexcept {
    if (out) *out = nullptr;
    return boundary([&] {
        active(s);
        envelope(c);
        need(out != nullptr && c->group <= AEC_CONTROLS && c->token.request != 0);
        revision_input(c->revision);
        need(same_stamp(s->stamp, c->token.base) && s->revision.size() == c->revision.count &&
             std::memcmp(s->revision.data(), c->revision.data, c->revision.count) == 0, AEC_STALE);
        need(c->token.request > s->last_request, AEC_DUPLICATE);
        need(!s->pending, AEC_BUSY);
        need(s->stamp.sequence != max_key, AEC_LIMIT);
        array_input(c->deletes, c->delete_count);
        array_input(c->updates, c->update_count);
        array_input(c->adds, c->add_count);
        auto next = std::make_unique<Pending>();
        auto result = std::make_unique<aec_prepared>();
        next->model = s->model;
        result->token = next->token = c->token;
        result->group = c->group;
        need(!range || (c->group == AEC_CONTROLS && seconds(range->start) &&
                        seconds(range->end) && range->end >= range->start));
        prepare_group(*s, *c, *next, *result, range);
        s->pending = std::move(next);
        s->last_request = c->token.request;
        *out = result.release();
    });
}
} // namespace

int32_t astra::editor::prepare_range_controls(aec_session* s, const aec_command* c,
                                              const RangeProjection& range, aec_prepared** out) noexcept {
    return prepare_projection(s,c,out,&range);
}

extern "C" {
int32_t AEC_CALL aec_prepare(aec_session *s, const aec_command *c, aec_prepared **out) noexcept {
    return prepare_projection(s,c,out,nullptr);
}

int32_t AEC_CALL aec_get_result(const aec_prepared *p, aec_result *out) noexcept {
    return boundary([&] {
        need(p != nullptr);
        envelope(out);
        out->token = p->token;
        out->group = p->group;
        out->changed = p->changed ? 1u : 0u;
        out->changes = p->changes.data();
        out->change_count = p->changes.size();
        out->normalized = p->normalized.data();
        out->normalized_count = p->normalized.size();
    });
}
int32_t AEC_CALL aec_bind_revision(aec_session *s, const aec_token *t, aec_bytes revision) noexcept {
    return boundary([&] {
        auto &p = pending_for(s, t);
        need(p.changed);
        need(p.revision.empty(), AEC_DUPLICATE);
        revision_input(revision);
        p.revision.assign(revision.data, revision.count);
    });
}
int32_t AEC_CALL aec_accept(aec_session *s, const aec_token *t) noexcept {
    return boundary([&] {
        auto &p = pending_for(s, t);
        need(!p.changed || !p.revision.empty());
        if (p.changed) {
            s->model = std::move(p.model);
            s->revision.swap(p.revision);
        }
        ++s->stamp.sequence;
        s->pending.reset();
    });
}
int32_t AEC_CALL aec_discard(aec_session *s, const aec_token *t) noexcept {
    return boundary([&] { pending_for(s, t); s->pending.reset(); });
}
int32_t AEC_CALL aec_close(aec_session *s) noexcept {
    return boundary([&] {
        need(s != nullptr);
        s->closed = true;
        s->pending.reset();
        s->model = Model{};
        s->revision.clear();
    });
}
void AEC_CALL aec_free_session(aec_session *s) noexcept { delete s; }
void AEC_CALL aec_free_prepared(aec_prepared *p) noexcept { delete p; }
} // extern "C"
