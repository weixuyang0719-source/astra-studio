(function(root){
'use strict';
function install(doc,win,api){
 let drag=null,suppressed=null,frame=null;
 const raf=win.requestAnimationFrame?.bind(win) || (fn=>win.setTimeout(fn,16));
 const cancelFrame=()=>{if(frame!==null){(win.cancelAnimationFrame?.bind(win)||win.clearTimeout?.bind(win))?.(frame);frame=null;}};
 function paint(){frame=null;if(drag)api.preview(drag.key,drag.position);}
 function commit(){cancelFrame();api.seek(drag.position,true);api.preview(drag.key,drag.position,true);}
 function schedulePaint(){if(frame===null)frame=raf(paint);}
 const clip=e=>e.target?.closest?.('.clip[data-do="clip"]');
 let hover=null,hoverFrame=null,rowHover=null;
 function clearRowHover(){if(rowHover){rowHover.removeAttribute('data-pointer-hover');rowHover=null;}}
 function rowHoverMove(e){
  const row=!drag&&!e.buttons&&['mouse','pen'].includes(e.pointerType)?e.target?.closest?.('.track-row'):null;
  if(row===rowHover)return;
  clearRowHover();if(row){rowHover=row;row.setAttribute('data-pointer-hover','');}
 }
 // DOM replacement must not retain a detached hovered row.
 if(win.MutationObserver){new win.MutationObserver(()=>{if(rowHover&&!rowHover.isConnected)clearRowHover();if(hover&&!hover.el.isConnected)clearHover();}).observe(doc.documentElement,{childList:true,subtree:true});}
 doc.addEventListener('pointermove',rowHoverMove,{passive:true});
 doc.addEventListener('pointerout',e=>{if(rowHover&&!rowHover.contains(e.relatedTarget))clearRowHover();},{passive:true});
 doc.addEventListener('pointercancel',()=>{clearRowHover();clearHover();},true);
 doc.addEventListener('pointerdown',clearRowHover,true);
 doc.addEventListener('scroll',clearRowHover,true);
 win.addEventListener('resize',clearRowHover);
 win.addEventListener('blur',e=>{if(e.target===win)clearRowHover();});
 function clearHover(){
  if(hoverFrame!==null){(win.cancelAnimationFrame?.bind(win)||win.clearTimeout?.bind(win))?.(hoverFrame);hoverFrame=null;}
  if(hover){hover.el.removeAttribute('data-seek-hover');hover.el.style.removeProperty('--seek-hover-x');hover=null;}
 }
 function hoverMove(e){
  if(drag||e.buttons||!['mouse','pen'].includes(e.pointerType)){clearHover();return;}
  const el=clip(e);if(!el||!(api.duration(el.dataset.key)>0)){clearHover();return;}
  if(hover?.el!==el){clearHover();hover={el,x:e.clientX};}else hover.x=e.clientX;
  if(hoverFrame===null)hoverFrame=raf(()=>{
   hoverFrame=null;if(!hover||!hover.el.isConnected){clearHover();return;}
   const r=hover.el.getBoundingClientRect();if(!r.width){clearHover();return;}
   hover.el.style.setProperty('--seek-hover-x',Math.max(0,Math.min(r.width-1,hover.x-r.left))+'px');
   hover.el.setAttribute('data-seek-hover','');
  });
 }
 doc.addEventListener('pointermove',hoverMove,{passive:true});
 doc.addEventListener('pointerout',e=>{if(hover&&!hover.el.contains(e.relatedTarget))clearHover();},{passive:true});
 doc.addEventListener('pointerdown',clearHover,true);
 doc.addEventListener('scroll',clearHover,true);
 win.addEventListener('resize',clearHover);
 win.addEventListener('blur',e=>{if(e.target===win)clearHover();});

 const pos=e=>Math.max(0,Math.min(drag.duration,(e.clientX-drag.rect.left)/drag.rect.width*drag.duration));
 function targetAt(e){
  if(drag.geometryDirty && drag.el.isConnected){
   const rect=drag.el.getBoundingClientRect();
   if(rect.width>0){drag.rect=rect;drag.geometryDirty=false;}
  }
  const r=drag.rect;
  if(!drag.geometryDirty && drag.el.isConnected && e.clientX>=r.left && e.clientX<r.right
    && e.clientY>=r.top && e.clientY<r.bottom)return false;
  const el=doc.elementFromPoint?.(e.clientX,e.clientY)?.closest?.('.clip[data-do="clip"]');
  if(!el)return false;
  const duration=api.duration(el.dataset.key),rect=el.getBoundingClientRect();
  if(!(duration>0&&rect.width>0))return false;
  const changed=drag.key!==el.dataset.key;
  drag.el=el;drag.key=el.dataset.key;drag.duration=duration;drag.rect=rect;drag.geometryDirty=false;
  if(changed){api.select(drag.key);drag.sent=-Infinity;}
  return changed;
 }
 function update(e,final=false){
  if(!drag)return;
  targetAt(e);drag.position=pos(e);
  if(final){commit();return;}
  schedulePaint();
  const now=api.now();
  if(api.playing?.() !== false && now-drag.sent>=60){api.seek(drag.position);drag.sent=now;}
 }
 function finish(e,atRelease=true){
  if(!drag || (e.pointerId!==undefined && e.pointerId!==drag.id))return;
  const d=drag;
  if(d.active || atRelease){
   if(atRelease && Number.isFinite(e.clientX))update(e,true);else commit();
   suppressed={el:d.el,origin:d.origin,id:d.id,until:api.now()+400};
  }
  cancelFrame();drag=null;
  try{d.capture?.releasePointerCapture(d.id);}catch{}
  api.finished?.();
 }
 doc.addEventListener('pointerdown',e=>{
  suppressed=null;
  const el=clip(e);if(!el || e.button!==0 || e.isPrimary===false || drag)return;
  const duration=api.duration(el.dataset.key),rect=el.getBoundingClientRect();
  if(!(duration>0 && rect.width>0))return;
  drag={el,origin:el,key:el.dataset.key,id:e.pointerId,x:e.clientX,y:e.clientY,rect,duration,active:false,position:0,sent:-Infinity,capture:doc.documentElement || el};
  drag.position=pos(e);api.begin?.();api.select(drag.key);paint();
 },true);
 doc.addEventListener('pointermove',e=>{
  if(!drag || drag.id!==e.pointerId)return;
  if(!drag.active){
   if(Math.hypot(e.clientX-drag.x,e.clientY-drag.y)<3)return;
   drag.active=true;
   try{drag.capture.setPointerCapture(drag.id);}catch{}
  }
  e.preventDefault();e.stopPropagation();update(e);
 },true);
 function invalidateGeometry(){if(drag)drag.geometryDirty=true;}
 doc.addEventListener('scroll',invalidateGeometry,true);
 win.addEventListener('resize',invalidateGeometry);
 win.addEventListener('pointerup',finish,true);
 win.addEventListener('pointercancel',e=>finish(e,false),true);
 win.addEventListener('lostpointercapture',e=>{if(drag && e.target===drag.capture)finish(e,false);},true);
 win.addEventListener('blur',e=>{if(e.target===win)finish({},false);},true);
 doc.addEventListener('click',e=>{
  if(suppressed && (clip(e)===suppressed.el||clip(e)===suppressed.origin) && e.detail!==0 && api.now()<suppressed.until && (e.pointerId===undefined || e.pointerId===suppressed.id)){
   suppressed=null;e.preventDefault();e.stopImmediatePropagation();
  }
 },true);
 return {position:()=>drag?drag.position:null,cancel:()=>{
  const d=drag;if(!d)return;
  cancelFrame();drag=null;
  try{d.capture?.releasePointerCapture(d.id);}catch{}
  api.finished?.();
 }};
}
if(typeof module!=='undefined')module.exports={install};
else root.AstraWaveSeek=install(document,window,{
 now:()=>performance.now(),
 begin:beginWaveSeek,
 finished:finishWaveSeek,
 playing:()=>!!state.player.playing,
 duration:key=>Number(sourceList().find(s=>s.key===key)?.duration || state.player.duration || 0),
 select:key=>{
  // A seek on the already audible lane is not another source selection.
  const selected=typeof audioSelectionPending!=='undefined'&&audioSelectionPending?audioSelectionPending.key:state.player.source;
  if(selected===key&&!['midi','joint'].includes(state.player.audition)&&!state.midi?.playing)return;
  dispatch('selectAudio',{key});
 },
 seek:(position,final)=>dispatch('seek',{position},undefined,true,Boolean(final)),
 preview:(key,position,committed)=>{
  if(!committed)sendWaveSeekVisual(1,position);
  paintPlaybackPosition(position);
  if(typeof paintAudioVisualPosition==='function'){paintAudioVisualPosition(position,true);return;}
  updateAudioCursor();
  // A newly selected lane may not have received its backend snapshot yet.
  // Paint every lane immediately, including initially hidden cursors.
  for(const el of document.querySelectorAll('.clip[data-do="clip"]')){
   const duration=Number(sourceList().find(s=>s.key===el.dataset.key)?.duration || state.player.duration || 0);
   if(!(duration>0))continue;
   const percent=100*Math.max(0,Math.min(1,position/duration));
   const cursor=el.querySelector('.playhead'),progress=el.querySelector('.wave-progress');
   if(cursor){cursor.hidden=false;cursor.style.left=`${percent}%`;cursor.style.transform='none';}
   if(progress)progress.style.width=`${percent}%`;
  }
 }
});
})(globalThis);
