// SPDX-License-Identifier: GPL-3.0-only
#pragma once
#include <string>

struct _fluid_synth_t;
struct _fluid_hashtable_t;

namespace astra_msbasic {

// The synthesis half of MuseScore Studio 4.6.5's muse::audio::synth::FluidSynth,
// with the same Fluid settings, sound-font loader and per-channel setup. The
// event side (MuseScore's FluidSequencer) is replaced by the host's MIDI stream,
// which the Astra score worker renders from MuseScore's own playback model.
class Synth {
public:
    ~Synth();
    bool init(double sampleRate, const std::string& soundFont, std::string& error);
    void release();
    bool ready() const { return m_synth != nullptr; }

    void noteOn(int channel, int key, int velocity);
    void noteOff(int channel, int key);
    void controller(int channel, int cc, int value);
    void pitchBend(int channel, int value14);
    void channelPressure(int channel, int value);
    void flushSound();
    void render(float* left, float* right, int frames);

private:
    void setupChannel(int channel);
    struct _fluid_synth_t* m_synth = nullptr;
    struct _fluid_hashtable_t* m_settings = nullptr;
};

} // namespace astra_msbasic
