#pragma once
#include <nlohmann/json.hpp>
#include "native_json.h"

namespace astra::editor {
// draft is the owner's private session state, never response.draft from the UI.
// See integration_notes for the state/response/commit envelope.
Json draft_command(const Json& draft, const Json& operation, const Json& context);
}
