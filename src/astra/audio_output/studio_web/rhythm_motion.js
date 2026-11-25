"use strict";
// Presentation only: never used for hit testing, MIDI export, playback or BPM data.
window.AstraRhythmMotion=(()=>{
  const duration=160,media=window.matchMedia?.('(prefers-reduced-motion: reduce)');
  const settles=new Map();let pending=null,motion=null,enabled=true,trackAccent=null;
  try{enabled=window.localStorage?.getItem('astra-rhythm-motion')!=='off';}catch{}
  const reduced=()=>Boolean(media?.matches),allowed=()=>enabled&&!reduced(),easeOut=t=>1-(1-t)**3;
  function cancel(){pending=null;if(motion){motion=null;requestRollDraw();}}
  function capture(){
    cancel();const doc=state.project?.editor,scroll=$('roll-scroll');
    if(!allowed()||rulerMode!=='bars'||!(doc?.tempo?.anchors?.length>1)||!scroll||!midiWidth)return null;
    // Published maps are replaced, not modified by draft or display edits.
    const axis=rollAxis(),offset=scroll.scrollLeft,viewport=scroll.clientWidth;
    return pending={project:state.project.path,revision:doc.revision,axis,scroll,offset,viewport,left:axis.seconds(offset),right:axis.seconds(offset+viewport)};
  }
  function discardCapture(token){if(pending===token)pending=null;}
  function tick(){
    const doc=state.project?.editor,scroll=$('roll-scroll');
    if(!allowed()){pending=motion=null;return;}
    if(pending&&pending.revision!==doc?.revision){
      const old=pending;pending=null;
      const before=old.axis.anchors,after=doc?.tempo?.anchors;
      if(old.project===state.project?.path&&old.axis.mode===rulerMode&&old.scroll===scroll&&old.axis.width===midiWidth&&old.viewport===scroll.clientWidth&&after?.length>1&&
         (before.length!==after.length||before.some((p,i)=>p.seconds!==after[i].seconds||p.beat!==after[i].beat)))motion={...old,started:performance.now(),progress:0,targetOffset:scroll.scrollLeft};
    }
    if(!motion)return;
    if(motion.project!==state.project?.path||motion.axis.mode!==rulerMode||motion.scroll!==scroll||motion.axis.width!==midiWidth||motion.viewport!==scroll.clientWidth||motion.targetOffset!==scroll.scrollLeft){motion=null;return;}
    motion.progress=Math.min(1,Math.max(0,(performance.now()-motion.started)/duration));
    if(motion.progress>=1){motion=null;return;}
    motion.ease=easeOut(motion.progress);requestRollDraw();
  }
  function x(seconds,offset){const target=midiX(seconds);if(!motion)return target;const before=motion.axis.x(seconds)-motion.offset;return offset+before+(target-offset-before)*motion.ease;}
  function bounds(start,end){return motion?{start:Math.min(start,motion.left),end:Math.max(end,motion.right)}:{start,end};}
  // Schedule damage through the existing roll frame, not a second animation loop.
  // The sampler never issues a command or reads/writes the durable model.
  function settle(key,ms,sample,repaint){
    if(!allowed())return false;
    settles.set(key,{ms,sample,repaint,start:performance.now()});sample(0,0);repaint();return true;
  }
  function stopSettle(key){settles.delete(key);}
  function advanceSettles(now){
    for(const [key,s] of settles){
      const t=allowed()?Math.max(0,Math.min(1,(now-s.start)/s.ms)):1;
      if(t===1)settles.delete(key);
      s.sample(easeOut(t),t);
    }
  }
  function scheduleSettles(){for(const s of settles.values())s.repaint();}
  function accentTrack(row){
    trackAccent?.cancel();trackAccent=null;
    if(allowed())trackAccent=row.querySelector('.track-name strong')?.animate?.([{opacity:.83},{opacity:1}],{duration:110,easing:'ease-out'})??null;
  }
  function resetMotion(){
    cancel();trackAccent?.cancel();trackAccent=null;
    for(const [key,s] of settles){settles.delete(key);s.sample(1,1);s.repaint();}
  }
  function setEnabled(value){enabled=Boolean(value);try{window.localStorage?.setItem('astra-rhythm-motion',enabled?'on':'off');}catch{}resetMotion();}
  // New input gets the authoritative mapping immediately. Do not throw away a
  // pending publication just because the user moved a modifier during the save.
  for(const type of ['pointerdown','wheel','keydown'])document.addEventListener(type,()=>{if(motion)cancel();},true);
  media?.addEventListener?.('change',resetMotion);
  const progress=()=>motion?.progress??1;
  return {capture,discardCapture,tick,x,bounds,cancel,setEnabled,toggle:()=>setEnabled(!enabled),enabled:()=>enabled,reduced,active:()=>Boolean(motion),progress,settle,stopSettle,advanceSettles,scheduleSettles,accentTrack,status:()=>({active:Boolean(motion),progress:progress(),duration,enabled,reduced:reduced(),pending:Boolean(pending)})};
})();
