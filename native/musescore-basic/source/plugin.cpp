// SPDX-License-Identifier: GPL-3.0-only
// VST3 shell around the MuseScore Basic synth for the Astra host.
#include "ids.h"
#include "msbasic_synth.h"
#include "version.h"

#include "public.sdk/source/main/pluginfactory.h"
#include "public.sdk/source/vst/vstaudioeffect.h"
#include "public.sdk/source/vst/vsteditcontroller.h"
#include "pluginterfaces/base/ibstream.h"
#include "pluginterfaces/vst/ivstevents.h"
#include "pluginterfaces/vst/ivstparameterchanges.h"
#include "pluginterfaces/vst/ivstprocesscontext.h"

#include <windows.h>
#include <algorithm>
#include <cmath>
#include <string>
#include <vector>

using namespace Steinberg;
using namespace Steinberg::Vst;

namespace astra_msbasic {

static std::wstring moduleDirectory()
{
    HMODULE self = nullptr;
    GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS | GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,
                       reinterpret_cast<LPCWSTR>(&moduleDirectory), &self);
    wchar_t path[MAX_PATH * 4] = {};
    DWORD n = GetModuleFileNameW(self, path, static_cast<DWORD>(std::size(path)));
    std::wstring s(path, n);
    return s.substr(0, s.find_last_of(L"\\/"));
}

// <bundle>.vst3/Contents/x86_64-win/<module>  ->  <bundle>.vst3/Contents/Resources/MS Basic.sf3
static std::string soundFontPath()
{
    std::wstring dir = moduleDirectory();
    std::wstring contents = dir.substr(0, dir.find_last_of(L"\\/"));
    std::wstring sf = contents + L"\\Resources\\MS Basic.sf3";
    int bytes = WideCharToMultiByte(CP_UTF8, 0, sf.c_str(), -1, nullptr, 0, nullptr, nullptr);
    std::string out(bytes > 0 ? bytes - 1 : 0, '\0');
    WideCharToMultiByte(CP_UTF8, 0, sf.c_str(), -1, out.data(), bytes, nullptr, nullptr);
    return out;
}

class Processor : public AudioEffect
{
public:
    Processor() { setControllerClass(kControllerUID); }
    static FUnknown* create(void*) { return static_cast<IAudioProcessor*>(new Processor); }

    tresult PLUGIN_API initialize(FUnknown* context) override
    {
        tresult r = AudioEffect::initialize(context);
        if (r != kResultOk) return r;
        addEventInput(STR16("MIDI In"), 16);
        addAudioOutput(STR16("Stereo Out"), SpeakerArr::kStereo);
        return kResultOk;
    }
    tresult PLUGIN_API setBusArrangements(SpeakerArrangement* in, int32 numIns, SpeakerArrangement* out, int32 numOuts) override
    {
        if (numIns == 0 && numOuts == 1 && out[0] == SpeakerArr::kStereo)
            return AudioEffect::setBusArrangements(in, numIns, out, numOuts);
        return kResultFalse;
    }
    tresult PLUGIN_API canProcessSampleSize(int32 size) override { return size == kSample32 ? kResultTrue : kResultFalse; }
    tresult PLUGIN_API setupProcessing(ProcessSetup& setup) override
    {
        m_rate = setup.sampleRate;
        return AudioEffect::setupProcessing(setup);
    }
    tresult PLUGIN_API setActive(TBool state) override
    {
        if (state) {
            std::string error;
            if (!m_synth.init(m_rate, soundFontPath(), error)) return kResultFalse;
        } else {
            m_synth.release();
        }
        return AudioEffect::setActive(state);
    }
    tresult PLUGIN_API setState(IBStream*) override { return kResultOk; }
    tresult PLUGIN_API getState(IBStream* state) override
    {
        int32 version = 1;
        return state ? state->write(&version, sizeof(version), nullptr) : kResultFalse;
    }

    tresult PLUGIN_API process(ProcessData& data) override
    {
        m_pending.clear();
        if (IParameterChanges* changes = data.inputParameterChanges) {
            for (int32 i = 0, n = changes->getParameterCount(); i < n; ++i) {
                IParamValueQueue* q = changes->getParameterData(i);
                if (!q || !isMidiParam(q->getParameterId())) continue;
                for (int32 p = 0, pn = q->getPointCount(); p < pn; ++p) {
                    int32 offset = 0; ParamValue v = 0;
                    if (q->getPoint(p, offset, v) == kResultOk)
                        m_pending.push_back({offset, 1, q->getParameterId(), v, {}});
                }
            }
        }
        if (IEventList* events = data.inputEvents) {
            for (int32 i = 0, n = events->getEventCount(); i < n; ++i) {
                Event e{};
                if (events->getEvent(i, e) == kResultOk && (e.type == Event::kNoteOnEvent || e.type == Event::kNoteOffEvent))
                    m_pending.push_back({e.sampleOffset, 0, 0, 0, e});
            }
        }
        std::stable_sort(m_pending.begin(), m_pending.end(), [](const Item& a, const Item& b) { return a.offset < b.offset; });

        if (data.numOutputs < 1 || data.outputs[0].numChannels < 2 || data.numSamples <= 0) {
            for (auto& item : m_pending) apply(item);
            return kResultOk;
        }
        float* left = data.outputs[0].channelBuffers32[0];
        float* right = data.outputs[0].channelBuffers32[1];
        std::fill_n(left, data.numSamples, 0.f);
        std::fill_n(right, data.numSamples, 0.f);
        int32 at = 0;
        for (auto& item : m_pending) {
            int32 offset = std::clamp(item.offset, at, data.numSamples);
            if (offset > at) { m_synth.render(left + at, right + at, offset - at); at = offset; }
            apply(item);
        }
        if (at < data.numSamples) m_synth.render(left + at, right + at, data.numSamples - at);
        data.outputs[0].silenceFlags = 0;
        return kResultOk;
    }

private:
    struct Item { int32 offset; int kind; ParamID id; ParamValue value; Event event; };
    void apply(const Item& item)
    {
        if (item.kind == 0) {
            const Event& e = item.event;
            if (e.type == Event::kNoteOnEvent) {
                int velocity = std::clamp(static_cast<int>(std::lround(e.noteOn.velocity * 127.f)), 0, 127);
                if (velocity == 0) m_synth.noteOff(e.noteOn.channel & 15, e.noteOn.pitch & 127);
                else m_synth.noteOn(e.noteOn.channel & 15, e.noteOn.pitch & 127, velocity);
            } else {
                m_synth.noteOff(e.noteOff.channel & 15, e.noteOff.pitch & 127);
            }
            return;
        }
        int channel = channelOf(item.id) & 15, cc = controllerOf(item.id);
        double v = std::clamp(item.value, 0.0, 1.0);
        if (cc == kPitchBend) m_synth.pitchBend(channel, static_cast<int>(std::lround(v * 16383.0)));
        else if (cc == kAfterTouch) m_synth.channelPressure(channel, static_cast<int>(std::lround(v * 127.0)));
        else if (cc < 128) m_synth.controller(channel, cc, static_cast<int>(std::lround(v * 127.0)));
    }

    Synth m_synth;
    double m_rate = 48000.0;
    std::vector<Item> m_pending;
};

class Controller : public EditController, public IMidiMapping
{
public:
    static FUnknown* create(void*) { return static_cast<IEditController*>(new Controller); }

    tresult PLUGIN_API initialize(FUnknown* context) override
    {
        tresult r = EditController::initialize(context);
        if (r != kResultOk) return r;
        for (int channel = 0; channel < 16; ++channel) {
            for (int cc : kMappedControllers) {
                std::u16string title = u"MIDI " + std::u16string(1, u'0' + channel / 10) +
                                       std::u16string(1, u'0' + channel % 10) + u" CC " +
                                       std::u16string(1, u'0' + cc / 100) + std::u16string(1, u'0' + (cc / 10) % 10) +
                                       std::u16string(1, u'0' + cc % 10);
                double def = cc == kPitchBend ? 0.5 : cc == 7 ? 100.0 / 127.0 : cc == 11 ? 64.0 / 127.0 : 0.0;
                parameters.addParameter(reinterpret_cast<const TChar*>(title.c_str()), nullptr,
                                        cc == kPitchBend ? 0 : 127, def, ParameterInfo::kIsHidden, paramFor(channel, cc));
            }
        }
        return kResultOk;
    }
    tresult PLUGIN_API setComponentState(IBStream*) override { return kResultOk; }

    tresult PLUGIN_API getMidiControllerAssignment(int32 busIndex, int16 channel, CtrlNumber cc, ParamID& id) override
    {
        if (busIndex != 0 || channel < 0 || channel > 15) return kResultFalse;
        for (int mapped : kMappedControllers) {
            if (mapped == cc) { id = paramFor(channel, cc); return kResultTrue; }
        }
        return kResultFalse;
    }

    OBJ_METHODS(Controller, EditController)
    DEFINE_INTERFACES
        DEF_INTERFACE(IMidiMapping)
    END_DEFINE_INTERFACES(EditController)
    REFCOUNT_METHODS(EditController)
};

} // namespace astra_msbasic

BEGIN_FACTORY_DEF(MSBASIC_VENDOR, MSBASIC_URL, MSBASIC_EMAIL)
    DEF_CLASS2(INLINE_UID_FROM_FUID(astra_msbasic::kProcessorUID), PClassInfo::kManyInstances, kVstAudioEffectClass,
               MSBASIC_NAME, Vst::kDistributable, "Instrument|Piano", MSBASIC_VERSION, kVstVersionString,
               astra_msbasic::Processor::create)
    DEF_CLASS2(INLINE_UID_FROM_FUID(astra_msbasic::kControllerUID), PClassInfo::kManyInstances, kVstComponentControllerClass,
               MSBASIC_NAME " Controller", 0, "", MSBASIC_VERSION, kVstVersionString,
               astra_msbasic::Controller::create)
END_FACTORY
