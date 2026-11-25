/* SPDX-License-Identifier: GPL-3.0-only
 * MuseScore Studio 4.6.5 PlaybackCursor geometry arrives in the worker's
 * logical page coordinates. No pixel inference, per-tick layout or raster IPC.
 *
 * Like MuseScore's cursor, which is moved on every audio position tick, the
 * mark is advanced on every display frame. Position packets arrive at ~20 Hz,
 * so between them the tick is predicted from the measured tick rate and each
 * packet only steers the prediction (no backward steps, snaps on real jumps).
 */
(() => {
  "use strict";
  const WIDTH=960, MAX_TICK=0x7fffffff, LIMIT=4096, CACHE=4;
  const finite=(x,a,b)=>typeof x==="number"&&Number.isFinite(x)&&x>=a&&x<=b;
  const int=(x,a,b)=>Number.isInteger(x)&&finite(x,a,b);
  function decodeRows(rows,height){
    if(!Array.isArray(rows)||rows.length>LIMIT||!int(height,480,1800))return null;
    let end=0;
    for(const row of rows){
      if(!Array.isArray(row)||row.length!==7)return null;
      const [a,b,x0,x1,y,w,h]=row;
      if(!int(a,end,MAX_TICK)||!int(b,a+1,MAX_TICK)||!finite(x0,-WIDTH,WIDTH*2)||
         !finite(x1,-WIDTH,WIDTH*2)||!finite(y,-height,height*2)||
         !finite(w,.001,WIDTH)||!finite(h,.001,height*2))return null;
      end=b;
    }
    return rows;
  }
  function hit(rows,tick){
    if(!rows||!finite(tick,0,MAX_TICK))return null;
    let lo=0,hi=rows.length;
    while(lo<hi){
      const mid=lo+((hi-lo)>>1);
      if(rows[mid][0]<=tick)lo=mid+1;else hi=mid;
    }
    const r=rows[lo-1];
    if(!r||tick>=r[1])return null; // exactly [start, end), never extrapolate a gap
    const [a,b,x0,x1,y,w,h]=r;
    return [x0+(x1-x0)*(tick-a)/(b-a),y,w,h];
  }
  function tickAt(rows,x,y){
    if(!rows||!finite(x,-WIDTH,WIDTH*2)||!finite(y,-1e5,1e5))return null;
    let best=null,score=Infinity;
    for(const [a,b,x0,x1,top,w,h] of rows){
      if(y<top||y>top+h)continue;              // same system only
      const left=x0-w*.5,right=x1;
      const d=x<left?left-x:x>right?x-right:0;
      if(d<score){score=d;best=[a,b,x0,x1];}
      if(!d)break;
    }
    if(!best||score>48)return null;
    const [a,b,x0,x1]=best;
    const f=x1>x0?Math.max(0,Math.min(1,(x-x0)/(x1-x0))):0;
    return Math.min(b-1,Math.round(a+(b-a)*f));
  }
  function create({request, canRequest=()=>true, onMove=null, observe=globalThis.ResizeObserver,
      schedule=(f)=>globalThis.requestAnimationFrame(f), cancel=(n)=>globalThis.cancelAnimationFrame(n)}){
    let frame=null,tick=null,layer=null,rect=null,observer=null,scheduled=null,hasBox=false,last=null,flight=null,serial=0;
    // Logical page units -> CSS px of the current raster (set by queueBox).
    let sx=1,sy=1,boxLeft=0,boxTop=0;
    // Frame-rate motion between authoritative packets.
    let motion=null,anim=0,samples=[];
    // A seek the user just made: shown at once, until the engine reports it.
    let pinned=null;
    const now=()=>globalThis.performance?.now?.()??Date.now();
    const cache=new Map();
    const validDOM=f=>Boolean(f?.surface?.isConnected&&!f.surface.hidden&&f.surface.parentElement===f.paper&&
       f.paper?.isConnected&&f.root?.isConnected&&f.root.contains(f.paper));
    const sameDoc=(a,b)=>a&&b&&a.root===b.root&&a.project===b.project&&a.generation===b.generation&&a.revision===b.revision;
    const key=f=>f.page+":"+f.logicalHeight;
    function setHidden(hide){if(layer&&layer.hasAttribute("hidden")!==hide)layer.toggleAttribute("hidden",hide);}
    function updateMark(){
      if(!hasBox||!validDOM(frame)||tick===null||!canRequest(frame,false)){setHidden(true);return;}
      const rows=cache.get(key(frame));
      let box=hit(rows,tick);
      // Hold the mark across a short unlaid gap instead of blinking.
      if(!box&&last&&rows&&last.tick!==undefined&&tick>last.tick&&tick-last.tick<960)box=last;
      if(!box){setHidden(true);return;}
      if(box!==last)box.tick=tick;
      const [x,y,w,h]=box;
      // A small composited bar moved by transform: no page repaint per frame.
      if(!last||last[0]!==x||last[1]!==y||last.sx!==sx||last.sy!==sy){
        rect.style.transform="translate3d("+(x*sx).toFixed(2)+"px,"+(y*sy).toFixed(2)+"px,0)";
        rect.dataset.at=x.toFixed(2)+" "+y.toFixed(2);
      }
      if(!last||last[2]!==w||last[3]!==h||last.sx!==sx||last.sy!==sy){
        rect.style.width=Math.max(2,w*sx).toFixed(2)+"px";rect.style.height=(h*sy).toFixed(2)+"px";
      }
      box.sx=sx;box.sy=sy;
      last=box;setHidden(false);
      onMove?.();
    }
    function detachBox(){
      if(scheduled!==null){cancel(scheduled);scheduled=null;}
      observer?.disconnect();observer=null;
      layer?.remove();layer=rect=null;
      hasBox=false;last=null;
    }
    function queueBox(){
      if(!frame||scheduled!==null)return;
      scheduled=schedule(()=>{
        scheduled=null;
        if(!validDOM(frame)||!layer){hasBox=false;setHidden(true);return;}
        // Only accepted frame/ResizeObserver paths can read layout.
        const s=frame.surface.getBoundingClientRect(),p=frame.paper.getBoundingClientRect();
        hasBox=s.width>0&&s.height>0;
        if(!hasBox){setHidden(true);return;}
        const x=s.left-p.left-frame.paper.clientLeft+frame.paper.scrollLeft;
        const y=s.top-p.top-frame.paper.clientTop+frame.paper.scrollTop;
        const box=layer.style;
        box.left=x+"px";box.top=y+"px";box.width=s.width+"px";box.height=s.height+"px";
        boxLeft=x;boxTop=y;
        sx=s.width/WIDTH;sy=s.height/frame.logicalHeight;
        updateMark();
      });
    }
    function createBox(){
      const doc=frame.paper.ownerDocument;
      layer=doc.createElement("div");layer.className="ast-sp-cursor-layer";
      layer.setAttribute("aria-hidden","true");layer.setAttribute("hidden","");
      rect=doc.createElement("div");rect.className="ast-sp-cursor-glyph";
      layer.append(rect);frame.paper.append(layer);
      if(typeof observe==="function"){
        observer=new observe(queueBox);
        observer.observe(frame.surface);observer.observe(frame.paper);
      }
      queueBox();
    }
    function stopMotion(){if(anim){cancel(anim);anim=0;}motion=null;samples=[];}
    function invalidate(dropTransport=false){
      stopMotion();pinned=null;
      tick=null;frame=null;cache.clear();detachBox();
      // A stale reply may be in flight against the same single-slot worker.
      // Drain it before submitting a different page; a replacement bridge resets it.
      if(dropTransport)flight=null;
    }
    function frameAccepted(f){
      if(!f||typeof f.project!=="string"||!f.project||!int(f.generation,1,MAX_TICK)||
         !int(f.revision,1,MAX_TICK)||!int(f.page,1,10000)||!int(f.logicalHeight,480,1800)||
         !validDOM(f)){invalidate();return;}
      const same=sameDoc(frame,f);
      if(same&&frame.page===f.page&&frame.logicalHeight===f.logicalHeight&&frame.surface===f.surface&&frame.paper===f.paper){
        // An adaptive raster density swap changes only the backing pixels.
        return;
      }
      if(!same)cache.clear();
      stopMotion();tick=null;setHidden(true);pinned=null;
      if(!frame||frame.paper!==f.paper||frame.surface!==f.surface)detachBox();
      frame=f;
      if(!layer)createBox();
      hasBox=false;queueBox();
    }
    function maybeRequest(){
      if(!frame||flight||cache.has(key(frame))||!validDOM(frame)||
         !canRequest(frame,true)||serial>=1_999_999_998)return;
      const f=frame, token=++serial;
      flight={frame:f,token};
      try{
        if(request(JSON.stringify({project:f.project,baseRevision:f.revision,page:f.page,receipt:token})))return;
      }catch{}
      if(flight?.token===token){flight=null;cache.set(key(f),null);}
    }
    function commit(raw){
      if(!flight||typeof raw!=="string"||raw.length>1_000_000)return false;
      let packet;try{packet=JSON.parse(raw);}catch{return false;}
      const run=flight,f=run.frame;
      if(!packet||packet.receipt!==run.token||packet.project!==f.project||
         packet.revision!==f.revision||packet.page!==f.page)return false;
      flight=null;
      const current=frame===f&&validDOM(f)&&canRequest(f,false);
      if(current){
        const id=key(f);
        cache.set(id,decodeRows(packet.rows,f.logicalHeight));
        if(cache.size>CACHE)cache.delete(cache.keys().next().value);
        updateMark();
      }
      maybeRequest();
      return current;
    }
    function predicted(t){
      const m=motion;
      if(!m)return tick;
      const dt=Math.min(t-m.t,m.limit);
      const steer=m.error*Math.min(1,Math.max(0,t-m.t)/m.settle);
      return Math.max(m.floor,m.tick+m.rate*Math.max(0,dt)+steer);
    }
    function step(){
      anim=0;
      if(!motion)return;
      const t=now(), next=predicted(t);
      motion.floor=next;
      tick=Math.floor(next);
      updateMark();
      anim=schedule(step);
    }
    function continuous(prev,sample){
      // The engine says what happened: a repeat or loop changes the written
      // occurrence or steps the written tick back, and a seek moves the
      // played clock away from wall time. Smoothing is only for plain play.
      if(!prev||sample.occ!==prev.occ||sample.tick<prev.tick)return false;
      if(sample.pos===null||prev.pos===null)return sample.tick-prev.tick<=960;
      const expected=prev.pos+(sample.t-prev.t)/1000*sample.speed;
      return Math.abs(sample.pos-expected)<=.12;
    }
    function steer(sample){
      const prev=samples[samples.length-1];
      if(!motion||!continuous(prev,sample)){
        // Jump straight to the engine position; keep the tempo estimate.
        const rate=motion?motion.rate:0;
        samples=[sample];
        motion={tick:sample.tick,t:sample.t,rate,error:0,settle:160,limit:260,floor:sample.tick};
        tick=sample.tick;
        return;
      }
      // Tick rate over the last ~0.4 s of this uninterrupted run.
      samples=samples.filter(s=>sample.t-s.t<=450);
      samples.push(sample);
      const first=samples[0];
      const span=sample.t-first.t;
      let rate=motion.rate;
      if(span>=90){
        const measured=(sample.tick-first.tick)/span;
        rate=motion.rate>0?motion.rate*.4+measured*.6:measured;
      }
      const shown=predicted(sample.t);
      const error=sample.tick-shown;
      motion={tick:shown,t:sample.t,rate:Math.max(0,rate),error,settle:160,limit:260,floor:shown};
    }
    function pin(at){
      if(!frame||!validDOM(frame)||!canRequest(frame,false)||!finite(at,0,MAX_TICK))return false;
      pinned={tick:at,until:now()+900};
      stopMotion();tick=at;maybeRequest();updateMark();
      return true;
    }
    function paint(project,status,playing,{hold=false}={}){
      if(pinned&&frame&&validDOM(frame)&&canRequest(frame,false)){
        const reported=finite(status?.writtenTick,0,MAX_TICK)&&project===frame.project?status.writtenTick:null;
        const confirmed=reported!==null&&reported>=pinned.tick-120&&reported<=pinned.tick+(playing?1920:240);
        if(!confirmed&&now()<pinned.until){
          // Older packets still describe the position before the seek.
          stopMotion();tick=pinned.tick;maybeRequest();updateMark();return;
        }
        pinned=null;
      }
      const showable=frame&&project===frame.project&&status?.revision===frame.revision&&status?.page===frame.page&&
        finite(status?.writtenTick,0,MAX_TICK)&&validDOM(frame)&&canRequest(frame,false);
      if(!showable||(!playing&&!hold)){
        stopMotion();tick=null;setHidden(true);
        if(frame&&validDOM(frame)&&canRequest(frame,false))maybeRequest();
        return;
      }
      if(!playing){
        stopMotion();tick=status.writtenTick;
        maybeRequest();updateMark();return;
      }
      steer({tick:status.writtenTick,t:now(),occ:Number.isInteger(status.tickOccurrence)?status.tickOccurrence:1,
        pos:finite(status.position,0,1e7)?status.position:null,speed:finite(status.speed,.05,4)?status.speed:1});
      if(tick===null)tick=status.writtenTick;
      maybeRequest();
      if(!anim)anim=schedule(step);
    }
    function locate(x,y){
      if(!frame||!validDOM(frame))return null;
      return tickAt(cache.get(key(frame)),x,y);
    }
    // Whether the shown page's geometry is here for locate(); asks for it if
    // not. A refused answer (worker busy) is asked for again, at most 4 times a second.
    let retryAt=0;
    function ready(){
      if(!frame||!validDOM(frame))return false;
      const id=key(frame);
      if(cache.get(id))return true;
      if(cache.has(id)&&!flight&&now()>=retryAt){cache.delete(id);retryAt=now()+250;}
      maybeRequest();
      return false;
    }
    // The shown mark in the paper's scroll coordinates (MuseScore's cursor rect).
    function box(){
      if(!last||!layer||layer.hasAttribute("hidden"))return null;
      return {x:boxLeft+last[0]*sx,y:boxTop+last[1]*sy,w:Math.max(2,last[2]*sx),h:last[3]*sy};
    }
    // Systems on the shown page (distinct cursor rows), as needAdjustCanvasVerticallyWhilePlayback counts them.
    function systemCount(){
      const rows=frame?cache.get(key(frame)):null;
      return rows?new Set(rows.map(r=>r[4])).size:0;
    }
    return Object.freeze({frameAccepted,commit,paint,pin,locate,ready,box,systemCount,reflow:queueBox,invalidate});
  }
  const api=Object.freeze({create,decodeRows,hit,tickAt});
  if(typeof module!=="undefined"&&module.exports)module.exports=api;
  else globalThis.AstraScorePlaybackCursor=api;
})();
