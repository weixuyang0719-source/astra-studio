#include "pcm_bus.hpp"
#include <array>
#include <cassert>
#include <iostream>
using namespace astra::rt;
struct S {uint64_t gen=0,pos=0,hint=0;int status=1,calls=0;};
int request(void* v,uint32_t source,uint64_t p,uint64_t g){assert(source==0);auto&s=*static_cast<S*>(v);s.pos=p;s.gen=g;++s.calls;return 1;}
int hint(void* v,uint32_t,uint64_t p,uint64_t g){auto&s=*static_cast<S*>(v);assert(g==s.gen);s.hint=p;return 1;}
int read(void* v,uint32_t,uint64_t p,uint32_t n,float*out){auto&s=*static_cast<S*>(v);if(s.status!=1)return s.status;for(uint32_t i=0;i<n*2;++i)out[i]=float(p+i);return 1;}
int main(){
 PcmBus b(8);S s;std::array<float,16> out{};b.bind({&s,request,read,hint});
 assert(b.add_to(out.data(),10,8,true,.5f,42)==PcmRead::ready);assert(out[0]==5 && out[15]==12.5f && s.hint==42);auto g=s.gen;
 out.fill(1);assert(b.add_to(out.data(),18,8,true,0)==PcmRead::ready && s.gen==g && out[0]==1);
 assert(b.add_to(out.data(),0,8,true,1)==PcmRead::ready && s.gen>g);g=s.gen;
 assert(b.add_to(out.data(),8,8,false,1)==PcmRead::inactive);assert(b.add_to(out.data(),8,8,true,1)==PcmRead::ready && s.gen>g);
 out.fill(3);s.status=0;assert(b.add_to(out.data(),16,8,true,1)==PcmRead::missing && out[0]==3 && out[15]==3);
 s.status=-1;assert(b.add_to(out.data(),24,8,true,1)==PcmRead::failed && out[0]==3);
 assert(b.add_to(out.data(),0,9,true,1)==PcmRead::invalid);assert(b.add_to(out.data(),UINT64_MAX,8,true,1)==PcmRead::invalid);
 b.bind({});assert(b.add_to(out.data(),0,8,true,1)==PcmRead::inactive);
 std::cout<<"pcm_bus PASS: contiguous/seek/loop/pause/miss/error/gain/bounds\n";
}
