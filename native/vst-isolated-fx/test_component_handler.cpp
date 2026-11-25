#include "component_handler.hpp"
#include <cassert>
using namespace astra::vst_host;
int main() {
 Handler h;
 const uint64_t before=h.state_generation.load();
 assert(h.restartComponent(kParamTitlesChanged)==kResultOk);
 assert(h.state_generation.load()==before);
 assert(h.restart.load()==kParamTitlesChanged);
 assert(h.restartComponent(kParamTitlesChanged)==kResultOk);
 assert(h.state_generation.load()==before);
 assert(h.restartComponent(kParamTitlesChanged|kParamValuesChanged)==kResultOk);
 assert(h.state_generation.load()==before+1);
 assert(h.restart.load()==(kParamTitlesChanged|kParamValuesChanged));
 assert(h.beginEdit(150)==kResultOk);
 assert(h.performEdit(150,0.125)==kResultOk);
 assert(h.endEdit(150)==kResultOk);
 assert(h.state_generation.load()==before+2);
 Edit e{};
 assert(h.pop(e)&&e.id==150&&e.value==0.125);
 assert(h.restartComponent(kMidiCCAssignmentChanged)==kResultOk);
 assert(h.state_generation.load()==before+3);
}
