#pragma once
// Worker-side pitch-preserving tempo processor. Never call from device callback.
// Signalsmith Stretch 1.1.0, the same algorithm/version shipped by Qt 6.12.
#include "third_party/signalsmith-stretch/signalsmith-stretch.h"
#include <vector>
#include <cmath>
#include <stdexcept>
#include <algorithm>
#include <cstdint>
#include <memory>
namespace astra {
class TempoStage {
 signalsmith::stretch::SignalsmithStretch<float> dsp;
 double tempo; uint64_t consumed=0,produced=0;
 std::vector<float> inL,inR,outL,outR;
public:
 TempoStage(unsigned sampleRate,double ratio):tempo(ratio){
  if(sampleRate<8000||sampleRate>384000||!std::isfinite(ratio)||ratio<.25||ratio>2)throw std::invalid_argument("invalid tempo format");
  dsp.presetDefault(2,sampleRate);
 }
 int inputLatency()const{return dsp.inputLatency();}
 int outputLatency()const{return dsp.outputLatency();}
 void reset(){dsp.reset();consumed=produced=0;}
 // Align the processing time with the first desired source sample. Call with
 // inputLatency() source samples beginning at that position, zero padded at EOF.
 void prime(const float* stereo,unsigned n){
  if(n!=unsigned(inputLatency()))throw std::invalid_argument("tempo prime length");
  planar(stereo,n);const float* in[]={inL.data(),inR.data()};dsp.seek(in,int(n),tempo);
 }
 uint64_t outputCount(unsigned n)const{return uint64_t(std::llround(double(consumed+n)/tempo))-produced;}
 void process(const float* stereo,unsigned n,std::vector<float>& output){
  auto count=outputCount(n);if(count>UINT32_MAX)throw std::length_error("tempo block too large");
  planar(stereo,n);outL.resize(size_t(count));outR.resize(size_t(count));
  const float* in[]={inL.data(),inR.data()};float* out[]={outL.data(),outR.data()};
  dsp.process(in,int(n),out,int(count));consumed+=n;produced+=count;interleave(output,size_t(count));
 }
 void flush(std::vector<float>& output){
  auto n=unsigned(outputLatency());outL.resize(n);outR.resize(n);float* out[]={outL.data(),outR.data()};dsp.flush(out,int(n));interleave(output,n);
 }
private:
 void planar(const float*p,unsigned n){inL.resize(n);inR.resize(n);for(unsigned i=0;i<n;++i){float a=p?p[i*2]:0,b=p?p[i*2+1]:0;if(!std::isfinite(a)||!std::isfinite(b))throw std::invalid_argument("nonfinite tempo input");inL[i]=a;inR[i]=b;}}
 void interleave(std::vector<float>&out,size_t n){out.resize(n*2);for(size_t i=0;i<n;++i){out[i*2]=outL[i];out[i*2+1]=outR[i];}}
};
// Signalsmith intentionally randomises phases above 2x time expansion. Cascading
// two <=2x stretches preserves stable pitch at .25.. .5 without selecting a lucky
// random seed or modifying vendored DSP. The first stage is exactly .5, so its
// integer output count is 2*N and the second retains exact cumulative duration.
class TempoProcessor {
 TempoStage first;std::unique_ptr<TempoStage>second;double tempo;int delay;
 std::vector<float>middle;
public:
 TempoProcessor(unsigned sampleRate,double ratio):first(sampleRate,ratio<.5?.5:ratio),tempo(ratio),delay(0){
  if(ratio<.5){second=std::make_unique<TempoStage>(sampleRate,ratio*2);delay=int(std::ceil((first.inputLatency()*2.+first.outputLatency()+second->inputLatency())/(ratio*2)+second->outputLatency()));}
 }
 int inputLatency()const{return second?0:first.inputLatency();}
 int outputLatency()const{return second?delay:first.outputLatency();}
 void reset(){first.reset();if(second)second->reset();}
 void prime(const float*p,unsigned n){if(second){if(n)throw std::invalid_argument("cascade prime length");}else first.prime(p,n);}
 void process(const float*p,unsigned n,std::vector<float>&out){if(!second){first.process(p,n,out);return;}first.process(p,n,middle);second->process(middle.data(),unsigned(middle.size()/2),out);}
 void flush(std::vector<float>&out){if(!second){first.flush(out);return;}process(nullptr,unsigned(std::ceil((delay+2)*tempo)),out);if(out.size()<size_t(delay)*2)throw std::runtime_error("tempo cascade tail too short");out.resize(size_t(delay)*2);}
};

}
