// Test executable only. Link with both owned modules and the actual shared
// foundation; no fake converter or fallback from the production kernel.
#include "pulse_commands.h"
#include "draft_commands.h"
#include <iostream>
#include <stdexcept>
#include <string>

int main() {
    using namespace astra::editor;
    for (std::string line; std::getline(std::cin,line);) {
        Json answer;
        try {
            const auto q=Json::parse(line);
            const auto mode=q.at("mode").get<std::string>();
            const Json context=q.value("context",Json::object());
            Json r;
            if (mode=="pulse") r=pulse_command(q.at("tempo"),q.at("operation"),q.value("fixed_anchors",Json::array()),context);
            else if (mode=="projection") r=pulse_state(q.at("tempo"));
            else if (mode=="draft") r=draft_command(q.at("state"),q.at("operation"),context);
            else throw std::invalid_argument("unknown driver mode");
            answer=Json{{"ok",true},{"result",std::move(r)}};
        } catch (const std::invalid_argument& e) {
            answer=Json{{"ok",false},{"invalid_argument",true},{"error",e.what()}};
        } catch (const std::exception& e) {
            answer=Json{{"ok",false},{"invalid_argument",false},{"error",e.what()}};
        }
        std::cout << answer.dump(-1,' ',false,Json::error_handler_t::replace) << '\n' << std::flush;
    }
}
