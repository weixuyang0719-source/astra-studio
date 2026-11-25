#include "click_bus.hpp"
#include <cassert>
#include <iostream>
using namespace astra::rt;
int main(){
 ClickBus p(48000,96000,{{0,true},{24000,false},{48000,false},{72000,true}});
 std::vector<float> whole(192000,0),parts(192000,0);
 assert(p.add_to(whole.data(),0,96000,1)==4);
 for(unsigned pos=0;pos<96000;pos+=127)p.add_to(parts.data()+2*pos,pos,std::min(127u,96000-pos),1);
 assert(whole==parts);
 std::vector<float> seek(1000,0);p.add_to(seek.data(),48010,500,1);
 assert(std::equal(seek.begin(),seek.end(),whole.begin()+96020));
 std::vector<float> muted(1000,0);assert(p.add_to(muted.data(),0,500,0)==0);for(auto v:muted)assert(v==0);
 float strong=0,weak=0;for(unsigned i=0;i<1152;++i){strong=std::max(strong,std::abs(whole[2*i]));weak=std::max(weak,std::abs(whole[48000+2*i]));}assert(strong>weak);
 ClickBus free(48000,96000,{});assert(free.add_to(muted.data(),0,500,1)==0);
 ClickBus same(48000,96000,{{0,false},{0,true}});assert(same.size()==1);
 bool rejected=false;try{ClickBus bad(48000,96000,{{10,false},{9,true}});}catch(const std::invalid_argument&){rejected=true;}assert(rejected);
 std::cout<<"click exact-block/seek/loop-replay/mute/accent/empty/duplicate/bounds passed\n";
}
