// SPDX-License-Identifier: GPL-3.0-only
// Astra MuseScore Basic: MuseScore Studio 4.6.5's FluidSynth instrument
// (src/framework/audio/worker/internal/synthesizers/fluidsynth) as a VST3
// instrument for the Astra host, playing the bundled MS Basic.sf3.
#pragma once
#include "pluginterfaces/base/funknown.h"
#include "pluginterfaces/vst/vsttypes.h"
#include "pluginterfaces/vst/ivstmidicontrollers.h"

namespace astra_msbasic {

static const Steinberg::FUID kProcessorUID(0x6A2F4C11, 0x8E3B4D27, 0x9C15A0E2, 0x31B7D845);
static const Steinberg::FUID kControllerUID(0x0D94B6F3, 0x52A14E8C, 0xB7E02C69, 0x4F18A3D0);

// One hidden parameter per (MIDI channel, controller) the host may map through
// IMidiMapping. CtrlNumber 128 = pitch bend, 129 = channel aftertouch.
constexpr int kMappedControllers[] = {1, 2, 4, 7, 10, 11, 64, 66, 67, 68, 91, 93, 120, 121, 123,
                                      Steinberg::Vst::kPitchBend, Steinberg::Vst::kAfterTouch};
constexpr int kMappedCount = sizeof(kMappedControllers) / sizeof(kMappedControllers[0]);
constexpr Steinberg::Vst::ParamID paramFor(int channel, int cc) { return 0x10000u | (channel << 8) | cc; }
constexpr bool isMidiParam(Steinberg::Vst::ParamID id) { return (id & 0xFFFF0000u) == 0x10000u; }
constexpr int channelOf(Steinberg::Vst::ParamID id) { return (id >> 8) & 0xFF; }
constexpr int controllerOf(Steinberg::Vst::ParamID id) { return id & 0xFF; }

} // namespace astra_msbasic
