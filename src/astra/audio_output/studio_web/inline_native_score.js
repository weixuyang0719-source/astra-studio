"use strict";
window.AstraNativeScore = (() => {
  let backend, root, project, response, busy, pending, serial = 0, zoom = 1;
  let shownZoom = 1, zoomAnchor = null, zoomFallback = 0, zoomStage = null, zoomRtt = 0, rasterSentAt = 0;
  const rasterZoom = new Map();
  let autoStamp, temporaryFocus=null;
  let nativeDocumentGeneration=0, textAfterInput=null;
  let textQueue=[];
  function inlineText(op,fields={}){
    const packet={op,fields,project,generation:nativeDocumentGeneration};
    const tail=textQueue.at(-1);
    if((op==="extend-to"&&tail?.op===op)||
       (op==="ime"&&typeof fields.preedit==="string"&&tail?.op===op&&typeof tail.fields.preedit==="string")){
      textQueue[textQueue.length-1]=packet;  // latest selection edge/IME preview, never discard committed text
    } else textQueue.push(packet);
    pumpInlineText();
  }
  function pumpInlineText(){
    if(busy||!textQueue.length)return;
    const msg=textQueue.shift();
    if(msg.project!==project||msg.generation!==nativeDocumentGeneration||!root?.isConnected){
      textQueue=[];return;
    }
    if(!response?.editing||response.readOnly||(msg.op!=="start-at"&&!response.textEditing)){
      textQueue=[];return;
    }
    if(!send("text-action",{operation:msg.op,...msg.fields}))textQueue.unshift(msg);
  }
  function inlineCommit(value){
    let chunk="";
    for(const c of value||""){
      if(chunk.length+c.length>150){inlineText("ime",{text:chunk});chunk="";}
      chunk+=c;
    }
    if(chunk)inlineText("ime",{text:chunk});
  }
  function alignInlineInput(){
    const canvas=sheet(), field=root?.querySelector(".astra-native-text-input");
    if(!canvas||!field||!response?.textEditing||!Array.isArray(response.textCaret))return;
    const [x,y]=response.textCaret, doc=canvas.getBoundingClientRect(), host=area().getBoundingClientRect();
    const clamp=(v,lo,hi)=>Math.max(lo,Math.min(v,hi-2));
    field.style.left=clamp(doc.left+x*doc.width/960,host.left,host.right)+"px";
    field.style.top=clamp(doc.top+y*doc.height/(response.frameHeight||1),host.top,host.bottom)+"px";
  }
  const catalogue=window.AstraNativeCatalogue.create({send});
  const paletteRead=k=>k==="palette-catalog" || k==="palette-icons";
  const auxRead=k=>paletteRead(k)||k==="properties"||k==="page-raster"||k==="vector-page"||k==="layout-tree";
  const inspector=window.AstraNativeInspector.create({send});
  let inspectorSelectionStamp=0;
  // Preserve a short run of deliberate pitch taps on this same selected note
  // while native pitch work is already acknowledged in flight. Never queue
  // hardware repeats, pointers, undo, selection, or a different document.
  const queuedPitchTaps=[];
  const pitchTarget=()=>response?.editing&&!response?.input&&!response?.textEditing&&
    !response?.readOnly&&response?.selectionKind==="Note"&&response?.selection?.mode==="single"&&
    (response.canPitch??response.selectedPitch>=0);
  function drainPitchTaps(ok){
    if(!ok){queuedPitchTaps.length=0;return;}
    const next=queuedPitchTaps.shift();
    if(!next)return;
    if(!pitchTarget()||document.activeElement!==area()||!root?.isConnected||
      next.project!==project||next.page!==response.page||next.generation!==nativeDocumentGeneration||
      busy||!send("pitch",next.fields))queuedPitchTaps.length=0;
  }

  // Serialize deliberate entry keys on this one native input session. The
  // worker, not the browser, continues to own cursor, chord and duration.
  const entryLimit=32;
  let entryFlight=null, entryFIFO=[], entryEscape=false;
  function clearEntryKeys(){
    entryFlight=null;entryFIFO=[];entryEscape=false;
  }
  function entryContext(stamp){
    return stamp&&stamp.project===project&&stamp.page===response?.page&&
      stamp.generation===nativeDocumentGeneration&&document.activeElement===area()&&
      root?.isConnected&&response?.editing&&!response.readOnly&&!response.textEditing;
  }
  function sendEntryTool(tool,value){
    const session=project,page=response?.page,generation=nativeDocumentGeneration;
    if(!send("tool",{tool,value}))return false;
    const inputMode=tool==="input-method"?value:tool==="toggle-timewise"?
      (response?.["input-method"]==="timewise"?"note-name":"timewise"):response?.["input-method"];
    entryFlight={request:busy,project:session,page,generation,inputMode};
    return true;
  }
  function drainEntryKeys(row){
    if(entryFlight?.request!==row.request)return;
    const queued=entryFIFO, shouldEscape=entryEscape, stamp=entryFlight;
    clearEntryKeys();
    if(!entryContext(stamp)||row.error||!response?.input)return;
    if(shouldEscape){send("tool",{tool:"input-stop",value:""});return;}
    const next=queued.shift();
    if(next&&sendEntryTool(...next))entryFIFO=queued;
  }
  function entryKey(event,inputMode){
    const key=event.key.toLowerCase();
    if(inputMode==="duration"&&!event.altKey&&!event.shiftKey&&(key==="arrowup"||key==="arrowdown"))
      return ["stage-pitch",(key==="arrowup"?"up":"down")+(event.ctrlKey?"-octave":"")];
    if(event.ctrlKey&&event.altKey&&!event.shiftKey&&/^[1-4]$/.test(key))
      return ["voice",key];
    if(event.altKey&&!event.ctrlKey){
      const raw=/^[0-9]$/.test(key)?key:/^(?:Digit|Numpad)[0-9]$/.test(event.code||"")?event.code.slice(-1):"";
      if(raw&&(!event.shiftKey||raw!=="1")){
        const interval=raw==="0"?10:Number(raw);
        return ["select-interval",String(event.shiftKey?-interval:interval)];
      }
    }
    if(event.ctrlKey&&!event.altKey&&event.shiftKey&&(key==="delete"||key==="backspace"))
      return ["full-measure-rest",""];
    if(event.ctrlKey&&!event.altKey&&key==="i")
      return event.shiftKey?["input-method","repitch"]:["toggle-timewise",""];
    if(event.ctrlKey&&!event.altKey&&event.shiftKey&&/^[a-h]$/.test(key))
      return ["insert-letter",key==="h"?"b":key];
    if(event.ctrlKey||event.altKey)return null;
    if(!event.shiftKey&&(key==="n"||key==="m"))
      return ["input-method",key==="n"?"note-name":"duration"];
    if(/^[a-g]$/.test(key))
      return [event.shiftKey?"chord-letter":"note-letter",key];
    if(key==="w"||key==="q")
      return ["relative-duration",event.shiftKey?(key==="w"?"longer-dotted":"shorter-dotted"):(key==="w"?"double":"half")];
    if(event.shiftKey&&key!=="+")return null;
    const dur=["","sixtyfourth","thirtysecond","sixteenth","eighth","quarter","half","whole","breve","long"];
    if(/^[1-9]$/.test(key))return ["duration",dur[Number(key)]];
    if(key==="0")return ["insert-rest",""];
    if(key==="."||(key===","&&event.location===KeyboardEvent.DOM_KEY_LOCATION_NUMPAD))return ["dot","toggle"];
    if(["-","+","="].includes(key))return ["accidental",key==="-"?"flat":key==="+"?"sharp":"natural"];
    return null;
  }

  let queuedNavigation=[];
  const navKind=kind=>kind==="navigate"||kind==="range-key";
  function navStamp(){
    return {project,generation:nativeDocumentGeneration,view:root?.parentElement,area:area(),input:!!response?.input};
  }
  const sameNavigationScope=n=>n&&n.project===project&&n.generation===nativeDocumentGeneration&&
    n.view===root?.parentElement&&n.area===area()&&n.area===document.activeElement&&n.input===!!response?.input&&
    root?.isConnected&&response?.editing&&!response.readOnly&&!response?.textEditing&&!response?.gripEdit;
  function pumpNavigation(){
    if(busy||!queuedNavigation.length)return;
    const next=queuedNavigation.shift();
    if(!sameNavigationScope(next)){queuedNavigation=[];return;}
    if(!send(next.kind,next.fields))queuedNavigation=[]; // no background motion after diversion
  }
  function selectionNavigation(event){
    const key=event.key, ctrl=event.ctrlKey, alt=event.altKey, shift=event.shiftKey;
    const horizontal=key==="ArrowLeft"||key==="ArrowRight";
    const boundary=key==="Home"||key==="End";
    const direction=key==="ArrowLeft"?"left":"right";
    let nav=null;
    if(horizontal&&ctrl&&alt&&shift)nav={mode:"segment-element",direction};
    if(horizontal&&response.input&&shift&&!ctrl&&!alt)nav={mode:"swap-chord",direction};
    if(boundary&&shift&&!alt&&!response.input)
      nav={mode:(key==="Home"?"begin-":"end-")+(ctrl?"score":"system")};
    if(!shift){
      if(horizontal&&!(ctrl&&alt))nav={direction,mode:alt?"element":ctrl?"measure":"chord"};
      if(alt&&(key==="ArrowUp"||key==="ArrowDown"))nav={direction:key==="ArrowUp"?"up":"down",mode:ctrl?"chord-edge":"chord-note"};
      if(ctrl&&!alt&&boundary)nav={mode:key==="Home"?"first":"last"};
      if(ctrl&&!alt&&key.toLowerCase()==="a")nav={mode:"all"};
    }
    if(nav)return {kind:"navigate",fields:nav};
    if(!response.input&&shift&&!alt&&(/^Arrow(Left|Right)$/.test(key)||(!ctrl&&/^Arrow(Up|Down)$/.test(key)))){
      return {kind:"range-key",fields:{dir:key.slice(5).toLowerCase(),unit:horizontal?(ctrl?"measure":"chord"):"staff",control:ctrl}};
    }
    return null;
  }
  function mayQueueNavigation(intent){
    // In text or handle edit the same arrows can mutate the document instead
    // of moving a selection. Preserve their existing path; only queue actual
    // score-selection navigation from known score-focused state.
    return !!intent&&intent.fields.mode!=="swap-chord"&&navKind(pending)&&!response?.textEditable&&
      !response?.gripEdit&&["Note","Rest","Range",""].includes(response.selectionKind);
  }
  let panReceipt=null, nativePointer=null, nativePointerId=0, gesture=null;
  // Clicks on another page that wait for the worker (see activatePage).
  let queuedPageClicks=[], pageClickLanding=false;
  // Motion has one in-flight request and at most two unsent axis samples.
  // Preserve the last coordinates that can update each axis across modifier
  // changes; an unrestricted sample subsumes both. An end/cancel is a barrier.
  const motionAxis=p=>p.shift?2:p.control?1:3;
  const isObjectMotion=g=>!g.ready||g.ready==="element"||g.ready==="grip";
  function retainPointerAxes(g,samples,p) {
    if(!isObjectMotion(g))return [p];
    let covered=motionAxis(p);
    const keep=[p];
    for(let i=samples.length-1;i>=0&&covered!==3;--i){
      const prior=samples[i], axis=motionAxis(prior);
      if(axis&~covered){keep.unshift(prior);covered|=axis;}
    }
    return keep;
  }
  function rememberMotion(g,p){g.moves=retainPointerAxes(g,g.moves,p);}
  const pointerPacket=(g,phase,p)=>({phase,gesture:g.sid,page:g.page,
    cssScale:phase==="begin"?g.startCssScale:g.box.width/960,pointerV2:true,...p});
  function requestPointerFrame(g){
    if(g.raf)return;
    g.raf=requestAnimationFrame(()=>{
      g.raf=0;
      if(gesture!==g&&nativePointer!==g)return;
      if(g.ready==="pan"||g.kind==="pan"||g.kind==="maybe-pan"){g.pan?.();return;}
      if(!g.finished)g.reproject?.();
      g.drawLasso?.();
      pumpPointer(true);
    });
  }
  function discardPointer(g) {
    if(!g)return;
    if(g.raf)cancelAnimationFrame(g.raf);
    g.raf=0;
    g.preview?.remove();
    g.preview=null;
    if(nativePointer===g){
      nativePointer=null;
      replayDeferredRaster();
    }
  }
  function pumpPointer(allowMove=false){
    const g=nativePointer;
    if(!g||busy||!g.ready||g.ready==="pan")return;
    if(g.project!==project||g.doc!==nativeDocumentGeneration){discardPointer(g);return;}
    if(g.pageDelta&&!g.next){
      const delta=g.pageDelta;g.pageDelta=0;g.moves=[];
      if(send("pointer-gesture",{...pointerPacket(g,"page",g.point),delta})){
        g.flight="page";g.viewDelta=delta;return;
      }
    }
    if(g.next){
      const end=g.next, endAxis=motionAxis(end.point);
      // The release itself supplies the last sample. Only transmit earlier
      // motion on a frozen axis, never an obsolete rectangle/range raster.
      g.moves=end.phase==="end"&&isObjectMotion(g)?
        g.moves.filter(p=>(motionAxis(p)&endAxis)!==motionAxis(p)):[];
      if(!g.moves.length){
        if(send("pointer-gesture",pointerPacket(g,end.phase,end.point))){
          g.next=null;g.flight=end.phase;
        }
        return;
      }
    }
    if(!g.moves.length)return;
    if(!allowMove&&!g.next){requestPointerFrame(g);return;}
    const move=g.moves[0];
    if(send("pointer-gesture",pointerPacket(g,"move",move))){
      g.moves.shift();g.flight="move";
    }
  }
  let deferredPointerActions=[];
  function clearPointerFollowup(){deferredPointerActions=[];}
  // MuseScore handles every press in order on its own thread. The worker here
  // owns one command at a time, so presses that arrive while it is busy are
  // kept (in arrival order, bounded) and replayed as soon as it is free.
  function rememberPointerAction(action, g) {
    if(deferredPointerActions.length<64)deferredPointerActions.push({node:root,home:g.home,
      project,doc:g.doc,page:g.page,input:!!response?.input,...action});
  }
  let replayingInput=false;
  const deferredInputScope=()=>({home:root.parentElement,doc:nativeDocumentGeneration,page:response?.page});
  function deferInputKey(event){
    if(event.repeat&&deferredPointerActions.length)return false;   // no autorepeat backlog
    if(deferredPointerActions.length>=64)return false;
    event.preventDefault();event.stopPropagation();
    rememberPointerAction({key:{key:event.key,code:event.code,location:event.location,ctrlKey:event.ctrlKey,
      shiftKey:event.shiftKey,altKey:event.altKey,bubbles:true,cancelable:true,composed:true}},deferredInputScope());
    return true;
  }
  function rememberPointerKey(event){
    const g=nativePointer;
    if(!g?.finished||g.next?.phase==="cancel"||g.flight==="cancel"||event.repeat||
       event.altKey||event.metaKey||event.isComposing||response?.input||response?.textEditing||
       !g.home?.contains(root)||!root?.isConnected)return false;
    const key=event.key.toLowerCase();
    const allowed=event.ctrlKey?(event.shiftKey?["q","w","z"]:["c","x","v","y","z","delete","backspace","arrowup","arrowdown"]).includes(key):
       !event.shiftKey&&["delete","backspace","r","arrowup","arrowdown"].includes(key);
    if(!allowed)return false;
    event.preventDefault();event.stopPropagation();
    rememberPointerAction({key:{key:event.key,code:event.code,ctrlKey:event.ctrlKey,shiftKey:event.shiftKey,
      bubbles:true,cancelable:true,composed:true}},g);
    return true;
  }
  function flushPointerFollowup(){
    // Earlier presses already held by the entry/navigation/pitch queues go first.
    if(busy||nativePointer||entryFIFO.length||queuedNavigation.length||queuedPitchTaps.length)return;
    const apply=todo=>todo&&todo.node===root&&todo.home===root.parentElement&&todo.project===project&&
      todo.doc===nativeDocumentGeneration&&todo.page===response?.page&&root.isConnected&&!response.textEditing&&
      !root.closest(scoreKeyHidden);   // as in MuseScore, each press acts on the state it is processed in
    while(!busy&&deferredPointerActions.length){
      const action=deferredPointerActions.shift();
      if(!apply(action))continue;
      if(action.fields)send("select",action.fields);
      else if(action.control){
        const c=action.control;
        if(c.isConnected&&root.contains(c)&&!c.disabled&&c.getAttribute("aria-disabled")!=="true")dispatch(c.dataset.nativeScore,c);
      }
      else if(document.activeElement===area()){
        replayingInput=true;
        try{area().dispatchEvent(new KeyboardEvent("keydown",action.key));}finally{replayingInput=false;}
      }
    }
    flushPageClicks();
  }
  // MuseScore's "play notes when editing": a note sounds when it is entered,
  // clicked, moved to, or its pitch changes (also while dragging it), on the
  // score's own instrument through the existing one-voice note feedback.
  let auditionSerial=0,auditionTimer=0,heardPitch=null;
  const AUDITION_KINDS=new Set(["select","tool","pitch","pointer-gesture","navigate","key"]);
  function auditionAfter(kind,row,oldRevision){
    const pitch=row?.selectedPitch;
    if(!AUDITION_KINDS.has(kind)||row.error||!Number.isInteger(pitch)||pitch<0||pitch>127){
      if(kind!=="pointer-gesture")heardPitch=null;
      return;
    }
    // Drag samples sound only on a pitch step; a tool command only when it
    // changed the score (a note entered, an accidental); clicks always sound.
    if(kind==="pointer-gesture"?pitch===heardPitch:(kind==="tool"&&row.revision===oldRevision))return;
    heardPitch=pitch;
    if(window.AstraScoreAudition&&!window.AstraScoreAudition.enabled())return;
    const owner=typeof bridge!=="undefined"?bridge:null, midi=typeof state!=="undefined"?state.midi:null;
    if(!owner||typeof owner.vstNoteFeedback!=="function"||!midi?.vst_feedback||midi.vst_epoch==null||
       midi.playing||transportScope?.playing)return;
    const epoch=midi.vst_epoch, token="score:"+epoch+":"+(++auditionSerial);
    clearTimeout(auditionTimer);
    try{owner.vstNoteFeedback(JSON.stringify({op:"on",epoch,token,pitch,channel:0,velocity:80}));}catch{return;}
    auditionTimer=setTimeout(()=>{try{owner.vstNoteFeedback(JSON.stringify({op:"off",epoch,token}));}catch{}},350);
  }
  function pointerResponse(row,kind){
    const g=nativePointer;
    if(!g)return;
    if(kind==="pointer-gesture"){
      const phase=g.flight;
      g.flight=null;
      if(row.pointerAborted){discardPointer(g);clearPointerFollowup();return;}
      if(phase==="begin"){
        g.ready=row.error?"blocked":row.pointerKind;
        g.paged=row.pointerMayTurnPage===true;
        if(g.ready==="pan"){
          discardPointer(g);
          if(g.released&&g.home===root.parentElement&&root.isConnected)g.pan();
          else if(!g.finished)requestPointerFrame(g);
          clearPointerFollowup();
          return;
        }
        if(!g.ready||g.ready==="blocked"){discardPointer(g);clearPointerFollowup();return;}
        if(g.ready==="lasso")g.drawLasso();
      }else if(phase==="page"){
        if(!row.error&&row.pointerPage===row.page){
          g.page=row.page;
          g.afterPage(g.viewDelta);
          if(g.releaseClient){
            const e=g.releaseClient;g.releaseClient=null;g.releaseAt(e);
          }
        }else{
          g.finished=true;g.next={phase:"cancel",point:g.point};
        }
        g.viewDelta=0;
      }else if(phase==="end"||phase==="cancel"){
        discardPointer(g);
        if(row.error||phase==="cancel"||row.pointerAccepted===false)clearPointerFollowup();
        return;
      }else if(row.error){
        g.finished=true;
        g.next={phase:"cancel",point:g.point};
      }
    }
    pumpPointer(true);
  }
  let menuProvider=null, contextPoint=null, popup=null;
  const area = () => root?.querySelector(".astra-native-paper");
  // Allow a small breathing space for panning, not an almost empty viewport.
  // Natural centring on a wide screen is separate from the draggable margin.
  const MAX_SCORE_PAN_GUTTER=64;
  let overscroll=null, overscrollObserver=null, pendingPaperPosition=null, lastPaperPosition=null;
  const readyPaper=paper=>Boolean(paper?.isConnected&&paper===area()&&paper.clientWidth&&paper.clientHeight);
  function syncOverscroll(paper=area()){
    if(!readyPaper(paper))return false;
    const panGutter=size=>Math.min(MAX_SCORE_PAN_GUTTER,Math.round(size*.10));
    const width=parseFloat(pageCssWidth());
    const x=Math.round(Math.max(0,(paper.clientWidth-width)/2))+panGutter(paper.clientWidth);
    const y=panGutter(paper.clientHeight);
    const same=overscroll?.paper===paper;
    if(same&&overscroll.x===x&&overscroll.y===y)return true;
    const dx=same?x-overscroll.x:0, dy=same?y-overscroll.y:0;
    paper.style.setProperty("--astra-ovx",x+"px");
    overscrollSpacers(paper,x,y);
    overscroll={paper,x,y};
    if(same){paper.scrollLeft+=dx;paper.scrollTop+=dy;}
    else{
      overscrollObserver?.disconnect();
      if(typeof ResizeObserver==="function"){overscrollObserver=new ResizeObserver(()=>syncOverscroll(paper));overscrollObserver.observe(paper);}
    }
    return true;
  }
  function overscrollSpacers(paper,x=overscroll?.x,y=overscroll?.y){
    if(x==null)return;
    let top=paper.querySelector(":scope>.astra-native-overscroll-top"), bottom=paper.querySelector(":scope>.astra-native-overscroll-bottom");
    if(!top){top=document.createElement("div");top.className="astra-native-overscroll-top";top.setAttribute("aria-hidden","true");paper.prepend(top);}
    if(!bottom){bottom=document.createElement("div");bottom.className="astra-native-overscroll-bottom";bottom.setAttribute("aria-hidden","true");paper.append(bottom);}
    // Room to the right: block children's end margins don't count as
    // scrollable overflow, an absolutely placed marker does.
    let edge=paper.querySelector(":scope>.astra-native-overscroll-edge");
    if(!edge){edge=document.createElement("div");edge.className="astra-native-overscroll-edge";edge.setAttribute("aria-hidden","true");paper.append(edge);}
    const width=(sheet()?.getBoundingClientRect().width||960*zoom)+2*x;
    top.style.height=y+"px";bottom.style.height=y+"px";
    let right=paper.clientLeft+parseFloat(getComputedStyle(paper).paddingLeft||0)+width-1;
    if(pageFlowHorizontal){
      const last=[...paper.children].filter(e=>e.matches("canvas.astra-native-live,.astra-native-page")).pop();
      if(last)right=last.offsetLeft+last.offsetWidth+x-1;
    }
    edge.style.left=right+"px";
  }
  // Where a page is first shown: centred across, its top margin in view.
  function homeScroll(paper=area(),atEnd=false){
    const canvas=sheet();
    if(!canvas?.isConnected||canvas.hidden||!syncOverscroll(paper))return false;
    const c=canvas.getBoundingClientRect(), v=paper.getBoundingClientRect();
    if(!c.width||!c.height)return false;
    paper.scrollLeft+=c.left-v.left-paper.clientLeft-(c.width<paper.clientWidth?(paper.clientWidth-c.width)/2:0);
    paper.scrollTop+=atEnd?c.bottom-v.top-paper.clientTop-paper.clientHeight+18:c.top-v.top-paper.clientTop-18;
    return true;
  }
  const samePaperScope=(record,paper)=>record?.paper===paper&&record.root===root&&
    record.project===project&&record.generation===nativeDocumentGeneration;
  function rememberPaperPosition(paper=area()){
    if(!readyPaper(paper)||!response?.revision||samePaperScope(pendingPaperPosition,paper))return;
    lastPaperPosition={paper,root,project,generation:nativeDocumentGeneration,left:paper.scrollLeft,top:paper.scrollTop};
  }
  function preservePaperPosition(){
    if(!root)return null;
    const paper=area();
    rememberPaperPosition(paper); // The same DOM node may be detached while another tab is shown.
    return {node:root,position:samePaperScope(lastPaperPosition,paper)?lastPaperPosition:null};
  }
  function placePaperPosition(paper=area()){
    const row=pendingPaperPosition, canvas=sheet();
    if(!row||!samePaperScope(row,paper)||!readyPaper(paper)||!canvas?.isConnected||canvas.hidden)return;
    if(row.position){
      if(!syncOverscroll(paper))return;
      paper.scrollLeft=row.position.left;
      paper.scrollTop=row.position.top;
    }else if(!homeScroll(paper))return;
    pendingPaperPosition=null;
    rememberPaperPosition(paper);
  }
  function setPaperPosition(paper,position=null){
    if(!paper)return;
    const row=pendingPaperPosition={paper,root,project,generation:nativeDocumentGeneration,position};
    placePaperPosition(paper);
    if(pendingPaperPosition===row)requestAnimationFrame(()=>{
      if(pendingPaperPosition===row)placePaperPosition(paper);
    });
  }

  // ---- 版面: MuseScore's Layout panel (instrumentsscene/LayoutPanel.qml), cut
  // down to what a single-piano score uses: the instrument and its staves,
  // their visibility buttons, and the instrument / staff settings popups.
  const layoutPanel=(()=>{
    let tree=null, treeKey=null, asked=null, popup=null, expanded=true;
    const host=()=>root?.querySelector(".astra-ms-layout");
    const shown=()=>{const h=host();return Boolean(h&&!h.closest("[hidden]"));};
    const key=()=>response?.revision+":"+nativeDocumentGeneration;
    const glyph=name=>{const s=document.createElement("span");s.className="astra-ms-glyph";s.setAttribute("aria-hidden","true");
      s.textContent=String.fromCodePoint(stock.iconCodes[name]);return s;};
    const canEdit=()=>Boolean(response?.editing&&!response.readOnly&&!response.input&&!response.textEditing);
    // Part and staff ids are separate sequences: a popup is keyed by both.
    const isOpen=(target,id)=>Boolean(popup&&popup.target===target&&popup.id===id);
    function want(){
      if(!shown()||!response?.revision||asked||treeKey===key())return;
      if(busy||deferredPointerActions.length||nativePointer)return;
      asked=key();
      if(!send("layout-tree"))asked=null;
    }
    function receive(row){
      const k=asked;asked=null;
      if(row.error||!row.layoutTree||!Array.isArray(row.layoutTree.parts))return;
      tree=row.layoutTree;treeKey=k;render();
    }
    function set(target,id,field,value){
      if(!canEdit())return;
      if(busy){setTimeout(()=>set(target,id,field,value),16);return;}
      send("layout-set",{target,id,field,value});
    }
    function button(cls,label,icon,action){
      const b=document.createElement("button");b.type="button";b.className=cls;b.title=label;b.setAttribute("aria-label",label);
      b.append(glyph(icon));b.disabled=!canEdit();b.addEventListener("click",action);return b;
    }
    function row(level,item,title,target){
      const r=document.createElement("div");r.className="astra-ms-layout-row";r.setAttribute("role","treeitem");
      r.style.setProperty("--level",level);r.classList.toggle("is-hidden",!item.visible);
      if(level===0){
        const arrow=document.createElement("button");arrow.type="button";arrow.className="astra-ms-layout-arrow";
        arrow.setAttribute("aria-label",expanded?"收起":"展开");arrow.append(glyph(expanded?"SMALL_ARROW_DOWN":"SMALL_ARROW_RIGHT"));
        arrow.addEventListener("click",()=>{expanded=!expanded;render();});
        r.setAttribute("aria-expanded",String(expanded));r.append(arrow);
      }
      r.append(button("astra-ms-layout-eye",item.visible?"隐藏":"显示",item.visible?"EYE_OPEN":"EYE_CLOSED",
        ()=>set(target,item.id,"visible",!item.visible)));
      const t=document.createElement("span");t.className="astra-ms-layout-title";t.textContent=title;r.append(t);
      const gear=button("astra-ms-layout-gear",level?"谱表设置":"乐器设置","SETTINGS_COG",()=>{
        popup=isOpen(target,item.id)?null:{target,id:item.id};render();
      });
      gear.setAttribute("aria-expanded",String(isOpen(target,item.id)));
      r.append(gear);
      return r;
    }
    function control(label,el){
      const w=document.createElement("label");w.className="astra-ms-popup-field";
      const t=document.createElement("span");t.textContent=label;w.append(t,el);return w;
    }
    function check(label,value,apply){
      const w=document.createElement("label");w.className="astra-ms-popup-check";
      const c=document.createElement("input");c.type="checkbox";c.checked=value;c.disabled=!canEdit();
      c.addEventListener("change",()=>apply(c.checked));
      const t=document.createElement("span");t.textContent=label;w.append(c,t);return w;
    }
    function choose(options,value,apply){
      const s=document.createElement("select");s.disabled=!canEdit();
      options.forEach((text,i)=>{const o=document.createElement("option");o.value=String(i);o.textContent=text;o.selected=i===value;s.append(o);});
      s.addEventListener("change",()=>apply(Number(s.value)));return s;
    }
    function text(value,apply){
      const i=document.createElement("input");i.type="text";i.value=value||"";i.maxLength=200;i.disabled=!canEdit();
      i.addEventListener("change",()=>apply(i.value));
      i.addEventListener("keydown",e=>{e.stopPropagation();if(e.key==="Enter")i.blur();});
      return i;
    }
    function settings(part,staff){
      const p=document.createElement("div");p.className="astra-ms-layout-popup";p.setAttribute("role","dialog");
      if(staff){
        p.setAttribute("aria-label","谱表设置");
        p.append(
          check("小谱表",staff.small,v=>set("staff",staff.id,"small",v)),
          control("隐藏空谱表",choose(["跟随乐器","总是隐藏","从不隐藏"],staff.hideWhenEmpty,v=>set("staff",staff.id,"hideWhenEmpty",v))),
          check("如果整行谱为空，则显示此谱表",staff.showIfEntireSystemEmpty,v=>set("staff",staff.id,"showIfEntireSystemEmpty",v)));
      }else{
        p.setAttribute("aria-label","乐器设置");
        p.append(
          control("主谱中的名称",text(part.name,v=>set("part",part.id,"name",v))),
          control("缩写名称",text(part.abbreviation,v=>set("part",part.id,"abbreviation",v))),
          control("隐藏空谱表",choose(["自动","总是隐藏","从不隐藏"],part.hideWhenEmpty,v=>set("part",part.id,"hideWhenEmpty",v))));
        if(part.staves.length>1)p.append(check("仅当整个乐器为空时才隐藏谱表",part.hideStavesWhenIndividuallyEmpty,
          v=>set("part",part.id,"hideStavesWhenIndividuallyEmpty",v)));
      }
      if(!canEdit()){const n=document.createElement("p");n.className="astra-ms-popup-note";n.textContent="请先开启编辑模式。";p.append(n);}
      return p;
    }
    function render(){
      const h=host();
      if(!h)return;
      if(!tree){h.replaceChildren();return;}
      const focus=document.activeElement&&h.contains(document.activeElement)?document.activeElement.getAttribute("aria-label"):null;
      const out=[];
      for(const part of tree.parts){
        out.push(row(0,part,part.name||"钢琴","part"));
        if(isOpen("part",part.id))out.push(settings(part,null));
        if(!expanded)continue;
        for(const staff of part.staves){
          out.push(row(1,staff,"谱表 "+staff.index,"staff"));
          if(isOpen("staff",staff.id))out.push(settings(part,staff));
        }
      }
      h.replaceChildren(...out);
      if(focus)[...h.querySelectorAll("[aria-label]")].find(e=>e.getAttribute("aria-label")===focus)?.focus({preventScroll:true});
    }
    document.addEventListener("pointerdown",e=>{
      if(popup&&!e.target.closest?.(".astra-ms-layout-popup,.astra-ms-layout-gear")){popup=null;render();}
    },true);
    document.addEventListener("keydown",e=>{if(popup&&e.key==="Escape"&&host()?.contains(e.target)){popup=null;render();}},true);
    return {want,receive,render,reset(){tree=null;treeKey=null;asked=null;popup=null;}};
  })();

  // ---- Every page in one scrolling column (MuseScore's vertical page view) ----
  // The worker's current page is the live canvas that every edit, pointer and
  // cursor path uses. The other pages are images from the worker's read-only
  // page-raster command, refreshed after edits once the person pauses.
  const pages={els:new Map(),doc:null,inflight:null,timer:0,quietUntil:0,view:0};
  // Vector page view: each page's display list (worker "vector-page"),
  // replayed at the screen's exact density whenever the zoom changes.
  let vectorMode=(()=>{try{return localStorage.getItem("astra.score.vectorView")!=="0";}catch{return true;}})()&&!!window.AstraVectorPage;
  const liveVec={v:null,revision:null,page:null};
  // Vector repaint changes the display, not the worker's bitmap damage base.
  // Keep the latter only while the live canvas is showing vector pixels.
  let liveRasterBase=null;
  const pagesDoc=()=>project+":"+nativeDocumentGeneration;
  function pageEntry(n){
    let e=pages.els.get(n);
    if(!e){
      const img=document.createElement("img");
      img.className="astra-native-page";img.dataset.page=String(n);img.alt="第 "+n+" 页";img.draggable=false;
      img.decoding="async";img.style.aspectRatio="960 / "+(response?.frameHeight||1358);
      e={img,el:img,revision:null,scale:null,url:null};
      pages.els.set(n,e);
    }
    return e;
  }
  // The visible picture stays until its successor is decoded (an async
  // <img> given a new src can show nothing for a frame).
  function setPageImage(e,src,ratio){
    const next=e.img.cloneNode(false), token=e.imgToken=(e.imgToken||0)+1;
    if(ratio)next.style.aspectRatio=ratio;
    next.src=src;
    const swap=()=>{
      if(e.imgToken!==token)return;
      next.style.width="";
      const old=e.el;
      if(old.isConnected)old.replaceWith(next);
      e.img=next;e.el=next;
    };
    if(next.decode)next.decode().catch(()=>{}).finally(swap);else swap();
  }
  function keepLivePage(n,canvas){
    const e=pageEntry(n), copy=document.createElement("canvas");
    copy.width=canvas.width;copy.height=canvas.height;copy.className="astra-native-page";
    copy.style.width=canvas.style.width;copy.dataset.page=String(n);
    copy.getContext("2d",{alpha:false}).drawImage(canvas,0,0);
    if(e.el.isConnected)e.el.replaceWith(copy);
    e.el=copy;e.revision=response?.revision;e.scale=response?.rasterScale;
    if(vectorMode&&liveVec.v&&liveVec.page===n){e.vec=liveVec.v;e.revision=liveVec.revision;return;}
    copy.toBlob(blob=>{
      if(!blob||pages.els.get(n)!==e||e.el!==copy)return;
      if(e.url)URL.revokeObjectURL(e.url);
      e.url=URL.createObjectURL(blob);
      setPageImage(e,e.url,copy.width+" / "+copy.height);
    },"image/png");
  }
  function layoutPages(){
    const paper=area(), canvas=sheet();
    if(!paper||!canvas||!response?.pages)return;
    if(pages.doc!==pagesDoc()){
      for(const e of pages.els.values()){e.el.remove();if(e.url)URL.revokeObjectURL(e.url);}
      pages.els.clear();pages.inflight=null;pages.doc=pagesDoc();liveVec.v=null;liveVec.revision=null;liveVec.page=null;
    }
    const total=response.pages, live=response.page;
    paper.classList.toggle("is-horizontal",pageFlowHorizontal);
    root?.querySelector("[data-score-page-flow]")?.setAttribute("aria-pressed",String(pageFlowHorizontal));
    syncViewConfigButtons();
    for(const [n,e] of [...pages.els])if(n>total||n===live){
      if(n>total){e.el.remove();if(e.url)URL.revokeObjectURL(e.url);pages.els.delete(n);}
      else if(e.el.isConnected)e.el.remove();
    }
    let previous=paper.querySelector(":scope>.astra-native-overscroll-top");
    for(let n=1;n<=total;n++){
      const el=n===live?canvas:pageEntry(n).el;
      if(previous?previous.nextElementSibling!==el:paper.firstElementChild!==el){
        if(previous)previous.after(el);else paper.prepend(el);
      }
      el.classList.toggle("is-first-page",n===1);
      previous=el;
    }
    const bottom=paper.querySelector(":scope>.astra-native-overscroll-bottom");
    if(bottom&&previous.nextElementSibling!==bottom)previous.after(bottom);
    paper.style.setProperty("--astra-page-w",pageCssWidth());
    if(overscroll?.paper===paper)overscrollSpacers(paper);
    // Re-laying out the column can move the live page without resizing it, so
    // the playback cursor's ResizeObserver never fires: re-measure it here.
    playbackCursor?.reflow();
    requestAnimationFrame(()=>{pages.view=pageInView();});
  }
  // Scroll one page (live canvas or picture) to the top-left of the view.
  function revealPage(paper,n){
    const el=n===response?.page?sheet():pages.els.get(n)?.el;
    if(!paper||!el?.isConnected)return false;
    const r=el.getBoundingClientRect(), v=paper.getBoundingClientRect();
    if(!r.width||!r.height)return false;
    programmaticScrollAt=performance.now();
    paper.scrollLeft+=r.left-v.left-paper.clientLeft-(r.width<paper.clientWidth?(paper.clientWidth-r.width)/2:18);
    paper.scrollTop+=r.top-v.top-paper.clientTop-18;
    return true;
  }
  function pageInView(){
    const paper=area();
    if(!paper||!response?.pages||response.pages<2)return response?.page||0;
    const v=paper.getBoundingClientRect(), mid=pageFlowHorizontal?v.left+v.width/2:v.top+v.height/2;
    let best=response.page,gap=Infinity;
    for(let n=1;n<=response.pages;n++){
      const el=n===response.page?sheet():pages.els.get(n)?.el;
      if(!el?.isConnected)continue;
      const r=el.getBoundingClientRect(), a=pageFlowHorizontal?r.left:r.top, b=pageFlowHorizontal?r.right:r.bottom;
      const d=mid<a?a-mid:mid>b?mid-b:0;
      if(d<gap){gap=d;best=n;}
    }
    return best;
  }
  function stalePage(){
    const paper=area();
    if(!paper||!response?.revision)return null;
    const v=paper.getBoundingClientRect(), scale=nativeRasterScale();
    let best=null,gap=Infinity;
    for(const [n,e] of pages.els){
      if(n===response.page||(e.revision===response.revision&&e.scale===scale))continue;
      if(!e.el.isConnected)continue;
      const r=e.el.getBoundingClientRect();
      const d=(r.bottom<v.top?v.top-r.bottom:r.top>v.bottom?r.top-v.bottom:0)+(r.right<v.left?v.left-r.right:r.left>v.right?r.left-v.right:0);
      if(d>Math.max(v.width,v.height)&&e.revision!==null)continue;   // far pages refresh when scrolled near
      if(d<gap){gap=d;best=n;}
    }
    return best;
  }
  // Another page costs the worker a full paint; never ahead of a person's input.
  function schedulePages(delay=0){
    clearTimeout(pages.timer);
    pages.timer=setTimeout(pumpPages,Math.max(delay,pages.quietUntil-performance.now(),0));
  }
  function pumpPages(){
    if(zoomStage||pages.inflight||busy||deferredPointerActions.length||nativePointer||!response?.revision||
       response.textEditing||!root?.isConnected||document.hidden)return;
    if(performance.now()<pages.quietUntil){schedulePages();return;}
    if(vectorMode&&(liveVec.revision!==response.revision||liveVec.page!==response.page)){
      pages.inflight={page:response.page,revision:response.revision,doc:pagesDoc(),vector:true,live:true};
      if(!send("vector-page",{page:response.page}))pages.inflight=null;
      return;
    }
    const n=stalePage();
    if(n===null)return;
    const scale=nativeRasterScale();
    const e=pages.els.get(n);
    if(vectorMode&&e?.rasterOnlyRevision!==response.revision){
      // Only the density changed: redraw from the display list already here.
      if(e?.vec&&e.revision===response.revision){drawVectorPage(n,e);schedulePages(0);return;}
      pages.inflight={page:n,revision:response.revision,doc:pagesDoc(),vector:true};
      if(!send("vector-page",{page:n}))pages.inflight=null;
      return;
    }
    pages.inflight={page:n,revision:response.revision,scale,doc:pagesDoc()};
    if(!send("page-raster",{page:n,rasterScale:scale}))pages.inflight=null;
  }
  function receiveVectorPage(row){
    const f=pages.inflight;pages.inflight=null;
    if(vectorMode&&f?.vector&&!row.error&&row.vector&&row.vectorPage===f.page&&f.doc===pagesDoc()&&
       row.revision===f.revision&&f.revision===response?.revision){
      const revision=row.revision;
      // Native pixmaps/images are not present in the display list. Never
      // replace the complete raster with this incomplete page.
      let v=null;
      if(row.vector.skipped===0){
        try { v=window.AstraVectorPage.prepare(row.vector); } catch {}
      }
      if(f.live){
        if(f.page===response?.page){liveVec.v=v;liveVec.revision=revision;liveVec.page=f.page;}
      }else{
        const e=pages.els.get(f.page);
        if(e&&f.page!==response?.page){
          e.vec=v;
          if(v){e.revision=revision;drawVectorPage(f.page,e);}
          else e.rasterOnlyRevision=revision;
        }
      }
    }
    schedulePages();
  }
  // A page shown from its display list is a canvas at the exact density.
  function drawVectorPage(n,e){
    const scale=nativeRasterScale(), w=Math.round(960*scale), h=Math.round(e.vec.h*scale);
    let c=e.el;
    if(c.tagName!=="CANVAS"){
      c=document.createElement("canvas");c.className=e.el.className;c.dataset.page=String(n);
      e.imgToken=(e.imgToken||0)+1;
      if(e.el.isConnected)e.el.replaceWith(c);
      e.el=c;
    }
    if(c.width!==w||c.height!==h){c.width=w;c.height=h;}
    const css=pageCssWidth();
    if(c.style.width!==css)c.style.width=css;
    window.AstraVectorPage.draw(c,e.vec,w/960);
    e.scale=scale;
  }
  // Every page in (or next to) the view that is not at the current density
  // is redrawn now from its display list; the live page too while its
  // display list matches its revision.
  function redrawVectorView(){
    const paper=area();
    if(!vectorMode||!paper||!response?.revision)return;
    const v=paper.getBoundingClientRect(), scale=nativeRasterScale();
    const near=el=>{const r=el.getBoundingClientRect();
      return r.right>v.left-v.width/2&&r.left<v.right+v.width/2&&r.bottom>v.top-v.height/2&&r.top<v.bottom+v.height/2;};
    for(const [n,e] of pages.els)
      if(e.vec&&n!==response.page&&e.el.isConnected&&e.scale!==scale&&near(e.el))drawVectorPage(n,e);
    const live=sheet();
    if(live&&!live.hidden&&liveVec.v&&liveVec.page===response.page&&liveVec.revision===response.revision&&near(live)){
      const w=Math.round(960*scale), h=Math.round(liveVec.v.h*scale);
      if(live.width!==w||live.height!==h){
        if(!liveRasterBase||liveRasterBase.target!==live){
          const bitmap=document.createElement("canvas");
          bitmap.width=live.width;bitmap.height=live.height;
          bitmap.getContext("2d",{alpha:false}).drawImage(live,0,0);
          liveRasterBase={target:live,bitmap,sequence:rasterSeq};
        }
        live.width=w;live.height=h;
        window.AstraVectorPage.draw(live,liveVec.v,w/960);
      }
    }
  }
  function receivePageRaster(row){
    const f=pages.inflight;pages.inflight=null;
    if(f?.stage){
      const st=f.stage, e=pages.els.get(f.page);
      zoomRtt=zoomRtt?zoomRtt*.6+(performance.now()-f.sent)*.4:performance.now()-f.sent;
      if(!row.error&&typeof row.pageRaster==="string"&&row.pageRasterPage===f.page&&e&&f.doc===pagesDoc()){
        st.decoding++;
        fetch("data:image/png;base64,"+row.pageRaster).then(r=>r.blob()).then(blob=>{
          const url=URL.createObjectURL(blob), img=e.img.cloneNode(false);
          img.style.aspectRatio="960 / "+row.pageRasterHeight;img.src=url;
          return (img.decode?img.decode():Promise.resolve()).catch(()=>{}).then(()=>{
            if(zoomStage!==st){URL.revokeObjectURL(url);return;}
            st.ready.set(f.page,{img,url,revision:row.revision??f.revision,scale:f.scale});
            // The zoom already showed (slow raster): this page joins now.
            if(st.committed)swapStagedPages(shownZoom);
          });
        }).catch(()=>{}).finally(()=>{st.decoding--;st.pending--;closeZoomStage(st);if(zoomStage===st)requestSharpRaster(0);});
      }else{st.pending--;closeZoomStage(st);}
      return;
    }
    if(f&&!row.error&&row.pageRasterPage===f.page&&f.doc===pagesDoc()&&typeof row.pageRaster==="string"){
      const e=pages.els.get(f.page);
      if(e&&f.page!==response?.page){
        if(e.url){URL.revokeObjectURL(e.url);e.url=null;}
        e.revision=row.revision??f.revision;e.scale=f.scale;
        const ratio="960 / "+row.pageRasterHeight, revision=e.revision;
        fetch("data:image/png;base64,"+row.pageRaster).then(r=>r.blob()).then(blob=>{
          if(pages.els.get(f.page)!==e||e.revision!==revision)return;
          if(e.url)URL.revokeObjectURL(e.url);
          e.url=URL.createObjectURL(blob);
          setPageImage(e,e.url,ratio);
        }).catch(()=>{});
      }
    }
    schedulePages();
  }
  // A click on another page makes it the live page, then lands as a click there.
  let pageClick=null, layoutPanelMode="";
  // View > Show (MuseScore ScoreConfig). Read-only browsing always paints as
  // printed, so these only change the editing view. Remembered.
  const viewConfig=Object.assign({showInvisible:true,showUnprintable:true,showFrames:true},(()=>{
    try{const v=JSON.parse(localStorage.getItem("astra.score.viewConfig")||"{}");
      return Object.fromEntries(["showInvisible","showUnprintable","showFrames"].filter(k=>typeof v[k]==="boolean").map(k=>[k,v[k]]));}catch{return {};}
  })());
  let viewConfigSent="", viewConfigTimer=0, tidyNote=null;
  function syncViewConfigButtons(){
    root?.querySelector("[data-score-vector-view]")?.setAttribute("aria-pressed",String(vectorMode));
    for(const b of root?.querySelectorAll("[data-score-view-config]")||[])b.setAttribute("aria-pressed",String(viewConfig[b.dataset.scoreViewConfig]));
  }
  // A newly opened score starts with MuseScore's defaults (all shown).
  function applyViewConfig(changed=false,tries=20){
    clearTimeout(viewConfigTimer);
    const key=nativeDocumentGeneration+":"+JSON.stringify(viewConfig);
    if(!response?.revision||key===viewConfigSent)return;
    if(!changed&&viewConfig.showInvisible&&viewConfig.showUnprintable&&viewConfig.showFrames){viewConfigSent=key;return;}
    if(send("view-config",{...viewConfig}))viewConfigSent=key;
    else if(tries>0)viewConfigTimer=setTimeout(()=>applyViewConfig(changed,tries-1),150);
  }
  // The project folder is "<file>-midi-<hash>": show the file's own name.
  function documentTitle(){
    const name=String(project||"").split(/[\\/]/).filter(Boolean).pop()||"钢琴谱";
    return name.replace(/-(?:(?:midi|audio|score|import)-)?[0-9a-f]{12}$/i,"").replace(/[_]+/g," ").trim()||name;
  }
  // Pages left to right (MuseScore's page view) or top to bottom; remembered.
  let pageFlowHorizontal=(()=>{try{return localStorage.getItem("astra.score.pageFlow")==="horizontal";}catch{return false;}})();
  function setPageFlow(horizontal){
    // Keep the person's place: the page they were reading (measured in the
    // old flow, before the flag changes), or the playhead while playing.
    const keep=pageInView();
    pageFlowHorizontal=horizontal;
    try{localStorage.setItem("astra.score.pageFlow",horizontal?"horizontal":"vertical");}catch{}
    const paper=area();
    if(!paper)return;
    layoutPages();
    if(overscroll?.paper===paper)overscrollSpacers(paper);
    if(!revealPage(paper,keep))homeScroll(paper);
    pages.view=pageInView();
    schedulePages(60);
    // The cursor is re-measured in the next frame (layoutPages -> reflow).
    requestAnimationFrame(()=>{
      if(area()!==paper)return;
      if(playbackCursor?.box()&&(transportScope?.playing||keep===response?.page)){
        if(transportScope?.playing)followPausedUntil=0;
        adjustCanvasToCursor();
      }
      pages.view=pageInView();
    });
  }
  // The click is kept as a place on its page, not on the screen: turning the
  // page can resize the pages above it before the click lands.
  function activatePage(n,event){
    if(!Number.isInteger(n)||n<1||n>(response?.pages||0)||n===response?.page)return;
    const r=event.target.closest?.(".astra-native-page")?.getBoundingClientRect();
    if(!r?.width||!r.height)return;
    const c={page:n,fx:(event.clientX-r.left)/r.width,fy:(event.clientY-r.top)/r.height,
      shiftKey:event.shiftKey,ctrlKey:event.ctrlKey,project,doc:nativeDocumentGeneration,at:performance.now()};
    pauseFollow();
    // A click while the worker still answers an earlier one waits its turn:
    // every click reaches the score, in order, instead of being dropped.
    if(busy||pageClickLanding||queuedPageClicks.length){if(queuedPageClicks.length<4)queuedPageClicks.push(c);return;}
    turnToPageClick(c);
  }
  function turnToPageClick(c){
    if(c.page===response?.page){landPageClick(c);return;}
    pageClick=c;
    if(!send("followPage",{page:c.page}))pageClick=null;
  }
  function landPageClick(c){
    pageClickLanding=true;
    requestAnimationFrame(()=>{
      pageClickLanding=false;
      const s=sheet(), b=s?.getBoundingClientRect();
      if(b?.width&&b.height&&response?.page===c.page&&c.project===project&&c.doc===nativeDocumentGeneration)
        s.dispatchEvent(new MouseEvent("click",{bubbles:true,cancelable:true,clientX:b.left+c.fx*b.width,
          clientY:b.top+c.fy*b.height,shiftKey:c.shiftKey,ctrlKey:c.ctrlKey,button:0,detail:1}));
      flushPageClicks();
    });
  }
  function replayPageClick(row){
    const c=pageClick;pageClick=null;
    if(!c||row.error||row.page!==c.page||!sheet())return;
    landPageClick(c);
  }
  function flushPageClicks(){
    if(busy||pageClickLanding||!queuedPageClicks.length)return;
    const c=queuedPageClicks.shift();
    // A click held this long behind a slow command is no longer what the person wants.
    if(c.project!==project||c.doc!==nativeDocumentGeneration||c.page>(response?.pages||0)||performance.now()-c.at>1500){queuedPageClicks=[];return;}
    turnToPageClick(c);
  }
  // Document shortcuts belong to the mounted edit surface, not just its raster
  // canvas. Never forward text editing or a hidden/detached compare pane.
  let pendingScoreKeySave=null,hostSaveInFlight=false;
  const scoreKeyHidden='[hidden],[inert],[aria-hidden="true"]';
  const scoreKeyShell='textarea,select,[role="textbox"],[role="combobox"],dialog[open],[role="dialog"][aria-modal="true"]';
  const nonTextInputs=new Set(["button","checkbox","color","file","hidden","image","radio","range","reset","submit"]);
  function scoreKeyEditable(el) {
    if(el?.isContentEditable || el?.matches?.(scoreKeyShell))return true;
    return Boolean(el?.matches?.("input")&&!nonTextInputs.has(el.type||"text"));
  }
  function scoreKeyOwner(node) {
    return Boolean(node===root&&node?.isConnected&&!node.closest(scoreKeyHidden)&&
      backend&&project&&response?.revision&&response.editing&&!response.readOnly&&!response.textEditing);
  }
  function handleScoreDocumentKey(node,event) {
    if(event.defaultPrevented||!event.ctrlKey||event.altKey||event.metaKey||event.isComposing||event.keyCode===229||
       event.getModifierState?.("AltGraph"))return;
    const key=event.key.toLowerCase();
    const command=key==="z"?(event.shiftKey?"redo":"undo"):key==="y"?"redo":null;
    if(!command||!scoreKeyOwner(node)||!node.contains(event.target)||event.composedPath().some(scoreKeyEditable))return;
    event.preventDefault();event.stopPropagation();
    if(!busy&&!nativePointer&&(replayingInput||!deferredPointerActions.length||event.target!==area()))send(command);
    else if(!rememberPointerKey(event)&&!nativePointer&&event.target===area())deferInputKey(event);
  }
  function saveWithProject(path,submit) {
    if(path!==project){submit(path);return;}
    if(busy||nativePointer||deferredPointerActions.length||entryFIFO.length){
      pendingScoreKeySave={project,generation:nativeDocumentGeneration,submit};
      return;
    }
    submit(path);
  }
  function flushScoreKeySave() {
    const wanted=pendingScoreKeySave;
    if(!wanted)return;
    if(wanted.project!==project||wanted.generation!==nativeDocumentGeneration){
      pendingScoreKeySave=null;return;
    }
    if(busy||nativePointer||deferredPointerActions.length||entryFIFO.length)return;
    pendingScoreKeySave=null;
    wanted.submit(wanted.project);
  }
  function acceptSaveState(raw) {
    let next;try{next=JSON.parse(raw);}catch{return;}
    if(next.project!==project)return;
    const wasSaving=hostSaveInFlight;
    hostSaveInFlight=Boolean(next.saving);
    if(wasSaving!==hostSaveInFlight)sync();
    if(!next.ready||next.revision!==response?.revision||
       next.saving||!Number.isInteger(next.receipt)||!next.receipt)return;
    // The shared request reaches the native owner independently of its web
    // command lane; reflect the same acknowledged revision in File / Save.
    if(!next.needsSave&&!next.error&&response.dirty){
      response={...response,dirty:false,saved:next.revision,error:undefined};
      sync();
    }
  }
  function historyForPage(path,command){
    if(path!==project||!scoreKeyOwner(root)||root.closest(".temp-right"))return false;
    if(!busy&&!nativePointer&&response[command])send(command);
    return true;
  }
  // End document shortcut ownership.
  function closeContext(restore=false){
    popup?.remove();popup=null;
    if(restore)area()?.focus({preventScroll:true});
  }
  function showContext(detail, x, y){
    closeContext();
    if(!root||!detail?.contextMenu||detail.input||!detail.editing)return;
    const options=[{label:"清除选择",kind:"selection-clear",fields:{}}];
    if(detail.selection?.mode==="single" && detail.selectionKind!=="BarLine"){
      options.unshift(
        {label:"选择全谱中相同类型",kind:"navigate",fields:{mode:"similar"}},
        {label:"选择当前谱表中相同类型",kind:"navigate",fields:{mode:"similar-staff"}});
    }
    if(detail.selection?.mode==="range"){
      options.unshift({label:"选择当前乐段中相同类型",kind:"navigate",fields:{mode:"similar-range"}});
    }
    if(detail.selection?.mode && detail.selection.mode!=="none"){
      options.unshift({label:"按条件选择…",kind:"select-more-query",fields:{}});
    }
    if(menuProvider){
      const custom=menuProvider(Object.freeze(structuredClone(detail)));
      if(Array.isArray(custom))for(const action of custom.slice(0,16)){
        if(action && typeof action.label==="string" && ["tool","content"].includes(action.kind)){
          options.unshift(action);
        }
      }
    }
    if(detail.systemLayout && Object.hasOwn(detail.systemLayout,"locked")){
      const state=detail.systemLayout;
      const layout=(value,label)=>({label,kind:"tool",fields:{tool:"system-layout",value}});
      if(state.previous)options.splice(options.length-1,0,layout("previous","移到上一谱行"));
      if(state.next)options.splice(options.length-1,0,layout("next","移到下一谱行"));
      if(state.make)options.splice(options.length-1,0,layout("make","选中小节排为一行"));
      options.splice(options.length-1,0,layout("lock",state.multiple?"锁定 / 解锁所选谱行":state.locked?"解锁此谱行":"锁定此谱行"));
    }
    if(detail.measureContext?.staves) options.splice(options.length-1,0,{label:"小节属性…",kind:"localMeasureProperties"});
    popup=document.createElement("div");
    popup.setAttribute("role","menu");
    popup.setAttribute("aria-label","乐谱操作");
    Object.assign(popup.style,{position:"fixed",zIndex:"50",minWidth:"130px",padding:"4px",background:"#fff",
      border:"1px solid #b4bfce",borderRadius:"6px",boxShadow:"0 4px 18px #18304032"});
    for(const action of options){
      const btn=document.createElement("button");btn.type="button";btn.setAttribute("role","menuitem");
      btn.textContent=action.label;btn.disabled=!!action.disabled;
      Object.assign(btn.style,{display:"block",width:"100%",textAlign:"left",padding:"4px 8px"});
      btn.addEventListener("click",()=>{
        closeContext(true);
        if(action.kind==="localMeasureProperties"){openMeasureProperties();return;}
        if(action.fields&&typeof action.fields==="object"){
          const {kind,project,request,baseRevision,...fields}=action.fields;
          send(action.kind,fields);
        }else send(action.kind);
      });
      popup.append(btn);
    }
    popup.addEventListener("keydown",ev=>{
      if(ev.key==="Escape"){ev.preventDefault();ev.stopPropagation();closeContext(true);return;}
      const xs=[...popup.querySelectorAll("button:not(:disabled)")];
      const i=xs.indexOf(document.activeElement);
      if(xs.length && ["ArrowUp","ArrowDown","Home","End"].includes(ev.key)){
        ev.preventDefault();ev.stopPropagation();
        xs[ev.key==="Home"?0:ev.key==="End"?xs.length-1:(i+(ev.key==="ArrowUp"?xs.length-1:1))%xs.length].focus();
      }
    });
    root.append(popup);
    popup.style.left=Math.max(0,Math.min(x,innerWidth-popup.offsetWidth))+"px";
    popup.style.top=Math.max(0,Math.min(y,innerHeight-popup.offsetHeight))+"px";
    popup.querySelector("button:not(:disabled)")?.focus({preventScroll:true});
  }
  const sheet = () => area()?.querySelector("canvas.astra-native-live");
  const buttons = () => root?.querySelectorAll("[data-native-score]") || [];
  const button = (kind, text, more = "") => '<button type="button" data-native-score="'+kind+'" '+more+'>'+text+'</button>';

  const stock=window.AstraMuseScoreStock;
  const names={...stock.labels,selection:"选择过滤器",palettes:"符号面板",layout:"版面",properties:"属性",search:"搜索符号面板"};
  const escape=s=>String(s??"").replace(/[&"<>]/g,k=>({"&":"&amp;",'"':"&quot;","<":"&lt;",">":"&gt;"})[k]);
  const glyph=(name,meta="")=>{
    const code=stock.iconCodes[name];
    return code ? '<span class="astra-ms-glyph" data-musescore-icon="'+name+'" '+meta+' aria-hidden="true">&#'+code+';</span>' : '';
  };
  const iconButton=(kind, name, title, more="") =>
    button(kind,glyph(name),'data-stock-icon="'+name+'" title="'+escape(title)+'" aria-label="'+escape(title)+'" '+more);
  const CARET=glyph("SMALL_ARROW_DOWN");
  const route=(tool,value)=>["tool",tool,value];
  const content=action=>["content",action];
  const routes={
    "note-input-by-note-name":route("input-method","note-name"),
    "note-input-by-duration":route("input-method","duration"),
    "note-input-rhythm":route("input-method","rhythm"),
    "note-input-repitch":route("input-method","repitch"),
    "note-input-timewise":route("input-method","timewise"),
    "pad-note-64":route("duration","sixtyfourth"),"pad-note-32":route("duration","thirtysecond"),
    "pad-note-16":route("duration","sixteenth"),"pad-note-8":route("duration","eighth"),
    "pad-note-4":route("duration","quarter"),"pad-note-2":route("duration","half"),"pad-note-1":route("duration","whole"),
    "pad-note-128":route("duration","128"),"pad-note-256":route("duration","256"),
    "pad-note-512":route("duration","512"),"pad-note-1024":route("duration","1024"),
    "note-breve":route("duration","breve"),"note-longa":route("duration","long"),
    "pad-dot":route("dot","toggle"),"pad-dot2":route("dot","2"),"pad-dot3":route("dot","3"),"pad-dot4":route("dot","4"),
    "pad-rest":route("rest","toggle"),"note-rest":route("rest","toggle"),
    "flat2":route("accidental","flat2"),flat:route("accidental","flat"),nat:route("accidental","natural"),
    sharp:route("accidental","sharp"),sharp2:route("accidental","sharp2"),
    tie:route("tie","toggle"),"add-slur":route("slur","toggle"),
    "add-marcato":route("articulation","marcato"),"add-sforzato":route("articulation","accent"),
    "add-tenuto":route("articulation","tenuto"),"add-staccato":route("articulation","staccato"),
    flip:route("flip","toggle"),"voice-1":route("voice","1"),"voice-2":route("voice","2"),
    "voice-3":route("voice","3"),"voice-4":route("voice","4"),
    "move-up":route("cross-staff","up"),"move-down":route("cross-staff","down"),
    "pitch-up":["pitchUp"],"pitch-down":["pitchDown"],
    "octave-up":["octaveUp"],"octave-down":["octaveDown"],
    undo:["undo"],redo:["redo"],"file-save":["save"],"file-export":["exportMusicXml"],print:["print"],
    "edit-element":["textEdit"],"title-text":["textTitle"],"composer-text":["textComposer"],
    "staff-text":["textStaff"],"system-text":["textSystem"],
    "subtitle-text":["textAdd","subtitle"],
    "poet-text":["textAdd","poet"],
    "part-text":["textAdd","part"],
    "frame-text":["textAdd","frame"],
    "expression-text":["textAdd","expression"],
    "rehearsalmark-text":["textAdd","rehearsalmark"],
    "fingering-text":["textAdd","fingering"],
    "chord-text":["textAdd","chord"],
    "roman-numeral-text":["textAdd","roman"],
    "nashville-number-text":["textAdd","nashville"],
    "lyrics":["textAdd","lyrics"],
    "tempo":["textAdd","tempo"],
    "figured-bass":["textAdd","figured-bass"],
    "chord-tie":route("chord-tie",""),"lv":route("lv",""),
    "notation-cut":content("cut"),"notation-copy":content("copy"),"notation-paste":content("paste"),"notation-paste-half":content("paste-half"),"notation-paste-double":content("paste-double"),"notation-paste-special":content("paste-special"),"notation-swap":content("swap-selection"),"repeat-sel":content("repeat-selection"),
    "notation-delete":content("delete"),"insert-measure":content("measure-add-before"),
    "append-measure":content("measure-add-end"),"insert-measures":["measureInsertDialog","measure-add-before"],"insert-measures-after-selection":["measureInsertDialog","measure-add-after"],"insert-measures-at-start-of-score":["measureInsertDialog","measure-add-start"],"append-measures":["measureInsertDialog","measure-add-end"],"measure-properties":["measurePropertiesDialog"],"transpose":["transposeDialog"],"split-measure":content("measure-split"),"join-measures":content("measure-join"),
    "add-hairpin":route("hairpin","cresc"),"add-hairpin-reverse":route("hairpin","dim"),
    "add-8va":route("ottava","8va"),"add-8vb":route("ottava","8vb"),
    "toggle-system-lock":route("system-layout","lock"),"measures-per-system":["systemLocksDialog"],"make-into-system":route("system-layout","make"),"time-delete":content("time-delete"),"del-empty-measures":content("remove-empty-tail"),
    "move-measure-to-prev-system":route("system-layout","previous"),"move-measure-to-next-system":route("system-layout","next"),
    "zoom-in":["larger"],"zoom-out":["smaller"]
  };
  const tupletCodes=["duplet","triplet","quadruplet","quintuplet","sextuplet","septuplet","octuplet","nonuplet"];
  tupletCodes.forEach((k,i)=>routes[k]=route("tuplet","auto:"+(i+2)));
  for(const v of ["12","13","14","23","24","34"]) routes["voice-x"+v]=route("swap-voices",v);
  for(const n of "cdefgab"){routes["note-"+n]=route("note-letter",n);routes["chord-"+n]=route("chord-letter",n);}
  const localTitles={"transpose":"移调…","measure-properties":"小节属性…","insert-measures":"在选区前插入多个小节…","insert-measures-after-selection":"在选区后插入多个小节…","insert-measures-at-start-of-score":"在乐谱开头插入多个小节…","append-measures":"在乐谱末尾插入多个小节…","notation-paste-half":"以一半时值粘贴","notation-paste-double":"以两倍时值粘贴","notation-paste-special":"按当前时值比例粘贴","notation-swap":"交换所选与记谱剪贴板","repeat-sel":"重复所选","file-export":"导出 MusicXML…","print":"导出 PDF…",
    "toggle-system-lock":"切换谱表行锁定","measures-per-system":"每行小节…","make-into-system":"将选中小节并为一行","time-delete":"删除所选时段（后续前移）","del-empty-measures":"删除末尾空小节",
    "move-measure-to-prev-system":"移至上一谱表行","move-measure-to-next-system":"移至下一谱表行"};
  // All shortcuts shown here are implemented in this embedded view. Their source
  // is the upstream Windows XML (except native platform Undo and Redo).
  const shortcutCodes=new Set([
    "note-input-by-note-name","note-input-by-duration","note-input-repitch","pad-note-64","pad-note-32","pad-note-16","pad-note-8",
    "pad-note-4","pad-note-2","pad-note-1","note-breve","note-longa","pad-dot","note-rest","flat","nat","sharp",
    "tie","add-slur","add-marcato","add-sforzato","add-tenuto","add-staccato","flip","voice-1","voice-2","voice-3","voice-4",
    "move-up","move-down","pitch-up","pitch-down","notation-cut","notation-copy","notation-paste","notation-delete",
    "add-hairpin","add-hairpin-reverse","chord-tie","chord-text","lyrics",...tupletCodes
  ]);
  const shortcut=key=>key==="undo"?"Ctrl+Z":key==="redo"?"Ctrl+Y":key==="file-save"?"Ctrl+S":key==="notation-paste-half"?"Ctrl+Shift+Q":key==="notation-paste-double"?"Ctrl+Shift+W":key==="repeat-sel"?"R":key==="time-delete"?"Ctrl+Del":
    shortcutCodes.has(key)?(stock.actions[key]?.shortcuts?.[0]||""):"";
  const actionTitle=code=>localTitles[code]||stock.actions[code]?.title||code;
  function control(code,inStrip=false,label) {
    const action=(["notation-paste-half","notation-paste-double","notation-paste-special","notation-swap","repeat-sel"].includes(code) ? (stock.actions[code]||{icon:""}) : stock.actions[code]) || (["make-into-system","time-delete","del-empty-measures"].includes(code) ? {icon:""} : null) || (["insert-measures","insert-measures-after-selection","insert-measures-at-start-of-score","append-measures"].includes(code) ? {icon:""} : null) || (code==="measure-properties" ? {icon:""} : null) || (code==="transpose" ? {icon:""} : null), mapping=routes[code];
    if(!action||!mapping)return "";
    const [kind,one,two]=mapping, key=shortcut(code), title=label??actionTitle(code);
    const labels=title+(key?"  "+key:"");
    const attrs='data-stock-action="'+code+'" data-stock-icon="'+action.icon+'" aria-label="'+escape(title)+'" title="'+escape(labels)+'"';
    const data=kind==="tool"?' data-score-tool="'+one+'" data-score-value="'+two+'"':
      kind==="content"?' data-score-content="'+one+'"':kind==="measureInsertDialog"?' data-insert-position="'+one+'"':kind==="textAdd"?' data-score-value="'+one+'"':"";
    const inner=inStrip ? glyph(action.icon) : '<span class="astra-ms-menu-label">'+escape(title)+'</span>'+
      (key?'<kbd>'+escape(key)+'</kbd>':'');
    return button(kind,inner,attrs+data+(inStrip?' class="astra-ms-tool"':' class="astra-ns-mi"'));
  }
  const toolGroup=(items)=>'<div class="astra-native-tool-group" role="group">'+items.join("")+'</div>';
  const dropMenu=(cls,name,summary,body,attrs="")=>
    '<details class="astra-ns-menu '+cls+'" '+attrs+'><summary title="'+escape(name)+'" aria-label="'+escape(name)+'">'+summary+'</summary>'+
    '<div class="astra-ns-menu-pop" role="group" aria-label="'+escape(name)+'">'+body+'</div></details>';
  const submenu=(name,body)=>'<details class="astra-ms-submenu"><summary>'+escape(name)+glyph("SMALL_ARROW_RIGHT")+
    '</summary><div>'+body+'</div></details>';
  const commandList=(...codes)=>codes.map(x=>control(x)).join("");
  const menuTitle=text=>'<span>'+escape(text)+'</span>';
  const addContent=()=>submenu(names.notes,commandList(...[..."cdefgab"].map(x=>"note-"+x))+
    '<hr>'+commandList(...[..."cdefgab"].map(x=>"chord-"+x)))+
    submenu(names.measures,commandList("insert-measure","append-measure","insert-measures","insert-measures-after-selection","insert-measures-at-start-of-score","append-measures"))+
    submenu(names.text,commandList("title-text","subtitle-text","composer-text","poet-text","part-text","frame-text","system-text","staff-text","expression-text","rehearsalmark-text","fingering-text","chord-text","roman-numeral-text","nashville-number-text","lyrics","tempo","figured-bass"))+
    submenu(names.lines,commandList("add-slur","chord-tie","lv","add-hairpin","add-hairpin-reverse","add-8va","add-8vb"));
  const optionalToolbar=()=>stock.stockToolbar.flat().filter(([code,on])=>!on&&routes[code]&&stock.actions[code]?.icon);
  const defaultToolbar=()=>stock.stockToolbar.map(group=>group.filter(([,on])=>on).map(([code])=>code));
  let toolbarConfig=new Map();
  try {
    for(const [k,v] of JSON.parse(localStorage.getItem("astra-score-musescore465-toolbar")||"[]"))
      if(typeof v==="boolean")toolbarConfig.set(k,v);
  } catch {}
  function buildToolbar() {
    const groups=stock.stockToolbar.map(group=>group.filter(([code,on])=>routes[code]||code==="tuplet")
      .filter(([code,on])=>toolbarConfig.get(code)??on).map(([code])=>code)).filter(g=>g.length);
    const strip=groups.map(group=>toolGroup(group.map(code=>code==="tuplet"?rhythmMenu("tuplet"):control(code,true)))).join("");
    const config=stock.stockToolbar.map(group=>group.filter(([code])=>routes[code]&&stock.actions[code]?.icon||code==="tuplet")
      .map(([code,on])=>'<label class="astra-ms-check">'+glyph(stock.actions[code].icon)+'<span>'+escape(actionTitle(code))+
      '</span><input type="checkbox" data-stock-visibility="'+code+'"'+((toolbarConfig.get(code)??on)?' checked':'')+'></label>').join("")).filter(Boolean).join("<hr>");
    return '<span class="astra-ms-grip" role="button" tabindex="0" aria-label="移动音符输入工具栏" title="拖动工具栏；双击可浮动或停靠">'+glyph("TOOLBAR_GRIP")+'</span>'+
      '<div class="astra-ns-section astra-ns-entry">'+strip+
      toolGroup([dropMenu("astra-ms-add",names.add,glyph("PLUS"),addContent(),'data-stock-action="add"'),
        dropMenu("astra-ms-customize",names.customize,glyph("SETTINGS_COG"),config,'data-stock-action="customize"')])+'</div>';
  }
  // Phase 2 eligibility from native selectionKind (Note/Rest/BarLine/Range/Other/"").
  // Phase 1 tools keep their existing editing-only gating.
  const REPEAT = new Set(["start-repeat","end-repeat","end-start-repeat"]);
  const noteOrRest = s => s === "Note" || s === "Rest";
  const ELIGIBLE = {
    articulation: (s, input) => s === "Note" || input,
    slur: (s, input) => input || noteOrRest(s) || s==="Range",
    tie: (s,input) => input || s==="Note" || s==="Range",
    "chord-tie": (s,input) => input || s==="Note" || s==="Range",
    lv: (s,input) => input || s==="Note" || s==="Range",
    hairpin: s => noteOrRest(s) || s==="Range",
    ottava: (s,input) => input || noteOrRest(s) || s==="Range",
    flip: (s, input) => !input && noteOrRest(s),
    arpeggio: (s, input) => !input && s === "Note",
    barline: (s, input, v) => s === "BarLine" || (!REPEAT.has(v) && !input && noteOrRest(s)),
    layout: (s, input) => s === "BarLine" || (!input && noteOrRest(s))};
  const eligible = (tool, value) => {
    if (tool==="tuplet") return !!response?.selectedRhythm || !!response?.input;
    if (tool==="system-layout") {
      const l=response?.systemLayout;
      return !response?.input&&!!l&&Object.hasOwn(l,"locked")&&(value==="lock"||!!l[value]);
    }
    if (tool==="cross-staff") return !response?.input && !!response?.selection?.hasRhythm &&
      (["list","range"].includes(response.selection.mode) || (value==="up" ? response.displayStaff>0 : response.displayStaff===0));
    if (tool==="swap-voices") return !response?.input && response?.selectionKind==="Range";
    const rule = ELIGIBLE[tool];
    if (!rule) return true;
    const kind = response?.selectionKind;
    return typeof kind === "string" && rule(kind, !!response.input, value);
  };
  let browseHold=false, transportScope=null, followKey=null, followTarget=null, acceptedPlaybackFrame=null, autoPan=true;
  // MuseScore (AbstractNotationPaintView::moveCanvas) stops following for 2 s
  // after the person moves the canvas, then follows the playback cursor again.
  let followPausedUntil=0, programmaticScrollAt=-1;
  const pauseFollow=()=>{followPausedUntil=performance.now()+2000;};
  const followPaused=()=>performance.now()<followPausedUntil;
  const ownedScoreSurface=()=>Boolean(root?.isConnected&&root.parentElement?.id==="session-body"&&
    root.closest("#workspace > .workbench[data-view=score]")&&!root.closest(".temp-right"));
  const playbackCursor=window.AstraScorePlaybackCursor?.create({
    request(raw){
      if(typeof backend?.requestCursorLayout!=="function"||!backend?.cursorLayoutResponse?.connect)return false;
      backend.requestCursorLayout(raw);return true;
    },
    canRequest:(frame,read)=>ownedScoreSurface()&&frame===acceptedPlaybackFrame&&frame.root===root&&
      frame.project===project&&frame.generation===nativeDocumentGeneration&&frame.revision===response?.revision&&
      frame.page===response?.page&&transportScope?.path===project&&!response.textEditing&&
      (!read||!busy&&!nativePointer),
    onMove:()=>{if(transportScope?.playing&&autoPan&&!followPaused())adjustCanvasToCursor();}
  });
  // Port of AbstractNotationPaintView::adjustCanvasPosition with
  // needAdjustCanvasVerticallyWhilePlayback: nothing moves while the cursor
  // is fully in view; otherwise jump once, keeping a 3-spatium border.
  function adjustCanvasToCursor(){
    const paper=area(), canvas=sheet(), cursor=playbackCursor?.box();
    if(!paper||!canvas||!cursor)return false;
    const view={left:paper.scrollLeft,top:paper.scrollTop,width:paper.clientWidth,height:paper.clientHeight};
    if(view.width*view.height<cursor.w*cursor.h)return false;
    const right=view.left+view.width, bottom=view.top+view.height;
    if(cursor.x>=view.left&&cursor.y>=view.top&&cursor.x+cursor.w<=right&&cursor.y+cursor.h<=bottom)return false;
    // 3 spatium; a default 1.75 mm spatium is 1/120 of an A4 page width.
    const border=3*canvas.getBoundingClientRect().width/120;
    let x=view.left,y=view.top;
    if(cursor.x<view.left)x=cursor.x-border;
    else if(cursor.x>right)x=cursor.x+cursor.w-view.width+border;
    else if(view.width>=cursor.w&&cursor.x+cursor.w>right)x=cursor.x-border;
    const intersects=cursor.x<right&&cursor.x+cursor.w>view.left&&cursor.y<bottom&&cursor.y+cursor.h>view.top;
    if(!intersects||playbackCursor.systemCount()>1){
      if(cursor.y<view.top&&cursor.y+cursor.h<bottom)y=cursor.y-border;
      else if(cursor.y>bottom)y=cursor.y+cursor.h-view.height+border;
      else if(view.height>=cursor.h&&cursor.y+cursor.h>bottom)y=cursor.y-border;
    }
    if(x===view.left&&y===view.top)return false;
    programmaticScrollAt=performance.now();
    // Moves close together (a page turn, then the cursor) keep the view from before the first.
    if(followJump?.paper===paper&&programmaticScrollAt-followJump.at<400)followJump.at=programmaticScrollAt;
    else followJump={paper,left:view.left,top:view.top,at:programmaticScrollAt};
    paper.scrollLeft=x;paper.scrollTop=y;
    return true;
  }
  // A press that comes right after following moved the view was aimed at what
  // was there before: put that view back, so the click lands where it was
  // aimed. While the button is down the view never moves by itself.
  let followJump=null, restoredPress=null;
  document.addEventListener("pointerdown",event=>{
    const paper=area();
    restoredPress=null;
    if(!transportScope?.playing||!paper?.contains(event.target))return;
    const jump=followJump;followJump=null;
    pauseFollow();
    if(jump?.paper===paper&&performance.now()-jump.at<400){
      programmaticScrollAt=performance.now();
      paper.scrollLeft=jump.left;paper.scrollTop=jump.top;
      // This press was hit-tested in the moved view: it starts no drag or
      // press selection; its click is given to what is under it now.
      restoredPress={paper,at:performance.now()};
      event.stopImmediatePropagation();
    }
  },true);
  document.addEventListener("click",event=>{
    const r=restoredPress;restoredPress=null;
    if(!r||!event.isTrusted||performance.now()-r.at>2000||!r.paper.isConnected)return;
    const el=document.elementFromPoint(event.clientX,event.clientY);
    if(!el||el===event.target||!r.paper.contains(el))return;
    event.stopImmediatePropagation();event.preventDefault();
    el.dispatchEvent(new MouseEvent("click",{bubbles:true,cancelable:true,clientX:event.clientX,clientY:event.clientY,
      shiftKey:event.shiftKey,ctrlKey:event.ctrlKey,altKey:event.altKey,metaKey:event.metaKey,button:0,detail:1}));
  },true);
  function paintNativePlaybackCursor(path,status,playing){
    const frame=acceptedPlaybackFrame;
    if(!playbackCursor)return;
    if(path!==project||!ownedScoreSurface()||!frame||frame.root!==root||frame.generation!==nativeDocumentGeneration||
       frame.revision!==response?.revision||frame.page!==response?.page){
      playbackCursor.invalidate();return;
    }
    playbackCursor.frameAccepted(frame);
    // Browsing keeps the paused cursor as the "play from here" mark.
    playbackCursor.paint(path,status,playing&&!response.textEditing,
      {hold:!response.editing&&(browseHold||Number(status?.position)>0)});
  }
  const rangeInputs=new Map([["editor-music-start","start"],["editor-music-end","end"],
                            ["editor-range-start","start"],["editor-range-end","end"]]);
  const footerButtons=new Set(["editorJumpLeft","editorJumpRight","editorLoop","editorMetronome"]);
  const element=id=>document.getElementById(id);

  function scrollFollow(target) {
    followTarget=null;
    if (!target || response?.page!==target.page) return;
    adjustCanvasToCursor();
  }
  function paintTransport(path, status, playing, autoFollow=true) {
    if (!path) {
      if(transportScope){
        const pos=element("editor-play-position-seconds"),speed=element("transport-speed-popup");
        if(pos)pos.disabled=false;if(speed)speed.inert=false;
      }
      playbackCursor?.invalidate();
      transportScope=null;followTarget=followKey=null;followPausedUntil=0;browseHold=false;return;
    }
    if (!transportScope || transportScope.path!==path) {followTarget=followKey=null;followPausedUntil=0;}
    // MuseScore re-enables following whenever playback starts.
    if (!transportScope?.playing && playing) followPausedUntil=0;
    autoPan=autoFollow;
    transportScope={path,status,playing};
    paintNativePlaybackCursor(path,status,playing);
    // Keep the old visuals only as a fallback when this separate score toolbar isn't attached.
    const container=window.AstraScoreTransport?.active()?null:element("musical-transport");
    if(!status)return;
    if(container){
    for(const [id,side] of rangeInputs){
      const f=element(id); if(!f)continue;
      f.disabled=false;
      f.title="当前谱面位置（秒）";
      if(document.activeElement!==f) {
        const formatted=Number(status[side]).toFixed(3);
        if(f.value!==formatted)f.value=formatted;
      }
      const hint=element(id+"-seconds");
      if(hint && hint.textContent!=="秒")hint.textContent="秒";
    }
    for(const button of container.querySelectorAll("[data-do]")){
      const action=button.dataset.do;if(!footerButtons.has(action))continue;
      button.disabled=action==="editorMetronome";
      if(action==="editorLoop") button.setAttribute("aria-pressed",String(status.loop));
    }
    const at=element("editor-play-position");
    if(at && document.activeElement!==at)at.value=Number(status.position).toFixed(3);
    const position=element("editor-play-position-seconds");
    if(position)position.disabled=true;
    const speed=element("transport-speed-popup");
    if(speed)speed.inert=true;
    }
    // The view is on the played page: the next turn to any page is a new one,
    // also back to a page followed to before (a rewind, loop or seek back).
    if(status.page&&response?.page===status.page)followKey=null;
    // The cursor itself drives following on this page (onMove). Only a page
    // turn needs the worker; MuseScore follows in edit mode too.
    if (!autoFollow || !playing || !status.page || followPaused() ||
        status.revision!==response?.revision || busy || response.page===status.page) return;
    const key=[status.revision,status.page].join(":");
    if(key===followKey)return;
    followKey=key;
    followTarget=status;
    if(!send("followPage",{page:status.page}))followKey=followTarget=null;
  }
  function scoreFooterEvent(event) {
    const scope=transportScope;
    if(!scope?.status || !element("musical-transport")?.contains(event.target))return;
    let {start,end,loop}=scope.status;
    if(event.type==="change"){
      const positionId=event.target.id==="editor-play-position";
      if(positionId){
        const position=Number(event.target.value);
        event.stopImmediatePropagation();
        if(Number.isFinite(position)&&position>=0&&position<=scope.status.duration)
          backend?.controlPlayback?.(scope.path,"seek",position);
        else event.target.value=Number(scope.status.position).toFixed(3);
        return;
      }
      const side=rangeInputs.get(event.target.id);
      if(!side)return;
      const value=Number(event.target.value);
      if(side==="start")start=value;else end=value;
      event.stopImmediatePropagation();
      if(Number.isFinite(start)&&Number.isFinite(end)&&start>=0&&start<end&&end<=scope.status.duration)
        backend?.setScoreLoop?.(scope.path,start,end,loop);
      else event.target.value=Number(scope.status[side]).toFixed(3);
      return;
    }
    const action=event.target.closest("[data-do]")?.dataset.do;
    if(!footerButtons.has(action))return;
    event.preventDefault();event.stopImmediatePropagation();
    if(action==="editorLoop")backend?.setScoreLoop?.(scope.path,start,end,!loop);
    if(action==="editorJumpLeft" || action==="editorJumpRight")
      backend?.controlPlayback?.(scope.path,"seek",action==="editorJumpLeft"?start:end);
  }
  for(const kind of ["click","change"])document.addEventListener(kind,scoreFooterEvent,true);
  document.addEventListener("wheel",event=>{if(transportScope?.playing && area()?.contains(event.target))pauseFollow();}, {passive:true});
  // Background page paints wait until the person pauses (keys, clicks, drags).
  for(const kind of ["keydown","pointerdown"])document.addEventListener(kind,event=>{
    if(root?.contains(event.target)){pages.quietUntil=performance.now()+600;}
  },true);
  document.addEventListener("scroll",event=>{
    if(event.target!==area())return;
    const label=root?.querySelector(".astra-native-pages");
    if(!pages.viewFrame)pages.viewFrame=requestAnimationFrame(()=>{
      pages.viewFrame=0;pages.view=pageInView();redrawVectorView();
      if(label&&response?.pages){const t=pages.view+" / "+response.pages;if(label.textContent!==t)label.textContent=t;}
    });
    pages.quietUntil=Math.max(pages.quietUntil,performance.now()+400);
    schedulePages(400);
  },true);
  // Popover menus also close when the pointer goes down anywhere outside them (incl. outside the score).
  document.addEventListener("pointerdown",event=>{
    if(!root?.isConnected)return;
    for(const menu of root.querySelectorAll(".astra-ns-menu[open],.astra-native-rhythm-menu[open]"))
      if(!menu.contains(event.target))menu.open=false;
  },true);


  function rhythmMenu(kind) {
    const tuplet = kind === "tuplet";
    const title = tuplet ? actionTitle("tuplet") : "自定义拍号";
    const form='<form data-native-rhythm="'+kind+'"><label><span>'+(tuplet?"连音数量":"分子")+
      '</span><input required type="number" name="n" value="'+(tuplet?"3":"7")+'" min="1" max="'+(tuplet?"9999":"64")+'"></label>'+
      '<label><span>'+(tuplet?"基准数量":"分母")+'</span>'+
      (tuplet?'<input required type="number" name="d" value="2" min="1" max="9999">':
      '<select name="d"><option>1</option><option>2</option><option>4</option><option selected>8</option><option>16</option><option>32</option><option>64</option></select>')+
      '</label>'+(tuplet?'<label><span>数字</span><select name="number"><option value="number">数字</option><option value="ratio">比例</option><option value="none">无</option></select></label><label><span>括号</span><select name="bracket"><option value="auto">自动</option><option value="show">显示</option><option value="hide">隐藏</option></select></label>':'')+
      '<button type="submit">应用</button>'+
      (tuplet?'':'<button type="submit" data-rhythm-rebar title="从这里按新拍号重划小节，保留音符位置">重划小节</button>')+'</form>';
    return '<details class="astra-native-rhythm-menu is-'+kind+'"'+(tuplet?' data-stock-action="tuplet"':'')+
      '><summary title="'+escape(title)+'" aria-label="'+escape(title)+'">'+
      (tuplet?glyph("NOTE_TUPLET"):menuTitle(title)+CARET)+'</summary><div class="astra-native-rhythm-popover">'+
      (tuplet?commandList(...tupletCodes)+submenu(actionTitle("tuplet-dialog"),form):form)+'</div></details>';
  }

  function systemLockDialog() {
    return '<dialog class="astra-native-system-lock-dialog" aria-labelledby="astra-lock-title" '+
      'style="position:fixed;inset:auto auto 50% 50%;transform:translate(-50%,50%);max-width:calc(100% - 24px);'+
      'max-height:calc(100% - 24px);overflow:auto;margin:0;padding:16px;border-radius:10px;border:1px solid #75839f;'+
      'background:Canvas;color:CanvasText;z-index:10000">'+
      '<form data-system-lock-plan><h3 id="astra-lock-title">每行小节</h3><p data-lock-scope></p>'+
      '<p><label><input type="radio" name="mode" value="interval" checked> 按固定小节数锁定</label> '+
      '<input type="number" name="interval" aria-label="每行小节数" min="1" max="99" step="1" value="4" required></p>'+
      '<p><label><input type="radio" name="mode" value="existing"> 锁定现有各谱行</label></p>'+
      '<p><label><input type="radio" name="mode" value="remove"> 移除现有谱行锁</label></p>'+
      '<p style="display:flex;gap:10px;justify-content:flex-end"><button type="button" data-lock-cancel>取消</button>'+
      '<button type="submit">应用</button></p></form></dialog>';
  }
  function measureInsertDialog() {
    return '<dialog class="astra-native-insert-measures-dialog" aria-labelledby="astra-insert-title" '+
      'style="position:fixed;inset:auto auto 50% 50%;transform:translate(-50%,50%);max-width:calc(100% - 24px);'+
      'max-height:calc(100% - 24px);overflow:auto;margin:0;padding:16px;border-radius:10px;border:1px solid #75839f;'+
      'background:Canvas;color:CanvasText;z-index:10000">'+
      '<form data-measure-insert><h3 id="astra-insert-title">插入多个小节</h3>'+
      '<p><label>位置 <select name="position"><option value="measure-add-before">选区前</option>'+
      '<option value="measure-add-after">选区后</option><option value="measure-add-start">乐谱开头</option>'+
      '<option value="measure-add-end">乐谱末尾</option></select></label></p>'+
      '<p><label>小节数 <input name="count" type="number" min="1" max="64" step="1" value="1" required></label></p>'+
      '<p style="display:flex;gap:10px;justify-content:flex-end"><button type="button" data-insert-cancel>取消</button>'+
      '<button type="submit">插入</button></p></form></dialog>';
  }
  function measurePropertiesDialog() {
    return '<dialog class="astra-native-measure-properties-dialog" aria-labelledby="astra-measure-title" '+
      'style="position:fixed;inset:auto auto 50% 50%;transform:translate(-50%,50%);max-width:calc(100% - 24px);'+
      'max-height:calc(100% - 24px);overflow:auto;margin:0;padding:16px;border-radius:10px;border:1px solid #75839f;'+
      'background:Canvas;color:CanvasText;z-index:10000">'+
      '<form data-measure-properties><h3 id="astra-measure-title">小节属性</h3><fieldset><legend>该小节中的谱表</legend><div data-measure-staves></div></fieldset>'+
      '<p><label>实际时值（分子） <input name="n" type="number" min="1" max="64" required></label> / '+
      '<label>分母 <select name="d">'+[1,2,4,8,16,32,64].map(n=>'<option>'+n+'</option>').join("")+'</select></label></p>'+
      '<p><label>编号 <select name="numberMode"><option value="0">自动</option><option value="1">始终显示</option><option value="2">始终隐藏</option></select></label> '+
      '<label>编号偏移 <input name="numberOffset" type="number" min="-999" max="999" required></label></p>'+
      '<p><label>谱面宽度比例 <input name="stretch" type="number" step="any" min="0.25" max="4" required></label> '+
      '<label data-repeat-count>重复次数 <input name="repeatCount" type="number" min="1" max="9999" required></label></p>'+
      '<p><label><input name="excludeNumber" type="checkbox"> 不计入小节编号</label> '+
      '<label><input name="breakMMR" type="checkbox"> 从这里断开多小节休止符</label></p>'+
      '<p style="display:flex;gap:10px;justify-content:flex-end"><button type="button" data-measure-cancel>取消</button>'+
      '<button type="submit" data-measure-step="previous">上一小节</button>'+
      '<button type="submit" data-measure-step="next">下一小节</button>'+
      '<button type="submit">应用</button></p><p data-measure-error role="status" hidden></p></form></dialog>';
  }
  function openMeasureProperties(refresh=false) {
    const dlg=root?.querySelector(".astra-native-measure-properties-dialog"), m=response?.measureContext;
    if(!dlg || (dlg.open&&!refresh) || busy || !response?.editing || response.input || response.textEditing || response.readOnly || !m?.staves)return;
    const form=dlg.querySelector("form"), f=form.elements;
    dlg.querySelector("h3").textContent="小节属性 · "+m.number;
    f.n.value=m.actual[0];f.d.value=m.actual[1];f.numberMode.value=m.numberMode;f.numberOffset.value=m.numberOffset;
    f.stretch.value=m.stretch;f.repeatCount.value=m.repeatCount;
    form.querySelector('[data-measure-step="previous"]').disabled=!m.previousMeasure;
    form.querySelector('[data-measure-step="next"]').disabled=!m.nextMeasure;
    form.querySelector('[data-measure-error]').hidden=true;
    f.repeatCount.disabled=!m.repeatEnd;form.querySelector("[data-repeat-count]").hidden=!m.repeatEnd;f.excludeNumber.checked=m.excludeNumber;f.breakMMR.checked=m.breakMMR;
    const staves=form.querySelector("[data-measure-staves]");staves.replaceChildren();
    m.staves.forEach((staff,index)=>{
      const row=document.createElement("p");row.dataset.measureStaff=String(index);
      row.append(document.createTextNode("谱表 "+(index+1)+" "));
      for(const [code,name] of [["visible","显示"],["stemless","无符干"]]){
        const input=document.createElement("input"), label=document.createElement("label");
        input.type="checkbox";input.dataset.staffField=code;input.checked=staff[code];
        label.append(input,document.createTextNode(" "+name+" "));row.append(label);
      }
      staves.append(row);
    });
    if(!dlg.open)dlg.showModal();
  }
  // Indexes exactly match engraving/dom/utils.cpp intervalList in MuseScore 4.6.5.
  const transposeIntervals=["纯一度","增一度","减二度","小二度","大二度","增二度","减三度","小三度","大三度","增三度",
    "减四度","纯四度","增四度","减五度","纯五度","增五度","减六度","小六度","大六度","增六度",
    "减七度","小七度","大七度","增七度","减八度","纯八度"];
  function transposeDialog() {
    const keys=["C♭","G♭","D♭","A♭","E♭","B♭","F","C","G","D","A","E","B","F♯","C♯"];
    const options=(list,offset=0)=>list.map((name,i)=>'<option value="'+(i+offset)+'">'+name+'</option>').join("");
    return '<dialog class="astra-native-transpose-dialog" aria-labelledby="astra-transpose-title" '+
      'style="position:fixed;inset:auto auto 50% 50%;transform:translate(-50%,50%);max-width:calc(100% - 24px);'+
      'max-height:calc(100% - 24px);overflow:auto;margin:0;padding:16px;border-radius:10px;border:1px solid #75839f;'+
      'background:Canvas;color:CanvasText;z-index:10000">'+
      '<form data-transpose-plan><h3 id="astra-transpose-title">移调</h3><p data-transpose-scope></p>'+
      '<p><label>方式 <select name="mode"><option value="key">移至指定调号</option><option value="chromatic">按音程</option>'+
      '<option value="diatonic">按调内音级</option></select></label> '+
      '<label>方向 <select name="direction"><option value="up">向上</option><option value="down">向下</option><option value="closest">最近</option></select></label></p>'+
      '<p data-transpose-field="key"><label>目标调号 <select name="key">'+options(keys,-7)+'</select></label></p>'+
      '<p data-transpose-field="chromatic"><label>音程 <select name="interval">'+options(transposeIntervals)+'</select></label></p>'+
      '<p data-transpose-field="diatonic"><label>音级距离 <select name="degree">'+options(["二度","三度","四度","五度","六度","七度"],1)+'</select></label></p>'+
      '<p><label><input name="transposeKeys" type="checkbox"> <span data-transpose-key-label>同时改写调号</span></label> '+
      '<label><input name="chordNames" type="checkbox"> 同时移调和弦标记</label> '+
      '<label><input name="useDouble" type="checkbox"> 允许重升降号</label></p>'+
      '<p style="display:flex;gap:10px;justify-content:flex-end"><button type="button" data-transpose-cancel>取消</button>'+
      '<button type="submit">应用</button></p></form></dialog>';
  }
  function refreshTransposeDialog(form,state) {
    const f=form.elements, mode=f.mode.value;
    for(const row of form.querySelectorAll("[data-transpose-field]"))row.hidden=row.dataset.transposeField!==mode;
    f.direction.querySelector('[value="closest"]').disabled=mode!=="key";
    if(mode!=="key"&&f.direction.value==="closest")f.direction.value="up";
    f.transposeKeys.disabled=!state.range&&mode!=="diatonic";
    form.querySelector("[data-transpose-key-label]").textContent=mode==="diatonic"?"保留音级上的变化音":"同时改写调号";
    f.useDouble.disabled=mode==="diatonic";
  }
  function markup() {
    const file=control("file-save",false,"保存")+
      submenu("导出…",control("print",false,"PDF")+control("file-export",false,"MusicXML"));
    const edit=commandList("undo","redo")+'<hr>'+commandList("notation-cut","notation-copy","notation-paste","notation-paste-half","notation-paste-double","notation-paste-special","notation-swap","notation-delete","repeat-sel");
    const findButton=button("findScore","查找 / 跳转",'class="astra-ns-mi" aria-keyshortcuts="Control+F"');
    const view=[["palettes","F9"],["layout","F7"],["properties","F8"],["selection",""]].map(([dock,key])=>
      '<button type="button" class="astra-ms-menu-plain" data-score-dock-toggle="'+dock+
      '" aria-pressed="true" aria-keyshortcuts="'+key+'" title="'+escape(names[dock]+"  "+key)+'">'+escape(names[dock])+'</button>').join("")+
      '<hr><button type="button" class="astra-ms-menu-plain" data-score-page-flow aria-pressed="false" title="页面左右排列（关闭时上下排列）">页面左右排列</button>'+
      '<button type="button" class="astra-ms-menu-plain" data-score-vector-view aria-pressed="true" title="页面以矢量绘制：缩放时立即清晰（关闭则使用位图）">矢量显示</button>'+
      '<hr>'+[["showInvisible","显示不可见元素","编辑时以灰色显示已隐藏的元素"],["showUnprintable","显示格式符号","编辑时显示换行、分隔符等不打印的标记"],
        ["showFrames","显示框","编辑时显示标题框等框的边线"]].map(([k,label,tip])=>
        '<button type="button" class="astra-ms-menu-plain" data-score-view-config="'+k+'" aria-pressed="true" title="'+escape(tip+"；只读浏览始终按打印效果显示")+'">'+escape(label)+'</button>').join("");
    const format=commandList("measures-per-system","make-into-system","move-measure-to-prev-system","move-measure-to-next-system","toggle-system-lock");
    const tools=commandList("transpose")+submenu(names.voices,commandList("voice-x12","voice-x13","voice-x14","voice-x23","voice-x24","voice-x34"))+
      submenu(names.measures,commandList("measure-properties","split-measure","join-measures","time-delete","del-empty-measures")+rhythmMenu("meter-custom"))+
      '<hr>'+commandList("move-up","move-down","pitch-up","pitch-down")+
      '<hr><button type="button" class="astra-ms-menu-plain" data-score-tidy title="补上标题；速度记号取整，隐藏变化很小的重复速度记号（播放速度不变，可撤销）">整理谱面（标题与速度记号）</button>';
    return '<section id="astra-native-score" class="astra-native-score" aria-label="钢琴乐谱" data-score-revision="0">'+
      '<div class="astra-native-score-tools" role="group" aria-label="乐谱菜单">'+
      ["file","edit","view","addMenu","format","tools"].map((k,i)=>dropMenu("astra-ns-doc-menu",names[k],menuTitle(names[k]),[file,edit+"<hr>"+findButton,view,addContent(),format,tools][i])).join("")+
      '<div class="astra-native-score-mode">'+button("open","生成钢琴乐谱")+'</div>'+
      '<div class="astra-native-history">'+iconButton("undo","UNDO",actionTitle("undo")+"  Ctrl+Z",'data-stock-action="undo"')+
      iconButton("redo","REDO",actionTitle("redo")+"  Ctrl+Y",'data-stock-action="redo"')+'</div>'+
      // Same pair, wording and place as the Notes editor heading (and the PDF
      // score page): the trailing end of the bar, "编辑 | 只读浏览".
      '<div class="editor-heading-actions astra-native-mode-actions">'+
      '<div class="segments editor-mode-switch" role="group" aria-label="乐谱模式：编辑或只读浏览">'+
      button("edit","编辑",'aria-pressed="false" title="编辑乐谱：输入与修改音符"')+
      button("read","只读浏览",'aria-pressed="true" title="只读浏览：不修改乐谱"')+'</div></div></div>'+
      '<div class="astra-native-notation-tools" role="toolbar" aria-label="音符输入工具栏" inert>'+buildToolbar()+'</div>'+
      systemLockDialog()+
      measureInsertDialog()+
      measurePropertiesDialog()+
      transposeDialog()+
      '<form class="astra-native-text-edit astra-native-rich-text" hidden role="dialog" aria-modal="false" aria-label="原生谱面文字编辑"><div class="astra-rich-controls" role="toolbar" aria-label="文字格式与原生光标"><span class="astra-rich-role"></span><button type="button" data-rich-command="bold" title="加粗" aria-label="加粗">'+glyph("TEXT_BOLD")+'</button><button type="button" data-rich-command="italic" title="斜体" aria-label="斜体">'+glyph("TEXT_ITALIC")+'</button><button type="button" data-rich-command="underline" title="下划线" aria-label="下划线">'+glyph("TEXT_UNDERLINE")+'</button><button type="button" data-rich-command="strike" title="删除线" aria-label="删除线">'+glyph("TEXT_STRIKE")+'</button><button type="button" data-rich-command="subscript" aria-label="下标">'+glyph("TEXT_SUBSCRIPT")+'</button><button type="button" data-rich-command="superscript" aria-label="上标">'+glyph("TEXT_SUPERSCRIPT")+'</button><button type="button" data-rich-command="select-all">选中全文</button><button type="button" data-rich-command="cursor-start">移到开头</button><button type="button" data-rich-command="cursor-end">移到末尾</button></div><div class="astra-rich-draft"><textarea rows="1" class="astra-native-text-input" aria-label="在乐谱原位编辑文字"></textarea><span class="astra-inline-text-hint">在乐谱原位输入</span><button type="submit" data-rich-submit="finish">完成</button></div></form>'+

      '<div class="astra-native-workspace">'+
      '<aside class="astra-native-palette" id="astra-native-score-palette" aria-label="符号面板">'+
      '<header class="astra-native-palette-header" role="tablist" aria-label="乐谱侧栏">'+
      '<div class="astra-ms-palette-tab-wrap"><button type="button" role="tab" data-score-dock="palettes" aria-selected="true" class="astra-native-palette-tab">符号面板</button>'+
      '<button type="button" class="astra-ms-palette-settings" data-palette-settings aria-label="符号面板选项" aria-expanded="false">'+glyph("MENU_THREE_DOTS")+'</button></div>'+
      '<button type="button" role="tab" data-score-dock="layout" aria-selected="false" class="astra-native-palette-tab">版面</button>'+
      '<button type="button" role="tab" data-score-dock="properties" aria-selected="false" class="astra-native-palette-tab astra-inspector-toggle">属性</button><button type="button" role="tab" data-score-dock="selection" aria-selected="false" class="astra-native-palette-tab">过滤</button></header>'+
      '<div class="astra-ms-palette-content" role="tabpanel">'+
      '<div class="astra-ms-palette-controls"><button type="button" class="astra-ms-add-palettes" data-palette-manage aria-haspopup="dialog" aria-expanded="false">面板管理</button>'+
      '<button type="button" class="astra-native-palette-search-toggle" aria-expanded="false" aria-controls="astra-native-score-palette-search" title="搜索符号面板  Ctrl+F9" aria-label="搜索符号面板" aria-keyshortcuts="Control+F9">'+glyph("SEARCH")+'</button></div>'+
      '<div class="astra-native-palette-search" hidden><input id="astra-native-score-palette-search" class="astra-native-catalogue-search" type="search" placeholder="搜索所有符号面板" aria-label="搜索符号面板"></div>'+
      '<div class="astra-native-palette-scroll"></div><p class="astra-native-catalogue-notice" role="status" hidden></p></div>'+
      '<div class="astra-ms-layout-dock" role="tabpanel" hidden><div class="astra-ms-layout" role="tree" aria-label="版面"></div></div>'+
      '<div class="astra-ms-inspector-dock" role="tabpanel" hidden></div><div class="astra-native-filter-dock" role="tabpanel" hidden></div></aside>'+
      '<div class="astra-native-canvas">'+
      '<div class="astra-native-doc-tabs"><span class="astra-native-doc-tab"><span class="astra-native-dirty" hidden>*</span><span class="astra-native-doc-name"></span></span></div>'+
      '<div class="paper-area astra-native-paper" tabindex="0" role="region" aria-label="原位乐谱画布">'+
      '<canvas class="astra-native-live" aria-label="钢琴谱" hidden></canvas></div></div></div>'+
      '<div class="astra-native-statusbar">'+
      '<div class="astra-native-score-status" role="status">从选定的钢琴 MIDI 生成乐谱。</div>'+
      '<small class="astra-ms-attribution" lang="en">MuseScore</small>'+
      '<div class="astra-native-zoom">'+iconButton("prev","CHEVRON_LEFT","上一页")+'<output class="astra-native-pages"></output>'+iconButton("next","CHEVRON_RIGHT","下一页")+
      '<span class="astra-native-sep" aria-hidden="true"></span>'+
      iconButton("smaller","ZOOM_OUT",actionTitle("zoom-out"))+iconButton("larger","ZOOM_IN",actionTitle("zoom-in"))+
      button("fit","100%",'class="astra-native-zoom-label" title="恢复到 100%" aria-label="当前缩放，点击恢复到 100%"')+'</div></div>'+
      '</section>';
  }
  const filterNames={elements:["力度","渐强/渐弱","指法","歌词","和弦符号","其他文字","发音记号","装饰音","连音线","延音线","数字低音","八度线","踏板线","其他线","琶音","滑音","指板图","呼吸记号","震音","倚音"],
    voices:["声部 1","声部 2","声部 3","声部 4"],indices:["最高音","第七音","第六音","第五音","第四音","第三音","第二音","最低音"]};
  function syncSelectionFilters(waiting){
    const panel=root.querySelector(".astra-native-filter-dock");
    if(!panel)return;
    const state=response?.filters;
    const sig=JSON.stringify(state||{});
    if(sig!==panel.dataset.signature){
      const active=panel.contains(document.activeElement)?document.activeElement.dataset.selectionFilter:null;
      const row=(group,i,label,checked)=>'<label style="display:block;padding:2px 6px"><input type="checkbox" data-selection-filter="'+group+':'+i+'" '+(checked?"checked":"")+'>'+label+'</label>';
      const m=(group,label,hasAll=true)=>{
        const rows=(state?.[group]||[]).map((value,i)=>value===null?"":row(group,i,filterNames[group][i],value)).join("");
        if(!rows)return "";
        const all=(state[group]||[]).filter(v=>v!==null).every(Boolean)&&(group!=="indices"||state.single);
        return '<fieldset><legend>'+label+'</legend>'+(hasAll?row(group,-1,"全部",all):"")+rows+
          (group==="indices"?row("single",-1,"单音",state.single):"")+'</fieldset>';
      };
      panel.innerHTML=state?m("elements","谱面记号")+m("voices","声部",false)+m("indices","和弦音"):"<p>进入乐谱编辑后可使用</p>";
      panel.dataset.signature=sig;
      if(active)panel.querySelector('[data-selection-filter="'+active+'"]')?.focus({preventScroll:true});
    }
    panel.querySelectorAll("[data-selection-filter]").forEach(i=>i.disabled=waiting||!!response?.input||!!response?.textEditing);
  }
  // MuseScore repaints the score as soon as the command commits and lets the
  // Qt property bindings of its toolbars settle on the next frame. Do the same:
  // publish the score stamps now, then walk the toolbar, palette, inspector and
  // transport once per animation frame, however many commands finished in it.
  let syncFrame=0, syncTimer=0, transportDirty=false;
  function flushSync(){
    if(syncFrame)cancelAnimationFrame(syncFrame);
    clearTimeout(syncTimer);syncFrame=syncTimer=0;
    syncNow();
    if(transportDirty){transportDirty=false;window.dispatchEvent(new Event("astra-native-score-transport-change"));}
  }
  function stampScore(){
    if(!root)return;
    const ready=Number.isSafeInteger(response?.revision);
    const set=(k,v)=>{if(root.dataset[k]!==v)root.dataset[k]=v;};
    set("currentPage",ready?String(response.page):"0");
    set("pageCount",ready?String(response.pages):"0");
    set("scoreRevision",ready?String(response.revision):"0");
    set("savedRevision",ready?String(response.saved):"0");
    set("selectedPitch",ready?String(response.selectedPitch):"-1");
    // The mode pair is usable the moment the score is (not one frame later).
    for(const el of root.querySelectorAll('.astra-native-mode-actions [data-native-score]'))if(el.disabled!==!ready)el.disabled=!ready;
  }
  function sync() {
    stampScore();
    if(syncFrame)return;
    syncFrame=requestAnimationFrame(flushSync);
    // rAF pauses for hidden views; never leave the controls stale.
    syncTimer=setTimeout(flushSync,120);
  }
  const attr=(el,name,value)=>{if(el.getAttribute(name)!==value)el.setAttribute(name,value);};
  function syncNow() {
    if (!root) return;
    const ready = Number.isSafeInteger(response?.revision);
    const readOnly = !!response?.readOnly;
    const editing = ready && !readOnly && !!response.editing;
    const dirty = ready && !readOnly && response.dirty === true;
    const waiting = visiblyBusy();
    stampScore();
    root.classList.toggle("is-editing", editing);
    root.classList.toggle("is-input", editing && !!response.input);
    root.querySelector(".astra-native-notation-tools").inert = !editing || !!response?.textEditing;
    const t=root.querySelector('.astra-native-rhythm-menu.is-tuplet');
    if(t){
      const enabled=editing && eligible("tuplet","");
      t.querySelector("summary").setAttribute("aria-disabled",String(!enabled));
      if(!enabled)t.open=false;
    }
    buttons().forEach(el => {
      const k = el.dataset.nativeScore;
      if (k === "read" || k === "edit") attr(el,"aria-pressed", String(k === (editing ? "edit" : "read")));
      if (k === "tool") {
        const {scoreTool, scoreValue} = el.dataset;
        const state = response?.[scoreTool];
        // Palette cells apply an item once; they never mirror native input state as "pressed".
        if (!el.closest(".astra-native-palette") && (typeof state === "boolean" || ["duration","voice","accidental","input-method"].includes(scoreTool))) {
          const selected = scoreTool==="dot" ? Number(response.dotCount??Number(state))===(scoreValue==="toggle"?1:Number(scoreValue)) :
            scoreValue === "toggle" ? state === true : String(state) === scoreValue;
          attr(el,"aria-pressed", String(selected));
        }
        // Keep focus on the tool while an asynchronous native edit is pending.
        const off = !editing || !eligible(scoreTool, scoreValue);
        if (el.disabled !== off) el.disabled = off;
        attr(el,"aria-disabled", String(off || waiting));
        return;
      }
      if (["smaller","larger","fit"].includes(k)) el.disabled = !ready;
      else if (readOnly && ["edit","save","reopen","print","exportMusicXml"].includes(k)) el.disabled = true;
      else if(k==="textAdd"){
        const role=el.dataset.scoreValue;
        const head=["subtitle","poet","part"].includes(role);
        const allowed=head || (role==="frame" ? response?.selectionKind==="Other" : response?.selectedRhythm || response?.selection?.mode==="range");
        el.disabled=waiting||!editing||!!response.input||!!response.textEditing||!allowed;
      }
      else if(k==="textEdit"){el.hidden=!editing||!response?.textEditable||!!response.textEditing;el.disabled=waiting||el.hidden;}
      else if(["textTitle","textComposer","textStaff","textSystem"].includes(k)){
        el.hidden=!editing;
        el.disabled=waiting||!editing||!!response.input||!!response.textEditing||
          ((k==="textStaff"||k==="textSystem")&&!response.selectedRhythm&&response.selection?.mode!=="range");
      }
      else if(response?.textEditing && !["smaller","larger","fit"].includes(k)) el.disabled=true;
      else if (k === "open") { el.disabled = waiting || ready; el.hidden = ready; }
      else if (k === "prev" || k === "next") el.disabled = !ready || waiting || (k === "prev" ? response.page <= 1 : response.page >= response.pages);
      else if (k === "exportMusicXml") el.disabled = waiting || !ready || !!response.input || !!response.textEditing;
      else if (k === "save") el.disabled = waiting || !ready || (!!root.closest(".temp-right")&&!dirty);
      else if (k === "reopen") el.disabled = waiting || !ready || dirty || !response.saved;
      else if (k === "undo" || k === "redo") el.disabled = waiting || !editing || !response[k];
      else if (k.startsWith("pitch") || k.startsWith("octave")) el.disabled = waiting || !editing || !!response.input || !(response.canPitch ?? response.selectedPitch >= 0);
      else if (k==="systemLocksDialog") el.disabled=waiting||!editing||!!response?.input;
      else if (k==="measureInsertDialog") el.disabled=waiting||!editing||!!response?.input||(["measure-add-before","measure-add-after"].includes(el.dataset.insertPosition)&&(!response?.selection?.mode||response.selection.mode==="none"));
      else if(k==="measurePropertiesDialog") el.disabled=waiting||!editing||!!response?.input||!response?.measureContext?.staves;
      else if(k==="transposeDialog") el.disabled=waiting||!editing||!!response?.input||!response?.transpose;
      else if (k==="content") {
        const a=el.dataset.scoreContent,mode=response?.selection?.mode,selected=!!mode&&mode!=="none";
        el.disabled=waiting||!editing||(!!response?.input&&a!=="paste-special")||
          ((a==="cut"||a==="copy"||a==="delete"||a==="measure-add-before"||a==="measure-split")&&!selected)||
          ((a.startsWith("paste")||a==="swap-selection")&&(!selected||!response.clipboardReady))||(a==="repeat-selection"&&!["Note","Rest","Range"].includes(response.selectionKind))||(a==="measure-join"&&mode!=="range")||(a==="time-delete"&&!response.timeDeleteKind);
      }
      // The mode pair never flickers grey while a note command is in flight.
      else if (k === "read" || k === "edit") { if (el.disabled !== !ready) el.disabled = !ready; }
      else el.disabled = waiting || !ready;
    });
    // MuseScore applies a palette cell to the note being entered: only text editing blocks it.
    if(layoutPanelMode!==String(editing)+(response?.input?"i":"")){layoutPanelMode=String(editing)+(response?.input?"i":"");layoutPanel.render();}
    layoutPanel.want();
    catalogue.update(root,{project,ready:ready&&!readOnly,editing,input:!!response?.textEditing,waiting,
                           page:response?.page,height:response?.frameHeight,revision:response?.revision,
                           documentGeneration:nativeDocumentGeneration});
    inspector.update(root,{project,ready:ready&&!readOnly,editing,waiting,input:!!response?.input,
      textEditing:!!response?.textEditing,selected:!!(response?.selection?.mode
        ? response.selection.mode!=="none" : response?.selectionKind),
      revision:response?.revision,documentGeneration:nativeDocumentGeneration,selectionStamp:inspectorSelectionStamp});
    syncSelectionFilters(waiting);
    const textForm=root.querySelector(".astra-native-text-edit");
    if(textForm) {
      const shouldShow=editing&&!!response?.textEditing;
      const wasHidden=textForm.hidden;
      textForm.hidden=!shouldShow;
      const nativeText = response?.textInfo || {};
      textForm.querySelector(".astra-rich-role").textContent =
        ({title:"标题",composer:"作曲",staff:"谱表文字",system:"系统文字"})[nativeText.role] || "谱面文字";
      for(const btn of textForm.querySelectorAll("button")){
        btn.disabled=!!waiting||!shouldShow;
        const action=btn.dataset.richCommand;
        if(["bold","italic","underline","strike"].includes(action))btn.setAttribute("aria-pressed",String(!!nativeText[action]));
      }
      if(shouldShow){
        alignInlineInput();
        if(wasHidden)textForm.querySelector("textarea")?.focus({preventScroll:true});
      }
      if(!shouldShow&&!wasHidden) {
        textForm.querySelector("textarea").value="";
        if(textForm.contains(document.activeElement)||document.activeElement===document.body)area()?.focus({preventScroll:true});
      }
    }
    root.querySelector(".astra-native-doc-name").textContent = documentTitle();
    root.querySelector(".astra-native-doc-tab").title = project || "";
    root.querySelector(".astra-native-dirty").hidden = !dirty;
    root.querySelector(".astra-native-pages").textContent = ready ? (pages.view||response.page)+" / "+response.pages : "";
    root.querySelector(".astra-native-zoom-label").textContent = Math.round(zoom*100)+"%";
    if (sheet()) applyPageWidth();
    const status = response?.error || (!waiting&&response?.nativeScorePreserved?"\u65b0\u751f\u6210\u7684\u8c31\u9762\u5df2\u53ef\u7528\uff0c\u5f53\u524d\u539f\u751f\u7f16\u8f91\u8c31\u5df2\u4fdd\u7559":null) || (waiting ? "正在处理钢琴谱面…" : !ready
      ? "从当前选定的钢琴 MIDI 生成一份独立谱面。原 MIDI 不会改变。" :
      [editing ? (response.selectedPitch >= 0 ? "音符  音高："+pitchName(response.selectedPitch) : response.input ? "音符输入" : "编辑") : "只读浏览",
       tidyNote?.revision===response.revision ? tidyNote.text : "", dirty ? "有未保存的更改" : ""].filter(Boolean).join(" · ")+
      (response.printed ? " · 已导出 PDF"+(response.printedFile ? "："+response.printedFile : "") : "")+
      (response.musicXmlFile ? " · 已导出可编辑 MusicXML：native-notation-prototype/"+response.musicXmlFile : ""));
    root.querySelector(".astra-native-score-status").textContent = readOnly && ready && !response.error ? "\u539f\u5de5\u7a0b\u53ea\u8bfb\u9884\u89c8" : status;
  }

  function pitchName(p){
    return ["C","C♯","D","E♭","E","F","F♯","G","A♭","A","B♭","B"][p%12]+(Math.floor(p/12)-1);
  }
  let rasterSeq = 0;
  async function decodeFrame(frame) {
    const image=await imageFromFrame(frame);
    // Decode before drawImage so the canvas copy never waits on a lazy decode.
    try{await image.decode?.();}catch{}
    return {image,naturalWidth:image.naturalWidth,naturalHeight:image.naturalHeight,close(){}};
  }
  function imageFromFrame(frame) {
    return new Promise((resolve,reject)=>{
      const decoded=new Image();
      decoded.onload=()=>resolve(decoded);
      decoded.onerror=reject;
      decoded.src="data:image/png;base64,"+frame;
    });
  }
  let shadowSerial=0, shadowPending=null, shadowTarget=null, shadowLast=0, shadowTimer=null, shadowImage=null;
  const shadowAllowed=()=>Boolean(backend?.shadow&&response?.editing&&response.input&&!response.readOnly&&!response.textEditing&&
    root?.isConnected&&sheet()&&!sheet().hidden);
  function clearShadow(forget=true){
    if(forget)shadowTarget=null;
    if(shadowImage)shadowImage.hidden=true;
  }
  function pumpShadow(){
    if(!shadowTarget||shadowPending||busy||!shadowAllowed())return;
    const delay=33-(performance.now()-shadowLast);
    if(delay>0){
      if(shadowTimer===null)shadowTimer=setTimeout(()=>{shadowTimer=null;pumpShadow();},delay);
      return;
    }
    const p=shadowTarget, stamp={...p, project, node:root, home:root.parentElement, generation:nativeDocumentGeneration, page:response.page, revision:response.revision};
    shadowLast=performance.now();
    shadowPending={...stamp, receipt:++shadowSerial};
    backend.shadow(JSON.stringify({project,receipt:shadowSerial,page:stamp.page,baseRevision:stamp.revision,x:p.x,y:p.y}));
  }
  function shadowResponse(raw){
    let row;try{row=JSON.parse(raw);}catch{return;}
    const pending=shadowPending;
    if(!pending||row.receipt!==pending.receipt)return;
    shadowPending=null;
    const current=()=>!busy&&shadowAllowed()&&pending.node===root&&pending.home===root.parentElement&&pending.project===project&&pending.generation===nativeDocumentGeneration&&
      pending.page===response.page&&pending.revision===response.revision&&pending.id===shadowTarget?.id;
    if(current()){
      const rect=row.shadowRect, data=row.shadowPng, canvas=sheet(), scale=row.shadowScale??1;
      if(row.project!==project||row.page!==response.page||row.revision!==response.revision||!validRasterScale(scale)||!Array.isArray(rect)||rect.length!==4||
         !rect.every(Number.isInteger)||rect[0]<0||rect[1]<0||rect[2]<=0||rect[3]<=0||rect[2]>192||rect[3]>192||
         rect[0]+rect[2]>960||rect[1]+rect[3]>(response?.frameHeight||1)||typeof data!=="string"||data.length>45000||
         !/^[A-Za-z0-9+/]+={0,2}$/.test(data)){
        clearShadow(false);
      }else{
        if(!shadowImage||shadowImage.parentElement!==area()){shadowImage=document.createElement("img");shadowImage.className="astra-native-shadow-note";
          shadowImage.setAttribute("aria-hidden","true");
          Object.assign(shadowImage.style,{position:"absolute",pointerEvents:"none",zIndex:"2"});
          area().style.position="relative";area().append(shadowImage);
        }
        imageFromFrame(data).then(decoded=>{
          if(!current()||decoded.naturalWidth!==Math.round(rect[2]*scale)||decoded.naturalHeight!==Math.round(rect[3]*scale))return;
          const bounds=canvas.getBoundingClientRect(), sx=bounds.width/960, sy=bounds.height/(response?.frameHeight||1);
          const left=canvas.offsetLeft+rect[0]*sx, top=canvas.offsetTop+rect[1]*sy, width=rect[2]*sx, height=rect[3]*sy;
          shadowImage.src=decoded.src;
          Object.assign(shadowImage.style,{left:left+"px",top:top+"px",width:width+"px",height:height+"px"});
          shadowImage.hidden=false;
        }).catch(()=>{if(current())clearShadow(false);});
      }
    }
    if(shadowTarget&&shadowTarget.id!==pending.id)pumpShadow();
  }
  let scoreFindPending=null;
  function pumpScoreFind() {
    if(!scoreFindPending||busy||!root?.isConnected||!response?.revision)return;
    const p=scoreFindPending;
    if(p.project!==project||p.document!==nativeDocumentGeneration||p.host!==root.parentElement){scoreFindPending=null;return;}
    if(send("find-score",{query:p.query}))scoreFindPending=null;
  }
  function revealScoreCoordinate(row) {
    const paper=area(),canvas=sheet();
    if(!paper||!canvas||!row.scoreRevealEdge)return;
    if(samePaperScope(pendingPaperPosition,paper))pendingPaperPosition=null;
    const end=row.scoreRevealEdge==="end";
    if(!Number.isFinite(row.scoreRevealY)||!(response?.frameHeight||1)){
      homeScroll(paper,end);
      return;
    }
    const box=canvas.getBoundingClientRect(), viewport=paper.getBoundingClientRect();
    paper.scrollTop+=box.top-viewport.top+row.scoreRevealY*box.height/(response?.frameHeight||1)-(end?paper.clientHeight:24);
    if(!end&&overscroll?.paper===paper)paper.scrollLeft=overscroll.x+Math.min(0,(box.width-paper.clientWidth)/2);
  }
  const notifyTransport=()=>{transportDirty=true;sync();};
  async function onResponse(raw) {
    let row;
    try { row=JSON.parse(raw); } catch { return; }
    if (row.project!==project || row.request!==busy) return;
    if(pending==="tick-hit"){
      busy=null;pending=null;
      const done=tickReceipt;tickReceipt=null;
      done?.(!row.error&&row.revision===response?.revision&&Number.isSafeInteger(row.hitTick)?row.hitTick:-1);
      pumpScoreFind();flushScoreKeySave();flushPointerFollowup();replayDeferredRaster();return;
    }
    if(pending==="pan-hit"){
      busy=null;pending=null;
      const done=panReceipt;panReceipt=null;
      done?.(!row.error&&row.revision===response?.revision&&row.panAllowed===true);
      pumpScoreFind();flushScoreKeySave();flushPointerFollowup();replayDeferredRaster();return;
    }
    if(row.skipped){
      if(row.nativeScorePreserved&&response?.revision)response={...response,nativeScorePreserved:true};
      if(pending==="pointer-gesture"){discardPointer(nativePointer);clearPointerFollowup();}
      clearEntryKeys();busy=null;pending=null;sync();notifyTransport();flushScoreKeySave();replayDeferredRaster();
      if(row.retryAutoOpen){
        const wanted=autoStamp;autoStamp=undefined;
        try{const [p,c]=JSON.parse(wanted);autoOpenTimer=setTimeout(()=>{if(p===project&&root?.isConnected)autoOpen(p,c);},100);}catch{}
      }
      return;
    }
    const wasPending=pending, previousPage=response?.page, oldRevision=response?.revision||0,
      wasNoteInput=Boolean(response?.input), pointerPhase=wasPending==="pointer-gesture"?nativePointer?.flight:null;
    const localMotion=["begin","move","page"].includes(pointerPhase)&&!row.error;
    if (!row.documentSwitch&&Number.isSafeInteger(row.revision)&&row.revision<oldRevision){clearEntryKeys();pendingScoreKeySave=null;busy=null;pending=null;sync();replayDeferredRaster();return;}
    const {frame,frameBands,width,height,rasterScale=1,logicalHeight=height,patchBase,patchRect,patchParts,frameSeq,...stamp}=row;
    if(auxRead(wasPending)) {
      busy=null;pending=null;
      // A person's queued press goes before the next background palette or
      // property read (receive() would otherwise start that read at once).
      flushPointerFollowup();
      if(paletteRead(wasPending)) catalogue.receive(wasPending,row);
      else if(wasPending==="page-raster") receivePageRaster(row);
      else if(wasPending==="vector-page") receiveVectorPage(row);
      else if(wasPending==="layout-tree") layoutPanel.receive(row);
      else inspector.receive(wasPending,row);
      sync();notifyTransport();pumpScoreFind();flushScoreKeySave();flushPointerFollowup();replayDeferredRaster();
      return;
    }
    const target=sheet();
    try {
      if (frame) {
        if (typeof frame!=="string" || !validRasterScale(rasterScale) || width!==Math.round(960*rasterScale) || !Number.isInteger(logicalHeight) || logicalHeight<480 || logicalHeight>1800 || !Number.isInteger(height) || Math.abs(height-logicalHeight*rasterScale)>1.01 ||
            !/^[A-Za-z0-9+/]+={0,2}$/.test(frame) || !target) throw Error("bad frame");
        // Large frames arrive as horizontal bands the worker encoded in parallel.
        const banded=frameBands!==undefined;
        if(banded&&(!Array.isArray(frameBands)||frameBands.length<2||frameBands.length>8||
           !frameBands.every((b,i)=>Array.isArray(b)&&b.length===(i?3:2)&&Number.isInteger(b[0])&&Number.isInteger(b[1])&&b[1]>0&&
             b[0]===(i?frameBands[i-1][0]+frameBands[i-1][1]:0)&&(!i||typeof b[2]==="string"&&/^[A-Za-z0-9+/]+={0,2}$/.test(b[2])))))
          throw Error("bad frame bands");
        const frameChars=frame.length+(banded?frameBands.slice(1).reduce((n,b)=>n+b[2].length,0):0);
        if(frameChars>=(rasterScale===1?1000000:2000000))throw Error("bad frame");
        const tiled=patchParts!==undefined;
        const partial=patchRect!==undefined||tiled;
        const rasterSource=()=>liveRasterBase?.target===target&&liveRasterBase.sequence===rasterSeq?liveRasterBase.bitmap:target;
        if(partial && (patchBase!==rasterSeq||frameSeq!==rasterSeq+1||rasterSource().width!==width||rasterSource().height!==height))
          throw Error("bad frame base");
        const within=(p,len)=>Array.isArray(p)&&p.length===len&&p.every(Number.isInteger)&&p[0]>=0&&p[1]>=0&&
          p[2]>0&&p[3]>0&&p[0]+p[2]<=width&&p[1]+p[3]<=height;
        if(tiled && (patchRect!==undefined||!Array.isArray(patchParts)||!patchParts.length||patchParts.length>29||
          !patchParts.every(p=>within(p,5)&&p[4]>=0&&p[4]+p[3]<=height))) throw Error("bad damage strips");
        if(partial&&!tiled&&!within(patchRect,4))throw Error("bad frame rect");
        const decodedBands=await Promise.all([frame,...(banded?frameBands.slice(1).map(b=>b[2]):[])].map(decodeFrame));
        const decoded=decodedBands[0];
        if(row.project!==project||row.request!==busy||target!==sheet()){decodedBands.forEach(d=>d.close());return;}
        const pieces=banded?frameBands.map((b,i)=>({img:decodedBands[i].image,y:b[0],h:b[1],w:decodedBands[i].naturalWidth,nh:decodedBands[i].naturalHeight}))
          :[{img:decoded.image,y:0,h:decoded.naturalHeight,w:decoded.naturalWidth,nh:decoded.naturalHeight}];
        if(pieces.some(p=>p.w!==pieces[0].w||p.nh!==p.h))throw Error("bad frame band dimensions");
        const sourceWidth=pieces[0].w, sourceHeight=pieces.reduce((n,p)=>n+p.h,0);
        const [x,y,w,h]=partial&&!tiled?patchRect:[0,0,width,height];
        if(tiled) {
          if(!patchParts.every(p=>p[2]<=sourceWidth&&p[4]+p[3]<=sourceHeight))
            throw Error("bad damage strip dimensions");
        } else if(sourceWidth!==w||sourceHeight!==h)throw Error("bad frame dimensions");
        if(previousPage&&row.page!==previousPage&&!target.hidden)keepLivePage(previousPage,target);
        // Resolve again after async decode: zoom may have repainted the
        // visible canvas while the PNG was decoding.
        const rasterBase=partial?rasterSource():null;
        if(target.width!==width||target.height!==height){target.width=width;target.height=height;}
        const canvas=target.getContext("2d",{alpha:false});
        if(rasterBase&&rasterBase!==target)canvas.drawImage(rasterBase,0,0);
        liveRasterBase=null;
        // Copy source rows [sy, sy+h) to (dx, dy), across band boundaries.
        const blit=(sy,h,w,dx,dy)=>{
          for(const p of pieces){
            const a=Math.max(sy,p.y),b=Math.min(sy+h,p.y+p.h);
            if(b>a)canvas.drawImage(p.img,0,a-p.y,w,b-a,dx,dy+(a-sy),w,b-a);
          }
        };
        if(tiled) {
          for(const [x,y,w,h,sy] of patchParts)blit(sy,h,w,x,y);
        } else blit(0,h,w,x,y);
        decodedBands.forEach(d=>d.close());
        if(!partial){rasterZoom.clear();if(zoomStage?.scale===rasterScale)swapStagedPages(zoom);}
        target.hidden=false;
        rasterSeq=frameSeq||0;
        target.dataset.paintSerial=String(Number(target.dataset.paintSerial||0)+1);
        syncOverscroll();
      }
    } catch(err) {
      queuedPitchTaps.length=0;clearEntryKeys();queuedNavigation=[];
      if(row.project!==project||row.request!==busy)return;
      if(wasPending==="pointer-gesture"&&nativePointer){
        // Preserve the committed canvas and end any transient edit after a
        // full native replay. Never reconstruct a gesture from stale pixels.
        const g=nativePointer;
        if(g.flight==="begin"&&!row.error)g.ready=row.pointerKind;
        if(g.flight==="end"||g.flight==="cancel"||g.ready==="pan"||g.ready==="blocked")discardPointer(g);
        else if(g.ready){g.finished=true;g.moves=[];g.next={phase:"cancel",point:g.point};}
        clearPointerFollowup();
      }
      // Preserve last good display. Native may already have advanced; request
      // a non-mutating full replay at that revision, never reapply the edit.
      busy=null;pending=null;
      if(Number.isSafeInteger(row.revision)&&wasPending!=="refreshFrame") {
        // Refresh native revision without publishing it to the visible
        // score metadata before its matching pixels have decoded.
        busy=++serial;pending="refreshFrame";
        backend.dispatch(JSON.stringify({kind:"refreshFrame",project,request:busy,baseRevision:row.revision}));
        sync();
      } else {
        pendingScoreKeySave=null;
        response={...response,error:"谱面画面未同步，请重开谱面。"};
        sync();
      }
      return;
    }
    busy=null;pending=null;
    if(row.documentSwitch){
      response=null;
      clearEntryKeys();pendingScoreKeySave=null;clearShadow();shadowPending=null;scoreFindPending=null;
      contextPoint=null;textQueue=[];textAfterInput=null;
    }
    response={...response,contextMenu:undefined,error:undefined,printed:wasPending==="print"&&!row.error,musicXmlFile:undefined,musicXmlRevision:undefined,...stamp};
    const opened=frame&&!row.error&&["open","autoOpen","reopen"].includes(wasPending);
    if(frame) {
      response.frameHeight=logicalHeight; response.rasterScale=rasterScale; rasterAttempt=null;
      if(opened){response.nativeScorePreserved=false;++nativeDocumentGeneration;setTimeout(()=>applyViewConfig(),0);}
    }
    if(frame&&!row.error) {
      // Position only the accepted first frame after the new page column exists.
      // A hidden or detached paper keeps this goal until it is measurable.
      const firstView=opened||!oldRevision||pages.doc!==pagesDoc();
      layoutPages();
      if(firstView)setPaperPosition(area());
      else if(previousPage&&previousPage!==row.page&&wasPending==="page"&&!row.scoreRevealEdge)homeScroll();
    }
    if(frame) {
      if(!row.error&&wasPending==="view-config"){for(const e of pages.els.values())e.revision=null;schedulePages(60);}
      if(!row.error&&wasPending==="tidy-score"){
        const t=row.tidyTitled|0, r=row.tidyRounded|0, h=row.tidyHidden|0;
        tidyNote={revision:row.revision,text:t+r+h?"已整理："+[t?"添加标题":"",r?"速度记号取整 "+r+" 处":"",h?"隐藏重复速度记号 "+h+" 处":""].filter(Boolean).join("，"):"谱面已整齐，无需整理"};
      }
    }
    if(frame&&!row.error&&Number.isSafeInteger(row.page)&&Number.isSafeInteger(row.revision)){
      const next={root,paper:area(),surface:target,project,generation:nativeDocumentGeneration,
        revision:row.revision,page:row.page,logicalHeight};
      const old=acceptedPlaybackFrame;
      // Preserve exact frame ownership across legal backing-density swaps.
      if(!old||Object.keys(next).some(k=>next[k]!==old[k]))acceptedPlaybackFrame=next;
    }else if((frame&&row.error)||acceptedPlaybackFrame&&(acceptedPlaybackFrame.revision!==response.revision||
       acceptedPlaybackFrame.page!==response.page)){
      acceptedPlaybackFrame=null;playbackCursor?.invalidate();
    }
    if(localMotion){
      // The raster and minimal pointer state are already current. Do not
      // walk buttons, the palette, inspector or transport per mouse sample.
      if(Number.isSafeInteger(row.selectedPitch))root.dataset.selectedPitch=String(row.selectedPitch);
      auditionAfter(wasPending,row,oldRevision);
      if(pointerPhase==="page"){
        root.dataset.currentPage=String(row.page);
        root.querySelector(".astra-native-pages").textContent=row.page+" / "+response.pages;
      }
      pointerResponse(row,wasPending);
      return;
    }
    if(wasPending==="followPage"&&followTarget&&row.revision===followTarget.revision&&transportScope?.playing)
      scrollFollow(followTarget);
    if(wasPending==="followPage"&&pageClick)replayPageClick(row);
    if(wasPending!=="exportMusicXml"&&(!nativePointer||["end","cancel"].includes(nativePointer.flight)))++inspectorSelectionStamp;
    const measureDialog=root?.querySelector(".astra-native-measure-properties-dialog");
    if(wasPending==="content"&&measureDialog?.dataset.stepPending==="true"){
      delete measureDialog.dataset.stepPending;
      const form=measureDialog.querySelector("form");form.inert=false;
      if(measureDialog.open&&row.measurePropertiesStep&&!row.error)openMeasureProperties(true);
      if(measureDialog.open&&row.error){const e=form.querySelector("[data-measure-error]");e.textContent=row.error;e.hidden=false;}
    }
    pointerResponse(row,wasPending);
    flushPointerFollowup();
    requestSharpRaster();
    if(!shadowAllowed()||previousPage&&previousPage!==response?.page)clearShadow();
    else pumpShadow();
    sync();
    if(wasPending==="pitch")drainPitchTaps(!row.error);
    if(wasPending==="tool")drainEntryKeys(row);
    if(wasPending==="text-action"&&row.error)textQueue=[];
    if(navKind(wasPending)&&row.error)queuedNavigation=[];
    pumpInlineText();
    pumpNavigation();
    schedulePages();
    layoutPanel.want();
    flushPointerFollowup();
    notifyTransport();
    // Only an intended score selection can alter playback. Never seek on a
    // raster, property, replay, cursor hover or a note-entry score mutation.
    if((["select","range-drag"].includes(wasPending)||(pointerPhase==="end"&&row.pointerKind==="lasso"&&row.pointerAccepted===true))&&!wasNoteInput&&!row.error&&oldRevision===row.revision&&
       root?.isConnected&&!root.closest(".temp-right")&&transportScope?.path===project&&
       Number.isSafeInteger(row.playbackSelection?.tick)&&row.playbackSelection.tick>=0&&
       (["element","range"].includes(row.playbackSelection.kind)||row.playbackSelection.kind==="other"&&!transportScope.playing)){
      // Show the new start at once; the engine's packets confirm it.
      // As MuseScore: in edit mode the playhead is shown only while playing.
      if(transportScope.playing)playbackCursor?.pin(row.playbackSelection.tick);
      backend?.controlScorePlayback?.(JSON.stringify({project,revision:row.revision,action:"seekSelection",value:{}}));
    }
    auditionAfter(wasPending,row,oldRevision);
    if(wasPending==="focus-measure"&&!row.error&&Number.isFinite(row.temporaryFocusY)&&root?.closest(".temp-right:not([hidden])")){
      const paper=area(), canvas=sheet();
      if(paper&&canvas&&(response?.frameHeight||1)){
        if(samePaperScope(pendingPaperPosition,paper))pendingPaperPosition=null;
        paper.scrollTop+=canvas.getBoundingClientRect().top-paper.getBoundingClientRect().top+
          row.temporaryFocusY*canvas.getBoundingClientRect().height/(response?.frameHeight||1)-32;
      }
    }
    if(temporaryFocus&&response?.revision&&!busy&&!nativePointer&&root?.closest(".temp-right:not([hidden])")) {
      const wanted=temporaryFocus;temporaryFocus=null;
      if(wanted.project===project&&!response.textEditing)send("focus-measure",{beat:wanted.beat});
    }
    if(["find-score","score-edge","page"].includes(wasPending)&&!row.error)revealScoreCoordinate(row);
    if(wasPending==="find-score"){
      const status=root?.querySelector(".astra-score-find-status");
      if(status&&!scoreFindPending)status.textContent=row.error||((row.scoreFindResult==="none"&&root.querySelector(".astra-score-find-field").value)?"未找到":"");
    }
    pumpScoreFind();
    if(wasPending==="select-more-query"&&!row.error)openSelectMore(row.selectMore);
    if(wasPending==="property-set") inspector.receive(wasPending,row);
    if(wasPending==="text-action"&&textAfterInput) {
      const next=textAfterInput;textAfterInput=null;
      if(!row.error) {
        const field=root?.querySelector(".astra-native-text-input");
        if(field)field.value="";
        if(next==="finish"){send("text-action",{operation:"finish"});return;}
        field?.focus({preventScroll:true});
      }
    }
    if(wasPending==="context-point"&&contextPoint){
      const p=contextPoint; contextPoint=null;
      if(!row.error)showContext(response,p.x,p.y);
    }
    flushScoreKeySave();
  }

  // MuseScore anchors zoom at the wheel cursor. A toolbar invocation uses
  // the viewport center instead. No score transaction or canvas repaint.
  let rasterTimer, rasterAttempt, rasterDeferred=false;
  // MuseScore redraws its view at the new zoom; nothing is shown stretched.
  // Here the page takes its new size in the same task that draws the raster
  // made for that zoom; only a raster slower than 300 ms shows the stretched
  // page meanwhile. shownZoom is the zoom the page is laid out at.
  // Pages beside the live one that are in view now: they are rendered at
  // the new density before the zoom shows (zooming out, pages that come into
  // view are drawn from a denser picture, so none is ever enlarged).
  function stageZoomNeighbours(){
    const scale=nativeRasterScale(), paper=area();
    if(zoomStage?.scale===scale)return;
    dropZoomStage();
    if(!paper||!response?.revision)return;
    const v=paper.getBoundingClientRect(), need=[];
    for(const [n,e] of pages.els){
      if(n===response.page||!e.el.isConnected||e.revision===null||(e.scale===scale&&e.revision===response.revision))continue;
      const r=e.el.getBoundingClientRect();
      if(r.right>v.left&&r.left<v.right&&r.bottom>v.top&&r.top<v.bottom)need.push(n);
    }
    zoomStage={scale,need,asked:new Set(),ready:new Map(),decoding:0,pending:0,committed:false,doc:pagesDoc()};
  }
  function sharpenView(){
    if(!response?.rasterScale)return;
    if(vectorMode){requestSharpRaster(0);schedulePages(0);return;}
    stageZoomNeighbours();
    requestSharpRaster(0);
  }
  function dropZoomStage(){
    if(!zoomStage)return;
    for(const s of zoomStage.ready.values())URL.revokeObjectURL(s.url);
    zoomStage=null;schedulePages(200);
  }
  // The next worker command for a staged zoom: a neighbour page, else null.
  function nextZoomPage(scale){
    const st=zoomStage;
    if(!st||st.committed||st.scale!==scale||st.doc!==pagesDoc())return null;
    return st.need.find(n=>!st.asked.has(n)&&n!==response?.page)??null;
  }
  function swapStagedPages(z){
    const st=zoomStage;
    if(!st||st.scale!==nativeRasterScale(z))return;
    for(const [n,s] of st.ready){
      const e=pages.els.get(n);
      if(!e||n===response?.page||s.revision!==response?.revision){URL.revokeObjectURL(s.url);continue;}
      e.imgToken=(e.imgToken||0)+1;
      s.img.style.width="";
      if(e.el.isConnected)e.el.replaceWith(s.img);
      if(e.url)URL.revokeObjectURL(e.url);
      e.img=s.img;e.el=s.img;e.url=s.url;e.revision=s.revision;e.scale=s.scale;
    }
    st.ready.clear();
    st.committed=true;closeZoomStage(st);
  }
  // A shown stage ends once no staged page is still on its way.
  function closeZoomStage(st){
    if(zoomStage===st&&st.committed&&st.pending<=0&&st.decoding<=0){zoomStage=null;schedulePages(200);}
  }
  function showZoom(z){
    const paper=area(), canvas=sheet();
    if(z===shownZoom&&!zoomAnchor)return;
    swapStagedPages(z);
    shownZoom=z;
    if(z===zoom)clearTimeout(zoomFallback);
    if(!paper||!canvas){zoomAnchor=null;return;}
    applyPageWidth();
    if(syncOverscroll(paper))overscrollSpacers(paper);
    playbackCursor?.reflow();
    const a=zoomAnchor;
    if(a){
      const after=canvas.getBoundingClientRect();
      paper.scrollLeft+=after.left+a.x*after.width-a.clientX;
      paper.scrollTop+=after.top+a.y*after.height-a.clientY;
    }
    if(shownZoom===zoom)zoomAnchor=null;
  }
  const validRasterScale=s=>typeof s==="number"&&Number.isFinite(s)&&s>=.5&&s<=3;
  // The page is drawn at exactly the device pixels it covers: 960 x zoom CSS
  // px snapped to whole device pixels, so the browser never resamples the
  // engraving (MuseScore paints into a backing store at the device pixel
  // ratio). Above 2.5x the raster stays at 2.5x and is scaled up.
  function pageDevicePixels(z=zoom){
    return Math.max(480,Math.round(960*z*(window.devicePixelRatio||1)));
  }
  function nativeRasterScale(z=zoom){
    return Math.min(3,pageDevicePixels(z)/960);
  }
  function pageCssWidth(){
    const dpr=window.devicePixelRatio||1, device=pageDevicePixels(shownZoom);
    return (device<=2880?device/dpr:960*shownZoom)+"px";
  }
  function applyPageWidth(){
    const paper=area(), canvas=sheet(), w=pageCssWidth();
    if(canvas&&canvas.style.width!==w)canvas.style.width=w;
    if(paper)paper.style.setProperty("--astra-page-w",w);
    for(const e of pages.els.values()){
      if(e.el.tagName==="CANVAS"){if(e.el.style.width!==w)e.el.style.width=w;}
      else if(e.el.style.width)e.el.style.width="";
    }
  }
  function requestSharpRaster(delay=shownZoom!==zoom?0:110){
    clearTimeout(rasterTimer);
    rasterTimer=setTimeout(()=>{
      if(!root?.isConnected || !response?.revision || !response?.rasterScale || response.textEditing){
        rasterDeferred=false;showZoom(zoom);return;
      }
      const rasterScale=nativeRasterScale();
      if(rasterScale===response.rasterScale&&!(zoomStage?.scale===rasterScale&&!zoomStage.committed)){rasterDeferred=false;showZoom(zoom);return;}
      // Retain just a pending density check. The current page, zoom and DPR
      // are sampled again at release; do not queue an old raster request.
      if(nativePointer||gesture||busy){rasterDeferred=true;return;}
      rasterDeferred=false;
      const staged=nextZoomPage(rasterScale);
      if(staged!==null){
        zoomStage.asked.add(staged);
        pages.inflight={page:staged,revision:response.revision,scale:rasterScale,doc:pagesDoc(),stage:zoomStage,sent:performance.now()};
        if(send("page-raster",{page:staged,rasterScale}))zoomStage.pending++;else pages.inflight=null;
        return;
      }
      // Wait for staged pages still decoding (they arrive within a frame or two).
      if(zoomStage?.scale===rasterScale&&!zoomStage.committed&&zoomStage.decoding>0)return;
      // The live page is already at this density: the staged pages show now.
      if(rasterScale===response.rasterScale){swapStagedPages(zoom);return;}
      const key=[project,response.page,response.revision,rasterScale].join(":");
      if(rasterAttempt!==key&&send("refreshFrame",{rasterScale})){rasterAttempt=key;rasterZoom.set(rasterScale,zoom);}
    },delay);
  }
  function replayDeferredRaster(){
    if(rasterDeferred&&!gesture&&!nativePointer&&!busy)requestSharpRaster();
  }
  function resetSharpRaster(){
    clearTimeout(rasterTimer);rasterTimer=undefined;
    rasterAttempt=null;rasterDeferred=false;
  }
  window.addEventListener("resize",()=>{
    applyPageWidth();
    requestSharpRaster();
    gesture?.invalidateBox?.();
  });
  let pointerViewportObserver;
  function setCanvasZoom(next, clientX, clientY) {
    const paper=area(), canvas=sheet();
    if(!paper || !canvas || canvas.hidden)return;
    const v=paper.getBoundingClientRect();
    clientX??=v.left+paper.clientWidth/2;clientY??=v.top+paper.clientHeight/2;
    const target=Math.min(2,Math.max(.55,next));
    if(target===zoom)return;
    const before=canvas.getBoundingClientRect();
    if(!before.width||!before.height)return;
    zoomAnchor={x:(clientX-before.left)/before.width,y:(clientY-before.top)/before.height,clientX,clientY};
    zoom=target;
    root.querySelector(".astra-native-zoom-label").textContent=Math.round(zoom*100)+"%";
    // AbstractNotationPaintView::scale: the view matrix changes for every
    // page at once, the point under the pointer stays put, then the view
    // repaints. The repaint here is asynchronous, so it starts once the
    // wheel settles and swaps every visible page in one task.
    dropZoomStage();clearTimeout(rasterTimer);
    pages.quietUntil=Math.max(pages.quietUntil,performance.now()+400);
    showZoom(zoom);
    redrawVectorView();
    clearTimeout(zoomFallback);
    zoomFallback=setTimeout(sharpenView,140);
  }

  let tickReceipt=null;
  function probeTick(x,y,cssScale,done){
    if(!backend||!project||busy||!response?.revision)return false;
    busy=++serial;pending="tick-hit";tickReceipt=done;
    backend.dispatch(JSON.stringify({kind:"tick-hit",project,request:busy,baseRevision:response.revision,x,y,cssScale}));
    return true;
  }
  function probePan(x,y,done,cssScale=0){
    if(!backend||!project||busy||!response?.revision)return false;
    busy=++serial;pending="pan-hit";panReceipt=done;
    backend.dispatch(JSON.stringify({kind:"pan-hit",project,request:busy,baseRevision:response.revision,x,y,cssScale}));
    return true;
  }

  // MuseScore's toolbars never grey out while a command runs. Only a native
  // command still running after 250 ms (open, save, export, a long layout)
  // shows the processing state; note and selection commands change nothing
  // around the score, so nothing flickers.
  let slowBusy=false, slowBusyTimer=0;
  const visiblyBusy=()=>Boolean(busy&&slowBusy);
  function send(kind, fields = {}) {
    // Background reads (palette, properties) never take the worker from a
    // waiting click or key; they are asked for again on the next sync.
    if (auxRead(kind) && (pendingScoreKeySave||deferredPointerActions.length&&!replayingInput)) return false;
    if (!backend || !project || busy || hostSaveInFlight || (nativePointer&&kind!=="pointer-gesture") ||
        (!["open","autoOpen"].includes(kind) && !response?.revision)) return false;
    if(!navKind(kind)&&!auxRead(kind))queuedNavigation=[];
    if(!auxRead(kind))clearShadow(false);
    busy = ++serial; pending = kind;
    clearTimeout(slowBusyTimer);slowBusy=false;
    const watched=busy;
    slowBusyTimer=setTimeout(()=>{if(busy===watched){slowBusy=true;sync();notifyTransport();}},250);
    if(!auxRead(kind))response = response ? {...response, error:undefined} : null;
    backend.dispatch(JSON.stringify({kind, project, request:busy, baseRevision:response?.revision || 0,
      ...(["open","autoOpen"].includes(kind)?{rasterScale:nativeRasterScale()}:{}), ...fields}));
    if(kind!=="pointer-gesture"){sync();notifyTransport();}
    return true;
  }

  function openSelectMore(detail){
    if(!root||!response?.editing||response.textEditing||response.input||busy||
       !Array.isArray(detail?.options)||!detail.options.length||root.querySelector(".astra-native-select-more-dialog"))return;
    const labels={head:"相同符头",pitch:"相同音高（含八度）",name:"相同音名",type:"相同音符类别",
      "duration-type":"相同记谱时值","duration-ticks":"相同实际长度",subtype:"相同子类型",
      staff:"同一谱表",voice:"同一声部",system:"同一谱行",beat:"同一拍位",measure:"同一小节"};
    const dlg=document.createElement("dialog");
    dlg.className="astra-native-select-more-dialog";
    dlg.setAttribute("aria-labelledby","astra-select-more-title");
    Object.assign(dlg.style,{border:"1px solid #75839f",borderRadius:"10px",padding:"16px",
      background:"Canvas",color:"CanvasText",maxHeight:"calc(100% - 24px)",overflow:"auto"});
    dlg.innerHTML='<form><h3 id="astra-select-more-title">按条件选择</h3><p data-match-type></p>'+
      '<fieldset data-match-options><legend>与当前元素一致的条件</legend></fieldset>'+
      '<p><label><input type="checkbox" name="inSelection">限于已经选中的元素</label></p>'+
      '<label>操作 <select name="action"><option value="replace">替换当前选择</option>'+
      '<option value="add">加入当前选择</option><option value="subtract">从选择中移除</option></select></label>'+
      '<p><button type="button" data-match-cancel>取消</button> <button type="submit">确定</button></p></form>';
    const form=dlg.querySelector("form"), options=dlg.querySelector("[data-match-options]");
    form.querySelector("[data-match-type]").textContent=String(detail.type||"");
    for(const [key,value] of detail.options){
      if(!Object.hasOwn(labels,key))continue;
      const line=document.createElement("label"), item=document.createElement("input");
      line.style.display="block";item.type="checkbox";item.name="criterion";item.value=key;
      line.append(item," "+labels[key]+(value?"："+value:""));
      options.append(line);
    }
    form.elements.inSelection.checked=!!detail.inSelection;
    form.elements.inSelection.disabled=!detail.inSelection;
    const context={project,generation:nativeDocumentGeneration,host:root.parentElement};
    const same=()=>context.project===project&&context.generation===nativeDocumentGeneration&&context.host===root?.parentElement;
    dlg.addEventListener("close",()=>{dlg.remove();if(same())area()?.focus({preventScroll:true});});
    form.querySelector("[data-match-cancel]").onclick=()=>dlg.close();
    form.addEventListener("submit",event=>{
      event.preventDefault();event.stopPropagation();
      if(!same()){dlg.close();return;}
      const criteria=[...form.querySelectorAll('[name=criterion]:checked')].map(i=>i.value);
      if(send("select-more-apply",{criteria,action:form.elements.action.value,inSelection:form.elements.inSelection.checked}))dlg.close();
    });
    root.append(dlg);
    dlg.showModal();
  }

  function dispatch(k, action) {
    if(k==="transposeDialog"){
      const dlg=root.querySelector(".astra-native-transpose-dialog"),t=response?.transpose;
      if(dlg.open || busy || !response?.editing || response.input || response.textEditing || response.readOnly || !t)return;
      const form=dlg.querySelector("form"),f=form.elements;
      form.reset();
      f.mode.querySelector('[value="key"]').disabled=!t.range;
      f.mode.value=t.range?"key":"chromatic";f.direction.value=t.range?"closest":"up";
      f.key.value=String(t.key);f.interval.value="4";f.degree.value="1";
      f.transposeKeys.checked=t.range;f.chordNames.checked=t.hasHarmony;f.chordNames.disabled=!t.hasHarmony;
      dlg.querySelector("[data-transpose-scope]").textContent=t.all?"应用到整份乐谱":"应用到当前选择";
      refreshTransposeDialog(form,t);
      dlg.showModal();
      return;
    }

    if(k==="findScore"){root?.dispatchEvent(new Event("astra-score-find-open"));return;}
    if(k==="measurePropertiesDialog"){openMeasureProperties();return;}
    if(k==="measureInsertDialog"){
      const dlg=root.querySelector(".astra-native-insert-measures-dialog");
      if(dlg.open || busy || !response?.editing || response.input || response.textEditing || response.readOnly)return;
      const hasTarget=!!response.selection?.mode && response.selection.mode!=="none";
      const form=dlg.querySelector("form");form.reset();
      for(const item of form.elements.position.options)
        item.disabled=!hasTarget&&["measure-add-before","measure-add-after"].includes(item.value);
      form.elements.position.value=action.dataset.insertPosition;
      if(form.elements.position.selectedOptions[0]?.disabled)return;
      dlg.showModal();
      return;
    }

    if(k==="systemLocksDialog"){
      const dlg=root.querySelector(".astra-native-system-lock-dialog");
      if(dlg.open || busy || !response?.editing || response.input || response.textEditing || response.readOnly)return;
      const form=dlg.querySelector("form");form.reset();form.elements.interval.disabled=false;
      dlg.querySelector("[data-lock-scope]").textContent=response.selection?.mode==="range"?"应用到当前选区":"应用到整份乐谱";
      dlg.returnValue="";
      dlg.showModal();
      return;
    }

    const role=({textTitle:"title",textComposer:"composer",textStaff:"staff",textSystem:"system"})[k];
    if(role||k==="textAdd"){send("text-add",{role:role||action.dataset.scoreValue});return;}
    if(k==="content"){area()?.focus({preventScroll:true});send("content",{action:action.dataset.scoreContent});return;}
    if(k==="textEdit"){send("text-action",{operation:"start"});return;}
    if (k === "tool") {
      area()?.focus({preventScroll:true});
      const tool=action.dataset.scoreTool,value=action.dataset.scoreValue;
      if(["input-method","note-letter","chord-letter"].includes(tool))sendEntryTool(tool,value);
      else send("tool",{tool,value});
      return;
    }
    if (k === "smaller" || k === "larger" || k === "fit") {
      setCanvasZoom(k === "fit" ? 1 : zoom + (k === "larger" ? .1 : -.1));
      pauseFollow();return;
    }
    if (k === "prev" || k === "next") { pauseFollow(); send("page",{delta:k==="next"?1:-1}); return; }
    if(k==="save"&&!root?.closest(".temp-right")){
      document.dispatchEvent(new CustomEvent("astra-project-save",{detail:project}));return;
    }
    if (k === "edit" || k === "read") {
      send("mode", {editing:k === "edit"}); return;
    }
    if (k === "pitchDown" || k === "pitchUp" || k === "octaveUp" || k==="octaveDown") {
      send("pitch",{up:k === "pitchUp" || k === "octaveUp",octave:k.startsWith("octave")}); return;
    }
    send(k);
  }

  let autoOpenTimer=null;
  function autoOpen(current, committed) {
    clearTimeout(autoOpenTimer);autoOpenTimer=null;
    const stamp = JSON.stringify([current, committed ?? null]);
    if (!backend || stamp === autoStamp || (response?.revision&&!committed)) return;
    if(busy||hostSaveInFlight||nativePointer){
      const mounted=root;
      autoOpenTimer=setTimeout(()=>{if(project===current&&root===mounted&&root?.isConnected)autoOpen(current,committed);},100);
      return;
    }
    if(send("autoOpen"))autoStamp=stamp;
  }

  function mount(current, previous, committed) {
    const fresh = document.getElementById("astra-native-score");
    if (!fresh || !current) return;
    if (project === current && previous && previous.node === root) {
      // Keep the real node and its current paint; unrelated Astra remounts don't
      // recreate a notation document, reset scroll, or clear selection.
      if(fresh.parentElement!==root.parentElement){
        clearShadow();scoreFindPending=null;clearPointerFollowup();
        if(gesture){
          const g=gesture;gesture=null;
          if(g===nativePointer&&!g.finished){
            g.finished=true;g.moves=[];g.pageDelta=0;g.next={phase:"cancel",point:g.point};
            pumpPointer();
          }else if(g!==nativePointer)discardPointer(g);
        }
        const bar=root.querySelector(".astra-score-find-bar");if(bar)bar.hidden=true;
      }
      fresh.replaceWith(previous.node);
      const paper = area();
      if(samePaperScope(pendingPaperPosition,paper))placePaperPosition(paper);
      else if(samePaperScope(previous.position,paper))setPaperPosition(paper,previous.position);
      else if(response?.revision)setPaperPosition(paper);
      autoOpen(current, committed);
      replayDeferredRaster();
      return;
    }
    resetSharpRaster();
    pointerViewportObserver?.disconnect();
    if(gesture&&gesture!==nativePointer)discardPointer(gesture);
    gesture=null;
    if (project !== current) {pendingScoreKeySave=null;hostSaveInFlight=false;clearShadow();shadowPending=null;discardPointer(nativePointer);clearPointerFollowup();textQueue=[];project = current; response = null; busy = null; autoStamp = undefined; rasterSeq = 0; liveRasterBase=null; contextPoint=null; textAfterInput=null; zoom = 1; shownZoom = 1; zoomAnchor = null; rasterZoom.clear(); clearTimeout(zoomFallback); dropZoomStage();}
    acceptedPlaybackFrame=null;playbackCursor?.invalidate();
    root = fresh;
    fresh.addEventListener("keydown", event=>handleScoreDocumentKey(fresh,event), true);
    window.AstraScoreInputDock?.attach(root);
    const paper = area();
    const onViewportChange=()=>{if(root===fresh)gesture?.invalidateBox?.();};
    paper.addEventListener("scroll",()=>{onViewportChange();rememberPaperPosition(paper);},{passive:true});
    if(typeof ResizeObserver==="function"){
      pointerViewportObserver=new ResizeObserver(()=>{onViewportChange();placePaperPosition(paper);});
      pointerViewportObserver.observe(paper);
    }
    let shadowMotion=0;
    paper.addEventListener("pointermove",event=>{
      if(event.buttons||event.pointerType!=="mouse"||!shadowAllowed())return; if(event.target!==sheet()){clearShadow();return;}
      const canvas=sheet(), box=canvas.getBoundingClientRect();
      const x=(event.clientX-box.left)*960/box.width, y=(event.clientY-box.top)*(response?.frameHeight||1)/box.height;
      if(!Number.isFinite(x)||!Number.isFinite(y)||x<0||x>=960||y<0||y>=(response?.frameHeight||1)){clearShadow();return;}
      shadowTarget={x,y,id:++shadowMotion};pumpShadow();
    },true);
    for(const e of ["pointerdown","pointerleave","scroll","wheel","pointercancel","blur"])paper.addEventListener(e,()=>clearShadow(),true);
    let suppressPointerClick = false;
    const textForm=root.querySelector(".astra-native-text-edit");
    const field=textForm.querySelector("textarea");
    let composingText=false, compositionCommit=null, textDrag=null;
    Object.assign(field.style,{position:"fixed",width:"1px",height:"1px",minWidth:"1px",minHeight:"1px",
      padding:"0",margin:"0",border:"none",opacity:"0.01",pointerEvents:"none",resize:"none",overflow:"hidden"});
    field.addEventListener("compositionstart",()=>{composingText=true;compositionCommit=null;});
    field.addEventListener("compositionupdate",event=>{
      if(composingText&&event.data.length<=180)inlineText("ime",{preedit:event.data});
    });
    field.addEventListener("compositionend",event=>{
      composingText=false; compositionCommit=event.data;
      if(event.data)inlineCommit(event.data);
      else inlineText("ime",{preedit:""});
      field.value="";
      setTimeout(()=>{compositionCommit=null;},20);
    });
    field.addEventListener("beforeinput",event=>{
      if(event.isComposing||composingText||event.inputType==="insertCompositionText")return;
      if(event.inputType==="insertText"||event.inputType==="insertReplacementText"){
        event.preventDefault();
        if(event.data!==compositionCommit)inlineCommit(event.data);
      }else if(event.inputType==="insertLineBreak"){
        event.preventDefault();inlineText("key",{key:"Enter"});
      }
    });
    field.addEventListener("input",()=>{if(!composingText)field.value="";});
    field.addEventListener("paste",event=>{
      if(composingText)return;
      event.preventDefault();inlineCommit(event.clipboardData?.getData("text/plain"));
    });
    for(const verb of ["copy","cut"])field.addEventListener(verb,event=>{
      const selected=response?.textSelection;
      if(!selected)return;
      event.preventDefault();event.clipboardData?.setData("text/plain",selected);
      if(verb==="cut")inlineText("key",{key:"Delete"});
    });
    textForm.addEventListener("submit",event=>{
      event.preventDefault();event.stopPropagation();
      if(!composingText)inlineText("finish");
    });
    textForm.addEventListener("click",event=>{
      const control=event.target.closest("[data-rich-command]");
      if(!control||control.disabled||composingText)return;
      event.stopPropagation();
      inlineText(control.dataset.richCommand);
      field.focus({preventScroll:true});
    });
    textForm.addEventListener("keydown",event=>{
      event.stopPropagation();
      if(event.isComposing||event.keyCode===229||composingText)return;
      const key=event.key.toLowerCase();
      const format=event.ctrlKey&&!event.altKey&&!event.metaKey ? ({b:"bold",i:"italic",u:"underline"})[key] : null;
      if(format){event.preventDefault();inlineText(format);return;}
      if(event.ctrlKey&&!event.altKey&&key==="a"){
        event.preventDefault();inlineText("select-all");return;
      }
      if(event.key==="Escape"||(event.ctrlKey&&event.key==="Enter")){
        event.preventDefault();field.value="";inlineText("finish");return;
      }
      const role=response?.textInfo?.role;
      const lyric=role==="lyrics", harmony=["chord","roman","nashville"].includes(role), bass=role==="figured-bass";
      let navigation;
      if((lyric||harmony||bass||role==="fingering"||role==="sticking")&&!event.metaKey&&!event.altKey){
        if((harmony||bass)&&event.ctrlKey&&!event.shiftKey&&/^[1-9]$/.test(key))navigation="fraction-"+key;
        if(!event.ctrlKey){
          if(key==="tab")navigation=event.shiftKey?"previous":"next";
          if(key===" ")navigation=event.shiftKey?"previous":"next-word";
          if(lyric&&["-","_"].includes(key))navigation=event.shiftKey||key==="_"?"melisma":"next-syllable";
          if(lyric&&["arrowup","arrowdown"].includes(key))navigation=key==="arrowup"?"previous-verse":"next-verse";
          if(lyric&&key==="enter"&&!event.shiftKey)navigation="new-verse";
          if(harmony&&[";",":"].includes(key))navigation=event.shiftKey||key===":"?"prev-beat":"next-beat";
        }
      }
      // Native text input, caret commands and navigation share one ordered queue.
      // Do not inspect the empty proxy field for native cursor boundaries.
      if(navigation){event.preventDefault();inlineText("navigate",{action:navigation});return;}
      if(["Backspace","Delete","ArrowLeft","ArrowRight","ArrowUp","ArrowDown","Home","End","Enter"].includes(event.key)){
        event.preventDefault();
        inlineText("key",{key:event.key,extend:event.shiftKey,control:event.ctrlKey});
      }
    });
    const textPoint=e=>{
      const r=sheet().getBoundingClientRect();
      const bound=(n,v)=>Math.max(0,Math.min(n,v-.001));
      return {x:bound((e.clientX-r.left)*960/r.width,960),y:bound((e.clientY-r.top)*response.frameHeight/r.height,response.frameHeight)};
    };
    paper.addEventListener("dblclick",event=>{
      if(event.button!==0||event.target!==sheet()||!response?.editing||response.readOnly||response.input)return;
      event.preventDefault();event.stopPropagation();
      if(response.textEditing)inlineText("select-word",textPoint(event));
      else inlineText("start-at",textPoint(event));
    });
    paper.addEventListener("click",event=>{
      if(event.detail<3||event.target!==sheet()||!response?.textEditing)return;
      event.preventDefault();event.stopPropagation();inlineText("select-text",textPoint(event));
    });
    paper.addEventListener("pointerdown",event=>{
      if(event.button!==0||event.target!==sheet()||!response?.textEditing||composingText)return;
      event.preventDefault();
      field.focus({preventScroll:true});
      textDrag={id:event.pointerId,x:event.clientX,y:event.clientY,move:false};
      inlineText("caret-at",textPoint(event));
    });
    paper.addEventListener("pointermove",event=>{
      if(!textDrag||textDrag.id!==event.pointerId||!response?.textEditing)return;
      if(!(event.buttons&1)){textDrag=null;return;}
      if(!textDrag.move&&(Math.abs(event.clientX-textDrag.x)+Math.abs(event.clientY-textDrag.y)>4)){
        textDrag.move=true;paper.setPointerCapture(event.pointerId);
      }
      if(textDrag.move)inlineText("extend-to",textPoint(event));
    });
    paper.addEventListener("pointerup",event=>{
      if(textDrag?.id!==event.pointerId)return;
      if(textDrag.move&&response?.textEditing)inlineText("extend-to",textPoint(event));
      textDrag=null;
    });
    paper.addEventListener("pointercancel",()=>{textDrag=null;});
    paper.addEventListener("lostpointercapture",()=>{textDrag=null;});
    paper.addEventListener("scroll",alignInlineInput,{passive:true});
    paper.addEventListener("contextmenu",event=>{
      if(event.target!==sheet()||!response?.editing||response.textEditing||busy)return;
      event.preventDefault();event.stopPropagation();
      closeContext();
      const box=sheet().getBoundingClientRect();
      contextPoint={x:event.clientX,y:event.clientY};
      send("context-point",{x:(event.clientX-box.left)*960/box.width,y:(event.clientY-box.top)*response.frameHeight/box.height,cssScale:box.width/960});
    });
    function finishGesture(cancelNative=false) {
      const old = gesture;
      gesture = null;
      if(old!==nativePointer)discardPointer(old);
      if(cancelNative&&nativePointer&&old===nativePointer&&!old.finished){
        const g=nativePointer;
        g.finished=true;
        g.moves=[];g.pageDelta=0;
        g.next={phase:"cancel",point:g.point};
        clearPointerFollowup();pumpPointer();
      }
      if(old)replayDeferredRaster();
      return old;
    }
    function viewPoint(x,y,g) {
      const clamp = (value,limit) => Math.max(0,Math.min(limit-.001,value));
      return {x:clamp((x-g.box.left)*960/g.box.width,960),
              y:clamp((y-g.box.top)*g.height/g.box.height,g.height)};
    }
    const mod=(event)=>({shift:event.shiftKey,control:event.ctrlKey,alt:event.altKey});
    const clientInside=(e,g)=>e.clientX>=g.box.left&&e.clientX<g.box.right&&e.clientY>=g.box.top&&e.clientY<g.box.bottom;
    const stable=(g)=>g.project===project&&g.doc===nativeDocumentGeneration&&g.page===response?.page;
    paper.addEventListener("pointerdown", event => {
      if (event.pointerType!=="mouse" || gesture || nativePointer) return;
      if (event.button===1) {
        event.preventDefault();
        gesture={kind:"pan",id:event.pointerId,x:event.clientX,y:event.clientY,
                 left:paper.scrollLeft,top:paper.scrollTop,latestX:event.clientX,latestY:event.clientY};
        const g=gesture;
        g.pan=()=>{paper.scrollLeft=g.left+g.x-g.latestX;paper.scrollTop=g.top+g.y-g.latestY;pauseFollow();};
        paper.setPointerCapture(event.pointerId);
      } else if (event.button===0 && event.target===sheet() && response?.editing && !response.input && !busy &&
                 !event.metaKey && event.ctrlKey && event.shiftKey && !event.altKey) {
        // Paired with native clipboard gesture dispatch. The DOM only forwards
        // coordinates; C++ decides copyable span, source symbol and target.
        event.preventDefault();
        paper.focus({preventScroll:true});
        gesture={kind:"duplicate",id:event.pointerId,x:event.clientX,y:event.clientY,
                 box:sheet().getBoundingClientRect(),height:response.frameHeight,active:false,
                 page:response.page,revision:response.revision};
        paper.setPointerCapture(event.pointerId);
      } else if(event.button===0&&event.target===sheet()&&!event.metaKey&&
                (!event.altKey||(!event.ctrlKey&&!event.shiftKey&&response?.selection?.mode==="range"))&&
                response?.editing&&!response.input&&!response.textEditing&&!response.readOnly&&!busy) {
        const g={kind:"native",id:event.pointerId,x:event.clientX,y:event.clientY,
          startMod:mod(event),left:paper.scrollLeft,top:paper.scrollTop,active:false,ready:null,
          box:sheet().getBoundingClientRect(),height:response.frameHeight,
          page:response.page,revision:response.revision,project,doc:nativeDocumentGeneration,home:root.parentElement,
          moves:[],viewSamples:[],boxDirty:false,raf:0};
        g.startPoint={...viewPoint(g.x,g.y,g),...g.startMod};
        g.startCssScale=g.box.width/960;
        g.pan=()=>{
          paper.scrollLeft=g.left+g.x-g.latestX;
          paper.scrollTop=g.top+g.y-g.latestY;
        };
        g.drawLasso=()=>{
          if(g.ready!=="lasso"||!g.point||!stable(g)||g.next?.phase==="cancel"||g.flight==="cancel")return;
          if(!g.preview){
            const p=document.createElement("div");
            p.className="astra-score-lasso-preview";p.setAttribute("aria-hidden","true");
            Object.assign(p.style,{position:"absolute",pointerEvents:"none",zIndex:"3",boxSizing:"border-box",
              border:"1px solid #4b80c8",background:"rgba(80,128,204,.07)",top:"0",left:"0"});
            paper.style.position="relative";paper.append(p);g.preview=p;
            g.offsetLeft=sheet().offsetLeft;g.offsetTop=sheet().offsetTop;
          }
          const sx=g.box.width/960,sy=g.box.height/g.height, a=g.startPoint,b=g.point;
          const left=g.offsetLeft+Math.min(a.x,b.x)*sx,top=g.offsetTop+Math.min(a.y,b.y)*sy;
          Object.assign(g.preview.style,{transform:"translate("+left+"px,"+top+"px)",
            width:Math.max(1,Math.abs(a.x-b.x)*sx)+"px",height:Math.max(1,Math.abs(a.y-b.y)*sy)+"px"});
        };
        g.syncPageBox=()=>{
          const canvas=sheet(), rect=canvas?.getBoundingClientRect();
          if(!rect||canvas.hidden||!rect.width||!rect.height||!response.frameHeight)return false;
          g.box=rect;g.height=response.frameHeight;g.boxDirty=false;
          if(g.preview){g.offsetLeft=canvas.offsetLeft;g.offsetTop=canvas.offsetTop;}
          return true;
        };
        g.invalidateBox=()=>{
          if(!stable(g)||g.finished||g.ready==="pan")return;
          g.boxDirty=true;
          if(g.active&&g.flight!=="page")requestPointerFrame(g);
        };
        const samePoint=(a,b)=>a&&a.x===b.x&&a.y===b.y&&a.shift===b.shift&&a.control===b.control&&a.alt===b.alt;
        const queueInView=(sample)=>{
          const point={...viewPoint(sample.clientX,sample.clientY,g),shift:sample.shift,control:sample.control,alt:sample.alt};
          if(samePoint(g.point,point))return;
          g.point=point;
          rememberMotion(g,point);
        };
        const latestInView=()=>({clientX:g.latestX,clientY:g.latestY,...g.latestMods});
        g.reproject=(releasing=false)=>{
          if(!g.boxDirty||!stable(g)||(!releasing&&g.flight==="page"))return;
          if(!g.syncPageBox())return false;
          const samples=g.viewSamples;
          g.viewSamples=[];
          // One view read per frame, not per hardware sample. While moving the
          // view alone, project the most recent held cursor into the new box.
          if(samples.length)for(const sample of samples)queueInView(sample);
          else if(!g.finished&&g.active)queueInView(latestInView());
        };
        g.recordMotion=()=>{
          const sample=latestInView();
          if(g.boxDirty)g.viewSamples=retainPointerAxes(g,g.viewSamples,sample);
          else queueInView(sample);
        };
        g.afterPage=delta=>{
          homeScroll(paper,delta<0);
          g.viewSamples=[];
          g.syncPageBox();
          if(!g.finished){
            g.point=null; // identical CSS coordinates still mean a new page
            queueInView(latestInView());
          }
        };
        g.releaseAt=e=>{
          if(g.paged)g.boxDirty=true;
          const hasView=g.reproject(true)!==false;
          g.point={...viewPoint(e.clientX,e.clientY,g),...mod(e)};
          const lasso=g.ready==="lasso"||(!g.ready&&g.startMod.shift&&!g.startMod.control);
          g.next={phase:hasView&&stable(g)&&(lasso||clientInside(e,g))?"end":"cancel",point:g.point};
          if(g.ready==="lasso")requestPointerFrame(g);
        };
        gesture=g;
      } else if(event.button===0&&!event.ctrlKey&&!event.altKey&&!event.shiftKey&&!event.metaKey
                && !response?.input&&!response?.textEditing&&(event.target===sheet()||event.target===paper||
                    event.target.classList?.contains("astra-native-page"))) {
        gesture={kind:"maybe-pan",id:event.pointerId,x:event.clientX,y:event.clientY,
          left:paper.scrollLeft,top:paper.scrollTop,active:false,allowed:null,
          outside:event.target!==sheet(),box:sheet()?.getBoundingClientRect(),height:response?.frameHeight,
          page:response?.page,revision:response?.revision};
      }
    });
    paper.addEventListener("pointermove", event => {
      const g=gesture;
      if (!g || event.pointerId!==g.id) return;
      if(g.kind==="native"){
        if(!(event.buttons&1)||!stable(g)){finishGesture(true);return;}
        g.latestX=event.clientX;g.latestY=event.clientY;g.latestMods=mod(event);
        if(!g.active&&Math.abs(g.latestX-g.x)+Math.abs(g.latestY-g.y)>4){
          if(response.revision!==g.revision){finishGesture();return;}
          // MuseScore moves the canvas at once when the press hit nothing
          // movable (NotationViewInputController::mouseMoveEvent). The press
          // already asked the worker (canPanAt), so no second round trip.
          if(press?.state==="empty"&&!g.startMod.shift&&!g.startMod.control&&!g.startMod.alt){
            press=null;
            const pg=gesture={kind:"pan",id:g.id,x:g.x,y:g.y,left:g.left,top:g.top,
              latestX:g.latestX,latestY:g.latestY,moved:true};
            pg.pan=()=>{paper.scrollLeft=pg.left+pg.x-pg.latestX;paper.scrollTop=pg.top+pg.y-pg.latestY;pauseFollow();};
            paper.setPointerCapture(event.pointerId);
            requestPointerFrame(pg);
            return;
          }
          if(busy)return;   // the press selection is still answering; begin on the next move
          g.active=true;
          g.sid=++nativePointerId;
          if(g.boxDirty)g.syncPageBox();
          g.point={...viewPoint(event.clientX,event.clientY,g),...g.latestMods};
          paper.focus({preventScroll:true});
          paper.setPointerCapture(event.pointerId);
          pauseFollow();
          nativePointer=g;
          if(!send("pointer-gesture",{...pointerPacket(g,"begin",g.startPoint),firstMove:g.point})){
            discardPointer(g);finishGesture();return;
          }
          g.flight="begin";
          return;
        }
        if(!g.active)return;
        if(g.ready==="pan"){requestPointerFrame(g);return;}
        if(g.flight==="page")return;
        g.recordMotion();
        requestPointerFrame(g);
        return;
      }
      if(g.kind==="maybe-pan"){
        if(!(event.buttons&1)){finishGesture();return;}
        g.latestX=event.clientX;g.latestY=event.clientY;
        g.pan=()=>{
          if(g.allowed&&gesture===g){
            paper.scrollLeft=g.left+g.x-g.latestX;
            paper.scrollTop=g.top+g.y-g.latestY;
            pauseFollow();
          }
        };
        if(!g.active&&Math.abs(g.latestX-g.x)+Math.abs(g.latestY-g.y)>4){
          g.active=true;paper.setPointerCapture(event.pointerId);
          pauseFollow();
          if(g.outside||!response?.editing){g.allowed=true;}
          else if(g.box&&g.height){
            const p=viewPoint(g.x,g.y,g);
            if(!probePan(p.x,p.y,ok=>{
              g.allowed=ok&&response?.revision===g.revision&&response?.page===g.page;
              requestPointerFrame(g);
            },g.box.width/960))g.allowed=false;
          }else g.allowed=false;
        }
        if(g.active&&g.allowed)requestPointerFrame(g);
        return;
      }
      if (g.kind==="pan") {
        g.latestX=event.clientX;g.latestY=event.clientY;
        requestPointerFrame(g);return;
      }
      if (!(event.buttons&1)) {finishGesture();return;}
      if (!g.active && Math.abs(event.clientX-g.x)+Math.abs(event.clientY-g.y)>4)g.active=true;
    });
    paper.addEventListener("pointerup", event => {
      if (!gesture || gesture.id!==event.pointerId) return;
      const started=gesture;
      if(started.active&&(started.ready==="pan"||started.kind==="maybe-pan")||started.kind==="pan"){
        started.latestX=event.clientX;started.latestY=event.clientY;
        started.pan?.();
      }
      const g=finishGesture();
      if(g.kind==="native"){
        if(!g.active){
          if(g.startMod.alt&&event.altKey&&!event.ctrlKey&&!event.shiftKey&&!busy&&stable(g)&&clientInside(event,g)){
            const at=viewPoint(event.clientX,event.clientY,g);
            send("content",{action:"range-copy-at",x:at.x,y:at.y});
          }
          return;
        }
        suppressPointerClick=true;setTimeout(()=>{suppressPointerClick=false;},0);
        if(nativePointer===g){
          g.finished=true;g.released=true;
          g.latestX=event.clientX;g.latestY=event.clientY;
          if(g.flight==="page"){
            g.releaseClient={clientX:event.clientX,clientY:event.clientY,...{
              shiftKey:event.shiftKey,ctrlKey:event.ctrlKey,altKey:event.altKey}};
          }else{
            g.pageDelta=0;g.releaseAt(event);pumpPointer();
          }
        }
        return;
      }
      if(g.kind==="maybe-pan"){
        if(g.active){suppressPointerClick=true;setTimeout(()=>{suppressPointerClick=false;},0);}
        return;
      }
      if (g.kind==="pan") {
        // A canvas drag is not a click: the release must not select.
        if(g.moved){suppressPointerClick=true;setTimeout(()=>{suppressPointerClick=false;},0);}
        return;
      }
      if (g.kind!=="duplicate") return;
      suppressPointerClick=true;
      setTimeout(()=>{suppressPointerClick=false;},0);
      if (!response?.editing || response.input || busy) return;
      paper.focus({preventScroll:true});
      const a=viewPoint(g.x,g.y,g),b=viewPoint(event.clientX,event.clientY,g);
      {
        const live=sheet()?.getBoundingClientRect();
        if(!live || ![live.left===g.box.left,live.top===g.box.top,live.width===g.box.width,live.height===g.box.height].every(Boolean) ||
           response.page!==g.page || response.revision!==g.revision ||
           event.clientX<live.left||event.clientX>=live.right||event.clientY<live.top||event.clientY>=live.bottom)return;
        if(g.active && event.ctrlKey && event.shiftKey && !event.altKey){
          send("content",{action:"drag-duplicate",sourceX:a.x,sourceY:a.y,x:b.x,y:b.y});
        }
      }
    });
    const cancelOwnedPointer=event=>{
      if(gesture?.id===event.pointerId)finishGesture(true);
    };
    paper.addEventListener("pointercancel",cancelOwnedPointer);
    paper.addEventListener("lostpointercapture",cancelOwnedPointer);
    paper.addEventListener("pointerleave",event=>{
      if(gesture?.id===event.pointerId&&["native","maybe-pan"].includes(gesture.kind)&&!gesture.active)finishGesture();
    });
    paper.addEventListener("auxclick",event=>{if(event.button===1)event.preventDefault();});
    paper.addEventListener("wheel",event=>{
      const g=nativePointer;
      if(g&&!g.finished){
        event.preventDefault();event.stopPropagation();
        if(!g.paged||g.flight==="page"||event.ctrlKey||event.altKey||event.metaKey||!event.deltaY)return;
        const delta=event.deltaY>0?1:-1;
        const max=Math.max(0,paper.scrollHeight-paper.clientHeight);
        const limit=delta<0?paper.scrollTop<=1:paper.scrollTop>=max-1;
        if(limit&&response.page+delta>=1&&response.page+delta<=response.pages){
          g.pageDelta=delta;pumpPointer();
        }else{
          const unit=event.deltaMode===1?16:event.deltaMode===2?paper.clientHeight:1;
          paper.scrollBy(0,event.deltaY*unit);
          g.invalidateBox();
        }
        return;
      }
      if(event.ctrlKey&&!event.altKey&&!event.metaKey&&sheet()&&!sheet().hidden){
        event.preventDefault();
        const factor=event.deltaMode===1?16:event.deltaMode===2?paper.clientHeight:1;
        const delta=Math.hypot(event.deltaX,event.deltaY)*(event.deltaY< -event.deltaX?1:-1)*factor;
        if(delta){pauseFollow();setCanvasZoom(zoom*Math.pow(2,delta/720),event.clientX,event.clientY);}
        return;
      }
      if (!event.shiftKey || event.ctrlKey || event.altKey || event.metaKey) return;
      event.preventDefault();
      paper.scrollLeft+=(event.deltaX||event.deltaY)*(event.deltaMode===1?16:event.deltaMode===2?paper.clientWidth:1);
    },{passive:false});

    root.addEventListener("toggle",event=>{
      const menu=event.target;
      if(!menu.matches?.(".astra-native-rhythm-menu,.astra-ns-menu")||!menu.open)return;
      const anchor=menu.querySelector("summary");
      if(anchor.getAttribute("aria-disabled")==="true"){menu.open=false;return;}
      for(const other of root.querySelectorAll(".astra-native-rhythm-menu[open],.astra-ns-menu[open]")){
        if(other!==menu&&!other.contains(menu)&&!menu.contains(other))other.open=false;
      }
      const box=anchor.getBoundingClientRect(),pop=menu.querySelector(".astra-native-rhythm-popover,.astra-ns-menu-pop");
      const left=Math.min(box.left,Math.max(4,innerWidth-pop.offsetWidth-4));
      const top=Math.min(box.bottom+3,Math.max(4,innerHeight-pop.offsetHeight-4));
      Object.assign(pop.style,{left:left+"px",top:top+"px"});
    },true);
    // Native DockPageView::toggleDock closes an open tab, even when it is
    // behind another one, and opens a closed tab into its dock group.
    const sidebar=root.querySelector(".astra-native-palette");
    const dockTabs=[...sidebar.querySelectorAll("[role=tab][data-score-dock]")];
    const dockKeys={F7:"layout",F8:"properties",F9:"palettes"};
    const dockOrder=["palettes","layout","properties","selection"], openDocks=new Set(dockOrder);
    const dockPanes={palettes:".astra-ms-palette-content",layout:".astra-ms-layout-dock",properties:".astra-ms-inspector-dock",selection:".astra-native-filter-dock"};
    let dock="palettes";
    function syncDocks() {
      sidebar.hidden=!openDocks.size;
      sidebar.querySelector(".astra-ms-palette-tab-wrap").hidden=!openDocks.has("palettes");
      for(const tab of dockTabs){
        const name=tab.dataset.scoreDock;
        tab.hidden=!openDocks.has(name);
        tab.setAttribute("aria-selected",String(name===dock));
      }
      for(const [name,selector] of Object.entries(dockPanes))sidebar.querySelector(selector).hidden=dock!==name;
      root.querySelectorAll("[data-score-dock-toggle]").forEach(item=>
        item.setAttribute("aria-pressed",String(openDocks.has(item.dataset.scoreDockToggle))));
      try{localStorage.setItem("astra.score.openDocks",JSON.stringify([...openDocks]));}catch{}
    }
    function showDock(next) {
      if(next)openDocks.add(next);
      const previous=dock;
      dock=next;
      syncDocks();
      if(previous===dock)return;
      inspector.setShown(dock==="properties");
      if(dock==="palettes")catalogue.invalidateLayout();
      if(dock==="layout"){layoutPanel.render();layoutPanel.want();}
    }
    function toggleDock(name) {
      if(!openDocks.has(name)){showDock(name);return;}
      openDocks.delete(name);
      if(dock!==name){syncDocks();return;}
      showDock(dockOrder.find(key=>openDocks.has(key))||null);
      if(sidebar.contains(document.activeElement)&&document.activeElement.closest("[hidden]"))
        area().focus({preventScroll:true});
    }
    // View > 符号面板 / 版面 / 属性 / 选择过滤器: which docks are open is remembered.
    (()=>{
      let saved=null;
      try{saved=JSON.parse(localStorage.getItem("astra.score.openDocks")||"null");}catch{}
      if(!Array.isArray(saved))return;
      openDocks.clear();
      for(const key of dockOrder)if(saved.includes(key))openDocks.add(key);
      const first=dockOrder.find(key=>openDocks.has(key))||null;
      if(first===dock)syncDocks();else showDock(first);
    })();
    function focusDock() {
      if(!dock)return false;
      const section=sidebar.querySelector(dockPanes[dock]);
      const candidate=dock==="palettes"?
        section.querySelector(".astra-native-catalogue-search:not([hidden])"):null;
      const target=(candidate&&!candidate.closest("[hidden]")?candidate:null) ||
        (dock==="palettes"?section.querySelector(".astra-native-palette-head"):null) ||
        section.querySelector("button:not(:disabled),input:not(:disabled),select:not(:disabled)") ||
        dockTabs.find(tab=>tab.dataset.scoreDock===dock);
      if(!target)return false;
      target.focus({preventScroll:true});target.scrollIntoView({block:"nearest",inline:"nearest"});
      return true;
    }
    root.addEventListener("change",event=>{
      const input=event.target.closest?.("[data-selection-filter]");
      if(!input)return;
      const [group,i]=input.dataset.selectionFilter.split(":");
      send("selection-filter",{group,index:Number(i),include:input.checked});
    });
    root.addEventListener("change",event=>{
      const k=event.target.dataset?.stockVisibility;
      if(!k)return;
      toolbarConfig.set(k,event.target.checked);
      try{localStorage.setItem("astra-score-musescore465-toolbar",JSON.stringify([...toolbarConfig]));}catch{}
      const tools=root.querySelector(".astra-native-notation-tools");
      tools.innerHTML=buildToolbar();
      sync();
      const settings=tools.querySelector(".astra-ms-customize");
      settings.open=true;settings.querySelector('[data-stock-visibility="'+k+'"]')?.focus({preventScroll:true});
    });
    const findBar=document.createElement("div");
    findBar.className="astra-score-find-bar";
    findBar.hidden=true;
    findBar.innerHTML='<label>查找 / 跳转：<input type="text" class="astra-score-find-field" maxlength="128" placeholder="小节 12、页 p2、排练标记 rA"></label> <span class="astra-score-find-status" role="status"></span> <button type="button" class="astra-score-find-close" aria-label="关闭查找">×</button>';
    root.querySelector(".astra-native-score-tools").after(findBar);
    const findField=findBar.querySelector(".astra-score-find-field");
    const showFind=show=>{
      if(show&&(!response?.revision||response?.input||response?.textEditing))return;
      findBar.hidden=!show;
      if(show){clearEntryKeys();pauseFollow();findField.focus({preventScroll:true});findField.select();}
      else area()?.focus({preventScroll:true});
    };
    root.addEventListener("astra-score-find-open",()=>showFind(true));
    findBar.querySelector("button").addEventListener("click",()=>showFind(false));
    const queueFind=()=>{
      scoreFindPending={query:findField.value,project,document:nativeDocumentGeneration,host:root.parentElement};
      pauseFollow();
      pumpScoreFind();
    };
    findField.addEventListener("input",e=>{if(!e.isComposing)queueFind();});
    findField.addEventListener("compositionend",queueFind);
    findField.addEventListener("keydown",e=>{
      if(e.isComposing||e.keyCode===229)return;
      if(e.key==="Enter"||e.key==="Escape"){e.preventDefault();e.stopPropagation();showFind(false);}
    });
    // Discrete palette search: magnifier reveals the field; Escape clears and hides it.
    const searchToggle=root.querySelector(".astra-native-palette-search-toggle");
    const searchRow=root.querySelector(".astra-native-palette-search");
    const searchField=searchRow?.querySelector("input");
    const showSearch=show=>{
      searchRow.hidden=!show;searchToggle.setAttribute("aria-expanded",String(show));
      if(show){searchField.focus({preventScroll:true});searchField.select();return;}
      if(searchField.value){searchField.value="";searchField.dispatchEvent(new Event("input"));}
      (sidebar.querySelector("[data-palette-manage]:not(:disabled)")||searchToggle).focus({preventScroll:true});
    };
    searchToggle?.addEventListener("click",()=>showSearch(searchRow.hidden));
    searchField?.addEventListener("keydown",event=>{if(event.key==="Escape"){event.preventDefault();showSearch(false);}});
    root.addEventListener("keydown", event => {
      if(event.defaultPrevented||event.isComposing||event.keyCode===229||event.repeat||event.altKey||event.metaKey||
         response?.textEditing||!root.isConnected||sidebar.querySelector(".astra-ms-palette-popup[open]"))return;
      const findKey=event.key.toLowerCase()==="f"&&event.ctrlKey&&!event.shiftKey;
      if(findKey&&(event.target===findField||!event.target.closest("input,textarea,select,[contenteditable]:not([contenteditable=false])"))){
        if(response?.revision&&!response.input){
          event.preventDefault();event.stopPropagation();showFind(true);return;
        }
      }
      if(event.target===area()&&response?.revision&&!event.shiftKey&&!event.altKey&&
         (/^Page(Up|Down)$/.test(event.key)||!event.ctrlKey&&["Home","End"].includes(event.key))){
        event.preventDefault();event.stopPropagation();
        if(busy||nativePointer)return;
        pauseFollow();
        if(event.key==="Home"||event.key==="End")send("score-edge",{edge:event.key==="Home"?"start":"end"});
        else {
          const el=area(),delta=event.key==="PageUp"?-1:1;
          const limit=delta<0?el.scrollTop<1:el.scrollTop+el.clientHeight>=el.scrollHeight-1;
          const next=response.page+delta;
          if(event.ctrlKey||limit){
            if(next>=1&&next<=response.pages)send("page",{delta,edge:!event.ctrlKey&&delta<0?"end":"start"});
          }else el.scrollBy(0,delta*el.clientHeight*0.8);
        }
        return;
      }
      const searchKey=event.key==="F9"&&event.ctrlKey&&!event.shiftKey;
      const focusedField=event.target.closest("input,textarea,select,[contenteditable]:not([contenteditable=false])");
      if(focusedField&&!(searchKey&&focusedField===searchField))return;
      if(searchKey){
        event.preventDefault();event.stopPropagation();
        showDock("palettes");showSearch(true);return;
      }
      if(!event.ctrlKey&&!event.shiftKey&&Object.hasOwn(dockKeys,event.key)){
        event.preventDefault();event.stopPropagation();toggleDock(dockKeys[event.key]);return;
      }
      if(event.key==="F6"&&event.shiftKey&&!event.ctrlKey&&!sidebar.contains(event.target)&&focusDock()){
        event.preventDefault();event.stopPropagation();return;
      }
      const tab=event.target.closest("[role=tab][data-score-dock]");
      if(tab&&!event.shiftKey&&!event.ctrlKey&&["ArrowLeft","ArrowRight","Home","End"].includes(event.key)){
        const list=dockTabs.filter(x=>!x.hidden&&!x.disabled), i=list.indexOf(tab);
        if(i<0)return;
        event.preventDefault();event.stopPropagation();
        const j=event.key==="Home"?0:event.key==="End"?list.length-1:
          (i+(event.key==="ArrowLeft"?list.length-1:1))%list.length;
        showDock(list[j].dataset.scoreDock);list[j].focus({preventScroll:true});
      }
    },true);
    root.addEventListener("keydown", event => {
      if (event.key!=="Escape") return;
      const menu=event.target.closest(".astra-native-rhythm-menu[open],.astra-ns-menu[open]");
      if (menu) { event.preventDefault();event.stopPropagation();menu.open=false;menu.querySelector("summary").focus(); }
    });
    const locksDialog=root.querySelector(".astra-native-system-lock-dialog");
    const locksForm=locksDialog.querySelector("form");
    locksForm.addEventListener("change",()=>{
      locksForm.elements.interval.disabled=locksForm.elements.mode.value!=="interval";
    });
    locksForm.querySelector("[data-lock-cancel]").addEventListener("click",()=>locksDialog.close());
    locksDialog.addEventListener("close",()=>area()?.focus({preventScroll:true}));
    locksForm.addEventListener("submit",event=>{
      event.preventDefault();event.stopPropagation();
      if(!response?.editing || response.input || response.textEditing || response.readOnly || busy || !locksForm.reportValidity())return;
      const mode=locksForm.elements.mode.value;
      const value=mode==="interval"?"interval:"+locksForm.elements.interval.value:mode;
      if(send("tool",{tool:"system-lock-plan",value}))locksDialog.close();
    });
    const insertDialog=root.querySelector(".astra-native-insert-measures-dialog");
    const insertForm=insertDialog.querySelector("form");
    insertForm.querySelector("[data-insert-cancel]").addEventListener("click",()=>insertDialog.close());
    insertDialog.addEventListener("close",()=>area()?.focus({preventScroll:true}));
    insertForm.addEventListener("submit",event=>{
      event.preventDefault();event.stopPropagation();
      if(!response?.editing || response.input || response.textEditing || response.readOnly || busy || !insertForm.reportValidity())return;
      const action=insertForm.elements.position.value;
      const count=insertForm.elements.count.valueAsNumber;
      if(insertForm.elements.position.selectedOptions[0]?.disabled || !Number.isInteger(count))return;
      if(send("content",{action,count}))insertDialog.close();
    });
    const measureDialog=root.querySelector(".astra-native-measure-properties-dialog");
    const measureForm=measureDialog.querySelector("form");
    measureForm.querySelector("[data-measure-cancel]").addEventListener("click",()=>measureDialog.close());
    measureDialog.addEventListener("close",()=>area()?.focus({preventScroll:true}));
    measureForm.addEventListener("submit",event=>{
      event.preventDefault();event.stopPropagation();
      if(!response?.editing || response.input || response.textEditing || response.readOnly || busy || !measureForm.reportValidity())return;
      const f=measureForm.elements;
      const value={actual:[f.n.valueAsNumber,Number(f.d.value)],numberMode:Number(f.numberMode.value),
        numberOffset:f.numberOffset.valueAsNumber,stretch:f.stretch.valueAsNumber,repeatCount:f.repeatCount.valueAsNumber,
        excludeNumber:f.excludeNumber.checked,breakMMR:f.breakMMR.checked,
        staves:[...measureForm.querySelectorAll("[data-measure-staff]")].map(row=>({
          visible:row.querySelector('[data-staff-field=visible]').checked,
          stemless:row.querySelector('[data-staff-field=stemless]').checked}))};
      if(!value.actual[1])return;
      const direction=event.submitter?.dataset.measureStep;
      if(direction){
        if(send("content",{action:"measure-properties-step",direction,value})){
          measureDialog.dataset.stepPending="true";measureForm.inert=true;
        }
      }else if(send("content",{action:"measure-properties",value}))measureDialog.close();
    });
    const transposeDlg=root.querySelector(".astra-native-transpose-dialog");
    const transposeForm=transposeDlg.querySelector("form");
    transposeForm.elements.mode.addEventListener("change",()=>refreshTransposeDialog(transposeForm,response?.transpose));
    transposeForm.querySelector("[data-transpose-cancel]").addEventListener("click",()=>transposeDlg.close());
    transposeDlg.addEventListener("close",()=>area()?.focus({preventScroll:true}));
    transposeForm.addEventListener("submit",event=>{
      event.preventDefault();event.stopPropagation();
      if(!response?.editing || response.input || response.textEditing || response.readOnly || busy || !transposeForm.reportValidity())return;
      const f=transposeForm.elements, mode=f.mode.value;
      if(send("transpose",{mode,direction:f.direction.value,key:Number(f.key.value),
        interval:Number(mode==="diatonic"?f.degree.value:f.interval.value),
        transposeKeys:!f.transposeKeys.disabled&&f.transposeKeys.checked,
        chordNames:!f.chordNames.disabled&&f.chordNames.checked,useDouble:!f.useDouble.disabled&&f.useDouble.checked}))transposeDlg.close();
    });
    root.addEventListener("submit", event => {
      const form = event.target.closest("[data-native-rhythm]");
      if (!form) return;
      event.preventDefault();
      if (!response?.editing || response.readOnly || busy || !form.reportValidity()) return;
      const data = new FormData(form), tool = event.submitter?.hasAttribute("data-rhythm-rebar") ? "meter-rebar" : form.dataset.nativeRhythm;
      form.closest(".astra-native-rhythm-menu").open = false;
      const parent=form.closest(".astra-ns-menu");if(parent)parent.open=false;
      area().focus({preventScroll:true});
      const ratio=String(data.get("n"))+(tool==="tuplet"?":":"/")+String(data.get("d"));
      send("tool", {tool, value:tool==="tuplet"?ratio+":"+data.get("number")+":"+data.get("bracket"):ratio});
    });
    root.addEventListener("click", event => {
      for(const menu of root.querySelectorAll(".astra-native-rhythm-menu[open]")){
        if(!menu.contains(event.target) || event.target.closest("[data-score-tool=tuplet]"))menu.open=false;
      }
      for(const menu of root.querySelectorAll(".astra-ns-menu[open]")){
        if(!menu.contains(event.target))menu.open=false;
        else if(event.target.closest(".astra-ns-menu-pop [data-native-score]")){
          menu.open=false;menu.querySelector("summary").focus({preventScroll:true});
        }
      }
      if (popup&&!popup.contains(event.target))closeContext();
      if (suppressPointerClick) {event.preventDefault();return;}
      const otherPage=event.target.closest?.(".astra-native-page");
      if(otherPage&&area()?.contains(otherPage)&&event.button===0){activatePage(Number(otherPage.dataset.page),event);return;}
      const viewConfigToggle=event.target.closest("[data-score-view-config]");
      if(viewConfigToggle||event.target.closest("[data-score-tidy]")){
        const menu=event.target.closest("details");
        if(menu&&!viewConfigToggle){menu.open=false;menu.querySelector("summary").focus({preventScroll:true});}
        if(viewConfigToggle){
          const k=viewConfigToggle.dataset.scoreViewConfig;
          viewConfig[k]=!viewConfig[k];
          try{localStorage.setItem("astra.score.viewConfig",JSON.stringify(viewConfig));}catch{}
          syncViewConfigButtons();applyViewConfig(true);
        }else if(response?.editing&&!response.readOnly){
          const title=documentTitle();
          send("tidy-score",{title,composer:""});
        }
        return;
      }
      if(event.target.closest("[data-score-vector-view]")){
        vectorMode=!vectorMode&&!!window.AstraVectorPage;
        try{localStorage.setItem("astra.score.vectorView",vectorMode?"1":"0");}catch{}
        pages.doc=null;layoutPages();syncViewConfigButtons();schedulePages(0);
        return;
      }
      const flowToggle=event.target.closest("[data-score-page-flow]");
      if(flowToggle){
        setPageFlow(!pageFlowHorizontal);
        return;
      }
      const viewToggle=event.target.closest("[data-score-dock-toggle]");
      if(viewToggle){
        toggleDock(viewToggle.dataset.scoreDockToggle);
        return;
      }
      const tab=event.target.closest("[role=tab][data-score-dock]");
      if(tab){showDock(tab.dataset.scoreDock);return;}
      const reveal = event.target.closest("[data-palette-reveal]");
      if (reveal) {
        if(!catalogue.reveal(reveal.dataset.paletteReveal))
          root.querySelector(".astra-native-catalogue-notice").textContent="符号面板中暂无该类别。";
        return;
      }
      const action = event.target.closest("[data-native-score]");
      if (action) {
        if (action.disabled || action.getAttribute("aria-disabled") === "true") return;
        if (busy && !nativePointer && !["open","autoOpen","reopen"].includes(pending) &&
            !["smaller","larger","fit","findScore"].includes(action.dataset.nativeScore)) {
          rememberPointerAction({control:action},deferredInputScope());
          return;
        }
        dispatch(action.dataset.nativeScore, action);
        return;
      }
      if (event.target===sheet()&&response?.revision&&!response.editing&&event.button===0&&
          !event.altKey&&!event.metaKey&&!event.ctrlKey&&!event.shiftKey){
        area().focus({preventScroll:true});
        browseSeek(event);
        return;
      }
      if (event.target !== sheet() || !response?.editing || response.textEditing || event.button !== 0 || event.altKey || event.metaKey || (event.ctrlKey&&event.shiftKey&&!response.input)) return;
      area().focus({preventScroll:true});
      const pressed=press;press=null;
      const hitAt={clientX:event.clientX,clientY:event.clientY,shiftKey:event.shiftKey,ctrlKey:event.ctrlKey};
      if(pressed&&Math.abs(pressed.at.clientX-event.clientX)+Math.abs(pressed.at.clientY-event.clientY)<=4&&performance.now()-pressed.t<2000){
        if(pressed.state==="selected")return;            // the press already selected it
        if(pressed.state==="probe"){pressed.clicked=hitAt;press=pressed;return;}   // probe answers for it
      }
      scoreHit(hitAt);
    });
    // MuseScore selects an element (or, in note input, places the note) on
    // mouse press, while empty staff space is selected on release and a drag
    // from it pans. A read-only native hit probe (canPanAt, no selection
    // change) tells the two apart before anything is selected.
    let press=null;
    // Browsing mode, as MuseScore's "play from the clicked note": the click
    // moves the playback position (and its cursor) without touching the score.
    function browseSeek(event){
      const box=sheet().getBoundingClientRect(), scope=transportScope;
      if(!box.width||!scope||scope.path!==project||!playbackCursor)return;
      const x=(event.clientX-box.left)*960/box.width, y=(event.clientY-box.top)*response.frameHeight/box.height;
      const revision=response.revision, doc=nativeDocumentGeneration, page=response.page;
      // Stay on the pass being played (a second time through a repeat);
      // the host falls back to the first pass if the bar isn't repeated.
      const occ=Number.isInteger(scope.status?.tickOccurrence)&&scope.status.tickOccurrence>0?scope.status.tickOccurrence:1;
      const go=tick=>{
        if(tick===null||tick<0||response?.revision!==revision||nativeDocumentGeneration!==doc||transportScope?.path!==project)return;
        browseHold=true;
        playbackCursor.pin(tick);
        backend?.controlScorePlayback?.(JSON.stringify({project,revision,action:"seekTick",value:{tick,occurrence:occ}}));
      };
      // A note, chord or rest seeks to its own native tick; only empty staff
      // space is placed by the cursor geometry. A page just turned to may not
      // have that geometry yet: wait for it briefly rather than drop the click.
      const until=performance.now()+1500;
      const place=()=>{
        if(response?.revision!==revision||nativeDocumentGeneration!==doc||response?.page!==page)return;
        if(acceptedPlaybackFrame?.page===page)playbackCursor.frameAccepted(acceptedPlaybackFrame);
        if(playbackCursor.ready())go(playbackCursor.locate(x,y));
        else if(performance.now()<until)requestAnimationFrame(place);
      };
      if(!probeTick(x,y,box.width/960,hit=>hit>=0?go(hit):place()))place();
    }
    function scoreHit(event){
      const box = sheet().getBoundingClientRect();
      const fields={x:(event.clientX-box.left)*960/box.width,y:(event.clientY-box.top)*response.frameHeight/box.height,
        shift:event.shiftKey,control:event.ctrlKey,cssScale:box.width/960};
      if(nativePointer?.finished&&!response.input&&nativePointer.home===root.parentElement){
        rememberPointerAction({fields},nativePointer);
      }else if(!nativePointer&&(busy||deferredPointerActions.length)&&!["open","autoOpen","reopen"].includes(pending)){
        // Every click reaches the native document in order, as in MuseScore:
        // Ctrl/Shift hits depend on the selection left by the previous one,
        // and a note-input click places a note after the command before it.
        rememberPointerAction({fields},deferredInputScope());
        if(!busy)flushPointerFollowup();
      }else if(!busy)send("select",fields);
    }
    area().addEventListener("pointerdown",event=>{
      press=null;
      if(event.pointerType!=="mouse"||event.button!==0||event.target!==sheet()||!response?.editing||response.readOnly||
         response.textEditing||event.altKey||event.metaKey||(event.ctrlKey&&event.shiftKey&&!response.input)||
         nativePointer||event.detail>1||busy||deferredPointerActions.length)return;   // busy: the click path keeps order
      const box=sheet().getBoundingClientRect();
      const at={clientX:event.clientX,clientY:event.clientY,shiftKey:event.shiftKey,ctrlKey:event.ctrlKey};
      const p={at,t:performance.now(),state:"probe",clicked:null};
      area().focus({preventScroll:true});
      if(!probePan((at.clientX-box.left)*960/box.width,(at.clientY-box.top)*response.frameHeight/box.height,panAllowed=>{
        if(press!==p)return;
        if(panAllowed){            // empty space: nothing on press, exactly as before
          press=p.clicked?null:p;p.state="empty";
          if(p.clicked)scoreHit(p.clicked);
        }else{                     // an element (or note input): select / place now
          p.state="selected";
          scoreHit(p.at);
          if(p.clicked)press=null;
        }
      },box.width/960))return;
      press=p;
    });
    // A plain click on the score while keys are still queued is replayed after
    // them (see the click handler), so it must not discard those keys.
    root.addEventListener("pointerdown",event=>{
      if(busy&&event.button===0&&event.target===sheet()&&!event.altKey&&!event.metaKey&&!(event.ctrlKey&&event.shiftKey))return;
      clearEntryKeys();queuedNavigation=[];
    },true);
    area().addEventListener("blur",()=>{queuedNavigation=[];queuedPitchTaps.length=0;clearEntryKeys();
      clearPointerFollowup();if(gesture?.kind==="native")finishGesture(true);
    });
    area().addEventListener("keydown", event => {
      if(response?.textEditing){event.stopPropagation();return;}
      if (!response?.editing || response.readOnly || event.isComposing || event.keyCode===229 ||
          event.metaKey || event.target !== area()) return;
      // Existing popup, drag and text handlers keep first refusal on Escape.
      if(rememberPointerKey(event))return;
      if(event.key==="Escape"&&nativePointer?.finished){
        event.preventDefault();event.stopPropagation();
        clearPointerFollowup();return;
      }
      if (event.key==="Escape" && gesture) {
        event.preventDefault();event.stopPropagation();
        const g=finishGesture(true);
        if (paper.hasPointerCapture(g.id))paper.releasePointerCapture(g.id);
        suppressPointerClick=true;
        setTimeout(()=>{suppressPointerClick=false;},0);
        return;
      }
      if(event.key==="Escape"&&!event.ctrlKey&&!event.altKey&&!event.shiftKey) {
        if(popup){event.preventDefault();event.stopPropagation();closeContext(true);return;}
        if(response.input||entryFlight||(!busy&&response.selection?.mode&&response.selection.mode!=="none")){
          event.preventDefault();event.stopPropagation();
          if(event.repeat)return;
          queuedPitchTaps.length=0;
          if(busy&&entryFlight?.request===busy&&entryContext(entryFlight)&&!deferredPointerActions.length){
            entryFIFO=[];entryEscape=true;
          }else if(!busy){
            if(response.input)send("tool",{tool:"input-stop",value:""});
            else send("selection-clear");
          }else if(!nativePointer)rememberPointerAction({key:{key:"Escape",code:"Escape",bubbles:true,cancelable:true,composed:true}},deferredInputScope());
          return;
        }
      }
      const activeFlight=busy&&entryFlight?.request===busy&&entryContext(entryFlight);
      const entry=entryKey(event,activeFlight?entryFlight.inputMode:response["input-method"]);
      const modifierOnly=["Shift","Control","Alt","Meta","AltGraph","CapsLock"].includes(event.key);
      // Keep arrival order once anything is waiting behind the native command.
      if(!replayingInput&&!modifierOnly&&event.key!=="Tab"&&!nativePointer&&deferredPointerActions.length&&
         (busy||entryFIFO.length||queuedNavigation.length||queuedPitchTaps.length)){
        deferInputKey(event);return;
      }
      if(busy){
        const intent=selectionNavigation(event);
        if(mayQueueNavigation(intent)){
          event.preventDefault();event.stopPropagation();
          if(!event.repeat&&queuedNavigation.length<24)queuedNavigation.push({...navStamp(),...intent});
          return;
        }
        if(event.key==="Escape")queuedNavigation=[];
        if(activeFlight&&entry&&!["input-method","toggle-timewise"].includes(entry[0])){
          event.preventDefault();event.stopPropagation();
          if(!event.repeat&&!entryEscape&&entryFIFO.length<entryLimit)entryFIFO.push(entry);
          return;
        }
        if(pending==="pitch"&&pitchTarget()&&!event.repeat&&!event.altKey&&!event.shiftKey&&
          (event.key==="ArrowUp"||event.key==="ArrowDown")&&queuedPitchTaps.length<7){
          event.preventDefault();event.stopPropagation();
          queuedPitchTaps.push({project,page:response.page,generation:nativeDocumentGeneration,
            fields:{up:event.key==="ArrowUp",octave:event.ctrlKey}});
        }
        // Anything else (Delete, R, marks, mode keys, a click-placed note's
        // follow-up letter...) is replayed after the native command.
        if(!event.defaultPrevented&&!modifierOnly&&event.key!=="Tab"&&!nativePointer)deferInputKey(event);
        return;
      }
      const invoke=(tool,value)=>{
        event.preventDefault();event.stopPropagation();
        if(!event.repeat)send("tool",{tool,value});
      };
      if(entry){
        const [tool,value]=entry;
        if(tool==="select-interval"&&!(response?.canPitch??response.selectedPitch>=0))return;
        if(response.input||["input-method","toggle-timewise","insert-rest","note-letter","chord-letter","insert-letter","stage-pitch"].includes(tool)){
          event.preventDefault();event.stopPropagation();
          if(!event.repeat)sendEntryTool(tool,value);
        }else invoke(tool,value);
        return;
      }
      const navKey=event.key;
      if(!response.input&&response.gripEdit&&!event.altKey&&!event.metaKey&&
         (/^Arrow(Left|Right|Up|Down)$/.test(navKey)||(navKey==="Tab"&&!event.ctrlKey))){
        event.preventDefault();event.stopPropagation();
        send("line-key",{key:navKey,shift:event.shiftKey,control:event.ctrlKey});return;
      }
      if(!response.input&&event.ctrlKey&&!event.shiftKey&&!event.altKey&&!event.metaKey&&["k","l","g"].includes(navKey.toLowerCase())&&
         (response.selectedRhythm||response.selection?.mode==="range")){
        event.preventDefault();event.stopPropagation();
        if(!event.repeat)send("text-add",{role:({k:"chord",l:"lyrics",g:"figured-bass"})[navKey.toLowerCase()]});return;
      }
      if(event.altKey&&!event.ctrlKey&&!event.metaKey&&["+","="].includes(event.key)&&eligible("chord-tie","")){
        event.preventDefault();event.stopPropagation();if(!event.repeat)send("tool",{tool:"chord-tie",value:""});return;
      }
      const boundary=navKey==="Home"||navKey==="End";
      const nav=selectionNavigation(event);
      if(nav&&(nav.kind==="navigate"||
        (response.selection?.mode&&response.selection.mode!=="none"))){
        event.preventDefault();event.stopPropagation();
        if(!event.repeat)send(nav.kind,nav.fields);
        return;
      }
      if(event.ctrlKey&&!event.altKey&&!event.shiftKey&&["+","=","-","0"].includes(navKey)){
        event.preventDefault();event.stopPropagation();
        setCanvasZoom(navKey==="0"?1:zoom+(navKey==="-"?-0.1:0.1));
        return;
      }
      if(event.altKey)return;
      if(event.shiftKey&&!event.ctrlKey&&!event.metaKey&&["<",">",",","."].includes(event.key)&&eligible("hairpin","")){
        event.preventDefault();event.stopPropagation();
        if(!event.repeat)send("tool",{tool:"hairpin",value:["<",","].includes(event.key)?"cresc":"dim"});
        return;
      }
      if (((event.key==="F10"&&event.shiftKey)||event.key==="ContextMenu") &&
          !response.input && response.selection?.mode && response.selection.mode!=="none") {
        const b=paper.getBoundingClientRect();
        event.preventDefault();event.stopPropagation();
        showContext({...response,contextMenu:response.selection.mode==="range"?"range":"selection"},b.left+b.width/2,b.top+b.height/2);
        return;
      }
      if((event.key==="F2"||event.key==="Enter")&&!event.ctrlKey&&!event.shiftKey&&response.textEditable&&!response.input&&!response.textEditing){
        event.preventDefault();event.stopPropagation();send("text-action",{operation:"start"});return;
      }
      const key=event.key.toLowerCase();
      if (event.ctrlKey && !event.shiftKey && /^[2-9]$/.test(key)) {
        event.preventDefault();event.stopPropagation();
        if (!event.repeat) send("tool",{tool:"tuplet",value:"auto:"+key});
        return;
      }
      if(event.ctrlKey&&event.shiftKey&&!response.input&&["ArrowUp","ArrowDown"].includes(event.key)){
        const value=event.key==="ArrowUp"?"up":"down";
        if(eligible("cross-staff",value))invoke("cross-staff",value);
        return;
      }
      if(!event.ctrlKey){
        const marks=event.shiftKey?{o:"add-marcato",v:"add-sforzato",n:"add-tenuto",s:"add-staccato"}:
          {t:"tie",s:"add-slur",x:"flip"};
        const mark=marks[key];
        if(mark&&eligible(routes[mark][1],routes[mark][2])){invoke(routes[mark][1],routes[mark][2]);return;}
      }
      if(!response.input && event.ctrlKey && !event.shiftKey && (key==="delete"||key==="backspace") && response.timeDeleteKind){
        event.preventDefault();event.stopPropagation();
        if(!event.repeat)send("content",{action:"time-delete"});
        return;
      }
      const canContent=!response.input && !response.readOnly && response.selection?.mode!=="none";
      const rhythm=["Note","Rest","Range"].includes(response.selectionKind);
      const contentAction=event.ctrlKey ? (event.shiftKey?{q:"paste-half",w:"paste-double"}[key]:{c:"copy",x:"cut",v:"paste"}[key]) :
        (!event.shiftKey && (key==="delete"||key==="backspace") ? "delete" : !event.shiftKey && rhythm && key==="r" ? "repeat-selection" : null);
      if (canContent && contentAction && (!contentAction.startsWith("paste") || response.clipboardReady)) {
        event.preventDefault(); event.stopPropagation();
        if (!event.repeat) send("content",{action:contentAction});
        return;
      }
      // MuseScore: Up/Down (Ctrl: octave) moves the selected note, which in
      // note input is the note just entered; duration mode stages a pitch above.
      if ((event.key === "ArrowUp" || event.key === "ArrowDown") && !event.shiftKey && !event.altKey &&
          (!response.input || response["input-method"]!=="duration") && (response.canPitch ?? response.selectedPitch>=0)) {
        event.preventDefault(); event.stopPropagation();
        send("pitch",{up:event.key==="ArrowUp",octave:event.ctrlKey});
      }
    });
    sync();
    autoOpen(current, committed);
  }
  const compareExcerptListeners=new Set();
  function receiveCompareExcerpt(raw){
    for(const callback of compareExcerptListeners)callback(raw);
  }
  return {
    enabled: () => !!backend,
    // True while a native command runs or presses are still queued behind it.
    isBusy: () => Boolean(busy || deferredPointerActions.length || entryFIFO.length || queuedNavigation.length),
    attach: owner => {
      if(!owner||owner===backend)return;
      acceptedPlaybackFrame=null;playbackCursor?.invalidate(true);
      backend=owner;owner.response.connect(onResponse);owner.shadowResponse?.connect(shadowResponse);
      owner.saveStateChanged?.connect(raw=>{if(owner===backend)acceptSaveState(raw);});
      owner.compareExcerptReady?.connect(raw=>{if(owner===backend)receiveCompareExcerpt(raw);});
      owner.cursorLayoutResponse?.connect(raw=>{if(owner===backend)playbackCursor?.commit(raw);});
    },
    onCompareExcerpt(callback){
      if(typeof callback==="function")compareExcerptListeners.add(callback);
      return ()=>compareExcerptListeners.delete(callback);
    },
    requestCompareExcerpt(payload){
      if(!backend?.requestCompareExcerpt)return false;
      // The dedicated drawer never mounts the writable main score. On return
      // request a fresh Main Score frame after any temporary page restoration.
      autoStamp=undefined;
      backend.requestCompareExcerpt(JSON.stringify(payload));
      return true;
    },
    cancelCompareExcerpt:()=>backend?.cancelCompareExcerpt?.(),
    endCompareSession:()=>backend?.endCompareSession?.(),
    paintTransport,
    playbackDocument: () => root?.isConnected ?
      {root,project,revision:response?.revision||null,generation:nativeDocumentGeneration} : null,
    playbackReady: () => Boolean(backend && response?.revision && !response?.readOnly && !visiblyBusy() && root?.isConnected),
    setPlaybackPage: (path, active) => backend?.setPlaybackPage?.(path, active),
    togglePlayback: path => backend?.togglePlayback?.(path),
    controlPlayback: (path, kind, position=0) => backend?.controlPlayback?.(path, kind, position),
    markup,saveWithProject,historyForPage,
    setContextActions: provider => { menuProvider=typeof provider==="function"?provider:null; },
    preserve: preservePaperPosition,
    mount,
    focusTemporary(span,path){
      if(path!==project||!root?.closest(".temp-right:not([hidden])")||!Number.isFinite(span?.start_beat))return;
      const beat=span.start_beat;
      if(response?.textEditing)return;
      if(!response?.revision||busy){temporaryFocus={project:path,beat};return;}
      temporaryFocus=null;
      send("focus-measure",{beat});
    }
  };
})();
