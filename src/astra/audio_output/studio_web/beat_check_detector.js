"use strict";
window.AstraBeatDetector=(()=>{
  const limit=200000, epsilon=1e-6;
  const ordered=values=>Array.isArray(values)&&values.length<=limit&&
    values.every((value,i)=>Number.isFinite(value)&&value>=0&&(!i||value>values[i-1]));
  const normal=ratio=>ratio>=.87&&ratio<=1.13;
  function excludedRanges(tempo){
    const orderedRanges=[...(tempo.explicit_partial_measures??[]),
                         ...(tempo.tempo_expression_annotations??[]),
                         ...(tempo.meter_diagnostics?.pulse_repairs??[])]
      .filter(r=>Number.isFinite(r?.start_seconds)&&Number.isFinite(r?.end_seconds)&&r.start_seconds<r.end_seconds)
      .sort((a,b)=>a.start_seconds-b.start_seconds);
    const merged=[];
    for(const {start_seconds:start,end_seconds:end} of orderedRanges){
      const previous=merged.at(-1);
      if(previous&&start<=previous.end)previous.end=Math.max(previous.end,end);
      else merged.push({start,end});
    }
    return merged;
  }

  function longIntervalEvidence(tempo){
    const diagnostics=tempo.pulse_diagnostics;
    const evidence=new Map();
    if(diagnostics?.automatic_correction!==false)return evidence;
    for(const row of diagnostics.ranges??[]){
      for(const detail of row.evidence??[]){
        if(detail.type==='isolated_long_interval'&&Number.isInteger(detail.interval_index)){
          evidence.set(detail.interval_index,{...detail,start:row.start_seconds,end:row.end_seconds});
        }
      }
    }
    return evidence;
  }

  function upper(rows,value,key){
    let lo=0,hi=rows.length;
    while(lo<hi){
      const middle=(lo+hi)>>>1;
      if(rows[middle][key]<=value)lo=middle+1;
      else hi=middle;
    }
    return lo;
  }

  function interpolate(anchors,value,from,to){
    const index=Math.max(0,Math.min(anchors.length-2,upper(anchors,value,from)-1));
    const a=anchors[index],b=anchors[index+1];
    return a[to]+(value-a[from])*(b[to]-a[to])/(b[from]-a[from]);
  }

  function manualClockChange(tempo,start,end){
    if(tempo.source!=='manual_editor')return false;
    const current=tempo.anchors,initial=tempo.rhythm_initial?.tempo?.anchors;
    if(!Array.isArray(initial)||initial.length<2)return true;
    const beatAt=(rows,seconds)=>interpolate(rows,seconds,'seconds','beat');
    const offset=beatAt(current,start)-beatAt(initial,start);
    const changed=seconds=>Math.abs(beatAt(current,seconds)-beatAt(initial,seconds)-offset)>.04;
    if(changed(end))return true;
    for(const rows of [current,initial]){
      for(let i=upper(rows,start,'seconds');i<rows.length&&rows[i].seconds<end;i++){
        if(changed(rows[i].seconds))return true;
      }
    }
    return false;
  }

  function scan({tempo,sections,points,downbeats}){
    const marks=[];
    const unavailable={marks,hasEvidence:false};
    const anchors=tempo?.anchors;
    if(!ordered(points)||!ordered(downbeats)||downbeats.length<4||
       !Array.isArray(anchors)||anchors.length<2||!Array.isArray(sections)||!sections.length)return unavailable;

    const projection=tempo.pulse_projection;
    // Legacy Beat This emits quarter pulses. A known scale is published as
    // a projection. Otherwise restrict inference to quarter-note meters.
    const advertised=projection?.effective_quarter_span_per_pulse??projection?.quarter_span_per_pulse;
    const legacy=tempo.source==='beat_this'||tempo.rhythm_initial?.tempo?.source==='beat_this';
    if(advertised!==undefined&&(!Number.isFinite(advertised)||advertised<=0))return unavailable;
    const meters=sections.map(m=>{
      const unit=advertised??(legacy||m.denominator===4?1:NaN);
      const count=m.numerator*4/m.denominator/unit;
      return {start:interpolate(anchors,m.beat,'beat','seconds'),
              count:Math.abs(count-Math.round(count))<epsilon?Math.round(count):0,
              skip:Boolean(m.pickup||m.intro||m.silence||m.short||m.free||m.smart_measure)};
    });
    const ignored=excludedRanges(tempo);
    const longDiagnostics=longIntervalEvidence(tempo);

    // Raw downbeats must actually be in the validated raw pulse sequence.
    const boundaries=[];
    let pulse=0;
    for(const time of downbeats){
      while(pulse<points.length&&points[pulse]<time)pulse++;
      if(points[pulse]!==time)return unavailable;
      boundaries.push(pulse);
    }

    const interval=index=>points[index+1]-points[index];
    function regular(begin,end,period){
      for(let i=begin;i<end;i++)if(!normal(interval(i)/period))return false;
      return true;
    }

    function longGap(begin,end,period){
      let gap=-1;
      for(let i=begin;i<end;i++){
        const ratio=interval(i)/period;
        if(normal(ratio))continue;
        if(gap>=0||ratio<1.84||ratio>2.16)return -1;
        gap=i;
      }
      return gap;
    }

    function midpoint(gap){
      return (points[gap]+points[gap+1])/2;
    }

    function anomalousPulse(begin,end,count,period){
      if(end-begin===count-1){
        const gap=longGap(begin,end,period);
        return gap<0?null:{seconds:midpoint(gap),kind:"missing"};
      }
      if(end-begin===count+1){
        let middle=null;
        for(let i=begin;i<end;i++){
          const ratio=interval(i)/period;
          if(normal(ratio))continue;
          const next=i+1<end?interval(i+1)/period:0;
          if(middle!==null||ratio<.38||ratio>.62||next<.38||next>.62||!normal(ratio+next))return null;
          middle=points[++i];
        }
        return middle===null?null:{seconds:middle,kind:"extra"};
      }
      return null;
    }

    let meterIndex=-1,ignoredIndex=0,hasEvidence=false;
    for(let i=1;i+2<boundaries.length;i++){
      const left=boundaries[i-1],begin=boundaries[i],end=boundaries[i+1],right=boundaries[i+2];
      const start=points[left],stop=points[right];
      while(meterIndex+1<meters.length&&meters[meterIndex+1].start<=start+epsilon)meterIndex++;
      const meter=meters[meterIndex],next=meters[meterIndex+1];
      if(!meter||meter.skip||meter.count<3||meter.count>16||next?.start<stop-epsilon)continue;
      const count=meter.count;
      if(begin-left!==count||right-end!==count)continue;
      const first=(points[begin]-start)/count,last=(stop-points[end])/count;
      if(first/last<.92||first/last>1.08||!regular(left,begin,first)||!regular(end,right,last))continue;
      while(ignoredIndex<ignored.length&&ignored[ignoredIndex].end<=start+epsilon)ignoredIndex++;
      if(ignored[ignoredIndex]?.start<stop-epsilon)continue;
      const period=(first+last)/2,units=(points[end]-points[begin])/period;
      let mark=null;
      if(Math.abs(units/count-1)<=.08){
        if(end-begin===count&&regular(begin,end,period)){
          hasEvidence=true;
          continue;
        }
        mark=anomalousPulse(begin,end,count,period);
      }
      // A fixed-count decoder shifts the next downbeat after a long gap.
      // Require a matching source diagnostic and leave its meaning undecided.
      if(!mark&&end-begin===count&&longDiagnostics.size&&Math.abs(units-(count+1))<=.24){
        const gap=longGap(begin,end,period),cached=longDiagnostics.get(gap);
        if(cached&&cached.start<=points[gap]&&cached.end>=points[gap+1]&&
           Math.abs(cached.interval_seconds-interval(gap))<=epsilon){
          mark={seconds:midpoint(gap),kind:"gap"};
        }
      }
      if(mark&&!manualClockChange(tempo,start,stop)){
        hasEvidence=true;
        marks.push(mark);
      }
    }
    return {marks,hasEvidence};
  }
  return {scan};
})();

