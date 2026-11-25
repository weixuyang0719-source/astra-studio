#pragma once
#include "astra_editor_core.h"

namespace astra::editor {
// Private Document-only projection request, never part of the event ABI or
// a WebChannel command. An empty landing window stages boundary points without
// making them incoming winners in the ordinary CC64 bucket reducer.
struct RangeProjection { double start, end; };
int32_t prepare_range_controls(aec_session*, const aec_command*, const RangeProjection&, aec_prepared**) noexcept;
}
