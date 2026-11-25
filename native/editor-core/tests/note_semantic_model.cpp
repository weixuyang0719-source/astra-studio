#include "note_semantic_commands.h"

#include <cmath>
#include <iostream>
#include <stdexcept>
#include <string>
#include <string_view>

using astra::editor::Json;
using astra::editor::note_command;

namespace {

void check(bool value, const char* message) { if (!value) throw std::runtime_error(message); }
void near(const Json& a, double b) {
    if (!a.is_number() || std::abs(a.get<double>() - b) > 1e-10)
        throw std::runtime_error(a.dump() + " not near " + std::to_string(b));
}

Json fixture() {
    return Json::parse(R"({
        "duration":16,"revision":"base","deleted_ids":[],"range":{"start":1,"end":4,"loop":false},
        "tempo":{"anchors":[{"seconds":0,"beat":0},{"seconds":2,"beat":4},{"seconds":16,"beat":18}],
                 "time_signatures":[{"beat":0,"numerator":4,"denominator":4}],"opaque":{"keep":true}},
        "notes":[{"id":"b","start":1.06,"end":1.56,"pitch":64,"velocity":126,"channel":1,"opaque":[false,1,1.0,"01"]},
                 {"id":"a","start":1.06,"end":1.38,"pitch":0,"velocity":3},
                 {"id":"c","start":2.3,"end":2.4,"pitch":127,"velocity":80}],
        "controls":[{"id":"off","seconds":3,"number":64,"value":0,"channel":0},
                    {"id":"ped","seconds":1,"number":64,"value":72},
                    {"id":"other","seconds":2,"number":1,"value":32}]})");
}

Json run(const Json& d, const char* cmd, const Json& context = Json::object()) {
    return note_command(d, Json::parse(cmd), context);
}

void model_tests() {
    auto d = fixture();
    const auto save = d.dump();
    auto r = run(d, R"({"action":"quantize","ids":["a","b"],"target":"start","strength":50})");
    const auto& up = r.at("operation").at("updates");
    check(up.at(0).at("id") == "b" && up.at(1).at("id") == "a", "note selection takes document order");
    near(up.at(0).at("start"), 1.03); near(up.at(0).at("end"), 1.53);
    auto next = d;
    next["notes"][0]["start"] = up.at(0).at("start"); next["notes"][0]["end"] = up.at(0).at("end");
    r = run(next, R"({"action":"quantize","ids":["b"],"strength":50})");
    near(r.at("operation").at("updates").at(0).at("start"), 1.015);
    r = run(d, R"({"action":"quantize","ids":["b"],"strength":0})");
    check(r.at("operation").is_null(), "zero strength");
    r = run(d, R"({"action":"quantize","ids":["a"],"target":"length","length":0.5})");
    near(r.at("operation").at("updates").at(0).at("end"), 1.31);

    next = d; next["notes"][0]["start"] = 1.013; next["notes"][0]["end"] = 1.0132;
    r = run(next, R"({"action":"quantize","ids":["b"],"target":"both"})");
    near(r.at("operation").at("updates").at(0).at("end"), 1.0002);
    check(r.at("preview").at("short") == 1, "short both preserves duration");
    next["tempo"]["score_start_seconds"] = 1.01;
    r = run(next, R"({"action":"quantize","ids":["b"],"target":"start"})");
    check(r.at("operation").is_null() && r.at("preview").at("edge") == 1, "quantize opening floor");

    r = run(d, R"({"action":"legato","ids":["a","b"],"mode":"any","overlap_ms":-50})");
    const auto& l = r.at("operation").at("updates");
    check(l.at(0).at("id") == "a", "legato order within chords");
    near(l.at(0).at("end"), 2.25); near(l.at(1).at("end"), 2.25);
    r = run(d, R"({"action":"legato","ids":["a","b"],"mode":"selected"})");
    check(r.at("operation").is_null() && r.at("preview").at("last") == 2, "legato ignores same onset");
    r = run(d, R"({"action":"length_scale","ids":["c"],"percent":150})");
    near(r.at("operation").at("updates").at(0).at("end"), 2.45);

    r = run(d, R"({"action":"transpose","ids":["a","c"],"semitones":1})");
    check(r.at("preview").at("blocked") == true && r.at("operation").is_null(), "transpose atomic rejection");
    r = run(d, R"({"action":"transpose","ids":["a","c"],"semitones":12,"policy":"clamp"})");
    near(r.at("preview").at("delta"), 0);
    r = run(d, R"({"action":"velocity","ids":["a","b"],"mode":"dynamics","percent":200})");
    near(r.at("preview").at("mean"), 64.5);
    check(r.at("preview").at("clipped") == 2 && r.at("operation").at("updates").at(1).at("velocity") == 1,
          "velocity mean and clipping");
    r = run(d, R"({"action":"velocity","ids":["a"],"mode":"drag","up_pixels":-1.5})");
    check(r.at("operation").at("updates").at(0).at("velocity") == 2, "JS half toward positive infinity");

    r = run(d, R"({"action":"nudge","ids":["c"],"direction":-1,"snap":{"unit":1,"enabled":false}})");
    near(r.at("operation").at("updates").at(0).at("start"), 1.65);
    near(r.at("operation").at("updates").at(0).at("end"), 1.7);
    r = run(d, R"({"action":"info_assign","ids":["b","c"],"field":"start","shown":1.061,"value":1.161})");
    near(r.at("operation").at("updates").at(0).at("start"), 1.16);
    near(r.at("operation").at("updates").at(1).at("end"), 2.5);
    r = run(d, R"({"action":"info_assign","ids":["b","c"],"field":"end","value":2,"absolute":true})");
    check(r.at("preview").at("outside") == 1 && r.at("operation").is_null(), "atomic absolute assignment");
    r = run(d, R"({"action":"form_assign","domain":"controls","id":"ped","fields":{"seconds":1.2,"value":72}})");
    check(!r.at("operation").at("updates").at(0).contains("value"), "half-pedal stays unassigned");
    r = run(d, R"({"action":"cc_state","ids":["ped"],"value":127})");
    check(r.at("operation").at("updates").at(0).at("value") == 127, "explicit state assignment");

    r = run(d, R"({"action":"duplicate","ids":["a","b"]})");
    const auto& n = r.at("operation").at("add").at(0);
    check(!n.contains("id") && n.at("opaque").dump() == "[false,1,1.0,\"01\"]", "note copy preserves fields/types, omits ID");
    near(n.at("start"), 1.56);
    r = run(d, R"({"action":"repeat_defaults","domain":"controls","ids":["off","ped"]})");
    check(r.at("preview").at("from_range") == true, "CC repeat locator rule");
    near(r.at("preview").at("default_step_seconds"), 3);
    r = run(d, R"({"action":"repeat","domain":"controls","ids":["off","ped"],"count":2,"step_seconds":3})");
    check(r.at("operation").at("add").at(0).at("value") == 72 && r.at("operation").at("add").at(2).at("seconds") == 7,
          "repeat outer loop is copy number, then lane order");

    r = run(d, R"({"action":"copy","domain":"controls","ids":["ped"]})");
    const Json clipboard = {{"clipboard", r.at("preview").at("clipboard")}};
    next = d; next["controls"] = Json::array();
    r = run(next, R"({"action":"paste","at":"cursor","channel":3,"cursor_seconds":4})", clipboard);
    check(r.at("operation").at("add").at(0).at("channel") == 3, "missing source channel follows receiving CC lane");
    near(r.at("operation").at("add").at(0).at("seconds"), 4);
    r = run(d, R"({"action":"delete","ids":["a","b","a"]})");
    check(r.at("operation").at("delete") == Json::array({"a","b"}), "note delete preserves selection order");
    check(d.dump() == save, "planner does not mutate the input document");


    // A crossing note before a 120 -> 60 BPM boundary holds 0.75 beats.
    // Rounding its 3.75 beat onset to 4 means it is now 0.75 seconds long.
    auto crossing = d;
    crossing["notes"][0]["start"] = 1.875; crossing["notes"][0]["end"] = 2.5;
    const auto q = run(crossing, R"({"action":"quantize","ids":["b"],"target":"start","preset":1})");
    const auto& moved = q.at("operation").at("updates").at(0);
    near(moved.at("start"), 2); near(moved.at("end"), 2.75);
    const auto partial = run(crossing, R"({"action":"quantize","ids":["b"],"target":"start","preset":1,"strength":50})");
    near(partial["operation"]["updates"][0]["start"], 1.9375);
    near(partial["operation"]["updates"][0]["end"], 2.625);
    check(run(crossing, R"({"action":"quantize","ids":["b"],"target":"start","strength":0})")["operation"].is_null(), "no movement at zero strength");

    // A legal short span can land across a faster tempo edge while the
    // end moves less than update_eps. Retain that edge with the onset,
    // even when its omission would leave a positive (but incorrect) note.
    auto dense = crossing;
    dense["tempo"]["anchors"] = Json::array({Json{{"seconds",0},{"beat",0}},
        Json{{"seconds",2},{"beat",2}}, Json{{"seconds",8},{"beat",26}}});
    dense["notes"][0]["start"] = 2 - .00000016;
    dense["notes"][0]["end"] = 2 - .00000001;
    const auto thin = run(dense, R"({"action":"quantize","ids":["b"],"target":"start","preset":1})");
    const auto& thin_row = thin["operation"]["updates"][0];
    near(thin_row.at("start"), 2); near(thin_row.at("end"), 2.0000000375);
    dense["notes"][0]["end"] = 2 + .000000025;
    const auto still_positive = run(dense, R"({"action":"quantize","ids":["b"],"target":"start","preset":1})");
    near(still_positive["operation"]["updates"][0].at("end"), 2.000000065);
    dense["notes"][0]["start"] = 2 - .00000004;
    const auto below_threshold = run(dense, R"({"action":"quantize","ids":["b"],"target":"start","preset":1})");
    check(below_threshold["operation"].is_null(), "jointly negligible onset landing remains a no-op");

    auto limit = crossing;
    limit["duration"] = 2.6;
    const auto outside = run(limit, R"({"action":"quantize","ids":["b"],"target":"start","preset":1})");
    check(outside.at("operation").is_null() && outside.at("preview").at("edge") == 1,
          "would-be musical duration beyond end rejected rather than cropped");
    limit["duration"] = 2.75;
    const auto exact_end = run(limit, R"({"action":"quantize","ids":["b"],"target":"start","preset":1})");
    near(exact_end["operation"]["updates"][0]["end"], 2.75);

    // Length uses its independent 0.5 unit despite an unrelated onset preset,
    // fixed takes one unit, legacy takes the nearest multiple, neither alters start.
    const auto fixed = run(crossing, R"({"action":"quantize","ids":["b"],"target":"length","length_mode":"fixed","preset":2,"length":0.5})");
    const auto multiple = run(crossing, R"({"action":"quantize","ids":["b"],"target":"length","preset":2,"length":0.5})");
    check(!fixed["operation"]["updates"][0].contains("start") && !multiple["operation"]["updates"][0].contains("start"), "length holds onset");
    near(fixed["operation"]["updates"][0]["end"], 2.25);
    near(multiple["operation"]["updates"][0]["end"], 2.75);
    near(fixed["preview"]["unit"], .5);
    check(fixed["preview"]["length_mode"]=="fixed" && !multiple["preview"].contains("length_mode"), "preserves legacy receipt");

    // b/a are an onset chord off-grid; c is a closer unselected onset; "far"
    // is selected later. Only explicitly requested events are resized.
    auto legato_fixture = d;
    legato_fixture["notes"].push_back(Json{{"id","far"},{"start",3.25},{"end",3.7},{"pitch",84},{"velocity",80}});
    const auto selected_next = run(legato_fixture, R"({"action":"legato","ids":["a","b","far"],"mode":"selected"})");
    const auto any_next = run(legato_fixture, R"({"action":"legato","ids":["a","b","far"],"mode":"any"})");
    for (const auto& pair : {std::make_pair(&selected_next,3.25),std::make_pair(&any_next,2.3)}) {
        const auto& updates = (*pair.first)["operation"]["updates"];
        check(updates.size()==2 && updates[0]["id"]=="a" && updates[1]["id"]=="b", "legato edits both chord notes, not reference");
        for (const auto& update : updates) {
            check(!update.contains("start"), "legato holds onset");
            near(update["end"], pair.second);
        }
        check((*pair.first)["preview"]["last"]==1, "last note is unchanged");
    }

    for (const char* cmd : {R"({"action":"transpose","ids":["missing"],"semitones":1})",
                            R"({"action":"cc_state","ids":["other"],"value":127})",
                            R"({"action":"repeat","ids":["a"],"count":1.5,"step_seconds":1})",
                            R"({"action":"quantize","ids":["a"],"length":true,"target":"length"})"}) {
        bool threw = false;
        try { (void)run(d, cmd); } catch (const std::invalid_argument&) { threw = true; }
        check(threw, "invalid inputs must throw invalid_argument");
    }
}

// Test transport only. Deliberately links the real note and timing objects;
// catches only invalid_argument from the API, not unrelated native failures.
void json_lines() {
    for (std::string line; std::getline(std::cin, line);) {
        const auto packet = Json::parse(line);
        try {
            const auto answer = note_command(packet.at("document"), packet.at("command"), packet.at("context"));
            std::cout << answer.dump() << '\n';
        } catch (const std::invalid_argument& e) {
            std::cout << Json{{"invalid_argument", e.what()}}.dump() << '\n';
        }
    }
}

} // namespace

int main(int argc, char** argv) {
    try {
        if (argc == 1) model_tests();
        else if (argc == 2 && std::string_view(argv[1]) == "--jsonl") json_lines();
        else throw std::runtime_error("usage: note_semantic_model [--jsonl]");
        return 0;
    } catch (const std::exception& e) { std::cerr << e.what() << '\n'; return 1; }
}
