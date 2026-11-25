/* Physical layout and raster are independent during a same-owner mode change.
 * The native scroll viewport (and the actual event targets) remain live;
 * this does not snapshot, clone, scale, or replace any of the editor layers. */
const AstraRollModeMotion = (() => {
  const lanes=['ruler-canvas','velocity-canvas','pedal-canvas','chord-canvas',
    'opening-canvas','beat-flags-canvas','mapping-canvas','waveform-canvas'];
  let current=null;
  const clamp=(n,max)=>Math.max(0,Math.min(max,n));
  const box=scroll=>{const r=scroll.getBoundingClientRect(),left=r.left+scroll.clientLeft;
    return {left,right:left+scroll.clientWidth,top:r.top, width:scroll.clientWidth,height:scroll.clientHeight};};
  const offset=(m,geometry)=>m.anchor.left<1?0:clamp(Math.round(m.anchor.left+geometry.left-m.anchor.x),
    Math.max(0,m.total-geometry.width));
  function stop() {
    const m=current;if(!m)return null;
    current=null;cancelAnimationFrame(m.raf);
    m.workspace.style.removeProperty('grid-template-columns');
    m.workspace.style.removeProperty('--mode-side-width');
    m.workspace.removeAttribute('data-side-leaving');
    m.canvas.style.removeProperty('position');m.canvas.style.removeProperty('left');m.canvas.style.removeProperty('top');
    for(const layer of m.layers){
      layer.style.removeProperty('translate');layer.style.removeProperty('clip-path');
    }
    return m;
  }
  function paintGeometry(m,p) {
    const column=m.column0+(m.column1-m.column0)*p;
    m.workspace.style.setProperty('grid-template-columns', column+'px minmax(0,1fr)','important');
    if(m.closing)m.workspace.style.setProperty('--mode-side-width',column+'px');
    const g=box(m.scroll);
    // The same clipped content box owns scroll bounds during overscan.
    // CSS min-width supplies the empty tail without adding travel.
    const content=m.canvas.parentElement,extent=m.total+'px';
    if(content.style.width!==extent)content.style.width=extent;
    m.scroll.scrollLeft=offset(m,g);
    const left=m.scroll.scrollLeft,cut=Math.max(0,left-m.from);
    const right=Math.max(0,m.rasterWidth-cut-g.width);
    for(const layer of m.layers){
      layer.style.translate=cut?(-cut)+'px':'';
      layer.style.clipPath='inset(0 '+right+'px 0 '+cut+'px)';
    }
    m.canvas.style.position='absolute';m.canvas.style.left=m.from+'px';m.canvas.style.top='0';
    m.geometry={...g,offset:left};
  }
  function start({workspace,on,anchor,before,after,oldColumn,newColumn,scroll,total,height,owner}) {
    // Zero is sticky by design in both directions. Treating its left hand
    // boundary as a positive anchor would put the first notes under the inspector.
    const zero=anchor?.left<1;
    if(!anchor||!before||before.width<80||after.width<80||Math.abs(oldColumn-newColumn)<2
      ||Math.abs(before.right-after.right)>1||Math.abs(before.top-after.top)>1||before.height!==after.height
      ||Math.abs(after.left-before.left-(newColumn-oldColumn))>1.1||document.hidden
      ||matchMedia('(prefers-reduced-motion: reduce)').matches)return null;
    const fromOffset=zero?0:anchor.left, toOffset=zero?0:clamp(Math.round(fromOffset+after.left-before.left),Math.max(0,total-after.width));
    const from=Math.min(fromOffset,toOffset),rasterWidth=Math.ceil(Math.max(fromOffset+before.width,toOffset+after.width)-from)+1;
    const cost=rasterWidth*(height+880)*canvasRatio()**2*4;
    if(!Number.isFinite(cost)||cost>96*1024*1024)return null;
    const m=current={workspace,scroll,total,owner,canvas:$('midi-canvas'),closing:!on,
      layers:lanes.map($).filter(Boolean),anchor,column0:oldColumn,column1:newColumn,from,rasterWidth,
      zoom,pitchZoom,ratio:canvasRatio(),axisMode:rulerMode,holderWidth:workspace.clientWidth,
      beforeHeight:before.height,raf:0};
    if(m.closing)workspace.setAttribute('data-side-leaving','');
    paintGeometry(m,0);
    let startAt=null;
    const tick=now=>{
      if(current!==m)return;
      if(!valid(m)||m.scroll.scrollLeft!==m.geometry.offset){
        const left=m.scroll.scrollLeft;
        stop();pendingRollScroll={scroll:m.scroll,left,top:m.scroll.scrollTop};
        if(m.scroll.isConnected)drawMidi();return;
      }
      startAt??=now;
      const t=clamp((now-startAt)/160,1);
      if(t===1){
        paintGeometry(m,1);const target=m.geometry.offset;
        stop();if(m.scroll.scrollLeft!==target)m.scroll.scrollLeft=target;
        drawMidi();updateMidiCursor(now);return;
      }
      paintGeometry(m,t*t*(3-2*t));
      if(rollGeometry?.canvas===m.canvas){
        rollGeometry.offset=m.geometry.offset;rollGeometry.viewport=m.geometry.width;
      }
      syncRollTimeScroll(m.scroll);
      updateMidiCursor(now);window.AstraRhythmTimeline?.paintBeatAim?.();
      m.raf=requestAnimationFrame(tick);
    };
    m.raf=requestAnimationFrame(tick);
    return m;
  }
  function valid(m=current) {
    return Boolean(m?.scroll.isConnected&&m.canvas===$('midi-canvas')&&m.owner===AstraProjectEditor.workingViewKey()
      &&m.zoom===zoom&&m.pitchZoom===pitchZoom&&m.axisMode===rulerMode&&m.ratio===canvasRatio()
      &&m.holderWidth===m.workspace.clientWidth&&m.beforeHeight===m.scroll.clientHeight
      &&!AstraProjectEditor.inspectorCollapsed()&&!window.AstraRhythmMotion?.active()
      &&!matchMedia('(prefers-reduced-motion: reduce)').matches);
  }
  function ownsSize(scroll) {
    return current?.scroll===scroll&&valid()&&scroll.clientWidth===current.geometry.width;
  }
  function rect(target) {
    const r=target?.getBoundingClientRect?.(),m=current;
    if(!r||!m||target!==m.canvas&&!m.layers.includes(target))return r;
    const left=m.geometry.left;
    return {x:left,y:r.top,left,top:r.top,width:m.geometry.width,height:r.height,right:m.geometry.right,bottom:r.bottom};
  }
  return {box,start,stop,rect,valid,ownsSize,get current(){return current;}};
})();

function rollPointerRect(target){return AstraRollModeMotion.rect(target);}
