#include "param_changes.hpp"
#include <cstdio>
#include <cstdlib>
#include <memory>

#define CHECK(x) do { if (!(x)) { std::fprintf(stderr, "%d: %s\n", __LINE__, #x); std::abort(); } } while (false)

int main() {
    using namespace astra::vst_params;
    auto c = std::make_unique<Changes>();
    for (unsigned cycle = 0; cycle != 3; ++cycle) {
        for (unsigned i = 0; i < kEditSlots-1; ++i) CHECK(c->setInitial(i, .2));
        CHECK(c->setInitial(0, .25)); // a second UI write, no extra point
        // First exhaust the full set of distinct mapped IDs, without relying
        // on any particular plugin's IMidiMapping implementation.
        for (unsigned i = 0; i < 16*130; ++i) CHECK(c->add(kEditSlots+i, 0, i/2080.));
        for (unsigned i = 0; i < AV_RT_MIDI_LIMIT-16*130; ++i)
            CHECK(c->add(0, static_cast<int32>(i/2), double(i%128)/127.));
        CHECK(c->getParameterCount() == Changes::kQueues);
        auto* q = c->getParameterData(0);
        CHECK(q && q->getPointCount() == 1+AV_RT_MIDI_LIMIT-16*130);
        int32 off = -1; ParamValue val = -1.;
        CHECK(q->getPoint(0, off, val) == kResultOk && off == 0 && val == .25);
        for (int32 i = 1; i < q->getPointCount(); ++i)
            CHECK(q->getPoint(i, off, val) == kResultOk && off == (i-1)/2 && val == double((i-1)%128)/127.);
        CHECK(!c->overflow && !c->add(0, 0, 1.) && c->overflow);
        c->clear();
        CHECK(!c->overflow && !c->getParameterCount());
        for (unsigned i = 0; i < AV_RT_MIDI_LIMIT; ++i) CHECK(c->add(77, 0, i%2));
        CHECK(c->getParameterData(0)->getPointCount() == AV_RT_MIDI_LIMIT);
        c->clear();
    }
}
