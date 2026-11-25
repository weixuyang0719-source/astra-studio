#pragma once
#include "astra_vst_host.h"
#include "pluginterfaces/vst/ivstparameterchanges.h"
#include <array>
#include <cstdint>

namespace astra::vst_params {
using namespace Steinberg;
using namespace Steinberg::Vst;
inline constexpr unsigned kEditSlots = 1024;

// Allocate once with Host. A queue can use the entire point budget; unused
// queues don't each reserve a dense array of points. Page indices retain O(1)
// access by point index, including while a plugin is writing output parameters.
struct Changes final : IParameterChanges {
    static constexpr unsigned kQueues = 16 * 130 + kEditSlots - 1;
    static constexpr unsigned kPoints = AV_RT_MIDI_LIMIT + kEditSlots - 1;
    static constexpr unsigned kPage = 64, kHash = 8192;
    static constexpr unsigned kPagesPerQueue = (kPoints + kPage - 1) / kPage;
    static constexpr unsigned kPages = kQueues + kPagesPerQueue;
    static_assert(kPages < UINT16_MAX && kQueues < kHash / 2);

    struct Point { int32 offset; ParamValue value; };
    struct Queue final : IParamValueQueue {
        Changes* owner = nullptr;
        ParamID id = 0;
        unsigned count = 0, hash_slot = 0;
        std::array<uint16_t, kPagesPerQueue> pages;

        Point& point(unsigned i) noexcept { return owner->points[pages[i/kPage]*kPage + i%kPage]; }
        ParamID PLUGIN_API getParameterId() override { return id; }
        int32 PLUGIN_API getPointCount() override { return static_cast<int32>(count); }
        tresult PLUGIN_API getPoint(int32 i, int32& off, ParamValue& val) override {
            if (i < 0 || static_cast<unsigned>(i) >= count) return kInvalidArgument;
            auto& p = point(static_cast<unsigned>(i)); off = p.offset; val = p.value;
            return kResultOk;
        }
        tresult PLUGIN_API addPoint(int32 off, ParamValue val, int32& i) override {
            if (owner->used_points == kPoints) { owner->overflow = true; return kOutOfMemory; }
            // At most one partly filled page per queue, plus kPoints/kPage full pages.
            if (!(count % kPage)) pages[count/kPage] = static_cast<uint16_t>(owner->used_pages++);
            i = static_cast<int32>(count); point(count++) = {off, val}; ++owner->used_points;
            return kResultOk;
        }
        tresult PLUGIN_API queryInterface(const TUID id_, void** p) override {
            if (FUnknownPrivate::iidEqual(id_, IParamValueQueue::iid) || FUnknownPrivate::iidEqual(id_, FUnknown::iid)) {
                *p = static_cast<IParamValueQueue*>(this); return kResultOk;
            }
            *p = nullptr; return kNoInterface;
        }
        uint32 PLUGIN_API addRef() override { return 1000; }
        uint32 PLUGIN_API release() override { return 1000; }
    };

    std::array<Queue, kQueues> queues;
    std::array<Point, kPages*kPage> points;
    std::array<uint16_t, kHash> lookup{};
    unsigned count = 0, used_points = 0, used_pages = 0;
    bool overflow = false;

    void clear() noexcept {
        for (unsigned i = 0; i < count; ++i) lookup[queues[i].hash_slot] = 0;
        count = used_points = used_pages = 0; overflow = false;
    }
    int32 PLUGIN_API getParameterCount() override { return static_cast<int32>(count); }
    IParamValueQueue* PLUGIN_API getParameterData(int32 i) override {
        return i >= 0 && static_cast<unsigned>(i) < count ? &queues[i] : nullptr;
    }
    IParamValueQueue* PLUGIN_API addParameterData(const ParamID& id, int32& i) override {
        uint32_t hash = id;
        hash ^= hash >> 16; hash *= 0x7feb352du; hash ^= hash >> 15;
        unsigned slot = hash & (kHash-1);
        for (; lookup[slot]; slot = (slot+1)&(kHash-1)) {
            i = lookup[slot]-1;
            if (queues[i].id == id) return &queues[i];
        }
        if (count == kQueues) { overflow = true; return nullptr; }
        auto& q = queues[count]; q.owner = this; q.id = id; q.count = 0; q.hash_slot = slot;
        i = static_cast<int32>(count++); lookup[slot] = static_cast<uint16_t>(count);
        return &q;
    }
    bool add(ParamID id, int32 off, ParamValue val) {
        int32 i; auto* q = addParameterData(id, i);
        return q && q->addPoint(off, val, i) == kResultOk;
    }
    bool setInitial(ParamID id, ParamValue val) {
        int32 i; auto* q = static_cast<Queue*>(addParameterData(id, i));
        if (!q) return false;
        if (q->count) { q->point(0).value = val; return true; }
        return q->addPoint(0, val, i) == kResultOk;
    }
    tresult PLUGIN_API queryInterface(const TUID id_, void** p) override {
        if (FUnknownPrivate::iidEqual(id_, IParameterChanges::iid) || FUnknownPrivate::iidEqual(id_, FUnknown::iid)) {
            *p = static_cast<IParameterChanges*>(this); return kResultOk;
        }
        *p = nullptr; return kNoInterface;
    }
    uint32 PLUGIN_API addRef() override { return 1000; }
    uint32 PLUGIN_API release() override { return 1000; }
};
}
