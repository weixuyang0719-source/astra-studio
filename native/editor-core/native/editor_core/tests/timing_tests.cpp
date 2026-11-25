#include "native_json.h"

#include <cmath>
#include <limits>
#include <stdexcept>
#include <iostream>

using astra::editor::Json;
using astra::editor::structural_command;
using astra::editor::timing_query;

namespace {
void check(bool value) { if (!value) throw std::runtime_error("native timing/structure assertion failed"); }
template<class F> void reject(F f) {
    try { f(); } catch (const std::invalid_argument&) { return; }
    throw std::runtime_error("expected std::invalid_argument");
}
Json tempo_op(const Json& tempo, Json operation) {
    operation["type"]="tempo";
    return structural_command(tempo,operation,Json::array({Json{{"seconds",2},{"beat",999},{"lock_beat",true}}}),Json::object());
}
}

int exercise() {
    auto original=timing_query("fixed_tempo",{{"bpm",120},{"duration",20}});
    original["anchors"][0]["external"]=Json::array({true,3,3.0});
    original["extensions"]={{"nullable",nullptr},{"opaque",Json::array({1,2.0,false})}};
    const auto before=original;
    check(timing_query("round_even",{{"value",-.5}})==0);
    check(timing_query("round_even",{{"value",-1.5}})==-2);
    check(timing_query("round_even",{{"value",2.5}})==2);
    const auto large=timing_query("round_even",{{"value",0x1p63}});
    check(large.is_number_unsigned());
    reject([]{ timing_query("round_even",{{"value",std::numeric_limits<double>::infinity()}}); });
    check(timing_query("to_beat",{{"tempo",original},{"seconds",21}})==42.);
    check(timing_query("to_seconds_many",{{"tempo",original},{"beats",Json::array({0,4,42})}})==Json::array({0.,2.,21.}));
    reject([&]{ timing_query("to_beat",{{"tempo",original},{"seconds",std::numeric_limits<double>::quiet_NaN()}}); });
    auto bare=original; bare.erase("time_signatures");
    const auto validated=timing_query("validate_tempo",{{"tempo",bare}});
    check(!bare.contains("time_signatures") && validated["time_signatures"][0]["denominator"]==4);
    auto ending=tempo_op(original,{{"action","score_end"},{"seconds",9.25}})["tempo"];
    const auto end=timing_query("ending_measure",{{"tempo",ending}});
    check(end["partial"]==true && end["meter"]["numerator"]==5 && end["meter"]["denominator"]==8);
    reject([&]{ tempo_op(original,{{"action","score_end"},{"seconds",9.251}}); });
    reject([&]{ timing_query("bar_at",{{"tempo",ending},{"beat",19}}); });
    check(timing_query("bar_at",{{"tempo",ending},{"beat",19},{"ignore_score_end",true}})["end"]==20.);
    reject([&]{ tempo_op(original,{{"action","add_meter_event"},{"seconds",.5},{"numerator",3}}); });
    reject([&]{ tempo_op(original,{{"action","meter_event"},{"seconds",0},{"numerator",3},{"denominator",4.0}}); });
    const auto flag=tempo_op(original,{{"action","move_beat"},{"seconds",2},{"to_seconds",2.1},{"beat",4}});
    check(flag["fixed_anchors"].empty() && !flag.contains("impact"));
    check(timing_query("to_seconds",{{"tempo",flag["tempo"]},{"beat",4}})==2.1);
    check(flag["tempo"]["anchors"][0]["external"][2].is_number_float());
    const auto lines=timing_query("boundaries",{{"tempo",original},{"revision","r"}});
    Json op={{"type","rebar"},{"boundary_revision","r"},{"tempo_sha256",lines[2]["tempo_sha256"]},
        {"left_boundary",lines[2]["id"]},{"right_boundary",lines[4]["id"]},{"start",4.},{"end",8.},
        {"numerator",3},{"denominator",4},{"bar_count",2}};
    reject([&]{ structural_command(original,op,nullptr,{{"revision","changed"}}); });
    const auto changed=structural_command(original,op,nullptr,{{"revision","r"}});
    check(changed["impact"]["following_quarter_shift"]==-2.);
    check(timing_query("to_beat",{{"tempo",changed["tempo"]},{"seconds",10}})==18.);
    check(changed["tempo"]["anchors"][0]["external"].dump()==original["anchors"][0]["external"].dump());
    check(changed["tempo"]["extensions"].dump()==original["extensions"].dump());
    check(original.dump()==before.dump());
    return 0;
}
int main(){try{return exercise();}catch(const std::exception& e){std::cerr<<e.what()<<"\n";return 1;}}
