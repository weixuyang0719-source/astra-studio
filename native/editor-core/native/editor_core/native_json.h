#pragma once
#include <nlohmann/json.hpp>
#include <string_view>
namespace astra::editor {
using Json = nlohmann::ordered_json;
Json timing_query(std::string_view name,const Json& args);
Json structural_command(const Json& tempo,const Json& operation,const Json& fixed_anchors,const Json& context);
Json pulse_command(const Json& tempo,const Json& operation,const Json& fixed_anchors,const Json& context);
Json note_command(const Json& document,const Json& semantic_command,const Json& context);
Json draft_command(const Json& draft,const Json& operation,const Json& context);
}
