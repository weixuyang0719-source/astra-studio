#include "component_handler.hpp"
#include <cstdio>
#include <cstdlib>
#define CHECK(x) do{if(!(x))std::abort();}while(false)
int main(){
 using namespace astra::vst_host;
 Handler h;Edit e;
 CHECK(h.state_generation.load()==0);
 h.beginEdit(1);h.endEdit(1);CHECK(h.state_generation.load()==0);
 CHECK(h.performEdit(1,.2)==kResultOk);CHECK(h.state_generation.load()==1);
 CHECK(h.pop(e)&&e.id==1&&e.value==.2);CHECK(h.state_generation.load()==1);
 h.restartComponent(0);CHECK(h.state_generation.load()==1);
 h.restartComponent(kParamValuesChanged);CHECK(h.state_generation.load()==2);
 CHECK(h.restart.load()&kParamValuesChanged);
 for(unsigned i=0;i<astra::vst_params::kEditSlots-1;++i)CHECK(h.performEdit(i,.3)==kResultOk);
 auto before=h.state_generation.load();CHECK(h.performEdit(0,.4)==kOutOfMemory);CHECK(h.state_generation.load()==before+1);
 h.state_generation.store(UINT64_MAX);h.restartComponent(kParamValuesChanged);CHECK(h.state_generation.load()==UINT64_MAX);
 std::puts("component handler generation tests passed");
}
