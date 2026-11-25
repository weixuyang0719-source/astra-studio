#include <cstdlib>
#include <new>
#include <cassert>
#include "insert_chain.hpp"
static bool in_callback=false;static int released=0;
void* operator new(size_t n){assert(!in_callback);if(auto*p=std::malloc(n))return p;throw std::bad_alloc();}
void operator delete(void*p)noexcept{assert(!in_callback);std::free(p);}
void operator delete(void*p,size_t)noexcept{assert(!in_callback);std::free(p);}
static void release(void*){assert(!in_callback);++released;}
static InsertChain* make(uint64_t revision){auto*p=new InsertChain;p->revision=revision;p->release=release;p->slots.push_back({reinterpret_cast<void*>(1),false});p->scratch.resize(512);return p;}
int main(){
 {InsertChains chains;
 for(uint64_t r=1;r<=300;++r){auto*p=make(r);assert(chains.publish(p));auto*busy=make(r+1);assert(!chains.publish(busy));delete busy;in_callback=true;chains.boundary();in_callback=false;assert(chains.applied.load()==r);assert(chains.count.load()==1);chains.collect();}
 assert(released==599);}
 assert(released==600);return 0;
}
