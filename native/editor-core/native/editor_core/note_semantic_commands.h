#pragma once

#include <nlohmann/json.hpp>
#include "native_json.h"

namespace astra::editor {

// Pure planner. The common transaction layer owns the authoritative document,
// revision checks, validation, the event reducer, receipts, and persistence.
// Result: {operation: null | low-level event operation, preview: object,
//          warnings: [{code: string, count?: integer}]}.
// A policy refusal has preview.blocked=true and no operation. Malformed input
// (including stale/foreign selection IDs on the supplied base) is invalid_argument.
Json note_command(const Json& document, const Json& semantic_command, const Json& context);

} // namespace astra::editor
