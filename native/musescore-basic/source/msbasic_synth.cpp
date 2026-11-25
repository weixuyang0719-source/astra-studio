// SPDX-License-Identifier: GPL-3.0-only
// Ported from MuseScore Studio 4.6.5 (653f38d8e):
//   src/framework/audio/worker/internal/synthesizers/fluidsynth/fluidsynth.cpp
//   src/framework/audio/worker/internal/synthesizers/fluidsynth/sfcachedloader.h
//   src/framework/audio/worker/internal/synthesizers/fluidsynth/fluidsequencer.cpp (expression level)
// Values and call order follow MuseScore; only MuseScore's module/ioc wiring is removed.
#include "msbasic_synth.h"

#include <cstdio>
#include <map>
#include <mutex>

#include "fluidsynth.h"
extern "C" {
#include "sfloader/fluid_sfont.h"
#include "sfloader/fluid_defsfont.h"
}

namespace astra_msbasic {
namespace {

// fluidsynth.cpp
constexpr double FLUID_GLOBAL_VOLUME_GAIN = 4.8;
constexpr int DEFAULT_MIDI_VOLUME = 100;
constexpr int MIN_NOTE_LENGTH = 10;            // msecs_t
constexpr int FLUID_AUDIO_CHANNELS_PAIR = 1;
constexpr int EXPRESSION_CONTROLLER = 11;      // muse::midi::EXPRESSION_CONTROLLER
// FluidSequencer::naturalExpressionLevel(): expressionLevel(DynamicType::Natural)
// = RealRound(16 + ((50% - 32.5%) / 5% - 0.5) * 16) = 64.
constexpr int NATURAL_EXPRESSION_LEVEL = 64;
constexpr int MIDI_CHANNELS = 16;

// sfcachedloader.h: one parsed sound font per process, shared by every synth.
struct SoundFontData {
    fluid_sfont_t* soundFontPtr = nullptr;
    std::FILE* fileStream = nullptr;
};
std::map<std::string, SoundFontData>& cache()
{
    static std::map<std::string, SoundFontData> s;
    return s;
}
std::mutex& cacheMutex()
{
    static std::mutex m;
    return m;
}

void* openSoundFont(const char* filename)
{
    auto search = cache().find(filename);
    if (search != cache().cend() && search->second.fileStream) {
        return search->second.fileStream;
    }
    std::FILE* stream = std::fopen(filename, "rb");
    SoundFontData sfData;
    sfData.fileStream = stream;
    cache().emplace(filename, std::move(sfData));
    return stream;
}
int readSoundFont(void* buf, fluid_long_long_t count, void* handle)
{
    return static_cast<int>(std::fread(buf, static_cast<size_t>(count), 1, static_cast<std::FILE*>(handle)));
}
int seekSoundFont(void* handle, fluid_long_long_t offset, int origin)
{
    return _fseeki64(static_cast<std::FILE*>(handle), offset, origin);
}
int closeSoundFont(void*) { return FLUID_OK; }
fluid_long_long_t tellSoundFont(void* handle) { return _ftelli64(static_cast<std::FILE*>(handle)); }
int deleteSoundFont(fluid_sfont_t*) { return FLUID_OK; }

fluid_file_callbacks_t FILE_CALLBACKS { openSoundFont, readSoundFont, seekSoundFont, closeSoundFont, tellSoundFont };

fluid_sfont_t* loadSoundFont(fluid_sfloader_t* loader, const char* filename)
{
    auto search = cache().find(filename);
    if (search != cache().cend() && search->second.soundFontPtr) {
        return search->second.soundFontPtr;
    }
    fluid_defsfont_t* defsfont = new_fluid_defsfont(static_cast<fluid_settings_t*>(fluid_sfloader_get_data(loader)));
    if (!defsfont) {
        return nullptr;
    }
    fluid_sfont_t* result = new_fluid_sfont(fluid_defsfont_sfont_get_name, fluid_defsfont_sfont_get_preset,
                                            fluid_defsfont_sfont_iteration_start, fluid_defsfont_sfont_iteration_next,
                                            deleteSoundFont);
    if (!result) {
        return result;
    }
    fluid_sfont_set_data(result, defsfont);
    defsfont->sfont = result;
    defsfont->fcbs = &FILE_CALLBACKS;
    if (fluid_defsfont_load(defsfont, &FILE_CALLBACKS, filename) == FLUID_FAILED) {
        fluid_defsfont_sfont_delete(result);
        return nullptr;
    }
    cache()[filename].soundFontPtr = result;
    return result;
}

void quietLog(int, const char*, void*) {}

} // namespace

Synth::~Synth() { release(); }

void Synth::release()
{
    if (m_synth) {
        delete_fluid_synth(m_synth);
        m_synth = nullptr;
    }
    if (m_settings) {
        delete_fluid_settings(m_settings);
        m_settings = nullptr;
    }
}

bool Synth::init(double sampleRate, const std::string& soundFont, std::string& error)
{
    release();
    std::lock_guard<std::mutex> lock(cacheMutex());
    for (int level : { FLUID_PANIC, FLUID_ERR, FLUID_WARN, FLUID_INFO, FLUID_DBG }) {
        fluid_set_log_function(level, quietLog, nullptr);
    }
    // FluidSynth::init()
    m_settings = new_fluid_settings();
    fluid_settings_setnum(m_settings, "synth.gain", FLUID_GLOBAL_VOLUME_GAIN);
    fluid_settings_setint(m_settings, "synth.audio-channels", FLUID_AUDIO_CHANNELS_PAIR);
    fluid_settings_setint(m_settings, "synth.lock-memory", 0);
    fluid_settings_setint(m_settings, "synth.threadsafe-api", 0);
    fluid_settings_setint(m_settings, "synth.midi-channels", MIDI_CHANNELS);
    fluid_settings_setint(m_settings, "synth.dynamic-sample-loading", 1);
    fluid_settings_setint(m_settings, "synth.polyphony", 512);
    fluid_settings_setnum(m_settings, "synth.sample-rate", sampleRate);
    fluid_settings_setint(m_settings, "synth.min-note-length", MIN_NOTE_LENGTH);
    fluid_settings_setint(m_settings, "synth.chorus.active", 0);
    fluid_settings_setint(m_settings, "synth.reverb.active", 0);
    fluid_settings_setstr(m_settings, "audio.sample-format", "float");

    // FluidSynth::createFluidInstance()
    m_synth = new_fluid_synth(m_settings);
    if (!m_synth) {
        error = "new_fluid_synth failed";
        release();
        return false;
    }
    fluid_sfloader_t* sfloader = new_fluid_sfloader(loadSoundFont, delete_fluid_sfloader);
    fluid_sfloader_set_data(sfloader, m_settings);
    fluid_synth_add_sfloader(m_synth, sfloader);

    // FluidSynth::addSoundFonts()
    if (fluid_synth_sfload(m_synth, soundFont.c_str(), 0) == FLUID_FAILED) {
        error = "Cannot load sound font: " + soundFont;
        release();
        return false;
    }

    // FluidSynth::setupSound(): piano, bank 0 program 0 on every channel.
    fluid_synth_activate_key_tuning(m_synth, 0, 0, "standard", nullptr, true);
    for (int channel = 0; channel < MIDI_CHANNELS; ++channel) {
        setupChannel(channel);
    }
    return true;
}

void Synth::setupChannel(int channelIdx)
{
    fluid_synth_set_interp_method(m_synth, channelIdx, FLUID_INTERP_DEFAULT);
    fluid_synth_pitch_wheel_sens(m_synth, channelIdx, 24);
    fluid_synth_bank_select(m_synth, channelIdx, 0);
    fluid_synth_program_change(m_synth, channelIdx, 0);
    fluid_synth_cc(m_synth, channelIdx, 7, DEFAULT_MIDI_VOLUME);
    fluid_synth_cc(m_synth, channelIdx, EXPRESSION_CONTROLLER, NATURAL_EXPRESSION_LEVEL);
    fluid_synth_cc(m_synth, channelIdx, 74, 0);
    fluid_synth_set_portamento_mode(m_synth, channelIdx, FLUID_CHANNEL_PORTAMENTO_MODE_EACH_NOTE);
    fluid_synth_set_legato_mode(m_synth, channelIdx, FLUID_CHANNEL_LEGATO_MODE_RETRIGGER);
    fluid_synth_activate_tuning(m_synth, channelIdx, 0, 0, 0);
}

// FluidSynth::handleEvent()
void Synth::noteOn(int channel, int key, int velocity)
{
    if (m_synth) fluid_synth_noteon(m_synth, channel, key, velocity);
}
void Synth::noteOff(int channel, int key)
{
    if (m_synth) fluid_synth_noteoff(m_synth, channel, key);
}
void Synth::controller(int channel, int cc, int value)
{
    if (!m_synth) return;
    if (cc == 120 || cc == 123) {   // all sound off / all notes off
        cc == 120 ? fluid_synth_all_sounds_off(m_synth, channel) : fluid_synth_all_notes_off(m_synth, channel);
        return;
    }
    fluid_synth_cc(m_synth, channel, cc, value);
}
void Synth::pitchBend(int channel, int value14)
{
    if (m_synth) fluid_synth_pitch_bend(m_synth, channel, value14);
}
void Synth::channelPressure(int channel, int value)
{
    if (m_synth) fluid_synth_channel_pressure(m_synth, channel, value);
}

// FluidSynth::doFlushSound()
void Synth::flushSound()
{
    if (!m_synth) return;
    fluid_synth_all_notes_off(m_synth, -1);
    for (int i = 0; i < MIDI_CHANNELS; ++i) {
        fluid_synth_cc(m_synth, i, 64, 0);    // SUSTAIN_PEDAL_CONTROLLER
        fluid_synth_cc(m_synth, i, 66, 0);    // SOSTENUTO_PEDAL_CONTROLLER
        fluid_synth_pitch_bend(m_synth, i, 8192);
    }
}

// FluidSynth::process()
void Synth::render(float* left, float* right, int frames)
{
    if (!m_synth || frames <= 0) return;
    fluid_synth_write_float(m_synth, frames, left, 0, 1, right, 0, 1);
}

} // namespace astra_msbasic
