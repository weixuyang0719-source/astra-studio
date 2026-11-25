#pragma once
#include <string_view>
#include <nlohmann/json.hpp>
#include "native_json.h"

namespace astra::editor {
Json pulse_command(const Json& tempo, const Json& operation,
                   const Json& fixed_anchors, const Json& context);

// Native equivalent of rhythm_pulses.pulse_state, also useful to a ruler
// reader that needs IDs before the first edit materializes them on disk.
Json pulse_state(const Json& tempo);
}
