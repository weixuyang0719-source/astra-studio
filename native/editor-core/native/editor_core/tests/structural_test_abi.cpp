#include "native_json.h"

#include <exception>
#include <stdexcept>
#include <string>

#if defined(_WIN32)
#define ASTRA_TEST_EXPORT __declspec(dllexport)
#elif defined(__GNUC__)
#define ASTRA_TEST_EXPORT __attribute__((visibility("default")))
#else
#define ASTRA_TEST_EXPORT
#endif

// Test-only UTF-8 JSON ABI. The result is borrowed until the next call on
// this thread. Neither this symbol nor its envelope is a production protocol.
extern "C" ASTRA_TEST_EXPORT const char* astra_editor_structural_test_call(const char* request) noexcept {
    using namespace astra::editor;
    try {
        static thread_local std::string output;
        try {
            if (!request) throw std::invalid_argument("null request");
            const auto req=Json::parse(request);
            Json result;
            if (req.at("entry")=="query") result=timing_query(req.at("name").get<std::string>(),req.at("args"));
            else if (req.at("entry")=="command") result=structural_command(req.at("tempo"),req.at("operation"),req.at("fixed_anchors"),req.at("context"));
            else throw std::invalid_argument("unknown test ABI entry");
            output=Json{{"ok",true},{"value",std::move(result)}}.dump();
        } catch (const std::invalid_argument& e) {
            output=Json{{"ok",false},{"kind","invalid_argument"},{"reason",e.what()}}.dump();
        } catch (const std::exception& e) {
            output=Json{{"ok",false},{"kind","unexpected_exception"},{"reason",e.what()}}.dump();
        }
        return output.c_str();
    } catch (...) {
        return "{\"ok\":false,\"kind\":\"unexpected_exception\",\"reason\":\"test ABI failed\"}";
    }
}
