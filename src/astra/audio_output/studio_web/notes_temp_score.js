"use strict";
window.AstraNotesTempScore=(()=>{
 let box,left,img,right,rightPaper,rightSheet,rightEmpty,status,nav,bookmark,call,timer,shown,bridge,button,alignRight=false,zoom=1,rightZoom=1,page=0;
 let rasterTimer=0,documentToken=null,bookReady=false,shownDocument=null,manualPos=false;
 let currentLocators=null,openedLocators=null;
 let warm=null,warmTimer=0,warmTried=null;
 const warmKey=(s,scale)=>JSON.stringify([s.project,s.base_revision,s.start,s.end,s.source_kind,s.committed||null,scale]);
 function dropWarm(reset=false){
  clearTimeout(warmTimer);warmTimer=0;
  const job=warm;warm=null;
  if(job?.token)try{job.owner.cancelScoreRangePreview(job.token);}catch{}
  if(reset)warmTried=null;
 }
 function offerWarm(owner,scope){
  if(box&&!box.hidden)return;
  if(!scope||!owner?.startScoreRangePreview){
   if(warm||warmTimer)dropWarm(true);
   return;
  }
  // Same default CSS width as the initially closed drawer. An open validates
  // its measured scale before using the result; do not send a low-res frame.
  const pixels=Math.max(360,Math.min(740,innerWidth-24))*(window.devicePixelRatio||1);
  const scale=pixels<=960?1:pixels<=1440?1.5:2;
  const key=warmKey(scope,scale);
  if(warmTried===key&&(!warm||warm.owner===owner))return;
  dropWarm();warmTried=key;
  const job={owner,key,scope,scale};
  warm=job;
  warmTimer=setTimeout(()=>{
   warmTimer=0;
   if(warm!==job||box&&!box.hidden)return;
   try{owner.startScoreRangePreview(JSON.stringify({...scope,notes_compare:true,raster_scale:scale,fast_preview_only:true}),raw=>{
    const r=data(raw);
    if(warm!==job){
     // Stale tokens are canceled by their original bridge, never the new one.
     if(r.token)try{owner.cancelScoreRangePreview(r.token);}catch{}
     return;
    }
    if(r.ok&&r.token&&r.document_token){job.token=r.token;job.document=r.document_token}
    else warm=null;
   });}catch{if(warm===job)warm=null;}
  },750);
 }
 let rightSubscribed=false,rightExcerpt=null,rightReqSeq=0,rightResizeTimer=0,rightFrame=0;
 let rightWarmTimer=0;
 const rightVecCache=new Map(),rightNodes=new Map();
 const api=()=>window.AstraNativeScore;
 const q=s=>box.querySelector(s);
 const ICON_SPLIT='<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="3" y="4" width="18" height="16" rx="2.5"/><path d="M12 4v16"/></svg>';
 const ICON_REFRESH='<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 11a7 7 0 1 1 2 6M5 6v5h5"/></svg>';
 const ICON_CLOSE='<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m6 6 12 12M18 6 6 18"/></svg>';
 function make(){
  if(box)return;
  box=document.createElement("section");
  box.className="notes-temp";box.hidden=true;box.tabIndex=-1;
  box.setAttribute("role","dialog");box.setAttribute("aria-modal","false");box.setAttribute("aria-label","临时看谱");
  box.innerHTML=
    '<header class="temp-head">'+
      '<div class="temp-head-title"><strong>临时看谱</strong><span class="temp-status" role="status"></span></div>'+
      '<div class="temp-head-actions">'+
        '<button type="button" class="temp-compare-toggle" data-compare aria-expanded="false">'+ICON_SPLIT+'<span>对照原谱</span></button>'+
        '<button type="button" class="temp-icon-btn temp-close-btn" data-close aria-label="关闭临时看谱" title="关闭（Esc）">'+ICON_CLOSE+'</button>'+
      '</div>'+
    '</header>'+
    '<div class="temp-book">'+
      '<section class="temp-left" aria-label="当前 MIDI 预览">'+
        '<div class="temp-toolbar">'+
          '<div class="temp-toolbar-meta"><strong>当前 MIDI</strong><span class="temp-range-chip" data-span hidden></span></div>'+
          '<div class="temp-toolbar-controls">'+
            '<button type="button" class="temp-icon-btn" data-refresh title="刷新当前 MIDI 预览" aria-label="刷新当前 MIDI 预览">'+ICON_REFRESH+'</button>'+
            '<span class="temp-sep" aria-hidden="true"></span>'+
            '<button type="button" class="temp-icon-btn" data-prev aria-label="上一页" title="上一页" disabled>‹</button>'+
            '<output class="temp-pages" data-pages>—</output>'+
            '<button type="button" class="temp-icon-btn" data-next aria-label="下一页" title="下一页" disabled>›</button>'+
            '<span class="temp-sep" aria-hidden="true"></span>'+
            '<button type="button" class="temp-icon-btn" data-less aria-label="缩小左谱" title="缩小左谱">−</button>'+
            '<button type="button" class="temp-fit-btn" data-fit title="恢复适宽">适宽</button>'+
            '<button type="button" class="temp-icon-btn" data-more aria-label="放大左谱" title="放大左谱">+</button>'+
          '</div>'+
        '</div>'+
        '<div class="temp-paper"><img alt="当前 MIDI 的预览乐谱" hidden></div>'+
      '</section>'+
      '<section class="temp-right" hidden aria-label="原谱（只读对照）">'+
        '<div class="temp-toolbar temp-right-bar">'+
          '<div class="temp-toolbar-meta">'+
            '<strong>原谱</strong>'+
            '<span class="astra-compare-ro">只读</span>'+
            '<span class="astra-compare-unsaved" data-right-unsaved hidden title="当前显示包含主乐谱页尚未保存的改动">主谱未保存</span>'+
            '<span class="astra-compare-pill" data-right-pill data-tone="idle" hidden></span>'+
          '</div>'+
          '<div class="temp-toolbar-controls">'+
            '<button type="button" class="temp-icon-btn" data-r-less aria-label="缩小原谱" title="缩小原谱" disabled>−</button>'+
            '<button type="button" class="temp-fit-btn" data-r-fit title="恢复适宽（与左谱谱表字号平衡）" disabled>适宽</button>'+
            '<button type="button" class="temp-icon-btn" data-r-more aria-label="放大原谱" title="放大原谱" disabled>+</button>'+
          '</div>'+
        '</div>'+
        '<div class="temp-paper temp-right-paper is-fit" tabindex="0">'+
          '<div class="temp-right-sheet" data-right-sheet hidden></div>'+
          '<div class="temp-right-empty" data-right-empty hidden></div>'+
        '</div>'+
      '</section>'+
    '</div>';
  document.body.append(box);
  left=q(".temp-paper");img=q("img");right=q(".temp-right");
  rightPaper=q(".temp-right-paper");rightSheet=q("[data-right-sheet]");rightEmpty=q("[data-right-empty]");
  status=q("[role=status]");nav=[q("[data-prev]"),q("[data-next]")];
  q("[data-close]").onclick=()=>close();q("[data-refresh]").onclick=refreshCurrent;
  q("[data-compare]").onclick=()=>drawer(right.hidden);
  nav[0].onclick=()=>flip(-1);nav[1].onclick=()=>flip(1);
  q("[data-less]").onclick=()=>scale(.85);q("[data-more]").onclick=()=>scale(1.18);q("[data-fit]").onclick=()=>scale(0);
  q("[data-r-less]").onclick=()=>scaleRight(.85);q("[data-r-more]").onclick=()=>scaleRight(1.18);q("[data-r-fit]").onclick=()=>scaleRight(0);
  box.addEventListener("keydown",e=>{
   if(e.key==="Escape"&&!e.defaultPrevented){e.preventDefault();e.stopPropagation();close()}
  });
  // Read-only right drawer: block editing shortcuts and cross-page undo/redo/save.
  right.addEventListener("keydown",e=>{
   const k=(e.key||"").toLowerCase();
   if((e.ctrlKey||e.metaKey)&&["+","=","-","0"].includes(e.key)){
    e.preventDefault();e.stopPropagation();
    scaleRight(e.key==="0"?0:e.key==="-"?.85:1.18);return;
   }
   if((e.ctrlKey||e.metaKey)&&["z","y","s","f","x","c","v","a"].includes(k)){
    e.preventDefault();e.stopPropagation();return;
   }
   if(["F2","F6","F7","F8","F9","F10","ContextMenu"].includes(e.key)){
    e.preventDefault();e.stopPropagation();
   }
  },{capture:true});
  rightPaper.addEventListener("contextmenu",e=>{e.preventDefault();e.stopPropagation();});
  // Smooth read-only canvas drag panning inside right excerpt paper
  rightPaper.addEventListener("pointerdown",e=>{
   if(e.pointerType!=="mouse"||(e.button!==0&&e.button!==1))return;
   if(e.button===1)e.preventDefault();
   const startX=e.clientX,startY=e.clientY,sl=rightPaper.scrollLeft,st=rightPaper.scrollTop;
   const a=new AbortController(),o={signal:a.signal};
   rightPaper.setPointerCapture(e.pointerId);
   rightPaper.addEventListener("pointermove",m=>{
    if(m.pointerId===e.pointerId){rightPaper.scrollLeft=sl+startX-m.clientX;rightPaper.scrollTop=st+startY-m.clientY;}
   },o);
   for(const k of ["pointerup","pointercancel","lostpointercapture"])
    rightPaper.addEventListener(k,m=>{if(m.pointerId===e.pointerId)a.abort()},o);
  });
  rightPaper.addEventListener("wheel",e=>{
   if(e.ctrlKey&&!e.altKey&&!e.metaKey){
    e.preventDefault();
    scaleRight(e.deltaY<0?1.12:1/1.12);
   }
  },{passive:false});
  const bar=q("header");
  bar.addEventListener("pointerdown",e=>{
   if(!e.isPrimary||e.button!==0||e.target.closest("button"))return;
   const a=new AbortController(),o={signal:a.signal},r=box.getBoundingClientRect();
   const x=e.clientX-r.left,y=e.clientY-r.top;
   e.preventDefault();bar.setPointerCapture(e.pointerId);manualPos=true;
   bar.addEventListener("pointermove",m=>{if(m.pointerId===e.pointerId)clamp(m.clientX-x,m.clientY-y)},o);
   for(const k of ["pointerup","pointercancel","lostpointercapture"])
    bar.addEventListener(k,m=>{if(m.pointerId===e.pointerId)a.abort()},o);
  });
  const onBoxResize=()=>{
   if(box.hidden)return;
   clamp();queueRaster();
    if(!right.hidden&&rightExcerpt)queueRightPaint(100);
  };
  window.addEventListener("resize",onBoxResize);
  if(typeof ResizeObserver==="function")new ResizeObserver(onBoxResize).observe(box);
  function watchDpr(){
   const m=window.matchMedia?.("(resolution: "+window.devicePixelRatio+"dppx)");
   m?.addEventListener?.("change",()=>{watchDpr();queueRaster();if(!right.hidden&&rightExcerpt)queueRightPaint(100);},{once:true});
  }
  watchDpr();
  ensureRightSubscription();
 }
 function ensureRightSubscription(){
  if(rightSubscribed||typeof api()?.onCompareExcerpt!=="function")return;
  rightSubscribed=true;
  api().onCompareExcerpt(raw=>onRightExcerptReply(raw));
 }
 function clamp(x,y){
  const r=box.getBoundingClientRect();
  const targetX=x??(manualPos?r.left:Math.round((innerWidth-r.width)/2));
  const targetY=y??(manualPos?r.top:Math.round((innerHeight-r.height)/2));
  box.style.left=Math.max(10,Math.min(Math.max(10,innerWidth-r.width-10),targetX))+"px";
  box.style.top=Math.max(10,Math.min(Math.max(10,innerHeight-r.height-10),targetY))+"px";
 }
 function scale(n){
  zoom=n?Math.max(.55,Math.min(2.6,zoom*n)):1;
  img.style.width=(zoom*100)+"%";
  q("[data-fit]").textContent=Math.abs(zoom-1)<0.02?"适宽":Math.round(zoom*100)+"%";
  queueRaster();
 }
 function scaleRight(n){
  rightZoom=n?Math.max(.60,Math.min(2.4,rightZoom*n)):1;
  const isFit=Math.abs(rightZoom-1)<0.02;
  if(isFit)rightZoom=1;
  q("[data-r-fit]").textContent=isFit?"适宽":Math.round(rightZoom*100)+"%";
  rightPaper.classList.toggle("is-fit",isFit);
  rightSheet.style.width=(rightZoom*100)+"%";
  if(rightExcerpt)queueRightPaint(100);
 }
 function queueRightPaint(delay=0){
  clearTimeout(rightResizeTimer);rightResizeTimer=0;
  const schedule=()=>{
   rightResizeTimer=0;
   if(!rightFrame)rightFrame=requestAnimationFrame(()=>{rightFrame=0;renderRightExcerpt();});
  };
  if(delay)rightResizeTimer=setTimeout(schedule,delay);
  else schedule();
 }
 function setRightControls(ready){
  for(const sel of ["[data-r-less]","[data-r-fit]","[data-r-more]"])q(sel).disabled=!ready;
 }
 function setRightPill(tone,label,detail=""){
  const pill=q("[data-right-pill]");
  if(!pill)return;
  pill.dataset.tone=tone||"idle";
  pill.textContent=label||"";
  pill.title=detail||label||"";
  pill.hidden=!label;
 }
 function stopRightWarm(){
  clearTimeout(rightWarmTimer);rightWarmTimer=0;
 }
 function queueRightWarm(){
  stopRightWarm();
  const scope=bookmark;
  rightWarmTimer=setTimeout(()=>{
   rightWarmTimer=0;
   if(!scope||scope!==bookmark||box.hidden||!right.hidden)return;
   api()?.requestCompareExcerpt?.({
    project:scope.project,clientTicket:++rightReqSeq,warmOnly:true,
    leftRevision:scope.base_revision,startSeconds:scope.start,endSeconds:scope.end
   });
  },160);
 }
 function invalidateRight(){
  stopRightWarm();
  ++rightReqSeq;
  clearTimeout(rightResizeTimer);rightResizeTimer=0;
  if(rightFrame)cancelAnimationFrame(rightFrame);rightFrame=0;
  api()?.cancelCompareExcerpt?.();
  rightExcerpt=null;
  rightPaintKey=null;
  rightPublishedReceipt=null;
  rightSheet.replaceChildren();
  rightSheet.hidden=true;
  q("[data-right-unsaved]").hidden=true;
  for(const key of ["rightRevision","rightMode","rightPhase","rightCoreBars","rightContextBars","rightDocument"])
   delete right.dataset[key];
  setRightControls(false);
 }
 let rightPaintKey=null,rightPublishedReceipt=null;
 function onRightExcerptReply(raw){
  if(!box||box.hidden||right.hidden)return;
  const res=data(raw);
  if(!res||!bookmark||res.project!==bookmark.project||res.clientTicket!==rightReqSeq)return;
  if(!res.ok){
   rightExcerpt=null;rightPublishedReceipt=null;
   rightPaintKey=null;
   right.dataset.rightMode="error";
   right.dataset.rightPhase="final";
   setRightPill("warn","未能读取原谱",res.error||"");
   q("[data-right-unsaved]").hidden=true;
   setRightControls(false);
   rightSheet.hidden=true;rightEmpty.hidden=false;
   rightEmpty.textContent=res.error||"当前工程尚无已生成的正式乐谱。";
   return;
  }
  if(Array.isArray(res.strips)){
   for(const s of res.strips){
    if(s.vector&&s.vectorKey){
     const ck=bookmark.project+"|"+s.vectorKey;
     rightVecCache.set(ck,window.AstraVectorPage.prepare(s.vector));
     if(rightVecCache.size>24){
      const firstKey=rightVecCache.keys().next().value;
      if(firstKey&&firstKey!==ck)rightVecCache.delete(firstKey);
     }
    }
   }
  }
  if(typeof res.documentKey!=="string"||!res.documentKey)return;
  rightExcerpt=res;
  queueRightPaint();
 }
 function publishRightPaint(res){
  if(rightPublishedReceipt===res)return;
  rightPublishedReceipt=res;
  right.dataset.rightDocument=res.documentKey;
  right.dataset.rightRevision=String(res.revision||0);
  right.dataset.rightMode=String(res.mode||"");
  right.dataset.rightPhase=String(res.phase||"");
  right.dataset.rightCoreBars=Array.isArray(res.coreBars)?res.coreBars.join("-"):"";
  right.dataset.rightContextBars=Array.isArray(res.contextBars)?res.contextBars.join("-"):"";
  right.dataset.loading=String(res.phase==="provisional");
  q("[data-right-unsaved]").hidden=res.hasUnsavedUserEdits!==true;
  const tone=res.phase==="provisional"?"busy":res.mode==="exact"?"ok":res.mode==="aligned"?"mapped":"muted";
  setRightPill(tone,res.label||"",res.detail||"");
  setRightControls(true);
 }
 function renderRightExcerpt(){
  if(!box||box.hidden||right.hidden)return;
  if(!rightExcerpt||!rightExcerpt.strips?.length||!window.AstraVectorPage?.drawCropped){
   rightSheet.hidden=true;rightEmpty.hidden=false;
   if(!rightEmpty.textContent)rightEmpty.textContent="正在读取原谱对照片段…";
   return;
  }
  const backing=window.devicePixelRatio||1;
  const nextKey=JSON.stringify([rightExcerpt.clientTicket,rightExcerpt.phase,rightExcerpt.documentKey,rightExcerpt.revision,
    rightZoom,rightPaper.clientWidth,backing,(rightExcerpt.strips||[]).map(s=>[s.vectorKey,s.cropTop,s.cropBottom,s.coreBands])]);
  if(nextKey===rightPaintKey){
   if(!rightSheet.hidden&&rightSheet.childElementCount)publishRightPaint(rightExcerpt);
   return;
  }
  rightPaintKey=nextKey;
  const priorPosition=rightPaper.scrollTop;
  const sameIntent=rightSheet.dataset.ticket===String(rightExcerpt.clientTicket);
  rightSheet.dataset.ticket=String(rightExcerpt.clientTicket);
  rightEmpty.hidden=true;rightSheet.hidden=false;
  const isFit=Math.abs(rightZoom-1)<0.02;
  rightPaper.classList.toggle("is-fit",isFit);
  rightSheet.style.width=(rightZoom*100)+"%";
  const availW=Math.max(240,(rightPaper.clientWidth-28)*rightZoom);
  const dpr=window.devicePixelRatio||1;
  const children=[];
  let prevPage=null,firstBand=true;
  const highlighted=rightExcerpt.phase==="final"&&(rightExcerpt.mode==="exact"||rightExcerpt.mode==="aligned");
  for(const strip of rightExcerpt.strips){
   const vec=rightVecCache.get(bookmark.project+"|"+strip.vectorKey);
   if(!vec)continue;
   if(prevPage!==null&&prevPage!==strip.page){
    const marker="separator:"+prevPage+":"+strip.page;
    let sep=[...rightSheet.children].find(node=>node.dataset.sep===marker);
    if(!sep){
     sep=document.createElement("div");sep.dataset.sep=marker;sep.className="temp-right-page-sep";
     const text=document.createElement("span");text.textContent='第 '+prevPage+' 页 · 接第 '+strip.page+' 页';sep.append(text);
    }
    children.push(sep);
   }
   prevPage=strip.page;
   const clip=Array.isArray(vec.clip)&&vec.clip.length===4?vec.clip:[0,0,960,vec.h||1358];
   const clipX=clip[0],clipW=clip[2],cropTop=strip.cropTop,cropBottom=strip.cropBottom;
   const cropH=Math.max(40,cropBottom-cropTop);
   const k=Math.min(2.8,Math.max(.75,(availW*dpr)/clipW),Math.sqrt(2_000_000/(clipW*cropH)));
   const id=strip.vectorKey+":"+cropTop+":"+cropBottom;
   let slot=rightNodes.get(id);
   if(!slot){
    const wrap=document.createElement("div"),cv=document.createElement("canvas");
    wrap.className="temp-right-strip-wrap";wrap.dataset.page=String(strip.page);
    cv.className="temp-right-strip";cv.style.aspectRatio=clipW+" / "+cropH;wrap.append(cv);
    slot={wrap,cv,pixels:"",bands:"",layer:null};
   }
   rightNodes.delete(id);
   rightNodes.set(id,slot);
   if(rightNodes.size>12)rightNodes.delete(rightNodes.keys().next().value);
   const w=Math.max(1,Math.round(clipW*k)),h=Math.max(1,Math.round(cropH*k));
   const pixels=w+":"+h;
   if(slot.pixels!==pixels){
    slot.cv.width=w;slot.cv.height=h;
    window.AstraVectorPage.drawCropped(slot.cv,vec,k,cropTop,cropBottom);
    slot.pixels=pixels;
   }
   const bands=[];
   if(highlighted&&Array.isArray(strip.coreBands)){
    for(const b of strip.coreBands){
     const x0=Math.max(clipX,b.x0),x1=Math.min(clipX+clipW,b.x1);
     const y0=Math.max(cropTop,b.y0),y1=Math.min(cropBottom,b.y1);
     if(!(x1>x0&&y1>y0))continue;
     bands.push([(x0-clipX)*100/clipW,(y0-cropTop)*100/cropH,
                 (x1-x0)*100/clipW,(y1-y0)*100/cropH,firstBand]);
     firstBand=false;
    }
   }
   const bandKey=bands.length?JSON.stringify([rightExcerpt.mode,bands]):"";
   if(bandKey!==slot.bands){
    slot.layer?.remove();slot.layer=null;slot.bands=bandKey;
    if(bands.length){
     const layer=document.createElement("div");layer.className="astra-compare-hl-layer";
     layer.setAttribute("aria-hidden","true");
     for(const [x,y,w,h,first] of bands){
      const div=document.createElement("div");
      div.className="astra-compare-band"+(first?" is-start":"")+(rightExcerpt.mode==="aligned"?" is-time-mapped":"");
      div.dataset.page=String(strip.page);
      Object.assign(div.style,{left:x.toFixed(3)+"%",top:y.toFixed(3)+"%",width:w.toFixed(3)+"%",height:h.toFixed(3)+"%"});
      layer.append(div);
     }
     slot.wrap.append(layer);slot.layer=layer;
    }
   }
   children.push(slot.wrap);
  }
  if(children.length!==rightSheet.children.length||children.some((node,i)=>rightSheet.children[i]!==node))
   rightSheet.replaceChildren(...children);
  if(!children.length){
   rightEmpty.textContent="原谱片段尚未就绪。";
   rightEmpty.hidden=false;rightSheet.hidden=true;
   setRightControls(false);
   return;
  }
  publishRightPaint(rightExcerpt);
  rightPaper.scrollTop=sameIntent?priorPosition:0;
  if(isFit)rightPaper.scrollLeft=0;
 }
 function requestRightExcerpt(r){
  if(!bookmark||right.hidden)return;
  stopRightWarm();
  ensureRightSubscription();
  const target=r?.compare_target||r?.context_range||null;
  const startSeconds=target?.start_seconds??target?.start??bookmark.start;
  const endSeconds=target?.end_seconds??target?.end??bookmark.end;
  const startBeat=target?.start_beat??null;
  const endBeat=target?.end_beat??null;
  const cachedKeys=[];
  const prefix=bookmark.project+"|";
  for(const k of rightVecCache.keys())if(k.startsWith(prefix))cachedKeys.push(k.slice(prefix.length));
  right.dataset.loading="true";
  if(!rightExcerpt){
   setRightPill("busy","正在载入原谱…");
   rightEmpty.hidden=false;rightEmpty.textContent="正在读取原谱对应片段…";
  }else{
   setRightPill("busy","正在定位…");
  }
  const clientTicket=++rightReqSeq;
  api()?.requestCompareExcerpt?.({
   project:bookmark.project,clientTicket,referenceOnly:!r,
   leftRevision:bookmark.base_revision,
   startSeconds,endSeconds,startBeat,endBeat,
   clockDigest:target?.clock_digest||null,
   cachedVectorKeys:cachedKeys
  });
 }
 function density(){
  const padding=getComputedStyle(left);
  const contentWidth=left.clientWidth-(parseFloat(padding.paddingLeft)||0)-(parseFloat(padding.paddingRight)||0);
  const width=(!img.hidden&&img.getBoundingClientRect().width)||Math.max(0,contentWidth*zoom);
  const pixels=width*(window.devicePixelRatio||1);
  return pixels<=960?1:pixels<=1440?1.5:2;
 }
 function requested(){return {index:page,rasterScale:density()}}
 function queueRaster(delay=105){
  clearTimeout(rasterTimer);rasterTimer=0;
  if(!box?.hidden&&bookReady)rasterTimer=setTimeout(replay,delay);
 }
 function desired(c){return c.index===page&&c.rasterScale===density()}
 function refreshNav(){
  const pages=shownDocument===documentToken?shown?.page?.pages:0;
  nav[0].disabled=!pages||page===0;nav[1].disabled=!pages||page+1>=pages;
 }
 function replay(){
  rasterTimer=0;
  if(!call||!documentToken||!bookReady||box.hidden||desired(call))return;
  const c={...bookmark,...requested(),pageOnly:true,documentToken};
  clearTimeout(timer);clearCallWatchdog(call);timer=0;call=c;
  if(shownDocument===documentToken&&shown?.page?.index!==c.index){note("正在翻页");refreshNav()}
  requestPreviewRpc(c,"scoreRangePreviewPage",JSON.stringify({token:c.documentToken,index:c.index,raster_scale:c.rasterScale}),raw=>reply(raw,c),true);
 }
 function note(s,stale=false){
  status.textContent=s;box.dataset.stale=String(stale);nav.forEach(b=>b.disabled=true);
 }
 function data(x){try{return JSON.parse(x)}catch{return {}}}
 function cancelPreviewToken(token){
  if(token)try{bridge.cancelScoreRangePreview(token);}catch{}
 }
 function clearCallWatchdog(c){
  if(c){clearTimeout(c.watchdog);c.watchdog=0;}
 }
 function previewWatchdogFailed(c){
  if(c!==call||box.hidden)return;
  const cached=!!shown;
  cancel();
  note(cached?"预览未响应 · 显示上次预览":"预览未响应 · 请点刷新重试",true);
 }
 function requestPreviewRpc(c,method,arg,read,returnsToken=false){
  clearCallWatchdog(c);
  // Bound the RPC callback, not DuET prediction. A healthy running worker
  // continues polling regardless of how long cold inference takes.
  const watchdog=c.watchdog=setTimeout(()=>previewWatchdogFailed(c),12000);
  let received=false;
  const done=raw=>{
   if(received)return;received=true;
   if(c!==call||c.watchdog!==watchdog){
    const token=returnsToken?data(raw).token:null;
    cancelPreviewToken(token);
    return;
   }
   clearCallWatchdog(c);read(raw);
  };
  try{bridge[method](arg,done);}
  catch{if(c.watchdog===watchdog)previewWatchdogFailed(c);}
 }
 function cancel(){
  clearTimeout(timer);clearTimeout(rasterTimer);clearCallWatchdog(call);timer=0;rasterTimer=0;
  const token=documentToken||call?.token;call=null;documentToken=null;bookReady=false;
  cancelPreviewToken(token);
 }
 function fail(c,errMsg=""){
  if(c.warmed&&c===call&&!box.hidden){refresh(0);return}
  if(c.pageOnly&&shownDocument===documentToken&&shown){
   const failedFlip=c.index!==shown.page.index;
   page=shown.page.index;
   note(failedFlip?"未能翻页":"清晰度未更新");refreshNav();
   return;
  }
  const clean=typeof errMsg==="string"&&errMsg.trim()?errMsg.trim():"";
  note(clean||(shown?"本次未生成 · 显示上次预览":"暂时无法生成预览"),true);
 }
 function reply(raw,c){
  const x=data(raw);
  if(c!==call){cancelPreviewToken(x.token);return}
  if(!x.ok||!x.token||!x.document_token||(c.pageOnly&&x.document_token!==c.documentToken)){fail(c,x.error);return}
  c.token=x.token;documentToken=x.document_token;poll(c);
 }
 function align(_c,r){
  if(!alignRight||right.hidden||!r||shownDocument!==documentToken||box.dataset.stale==="true")return;
  alignRight=false;
  requestRightExcerpt(r);
 }
 function poll(c){
  requestPreviewRpc(c,"scoreRangePreviewStatus",c.token,raw=>{
   if(c!==call||box.hidden)return;
   const x=data(raw);
   if(["queued","running","validating"].includes(x.status)){
     const progress=x.progress;
     if(!c.pageOnly&&progress?.phase){
      const detail=progress.phase==="prediction"?
        (progress.total>0?`DuET 正在识谱 · ${progress.completed}/${progress.total}`:"正在启动 DuET…"):
        (progress.phase==="engraving"?"正在整理谱面…":"正在绘制所选范围…");
      if(status.textContent!==detail)note(detail,!!shown);
     }
     timer=setTimeout(()=>poll(c),140);return
    }
   const r=x.result,p=r?.page,u=p?.dataUrl;
   if(x.status!=="ready"||r?.validated_revision!==c.base_revision||
    r?.requested_range?.start!==c.start||r?.requested_range?.end!==c.end||
    p?.index!==c.index||!Number.isSafeInteger(p.pages)||p.pages<=c.index||
    p.rasterScale!==c.rasterScale||p.width!==960*c.rasterScale||
    !Number.isSafeInteger(p.logicalHeight)||p.logicalHeight<480||p.logicalHeight>1800||
    p.height!==Math.round(p.logicalHeight*c.rasterScale)||
    !/^data:image\/png;base64,[A-Za-z0-9+/]+={0,2}$/.test(u||"")){fail(c,x.error);return}
   bookReady=true;
   if(!desired(c)){queueRaster(0);return}
   const next=new Image();
   c.watchdog=setTimeout(()=>previewWatchdogFailed(c),12000);
   next.onerror=()=>{
    if(c!==call||box.hidden)return;
    clearCallWatchdog(c);
    if(desired(c))fail(c);
    else queueRaster(0);
   };
   next.onload=()=>{
    if(c!==call||box.hidden)return;
    clearCallWatchdog(c);
    if(!desired(c)){queueRaster(0);return}
    if(next.naturalWidth!==p.width||next.naturalHeight!==p.height){fail(c);return}
    shown=r;shownDocument=documentToken;
    img.style.aspectRatio="960 / "+p.logicalHeight;img.src=u;img.hidden=false;
    const span=c.source_kind==="marquee"?"框选范围":"L/R 定位范围";
    note(span+(r.approximate?" · MIDI 近似草稿":"")+(r.reused_duet_context===true?" · 复用 DuET":""));
    const first=r.context_range.first_measure,last=r.context_range.last_measure;
    const chip=q("[data-span]");
    chip.textContent=first===last?"第 "+first+" 小节":"第 "+first+"–"+last+" 小节";
    chip.hidden=false;
    q("[data-pages]").textContent=(page+1)+" / "+p.pages;
    refreshNav();
    align(c,r);
   };next.src=u;
  });
 }
 function readLocators(){
  const r=currentLocators?.();
  return Number.isFinite(r?.start)&&Number.isFinite(r?.end)?{start:r.start,end:r.end}:null;
 }
 function refreshCurrent(){
  if(!bookmark||box.hidden)return;
  const next=readLocators();
  const changed=next&&(next.start!==openedLocators?.start||next.end!==openedLocators?.end);
  if(currentLocators&&(bookmark.source_kind==="locators"||changed)){
   if(!next||next.end<=next.start+.00001){
    cancel();invalidateRight();note(shown?"请先设好 L/R · 显示上次预览":"请先设好 L/R",!!shown);return;
   }
   bookmark={...bookmark,...next,source_kind:"locators",selection_token:null};
   openedLocators=next;
  }
  refresh(0);
 }
 function refresh(delay=150,allowWarm=false){
  if(!bookmark||box.hidden)return;
  cancel();
  if(!right.hidden){
   invalidateRight();
   alignRight=true;
   requestRightExcerpt(null);
  }
  page=0;note(shown?"正在更新 · 显示上次预览":"正在生成当前 MIDI 谱面",!!shown);
  const scope={...bookmark},c={...scope,index:0}; call=c;
  const warmScale=allowWarm?density():null;
  const cached=allowWarm&&warm?.owner===bridge&&warm.token&&warm.key===warmKey(scope,warmScale)?warm:null;
  if(cached){
   warm=null;c.warmed=true;c.rasterScale=warmScale;
   c.token=cached.token;documentToken=cached.document;
   poll(c);return;
  }
  if(warm)dropWarm();
  timer=setTimeout(()=>{
   if(c!==call||box.hidden)return;
   c.rasterScale=density();
   requestPreviewRpc(c,"startScoreRangePreview",JSON.stringify({...scope,notes_compare:true,raster_scale:c.rasterScale}),raw=>reply(raw,c),true);
  },delay);
 }
 function flip(d){
  if(!shown||shownDocument!==documentToken||page+d<0||page+d>=shown.page.pages)return;
  page+=d;
  clearTimeout(rasterTimer);replay();
 }
 function drawer(open){
  right.hidden=!open;
  const toggle=q("[data-compare]");
  toggle.setAttribute("aria-expanded",String(open));
  const lbl=toggle.querySelector("span");if(lbl)lbl.textContent=open?"收起原谱":"对照原谱";
  box.classList.toggle("is-compared",open);
  if(open){
   alignRight=true;
   if(shown&&shownDocument===documentToken&&box.dataset.stale!=="true")align(null,shown);
   else requestRightExcerpt(null);
  }else{
   invalidateRight();
   if(right.contains(document.activeElement))toggle.focus({preventScroll:true});
  }
  clamp();queueRaster();
 }
 function close(restore=true){
  if(!box||box.hidden)return;
  drawer(false);cancel();alignRight=false;box.hidden=true;
  if(restore&&button?.isConnected)button.focus({preventScroll:true});
 }
 function open(b,p,trigger,locators){
  if(!b||!p.project||!(typeof p.base_revision==="string"&&!!p.base_revision)||!Number.isFinite(p.start)||!Number.isFinite(p.end)||p.start>=p.end)return false;
  make();bridge=b;button=trigger;
  const wasHidden=box.hidden;
  if(bookmark?.project!==p.project){
   if(!box.hidden)close(false);
   shown=null;shownDocument=null;rightExcerpt=null;rightVecCache.clear();rightNodes.clear();
   img.hidden=true;img.removeAttribute("src");
   rightSheet.hidden=true;rightSheet.replaceChildren();rightEmpty.hidden=true;
   const chip=q("[data-span]");if(chip)chip.hidden=true;
  }
  if(wasHidden)manualPos=false;
  currentLocators=typeof locators==="function"?locators:null;openedLocators=readLocators();
  bookmark={...p};alignRight=true;box.hidden=false;
  clamp();
  if(wasHidden)box.focus({preventScroll:true});
  refresh(0,wasHidden);
  if(wasHidden&&right.hidden)queueRightWarm();
  return true;
 }
 function sync(project,revision,visible){
  if(!box||box.hidden)return;
  if(!visible||bookmark.project!==project){close(false);return}
  if(typeof revision==="string"&&revision&&revision!==bookmark.base_revision){bookmark={...bookmark,base_revision:revision};alignRight=true;refresh()}
 }
 return {open,sync,offerWarm,beforeWorkspace:t=>{if(t!=="notes"){dropWarm(true);close(false);}},preserved:()=>null};
})();
// A whole-song Update Score from unsaved Notes is not the range comparer or
// the editable right-hand document. Only the host's current receipt can paint.
window.AstraNotesScoreDraft=(()=>{
 let box=null,img=null,status=null,label=null,prev=null,next=null;
 let current=null,call=null,view=null,shown=null,button=null,timer=0;
 const parse=raw=>{try{return JSON.parse(raw)}catch{return {}}};
 function setup(){
   if(box)return;
   box=document.createElement('section');
   box.className='notes-temp notes-score-draft';box.hidden=true;
   box.setAttribute('role','dialog');box.setAttribute('aria-modal','false');
   box.setAttribute('aria-label','本次生成的临时谱面');
   box.innerHTML='<header><strong>本次生成的谱面</strong><span role="status"></span><button type="button" data-close aria-label="关闭本次谱面预览">×</button></header>'+
    '<div class="temp-book"><section class="temp-left"><div class="temp-toolbar"><strong>只读预览</strong><button type="button" data-prev aria-label="上一页">‹</button><small data-page></small><button type="button" data-next aria-label="下一页">›</button></div>'+
    '<div class="temp-paper"><img alt="本次生成的临时谱面" hidden></div></section></div>';
   document.body.append(box);
   img=box.querySelector('img');status=box.querySelector('[role=status]');label=box.querySelector('[data-page]');
   prev=box.querySelector('[data-prev]');next=box.querySelector('[data-next]');
   prev.onclick=()=>{if(shown)paint(shown.index-1)};next.onclick=()=>{if(shown)paint(shown.index+1)};
   box.querySelector('[data-close]').onclick=()=>close();
   box.addEventListener('keydown',e=>{
     if(e.key==='Escape'){e.preventDefault();e.stopPropagation();close()}
     // This read-only surface leaves Ctrl+S to the Notes project.
     else if((e.ctrlKey||e.metaKey)&&['z','y'].includes(e.key?.toLowerCase())){
       e.preventDefault();e.stopPropagation();
     }
   });
 }
 function rpc(b,w,callback=()=>{}){b.notesScoreDraftView(JSON.stringify(w),raw=>callback(parse(raw)))}
 function clear(){
   clearTimeout(timer);timer=0;call=null;
   if(view?.key)rpc(view.bridge,{action:'cancel',token:view.token,key:view.key});
   view=null;
 }
 function close(restore=true){
   if(!box||box.hidden)return;
   clear();box.hidden=true;shown=null;img.hidden=true;img.removeAttribute('src');
   if(restore&&button?.isConnected)button.focus({preventScroll:true});
   button=null;
 }
 function same(c){return c===call&&!box.hidden&&current?.token===c.token&&current?.revision===c.revision}
 function controls(){
   prev.disabled=Boolean(call)||!(shown?.index>0);
   next.disabled=Boolean(call)||!(shown&&shown.index+1<shown.pages);
   label.textContent=shown?(shown.index+1)+' / '+shown.pages:'';
 }
 function idleText(){
   if(current?.phase==='publish-failed')return '工程已保存 · 正式谱尚未更新';
   if(current?.phase==='retry-wait'||current?.phase==='publishing')return '工程已保存 · 正在发布谱面';
   return '临时生成 · 保存工程后发布';
 }
 function fail(c){
   if(!same(c))return;
   call=null;status.textContent='无法读取预览 · 正式谱未改变';controls();
 }
 function poll(c){
   if(!same(c))return;
   rpc(c.bridge,{action:'status',token:c.token,key:c.key},reply=>{
     if(!same(c))return;
     if(reply.ok&&reply.status==='running'){timer=setTimeout(()=>poll(c),140);return}
     const p=reply.page;
     if(!reply.ok||reply.status!=='ready'||p?.index!==c.index||!Number.isSafeInteger(p?.pages)||p.pages<=p.index||
        p?.rasterScale!==c.scale||p.width!==960*c.scale||!Number.isSafeInteger(p?.logicalHeight)||
        p.logicalHeight<480||p.logicalHeight>1800||p.height!==Math.round(p.logicalHeight*c.scale)||
        !/^data:image\/png;base64,[A-Za-z0-9+/]+={0,2}$/.test(p?.dataUrl||'')){fail(c);return}
     const checked=new Image();
     checked.onerror=()=>fail(c);
     checked.onload=()=>{
       if(!same(c))return;
       if(checked.naturalWidth!==p.width||checked.naturalHeight!==p.height){fail(c);return}
       shown=p;img.style.aspectRatio='960 / '+p.logicalHeight;img.src=p.dataUrl;img.hidden=false;
       call=null;status.textContent=idleText();controls();
     };
     checked.src=p.dataUrl;
   });
 }
 function paint(index){
   if(!current||!view||call||index<0||(shown&&index>=shown.pages))return;
   const width=(box.querySelector('.temp-paper').clientWidth||960)*(window.devicePixelRatio||1);
   const scale=width<=960?1:width<=1440?1.5:2;
   const c={...view,revision:current.revision,index,scale};call=c;status.textContent='正在读取临时谱面…';controls();
   rpc(c.bridge,{action:'page',token:c.token,index,raster_scale:scale},reply=>{
     if(!same(c)){if(reply.key)rpc(c.bridge,{action:'cancel',token:c.token,key:reply.key});return}
     if(!reply.ok||!reply.key){fail(c);return}
     c.key=reply.key;view.key=reply.key;poll(c);
   });
 }
 function sync(next,project,revision,visible){
   const accepted=visible&&next?.preview&&next.project===project&&next.revision===revision?next:null;
   if(!accepted||current?.token!==accepted.token)close(false);
   current=accepted;
   if(box&&!box.hidden&&!call)status.textContent=idleText();
 }
 function open(next,bridge,trigger){
   if(!next?.preview||!bridge?.notesScoreDraftView)return;
   close(false);setup();current=next;button=trigger;box.hidden=false;
   view={bridge,token:next.token};box.querySelector('[data-close]').focus({preventScroll:true});
   paint(0);
 }
 return {open,sync,beforeWorkspace:t=>{if(t!=='notes')close(false)}};
})();
