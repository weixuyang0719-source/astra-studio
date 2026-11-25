#include "astra_editor_core.h"

#include <cstdlib>
#include <cstring>
#include <iostream>
#include <memory>

#define CHECK(c) do { if (!(c)) { std::cerr << __LINE__ << ": " << #c << '\n'; std::exit(1); } } while (false)

namespace {
using Session = std::unique_ptr<aec_session, decltype(&aec_free_session)>;
using Prepared = std::unique_ptr<aec_prepared, decltype(&aec_free_prepared)>;

aec_bytes bytes(const char *s) { return {s, std::strlen(s)}; }
aec_row row(uint64_t id, double time, double end, int32_t x, int32_t y,
            uint64_t tick = 0, int32_t ch = 0) {
    return {id, 0, tick, time, end, x, y, ch};
}
Session open() {
    // Same ID in different groups is allowed. CC rows are deliberately not
    // sorted by time: list ordering and original time are distinct inputs.
    const aec_row notes[] = {row(30, 1., 2., 60, 90), row(3, 2., 4., 60, 65)};
    const aec_row cc[] = {row(30, 2.01, 0., 64, 60, 10), row(7, 2., 0., 64, 70, 10),
        row(9, 4., 0., 64, 0, 20), row(5, 4., 0., 7, 72, 0), row(8, 4., 0., 64, 44, 20, 1)};
    const aec_seed seed{sizeof(aec_seed), AEC_ABI, bytes("A"),
        {AEC_OPENING | AEC_CLOSING, 1., 8., 12.}, notes, 2, cc, 5};
    aec_session *s = nullptr;
    CHECK(aec_open(&seed, &s) == AEC_OK && s);
    return Session(s, &aec_free_session);
}
aec_stamp stamp(const Session &s) {
    aec_stamp out{};
    CHECK(aec_get_stamp(s.get(), &out) == AEC_OK);
    return out;
}
aec_command command(const Session &s, uint64_t request, uint32_t group, const char *revision = "A") {
    aec_command c{};
    c.size = sizeof(c); c.abi = AEC_ABI;
    c.revision = bytes(revision); c.token = {stamp(s), request}; c.group = group;
    return c;
}
Prepared prepare(const Session &s, const aec_command &c, int32_t status = AEC_OK) {
    aec_prepared *p = nullptr;
    CHECK(aec_prepare(s.get(), &c, &p) == status);
    CHECK((p != nullptr) == (status == AEC_OK));
    return Prepared(p, &aec_free_prepared);
}
aec_result view(const Prepared &p) {
    aec_result v{}; v.size = sizeof(v); v.abi = AEC_ABI;
    CHECK(aec_get_result(p.get(), &v) == AEC_OK);
    return v;
}
const aec_change &change(const aec_result &v, uint64_t key) {
    for (size_t i = 0; i < v.change_count; ++i) if (v.changes[i].key == key) return v.changes[i];
    CHECK(false);
    std::abort();
}

void notes_and_tokens() {
    auto s = open();
    const auto initial = stamp(s);
    auto c = command(s, 1, AEC_NOTES);
    const uint64_t duplicate[] = {3, 3};
    c.deletes = duplicate; c.delete_count = 2;
    prepare(s, c, AEC_MISSING);
    CHECK(stamp(s).sequence == initial.sequence);
    c.deletes = nullptr; c.delete_count = 0;
    aec_update updates[] = {{row(3, 8., 0., 0, 0), AEC_TIME},
                            {row(3, 2., 0., 0, 0), AEC_TIME}};
    c.updates = updates; c.update_count = 1;
    prepare(s, c, AEC_INVALID); // final onset is invalid
    c.update_count = 2;
    auto equal = prepare(s, c); // intermediate assignment is not a final edit
    const auto ev = view(equal);
    CHECK(ev.changed == 0 && ev.change_count == 0);
    prepare(s, c, AEC_DUPLICATE);
    c.token.request = 2;
    prepare(s, c, AEC_BUSY);
    CHECK(aec_accept(s.get(), &ev.token) == AEC_OK);
    CHECK(aec_accept(s.get(), &ev.token) == AEC_STALE);
    prepare(s, c, AEC_STALE);

    c = command(s, 2, AEC_NOTES, "wrong revision");
    prepare(s, c, AEC_STALE);
    c.revision = bytes("A");
    const aec_row added = row(31, 7., 10., 60, 80); // tail past score end is legal
    c.adds = &added; c.add_count = 1;
    auto p = prepare(s, c);
    const auto pv = view(p);
    CHECK(pv.changed && pv.change_count == 1 && change(pv, 31).after.order == 2);
    CHECK(stamp(s).sequence == 1);
    CHECK(aec_accept(s.get(), &pv.token) == AEC_INVALID); // no staged revision
    CHECK(aec_bind_revision(s.get(), &pv.token, bytes("B")) == AEC_OK);
    CHECK(aec_bind_revision(s.get(), &pv.token, bytes("C")) == AEC_DUPLICATE);
    CHECK(aec_accept(s.get(), &pv.token) == AEC_OK);
    c = command(s, 2, AEC_NOTES, "B");
    prepare(s, c, AEC_DUPLICATE);
    c = command(s, 3, AEC_NOTES, "B");
    c.deletes = &added.key; c.delete_count = 1;
    auto removal = prepare(s, c);
    CHECK(change(view(removal), 31).before.end == 10.);
    CHECK(aec_discard(s.get(), &c.token) == AEC_OK);
    CHECK(aec_discard(s.get(), &c.token) == AEC_STALE);
    CHECK(stamp(s).sequence == 2);

    // Reloading the original disk revision (as on undo) gets another epoch.
    auto undone = open();
    c = command(undone, 1, AEC_NOTES);
    CHECK(c.token.base.epoch != initial.epoch);
    c.token.base = initial;
    prepare(undone, c, AEC_STALE);
}

void controllers_and_lifetime() {
    auto s = open();
    auto c = command(s, 1, AEC_CONTROLS);
    const aec_update move[] = {{row(30, 4.01, 0., 0, 0, 20), AEC_TIME},
                               {row(7, 4., 0., 0, 0, 20), AEC_TIME}};
    c.updates = move; c.update_count = 2;
    auto p = prepare(s, c);
    const auto v = view(p);
    CHECK(v.change_count == 3 && v.normalized_count == 1 && v.normalized[0] == 9);
    const auto &displaced = change(v, 9);
    CHECK(displaced.flags == AEC_BEFORE && displaced.before.order == 2 && displaced.before.data2 == 0);
    CHECK(change(v, 7).before.time == 2. && change(v, 7).after.order == 1);
    CHECK(aec_discard(s.get(), &v.token) == AEC_OK);

    aec_row adds[] = {row(100, 4., 0., 64, 128, 20), row(101, 4., 0., 64, 127, 20)};
    c.token.request = 2; c.adds = adds; c.add_count = 2;
    prepare(s, c, AEC_INVALID); // invalid loser is still invalid
    adds[0].data2 = 1;
    auto last = prepare(s, c);
    const auto lv = view(last);
    CHECK(lv.normalized_count == 4);
    CHECK(lv.normalized[0] == 30 && lv.normalized[1] == 7 && lv.normalized[2] == 9 && lv.normalized[3] == 100);
    CHECK(lv.change_count == 4 && change(lv, 101).after.data2 == 127);
    CHECK(change(lv, 30).before.time == 2.01); // discarded view never published
    CHECK(aec_close(s.get()) == AEC_OK && aec_close(s.get()) == AEC_OK);
    CHECK(aec_accept(s.get(), &lv.token) == AEC_CLOSED);
    prepare(s, c, AEC_CLOSED);
    s.reset();
    CHECK(view(last).normalized[3] == 100 && v.normalized[0] == 9);
}
} // namespace

int main() {
    CHECK(aec_version() == AEC_ABI);
    notes_and_tokens();
    controllers_and_lifetime();
}
