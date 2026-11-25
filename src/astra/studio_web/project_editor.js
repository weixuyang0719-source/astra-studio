"use strict";

// The editor owns interaction, never a second tempo map or a mutable MIDI cache.
// All durable changes are seconds-based, version-bound operations on the backend.
window.AstraProjectEditor = (() => {
  let pendingRangeView=null,queuedRange=null,editScope=null,rebarBoundaryCache=null,meterClick=null,meterDoubleAt=-Infinity;
  let projectKey="", selected=new Set(), selectedCC=null, drag=null, ghost=null;
  let selectionDomain="notes";
  // Feedback owns no selected rows, preview geometry, or edit transaction.
  // The desktop bounds the pulse even if this page loses its timers/channel.
  let vstVoice=null,vstVoiceSerial=0;
  function vstFeedbackEpoch(){
    return typeof bridge!=='undefined'&&typeof bridge?.vstNoteFeedback==='function'&&
      typeof transportMidi!=='undefined'&&transportMidi&&tab==='notes'&&active()&&
      state.midi.vst_feedback?state.midi.vst_epoch:null;
  }
  function endVstFeedback(clear=false){
    const voice=vstVoice;vstVoice=null;
    if(!voice)return;
    const {owner,epoch,token}=voice;
    try{owner.vstNoteFeedback(JSON.stringify(clear?{op:'clear',epoch}:{op:'off',epoch,token}));}catch{}
  }
  function syncVstFeedback(){
    if(vstVoice&&(vstVoice.owner!==bridge||vstVoice.epoch!==vstFeedbackEpoch()))endVstFeedback(true);
  }
  function auditionVstNote(note,gesture=null,pitch=note?.pitch){
    const epoch=vstFeedbackEpoch();if(!epoch||!note||!Number.isInteger(pitch))return;
    if(gesture?.vstPitch===pitch)return;
    if(gesture)gesture.vstPitch=pitch;
    endVstFeedback();
    const token=`${epoch}:${++vstVoiceSerial}`;
    try{
      bridge.vstNoteFeedback(JSON.stringify({op:'on',epoch,token,pitch,channel:note.channel??0,velocity:note.velocity}));
      vstVoice={owner:bridge,epoch,token};
    }catch{}
  }
  const DEFAULT_INSERT_VELOCITY=80,INSERT_VELOCITY_PRESETS=[32,64,80,100,120];
  const SELECTED_NOTE_FILL='#111216';
  const selectionNoteColor=note=>window.AstraVelocity.color(velocityInk(note));
  const SELECTED_PITCH_FONTS=['600 10px AstraSans, system-ui, sans-serif','650 8px AstraSans, system-ui, sans-serif'];
  const ORDINARY_PITCH_FONTS=['700 11px AstraSans, system-ui, sans-serif','700 8px AstraSans, system-ui, sans-serif'];
  const pitchTextWidths=new Map();
  let pitchMeasure=null;
  function selectionPitch(pitch,w,h,ctx=null,ordinary=false){
    if(!Number.isInteger(pitch)||w<10||h<8)return null;
    const text=pitchName(pitch),inset=w>=18?3:2,space=w-inset-1.5;
    for(let density=h>=12?0:1;density<2;density++){
      const font=(ordinary?ORDINARY_PITCH_FONTS:SELECTED_PITCH_FONTS)[density],key=pitch+128*density+(ordinary?256:0);
      let width=pitchTextWidths.get(key);
      if(width===undefined){
        const meter=ctx||(pitchMeasure??=document.createElement('canvas').getContext('2d'));
        if(!meter)return null;
        meter.font=font;
        width=Math.ceil(meter.measureText(text).width);pitchTextWidths.set(key,width);
      }
      if(width<=space||(density&&space>=width*.63))
        return {text,font,width:Math.min(width,space),scale:Math.min(1,space/width),inset};
    }
    return null;
  }
  function drawSelectionPitch(ctx,label,x,y,h){
    ctx.font=label.font;
    ctx.fillText(label.text,x+label.inset,y+h/2,label.width);
  }
  const ordinaryPitchColors=new Map();
  // One pitch ink everywhere: same white as the selected painter (ctx.fillStyle='#fff'),
  // for ordinary, moved and pending labels alike. No stroke, no outline, no halo.
  const PITCH_INK='#fff';
  // Legacy names kept so any other reader still resolves; both are the same white ink now.
  const DARK_PITCH={fill:PITCH_INK,backing:null},LIGHT_PITCH=DARK_PITCH;
  // Pale fills only: a flat translucent chip directly behind the glyphs, just dark enough
  // that white keeps roughly 3:1 on the composite. Mid/dark fills get no chip at all.
  // Set PITCH_TARGET_Y=1 to disable the chip entirely (plain white everywhere).
  const PITCH_BACKING_RGB='16,22,48',PITCH_TARGET_Y=.28,PITCH_BACKING_MAX=.45;
  function ordinaryPitchColor(fill){
    let ink=ordinaryPitchColors.get(fill);
    if(ink)return ink;
    ink=DARK_PITCH;
    if(typeof fill==='string'&&/^#[\da-f]{6}$/i.test(fill)){
      const light=i=>{const c=parseInt(fill.slice(i,i+2),16)/255;return c<=.04045?c/12.92:((c+.055)/1.055)**2.4;};
      const y=.2126*light(1)+.7152*light(3)+.0722*light(5);
      if(y>PITCH_TARGET_Y+.03){
        // Linear-light estimate; canvas blends in sRGB, which darkens more, so this is conservative.
        const a=Math.min(PITCH_BACKING_MAX,Math.ceil((y-PITCH_TARGET_Y)/(y-.008)*20)/20);
        ink={fill:PITCH_INK,backing:'rgba('+PITCH_BACKING_RGB+','+a+')'};
      }
    }
    ordinaryPitchColors.set(fill,ink);return ink;
  }
  function paintOrdinaryPitch(ctx,pitch,x,y,w,h,color){
    if(x<0||w<10||h<8)return;
    const label=selectionPitch(pitch,w,h,ctx,true);
    if(!label)return;
    const ink=ordinaryPitchColor(color),u=x+label.inset,v=y+h/2;
    ctx.save();
    if(ink.backing){
      // Glyph-sized rectangle, kept inside the note box (1px clear of its edges); one fillRect, no clip.
      const gh=Math.min(h-2,label.font===ORDINARY_PITCH_FONTS[0]?12:9),bx=u-1.5,bw=Math.min(label.width+3,x+w-1-bx);
      if(bw>0&&gh>0){ctx.fillStyle=ink.backing;ctx.fillRect(bx,v-gh/2,bw,gh);}
    }
    ctx.fillStyle=ink.fill;ctx.textBaseline='middle';ctx.textAlign='left';ctx.font=label.font;
    ctx.fillText(label.text,u,v,label.width);
    ctx.restore();
  }
  const clampInsertVelocity=value=>Math.max(1,Math.min(127,Math.round(value)));
  let insertVelocity=DEFAULT_INSERT_VELOCITY,velocityReleaseOwner=null;
  try{
    const stored=localStorage.getItem('astra.insertVelocity');
    if(stored!==null&&/^\d+$/.test(stored))insertVelocity=clampInsertVelocity(Number(stored));
  }catch{}
  function setInsertVelocity(raw){
    if(!configEditable())return;
    const field=$('editor-insert-velocity'),value=Number(raw);
    if(String(raw).trim()!==''&&Number.isFinite(value)){
      insertVelocity=clampInsertVelocity(value);
      try{localStorage.setItem('astra.insertVelocity',String(insertVelocity));}catch{}
    }
    if(field)field.value=String(insertVelocity);
  }
  let notePendingPreview=null,noteClick=null,noteDoubleHandledUntil=0,suppressNoteDragDouble=false;
  const noteWaiting=[];
  let noteFlight=null,noteHistory=null,noteMessage='',noteNavigation=null,noteViewSerial=0,noteClosing=false;
  let pendingNoteHost=null;
  const pendingNoteBoxes=new Map(),pencilIntents=new WeakMap();
  let pencilSelection=null;
  function selectionDomainFromTarget(id){
    if(id==='pedal-canvas')selectionDomain=ccLaneHidden?"none":"controls";
    else if(['midi-canvas','roll-scroll','velocity-canvas'].includes(id))selectionDomain="notes";
    else if(['beat-flags-canvas','ruler-canvas','mapping-canvas','opening-canvas'].includes(id))selectionDomain="none";
  }
  let snap="grid", snapEnabled=true, lengthQuantize='link', tool='select', settingsOpen=false, busy=false, notice="", suppressClickUntil=0, cancelledClickPointer=null;
  let clipboard=null,copyRead=null,clipboardVersion=0;
  let selectionFlight=null,selectionViewSerial=0,rangeCopySelection=null;
  let tempoPreview=null, previewSerial=0, previewBusy=false, auditionRate=1, ccChannel=0;
  let eventSnapCache=null, calibrationEdges=null, detectedSnapCache=null, lastSnapTarget=null, selectedMapPoint=null, editOwner=null;
  // Keyboard extension state (Shift+Left/Right) and the note the user last acted on.
  let kbdSel=null, primaryNote=null;
  const boxModeOf=event=>(event.ctrlKey||event.metaKey)?'toggle':event.shiftKey?'add':'replace';
  let edgeFrame=0,edgePointer=null,edgeTime=0;
  let ccEventCache=null,ccSelected=new Set(),ccGhost=[],ccPendingPreview=null,ccRelease=null;
  let phaseTimer=0,phaseRequest=null,rhythmResults=null,qualityResults=null,qualitySelection=null;
  let temporarySnapBypass=false,bypassRevision=null;
  let noteRenderCache=null,previewGridCache=null,selectedMeterEvent=null,rangeDraftScope=null;
  let shortcutsInitialized=false,shortcutSavePending=false,shortcutSaveError="",confirmedShortcuts=null;
  const meterSectionCache=new WeakMap();
  const publication=()=>typeof rollInputVersion==='number'?rollInputVersion:0;
  let waveformCache=null,beatFlagCache=null,selectedBeatFlag=null,lastHover=null,hoveredCC=null;
  let ccInsertHover=null;
  let waveformCollapsed=false,ccLaneHidden=false;
  try { ccLaneHidden=window.localStorage?.getItem('astra-cc-lane-hidden')==='1'; } catch {}
  function toggleCCLane(){
    if(drag||(typeof pedalResize!=='undefined'&&pedalResize))return;
    ccLaneHidden=!ccLaneHidden;
    if(ccLaneHidden){ccSelected.clear();selectedCC=null;if(selectionDomain==='controls')selectionDomain='none';
      const focused=document.activeElement;if(focused?.id==='pedal-divider'||focused?.id==='pedal-canvas'||focused?.closest?.('#pedal-lane'))$('roll-scroll')?.focus?.({preventScroll:true});
    }
    try {window.localStorage?.setItem('astra-cc-lane-hidden',ccLaneHidden?'1':'0');}catch{}
    for(const id of ['pedal-lane','pedal-divider']){const el=$(id);if(el)el.hidden=ccLaneHidden||(id==='pedal-divider'&&$('pedal-lane')?.dataset.folded==='true');}
    syncViewControl('editorCCLaneToggle');
    refreshInfoLine();requestRollDraw();
  }
  let inspectorCollapsed=false;
  const inspectorCovered=()=>['editor-range','editor-structure'].includes(dialog)&&Boolean($('dialog')?.open);
  function syncViewControl(action){if(typeof syncEditorViewControls==='function')syncEditorViewControls(action);}
  // Info line: Cubase-style "first object shown, relative by default, Ctrl/absolute".
  let infoLineHidden=false;
  try { infoLineHidden=window.localStorage?.getItem('astra-info-line-hidden')==='1'; } catch {}
  let hoveredNote=null,infoAbsolute=false,infoMessage='',infoMessageKey='',infoRefocus=null;
  try { waveformCollapsed=window.localStorage?.getItem('astra-reference-waveform-collapsed')==='1'; } catch {}
  try { inspectorCollapsed=window.localStorage?.getItem('astra-inspector-collapsed')==='1'; } catch {}
  let scoreRangeRequest=null,scoreRangeTimer=0,scoreRangeSerial=0;
  let scoreNativeOpened=false,scoreFloatRect=null,scoreFloatGesture=null;
  try{const saved=JSON.parse(window.localStorage?.getItem('astra-score-window')||'null');if(saved&&['x','y','w','h'].every(k=>Number.isFinite(saved[k])))scoreFloatRect=saved;}catch{}
  let scoreRangeZoom=1,tempoReference=null,referenceSerial=0,referenceTimer=0;
  const editor=()=>state.project?.editor;
  const path=()=>editor()?.preview_path || editor()?.path;
  let ccChannelCache=null;
  function ccChannels(){
    const doc=editor(),notes=doc?.notes,controls=doc?.controls,c=ccChannelCache;
    if(c&&c.notes===notes&&c.controls===controls)return c.channels;
    const channels=new Set();
    for(const e of controls||[])if(e.number===64)channels.add(e.channel??0);
    for(const n of notes||[])channels.add(n.channel??0);
    ccChannelCache={notes,controls,channels:[...channels].sort((a,b)=>a-b)};
    return ccChannelCache.channels;
  }
  function stopCCRelease(){window.AstraRhythmMotion?.stopSettle('cc');ccRelease=null;}
  function ccReleaseAlive(r){
    return r&&r.project===stateProjectPath()&&r.channel===ccChannel&&tab==='notes'&&active()&&
      (r.pending?ccPendingAlive(r.pending):sameGestureBase(r.base,noteWriteScope()));
  }
  function sampleCCRelease(r){
    r.rows=r.target.filter(c=>(c.channel??0)===r.channel).map(c=>{
      const a=r.from.get(c.id),e=r.phase;
      return a?{...c,seconds:a.seconds+(c.seconds-a.seconds)*e,value:a.value+(c.value-a.value)*e}:c;
    }).sort((a,b)=>a.seconds-b.seconds);
  }
  function startCCRelease(intent,target,pending){
    stopCCRelease();const motion=window.AstraRhythmMotion;if(!motion?.settle||!intent.from?.length)return;
    const from=new Map(intent.from.map(c=>[c.id,c]));
    if(!target.some(c=>from.has(c.id)&&(Math.abs(c.seconds-from.get(c.id).seconds)>1e-7||Math.abs(c.value-from.get(c.id).value)>.1)))return;
    const r={intent,pending,project:stateProjectPath(),channel:ccChannel,from,target,phase:0,rows:null};
    ccRelease=r;sampleCCRelease(r);
    if(!motion.settle('cc',110,(ease,t)=>{
      if(ccRelease!==r)return;
      if(t>=1||!ccReleaseAlive(r)){stopCCRelease();return;}
      r.phase=ease;sampleCCRelease(r);
    },requestCCDraw))ccRelease=null;
  }
  const stateProjectPath=()=>state.project?.path;
  function ccPaintEvents(){return ccReleaseAlive(ccRelease)?ccRelease.rows:ccEvents();}
  function noteRows(){
    const pending=notePendingPreview,doc=editor();
    if(pending?.held&&!notePaintCurrent(pending)){ // held landing: the publication (or a stale scope) now owns these pixels
      const b=pending.creation&&pendingNoteBoxes.get(pending.creation);if(b)b.retire=true;notePendingPreview=null;
    }
    return pending?.rows&&notePaintCurrent(pending)?pending.rows:doc?.notes||[];
  }
  // Creation and queued notes are DOM overlays only: no temporary IDs in
  // noteRows(), hit testing, velocity editing, audition, or the saved count.
  const noteMotionAllowed=()=>window.AstraRhythmMotion?.enabled?.()!==false&&!window.AstraRhythmMotion?.reduced?.();
  const pencilRasters=new WeakMap();
  function makeNoteBox(host,pending){
    const el=document.createElement('span');el.setAttribute('aria-hidden','true');
    if(pending)el.dataset.pendingNote='true';else el.id='editor-note-creation';
    el.style.cssText='position:absolute;pointer-events:none;z-index:3;';
    const raster=document.createElement('canvas');raster.style.cssText='display:block;width:100%;height:100%;';el.appendChild(raster);
    host.appendChild(el);return el;
  }
  function placeNoteBox(el,n,selectedInk=true){
    // The temporary crop uses the same device-pixel origin, selection path,
    // fill and glyphs as the roll. It adds no separate DOM border or motion.
    const {high,row,axis}=rollGeometry,scroll=$('roll-scroll'),offset=scroll?.scrollLeft||0,ratio=canvasRatio();
    const x=axis.x(n.start)-offset,end=axis.x(n.end)-offset,width=rollGeometry.viewport;
    if(end<0||x>width){el.style.display='none';return;}
    const left=Math.max(0,x),w=Math.max(2,Math.min(width,end)-left),y=(high-n.pitch)*row+1,h=row-2;
    if(h<=0){el.style.display='none';return;}
    const px=Math.floor(left*ratio),py=Math.floor(y*ratio);
    const pw=Math.max(1,Math.ceil((left+w)*ratio)-px),ph=Math.max(1,Math.ceil((y+h)*ratio)-py);
    const ox=px/ratio,oy=py/ratio;
    const geometry={display:'block',left:(ox+offset)+'px',top:oy+'px',width:(pw/ratio)+'px',height:(ph/ratio)+'px'};
    for(const [key,value] of Object.entries(geometry))if(el.style[key]!==value)el.style[key]=value;
    const fill=selectionNoteColor(n),name=n.pitch,showName=x>=0;
    const stamp=[px,py,pw,ph,left,w,y,h,ratio,fill,name,showName,selectedInk];
    const old=pencilRasters.get(el);
    if(old?.every((value,i)=>value===stamp[i]))return;
    pencilRasters.set(el,stamp);
    const raster=el.firstElementChild;
    if(raster.width!==pw)raster.width=pw;
    if(raster.height!==ph)raster.height=ph;
    const ctx=raster.getContext('2d');
    ctx.resetTransform();ctx.clearRect(0,0,pw,ph);
    ctx.setTransform(ratio,0,0,ratio,-px,-py);
    if(selectedInk)paintNoteSelectionInk(ctx,draw=>draw(left,y,w,h,fill,true,true,showName?name:null));
    else{
      ctx.fillStyle=fill;ctx.fillRect(left,y,w,h);
      ctx.strokeStyle='rgba(46,56,88,.5)';ctx.lineWidth=1;ctx.strokeRect(left+.5,y+.5,Math.max(1,w-1),Math.max(1,h-1));
      if(w>=5&&h>=7){ctx.fillStyle='rgba(255,255,255,.16)';ctx.fillRect(left,y,w,1);}
      if(showName&&w>=10&&h>=8)paintOrdinaryPitch(ctx,name,left,y,w,h,fill);
    }
  }
  function paintNoteCreation(){
    let layer=$('editor-note-creation');
    const held=drag?.kind==='drawNote',n=held&&Array.isArray(ghost)?ghost[0]:null;
    if(!n){if(layer)layer.style.display='none';return false;}
    const host=$('roll-content');if(!host||!document.createElement||!rollGeometry)return false;
    layer??=makeNoteBox(host,false);
    if(layer.dataset.creationPhase!=='held')layer.dataset.creationPhase='held';
    placeNoteBox(layer,n);
    const value=n.velocity!==drag.note.velocity?String(n.velocity):null;
    if(value!==layer.getAttribute('data-held-velocity')){
      if(value===null)layer.removeAttribute('data-held-velocity');else layer.setAttribute('data-held-velocity',value);
    }
    return true;
  }
  function selectedPencilBox(ticket){return ticket===pencilSelection||Boolean(ticket.ids?.some(id=>selected.has(id)));}
  function paintPendingNotes(painted=false){
    const host=$('roll-content');
    if(host!==pendingNoteHost){pendingNoteBoxes.clear();pendingNoteHost=host;}
    if(!host||!document.createElement||!rollGeometry||rollGeometry.canvas&&rollGeometry.canvas!==$('midi-canvas'))return;
    const f=noteFlight,rows=noteWaiting.slice();
    const p=notePendingPreview;
    if(p?.creation){if(notePaintCurrent(p)||ownNoteLandingPublication(p))rows.unshift(p.creation);else{const b=pendingNoteBoxes.get(p.creation);if(b)b.retire=true;}}
    // Receipt processing owns actual selection. Retain this selected box through
    // an early native publication until that processing can hand it to Canvas.
    if(f?.intent.note&&!f.cancelled)rows.unshift(f.intent);
    const keep=new Set(rows);
    for(const [intent,box] of pendingNoteBoxes)if(!keep.has(intent)){
      if(!box.retire){box.el.remove();pendingNoteBoxes.delete(intent);continue;}
      const ticket=pencilIntents.get(intent);
      if(ticket&&box.landing&&!selectedPencilBox(ticket)){
        box.landing=false;box.el.dataset.landing='false';placeNoteBox(box.el,intent.note,false);
      }
      if(!painted||box.composing)continue;
      // The authoritative canvas just acquired the note. Keep the opaque
      // landing visible until that raster has had a compositing turn.
      box.composing=true;
      requestAnimationFrame(()=>requestAnimationFrame(()=>{
        if(pendingNoteBoxes.get(intent)===box){box.el.remove();pendingNoteBoxes.delete(intent);}
      }));
    }
    for(const intent of rows){
      if(!validSpan(intent.note.start,intent.note.end))continue;
      const ticket=pencilIntents.get(intent);
      const landing=ticket?selectedPencilBox(ticket):intent===f?.intent||intent===p?.creation;
      let box=pendingNoteBoxes.get(intent);
      if(!box){
        box={el:makeNoteBox(host,true),retire:false};pendingNoteBoxes.set(intent,box);
      }
      if(box.landing!==landing){box.landing=landing;box.el.dataset.landing=String(landing);}
      placeNoteBox(box.el,intent.note,landing);
    }
  }
  function ccEvents(){
    const controls=ccVisibleRows();
    if(ccEventCache&&ccEventCache.project===state.project?.path&&ccEventCache.controls===controls&&ccEventCache.channel===ccChannel)return ccEventCache.events;
    const events=(controls||[]).filter(c=>c.number===64&&(c.channel??0)===ccChannel).sort((a,b)=>a.seconds-b.seconds);
    ccEventCache={project:state.project?.path,controls,channel:ccChannel,events};return events;
  }
  let knownEditorView=null,readOnlyOverride=false,modeNotice="";
  function active(){return !readOnlyOverride&&workingView();}
  // The selected working document owns the roll in both modes. Decoder
  // preparation can change or briefly remove its file path without a selection.
  function workingView(){
    const doc=editor(),current=path(),project=state.project?.path,stamp=doc?.edit_stamp;
    if(!doc||!current)return false;
    const owner=typeof bridge==='undefined'?null:bridge,source=doc.source_key??null,midi=state.midi.path;
    const known=knownEditorView;
    const same=known&&known.project===project&&known.owner===owner&&known.source===source&&
      known.stamp?.generation===stamp?.generation;
    if(midi===current){
      if(!same||known.path!==current||known.revision!==doc.revision||known.stamp?.version!==stamp?.version)
        knownEditorView={project,owner,source,path:current,revision:doc.revision,stamp};
      return true;
    }
    const picked=typeof midiSelected==='undefined'?null:midiSelected;
    return Boolean(same&&
      (midi===known.path||!midi&&(picked===current||picked===known.path))&&
      (!picked||picked===current||picked===known.path)&&
      (stamp?stamp.version>=known.stamp.version:doc.revision>=known.revision));
  }
  function workingViewKey(){
    if(!workingView())return null;
    const doc=editor();
    return JSON.stringify([state.project.path,doc.edit_stamp?.generation??null,doc.source_key??null]);
  }

  function canChangeView(){if(busy||window.AstraRhythmTimeline?.hasPending?.()){notice='正在保存拍点操作，请完成后再切换视图。';refresh();return false;}return true;}
  function setReadOnly(value){
    if(busy||window.AstraRhythmTimeline?.hasPending?.()){notice='正在保存拍点操作，请完成后再切换模式。';refresh();return false;}
    if(typeof preserveRollModeViewport==='function')preserveRollModeViewport();
    const savedRange={...range()},unfinished=Boolean(drag||window.AstraRhythmTimeline?.isDragging?.()||(dialog&&dialog.startsWith('editor-')));
    if(value||readOnlyOverride){cancelNoteEntry();leaveSelectionView();}
    abortGesture();if(dialog?.startsWith('editor-'))$('dialog')?.close();
    readOnlyOverride=Boolean(value);selectionDomain="notes";selected.clear();selectedCC=null;ccSelected.clear();
    if(value)window.AstraTransportPanel?.adoptRange?.(savedRange);
    else if(active()&&hasLocators(savedRange)&&JSON.stringify(editor().range)!==JSON.stringify(savedRange))send('setEditorRange',savedRange);
    notice=unfinished?'已取消尚未提交的操作，当前工作稿中的修改保留。':'';modeNotice=notice;
    refresh();if(typeof render==='function')render();return true;
  }
  let afterEditCommand=null;
  // Visible capabilities and native write ordering are separate. A local edit
  // with an exact receipt may defer an existing command, not disable its control.
  const ORDERED_COMMANDS=new Set(['editorScore','editorExport','editorExternalMidiOpen','editorQuantize','editorTransposeApply',
    'editorLengthScale','editorLegato','editorLegatoFreeze','editorFixedLengths','editorDeleteOverlaps','editorVelocityScale','editorCCState','editorMeterEvent','editorMeterAtPlayhead',
    'editorDirectMeter','editorMeterBars','editorDirectRecount','editorShortBar','editorOpening','editorOpeningRestore',
    'editorMdTrial','editorScoreRange','editorScoreEnd','editorClearScoreEnd','editorDetectTrends','editorInfoCommit']);
  const SELECTION_COMMANDS=new Set(['editorQuantize','editorTransposeApply','editorLengthScale','editorLegato','editorLegatoFreeze','editorFixedLengths','editorDeleteOverlaps','editorVelocityScale',
    'editorCCState','editorInfoCommit']);
  const RANGE_COMMANDS=new Set(['editorMeterBars','editorDirectRecount','editorMdTrial','editorScoreRange','editorScoreEnd','editorDetectTrends']);
  const isHistoryCommand=name=>name==='editorUndo'||name==='editorRedo';
  const configEditable=()=>active() && (!state.job.running || state.job.editor_export) && !state.gpu_preflight;
  const editLocked=()=>busy||Boolean(noteFlight);
  const editable=()=>configEditable()&&!editLocked();
  const canOrderCommand=name=>ORDERED_COMMANDS.has(name)||busy&&isHistoryCommand(name);
  function ordinaryWriteCurrent(){
    if(!editOwner?.request)return false;
    const base=editOwner.base,current=noteWriteScope();
    return sameGestureScope(base,current)&&(sameGestureBase(base,current)||current.version>base.version&&current.request===editOwner.request);
  }
  const commandOffered=(name,key)=>configEditable()&&
    (tool==='rangeSelect'&&sidebarNoteActions.has(name)?!editLocked()&&sidebarActionCount(name,key)>0:
      !busy||canOrderCommand(name)&&ordinaryWriteCurrent());
  function historyOffered(name){
    if(!commandOffered(name))return false;
    const undo=name==='editorUndo',structure=window.AstraStructureEditor;
    return structure?.hasSession()?structure.canHistory(undo?'undo':'redo'):Boolean(undo?editor().can_undo:editor().can_redo);
  }
  // The normal busy gate remains in force for edits of existing objects. Only
  // an independent, fully resolved single-note addition enters this lane.
  const noteProtocol=()=>window.AstraNoteEntryProtocol;
  const usesNoteEntry=()=>Boolean(noteProtocol()&&editor()?.edit_stamp&&typeof dispatchNoteInsertion==='function'&&bridge?.editorTransactionChanged?.connect&&typeof bridge.poll==='function');
  function noteHistoryReady(){
    const f=noteFlight;if(!f?.intent.note||f.cancelled||noteHistory||noteNavigation||noteClosing||tab!=='notes'||!active())return false;
    const where=noteProtocol().relation(f.base,noteWriteScope(),f.request);
    return where==='base'||where==='own';
  }
  const noteEntryMode=()=>noteFlight?(noteHistoryReady()?'inserting':'waiting'):'idle';
  function noteWriteScope(){
    const p=state.project,d=p?.editor,s=d?.edit_stamp;
    return s&&{owner:bridge,project:p.path,source:p.source,sourceKey:d.source_key??null,exportPath:p.export,input:state.input_midi?.token,audio:state.audio?.path,
      generation:s.generation,version:s.version,request:s.request,revision:d.revision};
  }
  const sameNoteScope=(a,b)=>noteProtocol()?.sameScope(a,b)===true;
  function canDrawNote(){
    if(!usesNoteEntry())return editable();
    return tab==='notes'&&active()&&Boolean(editor()?.edit_stamp)&&!noteClosing&&!noteNavigation&&!state.project_opening?.pending&&
      !state.job.running&&!state.gpu_preflight&&!busy&&!noteHistory&&(!noteFlight||noteHistoryReady())&&!window.AstraRhythmTimeline?.hasPending?.();
  }
  function noteEntryStatus(){
    return {pending:noteWaiting.length+(noteFlight?.intent.note&&!noteFlight.cancelled?1:0),request:noteFlight?.request??null,
      phase:noteHistory||noteFlight&&!noteFlight.intent.note?'history':noteFlight?.cancelled?'departed':noteFlight?.receipt?.status==='committed'?'publication':noteFlight?'saving':'idle',message:noteMessage};
  }
  function paintNoteEntryStatus(){
    const el=$('note-write-status');if(!el)return;
    const s=noteEntryStatus(),parts=[];
    if(s.message)parts.push(s.message);
    const text=parts.length?' · '+parts.join(' · '):'';
    if(el.textContent!==text)el.textContent=text;
    if(el.hidden===Boolean(text))el.hidden=!text;
    if(el.dataset.phase!==s.phase)el.dataset.phase=s.phase;
  }
  function noteFeedback(controls=false){
    paintPendingNotes();paintNoteEntryStatus();
    if(controls&&!noteClosing&&typeof scheduleEditorRender==='function')scheduleEditorRender();
  }
  function deferredTarget(name,key){
    if(name==='editorMeterEvent'){
      const m=editor()?.tempo?.time_signatures?.[Number(key)];
      return m?JSON.stringify([m.beat,m.numerator,m.denominator,m.displayNumerator,m.free,m.smart_measure?.owner,m.silence]):null;
    }
    if(SELECTION_COMMANDS.has(name))return JSON.stringify([infoKey(),rulerMode,qState,tools,lengthQuantize,infoAbsolute]);
    if(RANGE_COMMANDS.has(name)){const r=range();return JSON.stringify([r.start,r.end]);}
    if(['editorMeterAtPlayhead','editorDirectMeter','editorOpening','editorShortBar'].includes(name)){
      const chosen=window.AstraRhythmTimeline?.selectionInfo?.(),r=range();
      return JSON.stringify([chosen?.kind,chosen?.seconds,selectedBeatFlag?.seconds,r.start,r.end,state.midi.position]);
    }
    return '';
  }
  function cancelEditCommand(reason=''){
    if(!afterEditCommand)return;
    const command=afterEditCommand;afterEditCommand=null;
    if(reason||noteMessage===command.message){noteMessage=reason;paintNoteEntryStatus();}
  }
  function queueEditCommand(name,key,info=null){
    const scope=noteWriteScope(),target=deferredTarget(name,key);
    if(noteFlight?.readLimit||!commandOffered(name)||!scope||target===null||noteClosing||noteNavigation||state.project_opening?.pending){
      noteMessage='当前编辑无法安全确认，未执行此操作。';paintNoteEntryStatus();return false;
    }
    if(afterEditCommand){
      if(afterEditCommand.name!==name||afterEditCommand.key!==key){
        noteMessage=afterEditCommand.message='已有操作将在当前编辑确认后执行，请稍后再试。';paintNoteEntryStatus();
      }
      return false;
    }
    const message=name==='editorScore'?'将在当前编辑确认后生成乐谱。':name==='editorExport'?'将在当前编辑确认后打开导出。':'等待当前编辑确认后执行';
    afterEditCommand={name,key,message,info,scope,target,view:noteViewSerial,barrier:busy?editOwner:null};
    noteMessage=message;paintNoteEntryStatus();return true;
  }
  function flushEditCommand(){
    if(!afterEditCommand||afterEditCommand.barrier||noteFlight||noteWaiting.length||busy||queuedRange)return;
    const command=afterEditCommand,scope=noteWriteScope();
    if(noteClosing||noteNavigation||state.project_opening?.pending||tab!=='notes'||!configEditable()||
       command.view!==noteViewSerial||!sameGestureScope(command.scope,scope)||drag||
       deferredTarget(command.name,command.key)!==command.target){
      cancelEditCommand('编辑目标或视图已改变，已取消等待中的操作。');return;
    }
    if(isHistoryCommand(command.name)&&!historyOffered(command.name)){
      cancelEditCommand('已没有可以撤销或重做的操作。');return;
    }
    cancelEditCommand();
    if(command.name==='editorInfoCommit'){
      const pending=command.info;
      if(infoKey()===pending.selection&&rulerMode===pending.axis)infoCommit(pending.input,pending.absolute);
      return;
    }
    action(command.name,command.key);
  }
  function cancelNoteEntry(reason='编辑视图已改变',forget=false,restorePencil=false){
    endVstFeedback(true);
    cancelEditCommand();
    const pencil=pencilSelection;
    ++noteViewSerial;pencilSelection=null;
    if(pencil){
      cancelPencilInfo(pencil);
      let anchor=pencil;
      while(restorePencil&&anchor.prior&&!anchor.prior.ids?.length)anchor=anchor.prior;
      const restore=restorePencil&&!selected.size?
        (anchor.prior?.ids||anchor.restore).filter(id=>noteRenderIndex().byId.has(id)):[];
      if(restore.length)selectPublishedNotes(restore);else refreshInfoLine();
    }
    const f=noteFlight,count=noteWaiting.length,live=drag?.kind==='drawNote';
    if(!f&&!count&&!live&&!pendingNoteBoxes.size)return;
    if(f?.cancelled&&!count&&!live&&!forget)return;
    noteWaiting.length=0;noteHistory=null;
    if(f){f.cancelled=true;if(queuedRange){queuedRange=null;pendingRangeView=null;}if(forget)noteFlight=null;}
    for(const box of pendingNoteBoxes.values())box.el.remove();pendingNoteBoxes.clear();
    if(live)abortGesture();
    noteMessage=reason+(count?`；已取消 ${count} 个尚未发送的音符`:'')+(f&&!f.receipt?'；已发送的保存仍可能完成':'');
    const announcement=$('announcement');if(announcement&&!noteClosing)announcement.textContent=noteMessage;
    noteFeedback(true);
  }
  function suspendNoteEntry(){
    const token={};noteNavigation=token;cancelNoteEntry();cancelCCContinuation();leaveSelectionView();
    return ()=>{if(noteNavigation===token)noteNavigation=null;};
  }
  function startNoteIntent(intent,base){
    const f=noteFlight={intent,base,request:nextEditorRequest(),sent:performance.now(),receipt:null,readAt:-Infinity,reads:0};
    noteFeedback(true);
    try{dispatchNoteInsertion(f);}catch(error){cancelNoteEntry(`无法确认发送：${error.message}`,true);}
  }
  function freezeIntent(value){
    if(value&&typeof value==='object'&&!Object.isFrozen(value)){Object.values(value).forEach(freezeIntent);Object.freeze(value);}
    return value;
  }
  function selectPublishedNotes(ids){
    if(!Array.isArray(ids)||!ids.length||ids.some(id=>typeof id!=='string'||!id))return;
    const lookup=noteRenderIndex().byId;
    if(ids.some(id=>!lookup.has(id)))return;
    const old=[...selected].map(id=>lookup.get(id)?.note).filter(Boolean);
    clearRangeObjects();
    selected=new Set(ids);primaryNote=ids[0];selectionDomain='notes';
    window.AstraRhythmTimeline?.clearSelection?.();
    refresh(false);
    const rows=old.concat(ids.map(id=>lookup.get(id).note));
    if(typeof requestNotePatchRows==='function')requestNotePatchRows(rows);else requestRollDraw();
  }
  function beginPencilSelection(note){
    const restore=[...selected],prior=restore.length?null:pencilSelection;
    const ticket=pencilSelection={note,restore,prior,view:noteViewSerial,project:stateProjectPath(),midi:state.midi.path};
    if(restore.length){
      const index=noteRenderIndex().byId;
      const rows=restore.map(id=>index.get(id)?.note).filter(Boolean);
      selected=new Set();primaryNote=null;kbdSel=null;
      refresh(false);
      if(typeof requestNotePatchRows==='function')requestNotePatchRows(rows);else requestRollDraw();
    }else refreshInfoLine();
    paintPendingNotes();
    return ticket;
  }
  function finishPencilSelection(ticket,ids){
    if(!ticket||ticket.done)return;
    cancelPencilInfo(ticket);ticket.done=true;ticket.ids=ids;
    if(ticket!==pencilSelection)return;
    pencilSelection=null;
    if(ticket.view!==noteViewSerial||ticket.project!==stateProjectPath()||ticket.midi!==state.midi.path||
       tab!=='notes'||!active()||!['select','draw'].includes(tool)||selected.size){refreshInfoLine();return;}
    if(ids?.length){selectPublishedNotes(ids);return;}
    let old=ticket;
    while(old.prior?.done&&!old.prior.ids?.length)old=old.prior;
    if(old.prior&&!old.prior.done){pencilSelection=old.prior;refreshInfoLine();return;}
    const lookup=noteRenderIndex().byId;
    const restore=(old.prior?.ids||old.restore).filter(id=>lookup.has(id));
    if(restore.length)selectPublishedNotes(restore);else refreshInfoLine();
  }
  function submitDrawNote(note,operation,ticket,heldElement){
    if(!canDrawNote()||window.AstraStructureEditor?.guardAction('editProject')){
      heldElement?.remove();finishPencilSelection(ticket);
      noteMessage='当前视图已改变，本次音符未写入。';paintNoteEntryStatus();return;
    }
    const queued=usesNoteEntry(),limit=queued?noteProtocol().limit:0;
    if(queued&&noteEntryStatus().pending>=limit){
      heldElement?.remove();finishPencilSelection(ticket);
      noteMessage=`待确认音符已达 ${limit} 个；本次未写入，请稍候。`;paintNoteEntryStatus();return;
    }
    // A final pointer sample can change length/velocity in the release turn.
    // Flush only that changed Info Line before handing this same ticket to the queue.
    if(ticket?.infoFrame){cancelPencilInfo(ticket);paintPencilInfo(ticket);}
    const intent=freezeIntent({action:'editProject',note:{...note},operation});
    pencilIntents.set(intent,ticket);
    const host=$('roll-content');
    if(heldElement&&host&&heldElement.parentNode===host){
      if(host!==pendingNoteHost){pendingNoteBoxes.clear();pendingNoteHost=host;}
      heldElement.dataset.pendingNote='true';heldElement.dataset.landing='true';
      delete heldElement.dataset.creationPhase;
      heldElement.removeAttribute('data-held-velocity');
      pendingNoteBoxes.set(intent,{el:heldElement,landing:true,retire:false});
    }else heldElement?.remove();
    if(!queued){
      if(!edit(operation,null,{creation:intent})){
        pendingNoteBoxes.get(intent)?.el.remove();pendingNoteBoxes.delete(intent);finishPencilSelection(ticket);
      }
      return;
    }
    if(noteFlight){noteWaiting.push(intent);noteFeedback();}
    else{noteMessage='';startNoteIntent(intent,noteWriteScope());}
  }
  function deferNoteHistory(name){
    if(!noteFlight)return false;
    if(!noteHistoryReady())return true;
    const commandCancelled=Boolean(afterEditCommand);cancelEditCommand();
    const count=noteWaiting.length;noteWaiting.length=0;noteHistory=name;
    if(queuedRange){queuedRange=null;pendingRangeView=null;}
    if(drag?.kind==='drawNote')abortGesture();
    noteMessage=count?`已取消 ${count} 个尚未发送的音符`:'';
    if(commandCancelled)noteMessage+=(noteMessage?'；':'')+'已取消等待中的操作。';
    noteFeedback(true);return true;
  }
  function syncNoteEntry(){
    const f=noteFlight;if(!f)return;
    const v=noteWriteScope(),r=f.receipt,protocol=noteProtocol();
    const where=protocol.relation(f.base,v,f.request);
    if(where==='departed'){cancelNoteEntry('工程或音符来源已改变',true);return;}
    if(where==='changed'){cancelNoteEntry('保存版本或撤销历史已改变',true);return;}
    if(!f.cancelled&&(tab!=='notes'||!active()||noteClosing||noteNavigation||state.project_opening?.pending||state.job.running||state.gpu_preflight))cancelNoteEntry();
    // A held creation can follow our own insert publication, not an unrelated
    // revision or a newly selected rendition. Its timing policy stays frozen.
    if(where==='own'&&!f.cancelled&&f.intent.note&&protocol.sameBase(drag?.noteScope,f.base)){
      drag.noteScope=v;if(temporarySnapBypass)bypassRevision=v.revision;
    }
    const outcome=protocol.outcome(f,where,v,editor()?.notes);
    if(outcome==='wait'){paintNoteEntryStatus();return;}
    if(outcome==='release'){cancelEditCommand();noteFlight=null;noteFeedback(true);return;}
    const saved=outcome==='saved';
    if(!saved)cancelEditCommand();
    if(!saved&&outcome!=='failed-at-base'&&outcome!=='noop'){
      cancelNoteEntry(r.warning||r.error||'无法核对保存的音符与当前工程；请检查结果',true,where==='base'&&r.status==='failed');return;
    }
    const box=pendingNoteBoxes.get(f.intent);if(saved&&box)box.retire=true; // hand off on the base paint, not one frame earlier
    const history=noteHistory;noteHistory=null;noteFlight=null;
    if(!saved){finishPencilSelection(pencilIntents.get(f.intent));}
    if(!saved)noteMessage=(r.error||gestureWarning(r)||(outcome==='noop'?'本次未新增音符。':'保存失败；本次音符未写入。'))+(history?'；未执行撤销 / 重做。':'');
    else{
      if(f.intent.note){
        const ticket=pencilIntents.get(f.intent);
        if(ticket)finishPencilSelection(ticket,r.added_ids);else selectPublishedNotes(r.added_ids);
        if(!selected.has(r.added_ids?.[0]))requestRollDraw();
      }
      if(r.audition_warning)noteMessage=r.audition_warning;
    }
    if(history&&saved&&canDrawNote()){
      const undo=history==='editorUndo';
      if(!window.AstraStructureEditor?.history?.(undo?'undo':'redo')){
        if(undo?editor().can_undo:editor().can_redo)startNoteIntent({action:undo?'undoProject':'redoProject'},v);
        else noteMessage+=(noteMessage?'；':'')+(undo?'没有可撤销的操作。':'没有可重做的操作。');
      }
    }else if(noteWaiting.length){
      if(canDrawNote())startNoteIntent(noteWaiting.shift(),v);
      else{cancelNoteEntry('当前编辑视图已不可写',true);return;}
    }
    noteFeedback(!noteFlight);
    if(!noteFlight){flushQueuedRange();flushEditCommand();}
  }
  // A capability for this exact local note request, not a general rebase permit.
  function rhythmNoteContinuation(){
    const f=noteFlight,protocol=noteProtocol();
    if(!f?.intent.note||f.cancelled||!protocol)return null;
    return ()=>{
      const v=noteWriteScope(),where=protocol.relation(f.base,v,f.request);
      if(where!=='own')return 'unsafe';
      const outcome=protocol.outcome(f,where,v,editor()?.notes);
      return outcome==='saved'?'saved':outcome==='wait'?'wait':'unsafe';
    };
  }
  function noteReceipt(event){
    const f=noteFlight;
    if(!f||event.request!==f.request||event.project!==f.base.project||event.action!==f.intent.action)return false;
    if(!f.receipt){f.receipt=event;f.receiptAt=performance.now();}
    syncNoteEntry();return true;
  }
  function recoverNoteEntry(observed,anotherRead=false){
    syncNoteEntry(); // lightweight polls can also announce a job or view change
    const f=noteFlight,now=performance.now();
    if(!f||noteClosing||noteNavigation||anotherRead)return;
    const protocol=noteProtocol(),read=protocol.recovery(f,observed,now);
    if(read==='limit'){
      if(afterEditCommand){afterEditCommand=null;f.readLimit=true;noteMessage='输入确认未完成，已取消等待中的操作。';paintNoteEntryStatus();return;}
      if(!f.readLimit){f.readLimit=true;noteMessage='输入确认尚未到达；不会重发。可继续等待，或重新打开工程核对。';paintNoteEntryStatus();}
      return;
    }
    if(read!=='read')return;
    const v=noteWriteScope(),serial=noteViewSerial,midi=state.midi.path;
    if(!sameNoteScope(f.base,v)||!v.owner?.snapshot)return;
    f.reading=true;f.readAt=now;f.reads++;
    try{v.owner.snapshot(raw=>{
      f.reading=false;
      if(noteFlight!==f||serial!==noteViewSerial||noteClosing||midi!==state.midi.path||!protocol.sameBase(v,noteWriteScope()))return;
      acceptSnapshot(raw,next=>{
        const p=next.project,d=p?.editor,s=d?.edit_stamp;
        return p?.path===v.project&&p.source===v.source&&p.export===v.exportPath&&(d?.source_key??null)===v.sourceKey&&
          next.input_midi?.token===v.input&&next.audio?.path===v.audio&&s?.generation===v.generation&&s.version>=v.version&&
          (next.midi?.path===midi||!f.cancelled&&active()&&next.midi?.path===(d.preview_path||d.path));
      });
    });}catch{f.reading=false;}
  }
  const duration=()=>workingView()?(editor()?.duration||1):(state.midi.duration||state.audio?.duration||1);
  const range=()=>drag?.kind==='range' ? drag.range : !active() ? (window.AstraTransportPanel?.range?.()||{start:0,end:0,loop:false}) : pendingRangeView && pendingRangeView.project===state.project?.path?pendingRangeView.range:editor()?.range || {start:0,end:0,loop:false};
  // Locators retain L/R identity. Only forward locators form an editing scope.
  const localScope=()=>hasRange()?{start:range().start,end:range().end}:null;
  const hasRange=()=>range().end>range().start+.00001;
  const hasLocators=(r=range())=>Number.isFinite(r.start)&&Number.isFinite(r.end)&&Math.min(r.start,r.end)>=0&&Math.max(r.start,r.end)<=duration()+1e-6;
  const fmt=x=>Number(x||0).toFixed(3);
  function syncScoreNative(){
    if(scoreNativeOpened&&typeof bridge!=='undefined'&&typeof bridge.syncScoreRangeWindow==='function'&&state.project?.path&&editor())bridge.syncScoreRangeWindow(JSON.stringify({project:state.project.path,base_revision:editor().revision,...range()}));
  }
  function sync(){
    syncScoreNative();
    window.AstraStructureEditor?.sync();
    syncNoteEntry();
    const key=state.project?.path || "",abandonedCC=key!==projectKey&&Boolean(ccPendingPreview?.queue?.length||drag?.continuation);
    if(qualityResults&&(qualityResults.project!==key||qualityResults.revision!==editor()?.revision)){qualityResults=null;qualitySelection=null;if($('editor-quality-ranges'))$('editor-quality-ranges').textContent='工程已更新，请重新检查当前拍点。';}
    if(scoreRangeRequest&&!scoreRangeAlive(scoreRangeRequest)){stopScoreRange();if(dialog==='editor-score-range')scoreRangeMessage('预览范围已改变 · 请刷新',true);}
    else if(scoreRangeRequest?.autoUpdating&&scoreRangeRequest.updateRevision!==editor()?.revision){scheduleScoreRangeUpdate(scoreRangeRequest);}
    else if(scoreRangeRequest?.token&&!scoreRangeRequest.pending&&scoreRangeRequest.validatedRevision!==editor()?.revision){scoreRangeRequest.pending=true;scoreRangeMessage('正在核对这段谱面…');pollScoreRange(scoreRangeRequest);}
    if(temporarySnapBypass&&(key!==projectKey||!state.midi.path||tab!=='notes'||bypassRevision!==editor()?.revision&&!drag?.continuation))setSnapBypass(false,false);
    if(phaseRequest&&(key!==phaseRequest.project||phaseRequest.revision!==editor()?.revision)){invalidatePreview();if($('editor-dialog-error'))$('editor-dialog-error').textContent='工程已更新，旧复核候选已失效，请重新预览。';}
    if(ccPendingPreview&&!ccPendingAlive(ccPendingPreview))cancelCCContinuation();
    if(ccRelease&&!ccReleaseAlive(ccRelease))stopCCRelease();
    if(drag&&!gestureAlive(drag))abortGesture();
    if(afterEditCommand&&!sameGestureScope(afterEditCommand.scope,noteWriteScope()))cancelEditCommand('编辑视图已改变，已取消等待中的操作。');
    if(key!==projectKey){ccPendingPreview=null;ccEventCache=null;stopCCRelease();selectionDomain="notes";readOnlyOverride=false;knownEditorView=null;modeNotice='';if(projectKey&&['editor-range','editor-score-range','editor-structure'].includes(dialog))$('dialog')?.close();projectKey=key;editScope=null;rebarBoundaryCache=null;selectedBeatFlag=null;beatFlagCache=null;selected.clear();selectedCC=null;ccSelected.clear();drag=ghost=null;notice="";busy=false;editOwner=null;tempoPreview=null;previewSerial++;eventSnapCache=calibrationEdges=detectedSnapCache=null;lastSnapTarget=null;selectedMapPoint=null;snapEnabled=true;if(snap==='detected')snap='grid';}
    if(abandonedCC){notice='已切换工程，未提交的后续踏板拖动已取消；原工程已发出的保存不受影响。';modeNotice=notice;}
    if(editor()){
      if(selectedBeatFlag&&selectedBeatFlag.revision!==editor().revision){const seconds=beatToSeconds(selectedBeatFlag.beat);selectedBeatFlag={...selectedBeatFlag,seconds,revision:editor().revision};}
      if(dialog==='editor-range'&&rangeDraftScope&&!['meter_event','rhythm_candidate','move_beat'].includes($('editor-operation')?.value)){
        const r=editor().range||{};if(r.start!==rangeDraftScope.start||r.end!==rangeDraftScope.end){invalidatePreview();rangeDraftScope={start:r.start,end:r.end};explainOperation();previewMessage('定位范围已改变，请重新预览。');}
      }
      if(selectedMapPoint&&selectedMapPoint.revision!==editor().revision){selectedMapPoint=null;const field=$('editor-map-point');if(field)field.value='';updateMapPointInfo();if($('editor-map-source'))$('editor-map-source').textContent='映射已更新，请重新选择当前映射点。';}
      if(Number.isFinite(editor().audition_rate))auditionRate=editor().audition_rate;
      if(tempoPreview && tempoPreview.revision!==editor().revision){tempoPreview=null;previewSerial++;previewMessage('编辑版本已改变，旧预览已失效。');const btn=document.querySelector('[data-do=editorApplyRange]');if(btn)btn.disabled=true;}
      const channels=ccChannels();if(channels.length&&!channels.includes(ccChannel))ccChannel=channels[0];
      if(selected.size){const ids=new Set(editor().notes.map(n=>n.id));selected=new Set([...selected].filter(id=>ids.has(id)));}
      const ccSelectionRows=ccVisibleRows();
      const ccSelectionIds=new Set(ccSelectionRows.map(c=>c.id));
      ccSelected=new Set([...ccSelected].filter(id=>ccSelectionIds.has(id)));
      if(selectedCC && !ccSelected.has(selectedCC))selectedCC=[...ccSelected][0]||null;
    }
    window.AstraRhythmTimeline?.syncBeat?.();
  }
  function flushQueuedRange(){
    if(editLocked())return;
    const queued=queuedRange;queuedRange=null;
    if(queued&&queued.project===state.project?.path){
      const command=afterEditCommand;
      send('setEditorRange',queued.payload);
      if(command&&afterEditCommand===command){
        if(editOwner?.base&&editOwner.request)command.barrier=editOwner;
        else cancelEditCommand('定位范围未确认，已取消等待中的操作。');
      }
    }
  }
  function send(action,payload={},done=null){
    if(action==='setEditorRange'&&window.AstraTransportPanel?.readonlyCapable()){try{window.AstraTransportPanel.setRange(payload);requestRollDraw();}catch(error){notice=error.message;window.AstraTransportPanel.error('editor-music-end',notice);}return;}
    if(action==='setEditorRange'&&editor())pendingRangeView={project:state.project?.path,range:{...payload}};
    if(action==='setEditorRange'&&editLocked()){queuedRange={project:state.project?.path,payload:{...payload}};return;}
    if(!editor() || editLocked() || window.AstraStructureEditor?.guardAction(action))return;
    busy=true;notice="";
    // Busy is enforced by the command gate, not a brief disable/remount cycle.
    const pedal=action==='editProject'&&isCCEdit(payload.operation);
    if(pedal)requestCCDraw();else requestRollDraw();
    const key=state.project?.path,revision=editor().revision,sentRange=action==='setEditorRange'?pendingRangeView:null;
    const op=payload.operation,beat=op?.type==='rhythm_intent'&&['move_beat','move_beats','add_beat','delete_beats','move_meter'].includes(op.action);
    // Only ordinary content/history edits on the receipt-backed bridge can host
    // the deferred-command slot. Structural drafts keep their existing lock.
    const content=action==='editProject'&&(pedal||op?.type==='notes'||op?.type==='note_semantic');
    const base=(content||['undoProject','redoProject','setEditorRange'].includes(action))&&bridge?.editorTransactionChanged?.connect?noteWriteScope():null;
    const request=payload.editor_request??(base?nextEditorRequest():null);
    const owner=editOwner={base,request,previous:payload.editor_after};
    try{dispatch(action,{base_revision:revision,...payload,...(request?{editor_request:request}:{})},receipt=>{
      if(editOwner!==owner||key!==state.project?.path)return;
      const command=afterEditCommand,upstream=command?.barrier===owner,installed=noteWriteScope();
      const confirmed=upstream&&sameGestureScope(owner.base,installed)&&receipt?.status==='committed'&&receipt.applied&&
        !receipt.warning&&!receipt.error&&!receipt.cancelled&&!gestureWarning(receipt)&&receipt.action===action&&receipt.project===key&&
        receipt.request===owner.request&&installed.request===owner.request&&installed.version>owner.base.version&&receipt.revision===installed.revision;
      if(upstream&&!confirmed)cancelEditCommand('前一步编辑未确认，已取消等待中的操作。');
      editOwner=null;busy=false;
      if(sentRange&&pendingRangeView===sentRange&&!queuedRange)pendingRangeView=null;
      const stale=beat&&receipt&&(receipt.project&&receipt.project!==key||
        (receipt.status==='failed'?revision!==editor()?.revision:receipt.revision!==undefined&&receipt.revision!==editor()?.revision));
      if(!stale)notice=receipt?(receipt.error||receipt.warning||gestureWarning(receipt)||(receipt.status==='failed'?'本次编辑未应用。':'')):(state.job.error||"");
      if(done){try{done(stale?'':notice,receipt);}catch(error){if(upstream)cancelEditCommand('前一步编辑未完成，已取消等待中的操作。');console.warn('edit completion handler failed',error);}}
      if(confirmed&&afterEditCommand===command){
        // A queued CC drag may claim the same writer during done(). Follow
        // only that exact successor; an unrelated write cannot rebase a command.
        if(!editOwner)command.barrier=null;
        else if(editOwner.previous===owner.request&&sameGestureScope(owner.base,editOwner.base))command.barrier=editOwner;
        else cancelEditCommand('编辑顺序已改变，已取消等待中的操作。');
      }
      sync();refresh(!pedal);if(pedal)requestCCDraw();
      flushQueuedRange();flushEditCommand();
    });}catch(error){
      if(editOwner===owner&&(base||op?.type==='selection_range'||op?.type==='rhythm_intent'&&['delete_beats','move_meter'].includes(op.action))){
        if(afterEditCommand?.barrier===owner)cancelEditCommand('无法发送前一步编辑，已取消等待中的操作。');
        editOwner=null;busy=false;
      }
      throw error;
    }
  }
  // Match MIDI PPQ=9600 and Python round (ties to even), not pixel distances.
  const ccRound=n=>{const f=Math.floor(n),r=n-f;return r===.5?f+(f%2):Math.round(n);};
  const ccTickKey=c=>`${c.channel??0}:${ccRound(secondsToBeat(c.seconds)*9600)}`;
  function ccResolve(operation,sourceRows=editor().controls){
    if(operation?.type!=='controls')return {removed:new Set(),rows:[]};
    const before=sourceRows.filter(c=>c.number===64),deleted=new Set(operation.delete||[]),updates=new Map((operation.updates||[]).map(c=>[c.id,c]));
    const after=before.filter(c=>!deleted.has(c.id)).map(c=>({...c,...updates.get(c.id)}));
    const current=new Map(after.map(c=>[c.id,c])),source=new Map(),incoming=new Set();
    before.forEach((c,index)=>{const k=ccTickKey(c),group=source.get(k)||[];group.push({row:c,index});source.set(k,group);const n=current.get(c.id);if(n&&(n.seconds!==c.seconds||n.value!==c.value))incoming.add(c.id);});
    const units=[];
    for(const group of source.values()){
      const changed=group.filter(x=>incoming.has(x.row.id));if(!changed.length)continue;
      const whole=changed.length===group.length&&new Set(changed.map(x=>ccTickKey(current.get(x.row.id)))).size===1;
      for(const members of whole?[changed]:changed.map(x=>[x]))units.push({rows:members.map(x=>current.get(x.row.id)),rank:[0,Math.max(...members.map(x=>x.row.seconds)),Math.max(...members.map(x=>x.index))]});
    }
    (operation.add||[]).forEach((c,index)=>{const row={...c,id:'__new_cc_'+index};after.push(row);units.push({rows:[row],rank:[1,index,0]});});
    const winners=new Map(),later=(a,b)=>a.some((v,i)=>v!==b[i]&&a.slice(0,i).every((n,j)=>n===b[j])&&v>b[i]);
    for(const unit of units){const key=ccTickKey(unit.rows[0]),old=winners.get(key);if(!old||later(unit.rank,old.rank))winners.set(key,unit);}
    const allowed=new Map([...winners].map(([k,u])=>[k,new Set(u.rows.map(c=>c.id))])),removed=new Set();
    for(const row of after){const ids=allowed.get(ccTickKey(row));if(ids&&!ids.has(row.id))removed.add(row.id);}
    return {removed,rows:after.filter(c=>!removed.has(c.id)),replaced:[...removed].filter(id=>!id.startsWith('__new_cc_')).length};
  }
  const ccCollision=operation=>ccResolve(operation).removed; // Preview targets, never a rejection guard.
  const ccActions=new Set(['cc_insert','cc_sweep','cc_drag']);
  const isCCEdit=op=>op?.type==='controls'||op?.type==='note_semantic'&&ccActions.has(op.command?.action);
  const gestureWarning=r=>r?.semantic_warnings?.some(w=>w.code==='cc_sweep_limit')?'扫绘已达 2048 个 MIDI 刻度；末点在保留范围之外。':
    r?.semantic_warnings?.some(w=>w.code==='note_creation_span')?'音符位置或长度无效，本次未写入。':'';
  const sameGestureScope=(a,b)=>Boolean(a&&b&&(noteProtocol()?.sameScope(a,b)??
    Object.keys(a).every(k=>['version','revision','request'].includes(k)||a[k]===b[k])));
  const sameGestureBase=(a,b)=>sameGestureScope(a,b)&&a.version===b.version&&a.revision===b.revision;
  const scopeWire=b=>b&&{project:b.project,generation:b.generation,version:b.version,source_key:b.sourceKey};
  const notePaintCurrent=p=>p.project===stateProjectPath()&&p.revision===editor()?.revision&&
    (!p.base||sameGestureBase(p.base,noteWriteScope()));
  function ownNoteLandingPublication(p){
    if(!p?.request||p.view!==noteViewSerial||p.midi!==state.midi.path||tab!=='notes'||!active()||noteNavigation||noteClosing)return false;
    const now=noteWriteScope();
    return sameGestureScope(p.base,now)&&now.request===p.request&&now.version>p.base.version;
  }
  function ccPendingAlive(p){
    if(!p||ccPendingPreview!==p||p.project!==stateProjectPath()||p.channel!==ccChannel||tab!=='notes'||
       noteNavigation||noteClosing||state.project_opening?.pending||!active())return false;
    const v=noteWriteScope();
    if(!p.base)return p.revision===editor()?.revision&&p.midiPath===state.midi.path;
    if(!sameGestureScope(p.base,v))return false;
    const own=Boolean(p.request&&v.request===p.request&&v.version>p.base.version);
    return (sameGestureBase(p.base,v)||own)&&(p.midiPath===state.midi.path||own&&state.midi.path===path());
  }
  const ccVisibleRows=()=>ccPendingAlive(ccPendingPreview)?ccPendingPreview.rows:editor()?.controls||[];
  function ccCanContinue(){
    return Boolean(busy&&ccPendingPreview?.request&&ccPendingPreview.base&&ccPendingAlive(ccPendingPreview)&&
      (!state.job.running||state.job.editor_export)&&!state.gpu_preflight&&tool==='select');
  }
  function cancelCCContinuation(){
    const p=ccPendingPreview;
    if(p){p.queue.length=0;if(afterEditCommand?.barrier?.request===p.request)cancelEditCommand('踏板操作已取消，未执行等待中的操作。');}
    ccPendingPreview=null;ccEventCache=null;
    if(['control','drawCC','ccBox'].includes(drag?.kind))abortGesture();
    else if(ccRelease)stopCCRelease();
    if(p)requestCCDraw();
  }
  function ccQueueEdit(intent){
    const p=ccPendingPreview,c=intent.operation.command;
    if(!ccCanContinue()||intent.parent!==p||c?.action!=='cc_drag'||c.copy||c.line_id!=null)return false;
    if(p.queue.length>=32){notice='待确认的拖动已达上限；本次拖动未提交。';refreshInfoLine();return true;}
    p.queue.push(intent);p.rows=ccResolve(intent.paint,p.rows).rows;ccEventCache=null;
    startCCRelease(intent,p.rows,p);requestCCDraw();return true;
  }
  function ccContinueAfter(p,error,r){
    if(ccPendingPreview!==p)return;
    const live=drag?.kind==='control'&&drag.continuation===p?drag:null,v=noteWriteScope();
    const own=ccPendingAlive(p)&&p.base&&v.request===p.request&&v.version>p.base.version;
    ccPendingPreview=null;ccEventCache=null;
    const fail=reason=>{
      if(afterEditCommand?.barrier?.request===p.request)cancelEditCommand('踏板操作未全部确认，已取消等待中的操作。');
      if(live&&drag===live)abortGesture();else if(ccRelease?.pending===p)stopCCRelease();
      if(live||p.queue.length)notice=`后续踏板拖动未提交：${reason}。`;
      requestCCDraw();
    };
    if(!own||r?.status!=='committed'||r.request!==p.request||r.project!==p.project||r.revision!==v.revision||r.warning||r.error||r.cancelled){
      fail(error||r?.warning||'编辑视图或前一步确认已改变');return;
    }
    const actual=new Map(editor().controls.filter(c=>c.number===64).map(c=>[c.id,c]));
    const lane=id=>{const c=actual.get(id);return c&&(c.channel??0)===p.channel?c:null;};
    const preview=r.semantic_preview,command=p.intent.operation.command,adds=r.added_ids||[];
    const selectId=r.changed===false&&preview?.select_id!=null&&lane(preview.select_id)?preview.select_id:null;
    const sources=new Map();
    if(command?.action==='cc_sweep')preview?.add_sources?.forEach((s,i)=>sources.set(s,i));
    if(command?.action==='cc_drag'&&command.copy)for(const t of preview?.targets||[])sources.set(t.id,t.add_index);
    const aliases=new Map();
    (p.intent.paint.add||[]).forEach((_,i)=>{
      const indexed=command?.action==='cc_sweep'||command?.action==='cc_drag'&&command.copy;
      const index=indexed?sources.get(p.intent.sources?.[i]):i;
      const id=index==null?null:adds[index]??(i===0&&!indexed?selectId:null);
      aliases.set('__new_cc_'+i,lane(id)?id:null);
    });
    const ident=id=>aliases.has(id)?aliases.get(id):lane(id)?id:null;
    const mapRows=rows=>(rows||[]).flatMap(c=>{const id=ident(c.id);return id==null?[]:[{...c,id}];});
    const mapIds=ids=>[...new Set([...ids].map(ident).filter(id=>id!=null))];
    ccSelected=new Set(mapIds(ccSelected));selectedCC=ident(selectedCC)??[...ccSelected][0]??null;
    if(selectId!=null&&!live&&!p.queue.length)selectCC(selectId);
    for(const intent of p.queue){
      const op=intent.operation,c=op.command;
      const id=ident(c.line_id??c.grabbed_id);
      if(id==null){fail('目标点已被替换');return;}
      intent.operation={...op,command:c.line_id!=null?{...c,line_id:id}:{...c,grabbed_id:id,ids:mapIds(c.ids)}};
      intent.paint={...intent.paint,updates:mapRows(intent.paint.updates)};
      intent.from=mapRows(intent.from);
    }
    if(live){
      const id=ident(live.cc.id);
      if(id==null){fail('抓住的点已被替换');return;}
      live.cc={...live.cc,id};live.controls=mapRows(live.controls);
      live.originalControls=live.controls.map(c=>actual.get(c.id));
      live.ccToggleOff=ident(live.ccToggleOff);
      if(live.ccRestore)live.ccRestore=new Set(mapIds(live.ccRestore));
      live.base_revision=v.revision;live.midiPath=state.midi.path;live.gestureScope=v;
      live.upstreamRequest=r.request;live.continuation=null;
      live.ccCollisionKey=live.ccCollisionResult=live.ccPreviewRows=live.ccConflicts=null;
      ccGhost=mapRows(ccGhost);ghost=ccGhost.find(c=>c.id===id);
      if(temporarySnapBypass)bypassRevision=v.revision;
    }
    if(p.queue.length){
      const [first,...queue]=p.queue;
      if(!edit(first.operation,null,null,{intent:first,queue,after:r.request,live})){fail('当前视图不允许继续编辑');return;}
    }else if(ccRelease?.pending===p){
      // Keep the animation's frame/ease. Only native ghosts and retained
      // rows are now the presentation basis for another press.
      const settle=ccRelease,from=new Map();
      for(const g of preview?.ghosts||[]){
        const id=g.add_index==null?g.id:adds[g.add_index];if(lane(id))from.set(id,g);
      }
      if(!from.size)stopCCRelease();
      else {Object.assign(settle,{pending:null,base:v,request:r.request,from,target:[...actual.values()]});sampleCCRelease(settle);}
    }
    requestCCDraw();
  }
  function edit(operation,done=null,visual=null,chain=null){
    const pedal=isCCEdit(operation),semantic=operation?.type==='note_semantic';
    const intent=pedal?(chain?.intent||{operation,paint:visual?.paint||(semantic?{type:'controls'}:operation),
      from:visual?.from,sources:visual?.sources,phase:visual?.phase,parent:visual?.parent}):null;
    if(busy&&intent&&ccQueueEdit(intent))return true;
    if(!editable()||window.AstraStructureEditor?.guardAction('editProject'))return false;
    const base=noteWriteScope(),project=stateProjectPath(),revision=editor().revision;
    const result=intent?ccResolve(intent.paint):null;
    const request=intent&&base&&typeof nextEditorRequest==='function'?nextEditorRequest():null;
    const pending=intent?{request,base,project,revision,midiPath:state.midi.path,channel:ccChannel,
      rows:result.rows,intent,queue:chain?.queue||[]}:null;
    if(pending){
      for(const next of pending.queue)pending.rows=ccResolve(next.paint,pending.rows).rows;
      ccPendingPreview=pending;ccEventCache=null;
      if(chain?.live)chain.live.continuation=pending;
      if(!chain)startCCRelease(intent,pending.rows,pending);
      else if(ccRelease){
        ccRelease.pending=pending;ccRelease.target=pending.rows;
        ccRelease.from=new Map((ccRelease.intent.from||[]).map(c=>[c.id,c]));sampleCCRelease(ccRelease);
      }
    }
    const paint=visual?.paint||(operation?.type==='notes'?operation:null);
    const notePending=!pedal&&(paint?.type==='notes'||visual?.creation)?{project,revision,base,creation:visual?.creation,view:noteViewSerial,midi:state.midi.path}:null;
    if(notePending){
      if(paint){
        const removed=new Set(paint.delete||[]),updates=new Map((paint.updates||[]).map(n=>[n.id,n]));
        notePending.rows=editor().notes.filter(n=>!removed.has(n.id)).map(n=>updates.has(n.id)?{...n,...updates.get(n.id)}:n)
          .concat((paint.add||[]).map((n,i)=>({...n,id:'__pending_note_'+i})));
        // Expose the move/copy destination to the ink pass only, never to the authoritative selection.
        const command=operation?.command;
        if(command?.action==='note_drag'){
          notePending.landing=notePending.rows.filter(n=>updates.has(n.id)||n.id.startsWith('__pending_note_'));
          notePending.copy=Boolean(command.copy);notePending.pitchRows=new Set(notePending.landing);
        }
      }
      notePendingPreview=notePending;if(notePending.creation)paintPendingNotes();
    }
    const after=chain?.after??visual?.after;
    const selectAdded=visual?.selectAdded||visual?.creation,selectView=noteViewSerial,midi=state.midi.path;
    const scoped=base&&(pedal||selectAdded||semantic&&['note_create','note_drag','pedals_to_note_length'].includes(operation.command?.action));
    send('editProject',{operation,...(scoped?{editor_scope:scopeWire(base)}:{}),
      ...(request?{editor_request:request}:{}),...(after?{editor_after:after,...(intent?.phase!=null?{cc_settle:intent.phase}:{})}:{})},(error,receipt)=>{
      if(notePending&&notePendingPreview===notePending){
        // A usable commit that arrives before its publication keeps the landing body; noteRows() drops it once editor() moves on.
        if(!error&&!receipt?.error&&!receipt?.cancelled&&receipt?.applied&&receipt.changed&&receipt.status==='committed'&&
           receipt.project===project&&notePaintCurrent(notePending))notePending.held=true;
        else{notePendingPreview=null;noteRenderCache=null;requestRollDraw();}
      }
      if(visual?.creation&&(error||receipt?.error||receipt?.cancelled||!receipt?.applied||!receipt?.changed))
        finishPencilSelection(pencilIntents.get(visual.creation));
      if(pending)ccContinueAfter(pending,error,receipt);
      if(selectAdded&&!error&&!receipt?.error&&!receipt?.warning&&!receipt?.cancelled&&receipt?.applied&&receipt.changed&&
         receipt.status==='committed'&&receipt.project===project&&receipt.revision===editor()?.revision&&
         tab==='notes'&&active()&&!noteNavigation&&!noteClosing&&selectView===noteViewSerial&&midi===state.midi.path){
        const now=noteWriteScope();
        if(sameGestureScope(base,now)&&now.request===receipt.request&&now.version>base.version){
          const ticket=pencilIntents.get(visual?.creation);
          const box=pendingNoteBoxes.get(visual?.creation);
          if(box)box.retire=true;
          if(ticket){finishPencilSelection(ticket,receipt.added_ids);if(!selected.has(receipt.added_ids?.[0]))requestRollDraw();}
          else selectPublishedNotes(receipt.added_ids);
        }
      }
      if(done)done(error,receipt);
    });
    if(notePending&&notePendingPreview===notePending)notePending.request=editOwner?.request;
    return true;
  }
  function semanticOperation(command,{axis,cursor_seconds,clipboard,range_token}={}){
    const context={};
    if(axis)context.axis={mode:axis.mode,width:axis.width,seconds_extent:axis.seconds_extent,beat_extent:axis.beat_extent};
    if(cursor_seconds!==undefined)context.cursor_seconds=cursor_seconds;
    if(clipboard!==undefined)context.clipboard=clipboard;
    if(range_token!==undefined)context.range_token=range_token;
    return {type:'note_semantic',command,context};
  }
  function semanticBase(){
    const d=editor(),p=state.project,s=d?.edit_stamp;
    return {owner:typeof bridge==='undefined'?null:bridge,project:p?.path,source:p?.source,sourceKey:d?.source_key,
      revision:d?.revision,generation:s?.generation,version:s?.version,midi:state.midi.path,view:noteViewSerial};
  }
  function semanticCurrent(base){
    const now=semanticBase();
    return tab==='notes'&&active()&&Object.keys(base).every(k=>base[k]===now[k]);
  }
  function previewSemantic(base,operation){
    return new Promise((resolve,reject)=>{
      if(typeof base.owner?.previewProject!=='function'){reject(new Error('当前运行版本无法读取编辑预览。'));return;}
      const scope=operation.type==='selection_range'?scopeWire(noteWriteScope()):null;
      if(operation.type==='selection_range'&&(!scope||!semanticCurrent(base))){reject(new Error('选区视图已更新。'));return;}
      base.owner.previewProject(JSON.stringify({base_revision:base.revision,operation,...(scope?{editor_scope:scope}:{})}),raw=>{
        try{
          const reply=typeof raw==='string'?JSON.parse(raw):raw,data=reply?.delta??reply;
          if(reply?.error||reply?.ok===false)throw new Error(reply.error||'无法读取编辑预览。');
          if(!data?.semantic_preview||!Array.isArray(data.semantic_warnings))throw new Error('无法读取编辑预览。');
          resolve(data);
        }catch(error){reject(error);}
      });
    });
  }
  // The head owns the interval. Local state here is only capture, paint, and
  // continuations of a particular ordered receipt; it supplies no event rows.
  const selectionSurfaces=new Set(['midi-canvas','pedal-canvas']);
  function shownSelection(){
    if(tool!=='rangeSelect'||tab!=='notes'||noteNavigation||noteClosing||!active())return null;
    const r=editor()?.selection_range;
    if(!r||!(r.end>r.start)||r.revision!==editor().revision)return null;
    return ['cc64','notes_cc64'].includes(r.scope.mode)&&(ccLaneHidden||r.scope.channel!==ccChannel)?null:r;
  }
  const commandRange=()=>selectionDomain==='none'?null:shownSelection();
  // The native interval still owns Range clipping, repeats and undo. At
  // a duplicate's landing only newly emitted IDs take the object ink.
  function rangeCreatedIds(r){
    const c=rangeCopySelection;
    return r&&c?.project===stateProjectPath()&&r.token===c.token&&r.revision===c.revision?c.ids:null;
  }
  const rangeCovers=(scope,id)=>scope.mode==='full'||(id==='midi-canvas'?scope.mode!=='cc64':
    !ccLaneHidden&&scope.mode!=='notes'&&scope.channel===ccChannel);
  function selectionContext(){
    const a=rollAxis();
    return {axis:{mode:a.mode,width:Math.max(1,a.width),seconds_extent:a.duration,beat_extent:a.beatExtent}};
  }
  const selectionOperation=(command,context)=>({type:'selection_range',command,context});
  // Dismissal while a native Range gesture receipt is pending: hide now, chain one
  // native clear against the confirmed token on receipt; hold hidden (no stale
  // rollback) only while that flight or its chained clear is pending on this view/MIDI/owner.
  let rangeDismissal=null,rangeDismissClick=null;
  function rangeHidden(){
    const d=rangeDismissal;
    const f=selectionFlight,now=noteWriteScope();
    return !!d&&!!f&&f===d.flight&&d.view===selectionViewSerial&&d.midi===state.midi.path&&!!now&&sameGestureScope(f.base,now);
  }
  // Presentation-only mask (Canvas/Info): a dismiss or committed clear in flight. Settling,
  // rejecting or losing the view ends the flight and restores the authoritative snapshot.
  function rangeMasked(){
    const f=selectionFlight,now=noteWriteScope();
    return rangeHidden()||!!f&&f.command?.action==='clear'&&f.view===selectionViewSerial&&f.midi===state.midi.path&&!!now&&sameGestureScope(f.base,now);
  }
  function pendingRangeGesture(){
    const f=selectionFlight;
    return tool==='rangeSelect'&&tab==='notes'&&active()&&!noteNavigation&&!noteClosing&&f?.command?.action==='gesture'&&
      f.view===selectionViewSerial&&f.midi===state.midi.path&&sameGestureBase(f.base,noteWriteScope())?f:null;
  }
  function dismissPendingRange(){
    const f=pendingRangeGesture();if(!f||drag)return false;
    if(rangeDismissal?.flight!==f){rangeDismissal={flight:f,view:f.view,midi:f.midi};f.queue.push({action:'clear',context:selectionContext()});}
    requestRollDraw();refresh();if(busy)refreshInfoLine();return true;
  }
  function abortSelectionGesture(){if(drag?.kind==='selectionRange')abortGesture();}
  function leaveSelectionView(){++selectionViewSerial;abortSelectionGesture();if(selectionFlight)selectionFlight.queue.length=0;}
  function clearRangeObjects(){selected.clear();ccSelected.clear();selectedCC=selectedBeatFlag=primaryNote=kbdSel=null;}
  function selectionEdit(command,context=selectionContext(),paint=null,after=null,queue=[]){
    const base=noteWriteScope();
    if(!base||!editable()||noteNavigation||noteClosing||tab!=='notes'||state.project_opening?.pending||
       typeof base.owner?.editorTransactionChanged?.connect!=='function'||window.AstraStructureEditor?.guardAction('editProject'))return false;
    const f=selectionFlight={base,view:selectionViewSerial,request:nextEditorRequest(),queue,paint,command,midi:state.midi.path};
    const cutVersion=command.action==='cut'?++clipboardVersion:null;
    if(cutVersion!==null)copyRead=null;
    try{send('editProject',{operation:selectionOperation(command,context),editor_delta:true,editor_scope:scopeWire(base),
      editor_request:f.request,...(after?{editor_after:after}:{})},(error,r)=>{
      if(selectionFlight!==f)return;
      selectionFlight=null;
      const now=noteWriteScope(),own=sameGestureScope(base,now)&&now.request===f.request&&now.version>base.version&&
        now.revision===r?.revision&&r?.request===f.request&&r?.status==='committed'&&!error&&!r.warning&&!r.cancelled;
      const cutWire=cutVersion!==null&&r?.applied&&!r.warning&&r.request===f.request?r.semantic_preview?.clipboard_json:null;
      if(typeof cutWire==='string'&&cutVersion===clipboardVersion)clipboard={type:'selection_range',wire:cutWire};
      if(!own||f.view!==selectionViewSerial||f.midi!==state.midi.path||tab!=='notes'||!active()||noteNavigation||noteClosing)return;
      if(command.action==='paste'){
        clearRangeObjects();tool='rangeSelect';selectionDomain='notes';
        const scope=editor()?.selection_range?.scope;
        if(scope&&['cc64','notes_cc64'].includes(scope.mode)){
          ccChannel=scope.channel;if(ccLaneHidden)toggleCCLane();
        }
      }
      if(command.action==='info')setInfoMessage('');
      const next=f.queue.shift(),range=commandRange();
      if(command.action==='duplicate'&&r.changed&&r.applied&&range?.token===r.selection_range?.token&&range){
        clearRangeObjects();selectionDomain=range.scope.mode==='cc64'?'controls':'notes';
        rangeCopySelection={project:base.project,token:range.token,revision:range.revision,
          ids:{notes:new Set(r.range_added_ids?.notes||[]),controls:new Set(r.range_added_ids?.controls||[])}};
      }
      if(next?.action==='duplicate'&&range){
        selectionEdit({action:'duplicate',token:range.token},next.context,null,r.request,f.queue);
      }else if(next?.action==='paste'&&typeof cutWire==='string'&&cutVersion===clipboardVersion){
        selectionEdit({action:'paste',at:next.at},{...next.context,clipboard:cutWire},null,r.request);
      }else if(next?.action==='clear'&&rangeDismissal?.flight===f){
        const d=rangeDismissal,cr=editor()?.selection_range;
        if(cr&&cr.end>cr.start&&cr.revision===r.revision&&(!r.selection_range||r.selection_range.token===cr.token)&&
           selectionEdit({action:'clear',token:cr.token},next.context,emptyRangePaint,r.request))d.flight=selectionFlight;
      }
    });}catch(error){if(selectionFlight===f)selectionFlight=null;notice=error.message;refresh();return false;}
    if(command.action==='clear'&&selectionFlight===f)refreshInfoLine();
    return true;
  }
  function rangeKeyContinuation(name){
    const f=selectionFlight,here=noteWriteScope();
    if(!f||tool!=='rangeSelect'||f.view!==selectionViewSerial||f.midi!==state.midi.path||noteNavigation||
       !sameGestureScope(f.base,here)||!active()||(!sameGestureBase(f.base,here)&&here.request!==f.request))return false;
    if(name==='editorDuplicate'){
      if(f.queue.length<16)f.queue.push({action:'duplicate',context:selectionContext()});
      return true;
    }
    if(['editorPaste','editorPasteAtOrigin'].includes(name)&&f.command.action==='cut'){
      if(!f.queue.length)f.queue.push({action:'paste',at:name==='editorPaste'?'cursor':'origin',
        context:{...selectionContext(),cursor_seconds:wireCursor()}});
      return true;
    }
    return false;
  }
  const emptyRangePaint={start:0,end:0,shift:null};
  function selectionHit(c,event,id){
    const r=rangeHidden()?null:shownSelection();if(!r||!rangeCovers(r.scope,id))return {part:'draw'};
    const a=midiX(r.start),b=midiX(r.end),x=c.worldX;
    const left=Math.abs(x-a),right=Math.abs(x-b);
    if(event.shiftKey){
      if(x<=a)return {r,part:'extend_start'};
      if(x>=b)return {r,part:'extend_end'};
      return {r,part:'shift_inside'};
    }
    if(Math.min(left,right)<=5)return {r,part:left<right?'start':'end'};
    return a<x&&x<b?{r,part:event.altKey?'copy_body':'move_body'}:{part:'draw'};
  }
  function rangePreviewClock(view,cfg,press,scope,exclude,detected){
    // Native supplies canonical meter sections; only project pixels into that
    // clock for immediate feedback. Release still sends raw samples to C++.
    const axis=rollAxis(),duration=editor().duration,beat=axis.toBeat,time=axis.toSeconds;
    const bars=view.mode==='bars',extent=bars?view.beat_extent:view.seconds_extent;
    const bound=(x,max)=>Math.max(0,Math.min(max,x)),limit=beat(duration);
    const coord=bars?beat:x=>x,cap=bars?limit:duration;
    const seconds=x=>x===cap?duration:(bars?time(x):x);
    const x=s=>coord(s)/extent*view.width;
    const raw=px=>bound(seconds(bound(px,view.width)/view.width*extent),duration);
    const sections=editor().range_grid_sections??gridSections();
    const pixelsPerBeat=view.width/view.beat_extent;
    let unit=cfg.unit;if(unit==='auto'){unit=1/32;while(unit<64&&unit*pixelsPerBeat<12)unit*=2;}
    const mode=cfg.mode,magnetic=mode==='events'||mode==='grid-events-cursor';
    let notes=[],controls=[];
    if(cfg.enabled&&!press.latched_bypass&&magnetic){
      if(scope.mode!=='cc64')notes=exclude?eventSnapPoints().filter(p=>p.note.start>=exclude.end||p.note.end<=exclude.start):eventSnapPoints();
      if(scope.mode!=='notes'){
        const rows=scope.mode==='full'?editor().controls.slice().sort((a,b)=>a.seconds-b.seconds):ccEvents();
        controls=exclude?rows.filter(p=>p.seconds<exclude.start||p.seconds>=exclude.end):rows;
      }
    }
    const snapAt=(s,sample,ref)=>{
      if(!cfg.enabled||press.latched_bypass||sample.bypass)return s;
      if(magnetic||mode==='detected'||mode==='cursor'){
        const at=x(s);let closest=s,gap=Infinity;
        const offer=v=>{if(v===undefined)return;const g=Math.abs(x(v)-at);if(g<gap||(g===gap&&v<closest)){gap=g;closest=v;}};
        const near=(rows,key)=>{const i=lowerBound(rows,s,key);offer(key?rows[i-1]?.[key]:rows[i-1]);offer(key?rows[i]?.[key]:rows[i]);};
        if(magnetic){near(notes,'seconds');near(controls,'seconds');}
        if(mode==='detected'&&detected?.available)near(detected.points);
        if(mode==='cursor'||mode==='grid-events-cursor'){
          const v=sample.cursor_seconds,g=Math.abs(x(v)-at);
          if(g<gap){gap=g;closest=v;}
        }
        if(gap<=10)return bound(closest,duration);
        if(mode!=='grid-events-cursor')return s;
      }
      const q=beat(s),a=ref===undefined?0:beat(ref);
      const target=mode==='relative'&&ref!==undefined?a+Math.round((q-a)/unit)*unit:gridBeat(q,unit,sections);
      return bound(time(bound(target,limit)),duration);
    };
    const move=(at,source)=>{
      const length=bars?source.length_beat:source.length;
      const where=coord(at),first=bound(where,Math.max(0,cap-length));
      return [first===where?at:bound(seconds(first),duration),bound(seconds(Math.min(cap,first+length)),duration)];
    };
    return {x,raw,snapAt,move};
  }
  function selectionDown(event,c){
    const id=event.target.id,previous=shownSelection(),{r,part}=selectionHit(c,event,id),context=selectionContext();
    const press={...wirePoint(c,event),latched_bypass:temporarySnapBypass,cursor_seconds:wireCursor()};
    const scope=r?.scope??(id==='pedal-canvas'?{mode:'cc64',channel:ccChannel}:{mode:'notes'});
    const cfg=wireSnap(),detected=cfg.enabled&&cfg.mode==='detected'&&detectedBeats(),body=part.endsWith('_body');
    if(detected?.available)context.detected_cache_key=detected.cache_key;
    const clock=rangePreviewClock(context.axis,cfg,press,scope,body?r:null,detected);
    const anchor=part==='draw'||part==='shift_inside'?clock.snapAt(clock.raw(press.x),press):null;
    capture(event,{kind:'selectionRange',pressCoordinate:c,rSource:r,rPrevious:previous,rScope:scope,rContext:context,
      rPress:press,rExtend:Boolean(event.shiftKey),rPart:part,rLiveAlt:part==='move_body',rSnap:cfg,rClock:clock,rAnchor:anchor,rBase:semanticBase(),rView:selectionViewSerial});
    selectionGhost(drag,press);requestRollDraw();
  }
  function selectionGhost(d,point,altKey=false){
    const {rClock:clock,rPress:press,rSource:source,rAnchor:anchor}=d;
    let part=d.rLiveAlt&&altKey?'copy_body':d.rPart,start,end,shift=null;
    const dx=point.x-press.x;
    if(part==='shift_inside'){
      // Keep showing the committed range on press. A click clears it;
      // a deliberate horizontal drag from inside starts a fresh range.
      d.rShiftDraw ||= d.everDragged && dx!==0;
      if(!d.rShiftDraw){
        d.rPaint={start:source.start,end:source.end,scope:d.rScope,source,shift:0,part};
        return;
      }
      part='draw';
    }
    if(part==='draw'){
      const at=dx===0?anchor:clock.snapAt(clock.raw(point.x),point,anchor);
      start=Math.min(anchor,at);end=Math.max(anchor,at);
    }else{
      const extend=part.startsWith('extend_'),left=part==='start'||part==='extend_start';
      const ref=part==='end'||part==='extend_end'?source.end:source.start;
      const raw=extend?clock.raw(point.x):dx===0?ref:clock.raw(clock.x(ref)+dx);
      const at=!extend&&raw===ref?ref:clock.snapAt(raw,point,ref);
      if(part.endsWith('_body')){
        if(at===ref){start=source.start;end=source.end;}else [start,end]=clock.move(at,source);
        shift=clock.x(start)-clock.x(source.start);
      }else{
        const fixed=left?source.end:source.start;
        const moving=extend?(left?Math.min(at,fixed):Math.max(at,fixed)):at;
        start=Math.min(moving,fixed);end=Math.max(moving,fixed);
      }
    }
    d.rPaint={start,end,scope:d.rScope,source,shift,part};
  }
  function forRangeNotes(start,end,visit){
    const rows=noteRenderIndex().entries;
    for(let i=lowerBound(rows,start,'maxEnd');i<rows.length&&rows[i].start<end;++i){
      const n=rows[i].note,a=Math.max(start,n.start),b=Math.min(end,n.end);
      if(b>a)visit(n,a,b);
    }
  }
  function handoffRangeNotes(r){
    if(!r)return;
    // Convert the current native interval to ordinary lane selection only
    // when switching to Object. Ctrl/Meta use that tool's usual toggles.
    clearRangeObjects();
    const {start,end,scope}=r,created=rangeCreatedIds(r);
    if(rangeCovers(scope,'midi-canvas'))forRangeNotes(start,end,n=>{if(!created||created.notes.has(n.id))selected.add(n.id);});
    if(rangeCovers(scope,'pedal-canvas')){
      const rows=ccEvents();
      for(let i=lowerBound(rows,start,'seconds');i<rows.length&&rows[i].seconds<end;++i)
        if(!created||created.controls.has(rows[i].id))ccSelected.add(rows[i].id);
      selectedCC=ccSelected.values().next().value??null;
    }
    primaryNote=selected.values().next().value??null;
    selectionDomain=scope.mode==='cc64'?'controls':'notes';
  }
  // Object and Range use the same note ink. An open side indicates a cut
  // through a note, not selection of its untouched remainder.
  function paintNoteSelectionInk(ctx,append){
    ctx.save();ctx.fillStyle=SELECTED_NOTE_FILL;ctx.lineWidth=1.5;
    const matrix=ctx.getTransform(),rx=Math.abs(matrix.a)||1,ry=Math.abs(matrix.d)||1;
    const paths=new Map(),labels=[];
    append((x,y,w,h,color,left=true,right=true,pitch=null)=>{
      if(w<=0||h<=0)return;
      // Full note body has the same backing pixels over either background.
      // At a range cut round inward, so no selected fill escapes the slice.
      const fx=(left?Math.floor:Math.ceil)(x*rx)/rx,ex=(right?Math.ceil:Math.floor)((x+w)*rx)/rx;
      const fy=Math.floor(y*ry)/ry,ey=Math.ceil((y+h)*ry)/ry;
      if(ex>fx)ctx.fillRect(fx,fy,ex-fx,ey-fy);
      if(w<2||h<=2)return;
      const text=left&&selectionPitch(pitch,w,h,ctx);
      if(text)labels.push([text,x,y,h]);
      const a=x+.75,b=x+w-.75,t=y+.75,z=y+h-.75;
      let path=paths.get(color);
      if(!path){path=new Path2D();paths.set(color,path);}
      if(left&&right){path.rect(a,t,b-a,z-t);return;}
      path.moveTo(a,t);path.lineTo(b,t);
      path.moveTo(a,z);path.lineTo(b,z);
      if(left){path.moveTo(a,t);path.lineTo(a,z);}
      if(right){path.moveTo(b,t);path.lineTo(b,z);}
    });
    for(const [color,path] of paths){ctx.strokeStyle=color;ctx.stroke(path);}
    if(labels.length){
      ctx.fillStyle='#fff';ctx.textBaseline='middle';ctx.textAlign='left';
      for(const [text,x,y,h] of labels)drawSelectionPitch(ctx,text,x,y,h);
    }
    ctx.restore();
  }
  function rangeInkState(){
    const r=rangeHidden()?null:shownSelection(),f=selectionFlight;
    const live=drag?.kind==='selectionRange'&&gestureAlive(drag)?drag.rPaint:null;
    const pending=f?.paint&&f.view===selectionViewSerial&&f.midi===state.midi.path&&!rangeHidden()&&sameGestureBase(f.base,noteWriteScope())?f.paint:null;
    const draft=live||pending;
    const landing=!live&&pending?.source&&['move_body','copy_body'].includes(pending.part);
    return {r,draft,landing};
  }
  function ordinaryPitchCover(){
    if(!active())return null;
    if(tool==='rangeSelect'){
      const {r,draft,landing}=rangeInkState();
      const span=!draft||draft.shift===0?r:draft.shift===null?draft:landing?null:r;
      if(!span||!rangeCovers(span.scope,'midi-canvas'))return null;
      const only=span===r?rangeCreatedIds(r)?.notes:null;
      return n=>n.start>=span.start&&n.start<span.end&&(!only||only.has(n.id));
    }
    const p=notePendingPreview;
    if(p?.landing?.length){
      if(notePaintCurrent(p))return n=>p.pitchRows?.has(n);
      if(ownNoteLandingPublication(p)){
        const receipt=state.editor_transaction;
        if(p.copy&&receipt?.request===p.request&&receipt.revision===editor()?.revision&&Array.isArray(receipt.added_ids)){
          if(p.pitchReceipt!==receipt){p.pitchReceipt=receipt;p.pitchIds=new Set(receipt.added_ids);}
          return n=>p.pitchIds.has(n.id);
        }
        if(!p.copy)return n=>selected.has(n.id);
      }
    }
    if(!selected.size)return null;
    const f=noteNudge,nudged=nudgeViewLive(f)&&sameGestureBase(f.seen,noteWriteScope())?f.inkIds:null;
    return n=>selected.has(n.id)&&!nudged?.has(n.id);
  }
  function paintSelectionRange(ctx,id,offset,height,width,layer='both'){
    const {r,draft,landing}=rangeInkState();
    if(!r&&!draft)return;
    const axis=rollAxis();
    const paint=(span,raw,shift=0,background=true,contents=true,positionClone=false)=>{
      const {start,end,scope}=span,created=span===r?rangeCreatedIds(r):null;
      if(!(end>start)||!rangeCovers(scope,id))return;
      const a=axis.x(start)+shift-offset,b=axis.x(end)+shift-offset;
      if(b< -2||a>width+2)return;
      if(background&&layer!=='ink'){
        ctx.fillStyle=raw?'rgba(67,107,196,.09)':'rgba(67,107,196,.14)';ctx.fillRect(a,0,b-a,height);
        ctx.fillStyle=raw?'#7794d0':'#436bc4';ctx.fillRect(a,0,1,height);ctx.fillRect(b,0,1,height);
      }
      if(!contents||layer==='wash')return;
      // Viewport-only display of the native half-open intersection. No IDs,
      // snapped positions, or membership from the raster enter the command.
      const lo=Math.max(start,axis.seconds(offset-shift)),hi=Math.min(end,axis.seconds(offset+width-shift));
      if(hi<=lo)return;
      ctx.fillStyle='rgba(67,107,196,.46)';
      if(id==='midi-canvas'){
        const {high,row}=rollGeometry,landed=landing&&span===draft.source;
        const visit=draw=>forRangeNotes(lo,hi,(n,a,b)=>{
          if(created&&!created.notes.has(n.id))return;
          const y=(high-n.pitch)*row+1;
          if(y<0||y>=height)return;
          const x=Math.max(0,axis.x(a)+shift-offset),w=Math.min(width,axis.x(b)+shift-offset)-x;
          const left=landed||n.start>=start,right=landed||n.end<=end;
          const labelAt=landed?Math.max(start,n.start):n.start;
          draw(x,y,left&&right?Math.max(2,w):w,row-2,selectionNoteColor(n),left,right,axis.x(labelAt)+shift-offset>=0?n.pitch:null);
        });
        if(positionClone)visit((x,y,w,h,color,left,right,pitch)=>paintPositionNote(ctx,null,x,y,w,h,left,right,color,pitch));
        else paintNoteSelectionInk(ctx,visit);
      }else{
        const rows=ccEvents();for(let i=lowerBound(rows,lo,'seconds');i<rows.length&&rows[i].seconds<hi;++i)
          if(!created||created.controls.has(rows[i].id))ctx.fillRect(axis.x(rows[i].seconds)+shift-offset-2,ccY(rows[i].value)-2,4,4);
      }
    };
    if(!draft||draft.shift===0){if(r)paint(r,false);return;}
    if(draft.shift!==null){
      paint(draft,true,0,true,false);
      if(id==='midi-canvas'){
        // Source selection remains visible beneath the ordinary held clone.
        // On release only the destination owns the selection ink.
        if(r&&!landing)paint(r,false);
        paint(draft.source,true,draft.shift,false,true,!landing);
      }else{
        // CC64 keeps the established range membership, geometry and ink.
        if(r)paint(r,false);
        paint(draft.source,true,draft.shift,false);
      }
    }else paint(draft,true);
  }
  function semanticSelection(action,domain='notes'){
    return domain==='controls'?{action,domain,channel:ccChannel,ids:[...ccSelected]}:{action,domain,ids:[...selected]};
  }
  const signed=n=>`${n>0?'+':''}${n}`;
  function semanticBlocked(command,warnings){
    const w=warnings?.[0];
    switch(w?.code){
    case 'transpose_pitch_range':return `移调 ${signed(command.semitones)} 会让部分音符超出 MIDI 0–127，未修改任何音符。`;
    case 'length_scale_boundary':return `缩放到 ${command.percent}% 会有 ${w.count} 个音符越过曲目终点（音频与速度图范围），未修改任何音符。`;
    case 'nudge_boundary':return command.domain==='controls'?'微移会越过曲首或曲尾，未移动。':'微移会越过边界或让音符过短，未修改。';
    case 'cc_duplicate_no_span':return '单个 CC64 点没有长度可重复；请用 Alt 拖动复制。';
    case 'repeat_event_limit':return '一次最多新增 20000 个事件，请减少次数。';
    case 'repeat_past_end':return command.action==='duplicate'?'重复会越过曲尾，未添加。':'重复后的末尾超出曲尾，未修改。';
    case 'paste_origin_event_limit':return '一次最多粘贴 20000 个事件。';
    case 'paste_origin_boundary':return '原位位置超出当前曲长，未粘贴。';
    case 'paste_past_end':return `粘贴位置会让${command.channel===undefined?'音符':' CC64 '}越过曲尾，未粘贴。`;
    case 'cc_form_boundary':return '无效踏板编辑；原值未修改';
    case 'note_form_span':return '音符需要有效音高与正时长。';
    case 'info_boundary':{
      const notes=command.domain==='notes',name=command.field;
      const label=(notes?NOTE_FIELDS:CC_FIELDS).find(f=>f[0]===name)?.[1]||name;
      const limit=notes?({pitch:'0–127',velocity:'1–127'}[name]||`0–${fmt(duration())} 秒且长度 > 0`):`0–${fmt(duration())} 秒`;
      return `${label}：${w.count} 个对象会超出 ${limit}，未修改任何对象。`;
    }
    default:return '未修改任何对象。';
    }
  }
  function submitSemantic(command,view={},done=null,visual=null){
    return edit(semanticOperation(command,view),(error,receipt)=>{
      if(!error&&receipt?.semantic_preview?.blocked){
        error=semanticBlocked(command,receipt.semantic_warnings);
        if(!done)notice=error;
      }
      done?.(error,receipt);
    },visual);
  }
  // These rows are paint inputs only. No predicted time, value or
  // collision decision from them is submitted in a gesture command.
  function ccDragOperation(d,rows){return d.copy||d.creating?{type:'controls',add:rows.map(c=>({seconds:c.seconds,number:64,channel:c.channel??ccChannel,value:c.value}))}:{type:'controls',updates:rows.map(c=>({id:c.id,seconds:c.seconds,value:c.value}))};}
  function ccCommitRows(d,rows){const originals=new Map((d.originalControls||d.controls||[]).map(c=>[c.id,c]));return rows.map(c=>({...c,seconds:d.originalControls&&!d.ccTimeActive?originals.get(c.id)?.seconds??c.seconds:c.seconds+(d.ccCommitDelta??0)-(d.ccPreviewDelta??0),value:d.ccValueActive?d.ccTarget:originals.get(c.id)?.value??c.value}));}
  let ccCollisionIndex=null;
  function ccPreviewResolve(operation){
    const doc=editor(),controls=ccVisibleRows();
    if(ccCollisionIndex?.controls!==controls||ccCollisionIndex?.revision!==doc.revision||ccCollisionIndex?.tempo!==doc.tempo){
      const byId=new Map(),groups=new Map();
      controls.forEach((c,index)=>{if(c.number!==64)return;const key=ccTickKey(c),row={c,index,key};byId.set(c.id,row);if(!groups.has(key))groups.set(key,[]);groups.get(key).push(row);});
      ccCollisionIndex={controls,revision:doc.revision,tempo:doc.tempo,byId,groups};
    }
    const {byId,groups}=ccCollisionIndex,changed=new Map(),touched=new Set(),units=[];
    for(const update of operation.updates||[]){const old=byId.get(update.id);if(!old)continue;const row={...old.c,...update};if(row.seconds===old.c.seconds&&row.value===old.c.value)continue;changed.set(row.id,row);touched.add(old.key);}
    for(const key of touched){const group=groups.get(key),members=group.filter(x=>changed.has(x.c.id));
      const whole=members.length===group.length&&new Set(members.map(x=>ccTickKey(changed.get(x.c.id)))).size===1;
      for(const block of whole?[members]:members.map(x=>[x]))units.push({rows:block.map(x=>changed.get(x.c.id)),rank:[0,Math.max(...block.map(x=>x.c.seconds)),Math.max(...block.map(x=>x.index))]});
    }
    (operation.add||[]).forEach((c,index)=>units.push({rows:[{...c,id:'__new_cc_'+index}],rank:[1,index,0]}));
    const winners=new Map(),later=(a,b)=>a.some((v,i)=>v!==b[i]&&a.slice(0,i).every((n,j)=>n===b[j])&&v>b[i]);
    for(const unit of units){const key=ccTickKey(unit.rows[0]),old=winners.get(key);if(!old||later(unit.rank,old.rank))winners.set(key,unit);}
    const allowed=new Map([...winners].map(([key,u])=>[key,new Set(u.rows.map(c=>c.id))])),removed=new Set();
    for(const [key,ids] of allowed)for(const old of groups.get(key)||[]){const row=changed.get(old.c.id)||old.c;if(ccTickKey(row)===key&&!ids.has(row.id))removed.add(row.id);}
    for(const unit of units)for(const row of unit.rows){const ids=allowed.get(ccTickKey(row));if(ids&&!ids.has(row.id))removed.add(row.id);}
    return {removed,replaced:[...removed].filter(id=>!id.startsWith('__new_cc_')).length};
  }
  // R16: a drawn CC64 point is useless when its value equals the level already in effect just
  // before it (existing events + the drawn points kept so far, in time order). Existing points at
  // the same output time count as replaced (collision rule). Only the essential points are
  // previewed and committed; the raw sweep stays in drag.points so moving back still replaces.
  // R16: an Alt/draw SWEEP has priority over what is already there. Every existing CC64 point of
  // this channel inside the swept time range (ends inclusive) is overwritten in the same edit.
  function ccSweepOverwrite(points){
    if(!points?.length)return [];
    let lo=Infinity,hi=-Infinity;for(const p of points){lo=Math.min(lo,p.seconds);hi=Math.max(hi,p.seconds);}
    return ccEvents().filter(e=>(e.seconds>=lo||ccSameOutputTime(e.seconds,lo))&&(e.seconds<=hi||ccSameOutputTime(e.seconds,hi)));
  }
  function ccEssentialPoints(points,overwritten=[]){
    const gone=new Set(overwritten.map(e=>e.id));
    const drawn=[...points].sort((a,b)=>a.seconds-b.seconds),events=gone.size?ccEvents().filter(e=>!gone.has(e.id)):ccEvents(),out=[];
    let level=null,i=0;
    for(const p of drawn){
      while(i<events.length&&events[i].seconds<p.seconds&&!ccSameOutputTime(events[i].seconds,p.seconds)){level=events[i].value;i++;}
      // An existing point at the same output time is replaced ONLY if p is committed. Without p,
      // that existing value is what holds here, so p is needed whenever it differs from it.
      let same=null;while(i<events.length&&ccSameOutputTime(events[i].seconds,p.seconds)){same=events[i];i++;}
      const current=same?same.value:level===null?0:level; // nothing before: the pedal is up
      if(p.value!==current)out.push(p);
      level=p.value!==current?p.value:current;
    }
    return out;
  }
  const ccPencilSettleMs=100,ccPencilBouncePx=2.5;
  function ccPencilDisplay(ink,now,span=0){
    if(!ink)return null;
    const t=Math.min(1,Math.max(0,(now-ink.at)/ccPencilSettleMs));
    // One damped return: fast approach (~33ms), a single small rebound past the
    // 0/127 target, then a zero-velocity landing at t=1. The rebound is capped in
    // pixels so tall lanes never swing further than short ones.
    const d=ink.from-ink.target,f=(1-t)**2*Math.cos(1.5*Math.PI*t);
    if(f>=0||!(span>0))return ink.target+d*Math.max(0,f);
    const bounce=Math.min(ccPencilBouncePx,Math.abs(d)/127*span*.35)*127/span;
    return ink.target+Math.sign(d)*bounce*f/.178;
  }
  function updateCCPencilInk(d,value,alt,now=performance.now()){
    const moving=d.softPencil&&alt&&noteMotionAllowed();
    const previous=d.ccPencilInk;
    if(!moving||!previous){d.ccPencilInk={target:value,from:value,at:now};return;}
    if(previous.target===value)return;
    // Land promptly, then ease only the last part of the vertical travel.
    // A rapid reversal replaces this single transition rather than queuing it.
    const current=ccPencilDisplay(previous,now);
    d.ccPencilInk={target:value,from:value+(current-value)*.42,at:now};
  }
  function ccPreviewCollision(){
    if(!drag)return;
    if(drag.kind==='drawCC'){
      const events=ccEvents(),stamp=drag.wire.samples.length,moved=drag.moved;
      if(drag.ccPencilProjection?.events===events&&drag.ccPencilProjection.stamp===stamp&&drag.ccPencilProjection.moved===moved)return;
      drag.ccPencilProjection={events,stamp,moved};
      drag.overwrite=moved?ccSweepOverwrite(drag.points):[];drag.essential=ccEssentialPoints(drag.points,drag.overwrite);
    }
    const op=drag.kind==='drawCC'?{type:'controls',add:drag.essential}:drag.kind==='control'?ccDragOperation(drag,ccCommitRows(drag,ccGhost)):null;
    const collisionKey=drag.kind==='control'?`${drag.ccCommitDelta||0}:${drag.ccValueActive?drag.ccTarget:'keep'}:${drag.copy}`:null;
    let resolved=collisionKey!==null&&drag.ccCollisionKey===collisionKey?drag.ccCollisionResult:op?ccPreviewResolve(op):{removed:new Set(),replaced:0};
    drag.ccCollisionKey=collisionKey;drag.ccCollisionResult=resolved;drag.ccConflicts=resolved.removed;
    if(drag.kind==='drawCC'&&drag.overwrite?.length){const removed=new Set(resolved.removed);let replaced=resolved.replaced||0;for(const e of drag.overwrite)if(!removed.has(e.id)){removed.add(e.id);replaced++;}drag.ccConflicts=removed;drag.ccCollisionResult=resolved={removed,replaced};}
    const preview=drag.kind==='drawCC'?drag.essential:ccGhost;
    drag.ccPreviewRows=(preview||[]).map((c,i)=>({...c,id:drag.kind==='drawCC'||drag.copy?'__new_cc_'+i:c.id})).filter(c=>!resolved.removed.has(c.id));
    if(drag.ccConflicts.size)statusPosition(ghost?.seconds||0,`松手替换 ${resolved.replaced||0} 个原点${resolved.removed.size>(resolved.replaced||0)?` · 合并 ${resolved.removed.size-(resolved.replaced||0)} 个新点`:''}`);
  }
  function options(list,value){return list.map(([v,label])=>`<option value="${v}" ${String(v)===String(value)?'selected':''}>${label}</option>`).join('');}
  const noteValues=[[1,'1/4'],[.5,'1/8'],[.25,'1/16'],[.125,'1/32'],[1/3,'1/8 三连'],[1/6,'1/16 三连']];
  // Only states that need attention: preparing playback, or edits not yet in
  // the score. Project save state is shown once, in the title row.
  function editorStateChip(){
    if(state.midi.loading&&!(Number(state.midi.duration)>0))return '<span class="editor-save-state" data-dom-key="state-chip" data-state="loading">准备试听…</span>';
    return (editor().score_stale??editor().dirty)?'<span class="editor-save-state" data-dom-key="state-chip" data-state="stale" title="之后的编辑尚未更新到谱面；可点“更新谱面”。项目保存不受影响">谱面待更新</span>':'';
  }
  // Edit / read-only are one pair in one place. The current mode is pressed and
  // a no-op (see action()); the other runs the unchanged editorEnter /
  // editorReadOnly commands. Both keep data-do so focus survives the re-render.
  function modeSwitch(unavailable=''){
    const editing=active(),enterBlocked=Boolean(unavailable)||busy||(state.job.running&&!state.job.editor_export);
    const seg=(key,label,on,action,disabled,title)=>`<button type="button" data-dom-key="mode-${key}" data-do="${action}" aria-pressed="${on}"${disabled?' disabled':''}${key==='edit'&&unavailable?' aria-describedby="editor-unavailable-summary"':''} title="${html(title)}">${label}</button>`;
    return `<div class="segments editor-mode-switch" data-dom-key="mode-switch" role="group" aria-label="模式：编辑或只读浏览">${seg('edit','编辑',editing,'editorEnter',!editing&&enterBlocked,unavailable?'暂不能编辑，原因见下方提示':editing?'当前：编辑模式':'进入编辑：修改音符、CC64 与拍点')}${seg('read','只读浏览',!editing,'editorReadOnly',editing&&busy,editing?'切换为只读浏览，保留当前工作稿及走带位置':'当前：只读浏览')}</div>`;
  }
  function editorHeading(commands='',unavailable=''){
    return `<div class="editor-heading"><strong>${active()?'演奏编辑':'只读 MIDI'}</strong>${unavailable?'':editorStateChip()}<div class="editor-heading-actions" data-dom-key="heading-actions">${commands}${modeSwitch(unavailable)}<details class="editor-command-menu editor-more-menu" data-disclosure="editor-command-more"><summary aria-label="更多：快捷键与设置" title="更多"><svg aria-hidden="true"><use href="#i-more"/></svg></summary><div class="editor-command-body">${button('快捷键…','editorPreferences',{style:'editor-task-action',label:'快捷键：在设置中查看；可修改和恢复编辑及走带快捷键（本机设置，不改工程）'})}${button('设置…','appPreferences',{style:'editor-task-action',symbol:'settings',label:'设置：本机、所有项目通用——外观与操作、音频输出、保存与恢复、快捷键'})}</div></details></div></div>`;
  }
  function panel(){
    sync();
    if(!editor()){
      const reason=state.project?.editor_unavailable;
      if(!reason)return '';
      const next=state.project?.editor_recovery?.options?.length?'可从项目标题旁查看恢复副本。':'可重新打开工程；若仍不可编辑，请保留原文件并查看日志。';
      return `<section class="project-editor-tools" id="project-editor-tools" data-tool="readonly">
        ${editorHeading('',reason)}
        <details class="editor-unavailable-status" data-disclosure="editor-unavailable">
          <summary id="editor-unavailable-summary"><span role="status">暂不能编辑，MIDI 和谱面仍可查看。</span><span class="editor-unavailable-details">查看原因</span></summary>
          <p>${html(reason)}<br>${next}</p>
        </details>
      </section>`;
    }
    const detected=detectedBeats(),stableMode=workingView();
    const commands=(active()||stableMode)?`${button('撤销','editorUndo',{disabled:!historyOffered('editorUndo'),label:'撤销 Ctrl+Z'})}${button('重做','editorRedo',{disabled:!historyOffered('editorRedo')})}${button('更新谱面','editorScore',{disabled:!commandOffered('editorScore')||state.job.running,style:'primary',label:'按当前编辑更新谱面和配套 MIDI，不重新转录'})}${button('导出 MIDI','editorExport',{disabled:!commandOffered('editorExport')||state.job.running})}`:'';
    const configurationDisabled=!configEditable(),press=(name,label,on,title='')=>{
      const accessible=label.replace('↖ ','').replace(' ⇄',''),icon=({editorSelectTool:'cursor',editorRangeTool:'range',editorPenToggle:tool==='beat'?'beat':'pen',editorDrawTool:'pen',editorBeatTool:'beat',editorBeatEraseTool:'erase',editorSnapToggle:'magnet'})[name];
      return `<button data-do="${name}" class="secondary editor-icon-tool" aria-label="${html(accessible)}" aria-pressed="${Boolean(on)}" title="${html(accessible+'：'+title)}" ${configurationDisabled?'disabled':''}><svg aria-hidden="true"><use href="#i-${icon}"/></svg>${name==='editorPenToggle'?'<span class="editor-tool-switch" aria-hidden="true">⇄</span>':''}</button>`;
    };
    return `<section class="project-editor-tools" id="project-editor-tools" data-tool="${active()?tool:'readonly'}">
      ${editorHeading(commands)}${modeNotice?`<div id="editor-mode-status" role="status" aria-live="polite"><span>${html(modeNotice)}</span><button data-do="editorDismissModeNotice" aria-label="关闭模式提示" title="关闭提示">×</button></div>`:''}${(active()||stableMode)?`
      <div class="editor-tools" role="toolbar" aria-label="音符编辑工具"><div class="editor-tool-pair" role="group" aria-label="工具">${press('editorSelectTool','↖ 选择',tool==='select','选择对象；空白处框选')}${press('editorRangeTool','范围选择',tool==='rangeSelect','选择时间范围，不改变 L/R；拖边缘调整，拖内部移动内容，Alt 拖内部复制；Shift 框外点按扩展、拖动伸缩；框内点按清除、拖动新画')}${press('editorPenToggle',tool==='beat'?'拍点笔 ⇄':'画笔 ⇄',['draw','beat'].includes(tool),'再次点击切换：音符 / CC 画笔 ⇄ 拍点笔')}${press('editorBeatEraseTool','橡皮',tool==='eraseBeat','点击删除当前区域的音符、踏板、拍点或拍号；可撤销')}</div>
      <div class="editor-snap-group" role="group" aria-label="吸附与网格">${press('editorSnapToggle','吸附',snapEnabled,'开启或关闭拖动吸附，不量化已有音符')}<details class="editor-command-menu editor-grid-menu" data-disclosure="editor-grid-menu"><summary aria-label="网格、显示轴与移动方式"><span id="editor-grid-summary">${gridMenuLabel()}</span></summary><div class="editor-command-body">${typeof rulerModeField==='function'?rulerModeField():''}<label for="editor-snap">吸附方式</label><select id="editor-snap" class="control" aria-label="吸附方式">${options([['grid','绝对网格'],['relative','相对网格'],['events','事件'],['cursor','光标'],['grid-events-cursor','网格 + 事件 + 光标']],snap)}<optgroup label="音频参考"><option value="detected" title="${html(detected.available?'吸附到原始拍点':detected.reason)}" ${snap==='detected'?'selected':''} ${detected.available?'':'disabled'}>原始拍点${detected.available?'':'（不可用）'}</option></optgroup></select><label for="editor-quantize">网格精度</label><select id="editor-quantize" class="control" aria-label="网格精度">${options(noteValues,qState.preset)}</select></div></details></div><div class="editor-note-group" role="group" aria-label="音符量化与长度">${button('量化所选','editorQuantize',{disabled:!commandOffered('editorQuantize')||!sidebarActionCount('editorQuantize'),label:`Q：量化${qTargetName()} · ${presetName(quantizeUnit())} · 强度 ${tools.strength}%（在左侧侧栏“量化”中设置）；一次撤销`})}
      <label>长度 <select id="editor-length" class="control" aria-label="新建与量化音符长度" title="跟随量化精度或独立时值；不随视图缩放改变">${options([['link','随量化'],...noteValues],lengthQuantize)}</select></label>
      </div>${active()?`<div class="editor-insert-velocity" data-dom-key="insert-velocity"><label for="editor-insert-velocity">输入力度</label><input type="number" id="editor-insert-velocity" class="control" aria-label="输入力度" min="1" max="127" step="1" value="${insertVelocity}" title="仅影响新音符；画笔按住时上下拖动可调整单个音符" ${configurationDisabled?'disabled':''}><select id="editor-insert-velocity-preset" class="control editor-insert-velocity-preset" aria-label="输入力度预设" title="输入力度预设" ${configurationDisabled?'disabled':''}><option value="" selected>▾</option>${INSERT_VELOCITY_PRESETS.map(v=>`<option value="${v}">${v}</option>`).join('')}</select></div>`:''}
      <span id="editor-snap-status" class="editor-snap-status" data-bypass="${temporarySnapBypass}" role="status" title="Astra 扩展：按住 B 临时绕过吸附，松开恢复">${snapBypassLabel()}</span>
      </div>${commandMenus()}`:''}</section>`;
  }
  function diagnosticRanges(){
    const data=editor()?.tempo?.pulse_diagnostics;
    if(!data||data.automatic_correction!==false||!Array.isArray(data.ranges))return [];
    const length=duration();
    return data.ranges.slice(0,5).map((entry,index)=>{
      const row=entry&&typeof entry==='object'?entry:{};
      const valid=typeof row.start_seconds==='number'&&Number.isFinite(row.start_seconds)&&typeof row.end_seconds==='number'&&Number.isFinite(row.end_seconds)&&row.end_seconds>row.start_seconds;
      const start=valid?Math.max(0,Math.min(length,row.start_seconds)):0,end=valid?Math.max(0,Math.min(length,row.end_seconds)):0;
      return {row,index,start,end,valid:valid&&end>start};
    });
  }
  function diagnosticMarkup(disabled){
    const ranges=diagnosticRanges();if(!ranges.length)return '';
    const labels={weak_pulse_near_strong_evidence:'附近存在更强拍点证据',isolated_long_interval:'局部拍距偏长',tempo_level_change:'变速 / 半倍速疑点'};
    const source=({full_activation:'完整音频概率缓存',selected_pulses_only:'已选拍点'})[editor().tempo.pulse_diagnostics.source]||'来源未标注';
    return `<details class="editor-side-section editor-diagnostics"><summary>源分析复核 <small>${ranges.length}</small></summary><p>原分析提示，可能已人工修正。定位含前后2秒，不改拍位或播放。证据：${source}。</p>${ranges.map(({row,index,valid})=>{
      const title=(Array.isArray(row.types)?row.types:[]).map(t=>labels[t]).filter(Boolean).join(' / ')||'拍点待复核';
      const first=Array.isArray(row.evidence)?row.evidence[0]:null;
      const detail=Number.isFinite(first?.ratio)?`拍距比例 ${first.ratio.toFixed(2)}；也可能是真实音乐变化。`:Number.isFinite(first?.displacement_seconds)?`邻近证据相差 ${Math.abs(first.displacement_seconds*1000).toFixed(0)} ms；不是自动纠错结论。`:'';
      return `<div class="editor-diagnostic-item"><strong>${html(title)}</strong><small>${valid?`${row.start_seconds.toFixed(2)} – ${row.end_seconds.toFixed(2)} 秒`:'时间范围无效或超出当前音频'}${detail?` · ${html(detail)}`:''}</small>${button('在卷帘定位','editorLocateDiagnostic',{key:String(index),disabled:disabled||!valid})}</div>`;
    }).join('')}</details>`;
  }
  function sidebarTools(){
    if(!active())return '';
    return `
      ${ccChannels().length>1?`<details class="editor-side-section editor-range-tools" data-inspector-section="pedal"><summary>踏板通道</summary><label>CC64 通道<select id="editor-cc-channel" class="control">${options(ccChannels().map(c=>[c,c+1]),ccChannel)}</select></label></details>`:''}
<p id="editor-notice" class="editor-notice" role="status">${html(notice)}</p>`;
  }
  function commandMenus(){
    if(!active()&&!workingView())return '';
    const disabled=!editable(),r=range(),ready=hasRange(),rhythm=window.AstraRhythmTimeline,rhythmSelection=rhythm?.selectionInfo?.();
    const pick=(label,action,enabled,need='')=>{
      const required=need==='beats'?'两个拍点':need==='range'?'L/R 定位范围':'';
      return button(label,action,{disabled:!enabled,style:'quiet',symbol:need==='beats'?'beat':need==='range'?'range':'',label:required?`${label}（${required}）`:''});
    };
    const divide=rhythmSelection?.kind==='beat'&&rhythmSelection.beats.length===2;
    const action=(text,name,description,needsRange=false,symbol='')=>{
      const brief=({editorMeterBars:'小节线不动，均分内部拍点',editorDirectRecount:'指定范围内重新划分小节',editorShortBar:'从所选位置到下一拍号',editorOpening:'设置起点及前面的内容',editorOpeningRestore:'替换人工节拍修改，需确认',editorScoreRange:'只预览，不覆盖正式谱面',editorScoreEnd:'止于右定位点，不裁剪音频',editorDetectTrends:'生成候选，不改变播放速度'})[name]||description;
      const glyph=symbol||({editorDirectRecount:'bars',editorOpening:'start',editorScoreRange:'score',editorScoreEnd:'range'})[name]||'settings';
      const hint=needsRange&&!ready?'先在上方标尺拖出 L/R 范围':description;
      return `<button type="button" class="editor-task-action" data-do="${name}" ${!commandOffered(name)||(needsRange&&!ready)?'disabled':''} title="${html(hint)}" aria-label="${html(text+'：'+hint)}"><span class="editor-task-icon"><svg aria-hidden="true"><use href="#i-${glyph}"/></svg></span><span class="editor-task-copy"><strong>${html(text.replace(/…$/,''))}</strong><small>${html(brief)}</small></span><span class="editor-task-next" aria-hidden="true">›</span></button>`;
    };
    return `<div class="editor-command-bar" aria-label="小节与制谱命令">
      <details class="editor-command-menu editor-rhythm-menu" data-disclosure="editor-command-meter"><summary><svg aria-hidden="true"><use href="#i-bars"/></svg><span>小节与拍号</span></summary><div class="editor-command-body editor-rhythm-command-body">
      <section class="rhythm-menu-group" aria-label="拍点时值与均分"><strong class="rhythm-menu-caption">拍点</strong>
      ${rhythm?.durationToolbar?.()||''}
      ${rhythmSelection?.kind==='beat'?rhythm.selectionMarkup():''}
      ${pick('÷N 两点均分…','editorBeatDivide',!disabled&&divide,'beats')}
      </section>
      <section class="rhythm-menu-group" aria-label="L/R 选区的小节线与拍号"><strong class="rhythm-menu-caption">L/R 选区</strong>
      <div class="rhythm-menu-pair">
      ${pick('保留小节线，改拍号…','editorMeterBars',ready&&commandOffered('editorMeterBars'),'range')}
      ${pick('重新划分小节数…','editorDirectRecount',ready&&commandOffered('editorDirectRecount'),'range')}
      </div></section>
      ${window.AstraBeatChecks?.menu()||''}
      ${rhythmSelection?.kind==='meter'?`<section class="rhythm-menu-group" aria-label="拍号"><strong class="rhythm-menu-caption">拍号</strong>${rhythm.selectionMarkup()}</section>`:''}
      </div></details>
      <details class="editor-command-menu" data-disclosure="editor-command-score"><summary><svg aria-hidden="true"><use href="#i-score"/></svg><span>制谱</span></summary><div class="editor-command-body">
      ${action('预览 L/R 谱面','editorScoreRange','预览选定范围及邻近小节，不覆盖正式谱面。',true)}
      ${action('将 R 设为谱面终点…','editorScoreEnd','谱面到右定位点结束；不裁剪演奏或音频。',true)}
      ${editor().tempo.score_end?`<small>当前终点：${fmt(editor().tempo.score_end.seconds)} 秒</small>${button('恢复完整谱面','editorClearScoreEnd',{disabled:!commandOffered('editorClearScoreEnd')})}`:''}
      ${action('查找渐快 / 渐慢…','editorDetectTrends',`${ready?'检查 L/R 范围':'检查全曲'}；生成标记候选，不改变播放速度。`,false,'beat')}
      <small>确认修改后，使用顶部“更新谱面”。</small></div></details>      <span class="editor-command-scope" role="status">${ready?`L/R：${fmt(r.start)} – ${fmt(r.end)} 秒`:'尚未设置 L/R 范围'}</span>
    </div>`;
  }
  function rangePosition(r){return `${secondsToBeat(r.start).toFixed(3)} → ${secondsToBeat(r.end).toFixed(3)} 四分音符单位`;}
  function pulseLimits(){
    const t=editor()?.tempo||{},provider='cubase_local_reconstruction';
    const pulseSource=t.pulse_source,hasSource=Boolean(pulseSource)&&(typeof pulseSource!=='object'||Object.keys(pulseSource).length>0);
    const dense=t.source===provider||hasSource||t.adopted_candidate?.provider===provider||t.pulse_evidence?.provider===provider;
    return dense?{seconds:20,count:24,dense:true}:{seconds:60,count:64,dense:false};
  }
  function pulseRequestError(r,count){
    const limit=pulseLimits();
    if(!Number.isInteger(count)||count<1||count>limit.count)return `请输入 1–${limit.count} 的整数四分音符间隔数；不会静默取整。`;
    if(!(r.end>r.start)||r.end-r.start>limit.seconds)return `重找拍点的选区必须大于 0 且不超过 ${limit.seconds} 秒；请先缩小定位范围。`;
    const frames=Math.round(r.end*100)-Math.round(r.start*100)+1;
    if(limit.dense&&count*frames*181*181>220000000)return '逐帧重找超出本次计算预算，请缩短选区；不会退回粗粒度拍点。';
    return '';
  }
  // Reconcile control markup in place. Stable controls retain focus, hover,
  // native select state and disclosure state instead of remounting on each save.
  function patchControlMarkup(el,markup){
    if(!el)return;
    const shell=document.createElement('div');shell.innerHTML=markup;const next=shell.firstElementChild;
    if(!next){el.remove?.();return;}
    if(!el.childNodes){el.replaceWith(next);return;}
    const sameContext=el.getAttribute('data-info-context')===next.getAttribute('data-info-context');
    const key=n=>n.nodeType===1?(n.id||n.getAttribute('data-dom-key')||n.getAttribute('data-tool-section')||n.getAttribute('data-disclosure')||''):'';
    function patch(old,fresh){
      if(old.nodeType!==fresh.nodeType||old.nodeName!==fresh.nodeName||key(old)!==key(fresh)){old.replaceWith(fresh);return;}
      if(old.nodeType!==1){if(old.nodeValue!==fresh.nodeValue)old.nodeValue=fresh.nodeValue;return;}
      const keepOpen=old.nodeName==='DETAILS'?old.open:null,freshValue=fresh.value;
      const typing=sameContext&&(old===focused||old===document.activeElement)&&'value' in old&&old.value!==old.defaultValue;
      for(const attr of [...old.attributes])if(!fresh.hasAttribute(attr.name)&&!(attr.name==='open'&&keepOpen!==null))old.removeAttribute(attr.name);
      for(const attr of [...fresh.attributes])if(!(attr.name==='open'&&keepOpen!==null)&&old.getAttribute(attr.name)!==attr.value)old.setAttribute(attr.name,attr.value);
      const children=[...fresh.childNodes];
      children.forEach((child,index)=>{
        const cursor=old.childNodes[index]||null,k=key(child);
        const match=k?[...old.childNodes].find(n=>key(n)===k):cursor&&!key(cursor)?cursor:null;
        if(!match){old.insertBefore(child,cursor);return;}
        if(match!==cursor)old.insertBefore(match,cursor);
        patch(match,child);
      });
      while(old.childNodes.length>children.length)old.lastChild.remove();
      if(keepOpen!==null&&old.open!==keepOpen)old.open=keepOpen;
      if(!typing&&['INPUT','TEXTAREA','SELECT'].includes(old.nodeName)&&old.value!==freshValue)old.value=freshValue;
    }
    const focused=el.contains?.(document.activeElement)?document.activeElement:null;
    const selection=focused&&typeof focused.selectionStart==='number'?[focused.selectionStart,focused.selectionEnd,focused.selectionDirection]:null;
    patch(el,next);
    // Moving an existing keyed subtree may blur its input in older Chromium.
    if(focused?.isConnected&&document.activeElement!==focused&&!focused.disabled){
      focused.focus({preventScroll:true});
      if(selection)focused.setSelectionRange(...selection);
    }
  }
  function refresh(redraw=true){
    if(busy){if(redraw)requestRollDraw();return;}
    queueCursorProbe();
    patchControlMarkup($('project-editor-tools'),panel());
    const notes=active()?inspectNotes():[];
    if(active()||!workingView())patchControlMarkup($('editor-inspector'),inspectorMarkup(notes));
    const lane=$('mapping-lane');if(lane)lane.hidden=!active()||tool!=='map';
    refreshInfoLine(notes);window.AstraTransportPanel?.render();dockRangePanel();if(redraw)requestRollDraw();
  }
  function noteRenderIndex(){
    const doc=editor(),notes=noteRows();
    const version=publication();
    if(noteRenderCache?.project===state.project?.path&&noteRenderCache.revision===doc?.revision&&noteRenderCache.notes===notes&&noteRenderCache.version===version)return noteRenderCache;
    const byId=new Map(),entries=notes.map((note,order)=>{const entry={note,order,start:note.start,end:note.end};byId.set(note.id,entry);return entry;}).sort((a,b)=>a.start-b.start);
    let end=-Infinity;for(const entry of entries)entry.maxEnd=end=Math.max(end,entry.end);
    noteRenderCache={project:state.project?.path,revision:doc?.revision,version,notes,byId,entries};return noteRenderCache;
  }
  function lowerBound(rows,value,key){let lo=0,hi=rows.length;while(lo<hi){const mid=(lo+hi)>>>1;if((key?rows[mid][key]:rows[mid])<value)lo=mid+1;else hi=mid;}return lo;}
  function inspectNotes(){if(!selected.size)return [];const lookup=noteRenderIndex().byId;return [...selected].map(id=>lookup.get(id)).filter(Boolean).sort((a,b)=>a.order-b.order).map(entry=>entry.note);}
  function extrema(rows,read){
    let low=Infinity,high=-Infinity;
    for(const row of rows){const value=read(row);low=Math.min(low,value);high=Math.max(high,value);}
    return [low,high];
  }
  const PITCH_NAMES=['C','C♯','D','D♯','E','F','F♯','G','G♯','A','A♯','B'];
  const pitchName=p=>PITCH_NAMES[((p%12)+12)%12]+(Math.floor(p/12)-1);
  function parsePitch(raw){
    const text=String(raw).trim().replace('#','♯');if(/^-?\d+$/.test(text))return Number(text);
    const m=/^([A-Ga-g])([♯b]?)(-?\d)$/.exec(text);if(!m)return NaN;
    const base={C:0,D:2,E:4,F:5,G:7,A:9,B:11}[m[1].toUpperCase()]+(m[2]==='♯'?1:m[2]==='b'?-1:0);
    return (Number(m[3])+1)*12+base;
  }
  const glyph=name=>`<svg aria-hidden="true"><use href="#i-${name}"/></svg>`;
  // ---- Inspector: batch MIDI edits for a transcription correction pass -----
  // Object facts live in the info line; this column only holds edit functions,
  // grouped like Cubase's Key Editor inspector (Quantize · Transpose · Length),
  // plus velocity dynamics and the meter map that Astra's workflow needs.
  const INTERVALS=['同度','小二度','大二度','小三度','大三度','纯四度','三全音','纯五度','小六度','大六度','小七度','大七度'];
  function intervalName(amount){
    const n=Math.abs(amount),oct=Math.floor(n/12),rest=n%12;if(!n)return '不移动';
    const name=rest?INTERVALS[rest]:(oct===1?'纯八度':`${oct} 个八度`);
    return `${amount>0?'上':'下'}${rest&&oct?`${oct} 个八度 + `:''}${name}`;
  }
  const tools={strength:100,transpose:0,overlapTicks:0,legatoAdjust:0,scale:100,velocity:100};
  // One fixed musical precision (Cubase: in Bars+Beats the snap grid is the quantize value).
  // qState.preset is that value: Q uses it, Length Link uses it, and a FIXED top grid is it.
  // Old settings may contain a zoom-following grid mode. Retain their musical
  // preset, but zoom now changes rendered line density only.
  const Q_TARGETS=[['start','起点'],['end','终点'],['length','长度'],['both','起止']];
  const qState={preset:.25,target:'start',legato:'selected'};
  try{const saved=JSON.parse(window.localStorage?.getItem('astra-quantize')||'{}');
    if(noteValues.some(([v])=>Math.abs(v-saved.preset)<1e-9))qState.preset=noteValues.find(([v])=>Math.abs(v-saved.preset)<1e-9)[0];
    if(Q_TARGETS.some(([k])=>k===saved.target))qState.target=saved.target;
    if(['selected','any'].includes(saved.legato))qState.legato=saved.legato;
    if(Number.isFinite(saved.strength))tools.strength=Math.max(0,Math.min(100,Math.round(saved.strength)));
    const savedLength=noteValues.find(([v])=>Math.abs(v-Number(saved.length))<1e-9);if(saved.length==='link'||savedLength)lengthQuantize=saved.length==='link'?'link':String(savedLength[0]);}catch{}
  function saveQuantize(){try{window.localStorage?.setItem('astra-quantize',JSON.stringify({preset:qState.preset,target:qState.target,legato:qState.legato,strength:tools.strength,length:lengthQuantize}));}catch{}}
  // The only note-length rule (same as the backend): finite times and a positive length.
  // No artificial minimum; real project boundaries are checked where they apply.
  const validSpan=(start,end)=>Number.isFinite(start)&&Number.isFinite(end)&&end>start;
  const presetName=unit=>noteValues.find(([n])=>Math.abs(n-unit)<1e-9)?.[1]||`${unit} 拍`;
  let sectionOpen={quantize:true,transpose:true,length:false,velocity:false,meter:false};
  try{Object.assign(sectionOpen,JSON.parse(window.localStorage?.getItem('astra-inspector-sections')||'{}'));}catch{}
  let toolStatus=null;
  const sidebarNoteActions=new Set(['editorQuantize','editorTransposeApply','editorLengthScale','editorLegato','editorLegatoFreeze','editorFixedLengths','editorDeleteOverlaps']);
  let sidebarTargetCache=null;
  function sidebarRange(){
    const r=commandRange();
    return r&&typeof r.token==='string'&&rangeCovers(r.scope,'midi-canvas')?r:null;
  }
  function selectionKey(section){
    const range=['quantize','transpose','length'].includes(section)&&sidebarRange();
    return range?JSON.stringify([range.start,range.end,range.scope.mode,range.scope.channel]):
      [...selected].sort().join(',')+'|'+[...ccSelected].sort().join(',');
  }
  function sidebarTargets(name,key,objects){
    if(tool!=='rangeSelect'||!sidebarNoteActions.has(name))return {notes:objects??inspectNotes(),range:null};
    const range=sidebarRange();
    if(!range)return {notes:[],range:null};
    const rows=noteRenderIndex().entries;
    let cache=sidebarTargetCache;
    if(!cache||cache.token!==range.token||cache.rows!==rows){
      const whole=[],tail=[];
      forRangeNotes(range.start,range.end,n=>{
        if(n.end<=range.start||n.end>range.end)return;
        tail.push(n);
        if(n.start>=range.start)whole.push(n);
      });
      cache=sidebarTargetCache={token:range.token,rows,whole,tail};
    }
    const target=Q_TARGETS.some(([k])=>k===key)?key:qState.target;
    const full=name==='editorTransposeApply'||name==='editorQuantize'&&(target==='start'||target==='both');
    return {notes:full?cache.whole:cache.tail,range};
  }
  const sidebarActionCount=(name,key,objects)=>sidebarTargets(name,key,objects).notes.length;
  const sidebarCommand=(action,t)=>t.range?{action}:{action,domain:'notes',ids:t.notes.map(n=>n.id)};
  const sidebarContext=t=>t.range?{range_token:t.range.token}:{};
  const rangeSidebarBusy=()=>tool==='rangeSelect'&&(Boolean(drag)||Boolean(selectionFlight));
  // A status belongs to one selection and one document state: the revision it was made on and,
  // for an edit, the first revision after it. Undo or any later edit retires it.
  function setToolStatus(section,text,tone='info'){toolStatus={section,text,tone,key:selectionKey(section),base:editor()?.revision,seen:null,settled:true};refresh();}
  // Undo may return to the very revision id the edit started from, so a committed edit's
  // status is valid only on its own committed revision, never on the base again.
  function statusLive(){const r=editor()?.revision;if(!toolStatus)return false;if(toolStatus.settled)return r===toolStatus.base;if(toolStatus.tone==='working')return r===toolStatus.base;if(toolStatus.seen==null){if(r===toolStatus.base)return true;toolStatus.seen=r;}return r===toolStatus.seen;}
  function statusFor(section){return toolStatus?.section===section&&(!['transpose','quantize','length'].includes(section)||toolStatus.tone==='error')&&toolStatus.key===selectionKey(section)&&statusLive()?`<p class="tool-status" data-tone="${toolStatus.tone}" role="status">${html(toolStatus.text)}</p>`:'';}
  // Use the acknowledged native outcome, including an unchanged or blocked plan.
  function toolEdit(section,command,format,view={}){
    const base=editor()?.revision,key=selectionKey(section),prior=toolStatus;
    const working=toolStatus={section,text:'正在应用…',tone:'working',key,base,seen:null,settled:false};
    if(!submitSemantic(command,view,(error,receipt)=>{
      if(toolStatus!==working)return;
      const p=receipt?.semantic_preview,failed=error||(!p?'无法读取编辑结果。':'');
      toolStatus={section,key,base,seen:receipt?.revision??null,settled:receipt?.changed!==true,
        text:failed?(p?.blocked?failed:`未完成：${failed}`):format(p,receipt),
        tone:failed?'error':receipt.changed?'ok':'info'};
      refresh();
    }))toolStatus=prior;
  }
  document.addEventListener('toggle',event=>{const key=event.target?.dataset?.toolSection;if(!key||!(key in sectionOpen))return;sectionOpen[key]=event.target.open;if(key==='length')planLengthPreview();try{window.localStorage?.setItem('astra-inspector-sections',JSON.stringify(sectionOpen));}catch{}},true);
  const SLIDERS={'editor-q-strength':'strength','editor-length-scale':'scale','editor-legato-overlap':'overlapTicks','editor-legato-adjust':'legatoAdjust','editor-vel-scale':'velocity'};
  // Left MIDI values only: native typing stays, deliberate scrub commits once.
  const scrubNumbers=new Set(['editor-q-strength-n','editor-transpose-amount','editor-legato-adjust-n','editor-legato-overlap-n','editor-length-scale-n','editor-vel-scale-n']);
  const wheelGridIds=new Set(['editor-q-preset','editor-quantize']);
  const scrubField=e=>e?.closest?.('#editor-inspector input[type=number]')||null;
  const scrubUsable=e=>e?.isConnected&&!e.disabled&&(scrubNumbers.has(e.id)||wheelGridIds.has(e.id))&&configEditable();
  let scrub=null, scrubWheel=null;
  function scrubPaint(s,value){
    s.value=value;s.el.value=String(value);
    if(s.pair?.isConnected)s.pair.value=String(value);
  }
  function scrubFinish(s,commit){
    if(!s)return;
    clearTimeout(s.timer);
    const usable=scrubUsable(s.el), accepted=commit&&usable&&s.value!==s.start;
    if(!accepted){if(s.el.isConnected)s.el.value=s.original;if(s.pair?.isConnected)s.pair.value=s.pairOriginal;return;}
    s.el.dispatchEvent(new Event(s.grid||s.el.id==='editor-transpose-amount'?'change':'input',{bubbles:true}));
  }
  function scrubBegin(el){
    if(wheelGridIds.has(el.id))return {el,grid:true,start:el.value,value:el.value,original:el.value};
    const v=Number(el.value),lo=Number(el.min),hi=Number(el.max),step=Number(el.step)||1;
    if(el.value===''||![v,lo,hi,step].every(Number.isFinite)||lo>=hi||step<=0)return null;
    const pair=SLIDERS[el.id.replace(/-n$/,'')]?$(el.id.replace(/-n$/,'')):null;
    return {el,pair,lo,hi,step,start:v,value:v,original:el.value,pairOriginal:pair?.value};
  }
  function scrubClamp(s,value){return Math.min(s.hi,Math.max(s.lo,Number(value.toFixed(6))));}
  function finishWheel(commit){const s=scrubWheel;scrubWheel=null;scrubFinish(s,commit);}
  document.addEventListener('wheel',e=>{
    const grid=e.target?.closest?.('select#editor-q-preset, select#editor-quantize');
    const el=scrubField(e.target)||grid;
    if(grid&&grid.matches?.(':open'))return;
    if(!scrubUsable(el)||e.ctrlKey||e.metaKey||e.altKey||!e.deltaY||Math.abs(e.deltaY)<Math.abs(e.deltaX))return;
    e.preventDefault();e.stopPropagation();
    if(scrub)return;
    if(scrubWheel?.el!==el)finishWheel(true);
    const s=scrubWheel||=scrubBegin(el);
    if(!s)return;
    // A normal Windows/Chromium wheel detent can arrive around 53px or 100px.
    // Count each as one; accumulate smaller high-resolution deltas.
    const delta=e.deltaY, magnitude=Math.abs(delta);
    const lines=e.deltaMode===0?(magnitude>=40?Math.sign(delta):delta/40):
      e.deltaMode===1?(magnitude>=1?Math.sign(delta):delta):Math.sign(delta);
    s.wheel=Math.abs(lines)>=1?lines:(s.wheel||0)+lines;
    const whole=Math.trunc(s.wheel);s.wheel-=whole;
    if(whole&&s.grid){
      const rows=Array.from(s.el.options), step=Math.sign(whole);
      let i=s.el.selectedIndex+step;
      while(i>=0&&i<rows.length&&(rows[i].disabled||rows[i].closest?.('optgroup')?.disabled))i+=step;
      if(i>=0&&i<rows.length)scrubPaint(s,rows[i].value);
    }else if(whole)scrubPaint(s,scrubClamp(s,s.value-Math.max(-8,Math.min(8,whole))*s.step));
    clearTimeout(s.timer);s.timer=setTimeout(()=>finishWheel(true),160);
  },{capture:true,passive:false});
  document.addEventListener('pointerdown',e=>{
    const el=scrubField(e.target);
    if(scrubWheel)finishWheel(true);
    if(!scrubUsable(el)||e.button!==0||e.pointerType!=='mouse'||e.ctrlKey||e.metaKey||e.altKey)return;
    const s=scrubBegin(el);if(!s)return;
    scrub={...s,pointer:e.pointerId,x:e.clientX,y:e.clientY,drift:0,moved:false};
  },true);
  document.addEventListener('pointermove',e=>{
    const s=scrub;if(!s||s.pointer!==e.pointerId)return;
    if(!scrubUsable(s.el)||!e.buttons){scrub=null;scrubFinish(s,false);return;}
    if(!s.moved){
      const dy=e.clientY-s.y,dx=e.clientX-s.x;
      if(Math.abs(dx)>5&&Math.abs(dx)>=Math.abs(dy)){scrub=null;return;}
      if(Math.abs(dy)<5)return;
      s.moved=true;s.el.setPointerCapture?.(s.pointer);
    }
    s.drift+=(s.y-e.clientY)/(e.shiftKey?14:4);s.y=e.clientY;
    scrubPaint(s,scrubClamp(s,s.start+Math.trunc(s.drift)*s.step));
    e.preventDefault();
  },true);
  for(const name of ['pointerup','pointercancel','lostpointercapture'])document.addEventListener(name,e=>{
    const s=scrub;if(!s||s.pointer!==e.pointerId)return;
    scrub=null;if(!s.moved)return;
    scrubFinish(s,name==='pointerup');e.preventDefault();
  },true);
  document.addEventListener('focusout',e=>{if(scrubWheel?.el===e.target)finishWheel(true);},true);
  document.addEventListener('keydown',e=>{
    if(e.key==='Escape'&&(scrub?.moved||scrubWheel)){const s=scrub;scrub=null;scrubFinish(s,false);finishWheel(false);e.preventDefault();e.stopImmediatePropagation();return;}
    if(scrubWheel&&e.target===scrubWheel.el)finishWheel(true);
  },true);
  window.addEventListener('blur',()=>{const s=scrub;scrub=null;scrubFinish(s,false);finishWheel(false);});

  // Slider/number input only updates its pair and the live preview text in place;
  // no inspector rebuild, so focus, hover and the pressed thumb stay put.
  document.addEventListener('input',event=>{const id=String(event.target?.id||''),baseId=id.replace(/-n$/,''),key=SLIDERS[baseId];if(!key)return;const el=event.target,raw=Number(el.value);if(el.value===''||!Number.isFinite(raw))return;const value=Math.max(Number(el.min),Math.min(Number(el.max),raw));tools[key]=value;const other=document.getElementById(id===baseId?baseId+'-n':baseId);if(other&&other!==el)other.value=String(value);if(key==='strength')saveQuantize();if(key==='overlapTicks'||key==='legatoAdjust')planLengthPreview();updateToolLabels();});
  function playheadSeconds(){return typeof midiDisplayPosition==='function'?midiDisplayPosition():(state.midi.position||0);}
  // Q remains independent of zoom and drag Snap. The native plan supplies
  // the counts and outline rows; neither is a write operation.
  const quantizeLength=()=>lengthQuantize==='link'?'link':Number(lengthQuantize);
  const quantizeCommand=(target,choice)=>({...sidebarCommand('quantize',choice),target,
    preset:qState.preset,length:quantizeLength(),strength:tools.strength});
  let qPlanCache=null,qPlanFlight=null;
  function quantizePlanCurrent(request){
    if(!request||!semanticCurrent(request.base))return false;
    const c=request.command,target=qPreviewTarget??qState.target;
    if(c.target!==target||c.preset!==qState.preset||c.length!==quantizeLength()||c.strength!==tools.strength)return false;
    if(tool==='rangeSelect')return typeof request.view.range_token==='string'&&sidebarRange()?.token===request.view.range_token;
    return !request.view.range_token&&c.ids.length===selected.size&&c.ids.every(id=>selected.has(id));
  }
  function requestQuantizePlan(){
    const request=qPlanCache;
    if(qPlanFlight||request?.sent||!quantizePlanCurrent(request))return;
    qPlanFlight=request;request.sent=true;
    const finish=plan=>{
      qPlanFlight=null;
      if(qPlanCache===request){
        if(!quantizePlanCurrent(request))qPlanCache=null;
        else{
          request.plan=plan;
          if(qPreviewHover)requestRollDraw();
        }
      }
      requestQuantizePlan();
    };
    previewSemantic(request.base,semanticOperation(request.command,request.view)).then(data=>{
      finish({...data.semantic_preview,rows:(data.events?.upsert||[]).map(entry=>entry.row)});
    },error=>finish({error:error.message}));
  }
  function currentQuantizePlan(notes){
    const target=qPreviewTarget??qState.target,choice=sidebarTargets('editorQuantize',target,notes);
    if(!choice.notes.length){qPlanCache=null;return null;}
    if(!quantizePlanCurrent(qPlanCache))qPlanCache={base:semanticBase(),command:quantizeCommand(target,choice),view:sidebarContext(choice),plan:null,sent:false};
    requestQuantizePlan();
    return qPlanCache.plan;
  }
    let lengthBox=null,lengthFlight=null,lengthQueued=false;
  function lengthChoice(name,allowPart=false){
    const target=sidebarTargets(name);
    const all=allowPart&&tool!=='rangeSelect'&&['select','draw'].includes(tool)&&selectionDomain==='notes'&&!selected.size&&!ccSelected.size;
    return all?{notes:editor()?.notes||[],range:null,all:true}:target;
  }
  function lengthLegatoCommand(target,percent){
    return {...(target.all?{action:'legato',all_notes:true}:sidebarCommand('legato',target)),
      mode:qState.legato,percent,overlap_ticks:tools.overlapTicks};
  }
  function lengthLegatoReady(target,percent){
    return target.notes.length>0&&(percent<0||qState.legato!=='selected'||target.notes.length>1);
  }
  function lengthBoxLive(b,strict=false){
    if(!b||!semanticCurrent(b.base)||!configEditable()||!sectionOpen.length||!tools.legatoAdjust||
       tools.legatoAdjust!==b.command.percent||tools.overlapTicks!==b.command.overlap_ticks||qState.legato!==b.command.mode)return false;
    if(b.target.range)return sidebarRange()?.token===b.target.range.token;
    if(b.target.all)return selectionDomain==='notes'&&tool!=='rangeSelect'&&!selected.size&&!ccSelected.size;
    return tool!=='rangeSelect'&&selected.size===b.target.notes.length&&(!strict||b.target.notes.every(n=>selected.has(n.id)));
  }
  function planLengthPreview(){
    if(!tools.legatoAdjust||!sectionOpen.length||!configEditable()){if(lengthBox){lengthBox=null;requestNoteFeedback();}return;}
    if(lengthBoxLive(lengthBox,true))return;
    const t=lengthChoice('editorLegatoFreeze',true);
    if(!lengthLegatoReady(t,tools.legatoAdjust)){if(lengthBox){lengthBox=null;requestNoteFeedback();}return;}
    lengthBox={base:semanticBase(),target:t,command:lengthLegatoCommand(t,tools.legatoAdjust),rows:[],sent:false};
    requestNoteFeedback();
    if(!lengthQueued){lengthQueued=true;setTimeout(runLengthPreview,50);}
  }
  function runLengthPreview(){
    lengthQueued=false;
    const b=lengthBox;
    if(lengthFlight||!lengthBoxLive(b,true)||b.sent)return;
    b.sent=true;lengthFlight=b;
    previewSemantic(b.base,semanticOperation(b.command,sidebarContext(b.target))).then(data=>{
      if(lengthBox!==b||!lengthBoxLive(b,true))return;
      const byId=new Map(b.target.notes.map(n=>[n.id,n]));
      b.rows=(data.semantic_preview.updates||[]).map(r=>{
        const n=byId.get(r.id);return n?{...n,end:r.end}:null;
      }).filter(Boolean).sort((a,b)=>a.start-b.start);
      let furthest=-Infinity;
      b.rows=b.rows.map(n=>({...n,furthest:(furthest=Math.max(furthest,n.end))}));
      requestNoteFeedback();
    },()=>{}).finally(()=>{lengthFlight=null;if(lengthBox!==b)runLengthPreview();});
  }
  function drawLengthPreview(ctx,offset,height,width){
    const b=lengthBox;
    if(drag||editLocked()||!lengthBoxLive(b)||!b.rows.length)return;
    ctx.save();ctx.strokeStyle='#375fae';ctx.fillStyle='rgba(67,107,196,.13)';ctx.lineWidth=1.3;ctx.setLineDash?.([4,2]);
    const first=midiSeconds(offset),last=midiSeconds(offset+width),i0=lowerBound(b.rows,first,'furthest');
    for(let i=i0;i<b.rows.length&&b.rows[i].start<=last;i++){
      const n=b.rows[i];
      if(!b.target.range&&!b.target.all&&!selected.has(n.id))continue;
      const x=midiX(n.start)-offset,right=midiX(n.end)-offset,y=(rollGeometry.high-n.pitch)*rollGeometry.row+1,h=rollGeometry.row-2;
      if(x>width||right<=0||y+h<0||y>height)continue;
      const a=Math.max(0,x),z=Math.min(width,right);
      ctx.fillRect(a,y,z-a,h);ctx.strokeRect(a+.5,y+.5,Math.max(1,z-a-1),Math.max(1,h-1));
    }
    ctx.restore();
  }
  let lengthPedalReview=null;
  function pedalLengthObjectScope(){return tool==='select'&&selectionDomain==='notes'&&!ccSelected.size&&selected.size>0;}
  function pedalLengthReviewLive(r){
    return r&&editable()&&pedalLengthObjectScope()&&semanticCurrent(r.base)&&
      selected.size===r.command.ids.length&&r.command.ids.every(id=>selected.has(id));
  }
  function pedalLengthText(warnings=[]){
    const code=warnings[0]?.code;
    return ({
      pedal_length_shared:'选区外还有音符用到同一段踏板。请把这些音符也选中。',
      pedal_length_edge_tie:'有音符和踩下或抬起在同一刻，未转换。',
      pedal_length_retrigger:'这段踏板中有同音重触发，未转换。',
      pedal_length_nonbinary:'这段有半踏板，未修改原踏板。',
      pedal_length_incomplete:'这段踏板缺少完整的踩下或抬起点。',
      pedal_length_channel_mode:'这段有其他 MIDI 控制影响发声，未转换。',
      pedal_length_no_pairs:'选中音符没有可转换的踏板段。'
    })[code]||'这次没有转换踏板。';
  }
  async function previewPedalLength(){
    if(!editable()||!pedalLengthObjectScope())return;
    const review={base:semanticBase(),command:{action:'pedals_to_note_length',domain:'notes',ids:[...selected]}};
    lengthPedalReview=review;
    let data;
    try{data=await previewSemantic(review.base,semanticOperation(review.command));}
    catch(error){if(lengthPedalReview===review&&pedalLengthReviewLive(review)){notice=error.message;refresh();}return;}
    if(lengthPedalReview!==review||dialog||!pedalLengthReviewLive(review))return;
    const p=data.semantic_preview;
    if(p.blocked||!p.changed||!p.deleted_control_ids?.length){
      lengthPedalReview=null;notice=pedalLengthText(data.semantic_warnings);refresh();return;
    }
    modal('踏板到音符长度',
      `<p>延长 ${p.changed} 个音符，并移除对应的 ${p.deleted_control_ids.length} 个踏板点。</p>`,
      `${button('取消','closeDialog')}${button('转换','editorPedalsToLengthCommit',{style:'primary'})}`,'editor-pedals-to-length');
  }
  function commitPedalLength(){
    const review=lengthPedalReview;lengthPedalReview=null;
    if(dialog!=='editor-pedals-to-length')return;
    $('dialog')?.close();
    if(!pedalLengthReviewLive(review)){notice='选区已变化，未转换。';refresh();return;}
    submitSemantic(review.command,{},(error,receipt)=>{
      if(error||!receipt?.changed){notice=receipt?.semantic_warnings?.length?pedalLengthText(receipt.semantic_warnings):String(error||'这次没有转换踏板。');refresh();}
    });
  }
  const qTargetName=(target=qState.target)=>Q_TARGETS.find(([k])=>k===target)?.[1]||'起点';
  function quantizeNoChange(plan){
    if(plan.strength===0)return '强度 0%，不会移动';
    const reasons=[];
    if(plan.short)reasons.push(`${plan.short} 个短音保持原长`);
    if(plan.edge)reasons.push(`${plan.edge} 个越界或非正长度未改`);
    return reasons.length?reasons.join(' · '):'已在网格上，无需改动';
  }
  // Hovering or focusing the quantize section outlines the result on the roll.
  let qPreviewHover=false,qPreviewTarget=null;
  function updateQuantizePreviewFocus(element){
    const on=Boolean(element?.closest?.('[data-tool-section=quantize]'));
    const key=on&&element.closest?.('[data-do=editorQuantize]')?.dataset.key;
    const target=Q_TARGETS.some(([k])=>k===key)?key:null;
    if(on===qPreviewHover&&target===qPreviewTarget)return;
    const hadTarget=qPreviewTarget!==null;
    qPreviewHover=on;qPreviewTarget=target;
    if(on||hadTarget){
      currentQuantizePlan(inspectNotes());
    }
    requestRollDraw();
  }
  document.addEventListener('pointerover',event=>updateQuantizePreviewFocus(event.target),true);
  document.addEventListener('focusin',event=>updateQuantizePreviewFocus(event.target),true);
  function drawQuantizePreview(ctx,offset,height,width){
    if(!qPreviewHover||drag||!active())return;
    const plan=quantizePlanCurrent(qPlanCache)?qPlanCache.plan:null;
    if(!plan?.rows?.length)return;
    ctx.save();ctx.strokeStyle='#a5577c';ctx.fillStyle='rgba(165,87,124,.16)';ctx.lineWidth=1.25;ctx.setLineDash?.([3,2]);
    for(const n of plan.rows){
      if(n.pitch>rollGeometry.high)continue;
      const y=(rollGeometry.high-n.pitch)*rollGeometry.row;if(y>height||y+rollGeometry.row<0)continue;
      const a=midiX(n.start)-offset,b=midiX(n.end)-offset;if(a>width||b<0)continue;
      ctx.fillRect(a,y+1,Math.max(2,b-a),rollGeometry.row-2);ctx.strokeRect(a+.5,y+1.5,Math.max(2,b-a-1),rollGeometry.row-3);
    }
    ctx.setLineDash?.([]);ctx.restore();
  }
  // In-place inspector update: keyed patch keeps focus, open sections and hover.
  function updateToolLabels(){patchControlMarkup($('editor-inspector'),inspectorMarkup());if(qPreviewHover)requestRollDraw();}
  function inspectorMarkup(notes){
    if(!active())return '';
    notes??=inspectNotes();const n=notes.length,disabled=!configEditable();
    // 折叠组保持用户的开/合选择；不因选择变化而折叠或重建结构。
    const section=(key,title,icon,content)=>`<details class="editor-tool-section" data-tool-section="${key}" ${sectionOpen[key]?'open':''}><summary>${glyph(icon)}<span>${title}</span></summary><div class="editor-tool-body">${content}${statusFor(key)}</div></details>`;
    // 参数无选择也可配置；只有应用需要音符。
    const slider=(id,label,min,max,step,value,unit,title)=>`<div class="tool-slider" data-dom-key="${id}-slider" title="${html(title)}"><label for="${id}-n">${label}</label><input type="range" id="${id}" min="${min}" max="${max}" step="${step}" value="${value}" ${disabled?'disabled':''} aria-label="${label}"><span class="tool-number"><input type="number" id="${id}-n" class="control" min="${min}" max="${max}" step="1" value="${value}" ${disabled?'disabled':''} aria-label="${label}（${unit.trim()}）"><small>${unit.trim()}</small></span></div>`;
    const numberRow=(id,label,value,min,max,unit,step=1)=>'<label class="tool-field inspector-number-row" for="'+id+'"><span>'+label+'</span><span class="inspector-spin"><input type="number" id="'+id+'" class="control" min="'+min+'" max="'+max+'" step="'+step+'" value="'+value+'" '+(disabled?'disabled':'')+' aria-label="'+label+'">'+(unit?'<small>'+unit+'</small>':'')+'</span></label>';
    const gridSteps=Math.round(1/(Number(qState.preset)||.25));
    const ruler=Array.from({length:4},(_,i)=>'<span style="--subdivisions:'+gridSteps+'"><b>'+(i+1)+'</b></span>').join('');
    // 按钮文字固定：选中/取消选中时文字槽不变，布局不位移；无目标时仅禁用。
    const apply=(text,action,extra={})=>{
      const {key,...rest}=extra,count=sidebarActionCount(action,key,notes);
      const title=(extra.label??text)+(count?' · '+count+' 个音符':' · 无适用的选中音符');
      return button(text,action,{style:'secondary tool-apply',...extra,label:title,disabled:!count||!commandOffered(action,key)||Boolean(rest.disabled)});
    };
    // items: [key, text, 可选无障碍说明]
    const seg=(label,items,current,action)=>`<div class="segments tool-segments" role="group" aria-label="${label}">${items.map(([k,t,d])=>`<button type="button" data-do="${action}" data-key="${k}" aria-pressed="${k===current}"${d?` aria-label="${html(d)}" title="${html(d)}"`:''} ${disabled?'disabled':''}>${t}</button>`).join('')}</div>`;

    // 量化（Cubase 01）：强度 → 精度 → 长度精度 → 量化长度 / 量化结尾 / 应用量化；Q 键目标放在次级小菜单。
    currentQuantizePlan(notes);
    // Q 键目标：原生 details 默认收起（无 open 属性）；开合状态由 DOM 协调器按 data-dom-key 保留。
    const qCurrent=(Q_TARGETS.find(([k])=>k===qState.target)??[])[1]??'';
    const quantize=section('quantize','量化','magnet',`${numberRow('editor-q-strength-n','量化强度',tools.strength,0,100,'%',1)}
      <label class="tool-field" for="editor-q-preset" title="固定音乐精度：量化与“跟随”长度都用它；顶部网格选固定值时就是这个值。顶部“自动 · 随缩放”只改拖动吸附，不改它"><span>网格类型</span><select id="editor-q-preset" class="control" ${disabled?'disabled':''}>${options(noteValues,qState.preset)}</select></label>
      <label class="tool-field" for="editor-q-length" title="只用于“量化长度”；跟随＝随量化精度，不受缩放影响；与顶部长度共用"><span>长度精度</span><select id="editor-q-length" class="control" ${disabled?'disabled':''}>${options([['link','跟随网格'],...noteValues],lengthQuantize)}</select></label>
      <div class="inspector-grid-preview" aria-hidden="true">${ruler}</div>
      <div class="tool-actions">
        ${apply('量化长度','editorQuantize',{key:'length',label:'按“长度”量化所选音符的时值；一次撤销；不改 Q 键目标'})}
        ${apply('量化结尾','editorQuantize',{key:'end',label:'按精度与强度量化所选音符的结尾；一次撤销；不改 Q 键目标'})}
        ${apply('应用量化','editorQuantize',{key:'start',label:'按精度与强度量化所选音符的起点；一次撤销；不改 Q 键目标'})}
      </div>
      <details class="tool-qkey" data-dom-key="editor-q-target-menu">
        <summary title="只决定键盘 Q 做什么；上方三个按钮各自独立，点击不会改动这里"><span class="tool-qkey-label">Q 键</span><span class="tool-qkey-current">${html(qCurrent)}</span><span class="tool-qkey-chevron" aria-hidden="true"></span></summary>
        <div class="tool-qkey-body tool-segment-full">${seg('Q 键目标（仅键盘 Q）',Q_TARGETS,qState.target,'editorQTarget')}</div>
      </details>`);

    // 移调（Cubase 02）：半音数字步进 → 应用转调。
    const transposeNotes=sidebarTargets("editorTransposeApply",undefined,notes).notes,tCount=transposeNotes.length;
    const [lo,hi]=tCount?extrema(transposeNotes,x=>x.pitch):[0,0],amt=tools.transpose,fits=lo+amt>=0&&hi+amt<=127;
    const transpose=section('transpose','移调','arrow',`${numberRow('editor-transpose-amount','半音',amt,-48,48,'')}
      ${apply('应用转调','editorTransposeApply',{disabled:!amt||!fits,label:tCount&&amt&&!fits?'无法移调：超出 MIDI 音域（0–127）':'所选音符整体移调；一次撤销'})}`);

    // 长度（Cubase 03）：比例 + 应用比例并排一组 → 重叠 → 扩展到下一个选定 → 应用连奏。
    // The top slider sets a private projection; Freeze alone commits that value.
    const toSel=qState.legato==='selected',freezeTarget=lengthChoice('editorLegatoFreeze',true),standard=sidebarTargets('editorLegato',undefined,notes);
    planLengthPreview();
    const length=section('length','长度','range',`
      <div class="length-parameter">${slider('editor-legato-adjust','调整连奏',-100,100,1,tools.legatoAdjust,'%','拖动预览；0% 保留原样，100% 到下一起点，负值缩短。点击冻结才写入。')}</div>
      <div class="length-parameter">${slider('editor-legato-overlap','重叠',-480,480,1,tools.overlapTicks,'Ticks','每拍 480 Ticks；正值留重叠，负值留间隙。')}</div>
      <button type="button" class="tool-switch" role="switch" data-do="editorLegatoMode" data-key="${toSel?'any':'selected'}" aria-checked="${toSel}" ${disabled?'disabled':''} title="只跟所选音符连奏；未勾选时跟下一个起点"><span class="tool-check" aria-hidden="true"></span><span class="tool-switch-text">扩展到下一个选定</span></button>
      ${apply('应用连奏','editorLegato',{disabled:!lengthLegatoReady(standard,100),label:'让所选音符接到下一起点，包含设定的间隙或重叠'})}
      ${button('冻结 MIDI 长度','editorLegatoFreeze',{style:'secondary tool-apply',disabled:!tools.legatoAdjust||!lengthLegatoReady(freezeTarget,tools.legatoAdjust)||!commandOffered('editorLegatoFreeze'),label:freezeTarget.all?'提交当前调整值（当前 MIDI 的所有音符）':'提交当前调整值（所选音符）'})}
      <div class="tool-divider"></div>
      ${apply('固定长度','editorFixedLengths',{label:'使用顶部长度精度指定的时值'})}
      ${button('踏板到音符长度','editorPedalsToLength',{style:'secondary tool-apply',disabled:!editable()||!pedalLengthObjectScope()||!(editor()?.controls||[]).some(c=>c.number===64),label:'将所选音符的完整踏板段转换为实际时值；先确认再写入'})}
      ${apply('删除重叠（单声道）','editorDeleteOverlaps',{key:'mono',label:'只缩短与后一个同音高音符重叠的选定音符'})}
      ${apply('删除重叠（复音）','editorDeleteOverlaps',{key:'poly',label:'按下一起点缩短重叠的选定音符，同起点和弦保留'})}`);

    return `<section id="editor-inspector" class="editor-inspector" data-layout="cubase-compact" data-notes="${n}" aria-label="MIDI 批量编辑">${quantize}${transpose}${length}${sidebarTools()}</section>`;
  }
  // ---- Info line ---------------------------------------------------------
  function pencilInfoNote(ticket=pencilSelection){
    return ticket?.note&&!ticket.done&&ticket.view===noteViewSerial&&ticket.project===stateProjectPath()&&ticket.midi===state.midi?.path&&
      !selected.size&&!ccSelected.size&&(!drag||drag.kind==='drawNote')&&['select','draw'].includes(tool)?ticket.note:null;
  }
  function cancelPencilInfo(ticket){
    if(ticket.infoFrame){cancelAnimationFrame(ticket.infoFrame);ticket.infoFrame=0;}
  }
  function queuePencilInfo(ticket,note){
    if(!ticket||ticket!==pencilSelection)return;
    const prev=ticket.note;ticket.note=note;
    if(infoLineHidden||ticket.infoFrame||(prev.end===note.end&&prev.velocity===note.velocity))return;
    ticket.infoFrame=requestAnimationFrame(()=>{ticket.infoFrame=0;paintPencilInfo(ticket);});
  }
  function paintPencilInfo(ticket){
    const note=ticket===pencilSelection&&pencilInfoNote(ticket);
    const line=$('editor-info-line');
    if(!note||!line||line.hidden||line.dataset.kind!=='pencil-note')return;
    // Start and pitch stay anchored for the draw. Only these values can
    // change while held; no markup rebuild, canvas work, or layout reads.
    const set=(name,value)=>{
      const input=$('info-note-'+name);
      if(!input||input.value===value)return null;
      input.defaultValue=value;input.value=value;return input;
    };
    set('end',fmt(note.end));set('length',fmt(note.end-note.start));
    const velocity=set('velocity',String(Math.round(note.velocity)));
    const swatch=velocity?.parentNode?.querySelector('.info-velocity');
    if(swatch){
      swatch.style.setProperty('--v',Math.max(0,Math.min(127,note.velocity))/127);
      swatch.style.setProperty('--c',window.AstraVelocity?.color?.(note)||'#83a8bd');
    }
  }
  function infoContext(notes){
    if(!active())return {kind:workingView()&&!window.AstraNoteInspector?.hasSelection?.()?'empty':'off'};
    const pending=pencilInfoNote();if(pending)return {kind:'pencil-note',items:[pending]};
    const selection=rangeMasked()?null:commandRange();if(selection)return {kind:'selection-range',selection};
    notes??=inspectNotes();const ccs=ccSelected.size?ccEvents().filter(c=>ccSelected.has(c.id)):[];
    if(notes.length&&ccs.length)return {kind:'mixed',notes,ccs};
    if(notes.length)return {kind:'notes',items:notes};
    if(ccs.length)return {kind:'cc',items:ccs};
    if(selectedBeatFlag?.revision===editor().revision)return {kind:'beat',flag:selectedBeatFlag,beats:[selectedBeatFlag.seconds]};
    // Beat-lane selection is owned by the rhythm timeline; read it, never copy it.
    const rhythm=window.AstraRhythmTimeline?.selectionInfo?.();
    if(rhythm&&rhythm.kind==='meter'){
      const list=editor().tempo.time_signatures||[],index=list.findIndex(m=>Math.abs(beatToSeconds(m.beat)-rhythm.seconds)<1e-6);
      if(index>=0)return {kind:'meter',index,meter:list[index],next:list[index+1]||null,seconds:rhythm.seconds};
    }
    if(rhythm&&rhythm.kind==='beat'){
      const beats=rhythm.beats?.length?rhythm.beats:[rhythm.seconds],first=Math.min(...beats);
      const flag=beatFlags(midiWidth,0).find(f=>Math.abs(f.seconds-first)<1e-6)||{seconds:first,number:'?'};
      return {kind:'beat',flag,beats};
    }
    return {kind:'empty'};
  }
  const NOTE_FIELDS=[['start','起点','s',n=>n.start],['end','终点','s',n=>n.end],['length','长度','s',n=>n.end-n.start],['pitch','音高','',n=>n.pitch],['velocity','力度','',n=>n.velocity]];
  const CC_FIELDS=[['seconds','位置','s',c=>c.seconds],['value','数值','',c=>c.value]];
  function meterAt(seconds){
    const list=editor().tempo.time_signatures||[],q=secondsToBeat(seconds);let index=-1;
    list.forEach((m,i)=>{if(m.beat<=q+1e-7)index=i;});
    return index<0?null:{index,meter:list[index]};
  }
  function meterText(m){return m.smart_measure?'智能':m.free?'自由':m.silence?'静音':`${m.displayNumerator||m.numerator}/${m.denominator}`;}
  // Bar | beat | fraction, fraction trimmed to 2 places for reading only.
  function musicAt(seconds){try{return String(window.AstraRhythmTimeline?.musicPosition?.(seconds)||'').replace(/(\d+\.\d{2})\d+/g,'$1');}catch{return '';}}
  function infoLineMarkup(notes){
    if(infoLineHidden)return '<div id="editor-info-line" class="editor-info-line" hidden></div>';
    const ctx=infoContext(notes),key=infoKey(ctx);
    const head=(title,sub='',control='')=>`<div class="info-head" data-dom-key="info-head"><span class="info-kind">${html(title)}</span>${control||(sub?`<small>${html(sub)}</small>`:'')}</div>`;
    // Shared facts carry a stable key: reserved width (CSS) and in-place patching.
    const fact=(label,value,id='',slot='')=>`<div class="info-field"${slot?` data-info-fact="${slot}" data-dom-key="fact-${slot}"`:''}><span>${html(label)}</span><strong class="info-value"${id?` id="${id}"`:''}>${html(value)}</strong></div>`;
    const shell=(body,message='')=>`<div id="editor-info-line" class="editor-info-line" data-kind="${ctx.kind}" data-info-context="${html(key)}" role="region" aria-label="信息行" tabindex="0">${body}${message}</div>`;
    if(ctx.kind==='off')return window.AstraNoteInspector?.infoLineMarkup?.()||shell(head('只读 MIDI')+fact('音符',state.midi.notes?.length??'—'));
    const pencil=ctx.kind==='pencil-note',inputDisabled=!pencil&&!commandOffered('editorInfoCommit');
    if(infoMessageKey!==key){infoMessage='';infoMessageKey=key;}
    const message=`<span id="editor-info-message" class="info-message" role="status">${html(infoMessage)}</span>`;
    const meterEntry=(at,label='拍号')=>at&&(pencil||!active())?fact(label,meterText(at.meter),'','meter'):at?`<button type="button" class="info-field info-meter" data-dom-key="info-meter" data-info-fact="meter" data-do="editorMeterEvent" data-key="${at.index}" ${!commandOffered('editorMeterEvent')?'disabled':''} title="修改此处拍号（先预览，可撤销）"><span>${label}</span><strong class="info-value">${html(meterText(at.meter))}</strong></button>`:fact(label,'—','','meter');
    const stamp=seconds=>`${fmt(seconds)} s`;
    let body='';
    if(ctx.kind==='selection-range'){
      const r=ctx.selection,bars=rulerMode==='bars',unit=bars?'四分音符':'s';
      const name=({notes:'仅音符',cc64:`CC64 · 通道 ${r.scope.channel+1}`,notes_cc64:`音符 + CC64 · 通道 ${r.scope.channel+1}`,full:'全垂直 · 含隐藏 CC'})[r.scope.mode];
      const fields=[['start','起点'],['end','终点'],['length','长度']].map(([key,label])=>
        `<label class="info-field" data-info-field="${key}"><span>${label}</span><input id="info-range-${key}" data-info="selection-range:${key}" data-range-token="${html(r.token)}" data-axis="${rulerMode}" class="control" inputmode="decimal" value="${fmt(r[bars?key+'_beat':key])}" ${inputDisabled?'disabled':''} aria-label="选区${label}（${unit}）" title="仅调整选区，不移动内容"><small>${unit}</small></label>`).join('');
      // Same 拍号 cell as every other state; a range spanning changes reads them, never edits them.
      const inside=(editor().tempo.time_signatures||[]).filter(m=>{const s=beatToSeconds(m.beat);return s>r.start+1e-6&&s<r.end-1e-6;}).length,at=meterAt(r.start);
      const scopeOptions=[['notes','仅音符'],...(!ccLaneHidden||['cc64','notes_cc64'].includes(r.scope.mode)
        ?[['cc64',`CC64 · 通道 ${r.scope.channel!=null?r.scope.channel+1:ccChannel+1}`],
           ['notes_cc64',`音符 + CC64 · 通道 ${r.scope.channel!=null?r.scope.channel+1:ccChannel+1}`]]:[]),
        ['full','全垂直 · 含隐藏 CC']];
      const scopePicker=editable()?`<select id="info-range-scope" aria-label="选区内容" title="选区内容" data-range-token="${html(r.token)}" data-committed="${r.scope.mode}">${scopeOptions.map(([key,label])=>`<option value="${key}" ${key===r.scope.mode?'selected':''} ${ccLaneHidden&&['cc64','notes_cc64'].includes(key)?'disabled':''}>${label}</option>`).join('')}</select>`:'';
      body=head('时间选区',name,scopePicker)+`<div class="info-fields">${fields}</div>`+(inside&&at?fact('拍号',`${meterText(at.meter)} 起 · 内含 ${inside} 处变化`):meterEntry(at));
    }else if(ctx.kind==='empty'){
      const r=range(),located=hasLocators(r)&&r.start!==r.end;
      body=head('未选择')+fact('音符总数',noteRows().length,'editor-info-note-count','count')+fact(`CC64 · 通道 ${ccChannel+1}`,ccEvents().length,'info-cc-count','cc')
        +meterEntry(meterAt(located?r.start:0),located?'L 处拍号':'起始拍号')
        +(located?fact('L',stamp(r.start),'','left')+fact('R',stamp(r.end),'','right'):fact('总时长',stamp(duration()),'','duration'));
    }else if(ctx.kind==='beat'){
      const f=ctx.flag,count=ctx.beats.length,value=window.AstraRhythmTimeline?.durationInfo?.(f);
      const [lo,hi]=extrema(ctx.beats,s=>s);
      const title=count>1?`${count} 个拍点`:f.number==='?'?'拍点':`第 ${f.number} 拍`;
      body=head(title,musicAt(f.seconds))+(count>1?fact('范围',`${fmt(lo)} – ${stamp(hi)}`):fact('音频时间',stamp(f.seconds),'','time')+(value?.value?fact('谱面时值',value.label):''))+meterEntry(meterAt(f.seconds));
    }else if(ctx.kind==='meter'){
      const next=ctx.next?beatToSeconds(ctx.next.beat):null;
      body=head('拍号事件')+meterEntry(ctx)+fact('位置',musicAt(ctx.seconds)||stamp(ctx.seconds))+fact('作用到',next===null?'曲尾':musicAt(next)||stamp(next));
    }else if(ctx.kind==='mixed'){
      body=head('混合选择')+fact('音符',ctx.notes.length)+fact('踏板点',ctx.ccs.length);
    }else{
      const notes=pencil||ctx.kind==='notes',items=ctx.items,first=items[0],fields=notes?NOTE_FIELDS:CC_FIELDS,multi=items.length>1;
      const title=multi?`${items.length} 个${notes?'音符':'踏板点'}`:notes?'音符':'踏板点';
      const sub=musicAt(notes?first.start:first.seconds);
      const cells=fields.map(([name,label,unit,get])=>{
        const [lo,hi]=extrema(items,get),mixed=hi-lo>1e-6,value=get(first);
        const shown=name==='pitch'?pitchName(value):unit?fmt(value):String(Math.round(value));
        const range=mixed?(name==='pitch'?`${pitchName(lo)} – ${pitchName(hi)}`:unit?`${fmt(lo)} – ${fmt(hi)} s`:`${Math.round(lo)} – ${Math.round(hi)}`):'';
        if(!notes&&name==='value')return `<div class="info-field"><span>CC64${mixed?' · 混合':''}</span><div class="segments" role="group" aria-label="踏板状态"><button type="button" data-do="editorCCState" data-key="0" aria-pressed="${items.every(c=>c.value===0)}" ${!commandOffered('editorCCState')?'disabled':''}>抬起</button><button type="button" data-do="editorCCState" data-key="127" aria-pressed="${items.every(c=>c.value>0)}" ${!commandOffered('editorCCState')?'disabled':''}>踩下</button></div>${items.some(c=>c.value!==0&&c.value!==127)?`<small title="保留原始半踏板值；选择状态才转换">${mixed?'原值混合':value}</small>`:''}</div>`;
        const swatch=name==='velocity'&&notes?`<span class="info-velocity" style="--v:${Math.max(0,Math.min(127,value))/127};--c:${window.AstraVelocity?.color?.(first)||'#83a8bd'}" aria-hidden="true"></span>`:'';
        return `<label class="info-field${mixed?' is-mixed':''}" data-info-field="${name}"><span>${label}${mixed?`<em title="所选对象此项不同：${html(range)}">≠</em>`:''}</span><input class="control" id="info-${notes?'note':'cc'}-${name}" data-info="${notes?'notes':'cc'}:${name}" type="text" inputmode="decimal" autocomplete="off" spellcheck="false" value="${html(shown)}" ${pencil?'readonly tabindex="-1"':inputDisabled?'disabled':''} aria-label="${label}${multi?'（显示第一个；'+(infoAbsolute?'绝对':'相对')+'修改全部所选）':''}" title="${multi?`显示第一个对象的值。回车按${infoAbsolute?'绝对':'相对'}方式修改全部 ${items.length} 个；Ctrl+回车 始终按绝对值。`:'回车应用；Esc 还原'}${range?'&#10;范围 '+html(range):''}">${unit?`<small>${unit}</small>`:''}${swatch}</label>`;
      }).join('');
      const mode=multi?`<div class="info-mode segments" role="group" aria-label="多选修改方式"><button type="button" data-do="editorInfoMode" data-key="relative" aria-pressed="${!infoAbsolute}" title="相对：每个对象按与第一个对象相同的差值修改（默认）">相对</button><button type="button" data-do="editorInfoMode" data-key="absolute" aria-pressed="${infoAbsolute}" title="绝对：所有对象设为同一值；也可 Ctrl+回车 临时使用">绝对</button></div>`:'';
      const channel=!notes?fact('通道',ccChannel+1,'','channel'):multi?'':fact('通道',Number.isInteger(first.channel)?first.channel+1:'—','','channel');
      body=head(title,sub)+`<div class="info-fields">${cells}</div>`+mode+channel;
    }
    return shell(body,message);
  }
  function infoKey(ctx=infoContext()){
    return JSON.stringify([ctx.kind,(ctx.items||ctx.notes||[]).map(i=>i.id),(ctx.ccs||[]).map(i=>i.id),
      ctx.flag?.seconds,ctx.kind==='meter'?ctx.seconds:null,ctx.selection?[ctx.selection.token,rulerMode]:null]);
  }
  function setInfoMessage(text){infoMessage=text;infoMessageKey=infoKey();const el=$('editor-info-message');if(el)el.textContent=text;}
  function infoCommit(input,absoluteOverride=false){
    const [domain,name]=String(input.dataset.info||'').split(':'),ctx=infoContext();
    if(ctx.kind==='pencil-note')return false;
    if(editLocked()){
      if(drag)return false;
      return queueEditCommand('editorInfoCommit',input.dataset.info,{selection:infoKey(ctx),axis:rulerMode,absolute:Boolean(absoluteOverride||infoAbsolute),
        input:{id:input.id,dataset:{...input.dataset},value:input.value,defaultValue:input.defaultValue}});
    }
    if(domain==='selection-range'){
      if(!editable()||drag||ctx.kind!==domain)return false;
      if(input.dataset.rangeToken!==ctx.selection.token||input.dataset.axis!==rulerMode){input.value=input.defaultValue;refreshInfoLine();return false;}
      const typed=input.value,previous=input.defaultValue,value=Number(typed);
      if(!String(typed).trim()||!Number.isFinite(value)){setInfoMessage('请输入有效的选区数值。');return false;}
      if(String(typed).trim()===String(previous).trim())return false;
      input.defaultValue=typed;
      const sent=selectionEdit({action:'info',token:ctx.selection.token,field:name,value});
      if(!sent)input.defaultValue=previous;
      return sent;
    }
    if(!editable()||!['notes','cc'].includes(domain)||ctx.kind!==domain)return false;
    const items=ctx.items,fields=domain==='notes'?NOTE_FIELDS:CC_FIELDS,field=fields.find(f=>f[0]===name);
    if(!field)return false;
    const label=field[1],typed=input.value,previous=input.defaultValue,text=String(typed??'').trim(),shownText=String(previous??'').trim();
    if(!text){setInfoMessage(`${label}不能为空；未修改任何对象。`);input.value=previous;input.select?.();return false;}
    // Do not write the rounded display value back without an explicit edit.
    if(text===shownText){setInfoMessage('');return false;}
    const parse=t=>name==='pitch'?parsePitch(t):Number(t),value=parse(text);
    if(!Number.isFinite(value)){setInfoMessage(name==='pitch'?'音高请输入 0–127 或音名，如 C4、F♯3；未修改。':`${label}请输入数字；未修改。`);input.select?.();return false;}
    if(domain==='cc'&&name==='value'&&value!==0&&value!==127){setInfoMessage('踏板状态只能为 0 或 127；原值未修改');return false;}
    const shown=parse(shownText),key=infoKey(ctx),base=semanticBase();
    const command={action:'info_assign',domain:domain==='notes'?'notes':'controls',ids:items.map(item=>item.id),field:name,value,
      absolute:Boolean(absoluteOverride||infoAbsolute),...(shownText&&Number.isFinite(shown)?{shown}:{}),...(domain==='cc'?{channel:ccChannel}:{})};
    // Absorb the change event that can follow Enter. On refusal restore the
    // prior display baseline without trampling text typed since submission.
    input.defaultValue=typed;
    const sent=submitSemantic(command,{},(error,receipt)=>{
      if(infoKey()!==key||semanticBase().generation!==base.generation)return;
      if(error){
        if($(input.id)===input&&input.defaultValue===typed&&input.value===typed){
          input.defaultValue=previous;input.value=typed;
          if(document.activeElement===input)input.select?.();
        }
        setInfoMessage(error);return;
      }
      const p=receipt?.semantic_preview;
      if(!p||!receipt.changed){setInfoMessage('');return;}
      setInfoMessage(items.length>1?`${label}已按${p.absolute?'绝对值':'相对差值'}修改 ${p.changed} 个；Ctrl+Z 撤销。`:`${label}已修改；Ctrl+Z 撤销。`);
    });
    if(!sent)input.defaultValue=previous;
    return sent;
  }
  // The committed field is disabled (or re-rendered) while the edit saves. Hand focus back
  // only if nothing else took it meanwhile; any pointer press elsewhere cancels.
  function handBackFocus(original){
    const id=original.id,until=performance.now()+4000;infoRefocus=id;
    const tick=()=>{if(infoRefocus!==id)return;const t=$(id),active=document.activeElement,idle=!active||active===document.body;
      if(!idle&&active!==t&&active!==original){infoRefocus=null;return;} // the user moved on
      if(idle&&t&&t!==original&&t.isConnected&&!t.disabled){t.focus();t.select?.();}
      if(performance.now()<until)setTimeout(tick,80);else infoRefocus=null;};
    setTimeout(tick,80);
  }
  // New-value emphasis is paint-only: the text is already the authoritative
  // value; a ≤200 ms background fade on that cell. Never during a gesture, never
  // queued (a newer change cancels the running one), never under reduced motion.
  // (State lives on the hoisted function, so an early refresh cannot hit a TDZ.)
  function infoFlashAllowed(){
    return !drag&&!window.AstraRhythmTimeline?.isDragging?.()&&!window.AstraRhythmMotion?.reduced?.()&&
      !window.matchMedia?.('(prefers-reduced-motion: reduce)')?.matches;
  }
  function flashInfoValue(node){
    if(typeof node?.animate!=='function'||!node.isConnected)return;
    const running=flashInfoValue.running||=new WeakMap();
    running.get(node)?.cancel();
    flashInfoValue.ink||=(getComputedStyle(node).getPropertyValue('--ui-accent-soft')||'').trim()||'rgba(117,96,175,.18)';
    running.set(node,node.animate([{backgroundColor:flashInfoValue.ink},{backgroundColor:'rgba(0,0,0,0)'}],{duration:200,easing:'ease-out'}));
  }
  function infoFactValues(el){
    const values=new Map();
    for(const cell of el.querySelectorAll?.('[data-info-fact]')||[])values.set(cell.dataset.infoFact,cell.querySelector('.info-value')?.textContent);
    return values;
  }
  function refreshInfoLine(notes){
    const el=$('editor-info-line');if(!el)return;
    const context=el.getAttribute?.('data-info-context'),before=!el.hidden&&infoFlashAllowed()?infoFactValues(el):null;
    // Keyed reconciliation keeps dirty input only in the same selection.
    // The rest of the row can reflect real model changes while the user types.
    patchControlMarkup(el,infoLineMarkup(notes));
    // Same context only: a different selection is new content, not a change.
    const live=$('editor-info-line');
    if(!before?.size||!live||live.getAttribute?.('data-info-context')!==context)return;
    for(const cell of live.querySelectorAll?.('[data-info-fact]')||[]){
      const prior=before.get(cell.dataset.infoFact),value=cell.querySelector('.info-value');
      if(prior!==undefined&&value&&value.textContent!==prior)flashInfoValue(value);
    }
  }
  document.addEventListener('keydown',event=>{
    // A focused scroll strip uses native horizontal scrolling, not note navigation.
    if(event.target?.id==='editor-info-line'&&!event.ctrlKey&&!event.metaKey&&!event.altKey&&!event.shiftKey&&['ArrowLeft','ArrowRight','Home','End'].includes(event.key)){event.stopImmediatePropagation();return;}
    const input=event.target?.closest?.('#editor-info-line input[data-info]');if(!input)return;
    if(event.key==='Enter'&&!event.isComposing){event.preventDefault();event.stopImmediatePropagation();if(infoCommit(input,event.ctrlKey||event.metaKey))handBackFocus(input);return;}
    if(event.key==='Escape'){event.preventDefault();event.stopImmediatePropagation();if(afterEditCommand?.name==='editorInfoCommit'&&afterEditCommand.info?.input.id===input.id)cancelEditCommand();input.value=input.defaultValue;setInfoMessage('');input.blur();$('roll-scroll')?.focus?.({preventScroll:true});}
  },true);
  document.addEventListener('change',event=>{
    const scope=event.target?.closest?.('#info-range-scope');
    if(scope){
      const key=scope.value,committed=scope.dataset.committed;
      scope.value=committed;
      const r=commandRange();
      if(r?.token===scope.dataset.rangeToken&&key!==committed)action('editorSelectionMembership',key);
      return;
    }
    const input=event.target?.closest?.('#editor-info-line input[data-info]');if(!input||input.value===input.defaultValue)return;
    infoCommit(input,false);
  },true);
  document.addEventListener('pointerdown',event=>{if(!event.target?.closest?.('#editor-info-line'))infoRefocus=null;},true);
  function coordinate(event){
    const rect=(event.target.id==='ruler-playhead'?$('ruler-canvas'):event.target).getBoundingClientRect(),offset=$('roll-scroll')?.scrollLeft||0;
    const x=event.clientX-rect.left,worldX=x+offset;
    return {seconds:midiSeconds(worldX),x,y:event.clientY-rect.top,offset,worldX,rect};
  }
  function pedalGeometry(){return typeof midiPedalGeometry==='function'?midiPedalGeometry():{height:52,bottom:46,span:34};}
  let ccLineClick=null;
  const ccSameOutputTime=(a,b)=>ccRound(secondsToBeat(a)*9600)===ccRound(secondsToBeat(b)*9600);
  function ccLineAt(c){
    const rows=ccEvents();let low=0,high=rows.length;
    // Last event at or before the pointer, including legacy equal-time events.
    while(low<high){const middle=(low+high)>>>1;if(rows[middle].seconds<=c.seconds)low=middle+1;else high=middle;}
    const prior=rows[low-1];
    return prior&&Math.abs(ccY(prior.value)-c.y)<=4?prior:null;
  }
  // Hover and press share the same insertion hit test and point priority.
  const ccInsertLineAt=(c,event,nearest)=>tool==='select'&&!event.shiftKey&&!nearest?ccLineAt(c):null;
  function ccPointAt(c){
    const rows=ccPaintEvents(),x=c.worldX??midiX(c.seconds),minimum=midiSeconds(x-14),maximum=midiSeconds(x+14);let nearest=null;
    for(let i=lowerBound(rows,minimum,'seconds');i<rows.length&&rows[i].seconds<=maximum;i++){
      const cc=rows[i],d=Math.hypot(midiX(cc.seconds)-x,ccY(cc.value)-c.y);
      if(d<14&&(!nearest||d<nearest.d))nearest={cc,d};
    }
    return nearest;
  }
  function selectCC(id){
    selectionDomain='controls';selected.clear();selectedBeatFlag=null;ccSelected=new Set([id]);selectedCC=id;
  }
  const recentLineClick=c=>ccLineClick?.project===stateProjectPath()&&ccLineClick.channel===ccChannel&&
    performance.now()-ccLineClick.at<450&&Math.hypot(c.worldX-ccLineClick.x,c.y-ccLineClick.y)<5;
  const ccY=value=>{const g=pedalGeometry();return g.bottom-value/127*g.span;};
  const ccValue=y=>{const g=pedalGeometry();return Math.max(0,Math.min(127,Math.round((g.bottom-y)/g.span*127)));};
  const snapUnit=()=>qState.preset;
  function displayGridUnit(view){
    const axis=view?.axis||(typeof rollAxis==='function'?rollAxis():null);
    const pixelsPerBeat=axis?axis.width/axis.beatExtent:Math.abs(midiX(beatToSeconds(1))-midiX(beatToSeconds(0)));
    const precision=snapUnit();
    if(!(pixelsPerBeat>0))return precision;
    // Omit dense subdivision lines without inventing a different display phase:
    // every painted subdivision is still a member of the fixed snap grid.
    let unit=precision;while(unit<64&&unit*pixelsPerBeat<12)unit*=2;
    return unit;
  }
  function precisionLabel(){return `固定 ${presetName(snapUnit())} · ${snapEnabled&&!temporarySnapBypass?'启用吸附':'自由移动'}`;}
  function gridMenuLabel(){
    const names={grid:'绝对',relative:'相对',events:'事件',cursor:'光标','grid-events-cursor':'网格＋事件＋光标',detected:'原始拍点'};
    const precision=['grid','relative','grid-events-cursor'].includes(snap)?` · ${presetName(snapUnit())}`:'';
    return `${names[snap]||'网格'}${precision}${!snapEnabled?' · 关闭':temporarySnapBypass?' · 临时关闭':''}`;
  }
  function updatePrecisionLabel(){const label=$('editor-grid-summary');if(label){const text=gridMenuLabel();if(label.textContent!==text)label.textContent=text;}}
  function setPrecision(value){
    const v=noteValues.find(([n])=>Math.abs(n-Number(value))<1e-9);if(!v)return;qState.preset=v[0];
    saveQuantize();lastSnapTarget=null;
    const top=$('editor-quantize'),inspector=$('editor-q-preset');
    if(top)top.value=String(qState.preset);
    if(inspector)inspector.value=String(qState.preset);
    updatePrecisionLabel();updateToolLabels();requestRollDraw();
  }
  function lengthUnit(){return lengthQuantize==='link'?qState.preset:Number(lengthQuantize);}
  function quantizeUnit(target=qState.target){return target==='length'?lengthUnit():qState.preset;}
  function noteLength(){return lengthUnit();}
  // Press-time Ctrl/Meta (or B) latches free time for this note gesture.
  // Later modifier presses can still temporarily bypass an otherwise snapped gesture.
  function noteSnapSettings(event){return {mode:snap,enabled:snapEnabled,unit:snapUnit(),length:noteLength(),sections:gridSections(),free:Boolean(event.ctrlKey||event.metaKey||temporarySnapBypass)};}
  // New starts have no pre-existing relative offset. The top grid places the
  // start; Length Quantize (Link = Q preset) supplies click/drag length, not zoom.
  function newNoteAt(c,event,timing=noteSnapSettings(event)){const start=snapped(c.seconds,event,null,'draw',timing);return {start,end:beatToSeconds(secondsToBeat(start)+timing.length),pitch:Math.max(0,Math.min(127,rollGeometry.high-Math.floor(c.y/rollGeometry.row))),velocity:insertVelocity,channel:0};}
  function snapOn(event,context='move',timing=null){
    return (timing?.enabled??snapEnabled)&&!temporarySnapBypass&&!timing?.free&&!((event?.ctrlKey||event?.metaKey)&&(context==='cc'||context==='draw'||context==='note'));
  }
  function snapBypassLabel(){return temporarySnapBypass?'临时自由 · 松开 B 恢复':'按住 B 自由拖动';}
  function setSnapBypass(value,recompute=true){
    if(temporarySnapBypass===value)return;
    temporarySnapBypass=value;bypassRevision=value?editor()?.revision:null;lastSnapTarget=null;
    const label=$('editor-snap-status');if(label){label.textContent=snapBypassLabel();label.setAttribute('data-bypass',String(value));}
    if(recompute&&drag?.lastEvent&&gestureAlive(drag))pointerMove(drag.lastEvent,true);
    else if(recompute&&pencilGuideTarget&&lastHover)updateCursor(lastHover);
    if(recompute)window.AstraRhythmTimeline?.refreshSnap?.();
    requestRollDraw();
  }
  function pointerState(event){return {target:drag?.target||event.target,pointerId:event.pointerId,clientX:event.clientX,clientY:event.clientY,ctrlKey:event.ctrlKey,metaKey:event.metaKey,altKey:event.altKey,shiftKey:event.shiftKey,preventDefault(){}};}
  function detectedBeats(){
    const payload=state.project?.detected_beats,key=state.project?.path||'',tempo=editor()?.tempo||{},evidence=tempo.pulse_evidence;
    if(detectedSnapCache?.project===key&&detectedSnapCache.payload===payload&&detectedSnapCache.evidence===evidence&&detectedSnapCache.tempo===tempo&&detectedSnapCache.cache_key===payload?.cache_key&&detectedSnapCache.duration===duration())return detectedSnapCache;
    // The backend resolves the active provider, checks project/source and result
    // hashes, and marks failures unavailable. Do not accept another map's cache.
    const provider='cubase_local_reconstruction',native=tempo.source===provider||Boolean(tempo.pulse_source)||tempo.adopted_candidate?.provider===provider||evidence?.provider===provider;
    const digest=v=>typeof v==='string'&&/^[a-f0-9]{64}$/.test(v),ref=payload?.evidence_ref;
    const fields=['version','provider','project_source_sha256','normalized_sha256','result_sha256','observations_sha256','decode_key','result_path'];
    const nativeValid=native&&payload?.source==='cubase_local_cache'&&payload.available===true&&payload.provider===provider&&ref?.version===1&&ref.provider===provider&&evidence&&fields.every(k=>ref[k]===evidence[k])&&['project_source_sha256','normalized_sha256','result_sha256','observations_sha256','decode_key'].every(k=>digest(ref[k]))&&payload.result_sha256===ref.result_sha256&&payload.cache_key===ref.decode_key;
    const validSource=nativeValid||(!native&&payload?.source==='beat_this_cache'&&typeof payload.cache_key==='string'&&payload.cache_key.length>0);
    const values=payload?.seconds;
    const valid=payload?.available!==false&&validSource&&Array.isArray(values)&&values.length>0&&values.every(s=>typeof s==='number'&&Number.isFinite(s)&&s>=0&&s<=duration()+.001);
    const points=valid?[...new Set(values)].sort((a,b)=>a-b):[],known=new Set(points),downs=payload?.downbeat_seconds;
    const downbeats=valid&&Array.isArray(downs)&&downs.every((s,i)=>known.has(s)&&s<duration()&&(i===0||s>downs[i-1]))?downs.slice():[];
    detectedSnapCache={project:key,payload,evidence,tempo,cache_key:payload?.cache_key,duration:duration(),available:Boolean(valid),points,downbeats,reason:payload?.reason||'当前项目没有可用的原始检测拍数据；不能用编辑速度图代替。'};
    return detectedSnapCache;
  }
  function eventSnapPoints(){
    const doc=editor(),key=state.project?.path||'',version=publication();
    if(eventSnapCache?.project===key&&eventSnapCache.revision===doc.revision&&eventSnapCache.notes===doc.notes&&eventSnapCache.version===version)return eventSnapCache.points;
    const points=[];
    for(const n of doc.notes){points.push({seconds:n.start,id:n.id,note:n,edge:'start',order:points.length},{seconds:n.end,id:n.id,note:n,edge:'end',order:points.length+1});}
    points.sort((a,b)=>a.seconds-b.seconds);
    eventSnapCache={project:key,revision:doc.revision,version,notes:doc.notes,points};return points;
  }
  function nearestPoint(points,seconds,events=false){
    // Binary search once per pointer update. Selection changes need no cache rebuild:
    // skip selected IDs on either side of the insertion point without allocations.
    let lo=0,hi=points.length;
    const at=i=>events?points[i].seconds:points[i];
    while(lo<hi){const mid=(lo+hi)>>>1;if(at(mid)<seconds)lo=mid+1;else hi=mid;}
    let left=lo-1,right=lo;
    if(events){while(left>=0&&selected.has(points[left].id))left--;while(right<points.length&&selected.has(points[right].id))right++;}
    const a=left>=0?at(left):undefined,b=right<points.length?at(right):undefined;
    if(a===undefined)return b;if(b===undefined)return a;
    const x=midiX(seconds);return Math.abs(midiX(a)-x)<=Math.abs(midiX(b)-x)?a:b;
  }
  const calibrationOn=event=>!(event?.ctrlKey||event?.metaKey)&&snapOn(event);
  function calibrationNoteHit(event){
    const canvas=$('midi-canvas'),viewport=$('roll-scroll');
    if(!canvas||!viewport||!Number.isFinite(event?.clientX)||!Number.isFinite(event?.clientY))return null;
    const rect=canvas.getBoundingClientRect(),clip=viewport.getBoundingClientRect(),{clientX:x,clientY:y}=event;
    return x>=Math.max(rect.left,clip.left)&&x<Math.min(rect.right,clip.right)&&y>=Math.max(rect.top,clip.top)&&y<Math.min(rect.bottom,clip.bottom)
      ?{worldX:x-rect.left+viewport.scrollLeft,y:y-rect.top}:null;
  }
  function calibrationSnap(seconds,event,view=null){
    const limit=duration(),raw=Math.max(0,Math.min(limit,seconds)),free={seconds:raw,note:null,edge:null};
    const min=view?.min??0,max=view?.max??limit;
    if(!calibrationOn(event)||!editor()||raw<min||raw>max)return free;
    const project=view?.axis?.x??midiX,x=project(raw),prior=view?.previous;
    function choose(a,b){
      const distance=p=>p&&p.seconds>=min&&p.seconds<=max?Math.abs(project(p.seconds)-x):Infinity;
      const da=distance(a),db=distance(b),left=da<db||da===db&&(a?.order??Infinity)<=(b?.order??Infinity);
      const best=left?a:b,gap=left?da:db,other=left?b:a,otherGap=left?db:da;
      if(!(gap<12))return null;
      // Hold only a subpixel tie, never extend the existing capture radius or
      // mask a directly hit edge. The free handle is not part of this latch.
      if(otherGap<12&&other.seconds===prior?.seconds&&other.edge===prior.edge&&other.note.id===prior.note?.id&&
         otherGap-gap<=Math.min(.5,Math.abs(project(other.seconds)-project(best.seconds))/4))return other;
      return best;
    }
    // Prefer the note actually under the pointer, including when the beat lane
    // owns capture. Use the same note hit geometry; do not seek in the captured Y.
    const hit=view?view.noteHit:calibrationNoteHit(event),note=hit?noteAt(null,hit,true):null;
    let picked=note&&choose({seconds:note.start,note,edge:'start',order:0},{seconds:note.end,note,edge:'end',order:1});
    if(!picked){
      const source=eventSnapPoints();
      if(calibrationEdges?.source!==source){
        const rows=[];
        // Stable sort in eventSnapPoints keeps the original tie order for chords.
        for(const p of source)if(Number.isFinite(p.seconds)&&rows.at(-1)?.seconds!==p.seconds)rows.push(p);
        calibrationEdges={source,rows};
      }
      const rows=calibrationEdges.rows,i=lowerBound(rows,raw,'seconds');
      // X is monotone in both projections. In a legal interval only the two
      // bracketing times can win: a dense chord/window adds no query work.
      picked=choose(rows[i-1],rows[i]);
    }
    return picked?{seconds:picked.seconds,note:picked.note,edge:picked.edge}:free;
  }
  function snapped(seconds,event,reference=null,context='move',timing=null){
    lastSnapTarget=null;
    if(!snapOn(event,context,timing))return Math.max(0,seconds);
    const mode=timing?.mode??snap;
    if(mode==='detected'){
      const detected=detectedBeats(),near=detected.available?nearestPoint(detected.points,seconds):undefined;
      if(near!==undefined&&Math.abs(midiX(near)-midiX(seconds))<=10){lastSnapTarget=near;return near;}
      return Math.max(0,seconds);
    }
    const unit=timing?.unit??snapUnit(),q=secondsToBeat(seconds);
    if(mode==='relative'&&reference!==null)return beatToSeconds(Math.max(0,secondsToBeat(reference)+Math.round((q-secondsToBeat(reference))/unit)*unit));
    const gridTime=beatToSeconds(gridBeat(q,unit,timing?.sections));
    if(mode==='grid'||mode==='relative')return gridTime;
    let near;
    if(mode==='events'||mode==='grid-events-cursor')near=nearestPoint(eventSnapPoints(),seconds,true);
    if(mode==='cursor'||mode==='grid-events-cursor'){
      const visible=drag?.kind==='seek'?drag.cursorSnapOrigin:(typeof midiDisplayPosition==='function'?midiDisplayPosition():state.midi.position),cursor=Number.isFinite(visible)?Math.max(0,visible):0;
      if(near===undefined||Math.abs(midiX(cursor)-midiX(seconds))<Math.abs(midiX(near)-midiX(seconds)))near=cursor;
    }
    // A magnetic target must be visibly close; distant events never pull notes across phrases.
    if(near!==undefined&&Math.abs(midiX(near)-midiX(seconds))<=10)return near;
    return mode==='grid-events-cursor'?gridTime:Math.max(0,seconds);
  }
  function statusPosition(seconds,label='位置'){
    const el=$('editor-position');if(el){const text=`${label} ${fmt(seconds)} 秒 · ${secondsToBeat(seconds).toFixed(3)} 四分音符单位${lastSnapTarget!==null?` · 吸附检测拍 ${fmt(lastSnapTarget)} 秒`:''}`;if(el.textContent!==text)el.textContent=text;}
  }
  function noteAt(event,c,calibrating=false){
    if(!rollGeometry)return null;
    const {worldX:x,y}=c??coordinate(event),{high,row}=rollGeometry,index=noteRenderIndex();
    let entries=index.entries;
    if(calibrating){
      // A long held note in another pitch must not drag the interval search
      // back through every onset. Build pitch-local spans only on this path.
      if(!index.byPitch){
        index.byPitch=new Map();
        for(const entry of entries){
          const pitch=entry.note.pitch;
          if(!index.byPitch.has(pitch))index.byPitch.set(pitch,[]);
          const rows=index.byPitch.get(pitch);
          rows.push({...entry,maxEnd:Math.max(rows.at(-1)?.maxEnd??-Infinity,entry.end)});
        }
      }
      entries=index.byPitch.get(high-Math.floor(y/row))||[];
    }
    const seconds=midiSeconds(x+1e-6),before=midiSeconds(x-3);let hit=null;
    for(let i=lowerBound(entries,before,'maxEnd');i<entries.length&&entries[i].start<=seconds;i++){
      const entry=entries[i],n=entry.note;if(hit&&entry.order<=hit.order)continue;
      if(y<(high-n.pitch)*row||y>=(high-n.pitch+1)*row)continue;
      const left=midiX(n.start);if(x>=left-1e-6&&x<=Math.max(left+3,midiX(n.end)))hit=entry;
    }
    return hit?.note||null;
  }
  function axisChanged(){
    if(drag||window.AstraRhythmTimeline?.isDragging?.())abortGesture();
    clearPencilGuide();
    hoveredNote=hoveredCC=ccInsertHover=null;lastSnapTarget=null;
    noteClick=null;stopNoteLanding();window.AstraRhythmTimeline?.clearHover?.();
    if(tool==='rangeSelect')refreshInfoLine();
  }
  function stopEdgeScroll(){if(edgeFrame)cancelAnimationFrame(edgeFrame);edgeFrame=0;edgePointer=null;edgeTime=0;}
  function abortNoteGesture(){if(drag?.kind!=='drawNote')return false;abortGesture();return true;}
  function finishCreationView(){
    paintNoteCreation();
    if(typeof scheduleEditorRender==='function'&&typeof render==='function'&&render.deferred&&!noteClosing)scheduleEditorRender();
  }
  function abortGesture(){
    cancelPencilGuide();
    endVstFeedback(true);
    window.AstraRhythmTimeline?.cancel();stopCCRelease();stopNoteLanding();
    meterClick=null;noteClick=null;stopEdgeScroll();setSnapBypass(false,false);const old=drag;drag=ghost=null;ccGhost=[];paintNoteCreation();
    if(old?.kind==='drawNote')finishPencilSelection(old.pencilSelection);
    if(old)cancelledClickPointer=old.pointer;
    // Chromium may still send a trailing dblclick after capture was
    // released by Escape; it is not a new note insertion gesture.
    if(old?.kind==='note'&&(!old.edge||old.repeatEdge)&&old.everDragged)suppressNoteDragDouble=true;
    if(old?.target.hasPointerCapture(old.pointer))old.target.releasePointerCapture(old.pointer);
    if(old?.kind==='seek'&&old.target?.style)old.target.style.cursor=old.target.id==='ruler-playhead'?(window.AstraCursors?.css('grab')||'grab'):'';
    queueCursorProbe();
    // Cancelling a locator gesture restores its live fields as well as the canvas.
    if(old?.kind==='range')window.AstraTransportPanel?.render();
    if(old?.kind==='drawNote'&&$('editor-note-creation'))finishCreationView();else requestRollDraw();
  }
  function notePatchScene(){
    const rhythm=window.AstraRhythmTimeline;
    if(!active()||dialog||!['select','draw'].includes(tool)||drag&&drag.kind!=='drawNote'||
       selected.size||ccSelected.size||selectedCC||selectedBeatFlag||selectedMapPoint||tempoPreview||tempoReference||
       notePendingPreview||noteLanding||ccPendingPreview||ccRelease||rhythm?.isDragging?.()||
       rhythm?.hasPending?.()||rhythm?.selectionInfo?.()||window.AstraStructureEditor?.enabled?.())return null;
    const r=range();return [editor().tempo,editor().controls,r.start,r.end,r.loop,ccChannel,ccLaneHidden];
  }
  function noteAppendKeepsChrome(note){return Boolean(notePatchScene()&&ccChannels().includes(note.channel??0));}
  function noteChromeMeter(){return notePatchScene()?meterAt(playheadSeconds())?.index??-1:null;}
  function updateNoteCount(){
    const el=$('editor-info-note-count');if(!el||!active())return;
    const value=String(noteRows().length),changed=el.textContent!==value;
    if(el.childNodes.length===1&&el.firstChild.nodeType===3){
      if(el.firstChild.nodeValue!==value)el.firstChild.nodeValue=value;
    }else if(changed)el.textContent=value;
    if(changed&&infoFlashAllowed())flashInfoValue(el);
  }
  function repaintNoteHover(prior){
    if(typeof requestNotePatchRows!=='function'||!notePatchScene()){requestRollDraw();return;}
    const ids=[prior,hoveredNote].filter(id=>id!==null),{byId}=noteRenderIndex();
    const rows=ids.map(id=>byId.get(id)?.note);
    if(rows.some(n=>!n))requestRollDraw();else requestNotePatchRows(rows);
  }
  // One line matches the actual write target. Ctrl replaces the snapped
  // position with the free pointer position; never show both at once.
  let pencilGuideLayer=null,pencilGuideTarget=null;
  function clearPencilGuide(){
    pencilGuideTarget=null;
    if(pencilGuideLayer&&!pencilGuideLayer.hidden)pencilGuideLayer.hidden=true;
  }
  function cancelPencilGuide(){if(pencilGuideTarget)lastHover=null;clearPencilGuide();}
  function makePencilStroke(ink){
    const node=document.createElement('span');
    node.id='editor-pencil-guide';node.hidden=true;node.setAttribute('aria-hidden','true');
    node.style.cssText='position:fixed;left:0;top:0;width:'+ink.width+'px;background:'+ink.color+';pointer-events:none;z-index:3;';
    document.body.append(node);
    return node;
  }
  function placePencilStroke(node,target,x,top,height,visible){
    if(node.hidden===visible)node.hidden=!visible;
    if(!visible)return;
    const transform='translate('+Math.round(x)+'px,'+Math.round(top)+'px)';
    if(node.style.transform!==transform)node.style.transform=transform;
    if(node.style.height!==height)node.style.height=height;
    if(node.dataset.lane!==target.id)node.dataset.lane=target.id;
  }
  function paintPencilGuide(target,c,seconds,free,stopY){
    const ink=window.AstraRhythmTimeline?.pencilGuideInk;
    if(!ink||!c||!target?.isConnected||dialog||tab!=='notes'){clearPencilGuide();return;}
    const {rect}=c,clip=target.id==='midi-canvas'?$('roll-scroll')?.getBoundingClientRect():rect;
    if(!clip){clearPencilGuide();return;}
    const x=rect.left+(free?c.x:midiX(seconds)-c.offset);
    const top=Math.max(rect.top,clip.top),bottom=Math.min(rect.bottom,clip.bottom);
    const left=Math.max(rect.left,clip.left),right=Math.min(rect.right,clip.right);
    if(!Number.isFinite(x)||x<left||x>=right||bottom<=top){clearPencilGuide();return;}
    pencilGuideLayer??=makePencilStroke(ink);
    pencilGuideTarget=target;
    // Presentation only: the guide may stop short (stopY) but keeps its x/target.
    const end=Number.isFinite(stopY)?Math.max(top,Math.min(bottom,stopY)):bottom;
    placePencilStroke(pencilGuideLayer,target,x,top,Math.round(end-top)+'px',true);
  }
  // The velocity pencil uses the timing grid visually, independent of
  // the nearby-onset hit-test. Stop the line above any stem it would cover.
  const velocityGuideTiming={mode:'grid'};
  function previewPencilGuide(event,c,kind,timing=null){
    const saved=lastSnapTarget;
    const free=!snapOn(event,kind,timing);
    const note=event.target.id==='midi-canvas'&&kind==='draw'?newNoteAt(c,event,timing??undefined):null;
    const seconds=note?.start??snapped(c.seconds,event,null,kind,timing);
    lastSnapTarget=saved; // advisory hover never overwrites a gesture target
    const head=event.target.id==='velocity-canvas'?velocityHeadTop(free?c.worldX:midiX(seconds)):Infinity;
    paintPencilGuide(event.target,c,seconds,free,Number.isFinite(head)?c.rect.top+head-3:undefined);
  }
  const cursorSurfaces=new Set(['ruler-playhead','midi-canvas','pedal-canvas','velocity-canvas','ruler-canvas','mapping-canvas','waveform-canvas','beat-flags-canvas','opening-canvas']);
  let cursorProbe=null,cursorProbePosted=false;
  const canProbeCursor=()=>tab==='notes'&&!drag&&!window.AstraRhythmTimeline?.isDragging?.()&&!document.hidden&&!noteClosing;
  // Advisory re-hit only. Press, drag, changed release samples and modifier
  // input still resolve synchronously against live geometry.
  function queueCursorProbe(event){
    if(event?.target)lastHover=pointerState(event);
    const sample=lastHover;
    if(!canProbeCursor()||!cursorSurfaces.has(sample?.target?.id)||sample.target.isConnected===false){
      if(pencilGuideTarget&&(!pencilGuideTarget.isConnected||!active()||dialog||tab!=='notes'))clearPencilGuide();
      return;
    }
    const view=$('roll-scroll');if(!view)return;
    cursorProbe={sample,view};
    if(cursorProbePosted)return;
    cursorProbePosted=true;
    // A task after rAF gives rendering an opportunity. It is not a guarantee
    // of clean layout, and neither a gesture nor a receipt waits for it.
    requestAnimationFrame(()=>{setTimeout(runCursorProbe,0);});
  }
  function runCursorProbe(){
    const work=cursorProbe;cursorProbe=null;cursorProbePosted=false;
    if(!work||work.sample!==lastHover||!canProbeCursor()||work.view!==$('roll-scroll')||work.sample.target.isConnected===false)return;
    const {sample}=work;
    // Capture can report the old lane on release, even outside its bounds.
    const target=typeof document.elementFromPoint==='function'?document.elementFromPoint(sample.clientX,sample.clientY):sample.target;
    if(!target)lastHover=null;
    const snapTarget=lastSnapTarget;
    try{updateCursor({...sample,target});}finally{lastSnapTarget=snapTarget;}
  }
  function updateCursor(event,knownBeatFlag){
    cursorProbe=null; // Real input supersedes a queued cosmetic re-hit.
    if(event?.target)lastHover=pointerState(event);
    const priorHover=hoveredCC,priorNote=hoveredNote,priorInsert=ccInsertHover;hoveredNote=null;ccInsertHover=null;
    hoveredCC=null;
    const target=event?.target;
    if(!target?.style||!cursorSurfaces.has(target.id)){
      clearPencilGuide();
      if(priorNote!==null){if(priorHover!==null||priorInsert!==null)requestRollDraw();else repaintNoteHover(priorNote);}
      else if(priorHover!==null||priorInsert!==null)requestCCDraw();
      return;
    }
    const notePen=target.id==='midi-canvas'&&(tool==='draw'||tool==='select'&&event.altKey)&&canDrawNote();
    let cursor='default',pencil=null;
    if(drag?.kind==='selectionRange'){
      const part=drag.rLiveAlt?(event.altKey?'copy_body':'move_body'):(drag.rPaint?.part??drag.rPart);
      cursor=part==='move_body'?'grabbing':part==='copy_body'?'copy':['start','end','extend_start','extend_end'].includes(part)?'ew-resize':'range';
    }
    else if(drag){cursor=drag.kind==='seek'?'grabbing':drag.kind==='note'?drag.repeatEdge?'pen':drag.edge?'ew-resize':drag.copy?'copy':'grabbing':drag.kind==='control'?(drag.creating?'pen':'grabbing'):drag.kind==='velocity'?'velocity':drag.kind==='velocityDraw'?'pen':drag.kind==='range'?(drag.edge?'ew-resize':drag.moving?'grabbing':'crosshair'):['map','beat'].includes(drag.kind)?'grabbing':drag.kind.startsWith('draw')?'pen':['box','ccBox'].includes(drag.kind)?(drag.moved?'range':'default'):'default';}
    else if(target.id==='ruler-playhead'||target.id==='waveform-canvas'||(target.id==='opening-canvas'&&editable()))cursor='grab';
    else if(tool==='eraseBeat'&&target.id==='beat-flags-canvas'&&active()){
      const reason=window.AstraRhythmTimeline?.eraseBlockAt?.(event);
      cursor=reason?'not-allowed':'erase';target.title=reason||'点击或扫过拍点；松开保存，一次手势可一次撤销。密集处可放大。';
    }
    else if(target.id==='velocity-canvas'&&['select','draw'].includes(tool)&&configEditable()){
      if(editable()||(velocityReleaseOwner&&editOwner===velocityReleaseOwner)){
        cursor='pen';pencil={c:coordinate(event),kind:'velocity'};
      }else cursor='progress';
    }
    else if(editable()||notePen||target.id==='pedal-canvas'&&ccCanContinue()){
      const c=coordinate(event);
      if(tool==='rangeSelect'&&selectionSurfaces.has(target.id)){
        const {part}=selectionHit(c,event,target.id);
        cursor=part==='move_body'?'grab':part==='copy_body'?'copy':['start','end','extend_start','extend_end','shift_inside'].includes(part)?'ew-resize':'range';
        target.title='拖边缘调整选区 · 拖内部移动内容 · Alt 复制 · Ctrl / B 临时自由定位';
      }else if(target.id==='midi-canvas'&&tool!=='map'){
        const note=noteAt(event,c);if(note&&!['map','beat','meter','eraseBeat'].includes(tool))hoveredNote=note.id;
        if(['map','beat','meter','eraseBeat'].includes(tool))cursor=note?'pointer':tool==='beat'?'beat':'default';
        else if(!note&&(tool==='draw'||event.altKey)){cursor='crosshair';if(notePen&&rollGeometry?.canvas===target)pencil={c,kind:'draw'};}
        else if(note){
           const edge=noteResizeHit(note,c);
           cursor=event.altKey?tool==='select'&&edge==='end'?'pen':'copy':(event.ctrlKey||event.metaKey)&&event.shiftKey?'velocity':edge?'ew-resize':'grab';
         }
      }else if(target.id==='pedal-canvas'&&tool!=='map'){
        const nearest=ccPointAt(c),point=nearest?.cc;
        if(tool==='draw'||(event.altKey&&!(tool==='select'&&point))){cursor='crosshair';if(editable()&&['select','draw'].includes(tool))pencil={c,kind:'cc'};}
        else {
          if(point){cursor=event.altKey?'copy':'grab';hoveredCC=point.id;}
          else {const line=ccInsertLineAt(c,event,nearest);if(line){cursor='pen';ccInsertHover={seconds:snapped(c.seconds,event,null,'cc'),value:line.value};}}
          target.title=ccInsertHover?'按下拖动创建踏板点 · Ctrl 自由定位 · Shift 框选':point?'自由拖动踏板点 · Ctrl 或 B 临时取消时间吸附':'';
        }
      }else if(target.id==='ruler-canvas'){
        // A movable meter label offers a grab; the initial meter and the rest of
        // the lane stay a click target (select / double-click to edit).
        if(target.id==='ruler-canvas'&&c.y>=16){const meter=c.y<=33?window.AstraRhythmTimeline?.meterHit?.(c.x):null;const hint=meter?.name||'';if(target.title!==hint)target.title=hint;cursor=meter&&!meter.fixed?'grab':'pointer';}
        else {const r=range(),left=midiX(r.start)-($('roll-scroll')?.scrollLeft||0),right=midiX(r.end)-($('roll-scroll')?.scrollLeft||0);cursor=hasRange()&&(Math.abs(c.x-left)<8||Math.abs(c.x-right)<8)?'ew-resize':hasLocators()&&c.seconds>Math.min(r.start,r.end)&&c.seconds<Math.max(r.start,r.end)?'grab':'default';}
      }else if(target.id==='beat-flags-canvas'){const flag=knownBeatFlag===undefined?flagAt(c.x,c.y):knownBeatFlag;cursor=flag?flag.locked?'not-allowed':'grab':'default';const value=window.AstraRhythmTimeline?.durationInfo?.(flag);target.title=flag?`拍点 ${flag.number} · ${value?.symbol||''} ${value?.label||''} · ${fmt(flag.seconds)} 秒${flag.last?' · 末尾计划时值，下一点尚未写入':''}`:tool==='beat'?'单击空白新增拍点 · Ctrl 暂停时间吸附':'拖动已有拍点 · 空白处框选';
      }else if(target.id==='mapping-canvas'&&tool==='map')cursor='ew-resize';
    }
    if(!drag&&(editable()||notePen)){
      if(tool==='eraseBeat'&&(['midi-canvas','pedal-canvas'].includes(target.id)||(target.id==='ruler-canvas'&&coordinate(event).y>=16&&coordinate(event).y<=33)))cursor='erase';
      else if(tool==='beat'&&target.id==='beat-flags-canvas'&&cursor==='default')cursor='beat';
      else if(cursor==='crosshair'&&(tool==='draw'||event.altKey))cursor='pen';
      else if(tool==='rangeSelect'&&target.id==='beat-flags-canvas')cursor='range';
    }
    if(!drag&&!editable()&&(!notePen||hoveredNote!==null)&&['midi-canvas','pedal-canvas'].includes(target.id)&&(event.altKey||target.id==='midi-canvas'&&tool==='draw')){
      // A receipt in flight is not a prohibited gesture. Keep the accepted
      // copy affordance while it lands; other in-flight note gestures rest
      // at neutral until ordinary hit-testing can run again.
      if(configEditable()&&editLocked()&&target.id==='midi-canvas'&&['select','draw','rangeSelect'].includes(tool)){
        const noteCopy=notePendingPreview?.copy&&(notePaintCurrent(notePendingPreview)||ownNoteLandingPublication(notePendingPreview));
        const rangeCopy=selectionFlight?.command?.part==='copy_body';
        cursor=event.altKey&&(noteCopy||rangeCopy)?'copy':'default';target.title='';
      }else{cursor='not-allowed';target.title=editLocked()?'正在保存，完成后可继续编辑':active()?'当前任务运行中，暂不能编辑':'只读浏览：切换到编辑模式后才能新增或修改';}}
    if(!drag&&editLocked()&&target.id==='pedal-canvas'&&(!ccCanContinue()||event.altKey||!ccPointAt(coordinate(event)))){cursor='progress';target.title='正在保存踏板修改，完成后可继续拖动';}
    if(target.id==='pedal-canvas'&&cursor==='default'&&target.title)target.title='';
    const styled=window.AstraCursors?.css(cursor)||(['pen','beat','erase','range'].includes(cursor)?'crosshair':cursor);
    if(target.style.cursor!==styled)target.style.cursor=styled;
    if(pencil?.kind==='velocity')previewPencilGuide(event,pencil.c,'draw',velocityGuideTiming);
    else if(pencil&&cursor!=='not-allowed'&&cursor!=='progress'){
      // A bare click on the playback head is seek, not pen entry.
      const playX=midiX(typeof midiDisplayPosition==='function'?midiDisplayPosition():(state.midi.position||0))-pencil.c.offset;
      if(event.altKey||event.ctrlKey||event.metaKey||Math.abs(pencil.c.x-playX)>7)previewPencilGuide(event,pencil.c,pencil.kind);
      else clearPencilGuide();
    }else if(!drag||!['drawNote','drawCC','velocityDraw'].includes(drag.kind)&&!(drag.kind==='note'&&(!drag.edge||drag.resizeInkIds)))clearPencilGuide();
    const ccChanged=priorHover!==hoveredCC||priorInsert?.seconds!==ccInsertHover?.seconds||priorInsert?.value!==ccInsertHover?.value;
    if(priorNote!==hoveredNote){if(ccChanged)requestRollDraw();else repaintNoteHover(priorNote);}
    else if(ccChanged)requestCCDraw();
  }
  // Modifier-only changes must not wait for the next mouse movement. Keep this
  // local: rebuilding toolbar/inspector DOM here delays the native cursor.
  function modifierCursor(event){
    if(!['Alt','Control','Meta','Shift'].includes(event.key)||event.repeat||event.isComposing||tab!=='notes'||dialog||!lastHover)return;
    if(drag){
      if(event.key==='Alt'&&(drag.kind==='note'&&drag.liveAlt||drag.kind==='selectionRange'&&drag.rLiveAlt)&&drag.lastEvent){
        const next={...drag.lastEvent,altKey:event.type==='keydown'};
        pointerMove(next,true);return;
      }
      if(event.key==='Alt'&&['drawNote','drawCC'].includes(drag.kind)){
        if(drag.lastEvent){const next={...drag.lastEvent,altKey:event.type==='keydown'};pointerMove(next,true);}
        return;
      }
      if(['note','drawNote','control','drawCC','selectionRange','velocityDraw'].includes(drag.kind)&&['Control','Meta'].includes(event.key)&&drag.lastEvent){const next={...drag.lastEvent};next[event.key==='Control'?'ctrlKey':'metaKey']=event.type==='keydown';pointerMove(next,true);}
      return;
    }
    // The rhythm timeline owns its drag cursor (grabbing / occupied target);
    // a stale pre-press hover sample must not repaint it on Ctrl/Meta.
    if(window.AstraRhythmTimeline?.isDragging?.())return;
    if(event.target?.closest?.('input,textarea,select,[contenteditable=true]'))return;
    const hover=lastHover;
    const next={target:hover.target,pointerId:hover.pointerId,clientX:hover.clientX,clientY:hover.clientY,
      altKey:Boolean(event.altKey),ctrlKey:Boolean(event.ctrlKey),metaKey:Boolean(event.metaKey),shiftKey:Boolean(event.shiftKey)};
    const field={Alt:'altKey',Control:'ctrlKey',Meta:'metaKey',Shift:'shiftKey'}[event.key];
    next[field]=event.type==='keydown';
    updateCursor(next);
  }
  function resetModifierCursor(){
    if(lastHover)updateCursor({target:lastHover.target,pointerId:lastHover.pointerId,clientX:lastHover.clientX,clientY:lastHover.clientY,altKey:false,ctrlKey:false,metaKey:false,shiftKey:false});
    cancelPencilGuide();
  }
  function edgeVelocity(position,start,end){
    const zone=Math.min(32,(end-start)/4);
    if(!(zone>0))return 0;
    if(position<start+zone)return -Math.min(1,(start+zone-position)/zone)*720;
    if(position>end-zone)return Math.min(1,(position-end+zone)/zone)*720;
    return 0;
  }
  function edgeMotion(){
    const scroll=$('roll-scroll');if(!scroll?.getBoundingClientRect||!edgePointer||!drag||drag.kind==='seek'||drag.kind==='velocity'||!drag.moved)return null;
    const rect=scroll.getBoundingClientRect(),left=rect.left+(scroll.clientLeft||0),top=rect.top+(scroll.clientTop||0);
    let x=edgeVelocity(edgePointer.clientX,left,left+scroll.clientWidth),y=0;
    if(drag.kind==='box'||(drag.kind==='note'&&!drag.edge))y=edgeVelocity(edgePointer.clientY,top,top+scroll.clientHeight);
    if((x<0&&scroll.scrollLeft<=0)||(x>0&&scroll.scrollLeft>=scroll.scrollWidth-scroll.clientWidth))x=0;
    if((y<0&&scroll.scrollTop<=0)||(y>0&&scroll.scrollTop>=scroll.scrollHeight-scroll.clientHeight))y=0;
    return x||y?{scroll,x,y}:null;
  }
  function edgeTick(time){
    edgeFrame=0;
    if(!drag||!gestureAlive(drag)||drag.target.isConnected===false){abortGesture();return;}
    const motion=edgeMotion();if(!motion){stopEdgeScroll();return;}
    const elapsed=edgeTime?Math.min(40,Math.max(1,time-edgeTime)):16;edgeTime=time;
    const {scroll,x,y}=motion,oldX=scroll.scrollLeft,oldY=scroll.scrollTop;
    scroll.scrollLeft=Math.max(0,Math.min(scroll.scrollWidth-scroll.clientWidth,oldX+x*elapsed/1000));
    scroll.scrollTop=Math.max(0,Math.min(scroll.scrollHeight-scroll.clientHeight,oldY+y*elapsed/1000));
    if(scroll.scrollLeft===oldX&&scroll.scrollTop===oldY){stopEdgeScroll();return;}
    pointerMove(edgePointer,true);
    if(drag&&!edgeFrame)edgeFrame=requestAnimationFrame(edgeTick);
  }
  function trackEdgePointer(){
    edgePointer=drag.lastEvent;
    if(edgeMotion()){if(!edgeFrame)edgeFrame=requestAnimationFrame(edgeTick);}
    else stopEdgeScroll();
  }
  function paintSeekPreview(at){if(typeof paintMidiPlayhead==='function')paintMidiPlayhead(at);else {const cursor=$('midi-cursor'),wave=$('waveform-cursor');if(cursor)cursor.style.transform=`translateX(${midiX(at)}px)`;if(wave)wave.style.transform=`translateX(${midiX(at)-($('roll-scroll')?.scrollLeft||0)}px)`;}if(typeof paintPlaybackPosition==='function')paintPlaybackPosition(at);}
  const wireKinds=new Set(['drawNote','note','drawCC','control']);
  const wireCursor=()=>{const s=playheadSeconds();return Number.isFinite(s)?Math.max(0,s):0;};
  const wireSnap=t=>({enabled:t?.enabled??snapEnabled,mode:t?.mode??snap,unit:t?.unit??snapUnit()});
  const wirePoint=(c,e)=>({x:c.worldX,y:c.y,bypass:Boolean(e.ctrlKey||e.metaKey||temporarySnapBypass)});
  function gestureInput(c,event,kind,timing){
    const a=rollAxis(),press=wirePoint(c,event),pedal=kind==='control'||kind==='drawCC';
    const detected=detectedBeats();
    return {axis:{mode:a.mode,width:a.width,seconds_extent:a.duration,beat_extent:a.beatExtent},
      press,snap:wireSnap(timing),cursor:wireCursor(),detected:detected.available?detected.cache_key:null,
      geometry:pedal?pedalGeometry():{row_height:rollGeometry.row,high_pitch:rollGeometry.high},
      channel:pedal?ccChannel:null,samples:kind==='drawCC'?[press]:[],last:press,
      preset:qState.preset,length:lengthQuantize==='link'?'link':Number(lengthQuantize),exclusions:[...selected]};
  }
  function wireSample(d,c,event){
    const w=d.wire,s=wirePoint(c,event),cfg=w.channel==null?w.snap:wireSnap();
    if(w.channel!=null)s.snap=cfg;
    if(cfg.mode==='cursor'||cfg.mode==='grid-events-cursor')s.cursor_seconds=wireCursor();
    return s;
  }
  function gestureOperation(w,command){
    const context={axis:w.axis,cursor_seconds:w.cursor};
    const detected=s=>s?.enabled&&s.mode==='detected';
    if(w.detected&&(detected(command.snap)||command.samples?.some(s=>detected(s.snap))))context.detected_cache_key=w.detected;
    return {type:'note_semantic',command,context};
  }
  function noteCreateOperation(w,velocity){
    return gestureOperation(w,{action:'note_create',press:w.press,...w.geometry,preset:w.preset,length:w.length,velocity,
      snap:w.snap,exclude_note_ids:w.exclusions,...(w.current?{current:w.current}:{})});
  }
  function gestureAlive(g){
    if(g.kind==='selectionRange'){
      const a=rollAxis(),w=g.rContext.axis;
      return g.rView===selectionViewSerial&&tool==='rangeSelect'&&editable()&&!noteNavigation&&!noteClosing&&
        !state.project_opening?.pending&&g.target.isConnected!==false&&g.target===$(g.target.id)&&
        a.mode===w.mode&&a.width===w.width&&a.duration===w.seconds_extent&&a.beatExtent===w.beat_extent&&
        rangeCovers(g.rScope,g.target.id)&&semanticCurrent(g.rBase);
    }
    if(g.wire){
      if(tab!=='notes'||g.target.isConnected===false||($(g.target.id)&&g.target!==$(g.target.id))||
         g.gestureProject!==stateProjectPath()||noteNavigation||noteClosing||!active()||
         state.project_opening?.pending||state.gpu_preflight||state.job.running&&!state.job.editor_export)return false;
      const w=g.wire,a=rollAxis();
      if(a.mode!==w.axis.mode||a.width!==w.axis.width||a.duration!==w.axis.seconds_extent||a.beatExtent!==w.axis.beat_extent)return false;
      if(w.channel!=null){
        const geom=pedalGeometry();if(w.channel!==ccChannel||geom.span!==w.geometry.span||geom.bottom!==w.geometry.bottom)return false;
      }else if(w.geometry.row_height!==rollGeometry?.row||w.geometry.high_pitch!==rollGeometry?.high)return false;
      if(g.noteScope)return g.target===$('midi-canvas')&&canDrawNote()&&noteProtocol().sameBase(g.noteScope,noteWriteScope());
      if(g.continuation)return ccPendingAlive(g.continuation);
      if(g.gestureScope&&!sameGestureBase(g.gestureScope,noteWriteScope()))return false;
    }
    return tab==='notes'&&g.midiPath===state.midi.path&&(['seek','range'].includes(g.kind)?Boolean(state.midi.path):active()&&g.base_revision===editor()?.revision);
  }
  function capture(event,data){
    stopCCRelease();stopNoteLanding();stopEdgeScroll();ccGhost=[];
    const c=data.pressCoordinate??coordinate(event);
    drag={...data,cursorSnapOrigin:data.kind==='seek'?(typeof midiDisplayPosition==='function'?midiDisplayPosition():(state.midi.position||0)):null,
      base_revision:editor()?.revision,midiPath:state.midi.path,pointer:event.pointerId,target:event.target,originX:event.clientX,originY:event.clientY,originContentY:data.originContentY??c.y,moved:false};
    if(data.kind==='note'&&data.edge&&!data.repeatEdge)drag.resizeInkIds=new Set(data.notes.map(n=>n.id));
    if(wireKinds.has(data.kind)){
      drag.wire=gestureInput(c,event,data.kind,data.noteTiming);drag.gestureScope=noteWriteScope();drag.gestureProject=stateProjectPath();
      // Prepare the existing revision index at press, not during the first move.
      // Releasing Ctrl/B can enable snapping later in this same gesture.
      if((data.noteTiming?.mode??snap).includes('events'))eventSnapPoints();
    }
    drag.lastEvent=pointerState(event);event.target.setPointerCapture(event.pointerId);updateCursor(event);if(!data.nativeMouse)event.preventDefault();event.stopImmediatePropagation();suspendRollFollow();if(data.kind==='seek')paintSeekPreview(data.at);
    if(data.kind==='drawNote'){paintPencilGuide(event.target,c,data.note.start,!snapOn(event,'draw',data.noteTiming));}
    else if(data.kind==='drawCC')paintPencilGuide(event.target,c,data.points[0].seconds,!snapOn(event,'cc'));
    else if(drag.resizeInkIds)paintPencilGuide(event.target,c,data.note[data.edge],false);
    if(!data.toggleOff&&['drawNote','note','noteSelection','velocity'].includes(data.kind))auditionVstNote(data.note,drag);
  }
  function boxIndex(){return noteRenderIndex().entries;}
  // Preserve the grab offset in projected pixels, then invert at the target.
  // Adding a pointer's seconds delta to the note edge slips at tempo changes.
  function dragSeconds(c,pressed,reference){
    return c.seconds===pressed?reference:midiSeconds(midiX(reference)+(c.worldX-midiX(pressed)));
  }
  const movedStart=(note,move)=>move.edge==='end'?note.start:Math.max(0,note.start+move.delta);
  const movedEnd=(note,move)=>move.edge==='start'?note.end:note.end+move.delta;
  function setNoteMove(d,delta,pitch){
    if(d.noteMove?.delta===delta&&d.noteMove.pitch===pitch)return;
    const move={delta,pitch,edge:d.edge},margin=d.bounds.minLen+(d.edge==='start'?-delta:d.edge==='end'?delta:0);
    const scale=Math.max(1,d.bounds.end+Math.abs(delta));
    // Cheap proof for ordinary moves; near cancellation use the exact stored
    // double formula. This is not a minimum note length.
    if(!(margin>4*Number.EPSILON*scale)&&!d.notes.every(n=>validSpan(movedStart(n,move),movedEnd(n,move)))){
      if(!d.edge)d.noteMove=null;
      return;
    }
    d.noteMove=move;
  }
  function activeNoteResize(){const d=drag;return d?.resizeInkIds&&d.noteMove&&d.noteMove.delta!==0&&d.target===$('midi-canvas')?d:null;}
  function noteMovePaint(d){
    if(!d.noteMove)return null;
    const move=d.noteMove,rows=[];
    for(const n of d.notes){
      const start=movedStart(n,move),end=movedEnd(n,move),pitch=n.pitch+move.pitch;
      if(d.copy){const {id,...rest}=n;rows.push({...rest,start,end,pitch});}
      else if(start!==n.start||end!==n.end||pitch!==n.pitch)rows.push({id:n.id,start,end,pitch});
    }
    return rows.length?{type:'notes',[d.copy?'add':'updates']:rows}:null;
  }
  // Position-only clones always use the ordinary velocity palette, not selection ink.
  function paintPositionNote(ctx,n,x,y,w,h,left=true,right=true,color=selectionNoteColor(n),pitch=n?.pitch){
    if(w<=0||h<=0)return;
    ctx.fillStyle=color;ctx.fillRect(x,y,w,h);
    ctx.fillStyle='rgba(46,56,88,.5)';
    ctx.fillRect(x,y,w,1);ctx.fillRect(x,y+h-1,w,1);
    if(left)ctx.fillRect(x,y,1,h);if(right)ctx.fillRect(x+w-1,y,1,h);
    if(w>=5&&h>=7){ctx.fillStyle='rgba(255,255,255,.16)';ctx.fillRect(x,y,w,1);}
    if(left)paintOrdinaryPitch(ctx,pitch,x,y,w,h,color);
  }
  function drawNoteMove(ctx,d,offset,height,startSeconds,endSeconds){
    const move=d.noteMove;
    if(!d.moveIndex){
      const rows=d.notes.map((note,order)=>({note,order,maxEnd:0})).sort((a,b)=>a.note.start-b.note.start);
      let end=-Infinity;for(const row of rows)row.maxEnd=end=Math.max(end,row.note.end);
      d.moveIndex=rows;
    }
    const rows=d.moveIndex,endShift=move.edge==='start'?0:move.delta,visible=[];
    let lo=0,hi=rows.length;
    while(lo<hi){const mid=(lo+hi)>>>1;if(rows[mid].maxEnd+endShift<startSeconds)lo=mid+1;else hi=mid;}
    for(let i=lo;i<rows.length;i++){
      const entry=rows[i],n=entry.note,start=movedStart(n,move);
      if(start>endSeconds)break;
      if(movedEnd(n,move)<startSeconds)continue;
      const y=(rollGeometry.high-n.pitch-move.pitch)*rollGeometry.row+1;
      if(y+rollGeometry.row>=0&&y<=height)visible.push(entry);
    }
    visible.sort((a,b)=>a.order-b.order); // same ordinary note stacking order
    ctx.save();
    for(const {note:n} of visible){
      const y=(rollGeometry.high-n.pitch-move.pitch)*rollGeometry.row+1,x=midiX(movedStart(n,move))-offset;
      const w=Math.max(2,midiX(movedEnd(n,move))-offset-x),h=rollGeometry.row-2;
      paintPositionNote(ctx,n,x,y,w,h,true,true,selectionNoteColor(n),n.pitch+move.pitch);
    }
    ctx.restore();
  }
  function requestNoteFeedback(){if(typeof requestNoteDragDraw==='function')requestNoteDragDraw();else requestRollDraw();}
  let noteLanding=null;
  function stopNoteLanding(){noteLanding=null;window.AstraRhythmMotion?.stopSettle('note-target');}
  function startNoteLanding(d,preview=null){
    const aim=preview?preview.snap_aim:d.snapAim,move=preview?preview.move:d.noteMove;
    if(aim==null||!move){if(preview)stopNoteLanding();return;}
    const seconds=preview?preview.at:d.note[d.edge||'start']+move.delta,pitch=d.note.pitch+(preview?move.pitch_delta:move.pitch);
    if(noteLanding?.canvas===d.target&&noteLanding.seconds===seconds&&noteLanding.pitch===pitch)return;
    stopNoteLanding();
    const landing={seconds,pitch,project:stateProjectPath(),canvas:d.target,alpha:1};
    if(window.AstraRhythmMotion?.settle('note-target',90,(ease,t)=>{noteLanding=t===1?null:landing;landing.alpha=1-ease;},requestNoteFeedback))noteLanding=landing;
  }
  function drawNoteTarget(ctx,offset,width){
    const d=drag?.kind==='note'?drag:null;
    const landing=d?.noteMove&&d.snapAim!=null?{seconds:d.note[d.edge||'start']+d.noteMove.delta,pitch:d.note.pitch+d.noteMove.pitch,alpha:1}:noteLanding;
    if(!landing||landing.canvas&&(landing.canvas!==rollGeometry.canvas||landing.project!==state.project?.path))return;
    const x=midiX(landing.seconds)-offset,y=(rollGeometry.high-landing.pitch+.5)*rollGeometry.row;
    if(x< -4||x>width+4)return;
    ctx.save();ctx.globalAlpha=landing.alpha;ctx.fillStyle='#4f6bc0';ctx.fillRect(Math.round(x),y-rollGeometry.row/2,1,rollGeometry.row);
    if(d?.snapAim!=null){const raw=midiX(d.snapAim)-offset;ctx.globalAlpha*=.4;ctx.fillRect(Math.min(raw,x),y,Math.abs(raw-x),1);}
    ctx.restore();
  }
  const noteResizeHit=(note,c)=>{
    const left=midiX(note.start)-c.offset,right=midiX(note.end)-c.offset;
    return right-left>14&&Math.abs(c.x-left)<5?'start':Math.abs(c.x-right)<6?'end':null;
  };
  function noteRepeatFrame(bounds){
    const axis=rollAxis(),bars=axis.mode==='bars',toUnit=bars?axis.toBeat:x=>x,toTime=bars?axis.toSeconds:x=>x;
    const lead=toUnit(bounds.start),tail=toUnit(bounds.end),step=tail-lead;
    return {toUnit,toTime,tail,step,limit:toUnit(duration()),count:0,end:bounds.end};
  }
  function previewNoteRepeat(d,c){
    const f=d.repeatEdge,ref=d.bounds.end,raw=dragSeconds(c,d.at,ref);
    // The group span itself is the repeat grid: the first complete copy
    // previews just past the source, and every additional span adds one.
    const reach=f.toUnit(raw)-f.tail;
    const fits=Math.floor((f.limit-f.tail)/f.step+1e-9);
    f.count=f.step>0&&reach>1e-9?Math.max(0,Math.min(999,Math.floor(20000/d.notes.length),fits,1+Math.floor(reach/f.step+1e-9))):0;
    f.end=f.count?f.toTime(f.tail+f.count*f.step):ref;
    requestNoteFeedback();
  }
  function noteRepeatCopies(d){
    const {count,step,toTime,toUnit}=d.repeatEdge;
    const add=[];
    for(let i=1;i<=count;i++)for(const {id,start,end,...rest} of d.notes){
      const a=toTime(toUnit(start)+step*i),b=toTime(toUnit(end)+step*i);
      if(!validSpan(a,b)||a<0||b>duration()+1e-6)return [];
      add.push({...rest,start:a,end:b});
    }
    return add;
  }
  function drawNoteRepeat(ctx,d,offset,height,width){
    const f=d?.repeatEdge;if(!f?.count)return;
    const {high,row}=rollGeometry;
    const a=midiX(d.bounds.start)-offset,b=midiX(f.end)-offset,top=(high-d.bounds.high)*row+1,bottom=(high-d.bounds.low+1)*row-1;
    if(b<0||a>width||bottom<0||top>height)return;
    ctx.save();
    ctx.strokeStyle='rgba(48,65,94,.55)';ctx.lineWidth=3;
    ctx.strokeRect(a+.5,top+.5,Math.max(1,b-a-1),Math.max(1,bottom-top-1));
    ctx.strokeStyle='#fff';ctx.lineWidth=1;
    ctx.strokeRect(a+.5,top+.5,Math.max(1,b-a-1),Math.max(1,bottom-top-1));
    const label='\u91cd\u590d\u6570\uff1a'+f.count;
    ctx.font='11px AstraSans,sans-serif';ctx.textBaseline='middle';ctx.textAlign='left';
    const w=ctx.measureText(label).width+12,x=Math.max(0,Math.min(width-w-1,b+5)),y=Math.max(1,Math.min(height-19,bottom+5));
    ctx.fillStyle='#fff';ctx.fillRect(x,y,w,17);
    ctx.strokeStyle='rgba(48,65,94,.50)';ctx.strokeRect(x+.5,y+.5,w-1,16);
    ctx.fillStyle='#374155';ctx.fillText(label,x+6,y+8.5);
    ctx.restore();
  }
  function pointerDown(event){
    // A fresh press owns a new click; a cancelled press retains its click until then.
    cancelledClickPointer=null;suppressNoteDragDouble=false;
    // A fresh canvas may not have been sized by the next animation frame yet.
    // The lane remains an interactive surface; normalize its blank area to canvas.
    if(event.target?.matches?.('.editor-beat-flags,.editor-opening')){
      const target=event.target.querySelector('canvas'),rect=target?.getBoundingClientRect();
      if(target&&event.clientX>=rect.left){const native=event;event={target,button:native.button,pointerId:native.pointerId,clientX:native.clientX,clientY:native.clientY,ctrlKey:native.ctrlKey,metaKey:native.metaKey,altKey:native.altKey,shiftKey:native.shiftKey,type:native.type,preventDefault:()=>native.preventDefault(),stopImmediatePropagation:()=>native.stopImmediatePropagation()};}
    }
    if(event.button!==0||tab!=='notes'||!state.midi.path)return;
    const id=event.target.id;if(!['ruler-playhead','ruler-canvas','midi-canvas','pedal-canvas','velocity-canvas','mapping-canvas','waveform-canvas','beat-flags-canvas','opening-canvas'].includes(id))return;
    if(id==='beat-flags-canvas'&&!active())return;
    if(!drag&&['midi-canvas','pedal-canvas'].includes(id)&&event.button===0&&event.isPrimary!==false&&
       !event.shiftKey&&!event.altKey&&!event.ctrlKey&&!event.metaKey&&pendingRangeGesture()){
      rangeDismissClick={pointer:event.pointerId,x:event.clientX,y:event.clientY,flight:selectionFlight,view:selectionViewSerial};
      try{event.target.setPointerCapture?.(event.pointerId);}catch(_){}
      cancelledClickPointer=event.pointerId;event.preventDefault();event.stopImmediatePropagation();return;
    }
    const wantsNote=id==='midi-canvas'&&(tool==='draw'||tool==='select'&&event.altKey);
    if(wantsNote&&(!rollGeometry||rollGeometry.canvas&&rollGeometry.canvas!==event.target||event.isPrimary===false||drag)){
      if(!drag&&event.isPrimary!==false){noteMessage='音符视图尚未就绪；本次未写入。';paintNoteEntryStatus();}
      cancelledClickPointer=event.pointerId;event.preventDefault();event.stopImmediatePropagation();return;
    }
    if(drag?.wire||drag?.kind==='selectionRange'){event.preventDefault();event.stopImmediatePropagation();return;}
    // The existing CC continuation owns its own transaction. A note flight
    // neither lends its base to CC nor changes the CC drawing/settle rules.
    if(id==='pedal-canvas'&&editLocked()&&!(ccCanContinue()&&!event.altKey&&ccPointAt(coordinate(event)))){updateCursor(event);const feedback=$('editor-position');if(feedback)feedback.textContent='正在保存踏板修改，完成后可继续拖动';event.preventDefault();event.stopImmediatePropagation();return;}
    if(id!=='midi-canvas')noteClick=null;
    selectionDomainFromTarget(id);
    event.target.tabIndex=-1;event.target.focus?.({preventScroll:true});
    const c=coordinate(event),canEdit=editable(),canInsert=wantsNote&&canDrawNote();
    const note=id==='midi-canvas'&&tool!=='rangeSelect'&&(canEdit||canInsert||tool==='beat'&&active())?noteAt(event,c):null;
    const independent=canInsert&&!note;
    // The beat lane and empty roll under the beat pencil belong to the rhythm
    // controller before seek/box handling. Existing notes keep their own hit path.
    if((id==='beat-flags-canvas'||id==='midi-canvas'&&tool==='beat'&&!note)&&window.AstraRhythmTimeline?.down(event,id==='midi-canvas',c))return;
    const seconds=id==='ruler-canvas'||id==='opening-canvas'?snapped(c.seconds,event):c.seconds;
    // Locator modifiers own the header before opening/meter/structure hit tests.
    // Ordinary opening-lane clicks retain their musical editing behavior.
    if(['ruler-canvas','opening-canvas'].includes(id)&&((event.ctrlKey||event.metaKey)||event.altKey)){
      meterClick=null;
      const ctrl=event.ctrlKey||event.metaKey,r={...range()};
      if(ctrl&&!event.altKey){capture(event,{kind:'range',at:seconds,edge:'start',range:{...r,start:seconds}});return;}
      if(event.altKey&&!ctrl){capture(event,{kind:'range',at:seconds,edge:'end',range:{...r,end:seconds}});return;}
      if(ctrl&&event.altKey){capture(event,{kind:'range',at:seconds,edge:'both',range:{...r,start:seconds,end:seconds}});return;}
    }
    if(id==='ruler-playhead'){capture(event,{kind:'seek',at:playheadSeconds(),grab:true});return;}
    // Reference audio is a seek surface, never a range or musical editing surface.
    // Route before pens/modifiers so all tools preserve the same audition gesture.
    if(id==='waveform-canvas'){capture(event,{kind:'seek',at:c.seconds,freeSeek:true});return;}
    // Grab the visible playback line without entering a note-edit transaction.
    const playX=midiX(typeof midiDisplayPosition==='function'?midiDisplayPosition():(state.midi.position||0))-($('roll-scroll')?.scrollLeft||0);
    if(['midi-canvas','pedal-canvas','waveform-canvas'].includes(id)&&!['rangeSelect','eraseBeat'].includes(tool)&&!event.altKey&&!event.ctrlKey&&!event.metaKey&&Math.abs(c.x-playX)<=7&&!(canEdit&&note)&&!(id==='pedal-canvas'&&(canEdit||ccCanContinue())&&ccPointAt(c))){capture(event,{kind:'seek',at:playheadSeconds(),grab:true});return;}
    if(!canEdit&&!independent&&!(id==='pedal-canvas'&&ccCanContinue())){
      if(wantsNote||['midi-canvas','pedal-canvas'].includes(id)&&event.altKey){
        updateCursor(event);const message=editLocked()?'正在保存，完成后可编辑已有对象':active()?'当前视图暂不能编辑':'只读浏览：切换到编辑模式后才能新增或修改';
        if(wantsNote){noteMessage=message+'；本次未写入。';paintNoteEntryStatus();}
        const feedback=$('editor-position');if(feedback)feedback.textContent=message;
        cancelledClickPointer=event.pointerId;event.preventDefault();event.stopImmediatePropagation();return;
      }
      if(id==='ruler-canvas'){
        const r={...range()},left=midiX(r.start)-($('roll-scroll')?.scrollLeft||0),right=midiX(r.end)-($('roll-scroll')?.scrollLeft||0);
        if(id==='ruler-canvas'&&c.y>=16){capture(event,{kind:'seek',at:seconds});return;}
        const edge=hasLocators()&&Math.abs(c.x-left)<8?'start':hasLocators()&&Math.abs(c.x-right)<8?'end':null;
        const moving=!edge&&hasLocators()&&c.seconds>Math.min(r.start,r.end)&&c.seconds<Math.max(r.start,r.end);
        capture(event,{kind:'range',at:seconds,edge,moving,original:{...r},range:edge||moving?r:{start:seconds,end:seconds,loop:r.loop}});return;
      }
      return;
    }
    if(['midi-canvas','pedal-canvas','velocity-canvas'].includes(id)&&window.AstraStructureEditor?.guardAction('editProject'))return;
    if(['midi-canvas','pedal-canvas'].includes(id))window.AstraRhythmTimeline?.clearSelection();
    if(tool==='rangeSelect'&&selectionSurfaces.has(id)){
      noteClick=null;
      if(canEdit&&event.isPrimary!==false&&noteWriteScope()&&!(id==='pedal-canvas'&&ccLaneHidden)&&
         (id!=='midi-canvas'||rollGeometry&&(!rollGeometry.canvas||rollGeometry.canvas===event.target)))selectionDown(event,c);
      else {cancelledClickPointer=event.pointerId;event.preventDefault();event.stopImmediatePropagation();}
      return;
    }
    if(id!=='beat-flags-canvas'&&window.AstraRhythmTimeline?.down(event))return;
    const structure=window.AstraStructureEditor;if(structure?.enabled()){if(structure.busy())return;if(id==='ruler-canvas'&&c.y>=16&&c.y<=33){const m=structure.labelAt(c.x,$('roll-scroll')?.scrollLeft||0);if(m){structure.choose(m.seconds,'label');event.preventDefault();event.stopImmediatePropagation();return;}}if(['midi-canvas','pedal-canvas'].includes(id))structure.clearSelection();}
    if(id!=='ruler-canvas'||c.y<16||c.y>33)meterClick=null;
    if(id==='beat-flags-canvas'){
      const flag=flagAt(c.x,c.y);if(!flag){if(structure?.enabled()){structure.choose(c.seconds,'empty');event.stopImmediatePropagation();}return;}
      if(structure?.enabled())structure.choose(flag.seconds,'pulse');
      selectedBeatFlag={...flag,revision:editor().revision};selected.clear();selectedCC=null;ccSelected.clear();
      if(flag.locked){notice='此拍已锁定，可在左侧侧栏解除锁定后拖动。';refresh();event.preventDefault();event.stopImmediatePropagation();return;}
      invalidatePreview();capture(event,{kind:'beat',point:flag,at:flag.seconds,grabOffset:c.x+($('roll-scroll')?.scrollLeft||0)-midiX(flag.seconds),to:flag.seconds});refresh();return;
    }
    if(id==='mapping-canvas'){
      if(tool!=='map')return;
      const rows=editor().tempo.anchors,at=lowerBound(rows,c.seconds,'seconds');
      const index=[at-1,at].filter(i=>i>=0&&i<rows.length).sort((a,b)=>Math.abs(midiX(rows[a].seconds)-midiX(c.seconds))-Math.abs(midiX(rows[b].seconds)-midiX(c.seconds)))[0];
      if(index===undefined||Math.abs(midiX(rows[index].seconds)-midiX(c.seconds))>8)return;
      const point=rows[index];selectedMapPoint={...point,revision:editor().revision};
      if(index===0||index===rows.length-1){notice='此点是固定锚点或映射端点，不能拖动。';refresh();return;}
      invalidatePreview();capture(event,{kind:'map',index,point,at:point.seconds,to:point.seconds,minimum:rows[index-1].seconds+.000001,maximum:rows[index+1].seconds-.000001});requestRollDraw();return;
    }
    if(id==='ruler-canvas'){
      if(id==='ruler-canvas'&&c.y>=16&&c.y<=33&&!event.ctrlKey&&!event.metaKey&&!event.altKey){const offset=$('roll-scroll')?.scrollLeft||0,index=(editor().tempo.time_signatures||[]).findIndex(m=>{const x=midiX(beatToSeconds(m.beat))-offset;return c.x>=x+3&&c.x<=x+35;});if(index>=0){window.AstraRhythmTimeline?.open('meter',beatToSeconds(editor().tempo.time_signatures[index].beat));event.preventDefault();event.stopImmediatePropagation();return;}}
      if(id==='ruler-canvas'&&c.y>=16&&!event.ctrlKey&&!event.metaKey&&!event.altKey){capture(event,{kind:'seek',at:seconds,nativeMouse:false});return;}
      const r={...range()},left=midiX(r.start)-($('roll-scroll')?.scrollLeft||0),right=midiX(r.end)-($('roll-scroll')?.scrollLeft||0);
      const edge=hasLocators()&&Math.abs(c.x-left)<8?'start':hasLocators()&&Math.abs(c.x-right)<8?'end':null;
      const moving=!edge&&hasLocators()&&c.seconds>Math.min(r.start,r.end)&&c.seconds<Math.max(r.start,r.end);
      capture(event,{kind:'range',at:seconds,edge,moving,original:{...r},range:edge||moving?r:{start:seconds,end:seconds,loop:r.loop}});return;
    }
    if(['map','beat','meter'].includes(tool)&&id!=='midi-canvas')return; // Musical pens select notes, but do not edit CC64.
    if(id==='velocity-canvas'){
      // Velocity lane (Cubase-style): drag draws velocities onto the notes under the pointer path.
      // With a note selection, only selected notes change. One edit (one undo) on release.
      if(['map','beat','meter','eraseBeat','rangeSelect'].includes(tool))return;
      const onlySelected=selected.size>0,index=noteRenderIndex(),geometry=velocityGeometry();
      const hit=velocityOnsetAt(index.entries,c.worldX,onlySelected);
      capture(event,{kind:'velocityDraw',pressCoordinate:c,index,geometry,values:new Map(),lastX:c.worldX,lastY:c.y,onlySelected});
      if(hit!=null)velocityPaint(hit,c.y,hit,c.y);
      previewPencilGuide(event,c,'draw',velocityGuideTiming);
      velocityFeedback(c.seconds,`力度 ${velocityAtY(c.y,geometry)}${onlySelected?' · 仅写选中音符':''}`);
      return;
    }
    if(id==='pedal-canvas'){
      const nearest=ccPointAt(c);
      if(tool==='draw'||(event.altKey&&!(tool==='select'&&nearest?.d<14))){selected.clear();selectedCC=null;ccSelected.clear();const cc={seconds:snapped(c.seconds,event,null,'cc'),number:64,channel:ccChannel,value:ccValue(c.y)>=64?127:0};capture(event,{kind:'drawCC',pressCoordinate:c,points:[{...cc,sample_index:0}],softPencil:tool!=='draw'&&event.altKey});updateCCPencilInk(drag,cc.value,event.altKey);ghost=cc;requestCCDraw();return;}
      if(tool==='eraseBeat'){if(nearest?.d<14)edit({type:'controls',delete:[nearest.cc.id]});return;}
      const line=ccInsertLineAt(c,event,nearest);
      if(line){
        if(recentLineClick(c)){event.preventDefault();event.stopImmediatePropagation();return;}
        selected.clear();ccSelected.clear();selectedCC=null;
        const cc={id:'__new_cc_0',seconds:c.seconds,number:64,channel:ccChannel,value:line.value>=64?127:0};
        capture(event,{kind:'control',pressCoordinate:c,creating:true,lineId:line.id,cc,controls:[cc],at:c.seconds,valueSpan:pedalGeometry().span,copy:false,ccPreviewDelta:0,ccCommitDelta:snapped(c.seconds,event,null,'cc')-c.seconds});
        ghost=cc;ccGhost=[cc];requestCCDraw();return;
      }
      if(!(nearest?.d<14)){
        const ccMode=boxModeOf(event);selected.clear();if(ccMode==='replace'){ccSelected.clear();selectedCC=null;}
        capture(event,{kind:'ccBox',from:c.seconds,to:c.seconds,y:c.y,y2:c.y,original:new Set(ccSelected),mode:ccMode});refreshInfoLine();requestCCDraw();return;
      }
      const ccCtrl=(event.ctrlKey||event.metaKey)&&!event.shiftKey,ccShift=event.shiftKey&&!(event.ctrlKey||event.metaKey);let ccToggleOff=null,ccRestore=null;
      if(ccSelected.has(nearest.cc.id)){if(ccCtrl||ccShift)ccToggleOff=nearest.cc.id;}
      else if(ccShift)ccSelected.add(nearest.cc.id);
      else{if(ccCtrl)ccRestore=new Set(ccSelected);else ccSelected.clear();ccSelected.add(nearest.cc.id);}
      selectedCC=nearest.cc.id;selected.clear();
      const ccDragIds=ccRestore?new Set([nearest.cc.id]):ccSelected,controls=ccPaintEvents().filter(cc=>ccDragIds.has(cc.id)).map(cc=>({...cc})),originalControls=ccEvents().filter(cc=>ccDragIds.has(cc.id)).map(cc=>({...cc}));
      const settle=ccReleaseAlive(ccRelease)?ccRelease:null;
      capture(event,{kind:'control',pressCoordinate:c,cc:nearest.cc,at:c.seconds,valueSpan:pedalGeometry().span,controls,originalControls,
        continuation:busy?ccPendingPreview:null,upstreamRequest:settle?.request,settlePhase:settle?.phase,copy:Boolean(event.altKey),ccToggleOff,ccRestore});
      ccGhost=controls;ghost=ccGhost.find(cc=>cc.id===nearest.cc.id);refreshInfoLine();requestCCDraw();
      return;
    }
    selectedBeatFlag=null;selectedCC=null;ccSelected.clear();
    if(tool==='eraseBeat'){if(note)edit({type:'notes',delete:[note.id]});return;}
    const secondClick=!note&&tool==='select'&&!event.altKey&&!event.shiftKey&&!event.ctrlKey&&!event.metaKey&&noteClick&&noteClick.project===state.project?.path&&noteClick.revision===editor()?.revision&&performance.now()-noteClick.time<450&&Math.hypot(event.clientX-noteClick.x,event.clientY-noteClick.y)<5;
    noteClick=null;
    if(independent||secondClick&&rollGeometry&&canDrawNote()){
      if(secondClick)noteDoubleHandledUntil=performance.now()+600;
      const noteTiming=noteSnapSettings(event),n=newNoteAt(c,event,noteTiming);
      const noteScope=usesNoteEntry()?noteWriteScope():null;
      capture(event,{kind:'drawNote',pressCoordinate:c,note:n,at:c.seconds,originContentY:c.y,drawWorldX:c.worldX,noteTiming,noteScope,pencilSelection:beginPencilSelection(n)});
      ghost=[n];if(!paintNoteCreation())requestRollDraw();return;
    }
    if(!note){const hadSelection=selected.size>0,mode=boxModeOf(event);kbdSel=null;if(mode==='replace')selected.clear();capture(event,{kind:'box',from:c.seconds,to:c.seconds,y:c.y,y2:c.y,original:new Set(selected),mode,index:null});if(hadSelection){refreshInfoLine();requestRollDraw();}return;}
    // Selection matrix (Astra product rule, see DESIGN.md): a bare press on an unselected
    // note replaces; on a selected note it keeps the set for dragging. Ctrl- or Shift-click
    // removes a selected one only on a still click, so Ctrl/Shift-dragging a selected group
    // never drops the grabbed note first. Both modifiers add without collapsing the
    // existing set; a Ctrl drag resolves to that note alone only after real travel.
    const ctrlOnly=(event.ctrlKey||event.metaKey)&&!event.shiftKey,shiftOnly=event.shiftKey&&!(event.ctrlKey||event.metaKey);let toggleOff=null,restore=null;kbdSel=null;primaryNote=note.id;
    if(selected.has(note.id)){if(ctrlOnly||shiftOnly)toggleOff=note.id;}else if(shiftOnly)selected.add(note.id);else{if(ctrlOnly)restore=new Set(selected);else selected.clear();selected.add(note.id);}
    if(['map','beat','meter','eraseBeat'].includes(tool)){capture(event,{kind:'noteSelection',note,toggleOff,restore});refresh();return;}
    if((event.ctrlKey||event.metaKey)&&event.shiftKey&&!event.altKey){
      // Cubase: Ctrl+Shift + drag up/down changes the velocity of the selected notes; nothing moves.
      const notes=inspectNotes();capture(event,{kind:'velocity',note,notes,index:noteRenderIndex(),values:new Map(),delta:0});refreshInfoLine(notes);requestNoteFeedback();return;
    }
    const hit=noteResizeHit(note,c),repeat=tool==='select'&&event.altKey&&hit==='end';
    const edge=event.altKey&&!repeat?null:hit;
    const notes=(restore?[note]:inspectNotes()).map(n=>({...n})),bounds={start:Infinity,end:0,minLen:Infinity,low:127,high:0};
    for(const n of notes){bounds.start=Math.min(bounds.start,n.start);bounds.end=Math.max(bounds.end,n.end);bounds.minLen=Math.min(bounds.minLen,n.end-n.start);bounds.low=Math.min(bounds.low,n.pitch);bounds.high=Math.max(bounds.high,n.pitch);}
    // Object moves use the current modifier sample, not the pencil's press-time
    // bypass latch. The press still supplies the exact projected grab offset.
    const noteTiming=noteSnapSettings(event);
    noteTiming.free=false;
    capture(event,{kind:'note',pressCoordinate:c,note,notes,bounds,at:c.seconds,edge,repeatEdge:repeat?noteRepeatFrame(bounds):null,copy:Boolean(event.altKey)&&!repeat,liveAlt:tool==='select'&&!edge&&!event.altKey,toggleOff,restore,noteTiming});
    drag.wire.press={...drag.wire.press,bypass:false};
    refresh();
  }
  function pointerMove(event,fromScroll=false){
    if(window.AstraRhythmTimeline?.move(event))return;
    // A normal Object-body press can acquire/release Alt without another
    // pointer movement. An already-Alt press keeps its existing copy intent.
    if(drag?.kind==='note'&&drag.liveAlt&&drag.pointer===event.pointerId)drag.copy=Boolean(event.altKey);
    updateCursor(event);
    if(!drag||event.pointerId!==drag.pointer)return;
    if(!gestureAlive(drag)){abortGesture();return;}
    drag.lastEvent=pointerState(event);if(fromScroll&&edgePointer)edgePointer=drag.lastEvent;
    const c=coordinate(event);drag.everDragged ||= Math.abs(event.clientX-drag.originX)+Math.abs(event.clientY-drag.originY)>3;drag.moved ||= drag.everDragged;
    if(drag.kind==='selectionRange'){
      selectionGhost(drag,{...wirePoint(c,event),cursor_seconds:wireCursor()},event.altKey);
      if(!fromScroll)trackEdgePointer();
      if(drag.rScope.mode==='notes'&&(!drag.rPrevious||drag.rPrevious.scope.mode==='notes'))requestNoteFeedback();
      else requestRollDraw();
      event.preventDefault();return;
    }
    if(drag.wire){
      const w=drag.wire,s=wireSample(drag,c,event);w.last=s;
      if(drag.kind==='drawNote'){
        drag.timeDrawActive ||= Math.abs(c.worldX-drag.drawWorldX)>=4;
        w.current={x:s.x,bypass:s.bypass,dragged:drag.timeDrawActive};
      }
      else if(drag.kind==='note'){if(drag.everDragged)w.samples.push({...s,dragged:true});}
      else if(drag.kind==='control')w.samples.push({...s,client_dx:event.clientX-drag.originX});
      else w.samples.push(s);
    }
    // Resolve Ctrl's click-vs-drag choice only after actual travel. A press must not
    // collapse the selection, and returning home never turns a drag into a click.
    if(drag.everDragged&&!drag.selectionResolved){
      drag.selectionResolved=true;
      if(drag.kind==='note'&&drag.restore){selected=new Set([drag.note.id]);refreshInfoLine();}
      if(drag.ccRestore){ccSelected=new Set([drag.cc.id]);selectedCC=drag.cc.id;refreshInfoLine();}
    }
    if(drag.kind==='beat'){drag.to=midiSeconds(c.x+($('roll-scroll')?.scrollLeft||0)-drag.grabOffset);statusPosition(drag.to,`第 ${drag.point.number} 拍 · 松手预览`);
    }else if(drag.kind==='map'){
      drag.to=Math.max(drag.minimum,Math.min(drag.maximum,snapped(c.seconds,event,drag.at)));
      statusPosition(drag.to,'候选映射点 · 松手预览');
    }else if(drag.kind==='range'){
      const s=Math.min(duration(),snapped(c.seconds,event));
      if(drag.moving){const delta=Math.min(duration()-Math.max(drag.original.start,drag.original.end),Math.max(-Math.min(drag.original.start,drag.original.end),s-drag.at));drag.range.start=drag.original.start+delta;drag.range.end=drag.original.end+delta;}
      else if(drag.edge==='start')drag.range.start=Math.max(0,Math.min(s,duration()));
      else if(drag.edge==='end')drag.range.end=Math.min(duration(),Math.max(s,0));
      else if(drag.edge==='both'){drag.range.start=Math.min(drag.at,s);drag.range.end=Math.max(drag.at,s);}
      else {drag.range.start=Math.min(drag.at,s);drag.range.end=Math.max(drag.at,s);}
      const a=$('editor-range-start'),b=$('editor-range-end');if(a)a.value=fmt(drag.range.start);if(b)b.value=fmt(drag.range.end);
      const ml=$('editor-music-start'),mr=$('editor-music-end');if(ml)ml.value=window.AstraRhythmTimeline?.musicPosition(drag.range.start)||'';if(mr)mr.value=window.AstraRhythmTimeline?.musicPosition(drag.range.end)||'';
      statusPosition(s,'定位点');
    }else if(drag.kind==='seek'){drag.at=drag.freeSeek?Math.min(duration(),Math.max(0,c.seconds)):snapped(c.seconds,event);statusPosition(drag.at,'播放位置');paintSeekPreview(drag.at);}
    else if(drag.kind==='drawNote'){
      const {note,noteTiming:timing}=drag,unit=timing.length;
      let end=note.end;
      // A click (including sub-threshold jitter) keeps Length Quantize. A
      // horizontal draw either grows in musical length steps or ends at the
      // raw seconds under the pointer. No time rule can change the pitch row.
      if(drag.timeDrawActive&&Math.abs(c.worldX-drag.drawWorldX)>.5){
        if(snapOn(event,'draw',timing)){
          const q0=secondsToBeat(note.start),delta=secondsToBeat(c.seconds)-q0;
          end=beatToSeconds(q0+Math.max(unit,Math.round(delta/unit)*unit));
        }else if(c.seconds>note.start)end=c.seconds;
      }
      // This short screen-space segment separates length-draw drift from a
      // deliberate vertical leg. Horizontal travel rolls the anchor forward
      // at its current velocity, so either leg can come first without a jump.
      const x=event.clientX,y=event.clientY;
      let v=drag.drawVelocity??={x:drag.originX,y:drag.originY,base:note.velocity,value:note.velocity,active:false,lastY:drag.originY};
      const dx=Math.abs(x-v.x),dy=v.y-y,vertical=Math.abs(dy);
      if(!v.active&&vertical>=7&&vertical>=1.4*dx)v.active=true;
      if(dx>=5&&(!v.active||dx>1.4*Math.abs(v.lastY-y))){
        drag.drawVelocity=v={x,y,base:v.value,value:v.value,active:false,lastY:y};
      }else if(v.active){
        const delta=Math.sign(dy)*Math.max(0,vertical-6);
        v.value=clampInsertVelocity(v.base+delta);
        v.lastY=y;
      }
      const velocity=v.value,shift=velocity-note.velocity;
      ghost=[{...note,end,velocity}];
      queuePencilInfo(drag.pencilSelection,ghost[0]);
      if(tool==='draw'||event.altKey)paintPencilGuide(drag.target,c,drag.timeDrawActive?end:note.start,!snapOn(event,'draw',timing));else clearPencilGuide();
      statusPosition(end,shift?`输入力度 ${velocity} · 音符终点`:'音符终点');
    }else if(drag.kind==='velocity'){
      const delta=Math.round(drag.originY-event.clientY);
      if(delta!==drag.delta){
        drag.delta=delta;for(const n of drag.notes){const value=Math.max(1,Math.min(127,n.velocity+delta));if(value!==n.velocity)drag.values.set(n.id,value);else drag.values.delete(n.id);}
        velocityFeedback(drag.note.start,`力度 ${delta>0?'+':''}${delta} · ${drag.notes.length} 个音符`);
      }
      event.preventDefault();return;
    }else if(drag.kind==='velocityDraw'){
      if(c.worldX!==drag.lastX||c.y!==drag.lastY){
        const hit=velocityOnsetAt(drag.index.entries,c.worldX,drag.onlySelected);
        if(c.worldX!==drag.lastX)velocityPaint(drag.lastX,drag.lastY,c.worldX,c.y);
        if(hit!=null)velocityPaint(hit,c.y,hit,c.y);
        drag.lastX=c.worldX;drag.lastY=c.y;
        velocityFeedback(c.seconds,`力度 ${velocityAtY(c.y,drag.geometry)}${drag.onlySelected?' · 仅写选中音符':''}`);
      }
      previewPencilGuide(event,c,'draw',velocityGuideTiming);
      if(!fromScroll)trackEdgePointer();event.preventDefault();return;
    }else if(drag.kind==='drawCC'){
      const cc={seconds:snapped(c.seconds,event,null,'cc'),number:64,channel:ccChannel,value:ccValue(c.y)>=64?127:0};
      const tick=ccRound(secondsToBeat(cc.seconds)*9600),previous=drag.points.findIndex(p=>ccRound(secondsToBeat(p.seconds)*9600)===tick);
      const painted={...cc,sample_index:drag.wire.samples.length-1};
      if(previous>=0)drag.points[previous]=painted;else if(drag.points.length<2048)drag.points.push(painted);
      updateCCPencilInk(drag,cc.value,event.altKey);
      ghost=cc;if(tool==='draw'||event.altKey)paintPencilGuide(drag.target,c,cc.seconds,!snapOn(event,'cc'));else clearPencilGuide();statusPosition(cc.seconds,`CC64 ${cc.value}`);
    }else if(drag.kind==='note'){
      if(!drag.everDragged)return;
      if(drag.repeatEdge){
        previewNoteRepeat(drag,c);
        if(!fromScroll)trackEdgePointer();
        event.preventDefault();return;
      }
      const edge=drag.edge,reference=drag.note[edge||'start'],raw=dragSeconds(c,drag.at,reference);
      const target=raw===reference?reference:snapped(raw,event,reference,'note',drag.noteTiming);
      if(raw===reference)lastSnapTarget=null; // vertical-only and exact return-to-origin
      let delta=target-reference;
      // Clamp one seconds delta for the group; pitch remains independent of time.
      const endLimit=Math.max(duration(),drag.bounds.end||0),shrink=Math.max(0,drag.bounds.minLen||0);
      if(!edge)delta=Math.max(-drag.bounds.start,Math.min(endLimit-(drag.bounds.end||0),delta));
      else if(edge==='start')delta=Math.max(-drag.bounds.start,Math.min(shrink,delta));
      else delta=Math.max(-shrink,Math.min(endLimit-(drag.bounds.end||0),delta));
      const requested=edge?0:Math.round((drag.originContentY-c.y)/rollGeometry.row);
      const pitchDelta=Math.max(-drag.bounds.low,Math.min(127-drag.bounds.high,requested));
      setNoteMove(drag,delta,pitchDelta);
      if(!edge&&drag.noteMove)auditionVstNote(drag.note,drag,drag.note.pitch+drag.noteMove.pitch);
      const at=reference+(drag.noteMove?.delta??0);
      // Ctrl changes the resolution, never which musical endpoint the guide follows.
      // An invalid inward sample keeps the last legal endpoint, like native.
      if(drag.noteMove||drag.resizeInkIds)paintPencilGuide(drag.target,c,at,false);else clearPencilGuide();
      drag.snapAim=null; // one endpoint guide, rather than a second snap-target marks layer
      statusPosition(at,drag.copy?'复制到':'音符');
    }else if(drag.kind==='control'){
      const rows=drag.controls||[drag.cc],dx=event.clientX-drag.originX,dy=c.y-drag.originContentY;
      // CC always follows freely in 2-D. Ctrl/Meta and B bypass time snap;
      // they never silently turn the same gesture into an axis constraint.
      let delta=0,preview=drag.cc.value;drag.ccValueActive=false;
      {
        const minimum=-Math.min(...rows.map(c=>c.seconds)),maximum=duration()-Math.max(...rows.map(c=>c.seconds));
        delta=Math.max(minimum,Math.min(maximum,dragSeconds(c,drag.at,drag.cc.seconds)-drag.cc.seconds));
        const context='cc';
        drag.ccTimeActive=drag.creating||Math.abs(dx)>=(snapOn(event,context)?3:.5);
        drag.ccCommitDelta=drag.ccTimeActive?Math.max(minimum,Math.min(maximum,snapped(drag.cc.seconds+delta,event,drag.creating?null:drag.cc.seconds,context)-drag.cc.seconds)):0;
      }
      drag.ccPreviewDelta=delta;
      // A small vertical deadzone protects imported half-pedal values during
      // horizontal moves. Ease through its edge, then track the pointer exactly.
      if(Math.abs(dy)>=4){
        const blend=Math.min(1,(Math.abs(dy)-4)/4),travel=dy*blend*blend*(3-2*blend);
        drag.ccValueActive=true;preview=Math.max(0,Math.min(127,drag.cc.value-travel/drag.valueSpan*127));
        drag.ccTarget??=drag.cc.value>=64?127:0;
        const hysteresis=3/drag.valueSpan*127;
        if(preview>63.5+hysteresis)drag.ccTarget=127;
        else if(preview<63.5-hysteresis)drag.ccTarget=0;
      }
      // Ghost follows continuously; the stored points change only on release.
      ccGhost=rows.map(cc=>({...cc,seconds:cc.seconds+delta,value:drag.ccValueActive?Math.max(0,Math.min(127,cc.value+(preview-drag.cc.value))):cc.value}));
      ghost=ccGhost.find(cc=>cc.id===drag.cc.id);drag.moved=Boolean(delta||drag.ccValueActive);
      statusPosition(ghost.seconds,`CC64 ${drag.ccValueActive?(drag.ccTarget?'踩下':'抬起'):ghost.value}${rows.length>1?' · '+rows.length+' 点':''}`);
    }else if(drag.kind==='ccBox'){
      drag.to=c.seconds;drag.y2=c.y;
      const lo=Math.min(drag.from,drag.to),hi=Math.max(drag.from,drag.to),top=Math.min(drag.y,drag.y2),bottom=Math.max(drag.y,drag.y2);
      const rows=ccEvents(),g=pedalGeometry();ccSelected=new Set(drag.original);
      for(let i=lowerBound(rows,lo,'seconds');i<rows.length&&rows[i].seconds<=hi;i++){
        const cc=rows[i],y=g.bottom-cc.value/127*g.span;
        if(drag.fullHeight||y>=top&&y<=bottom){if(drag.mode==='toggle'&&drag.original.has(cc.id))ccSelected.delete(cc.id);else ccSelected.add(cc.id);}
      }
      selectedCC=[...ccSelected][0]||null;
    }else if(drag.kind==='box'){
      if(!drag.moved)return;
      drag.index ||= boxIndex();noteClick=null;updateCursor(event);
      drag.to=c.seconds;if(!drag.fullHeight)drag.y2=c.y;
      const x1=Math.min(drag.from,drag.to),x2=Math.max(drag.from,drag.to),y1=Math.min(drag.y,drag.y2),y2=Math.max(drag.y,drag.y2);
      selected=new Set(drag.original);
      let lo=0,hi=drag.index.length;while(lo<hi){const mid=(lo+hi)>>>1;if(drag.index[mid].maxEnd<x1)lo=mid+1;else hi=mid;}
      for(let i=lo;i<drag.index.length&&drag.index[i].start<=x2;i++){const p=drag.index[i],n=p.note;if(p.end>=x1&&(rollGeometry.high-n.pitch+1)*rollGeometry.row>=y1&&(rollGeometry.high-n.pitch)*rollGeometry.row<=y2){if(drag.mode==='toggle'&&drag.original.has(n.id))selected.delete(n.id);else selected.add(n.id);}}
    }
    // Resolve collisions once per paint, not for every high-rate pointer event.
    if(!fromScroll)trackEdgePointer();
    if(drag.kind==='note'){requestNoteFeedback();event.preventDefault();return;}
    if(drag.kind==='drawNote'){if(!paintNoteCreation())requestRollDraw();}else if(['control','drawCC','ccBox'].includes(drag.kind))requestCCDraw();else if(drag.kind!=='seek')requestRollDraw();event.preventDefault();
  }
  function pointerUp(event){
    if(window.AstraRhythmTimeline?.up(event))return;
    if(rangeDismissClick&&event.pointerId===rangeDismissClick.pointer){
      const k=rangeDismissClick;rangeDismissClick=null;
      if(event.type==='pointerup'&&Math.hypot(event.clientX-k.x,event.clientY-k.y)<=4&&!event.shiftKey&&!event.altKey&&!event.ctrlKey&&!event.metaKey&&
         !dismissPendingRange()&&!drag&&!selectionFlight&&k.view===selectionViewSerial&&tool==='rangeSelect'&&active())action('editorSelectNone');
      event.preventDefault?.();return;
    }
    if(!drag||event.pointerId!==drag.pointer)return;
    // Release can carry a newer position/modifier state than the last move
    // (coalesced input or a modifier changed immediately before mouse-up).
    // Resolve that final sample through the same gesture logic; cancellation
    // and lost capture must never turn into an edit.
    const last=drag.lastEvent,final=event.type==='pointerup'&&drag.wire?coordinate(event):null;
    // An identical release is already represented by the last processed
    // sample (by the press for a still click). Scroll can change world x/y
    // without changing that sample's client coordinates.
    if(event.type==='pointerup'&&gestureAlive(drag)&&last&&
       (final&&(final.worldX!==drag.wire.last.x||final.y!==drag.wire.last.y)||event.clientX!==last.clientX||event.clientY!==last.clientY||
        ['ctrlKey','metaKey','altKey','shiftKey'].some(key=>Boolean(event[key])!==Boolean(last[key])))){
      pointerMove(event,true);
      if(!drag||event.pointerId!==drag.pointer)return;
    }
    stopEdgeScroll();
    const d=drag,g=ghost,gcc=ccGhost;
    const heldElement=event.type==='pointerup'&&d.kind==='drawNote'?$('editor-note-creation'):null;
    // Move ownership, not the element. Clearing stale hover must not hide it.
    heldElement?.removeAttribute('id');
    ccGhost=[];drag=ghost=null;
    suppressClickUntil=performance.now()+350;
    if(d.kind==='note'&&(!d.edge||d.resizeInkIds))clearPencilGuide();
    if(event.type==='pointerup'&&d.kind==='drawNote'){
      // Pencil (incl. Alt pencil) landing: the drop sample is stale advisory
      // input. Re-hitting it repaints the hover create/guide over the accepted
      // note. Drop the cached sample, void any scheduled re-hit and clear the
      // held guide; the next real pointer movement re-resolves hover normally.
      cursorProbe=null;lastHover=null;clearPencilGuide();
    }else if(event.type==='pointerup')queueCursorProbe(event);
    else {cancelPencilGuide();queueCursorProbe();}
    if(d.target.hasPointerCapture(event.pointerId))d.target.releasePointerCapture(event.pointerId);
    if(event.type==='pointercancel'){
      endVstFeedback(true);
      cancelledClickPointer=d.pointer;noteClick=null;setSnapBypass(false,false);
      if(d.kind==='note'&&(!d.edge||d.repeatEdge)&&d.everDragged)suppressNoteDragDouble=true;
      // Drop the known held cursor immediately; the queued probe can re-hit later.
      if(d.kind==='seek'&&d.target?.style)d.target.style.cursor=d.target.id==='ruler-playhead'?(window.AstraCursors?.css('grab')||'grab'):'';
      if(d.kind==='drawNote'){finishPencilSelection(d.pencilSelection);finishCreationView();}else {paintNoteCreation();refresh();}
      return;
    }
    if(!gestureAlive(d)){heldElement?.remove();if(d.kind==='drawNote')finishPencilSelection(d.pencilSelection);endVstFeedback(true);finishCreationView();notice='工程或编辑视图已更新，本次拖动未应用。';refresh();render();return;}
    if(d.kind==='selectionRange'){
      const c=coordinate(event),point={...wirePoint(c,event),cursor_seconds:wireCursor()};
      selectionGhost(d,point,event.altKey);
      const {start,end,part}=d.rPaint,source=part==='draw'?null:d.rSource,click=!d.everDragged&&point.x===d.rPress.x;
      if(part==='shift_inside'||(click&&!d.rExtend)||!(end>start)){
        if(d.rPrevious)selectionEdit({action:'clear',token:d.rPrevious.token},d.rContext,emptyRangePaint);
      }else if(!source||start!==source.start||end!==source.end){
        const command={action:'gesture',part,press:d.rPress,point,snap:d.rSnap,
          ...(source?{token:source.token}:{scope:d.rScope})};
        selectionEdit(command,d.rContext,d.rPaint);
      }
      if(typeof scheduleEditorRender==='function'&&render.deferred)scheduleEditorRender();
      requestRollDraw();event.preventDefault();event.stopImmediatePropagation();return;
    }
    if(d.kind==='drawNote'){
      if(g?.[0]){
        noteDoubleHandledUntil=performance.now()+600;
        submitDrawNote(g[0],noteCreateOperation(d.wire,g[0].velocity),d.pencilSelection,heldElement);
      }else {heldElement?.remove();finishPencilSelection(d.pencilSelection);}
      finishCreationView();
      event.preventDefault();event.stopImmediatePropagation();return;
    }
    if(d.kind==='box'&&!d.moved&&!event.shiftKey&&!event.ctrlKey&&!event.metaKey&&!event.altKey){
      noteClick={time:performance.now(),x:event.clientX,y:event.clientY,project:state.project?.path,revision:editor()?.revision};
      refreshInfoLine();event.preventDefault();event.stopImmediatePropagation();return;
    }
    if(d.kind==='velocity'||d.kind==='velocityDraw'){
      const updates=[...d.values].map(([id,velocity])=>({id,velocity}));
      if(updates.length&&edit({type:'notes',updates})&&d.kind==='velocityDraw')velocityReleaseOwner=editOwner;
      refreshInfoLine();requestRollDraw();event.preventDefault();event.stopImmediatePropagation();return;
    }
    if(d.kind==='beat'){
      if(d.moved&&Math.abs(d.to-d.at)>1e-7&&window.AstraStructureEditor?.enabled()){window.AstraStructureEditor.edit({type:'calibrate_pulse',seconds:d.at,to_seconds:d.to});}
      else if(d.moved&&Math.abs(d.to-d.at)>1e-7){rangeMenu('move_beat');$('editor-point-seconds').value=String(d.to);explainOperation();previewRange();}
    }else if(d.kind==='map'){
      if(d.moved&&Math.abs(d.to-d.at)>1e-7){
        rangeMenu('move_point');$('editor-map-point').value=String(d.index);selectMapPoint(String(d.index));
        $('editor-point-seconds').value=String(d.to);previewRange();
      }
    }else if(d.kind==='range'){
      if((d.moved||d.edge)&&hasLocators(d.range)){editScope=d.range.end>d.range.start?{project:state.project.path,start:d.range.start,end:d.range.end}:null;send('setEditorRange',{...d.range});}
      else {if(!d.moving&&!d.edge)editScope=null;commitNotesSeek(d.at);}
    }else if(d.kind==='seek'&&d.grab&&!d.moved){
      // A still press on the playback line is a grab that went nowhere: re-seeking to a
      // grid point here made playing audio jump (an audible hiccup) and moved a paused cursor.
    }else if(d.kind==='seek'){
      const now=performance.now(),c=d.nativeMouse&&!d.moved?coordinate(event):null,prior=meterClick;
      if(c&&prior&&prior.project===projectKey&&prior.revision===editor()?.revision&&now-prior.time<450&&Math.hypot(c.x-prior.x,c.y-prior.y)<5){meterClick=null;meterDoubleAt=now;window.AstraRhythmTimeline?.open('meter',snapped(c.seconds,event));}
      else {meterClick=c?{project:projectKey,revision:editor()?.revision,time:now,x:c.x,y:c.y}:null;commitNotesSeek(d.at);}
    }
    else if(d.kind==='drawCC'){
      const w=d.wire,overwrite=d.moved?ccSweepOverwrite(d.points):[],points=d.moved?ccEssentialPoints(d.points,overwrite):d.points;
      const paint={type:'controls',add:points,delete:overwrite.map(e=>e.id)};
      edit(gestureOperation(w,{action:'cc_sweep',samples:w.samples,snap:w.snap,bottom:w.geometry.bottom,span:w.geometry.span,channel:w.channel,swept:d.moved}),
        null,{paint,sources:points.map(p=>p.sample_index)});
    }else if((d.kind==='note'||d.kind==='noteSelection')&&!d.everDragged&&(d.toggleOff||d.restore)){if(d.toggleOff){selected.delete(d.toggleOff);if(primaryNote===d.toggleOff)primaryNote=null;}else selected=new Set([...d.restore,d.note?.id??primaryNote]);
    }else if(d.kind==='note'&&d.repeatEdge){
      // The frame belongs only to the held gesture. Submit exactly one atomic
      // note-add edit and adopt only its authoritative newly allocated IDs.
      suppressNoteDragDouble=true;
      const add=d.everDragged?noteRepeatCopies(d):[];
      if(add.length)edit({type:'notes',add},null,{selectAdded:true});
      refreshInfoLine();requestNoteFeedback();event.preventDefault();event.stopImmediatePropagation();return;
    }else if(d.kind==='note'&&d.everDragged){
      if(!d.edge)suppressNoteDragDouble=true;
      const w=d.wire,op=gestureOperation(w,{action:'note_drag',ids:d.notes.map(n=>n.id),grabbed_id:d.note.id,part:d.edge||'move',copy:d.copy,
        press:w.press,row_height:w.geometry.row_height,snap:w.snap,samples:w.samples});
      if(d.noteMove)startNoteLanding(d);
      if(!edit(op,(error,r)=>{
        if(error||r?.changed===false)stopNoteLanding();
      },{paint:noteMovePaint(d),selectAdded:d.copy}))stopNoteLanding();
      refreshInfoLine();requestNoteFeedback();event.preventDefault();event.stopImmediatePropagation();return;
    }else if(d.kind==='control'&&g){
      // A still Ctrl/Shift click only changes the selection: no sub-pixel move is committed.
      if(!d.creating&&!d.everDragged&&(d.ccToggleOff||d.ccRestore)){
        if(d.ccToggleOff)ccSelected.delete(d.ccToggleOff);else ccSelected=new Set([...d.ccRestore,d.cc.id]);
        selectedCC=ccSelected.has(d.cc.id)?d.cc.id:[...ccSelected][0]||null;refreshInfoLine();requestCCDraw();event.preventDefault();event.stopImmediatePropagation();return;
      }
      const w=d.wire;
      if(d.creating&&!d.everDragged)ccLineClick={x:w.press.x,y:w.press.y,project:stateProjectPath(),channel:w.channel,at:performance.now()};
      if(d.creating||d.everDragged||w.samples.some(s=>s.x!==w.press.x||s.y!==w.press.y||s.client_dx!==0)){
        const identity=d.creating?{line_id:d.lineId}:{ids:d.controls.map(c=>c.id),grabbed_id:d.cc.id};
        const op=gestureOperation(w,{action:'cc_drag',...identity,copy:d.copy,press:w.press,samples:w.samples,
          span:w.geometry.span,snap:w.snap,channel:w.channel});
        const display=gcc.length?gcc:[g],paint=ccDragOperation(d,ccCommitRows(d,display));
        const from=display.map((c,i)=>d.copy?{...c,id:'__new_cc_'+i}:c);
        edit(op,null,{paint,from,sources:d.copy?display.map(c=>c.id):null,
          parent:d.continuation,after:d.upstreamRequest,phase:d.settlePhase});
      }
      refreshInfoLine();requestCCDraw();event.preventDefault();event.stopImmediatePropagation();return;
    }
    if(d.kind==='control'||d.kind==='drawCC'||d.kind==='ccBox'){refreshInfoLine();requestCCDraw();}else{refresh();render();}event.preventDefault();event.stopImmediatePropagation();
  }
  function placeMeter(seconds){
    const bar=musicalBar(secondsToBeat(seconds)),at=beatToSeconds(bar.start),meters=editor().tempo.time_signatures,index=meters.findIndex(m=>Math.abs(m.beat-bar.start)<1e-7);
    rangeMenu(index>=0?'meter_event':'add_meter_event',index>=0?index:undefined);
    selectedMeterEvent={index,beat:bar.start,seconds:at,end_seconds:beatToSeconds(bar.end),clicked_seconds:seconds,revision:editor().revision};
    const value=`${bar.meter.numerator}/${bar.meter.denominator}`,preset=['4/4','3/4','2/4','6/8','9/8','12/8','5/4','7/8'].includes(value);
    $('editor-meter').value=preset?value:'custom';$('editor-meter-numerator').value=String(bar.meter.numerator);$('editor-meter-denominator').value=String(bar.meter.denominator);explainOperation();requestRollDraw();
  }
  function doubleClick(event){
    if(window.AstraRhythmTimeline?.double(event))return;
    if(window.AstraStructureEditor?.enabled()&&event.target.id==='ruler-canvas'){event.preventDefault();event.stopImmediatePropagation();return;}
    if(event.target.id==='ruler-canvas')return; // Only an existing attached label is double-click editable.
    if(event.target.id==='midi-canvas'&&(suppressNoteDragDouble||performance.now()<noteDoubleHandledUntil)){event.preventDefault();event.stopImmediatePropagation();return;}
    if(event.target.id==='midi-canvas'&&event.shiftKey&&!event.altKey&&!(event.ctrlKey||event.metaKey)&&tool==='select'&&active()){const note=noteAt(event);if(note){selected=new Set(editor().notes.filter(n=>n.pitch===note.pitch&&n.start>=note.start-1e-9).map(n=>n.id));primaryNote=note.id;kbdSel=null;selectionDomain='notes';selectedCC=null;ccSelected.clear();refresh();event.preventDefault();event.stopImmediatePropagation();return;}}
    if(tool!=='select'||event.altKey||(!editable()&&(event.target.id!=='midi-canvas'||!canDrawNote())))return;
    const c=coordinate(event);
    if(event.target.id==='pedal-canvas'){
      if(recentLineClick(c)){event.preventDefault();event.stopImmediatePropagation();return;}
      let hit=null,distance=11;
      for(const cc of ccEvents()){const d=Math.hypot(midiX(cc.seconds)-c.worldX,ccY(cc.value)-c.y);if(d<distance){hit=cc;distance=d;}}
      if(hit){selectCC(hit.id);refresh();}
      else {
        const w=gestureInput(c,event,'drawCC'),{bottom,span}=w.geometry;
        const op=gestureOperation(w,{action:'cc_insert',point:w.press,bottom,span,channel:w.channel,snap:w.snap,
          match_seconds:true,exclude_note_ids:w.exclusions});
        edit(op);
      }
      event.preventDefault();event.stopImmediatePropagation();
    }
    else if(event.target.id==='midi-canvas'&&rollGeometry&&!drag&&canDrawNote()&&!noteAt(event,c)){
      const timing=noteSnapSettings(event),n=newNoteAt(c,event,timing),w=gestureInput(c,event,'drawNote',timing);
      auditionVstNote(n);
      submitDrawNote(n,noteCreateOperation(w,n.velocity),beginPencilSelection(n));
      event.preventDefault();event.stopImmediatePropagation();
    }
  }
  function currentMapPoint(){
    if(!selectedMapPoint||selectedMapPoint.revision!==editor()?.revision)return null;
    return editor().tempo.anchors.find(p=>p.seconds===selectedMapPoint.seconds&&p.beat===selectedMapPoint.beat)||null;
  }
  function mapPointOptions(){
    const current=currentMapPoint(),r=range(),rows=editor().tempo.anchors;
    const row=(point,index)=>{const edge=index===0||index===rows.length-1;return `<option value="${index}" ${current===point?'selected':''}>${index+1} · ${fmt(point.seconds)} 秒 · ${point.beat.toFixed(4)} 四分音符${edge?' · 音频端点':''}</option>`;};
    const inside=[],outside=[];rows.forEach((p,i)=>(p.seconds>=r.start&&p.seconds<=r.end?inside:outside).push(row(p,i)));
    return `<option value="" ${!current?'selected':''}>请选择映射点（不会自动选最近点）</option>${inside.length?`<optgroup label="定位范围内">${inside.join('')}</optgroup>`:''}${outside.length?`<optgroup label="定位范围外">${outside.join('')}</optgroup>`:''}`;
  }
  function selectMapPoint(value){
    const point=value!==''?editor().tempo.anchors[Number(value)]:null;
    selectedMapPoint=point?{...point,revision:editor().revision}:null;
    if(point){
      if($('editor-point-seconds'))$('editor-point-seconds').value=fmt(point.seconds);
      const scroll=$('roll-scroll');if(scroll)scroll.scrollLeft=Math.max(0,midiX(point.seconds)-scroll.clientWidth/2);
    }
    updateMapPointInfo();requestRollDraw();
  }
  function updateMapPointInfo(){
    const el=$('editor-map-source');if(!el)return;
    const op=$('editor-operation')?.value,point=currentMapPoint(),isPoint=['move_point','remove_point','add_point'].includes(op);
    const title=$('dialog-title'),summary=$('editor-operation-summary');
    if(title)title.textContent=['score_end','clear_score_end'].includes(op)?(op==='score_end'?'设置谱面终点':'取消谱面终点'):op==='meter_event'?'只改拍号标记':op==='meter'?'只划小节':op==='recount'?'纠正范围拍数':isPoint?({move_point:'移动映射点',remove_point:'删除映射点',add_point:'添加映射点'}[op]):'编辑选定范围';
    if(summary){
      if(['score_end','clear_score_end'].includes(op)){summary.textContent=op==='score_end'?`右定位点 ${fmt(range().end)} 秒作为谱面结束；音频余音、演奏音符和踏板秒时间不裁剪。`:'恢复按记谱内容结束，演奏时序保持。';
      }else if(op==='move_beat'){const f=selectedBeatFlag;if(title)title.textContent='移动拍位旗帜';summary.textContent=f?`第 ${f.number} 拍：${fmt(f.seconds)} → ${fmt(Number($('editor-point-seconds').value))} 秒。拍数不变，演奏音符与 CC64 秒时间不变；检查相邻拍位和固定锁。`:'请重新选择拍位旗帜。';
      }else if(op==='meter_event'){const mark=selectedMeterEvent,next=mark?editor().tempo.time_signatures[mark.index+1]:null;summary.textContent=mark?`${fmt(mark.seconds)} – ${fmt(next?beatToSeconds(next.beat):duration())} 秒 · 持续到${next?'下一既有标记':'曲尾'}，后续标记保留。`:'请选择已有拍号标记。';summary.textContent+=' 拍点与 BPM 不变；演奏音符、CC64 秒时间不变。';
      }else if(op==='meter'){const r=range();summary.textContent=`${fmt(r.start)} – ${fmt(r.end)} 秒 · 当前 ${(secondsToBeat(r.end)-secondsToBeat(r.start)).toFixed(3)} 个四分音符单位；只划小节，范围末恢复原拍号。拍点与 BPM 不变；演奏音符、CC64 秒时间不变。`;
      }else if(op==='recount'){const r=range(),old=secondsToBeat(r.end)-secondsToBeat(r.start),value=$('editor-quarters')?.value;summary.textContent=`${fmt(r.start)} – ${fmt(r.end)} 秒固定 · ${old.toFixed(3)} → ${value||'待输入'} 四分音符间隔；范围后编号相应平移。音乐网格 BPM 可能改变；演奏音符、CC64 秒时间不变。${$('editor-distribution')?.value==='uniform'?' 均匀拍点会移除选区的网格渐变；固定锚点仍保留，并在相邻锚点间分别均分。':''}`;
      }else if(isPoint){
        if(point&&op!=='add_point'){
          const rows=editor().tempo.anchors,index=rows.indexOf(point),left=rows[Math.max(0,index-1)],right=rows[Math.min(rows.length-1,index+1)];
          summary.textContent=`源点 ${fmt(point.seconds)} 秒 · ${point.beat.toFixed(4)} 四分音符。影响相邻插值区间 ${fmt(left.seconds)} – ${fmt(right.seconds)} 秒；不受定位范围限制。`;
        }else summary.textContent=op==='add_point'?'在当前生效图中添加时间—音乐坐标约束，影响目标点两侧相邻插值区间；不受定位范围限制。':'请选择当前生效图中的源点；操作影响其两侧相邻插值区间，不受定位范围限制。';
      }else{const r=range();summary.textContent=`${fmt(r.start)} – ${fmt(r.end)} 秒 · 当前 ${(secondsToBeat(r.end)-secondsToBeat(r.start)).toFixed(3)} 个四分音符单位`;}
    }
    el.hidden=!isPoint;
    el.textContent=point&&op!=='add_point'?`选中源点：${fmt(point.seconds)} 秒 ↔ ${point.beat.toFixed(4)} 四分音符。橙线标出当前映射点；原始检测拍不变。`:'当前生效图的映射点与原始模型检测拍分开保存；请明确选择或输入约束。';
  }
  function renderPointPreview(operation,nextTempo){
    const host=$('editor-point-preview');if(!host)return;
    if(!['move_point','remove_point','add_point'].includes(operation.action)){host.hidden=true;return;}
    host.hidden=false;
    const old=editor().tempo.anchors,at=operation.seconds,index=old.findIndex(p=>p.seconds>=at),pivot=index<0?old.length-1:index;
    const oldRows=old.slice(Math.max(0,pivot-1),Math.min(old.length,pivot+2)),source=old.find(p=>p.seconds===at);
    const target=operation.action==='move_point'?operation.to_seconds:at;
    const start=Math.min(oldRows[0].seconds,target),end=Math.max(oldRows.at(-1).seconds,target);
    const next=nextTempo?.anchors?.filter(p=>p.seconds>=start&&p.seconds<=end)||[];
    const all=[...oldRows,...next],q0=Math.min(...all.map(p=>p.beat)),q1=Math.max(...all.map(p=>p.beat));
    const x=t=>38+(t-start)/Math.max(.000001,end-start)*440,y=q=>112-(q-q0)/Math.max(.000001,q1-q0)*76;
    const path=rows=>rows.map((p,i)=>`${i?'L':'M'}${x(p.seconds).toFixed(2)},${y(p.beat).toFixed(2)}`).join(' ');
    const heading=operation.action==='remove_point'?`删除 ${fmt(at)} 秒的映射约束；相邻点重新插值，总拍数不减。`:operation.action==='move_point'?`源 ${fmt(at)} 秒 → 目标 ${fmt(target)} 秒；音乐坐标 ${source?.beat.toFixed(4)??'—'} 保持。`:`新增约束 ${fmt(at)} 秒 ↔ ${Number(operation.beat).toFixed(4)} 四分音符。`;
    const targetPoint=next.find(p=>p.seconds===target);
    host.innerHTML=`<strong>${html(heading)}</strong><svg viewBox="0 0 520 148" role="img" aria-label="局部映射预览：橙色为当前映射，绿色为通过检查后的新映射"><path d="M38 24V112H478" fill="none" stroke="#596170"/><path d="${path(oldRows)}" fill="none" stroke="#a5577c" stroke-width="2" stroke-dasharray="5 4"/>${next.length?`<path d="${path(next)}" fill="none" stroke="#3d55b0" stroke-width="2.5"/>`:''}${source?`<circle cx="${x(source.seconds)}" cy="${y(source.beat)}" r="5" fill="#a5577c"/>`:''}${targetPoint?`<circle cx="${x(targetPoint.seconds)}" cy="${y(targetPoint.beat)}" r="5" fill="#3d55b0"/>`:''}<text x="38" y="137">${fmt(start)} 秒</text><text x="395" y="137">${fmt(end)} 秒</text><text x="38" y="16">音乐坐标 ${q0.toFixed(3)} – ${q1.toFixed(3)}</text></svg><span>${nextTempo?'橙色虚线：当前图　绿色实线：新图；确认后才会应用。':'约束未通过，尚未生成可应用的新图。'}</span>`;
  }
  function clampScoreWindow(rect){
    const vw=window.innerWidth||1200,vh=window.innerHeight||800,pad=8;
    const w=Math.max(Math.min(420,vw-2*pad),Math.min(rect.w,vw-2*pad));
    const h=Math.max(Math.min(230,vh-2*pad),Math.min(rect.h,vh-2*pad));
    return {x:Math.max(pad,Math.min(rect.x,vw-w-pad)),y:Math.max(pad,Math.min(rect.y,vh-h-pad)),w,h};
  }
  function placeScoreWindow(){
    const el=$('dialog');if(dialog!=='editor-score-range'||!el?.open)return;
    if(!scoreFloatRect)scoreFloatRect={x:Math.max(16,(window.innerWidth||1200)-800),y:160,w:760,h:420};
    scoreFloatRect=clampScoreWindow(scoreFloatRect);
    const {x,y,w,h}=scoreFloatRect;Object.assign(el.style,{left:`${x}px`,top:`${y}px`,width:`${w}px`,height:`${h}px`});
  }
  function saveScoreWindow(){try{window.localStorage?.setItem('astra-score-window',JSON.stringify(scoreFloatRect));}catch{}}
  function endScoreWindowGesture(){
    const g=scoreFloatGesture;if(!g)return;scoreFloatGesture=null;
    try{if(g.target?.hasPointerCapture?.(g.id))g.target.releasePointerCapture(g.id);}catch{}
    saveScoreWindow();
  }
  function bindScoreWindow(){
    const el=$('dialog');if(!el?.querySelector)return;
    const head=el.querySelector('.dialog-head');
    if(head){head.title='拖动看谱窗口';head.onpointerdown=e=>{
      if(e.button!==0||e.target.closest('button'))return;
      e.preventDefault();e.stopPropagation();scoreFloatGesture={kind:'move',target:head,id:e.pointerId,x:e.clientX,y:e.clientY,rect:{...scoreFloatRect}};head.setPointerCapture?.(e.pointerId);
    };}
    const grip=el.querySelector('.editor-score-resize');
    if(grip)grip.onpointerdown=e=>{if(e.button!==0)return;e.preventDefault();e.stopPropagation();scoreFloatGesture={kind:'resize',target:grip,id:e.pointerId,x:e.clientX,y:e.clientY,rect:{...scoreFloatRect}};grip.setPointerCapture?.(e.pointerId);};
  }
  document.addEventListener('pointermove',e=>{
    const g=scoreFloatGesture;if(!g||e.pointerId!==g.id)return;
    if((typeof e.buttons==='number'&&!(e.buttons&1))||dialog!=='editor-score-range'||!$('dialog')?.open){endScoreWindowGesture();return;}
    e.preventDefault();e.stopPropagation();const dx=e.clientX-g.x,dy=e.clientY-g.y;
    scoreFloatRect=g.kind==='move'?{...g.rect,x:g.rect.x+dx,y:g.rect.y+dy}:{...g.rect,w:g.rect.w+dx,h:g.rect.h+dy};placeScoreWindow();
  },true);
  document.addEventListener('pointerup',e=>{if(scoreFloatGesture&&e.pointerId===scoreFloatGesture.id){e.stopPropagation();endScoreWindowGesture();}},true);
  document.addEventListener('pointercancel',e=>{if(scoreFloatGesture?.id===e.pointerId)endScoreWindowGesture();},true);
  document.addEventListener('lostpointercapture',e=>{if(scoreFloatGesture?.id===e.pointerId)endScoreWindowGesture();},true);
  document.addEventListener('visibilitychange',()=>{if(document.hidden)endScoreWindowGesture();});
  window.addEventListener?.('blur',e=>{if(e.target===window)endScoreWindowGesture();});
  window.addEventListener?.('pagehide',endScoreWindowGesture);
  window.addEventListener?.('resize',()=>{placeScoreWindow();saveScoreWindow();});
  function dockRangePanel(){
    const el=$('dialog'),workspace=document.querySelector('.editor-workspace'),side=document.querySelector('.editor-side');
    if(el?.open&&['editor-range','editor-score-range','editor-structure'].includes(dialog)&&tab!=='notes'){el.close();return;}
    if(!workspace||!side||!el?.style)return;
    const covered=inspectorCovered(),scoreOpened=Boolean(el.open&&dialog==='editor-score-range');
    const opened=covered||scoreOpened;
    workspace.classList.toggle('has-correction',covered);
    el.classList.toggle('editor-score-floating',scoreOpened);
    workspace.classList.remove('has-score-range');
    workspace.classList.toggle('inspector-collapsed',inspectorCollapsed&&!covered);
    if(typeof syncInstrumentFader==='function')syncInstrumentFader(transportMidi);
    syncViewControl('editorInspector');
    workspace.style?.removeProperty('--score-panel-height');
    if(!opened){scoreFloatGesture=null;el.classList.remove('editor-docked');el.style.removeProperty('left');el.style.removeProperty('top');el.style.removeProperty('width');el.style.removeProperty('height');return;}
    el.classList.add('editor-docked');
    if(scoreOpened){placeScoreWindow();bindScoreWindow();return;}
    const rect=side.getBoundingClientRect();
    Object.assign(el.style,{left:`${rect.left}px`,top:`${rect.top}px`,width:`${rect.width}px`,height:`${rect.height}px`});
  }
  function meterEventOptions(){return (editor().tempo.time_signatures||[]).map((m,i)=>`<option value="${i}">${m.numerator}/${m.denominator} · ${fmt(beatToSeconds(m.beat))} 秒</option>`).join('');}
  function selectMeterEvent(){
    const index=Number($('editor-meter-event')?.value),m=editor().tempo.time_signatures[index];
    selectedMeterEvent=m?{index,beat:m.beat,seconds:beatToSeconds(m.beat),revision:editor().revision}:null;
    if(!m)return;
    const value=`${m.numerator}/${m.denominator}`,preset=['4/4','3/4','2/4','6/8','9/8','12/8','5/4','7/8'].includes(value);
    $('editor-meter').value=preset?value:'custom';$('editor-meter-numerator').value=String(m.numerator);$('editor-meter-denominator').value=String(m.denominator);
    const scroll=$('roll-scroll');if(scroll)scroll.scrollLeft=Math.max(0,midiX(selectedMeterEvent.seconds)-scroll.clientWidth/3);
  }
  function previewKind(){const op=$('editor-operation')?.value;if(op==='move_point'&&$('editor-acoustic-snap')?.checked)return 'acoustic_snap';return op==='recount'&&$('editor-distribution')?.value==='pulse_times'?'pulse_times':op;}
  let scoreEndDraft=null,scoreEndSerial=0;
  function scoreEndMenu(clear=false){
    const revision=editor().revision,project=state.project.path,serial=++scoreEndSerial;
    const operation={type:'tempo',action:clear?'clear_score_end':'score_end',seconds:range().end};scoreEndDraft=null;
    modal(clear?'取消谱面结束位置':'设置谱面结束位置',`<p>${clear?'恢复按记谱内容确定结尾。':`谱面在 R：${html(window.AstraRhythmTimeline?.musicPosition(range().end)||'')}（${fmt(range().end)} 秒）结束。`}</p><p>只改变谱面结尾，不裁剪音频、MIDI 音符或 CC64；之后点击“更新谱面”生成。</p><p id="editor-score-end-status" role="status">正在检查…</p>`,`${button('取消','closeDialog')}${button('确认','editorConfirmScoreEnd',{style:'primary',disabled:true})}`,'editor-score-end');
    bridge.previewProject(JSON.stringify({base_revision:revision,operation}),raw=>{
      if(serial!==scoreEndSerial||dialog!=='editor-score-end'||project!==state.project?.path||revision!==editor()?.revision)return;
      let result;try{result=JSON.parse(raw);}catch{result={error:'无法读取检查结果。'};}
      $('editor-score-end-status').textContent=result.error||['可以应用，可撤销。',...(result.warnings||[])].join(' ');
      if(!result.error){scoreEndDraft={revision,operation};document.querySelector('[data-do=editorConfirmScoreEnd]').disabled=false;}
    });
  }
  function rangeMenu(which,markerIndex){
    stopScoreRange();
    qualityResults=null;qualitySelection=null;
    invalidatePreview();
    const r=range(),quarters=secondsToBeat(r.end)-secondsToBeat(r.start);rangeDraftScope={start:r.start,end:r.end};
    modal('编辑选定范围',`<p id="editor-operation-summary">${fmt(r.start)} – ${fmt(r.end)} 秒 · 当前 ${quarters.toFixed(3)} 个四分音符单位</p><label class="field">操作<select id="editor-operation" class="control"><optgroup label="常用修正"><option value="meter_event">只改拍号标记 · 至下一标记（拍点/BPM 不变）</option><option value="meter" selected>只划小节 · 末尾恢复（拍点/BPM 不变）</option><option value="rebar">纠正选区拍数 · 小节数保持</option></optgroup><optgroup label="更多拍位操作"><option value="bar_phase">按确认强拍复核分组</option><option value="pulse_times" hidden>按确认拍数重找拍点</option><option value="interpret-half">网格拍数 ×0.5（演奏不变）</option><option value="interpret-double">网格拍数 ×2（演奏不变）</option><option value="first_bar-intro">左定位点设为首个完整小节</option><option value="first_bar-pickup">左定位点结束弱起</option><option value="add_meter_event" hidden>新增拍号标签</option><option value="remove_meter_event" hidden>删除拍号标签</option><option value="move_beat" hidden>拖动拍位旗帜</option><option value="move_point">移动当前映射点</option><option value="add_point">添加映射点</option><option value="remove_point">删除当前映射点</option><option value="score_end">谱面终点 · 右定位点</option><option value="clear_score_end">取消谱面终点</option></optgroup></select></label><div class="editor-operation-fields"><label class="field">选择已有拍号标记<select id="editor-meter-event" class="control">${meterEventOptions()}</select></label><label class="field">已确认的内部强拍 / 秒<input id="editor-phase-downbeat" class="control" type="number" min="0" step="any" value="${Number(state.midi.position)||0}"></label><label class="field">该强拍处拍号<select id="editor-phase-meter" class="control"><option value="">在 3/4、4/4 中复核</option><option value="3">已确认 3/4</option><option value="4">已确认 4/4</option></select></label><label class="field">拍号<select id="editor-meter" class="control">${options([['4/4','4/4'],['3/4','3/4'],['2/4','2/4'],['6/8','6/8'],['9/8','9/8'],['12/8','12/8'],['5/4','5/4'],['7/8','7/8'],['custom','自定义…']], '4/4')}</select></label><label class="field">自定义分子<input id="editor-meter-numerator" class="control" type="number" min="1" max="32" step="1" value="4"></label><label class="field">自定义分母<select id="editor-meter-denominator" class="control">${options([1,2,4,8,16,32,64,128].map(d=>[d,d]),4)}</select></label><label class="field editor-map-choice">当前生效图的映射点<select id="editor-map-point" class="control">${mapPointOptions()}</select></label><label class="field" hidden>选区小节数（保持）<input id="editor-bar-count" class="control" type="number" readonly></label><label class="field">确认四分音符跨度<input id="editor-quarters" class="control" type="number" min="0.001" step="any" value="${quarters.toFixed(4)}"></label><label class="field">音乐网格拍点分布<select id="editor-distribution" class="control"><option value="preserve">保留拍点变化</option><option value="pulse_times">按音频证据重找拍点</option><option value="uniform">均匀分布拍点</option></select></label><label class="field" hidden>对照 BPM（仅参考，不生效）<input id="editor-reference-bpm" class="control" type="number" min="10" max="1000" step="any" placeholder="可选"></label><p id="editor-reference-status" class="form-note" hidden></p><label class="field">映射点目标时间 / 秒<input id="editor-point-seconds" class="control" type="number" min="0" step="0.001" value="${fmt(currentMapPoint()?.seconds??r.start)}"></label><label class="field">映射点音乐坐标 / 四分音符<input id="editor-point-beat" class="control" type="number" min="0" step="any" value="${secondsToBeat(r.start).toFixed(4)}"></label></div><label class="checkbox-line" hidden><input id="editor-acoustic-snap" type="checkbox">建议吸附到附近音频拍点</label><p id="editor-map-source" class="form-note"></p><div id="editor-point-preview" class="editor-point-preview" hidden></div><label class="checkbox-line"><input id="editor-replace-meter" type="checkbox">明确替换范围内已有拍号变化</label><details class="editor-correction-help"><summary>操作说明</summary><p id="editor-operation-explanation" class="form-note"></p></details><p class="editor-preview-status" id="editor-dialog-error" data-status="idle" role="status"></p><details id="editor-preview-details" class="editor-correction-help" hidden><summary>预览细节</summary><div id="editor-preview-detail-text"></div></details>`,`${button('取消','closeDialog')}${button('预览新网格','editorPreviewRange')}${button('应用修改','editorApplyRange',{style:'primary',disabled:true})}`,'editor-range');
    $('editor-operation').insertAdjacentHTML('beforeend','<optgroup label="证据复核"><option value="pulse_quality">检查当前拍点</option></optgroup><optgroup label="全曲候选"><option value="rhythm_candidate">分析新的拍点候选</option></optgroup>');
    $('editor-dialog-error').insertAdjacentHTML('afterend','<div id="editor-quality-ranges" hidden></div>');
    $('editor-dialog-error').insertAdjacentHTML('beforebegin','<label class="checkbox-line" hidden><input id="editor-rhythm-redecode" type="checkbox">额外比较半拍值重新分组（复用观测，CPU 解码）</label><div id="editor-rhythm-candidates" hidden></div>');
    if(which)$('editor-operation').value=which;else if(!localScope())$('editor-operation').value='meter_event';
    if(markerIndex!==undefined)$('editor-meter-event').value=String(markerIndex);
    if(['meter_event','remove_meter_event'].includes($('editor-operation').value))selectMeterEvent();
    explainOperation();dockRangePanel();loadRebarBoundaries();
  }
  function explainOperation(){
    const op=$('editor-operation')?.value;if(!op)return;
    const descriptions={score_end:'将右定位点作为谱面结束。只影响末小节表示，不裁剪原音频、原始 MIDI 或 CC64。若记谱音仍跨越终点，制谱会明确提示，不会自动截短来凑齐。',clear_score_end:'取消手动谱面终点，按记谱内容结束；原始演奏与音频保持。',acoustic_snap:'在输入目标附近利用原音频缓存寻找拍点建议；不自动改写手输秒数，先预览再应用。固定锚点仍受保护，演奏时序不变。没有有效证据时可取消勾选，继续按手输时间移动。此项是可选的 Astra 声学辅助。',meter_event:'修改一个既有拍号标记，从该位置持续到下一既有标记或曲尾。后续标记原样保留；不会在定位范围末尾插入恢复标记。音符、踏板、秒位置与速度映射不变。',pulse_times:`从缓存重选范围内部拍点；请输入已确认的整数四分音符间隔数（1–${pulseLimits().count}），范围最多 ${pulseLimits().seconds} 秒。${pulseLimits().dense?'逐帧重找另有总计算量限制，范围太长时请分段处理。':''}保留两端秒位置、源音符和 CC64；不重新识别拍号，范围后音乐编号可能改变。这里数的是两端之间的间隔，不是含两端的点数；不会静默取整。`,bar_phase:'仅复核 3/4、4/4 小节分组，复用已有缓存；不改变速度图、音符或 CC64，不修漏拍或拍数。左右边界和内部确认强拍必须精确落在当前四分拍网格；不会自动吸附，时间不对请先移动映射点。默认秒数来自打开窗口时的播放头，请明确确认。',meter:'只改小节分组：音符、踏板的秒时间和速度映射都不动。范围结束恢复原拍号，边界必须满足当前拍位与整数小节约束。',recount:'两端音频时间固定，重定四分音符跨度；后续音乐编号平移，音符和踏板秒时间不动。默认保留段内变速。','interpret-half':'拍值减半、BPM 减半；演奏耗时不变，拍号不变。范围后的音乐编号相应平移。','interpret-double':'拍值加倍、BPM 加倍；演奏耗时不变，拍号不变。范围后的音乐编号相应平移。','first_bar-intro':'保留前方完整引子：左定位点作为第一个主段小节线，不缩短或删除任何音符。','first_bar-pickup':'左定位点作为弱起结束；前方时长应不足一个完整小节。',move_point:'先明确选择当前生效图中的映射点，再移动到目标秒数；音乐坐标保留。它不是原始模型检测拍，固定锚点受保护。',add_point:'新增当前生效图的时间—音乐坐标约束，必须保持顺序。原始检测拍缓存不变；补足整段拍数请用“重新指定范围内拍数”。',remove_point:'删除明确选中的当前映射点；不会减少总拍数，而是由相邻点重新插值。需要纠正多拍时请用“重新指定范围内拍数”；固定锚点不能删除。'};
    descriptions.add_meter_event='新增标签挂在明确显示的小节首拍，不在点击的小节中间硬插。只改分组，不改变拍点密度或演奏速度；预览通过后才能采用。';descriptions.remove_meter_event='移除此标签并沿用前一拍号。不会自动改 BPM 凑拍；首标签不能删除，完整小节或固定点冲突会拒绝。';
    descriptions.rebar='仅重建明确选中的完整小节：左右边界和框外拍点时刻保持，小节数量固定。选择新拍号决定内部总拍数；保留变速形状或明确均匀。音符与 CC64 秒时间不动。';
    descriptions.move_beat='拖动已选拍位对应的音频时间，拍号、拍数和源音符/CC64秒时间不变；相邻拍位与固定锁受保护。';
    descriptions.rhythm_candidate='独立分析原曲拍点，生成原拍值和半拍值分组供对照。分析完成也不会替换人工图；明确采用后只改变音乐网格，演奏音符和 CC64 秒时间保持。固定锚点冲突时可查看，不能覆盖采用。';
    $('editor-operation-explanation').textContent=descriptions[previewKind()];
    if(op==='move_beat'){const f=selectedBeatFlag;$('dialog-title').textContent='移动拍位旗帜';$('editor-operation-summary').textContent=f?`第 ${f.number} 拍：${fmt(f.seconds)} → ${fmt(Number($('editor-point-seconds').value))} 秒。拍数不变；演奏秒时间不变。`:'请重新点选拍位旗帜。';}
    if(op==='pulse_quality')$('editor-operation-explanation').textContent='框选时只检查框内，未框选时检查全曲；试听循环不限制检查范围。最多列出三段，先定位试听，再生成局部建议。不会自动移动拍点；证据不足时可继续手动修正。';
    if($('editor-quality-ranges'))$('editor-quality-ranges').hidden=op!=='pulse_quality';
    const custom=['meter','meter_event','rebar','add_meter_event'].includes(op)&&$('editor-meter').value==='custom';
    const show={ 'editor-meter':['meter','meter_event','rebar','add_meter_event'].includes(op),'editor-meter-event':['meter_event','remove_meter_event'].includes(op),'editor-meter-numerator':custom,'editor-meter-denominator':custom,'editor-map-point':['move_point','remove_point'].includes(op),'editor-quarters':['recount','pulse_times'].includes(op),'editor-distribution':['recount','rebar'].includes(op),'editor-bar-count':op==='rebar','editor-reference-bpm':op==='rebar'&&$('editor-distribution').value==='uniform','editor-point-seconds':['move_beat','move_point','add_point'].includes(op),'editor-point-beat':op==='add_point','editor-replace-meter':op==='meter','editor-acoustic-snap':op==='move_point'};
    show['editor-phase-downbeat']=op==='bar_phase';show['editor-phase-meter']=op==='bar_phase';
    show['editor-rhythm-redecode']=op==='rhythm_candidate';
    for(const [id,on] of Object.entries(show)){const label=$(id)?.closest('label');if(label)label.hidden=!on;}
    const previewButton=document.querySelector('[data-do=editorPreviewRange]');if(previewButton)previewButton.textContent=['bar_phase','pulse_times','acoustic_snap'].includes(previewKind())?'开始缓存复核':'预览新网格';
    if(op==='pulse_quality'&&previewButton)previewButton.textContent=qualitySelection?'生成局部建议':'检查当前拍点';
    updateMapPointInfo();
    if(op==='rhythm_candidate'){
      $('dialog-title').textContent='分析新的拍点候选';
      $('editor-operation-summary').textContent='全曲音频 · 当前生效图仍在使用。选择绿色候选网格仅用于比较，尚未采用。';
      if(previewButton)previewButton.textContent='分析新候选';
    }
    if($('editor-rhythm-candidates'))$('editor-rhythm-candidates').hidden=op!=='rhythm_candidate';
    const acoustic=$('editor-distribution')?.querySelector?.('[value=pulse_times]');if(acoustic)acoustic.disabled=op==='rebar';if(op==='rebar'&&$('editor-distribution').value==='pulse_times')$('editor-distribution').value='preserve';
    if(['add_meter_event','remove_meter_event'].includes(op)){
      const mark=selectedMeterEvent,scope=localScope(),next=mark?editor().tempo.time_signatures.find(m=>m.beat>mark.beat+1e-7):null;
      $('dialog-title').textContent=op==='add_meter_event'?'新增小节首拍拍号':'删除拍号标签';
      $('editor-operation-summary').textContent=mark?`${mark.clicked_seconds!==undefined?`点击 ${fmt(mark.clicked_seconds)} 秒；拟放在该小节首拍 ${fmt(mark.seconds)} 秒。 `:`标签 ${fmt(mark.seconds)} 秒。 `}${scope?`仅从标签到右边界 ${fmt(scope.end)} 秒，末端恢复；框外位置与拍号保持，编号自然续排。`:`持续到${next?`下一既有标记 ${fmt(beatToSeconds(next.beat))} 秒`:'曲尾'}，后方小节编号可能变化。`} 只改分组，拍点/BPM 与演奏秒时间不变。`:'请选择要修改的标签位置。';
    }
    if(op==='rebar')explainRebar();
    if($('editor-reference-status'))$('editor-reference-status').hidden=!(op==='rebar'&&$('editor-distribution').value==='uniform');
    if(op==='meter_event'){const mark=selectedMeterEvent;$('editor-operation-summary').textContent+=' 后方小节编号可能变化。';if(mark?.clicked_seconds!==undefined)$('editor-operation-summary').textContent=`点击 ${fmt(mark.clicked_seconds)} 秒；选择该小节首拍 ${fmt(mark.seconds)} 秒。 `+$('editor-operation-summary').textContent;}
  }
  function loadRebarBoundaries(){
    if(typeof bridge?.rebarBoundaries!=='function')return;
    const revision=editor().revision,key=projectKey;
    if(rebarBoundaryCache?.revision===revision&&!rebarBoundaryCache.error)return;
    bridge.rebarBoundaries(JSON.stringify({base_revision:revision}),raw=>{
      if(projectKey!==key||editor()?.revision!==revision)return;
      const data=phaseResult(raw);rebarBoundaryCache={revision,rows:data?.ok?data.boundaries:[],error:data?.error};
      if(dialog==='editor-range'&&$('editor-operation')?.value==='rebar')explainRebar();
    });
  }
  function rebarSelection(){
    const scope=localScope();if(!scope)throw new Error('先在上标尺设置左右定位点，确定需要修正的范围。');
    if(rebarBoundaryCache?.revision!==editor().revision)throw new Error('正在读取当前小节线，请稍后预览。');
    if(rebarBoundaryCache.error)throw new Error(rebarBoundaryCache.error);
    const rows=rebarBoundaryCache.rows||[],left=rows.find(r=>Math.abs(r.seconds-scope.start)<1e-6),right=rows.find(r=>Math.abs(r.seconds-scope.end)<1e-6);
    if(!left||!right){const nearest=t=>rows.reduce((best,r)=>!best||Math.abs(r.seconds-t)<Math.abs(best.seconds-t)?r:best,null);const suggestions=[];if(!left){const row=nearest(scope.start);suggestions.push(`左边界 ${fmt(scope.start)} 秒${row?`，附近 ${row.label} ${fmt(row.seconds)} 秒`:''}`);}if(!right){const row=nearest(scope.end);suggestions.push(`右边界 ${fmt(scope.end)} 秒${row?`，附近 ${row.label} ${fmt(row.seconds)} 秒`:''}`);}throw new Error('选区需落在现有小节线；'+suggestions.join('；')+'。请拖动范围手柄对齐，不会自动扩大选区。');}
    if(right.index<=left.index)throw new Error('请至少选择一个完整小节。');
    return {left,right,count:right.index-left.index};
  }
  function explainRebar(){
    $('dialog-title').textContent='纠正选区拍数与小节';
    try{const {left,right,count}=rebarSelection();if($('editor-bar-count'))$('editor-bar-count').value=count;const [n,d]=$('editor-meter').value==='custom'?[Number($('editor-meter-numerator').value),Number($('editor-meter-denominator').value)]:$('editor-meter').value.split('/').map(Number);$('editor-operation-summary').textContent=`${fmt(left.seconds)} – ${fmt(right.seconds)} 秒 · ${count} 小节保持，内部 ${count*n} 个 ${d} 分音符拍。框外拍点、小节编号及演奏秒时间保持。${$('editor-distribution').value==='uniform'?` 选区内改为 ${((count*n*4/d)*60/(right.seconds-left.seconds)).toFixed(2)} BPM 均匀拍点，原有渐变不保留。`:' 保留选区内渐快渐慢形状。'}`;}catch(error){if($('editor-bar-count'))$('editor-bar-count').value='';$('editor-operation-summary').textContent=error.message;}
  }
  function previewTempoReference(){
    tempoReference=null;const field=$('editor-reference-bpm'),status=$('editor-reference-status');
    if(!field||!status||$('editor-operation')?.value!=='rebar'||$('editor-distribution')?.value!=='uniform')return;
    if(!field.value.trim()){status.textContent='留空不显示参考网格；实际均匀 BPM 由选区时长与拍数决定。';requestRollDraw();return;}
    if(typeof bridge?.previewTempoReference!=='function'){status.textContent='当前运行版本未提供参考网格；不影响普通重划。';return;}
    let operation;try{operation=rangeOperation();}catch(error){status.textContent=error.message;return;}
    const reference_bpm=Number(field.value);if(!Number.isFinite(reference_bpm)||reference_bpm<=0){status.textContent='请输入正数 BPM；参考不会改变播放。';return;}
    const serial=++referenceSerial,revision=editor().revision,key=projectKey,scope=localScope();status.textContent='正在比较固定边界与参考 BPM…';
    bridge.previewTempoReference(JSON.stringify({base_revision:revision,operation,reference_bpm,phase_seconds:operation.start}),raw=>{
      if(serial!==referenceSerial||key!==projectKey||revision!==editor()?.revision||dialog!=='editor-range'||JSON.stringify(scope)!==JSON.stringify(localScope())||$('editor-operation')?.value!=='rebar'||$('editor-distribution')?.value!=='uniform'||Number($('editor-reference-bpm')?.value)!==reference_bpm)return;
      const result=phaseResult(raw);if(!result?.ok){status.textContent=result?.error||'参考网格不可用；当前图不变。';return;}
      tempoReference={...result,revision,scope};const error=Number(result.constraints?.duration_error_seconds)||0;
      status.textContent=`橙色虚线：${reference_bpm} BPM 参考，不驱动播放、吸附或量化。${result.feasible?'与当前固定边界及拍数兼容。':`与固定边界不兼容，时长差 ${error.toFixed(3)} 秒；不会按此数值改图。`} 应用仍使用上方由选区推导的 BPM。`;requestRollDraw();
    });
  }
  function rangeOperation(){
    const r=range(),which=$('editor-operation').value;let operation={type:'tempo',start:r.start,end:r.end};
    if(['add_meter_event','remove_meter_event'].includes(which)){
      const mark=selectedMeterEvent;if(!mark||mark.revision!==editor().revision)throw new Error('标签位置已变化，请重新选择。');
      if(which==='remove_meter_event'&&mark.index<=0)throw new Error('首个拍号标签不能删除。');
      const prev=which==='remove_meter_event'?editor().tempo.time_signatures[mark.index-1]:null;
      const [numerator,denominator]=prev?[prev.numerator,prev.denominator]:$('editor-meter').value==='custom'?[Number($('editor-meter-numerator').value),Number($('editor-meter-denominator').value)]:$('editor-meter').value.split('/').map(Number);
      const scope=localScope();if(scope){if(mark.seconds<scope.start-1e-7||mark.seconds>=scope.end-1e-7)throw new Error('该小节首拍不在当前编辑框内；请重新选择范围，不会越框修改。');return {type:'tempo',action:'meter',start:mark.seconds,end:scope.end,numerator,denominator,replace_changes:false,scope};}
      return {type:'tempo',action:which,seconds:mark.seconds,...(which==='add_meter_event'?{numerator,denominator}:{})};
    }
    if(which==='rebar'){const {left,right,count}=rebarSelection(),[numerator,denominator]=$('editor-meter').value==='custom'?[Number($('editor-meter-numerator').value),Number($('editor-meter-denominator').value)]:$('editor-meter').value.split('/').map(Number);return {type:'rebar',boundary_revision:editor().revision,tempo_sha256:left.tempo_sha256,left_boundary:left.id,right_boundary:right.id,start:left.seconds,end:right.seconds,numerator,denominator,bar_count:count,distribution:$('editor-distribution').value,replace_changes:false};}
    if(['meter','meter_event'].includes(which)){const [numerator,denominator]=$('editor-meter').value==='custom'?[Number($('editor-meter-numerator').value),Number($('editor-meter-denominator').value)]:$('editor-meter').value.split('/').map(Number);Object.assign(operation,{action:which,numerator,denominator});if(which==='meter'){if(!localScope())throw new Error('请先在上标尺设置左右定位点，确定需要修改的范围。');operation.replace_changes=$('editor-replace-meter').checked;operation.scope=localScope();}else{if(!selectedMeterEvent||selectedMeterEvent.revision!==editor().revision)throw new Error('拍号图已更新，请重新选择已有标记。');operation.seconds=selectedMeterEvent.seconds;delete operation.start;delete operation.end;}}
    if(which==='move_beat'){const f=selectedBeatFlag;if(!f||f.revision!==editor().revision)throw new Error('拍位已更新，请重新选择旗帜。');return {type:'tempo',action:'move_beat',seconds:f.seconds,to_seconds:Number($('editor-point-seconds').value),beat:f.beat,...(localScope()?{scope:localScope()}:{})};}
    if(which==='recount')throw new Error('请用选区拍数与小节修正；旧任意秒重定拍数入口已停用。');
    if(which.startsWith('interpret'))Object.assign(operation,{action:'interpret',factor:which.endsWith('half')?.5:2});
    if(which.startsWith('first_bar'))Object.assign(operation,{action:'first_bar',seconds:r.start,mode:which.endsWith('intro')?'intro':'pickup'});
    if(['move_point','remove_point'].includes(which)){
      const point=currentMapPoint();if(!point)throw new Error('请先选择要修改的当前映射点；不会自动选择最近点。');
      Object.assign(operation,{action:which,seconds:point.seconds,to_seconds:Number($('editor-point-seconds').value)});
    }
    if(['score_end','clear_score_end'].includes(which))Object.assign(operation,{action:which,seconds:r.end});
    if(which==='add_point')Object.assign(operation,{action:which,seconds:Number($('editor-point-seconds').value),beat:Number($('editor-point-beat').value)});
    return operation;
  }
  function previewMessage(text,status='error',details=[]){
    const el=$('editor-dialog-error');if(!el)return;el.textContent=text;if(el.dataset)el.dataset.status=status;
    const box=$('editor-preview-details'),body=$('editor-preview-detail-text');if(box&&body){box.hidden=!details.length;body.textContent=details.join('\n');}
  }
  function invalidatePreview(){tempoReference=null;referenceSerial++;if(referenceTimer)clearTimeout(referenceTimer);referenceTimer=0;rhythmResults=null;if($('editor-rhythm-candidates'))$('editor-rhythm-candidates').textContent='';if($('editor-dialog-error')?.dataset)$('editor-dialog-error').dataset.status='error';if($('editor-preview-details'))$('editor-preview-details').hidden=true;if(phaseTimer)clearTimeout(phaseTimer);phaseTimer=0;phaseRequest=null;previewBusy=false;if($('editor-point-preview'))$('editor-point-preview').hidden=true;tempoPreview=null;previewSerial++;const btn=document.querySelector('[data-do=editorApplyRange]');if(btn){btn.disabled=true;btn.textContent='应用修改';}requestRollDraw();}
  function phaseAlive(request){const r=editor()?.range||{};return (request.kind==='rhythm_candidate'||(r.start===request.start&&r.end===request.end&&JSON.stringify(request.edit_scope??null)===JSON.stringify(localScope())))&&phaseRequest===request&&request.serial===previewSerial&&dialog==='editor-range'&&$('dialog')?.open!==false&&previewKind()===request.kind&&request.project===state.project?.path&&request.revision===editor()?.revision;}
  function phaseResult(raw){try{return typeof raw==='string'?JSON.parse(raw):raw;}catch{return {error:'无法读取缓存复核结果。'};}}
  function phaseFailed(message){previewBusy=false;phaseRequest=null;phaseTimer=0;previewMessage(message);}
  function startPhasePreview(device='prefer_gpu',qualityMode='discover'){
    invalidatePreview();
    if(typeof bridge?.startBarPhasePreview!=='function'||typeof bridge?.barPhasePreviewStatus!=='function'){$('editor-dialog-error').textContent='当前运行版本尚未提供缓存复核，可继续使用手工拍号或拍数修正。';return;}
    const r=range(),kind=previewKind(),payload={base_revision:editor().revision,start:r.start,end:r.end};
    if(kind==='pulse_quality'){
      Object.assign(payload,{kind,mode:qualityMode});
      if(qualityMode==='propose'){
        const chosen=qualitySelection;
        if(!chosen||chosen.revision!==editor().revision||chosen.project!==state.project.path||chosen.start_seconds!==r.start||chosen.end_seconds!==r.end){previewMessage('请重新检查并定位一段，再生成建议；当前选区或版本已变化。');return;}
        Object.assign(payload,{start_seconds:chosen.start_seconds,end_seconds:chosen.end_seconds,interval_count:chosen.interval_count,count_confirmed:false});
      }else{const scope=localScope();if(scope)payload.scope=[scope.start,scope.end];qualityResults=null;qualitySelection=null;if($('editor-quality-ranges'))$('editor-quality-ranges').textContent='';}
    }else if(kind==='rhythm_candidate'){
      rhythmResults=null;if($('editor-rhythm-candidates'))$('editor-rhythm-candidates').textContent='';
      Object.assign(payload,{kind,provider:'cubase_local',execution_device:device,include_redecode:Boolean($('editor-rhythm-redecode')?.checked)});
    }else if(kind==='acoustic_snap'){
      const point=currentMapPoint(),to_seconds=Number($('editor-point-seconds').value);
      if(!point){previewMessage('请先明确选择要移动的当前映射点。');return;}
      if(!Number.isFinite(to_seconds)||to_seconds<0){previewMessage('请输入有效的非负目标秒数；不会自动修正输入。');return;}
      Object.assign(payload,{kind,seconds:point.seconds,to_seconds});
    }else if(kind==='pulse_times'){
      const interval_count=Number($('editor-quarters').value);
      const error=pulseRequestError(r,interval_count);if(error){$('editor-dialog-error').textContent=error;return;}
      Object.assign(payload,{kind,interval_count});
    }else{
      const downbeat=Number($('editor-phase-downbeat').value),meterValue=$('editor-phase-meter').value,meter=meterValue===''?null:Number(meterValue);
      if(!Number.isFinite(downbeat)||!(r.start<downbeat&&downbeat<r.end)){$('editor-dialog-error').textContent='确认强拍必须严格位于左右定位点之间；请移动播放头或输入对应秒数，不会自动夹紧或吸附。';return;}
      Object.assign(payload,{downbeat_seconds:downbeat,meter});
    }
    const request={serial:previewSerial,revision:editor().revision,project:state.project.path,kind,start:r.start,end:r.end,edit_scope:localScope()};phaseRequest=request;previewBusy=true;
    previewMessage(kind==='acoustic_snap'?'正在目标附近寻找音频拍点建议…':kind==='pulse_times'?'正在缓存中重找拍点…':'正在缓存中复核分组…','running');
    if(kind==='rhythm_candidate')previewMessage('正在独立分析全曲拍点… 当前生效图保持，可继续查看卷帘。','running');
    if(kind==='pulse_quality')previewMessage(qualityMode==='discover'?'正在检查当前拍点证据…':'正在生成并核验局部建议…','running');
    const poll=()=>{
      phaseTimer=0;if(!phaseAlive(request))return;
      bridge.barPhasePreviewStatus(request.token,raw=>{
        if(!phaseAlive(request))return;
        const result=phaseResult(raw)||{};
        if(result.status==='running'&&result.ok!==false&&!result.error){phaseTimer=setTimeout(poll,500);return;}
        if(result.ok===false||result.error||['failed','stale'].includes(result.status)){phaseFailed(result.error||(result.status==='stale'?'复核候选已过期，请重新预览。':'缓存复核失败；可继续手工修正。'));return;}
        if(request.kind==='pulse_quality'&&result.status==='ready'){
          previewBusy=false;
          if(result.mode==='discover'){
            qualityResults={revision:request.revision,project:request.project,ranges:(result.ranges||[]).slice(0,3)};renderQualityRanges();
            previewMessage(result.summary||'检查完成，当前速度图保持。','ready',result.warnings||[]);return;
          }
          if(result.candidate_available!==true){previewMessage(result.summary||'局部证据不足，候选已拒绝；当前速度图保持。','idle',[...(result.review_messages||[]),...(result.warnings||[])]);return;}
        }
        if(request.kind==='rhythm_candidate'&&result.status==='ready'){
          previewBusy=false;
          if(result.needs_device_choice){
            previewMessage(result.summary||'GPU 正忙，请选择本次计算设备。','idle');
            $('editor-rhythm-candidates').innerHTML=button('本次用 CPU','editorRhythmCPU')+button('继续使用 GPU','editorRhythmGPU');return;
          }
          if(!result.candidate_token||!Array.isArray(result.candidates)){phaseFailed('没有返回有效的拍点候选。');return;}
          rhythmResults={...result,revision:request.revision};renderRhythmCandidates();
          previewMessage('分析完成，当前图仍生效。请选择候选查看绿色网格；采用可一次撤销。','ready',result.warnings||[]);return;
        }
        if(result.status!=='ready'||!result.candidate_token||!Array.isArray(result.tempo?.anchors)||result.tempo.anchors.length<2||!Array.isArray(result.tempo?.time_signatures)||!result.tempo.time_signatures.length){phaseFailed('缓存复核没有返回可应用的有效候选。');return;}
        previewBusy=false;
        tempoPreview={...result,revision:request.revision,operation:{action:request.kind},candidate_token:result.candidate_token};
        const summary=typeof result.summary==='string'?result.summary:JSON.stringify(result.summary||{});
        previewMessage(request.kind==='acoustic_snap'?(summary||'仅候选预览 · 绿色为建议映射；手输秒数未改变。'):request.kind==='pulse_times'?'仅候选预览 · 演奏时序保持，绿色为新网格。应用后更新内部拍点与音乐编号。':'仅候选预览 · 演奏时序保持，绿色为新网格。应用后修改分组。','ready',[...(request.kind==='acoustic_snap'?[]:[summary]),...(result.warnings||[])].filter(Boolean));
        if(request.kind==='pulse_quality')previewMessage('绿色为通过证据检查的局部建议，尚未采用。音符与踏板演奏秒时间保持；采用后可一次撤销。','ready',[summary,...(result.warnings||[])]);
        document.querySelector('[data-do=editorApplyRange]').disabled=false;requestRollDraw();
      });
    };
    bridge.startBarPhasePreview(JSON.stringify(payload),raw=>{
      if(!phaseAlive(request))return;
      const result=phaseResult(raw)||{};
      if(!result.ok||!result.token){phaseFailed(result.error||'无法启动缓存复核；可继续手工修正。');return;}
      request.token=result.token;phaseTimer=setTimeout(poll,500);
    });
  }
  function previewRange(){
    if(previewKind()==='pulse_quality'&&qualitySelection){startPhasePreview('prefer_gpu','propose');return;}
    if(['bar_phase','pulse_times','acoustic_snap','rhythm_candidate','pulse_quality'].includes(previewKind())){startPhasePreview();return;}
    let operation;try{operation=rangeOperation();}catch(error){$('editor-dialog-error').textContent=error.message;return;}
    const serial=++previewSerial,revision=editor().revision,edit_scope=localScope();previewBusy=true;tempoPreview=null;
    document.querySelector('[data-do=editorApplyRange]').disabled=true;
    previewMessage('正在检查锚点与边界…','running');
    bridge.previewProject(JSON.stringify({base_revision:revision,operation}),raw=>{
      if(serial!==previewSerial||dialog!=='editor-range')return;
      if(JSON.stringify(edit_scope)!==JSON.stringify(localScope())){previewBusy=false;previewMessage('编辑选区已改变，请重新预览。');return;}
      previewBusy=false;let result;try{result=JSON.parse(raw);}catch{result={error:'无法读取预览'};}
      if(revision!==editor()?.revision){previewMessage('生效图已经更新，请重新选择映射点并预览。');return;}
      if(operation.start!==undefined&&(operation.start!==editor().range.start||operation.end!==editor().range.end)){previewMessage('定位范围已改变，请重新预览。');return;}
      if(result.error){previewMessage(result.error);renderPointPreview(operation,null);return;}
      tempoPreview={...result,operation,revision,edit_scope};renderPointPreview(operation,result.tempo);
      const summary=typeof result.summary==='string'?result.summary:JSON.stringify(result.summary||{});
      previewMessage(['score_end','clear_score_end'].includes(operation.action)?'仅候选预览 · 谱面结束表示改变，演奏时序保持。确认后生效。':'仅候选预览 · 演奏时序保持，绿色为新网格。确认应用后生效。','ready',[summary,...(result.warnings||[])].filter(Boolean));
      document.querySelector('[data-do=editorApplyRange]').disabled=false;requestRollDraw();
    });
  }
  function applyRange(){
    if(!tempoPreview||tempoPreview.revision!==editor().revision||previewBusy)return;
    if(tempoPreview.edit_scope!==undefined&&JSON.stringify(tempoPreview.edit_scope)!==JSON.stringify(localScope())){invalidatePreview();previewMessage('编辑选区已改变，请重新预览。');return;}
    const r=editor().range;if(tempoPreview.operation.start!==undefined&&(tempoPreview.operation.start!==r.start||tempoPreview.operation.end!==r.end)){invalidatePreview();previewMessage('定位范围已改变，请重新预览。');return;}
    if(tempoPreview.candidate_token&&phaseRequest&&!phaseAlive(phaseRequest)){invalidatePreview();previewMessage('定位范围或操作已改变，请重新预览。');return;}
    if(tempoPreview.candidate_token){const token=tempoPreview.candidate_token,candidate_id=tempoPreview.candidate_id;invalidatePreview();$('dialog').close();send('applyBarPhaseCandidate',{token,...(candidate_id?{candidate_id}:{})});return;}
    const operation=tempoPreview.operation;tempoPreview=null;$('dialog').close();edit(operation);
  }
  function renderRhythmCandidates(){
    const host=$('editor-rhythm-candidates');if(!host||!rhythmResults)return;
    host.innerHTML=`<label class="field">比较网格<select class="control" id="editor-rhythm-choice"><option value="">当前生效图 · 不采用</option>${rhythmResults.candidates.map(c=>`<option value="${html(c.id)}" ${c.valid?'':'disabled'}>${html(c.label)}${c.valid?'':' · 边界不兼容'}</option>`).join('')}</select></label>${rhythmResults.provenance?.original_reverified===false?'<p class="form-note">原文件不可用：使用已校验的 NCM 导入缓存；原文件本次未重新核验。</p>':''}<p id="editor-rhythm-summary" class="form-note">同一批网络观测的不同拍值与小节解释。此处选择只比较，不改演奏。</p><details class="editor-correction-help"><summary>来源与不可用候选</summary><p>${html(rhythmResults.provenance?.provider||'')} / ${html(rhythmResults.provenance?.decoder||'')}</p>${rhythmResults.candidates.filter(c=>!c.valid).map(c=>`<p>${html(c.label)}：${html(c.reason)}</p>`).join('')}</details>`;
  }
  function renderQualityRanges(){
    const host=$('editor-quality-ranges');if(!host||!qualityResults)return;
    host.innerHTML=`${qualitySelection?`<p class="form-note">已选 ${fmt(qualitySelection.start_seconds)} – ${fmt(qualitySelection.end_seconds)} 秒 · 当前图 ${qualitySelection.interval_count} 个四分间隔</p>`:''}<details ${qualitySelection?'':'open'}><summary>${qualityResults.ranges.length} 段复核范围${qualitySelection?' · 更换范围':''}</summary>`+qualityResults.ranges.map((row,index)=>{
      const valid=Number.isFinite(row.start_seconds)&&Number.isFinite(row.end_seconds)&&row.start_seconds>=0&&row.end_seconds>row.start_seconds&&row.end_seconds<=duration()&&Number.isInteger(row.interval_count)&&row.interval_count>=1&&row.interval_count<=64;
      return `<div class="editor-diagnostic-item"><strong>复核 ${index+1} · ${fmt(row.start_seconds)} – ${fmt(row.end_seconds)} 秒</strong><small>当前图 ${html(row.interval_count)} 个四分间隔 · 拍数未经人工确认</small>${button('定位此段','editorQualityLocate',{key:String(index),disabled:!valid})}</div>`;
    }).join('')+`</details><p class="form-note">定位后用播放键试听，不自动播放，保留循环开关。拍数尚未确认；只采用证据合格的建议。</p>${button('重新检查当前图','editorQualityDiscover')}`;
    const preview=document.querySelector('[data-do=editorPreviewRange]');if(preview)preview.textContent=qualitySelection?'生成局部建议':'检查当前拍点';
  }
  function locateQualityRange(index){
    if(!qualityResults||qualityResults.project!==state.project.path||qualityResults.revision!==editor().revision){previewMessage('复核列表已过期，请重新检查。');return;}
    const row=qualityResults.ranges[Number(index)];
    if(!row||!Number.isFinite(row.start_seconds)||!Number.isFinite(row.end_seconds)||!(0<=row.start_seconds&&row.start_seconds<row.end_seconds&&row.end_seconds<=duration())||!Number.isInteger(row.interval_count)||row.interval_count<1||row.interval_count>64)return;
    invalidatePreview();qualitySelection={...row,revision:qualityResults.revision,project:qualityResults.project};
    const scroll=$('roll-scroll');if(scroll)scroll.scrollLeft=Math.max(0,midiX(row.start_seconds)-scroll.clientWidth*.1);
    send('setEditorRange',{start:row.start_seconds,end:row.end_seconds,loop:Boolean(range().loop)});
    dispatch('midiSeek',{position:row.start_seconds});
    renderQualityRanges();previewMessage('已定位复核范围。可试听后点“生成局部建议”；当前图尚未修改。','idle');requestRollDraw();
  }
  function chooseRhythmCandidate(id){
    if(!rhythmResults||!phaseRequest||!phaseAlive(phaseRequest))return;
    const row=rhythmResults.candidates.find(c=>c.id===id&&c.valid),apply=document.querySelector('[data-do=editorApplyRange]');
    if(!row){tempoPreview=null;if(apply)apply.disabled=true;requestRollDraw();return;}
    tempoPreview={tempo:row.tempo,revision:rhythmResults.revision,operation:{action:'rhythm_candidate'},candidate_token:rhythmResults.candidate_token,candidate_id:row.id};
    const info=row.summary||{},first=Number.isFinite(info.first_bar_seconds)?`${fmt(info.first_bar_seconds)} 秒（待确认）`:'待确认';
    $('editor-rhythm-summary').textContent=`${(info.bpm_min||0).toFixed(1)}–${(info.bpm_max||0).toFixed(1)} BPM；${(info.meters||[]).join(' / ')}；首完整小节 ${first}。${info.derived_bar_boundaries?'拍点秒位置相同，双拍值拆分小节；中间小节线由拍位推导，不是新检测到的强拍。':info.decode_source==='same_observations_separate_6_8_pulse_decode'?'同一网络观测经独立解码；拍点秒位置可能不同。6/8 是内部拍点组数，此候选记为 3/4 或 4/4。':info.preserves_pulse_seconds?'使用相同拍点秒位置，仅解释不同。':'重新解码，拍点秒位置可能不同。'}`;
    previewMessage(row.adoptable?'绿色为比较候选，当前图仍生效。采用后请点“更新谱面”；音符与 CC64 秒时间保持。':row.reason,'ready');
    if(apply){apply.disabled=!row.adoptable;apply.textContent='采用此候选';}requestRollDraw();
  }
  function exportPurposeBaseHelp(purpose){
    return {
      mapped:'写入当前速度图，适合独立播放，或在新工程中导入速度轨。',
      target_grid:'沿用目标工程的速度轨。目标工程须与当前完整速度图、起点及拍号匹配。',
      seconds_120:'播放端须设为固定 120 BPM。按原演奏秒数播放，变速网格不会写入。'
    }[purpose] || '';
  }
  function exportOpeningLimit(){const t=editor()?.tempo;if(!t||!['intro','normal'].includes(t.opening_mode)||!((Number(t.first_full_bar_beat)||0)>0||(Number(t.score_start_seconds)||0)>0))return '';return '<p id="editor-export-opening-limit" class="form-note" role="note"><strong>外部 DAW 的小节边界限制：</strong>标准 MIDI 的标记 / 拍号事件不保证 Cubase 等软件在任意位置重新起小节。保留演奏秒时间及边界意图，不等于小节布局完全无损；导入后请核对并按需设置第一完整小节。</p>';}
  function exportPurposeHelp(purpose){
    const buckets=new Map();for(const cc of editor()?.controls||[]){if(cc.number!==64)continue;const tick=ccRound((purpose==='seconds_120'?cc.seconds*2:secondsToBeat(cc.seconds))*9600),key=`${cc.channel??0}:${tick}`;buckets.set(key,(buckets.get(key)||0)+1);}
    const count=[...buckets.values()].filter(n=>n>1).length;
    return exportPurposeBaseHelp(purpose)+(count?` 此导出时基有 ${count} 组踏板事件落在同一 MIDI 刻度，将保留全部事件及顺序。`:'');
  }
  function exportMenu(){
    modal('导出演奏 MIDI',`<label class="field">导出用途<select id="editor-export-purpose" class="control" aria-describedby="editor-export-help"><option value="mapped">独立播放或新工程</option><option value="target_grid">已有速度轨的工程</option><option value="seconds_120">固定 120 BPM 的工程</option></select></label><p id="editor-export-help" class="form-note" role="status">${exportPurposeHelp('mapped')}</p>${exportOpeningLimit()}${(editor()?.tempo?.time_signatures||[]).some(m=>m.free)?'<p class="form-note">标准 MIDI 不支持自由拍号：保留精确播放时序与文字标记，外部 DAW 不一定显示相同的自由小节。</p>':''}<label class="checkbox-line"><input id="editor-export-meter" type="checkbox" checked>包含拍号事件</label>`,`${button('取消','closeDialog')}${button('导出 MIDI','editorExportApply',{style:'primary'})}`,'editor-export');
  }
  function stopScoreRange(){if(scoreRangeTimer)clearTimeout(scoreRangeTimer);scoreRangeTimer=0;scoreRangeRequest=null;scoreRangeSerial++;}
  function scoreRangeAlive(request){const r=editor()?.range;return request===scoreRangeRequest&&request.serial===scoreRangeSerial&&request.project===state.project?.path&&request.start===r?.start&&request.end===r?.end&&tab==='notes'&&dialog==='editor-score-range'&&$('dialog')?.open!==false;}
  function scoreRangeMessage(message,invalid=false){
    const status=$('editor-score-range-status');if(status){status.textContent=message;status.dataset.status=invalid?'error':'running';}
    const img=$('editor-score-range-image');if(img&&invalid){img.classList?.add('is-stale');img.alt='上次预览（已过期，仅供对照）';}
    for(const action of ['editorScoreRangePrev','editorScoreRangeNext','editorScoreRangeOpen']){const b=document.querySelector(`[data-do=${action}]`);if(b)b.disabled=true;}
  }
  function scoreRangeShow(request,result){
    if(!scoreRangeAlive(request))return;
    if((result.validated_revision??result.revision)!==editor()?.revision){scoreRangeMessage('正在核对这段谱面…');scoreRangeTimer=setTimeout(()=>pollScoreRange(request),100);return;}
    if(result.requested_range?.start!==request.start||result.requested_range?.end!==request.end){stopScoreRange();scoreRangeMessage('返回的谱面版本或范围不一致，已丢弃。请重新预览。',true);return;}
    request.result=result;request.pending=false;request.validatedRevision=result.validated_revision??result.revision;
    const page=result.page||{},image=$('editor-score-range-image'),valid=/^data:image\/(png|jpeg);base64,[A-Za-z0-9+/=]+$/.test(page.dataUrl||'');
    if(image){image.classList?.remove('is-stale');image.hidden=!valid;if(valid){image.src=page.dataUrl;image.alt=`选区谱面，第 ${Number(page.index)+1} 页`;if(image.style)image.style.width=`${scoreRangeZoom*100}%`;}else image.removeAttribute('src');}
    const draft=result.preview_kind==='edited_range_draft',status=$('editor-score-range-status');
    if(status){status.textContent=draft?'局部草稿 · 未重新识别节奏':'局部预览 · 与当前片段一致';status.dataset.status='ready';}
    const label=$('editor-score-range-page');if(label)label.textContent=page.pages?`${page.index+1} / ${page.pages}`:'无图像预览';
    const context=$('editor-score-range-context');if(context)context.textContent=`显示 ${fmt(result.context_range?.start)} – ${fmt(result.context_range?.end)} 秒（含邻接小节）。${(result.notation_warnings||[]).join('；')}${valid?'':' 图像暂不可用，可打开 PDF 查看。'}`;
    const prev=document.querySelector('[data-do=editorScoreRangePrev]'),next=document.querySelector('[data-do=editorScoreRangeNext]'),open=document.querySelector('[data-do=editorScoreRangeOpen]');
    if(prev)prev.disabled=!(page.index>0);if(next)next.disabled=!(page.index+1<page.pages);if(open)open.disabled=!result.pdf||result.can_open_pdf!==true;
  }
  function scheduleScoreRangeUpdate(request,targetRevision){
    if(!scoreRangeAlive(request))return;
    request.autoUpdating=true;request.pending=true;request.updateRevision=editor()?.revision;
    scoreRangeMessage('局部预览更新中 · 可继续编辑');
    if(scoreRangeTimer)clearTimeout(scoreRangeTimer);
    if(targetRevision&&targetRevision!==editor()?.revision){scoreRangeTimer=setTimeout(()=>pollScoreRange(request),150);return;}
    scoreRangeTimer=setTimeout(()=>{
      if(!scoreRangeAlive(request))return;
      const next={serial:++scoreRangeSerial,project:request.project,revision:editor().revision,start:request.start,end:request.end,pending:true};
      scoreRangeRequest=next;
      bridge.startScoreRangePreview(JSON.stringify({base_revision:next.revision,start:next.start,end:next.end}),raw=>acceptScoreRangeStart(next,raw));
    },350);
  }
  function pollScoreRange(request){
    scoreRangeTimer=0;if(!scoreRangeAlive(request))return;
    bridge.scoreRangePreviewStatus(request.token,raw=>{
      if(!scoreRangeAlive(request))return;const response=phaseResult(raw);
      if(response.status==='stale'){scheduleScoreRangeUpdate(request,response.current_revision);return;}
      if(response.status==='checking'){request.pending=true;scoreRangeMessage('正在检查这段谱面 · 可继续编辑');}
      if(response.error||response.ok===false||response.status==='failed'){stopScoreRange();scoreRangeMessage(response.error||'选区预览已失效，请重新生成。',true);return;}
      if(response.status==='ready'){scoreRangeShow(request,response.result||{});return;}
      scoreRangeTimer=setTimeout(()=>pollScoreRange(request),500);
    });
  }
  function acceptScoreRangeStart(request,raw){
    if(!scoreRangeAlive(request))return;const response=phaseResult(raw);
    if(response.error||response.ok===false||!response.token){request.pending=false;scoreRangeMessage(response.error||'无法启动选区谱面预览。',true);return;}
    request.token=response.token;scoreRangeTimer=setTimeout(()=>pollScoreRange(request),500);
  }
  function scoreRangeMenu(){
    stopScoreRange();invalidatePreview();if(!editable()||!hasRange())return;
    const r=range();
    if(typeof bridge?.openScoreRangeWindow==='function'){scoreNativeOpened=true;bridge.openScoreRangeWindow(JSON.stringify({project:state.project.path,base_revision:editor().revision,...r}),raw=>{let result;try{result=JSON.parse(raw);}catch{result={error:'无法打开看谱窗口'};}if(result.error){notice=result.error;refresh();}});return;}
    const alreadyOpen=dialog==='editor-score-range'&&$('dialog')?.open;
    if(!alreadyOpen)modal('看谱',`<div class="editor-score-range-toolbar"><span id="editor-score-range-status" role="status">正在准备…</span><span class="editor-score-range-pages">${button('‹','editorScoreRangePrev',{disabled:true,label:'上一页'})}<span id="editor-score-range-page">—</span>${button('›','editorScoreRangeNext',{disabled:true,label:'下一页'})}</span><span class="editor-score-range-zoom">${button('−','editorScoreRangeZoomOut',{label:'缩小谱面'})}<span id="editor-score-range-zoom">${Math.round(scoreRangeZoom*100)}%</span>${button('+','editorScoreRangeZoomIn',{label:'放大谱面'})}${button('适宽','editorScoreRangeFit')}${button('居中','editorScoreRangeCenter',{label:'恢复看谱窗口位置和大小'})}</span></div><div class="editor-score-range-paper"><img id="editor-score-range-image" alt="选区谱面" hidden></div><details class="editor-score-range-details"><summary>范围与说明</summary><p id="editor-score-range-context">显示 L/R 及相邻小节，一次最多 8 小节；不会覆盖正式谱。</p></details>`,`${button('刷新当前 L/R','editorScoreRange')}${button('打开 PDF','editorScoreRangeOpen',{disabled:true})}<span class="editor-score-resize" title="拖动调整窗口大小" aria-label="拖动调整窗口大小"></span>`,'editor-score-range');
    else scoreRangeMessage('正在刷新 · 旧图仅供对照',true);
    dockRangePanel();requestRollDraw();
    if(typeof bridge?.startScoreRangePreview!=='function'||typeof bridge?.scoreRangePreviewStatus!=='function'){scoreRangeMessage('当前运行版本尚未提供局部预览，请重新启动更新后的应用。',true);return;}
    const request={serial:scoreRangeSerial,project:state.project.path,revision:editor().revision,start:r.start,end:r.end,pending:true};scoreRangeRequest=request;
    bridge.startScoreRangePreview(JSON.stringify({base_revision:request.revision,start:r.start,end:r.end}),raw=>acceptScoreRangeStart(request,raw));
  }
  function scoreRangePage(delta){
    const request=scoreRangeRequest;if(!request||!scoreRangeAlive(request)||request.pending||!request.result?.page||typeof bridge?.scoreRangePreviewPage!=='function')return;
    const page=request.result.page,index=page.index+delta;if(index<0||index>=page.pages)return;
    request.pending=true;scoreRangeMessage('正在读取选区页面…');
    bridge.scoreRangePreviewPage(JSON.stringify({token:request.token,index}),raw=>acceptScoreRangeStart(request,raw));
  }
  function zoomScoreRange(multiplier){scoreRangeZoom=multiplier===0?1:Math.max(.5,Math.min(3,scoreRangeZoom*multiplier));const image=$('editor-score-range-image');if(image?.style)image.style.width=`${scoreRangeZoom*100}%`;const value=$('editor-score-range-zoom');if(value)value.textContent=`${Math.round(scoreRangeZoom*100)}%`;}
  function initializeShortcuts(saved){
    const api=window.AstraEditorShortcuts;if(shortcutsInitialized||!api||saved===undefined)return;
    shortcutsInitialized=true;
    try{api.hydrate(saved||{});}catch(error){shortcutSaveError=`无法读取已保存快捷键：${error.message}`;api.hydrate({});}
    confirmedShortcuts=api.bindings();
    api.onChanged(bindings=>{
      shortcutSavePending=true;shortcutSaveError='';
      for(const button of document.querySelectorAll?.('[data-do^="editorShortcut"]')||[])button.disabled=true;
      const done=result=>{
        shortcutSavePending=false;
        if(result?.ok){confirmedShortcuts=result.bindings||bindings;api.hydrate(confirmedShortcuts);}else{api.hydrate(confirmedShortcuts);shortcutSaveError=`未保存，已恢复原快捷键：${result?.error||'本机保存接口不可用'}`;}
        if(dialog==='editor-preferences'){updateShortcutField();const el=$('editor-shortcut-error');el.textContent=shortcutSaveError||'已保存到本机，下次启动继续使用。';if(el.dataset)el.dataset.status=shortcutSaveError?'error':'saved';for(const button of document.querySelectorAll?.('[data-do^="editorShortcut"]')||[])button.disabled=false;}
      };
      if(typeof bridge?.saveEditorShortcuts!=='function'){done({error:'当前运行版本不支持保存快捷键，请重启更新后的应用'});return;}
      bridge.saveEditorShortcuts(JSON.stringify({bindings}),raw=>{let result;try{result=typeof raw==='string'?JSON.parse(raw):raw;}catch{result={error:'无法读取本机保存回执'};}done(result);});
    });
  }
  // Arrow moves use the native clock and command for data; all projected rows
  // below are renderer-only. No fake revision, delay or preview RPC per key.
  let noteNudge=null;
  function nudgeViewLive(f){
    return f&&tab==='notes'&&tool!=='rangeSelect'&&configEditable()&&sameGestureScope(f.base,noteWriteScope())&&
      f.view===noteViewSerial&&f.midi===state.midi.path&&f.ids.length===selected.size&&f.ids.every(id=>selected.has(id));
  }
  function nudgeProjection(rows,command){
    if(command.action==='transpose'){
      let lo=127,hi=0;for(const n of rows){lo=Math.min(lo,n.pitch);hi=Math.max(hi,n.pitch);}
      const shift=Math.max(-lo,Math.min(127-hi,command.semitones));
      return rows.map(n=>({...n,pitch:n.pitch+shift}));
    }
    const shift=command.direction*command.snap.unit,limit=rows.reduce((v,n)=>Math.max(v,n.end),editor().duration);
    const move=t=>beatToSeconds(secondsToBeat(t)+shift);
    const next=rows.map(n=>({...n,start:move(n.start),end:move(n.end)}));
    return next.every(n=>n.start>=-1e-9&&n.end<=limit+1e-6&&validSpan(n.start,n.end))?next:rows;
  }
  function previewQueuedNudge(f,rows){
    f.paint=f.queue.reduce((r,c)=>nudgeProjection(r,c),f.current?nudgeProjection(rows,f.current):rows);
    requestNoteFeedback();
  }
  function sendNoteNudge(f,after=null){
    if(!nudgeViewLive(f)||!f.queue.length){if(noteNudge===f)noteNudge=null;requestNoteFeedback();return;}
    const lookup=noteRenderIndex().byId,rows=f.ids.map(id=>lookup.get(id)?.note);
    if(rows.some(n=>!n)){noteNudge=null;requestNoteFeedback();return;}
    f.current=f.queue.shift();f.seen=noteWriteScope();
    previewQueuedNudge(f,rows);
    const sent=submitSemantic(f.current,{},(error,r)=>{
      if(noteNudge!==f)return;
      const here=noteWriteScope(),own=r?.status==='committed'&&r.applied&&!error&&!r.warning&&!r.cancelled&&
        here?.request===r.request&&here.revision===r.revision&&here.version>f.seen.version;
      f.current=null;
      if(own&&nudgeViewLive(f)&&f.queue.length)sendNoteNudge(f,r.request);
      else{noteNudge=null;requestNoteFeedback();}
    },{after});
    if(!sent&&noteNudge===f){noteNudge=null;requestNoteFeedback();}
  }
  function positionNudge(command){
    if(noteNudge&&nudgeViewLive(noteNudge)){
      // The same authoritative selection remains targeted throughout repeat.
      if(noteNudge.queue.length<32){
        noteNudge.queue.push(command);
        noteNudge.paint=nudgeProjection(noteNudge.paint,command);
        requestNoteFeedback();
      }
      return true;
    }
    if(!editable())return false;
    const base=noteWriteScope();
    if(!base)return submitSemantic(command);
    const f=noteNudge={base,ids:command.ids,inkIds:new Set(command.ids),view:noteViewSerial,midi:state.midi.path,queue:[command],paint:[]};
    sendNoteNudge(f);return true;
  }
  function paintNoteNudge(ctx,offset,height,width,startSeconds,endSeconds){
    const f=noteNudge;
    if(!nudgeViewLive(f)||!sameGestureBase(f.seen,noteWriteScope()))return null;
    const {high,row}=rollGeometry,motion=window.AstraRhythmMotion;
    const vx=motion?.active?.()?seconds=>motion.x(seconds,offset):midiX;
    const {start,end}=motion?.bounds(startSeconds,endSeconds)??{start:startSeconds,end:endSeconds};
    paintNoteSelectionInk(ctx,ring=>{
      for(const n of f.paint){
        if(n.end<start||n.start>end)continue;
        const y=(high-n.pitch)*row+1;
        if(y<0||y>=height)continue;
        const first=vx(n.start)-offset,last=vx(n.end)-offset;
        if(first>width||last<0)continue;
        const x=Math.max(0,first),w=Math.max(2,Math.min(width,last)-x);
        ring(x,y,w,row-2,selectionNoteColor(n),true,true,first>=0?n.pitch:null);
      }
    });
    return f.inkIds;
  }
  function positionArrow(event,k,ctrl){
    const chord=window.AstraEditorShortcuts?.canonical?.(event)||'';
    if(['Ctrl+ArrowLeft','Ctrl+ArrowRight'].includes(chord)){
      nudge(k==='arrowright'?1:-1,'move');return true;
    }
    if(!ctrl&&!event.altKey&&['arrowup','arrowdown'].includes(k)&&selected.size){
      const semitones=(k==='arrowup'?1:-1)*(event.shiftKey?12:1);
      return positionNudge({...semanticSelection('transpose'),semitones,policy:'clamp'});
    }
    return false;
  }
  // Cubase Nudge (Link to Grid): move or trim by one current grid unit in musical time.
  function nudge(dir,part){
    if(tool==='rangeSelect'){
      const r=commandRange();if(r)selectionEdit({action:'nudge',token:r.token,direction:dir,part,snap:wireSnap()});
      return;
    }
    const domain=ccSelected.size&&!selected.size?'controls':'notes';
    const command=semanticSelection('nudge',domain);
    if(!command.ids.length||domain==='controls'&&part!=='move')return;
    Object.assign(command,{direction:dir,part,snap:{unit:snapUnit()}});
    if(domain==='notes'&&part==='move')positionNudge(command);
    else submitSemantic(command);
  }
  function setLocatorAtCursor(which){
    const at=playheadSeconds(),r=range();
    if(which==='start'?at>=r.end:at<=r.start){notice=which==='start'?'L 必须早于 R；先移动 R 或换个位置。':'R 必须晚于 L；先移动 L 或换个位置。';refresh();return;}
    send('setEditorRange',{start:which==='start'?at:r.start,end:which==='end'?at:r.end,loop:Boolean(r.loop)});
  }
  function shortcutMenu(){
    const api=window.AstraEditorShortcuts;
    if(!api){notice='快捷键模块尚未加载，请重新启动更新后的应用。';refresh();return;}
    const bindings=api.bindings();
    modal('快捷键',`<p class="form-note">默认主命令参照 Cubase 16 官方指南。修改只保存在本机，不改工程。</p><label class="field">命令<select id="editor-shortcut-command" class="control">${api.commands.map(c=>`<option value="${c.action}">${html(c.label)} · ${html(bindings[c.action]||'未分配')}</option>`).join('')}</select></label><label class="field">按下新组合键<input id="editor-shortcut-binding" class="control" readonly placeholder="点击后按键；不会立即生效" aria-label="录制快捷键"></label><p id="editor-shortcut-current" class="form-note"></p><p id="editor-shortcut-error" class="form-error" role="alert"></p><details class="editor-correction-help"><summary>哪些操作不能改键？</summary><p>按住 B 临时绕过吸附（Astra）；Escape 取消，箭头导航 / 改音高，Space 试听。Ctrl + / − / 0 与滚轮缩放、输入框编辑及系统窗口键保留，不可在这里覆盖。</p><p>Ctrl 拖动临时取消时间吸附（可斜向移动）、Alt 拖动复制、Shift 多选属于手势，不是可重新分配的单键命令。</p></details><details class="editor-correction-help"><summary>另有 Cubase 默认键（固定）</summary><p>H / G 时间缩放 · Shift+F 适合全曲 · F 自动跟随 · Alt+I 或 Ctrl+Alt+L 乐器与 MIDI 侧栏 · Ctrl+I 信息行 · Ctrl+Alt+T 走带 · 2 范围工具 · 5 擦除 · Ctrl+X 剪切 · Ctrl+D 重复 · Ctrl+Shift+A 取消选择 · Delete / Backspace 删除 · Ctrl+←/→ 按网格微移 · Alt+← 起点左移 · Alt+Shift+→ 起点右移 · Alt+Shift+← 终点左移 · Alt+→ 终点右移 · Ctrl+小键盘1/2 把 L/R 设到播放位置。</p><p>原有 Ctrl + / − / 0 缩放与 B 临时绕过吸附继续可用。若你在上面把同一组合分配给其他命令，以你的分配为准。</p></details>`,`${button('返回设置','appPreferences',{key:'keys'})}${button('恢复快捷键默认','editorShortcutReset')}${button('清除此命令','editorShortcutClear')}${button('分配','editorShortcutAssign',{style:'primary'})}`,'editor-preferences');
    updateShortcutField();if(shortcutSaveError)$('editor-shortcut-error').textContent=shortcutSaveError;
  }
  function updateShortcutField(){
    const api=window.AstraEditorShortcuts,command=api?.commands.find(c=>c.action===$('editor-shortcut-command')?.value);if(!command)return;
    $('editor-shortcut-binding').value=api.bindings()[command.action]||'';
    $('editor-shortcut-current').textContent=`当前：${api.bindings()[command.action]||'未分配'} · Cubase 16 默认：${command.defaultBinding||'未分配'}`;
    $('editor-shortcut-current').title=command.source||'';$('editor-shortcut-error').textContent='';
    for(const option of $('editor-shortcut-command').options||[]){const item=api.commands.find(c=>c.action===option.value);if(item)option.textContent=`${item.label} · ${api.bindings()[item.action]||'未分配'}`;}
  }
  let repeatDraft=null;
  function repeatCommand(){
    const draft=repeatDraft,count=Number($('editor-repeat-count')?.value),step=Number($('editor-repeat-step')?.value);
    if(!draft||!semanticCurrent(draft.base))return {error:'工程已更新，请重新打开重复。'};
    if(!Number.isInteger(count)||count<1||count>999)return {error:'重复次数为 1–999 的整数。'};
    if(!Number.isFinite(step)||step<=0)return {error:'重复间隔必须大于 0 秒。'};
    return {command:{...draft.selection,action:'repeat',count,step_seconds:step}};
  }
  function repeatStatus(text,status,disabled){
    const el=$('editor-repeat-status');if(el){el.textContent=text;el.dataset.status=status;}
    const apply=document.querySelector('[data-do="editorRepeatApply"]');if(apply)apply.disabled=disabled;
  }
  function repeatPreview(){
    const draft=repeatDraft,{command,error}=repeatCommand();
    const ticket={};if(draft)draft.preview=ticket;
    if(error){repeatStatus(error,'error',true);return;}
    const alive=()=>repeatDraft===draft&&draft.preview===ticket&&dialog==='editor-repeat'&&$('dialog')?.open&&semanticCurrent(draft.base);
    repeatStatus('正在检查…','running',false);
    previewSemantic(draft.base,semanticOperation(command)).then(data=>{
      if(!alive())return;
      const p=data.semantic_preview;
      if(p.blocked){repeatStatus(semanticBlocked(command,data.semantic_warnings),'error',true);return;}
      const replaced=(data.events?.normalized||[]).filter(row=>!('add_index' in row)).length;
      repeatStatus(`新增 ${p.total} 个事件 · 末尾 ${fmt(p.end)} 秒${replaced?` · 替换 ${replaced} 个踏板点`:''}`,'ready',false);
    },error=>{if(alive())repeatStatus(error.message,'error',false);});
  }
  function repeatOpen(){
    if(!editable()||drag)return;
    if(selected.size&&ccSelected.size){notice='请分别选择音符或踏板点后重复。';refresh();return;}
    const kind=ccSelected.size?'controls':'notes',selection=semanticSelection('repeat_defaults',kind);
    if(!selection.ids.length){notice='请先选择要重复的事件。';refresh();return;}
    const draft=repeatDraft={base:semanticBase(),selection},priorDialog=dialog;
    const alive=()=>repeatDraft===draft&&dialog===priorDialog&&semanticCurrent(draft.base);
    previewSemantic(draft.base,semanticOperation(selection)).then(({semantic_preview:p})=>{
      if(!alive()||!editable()||drag||selectionKey()!==draft.selectionKey)return;
      const step=p.default_step_seconds;
      modal('重复',`<label class="field">新增副本数<input id="editor-repeat-count" class="control" type="number" min="1" max="999" step="1" value="1"></label><label class="field">重复间隔（秒）<input id="editor-repeat-step" class="control" type="number" min="0.000001" step="any" value="${step>0?step:''}"></label><small>${p.from_range?'间隔取自 L/R 长度':kind==='controls'?'间隔取自首末踏板点；同刻度目标将被替换':'间隔取自选区起止跨度'}</small><p id="editor-repeat-status" role="status"></p>`,`${button('取消','closeDialog')}${button('重复','editorRepeatApply',{style:'primary'})}`,'editor-repeat');
      repeatPreview();
    },error=>{if(alive()){notice=error.message;refresh();}});
    draft.selectionKey=selectionKey();
  }
  function deletionOperation(){
    if(ccSelected.size){
      const ids=ccEvents().filter(c=>ccSelected.has(c.id)).map(c=>c.id);
      return ids.length?{type:'controls',delete:ids}:null;
    }
    return selected.size?{type:'notes',delete:[...selected]}:null;
  }
  const clipboardCount=()=>copyRead?.count??(clipboard?.type==='selection_range'?1:clipboard?.count??0);
  function copySelection(cut=null){
    if(!active())return;
    const r=commandRange(),isRange=tool==='rangeSelect';
    if(isRange&&(!r||r.end===r.start||!editable()||drag))return;
    const domain=isRange?'selection_range':ccSelected.size&&!selected.size?'controls':'notes';
    const command=isRange?{action:'copy',token:r.token}:semanticSelection('copy',domain);
    const operation=isRange?selectionOperation(command,selectionContext()):semanticOperation(command);
    const read=copyRead={base:semanticBase(),count:isRange?1:command.ids.length,paste:null,version:++clipboardVersion};
    const failure=error=>{
      const latest=copyRead===read;if(latest)copyRead=null;
      if(latest&&semanticCurrent(read.base)||read.paste&&semanticCurrent(read.paste.base)){notice=error.message;refresh();}
    };
    previewSemantic(read.base,operation).then(({semantic_preview:p})=>{
      if(typeof p.clipboard_json!=='string'||(isRange?p.clipboard_type!==domain:
         p.clipboard?.type!==domain||!Array.isArray(p.clipboard.rows)||p.clipboard.rows.length!==read.count)){
        failure(new Error('无法读取复制结果；剪贴板未更改。'));return;
      }
      const clip={type:domain,count:read.count,wire:p.clipboard_json};
      if(copyRead===read){
        copyRead=null;clipboard=clip;
        if(semanticCurrent(read.base))notice=isRange?'已复制时间选区（含空白）。':domain==='controls'?`已复制 ${clip.count} 个 CC64 点；Ctrl+V 粘贴到播放位置。`:`已复制 ${clip.count} 音；Ctrl+V 粘贴到播放位置。`;
        refresh();
      }
      // Cut must never delete before canonical copy, nor delete a new view's
      // selection. It retains the existing delete transaction and busy rule.
      if(cut&&semanticCurrent(read.base)&&editable()&&!window.AstraStructureEditor?.guardAction('editorDelete'))edit(cut);
      if(read.paste&&(!isRange||read.version===clipboardVersion))commitPaste(read.paste,clip);
    },failure);
  }
  function commitPaste(intent,clip){
    if(!clip?.count||!semanticCurrent(intent.base)||!editable()||drag)return;
    if(clip.type==='selection_range'){
      selectionEdit({action:'paste',at:intent.at},{...selectionContext(),clipboard:clip.wire,cursor_seconds:intent.cursor});return;
    }
    const controls=clip.type==='controls';
    if(controls&&ccLaneHidden)toggleCCLane();
    const command={action:'paste',at:intent.at,...(controls?{channel:intent.channel}:{})};
    const view={clipboard:clip.wire};
    if(intent.at==='cursor')view.cursor_seconds=intent.cursor;
    submitSemantic(command,view);
  }
  function pasteSelection(at){
    if(!editable()||drag||!clipboardCount())return;
    const cursor=at==='cursor'?playheadSeconds():0;
    const intent={base:semanticBase(),at,channel:ccChannel,cursor:Number.isFinite(cursor)?Math.max(0,cursor):0};
    // The first paste waiting on this exact read takes the same slot that an
    // immediate paste would have taken in the ordinary busy lane. Other edits
    // and navigation are not locked by the read. Never rebase a delayed paste.
    if(copyRead){
      if(!copyRead.paste||!semanticCurrent(copyRead.paste.base))copyRead.paste=intent;
    }else commitPaste(intent,clipboard);
  }
  function action(name,key){
    if(window.AstraBeatChecks?.action(name))return true;
    // The pressed half of the mode pair: already in that mode, nothing to do.
    if(name==='editorEnter'&&(!editor()||active())||name==='editorReadOnly'&&!active())return;
    if(editLocked()&&canOrderCommand(name)){
      // A range transpose must bind the committed token at the click, not
      // become a generic command to re-target after another edit.
      if(!sidebarNoteActions.has(name)||tool!=='rangeSelect')queueEditCommand(name,key);
      return;
    }
    if(name==='editorExternalMidiOpen'){if(active()&&!editLocked())dispatch('openFile',{path:path()});return;}
    if(name==='editorScoreRangeCenter'){scoreFloatRect={x:((window.innerWidth||1200)-760)/2,y:((window.innerHeight||800)-420)/2,w:760,h:420};placeScoreWindow();saveScoreWindow();return;}
    if(name==='editorMotionToggle'){window.AstraPreferences?.open('appearance');return;}
    if(name==='editorDismissModeNotice'){modeNotice='';refresh();return;}
    if(window.AstraRhythmTimeline?.action?.(name,key))return;
    if(window.AstraStructureEditor?.guardAction(name))return;
    if(['editorSelectTool','editorRangeTool','editorDrawTool','editorPenToggle','editorBeatTool','editorBeatEraseTool','editorMeterTool','editorMapTool'].includes(name))leaveSelectionView();
    if(tool==='rangeSelect'&&['editorDelete','editorCut','editorDuplicate','editorSelectionMembership'].includes(name)){
      if(rangeKeyContinuation(name))return;
      const r=commandRange();if(!r||drag)return;
      if(name==='editorSelectionMembership'){
        if(!['notes','cc64','notes_cc64','full'].includes(key)||ccLaneHidden&&['cc64','notes_cc64'].includes(key))return;
        selectionEdit({action:'scope',token:r.token,scope:{mode:key,...(['cc64','notes_cc64'].includes(key)?{channel:ccChannel}:{})}});
      }else selectionEdit({action:({editorDelete:'delete',editorCut:'cut',editorDuplicate:'duplicate'})[name],token:r.token});
      return;
    }
    if(['editorRangeMenu','editorScoreRange','editorPreferences','editorKeyCommands','editorExport','editorRhythmCandidates','editorMeterEvent'].includes(name)&&window.AstraStructureEditor?.guardNavigation())return;
    if(name==='editorPitchZoomIn'||name==='editorPitchZoomOut'){setPitchZoom(pitchZoom*(name==='editorPitchZoomIn'?1.25:.8));return;}
    if(name==='editorCCLaneToggle'){toggleCCLane();return;}
    if(name==='editorInvertSelection'){
      if(!editable()||drag||tool==='rangeSelect'||selectionDomain==='none')return;
      if(selectionDomain==='controls'){selected.clear();ccSelected=new Set(ccEvents().filter(c=>!ccSelected.has(c.id)).map(c=>c.id));selectedCC=[...ccSelected][0]||null;}
      else {selected=new Set(editor().notes.filter(n=>!selected.has(n.id)).map(n=>n.id));ccSelected.clear();selectedCC=null;}
      selectedBeatFlag=null;window.AstraRhythmTimeline?.clearSelection?.();refresh();return;
    }
    if(name==='editorCCZoomIn'||name==='editorCCZoomOut'){setPedalHeight(rollLanes.pedal.height+(name==='editorCCZoomIn'?24:-24));return;}
    if(name==='editorWaveform'){
      waveformCollapsed=!waveformCollapsed;
      try { window.localStorage?.setItem('astra-reference-waveform-collapsed',waveformCollapsed?'1':'0'); } catch {}
      const control=document.querySelector('[data-do=editorWaveform]');
      control?.setAttribute('aria-expanded',String(!waveformCollapsed));
      if(control?.parentElement)control.parentElement.dataset.collapsed=String(waveformCollapsed);
      requestRollDraw();
      return;
    }
    if(name==='editorInspector'){
      if(!active()||inspectorCovered())return;
      inspectorCollapsed=!inspectorCollapsed;
      try { window.localStorage?.setItem('astra-inspector-collapsed',inspectorCollapsed?'1':'0'); } catch {}
      const side=document.querySelector('.editor-side');
      if(inspectorCollapsed&&(side||$('editor-inspector'))?.contains(document.activeElement))$('roll-scroll')?.focus({preventScroll:true});
      dockRangePanel();
      requestRollDraw();
      return;
    }

    // One Settings destination: contextual 快捷键 entries open its 快捷键 page;
    // the recorder itself is unchanged and returns there.
    if(name==='editorPreferences'){if(typeof window.AstraPreferences?.open==='function')window.AstraPreferences.open('keys');else shortcutMenu();return;}
    if(name==='editorKeyCommands'){shortcutMenu();return;}
    if(name.startsWith('editorShortcut')){if(shortcutSavePending)return;try{const api=window.AstraEditorShortcuts;if(name==='editorShortcutReset')api.reset();else api.set($('editor-shortcut-command').value,name==='editorShortcutClear'?'':$('editor-shortcut-binding').value);updateShortcutField();$('editor-shortcut-error').textContent=shortcutSavePending?'正在保存到本机…':shortcutSaveError||'当前窗口已应用。';if($('editor-shortcut-error').dataset)$('editor-shortcut-error').dataset.status=shortcutSaveError?'error':'pending';}catch(error){$('editor-shortcut-error').textContent=error.message;if($('editor-shortcut-error').dataset)$('editor-shortcut-error').dataset.status='error';}return;}
    if(name==='editorSelectAll'){
      if(!editable()||drag||tool==='rangeSelect'||selectionDomain==='none')return;
      kbdSel=null;
      if(selectionDomain==='controls'){selected.clear();ccSelected=new Set(ccEvents().map(c=>c.id));selectedCC=[...ccSelected][0]||null;}
      else {selected=new Set(editor().notes.map(n=>n.id));selectedCC=null;ccSelected.clear();}
      selectedBeatFlag=null;refresh();return;
    }
    if(name==='editorLocateSelection'&&selected.size){const notes=inspectNotes();send('setEditorRange',{start:Math.min(...notes.map(n=>n.start)),end:Math.max(...notes.map(n=>n.end)),loop:Boolean(range().loop)});return;}
    if(name==='editorSelectTool'){const r=shownSelection();abortGesture();handoffRangeNotes(r);tool='select';refresh();}
    if(name==='editorRangeTool'){abortGesture();clearRangeObjects();window.AstraRhythmTimeline?.clearSelection?.();selectionDomain='notes';tool='rangeSelect';refresh();}
    if(name==='editorPenToggle'){abortGesture();tool=tool==='draw'?'beat':'draw';notice=tool==='beat'?'拍点笔 · 再点切换画笔':'画笔 · 再点切换拍点笔';refresh();}
    if(name==='editorDrawTool'){tool='draw';refresh();}
    if(name==='editorBeatTool'){abortGesture();tool='beat';refresh();}
    if(name==='editorMeterTool'){abortGesture();tool='select';refresh();}
    if(name==='editorBeatEraseTool'){abortGesture();tool='eraseBeat';refresh();}
    if(name==='editorMapTool'){abortGesture();tool=tool==='map'?'select':'map';notice=tool==='map'?'拖动校准点，松手后预览。已有拍位锁保持原位。':'';refresh();}
    if(name==='editorDetectedSnap'){const detected=detectedBeats();if(detected.available){snap=snap==='detected'?'grid':'detected';notice='原始检测拍使用已有音频位置。开启吸附后生效。';}else notice=detected.reason;refresh();}
    if(name==='editorSnapToggle'){snapEnabled=!snapEnabled;refresh();}
    if(name==='editorSettings'){settingsOpen=!settingsOpen;refresh();}
    // The same controller command serves the footer button, View and the shortcut.
    // A programmatic click still reaches that command when the footer has no box.
    if(name==='editorTransportFold'){window.AstraTransportPanel?.toggleFold?.();return;}
    if(name==='editorInfoLineToggle'){
      const bar=$('editor-info-bar');if(!bar)return;
      infoLineHidden=!infoLineHidden;
      if(infoLineHidden&&bar.contains(document.activeElement))$('roll-scroll')?.focus({preventScroll:true});
      bar.hidden=infoLineHidden;
      try{window.localStorage?.setItem('astra-info-line-hidden',infoLineHidden?'1':'0');}catch{}
      if(!infoLineHidden)refreshInfoLine();
      syncViewControl('editorInfoLineToggle');requestRollDraw();return;
    }
    if(name==='editorSelectNone'){const r=shownSelection();if(tool==='rangeSelect'){if(r&&!drag&&!rangeMasked())selectionEdit({action:'clear',token:r.token},selectionContext(),emptyRangePaint);requestRollDraw();return;}selected.clear();ccSelected.clear();selectedCC=null;selectedBeatFlag=null;window.AstraRhythmTimeline?.clearSelection?.();refresh();return;}
    if(name==='editorCut'&&editable()){const cut=deletionOperation();if(cut)copySelection(cut);return;}
    if(name==='editorRepeat'){repeatOpen();return;}
    if(name==='editorPasteAtOrigin'){pasteSelection('origin');return;}
    if(name==='editorRepeatApply'){
      if(dialog!=='editor-repeat'||!$('dialog')?.open||!editable())return;
      const draft=repeatDraft,{command,error}=repeatCommand();
      if(error){repeatStatus(error,'error',true);return;}
      draft.preview=null;
      submitSemantic(command,{},(error,receipt)=>{
        if(repeatDraft!==draft||dialog!=='editor-repeat'||!$('dialog')?.open)return;
        if(error){repeatStatus(error,'error',Boolean(receipt?.semantic_preview?.blocked));return;}
        repeatDraft=null;$('dialog')?.close();
      });return;
    }
    if(name==='editorDuplicate'&&editable()){
      const command=semanticSelection('duplicate',ccSelected.size&&!selected.size?'controls':'notes');
      if(command.ids.length)submitSemantic(command,{},null,{selectAdded:command.domain==='notes'});return;
    }
    if(name==='editorCCState'){
      if(!editable())return;
      const ctx=infoContext(),value=Number(key);if(ctx.kind!=='cc'||(value!==0&&value!==127))return;
      submitSemantic({...semanticSelection('cc_state','controls'),value});return;
    }
    if(name==='editorInfoMode'){infoAbsolute=key==='absolute';refreshInfoLine();return;}
    if(name==='editorTranspose'&&selected.size&&editable()){
      const semitones=Number(key);
      if(Number.isInteger(semitones))submitSemantic({...semanticSelection('transpose'),semitones,policy:'reject'});
      return;
    }
    if(name==='editorTransposeStep'&&configEditable()){tools.transpose=key==='reset'?0:Math.max(-48,Math.min(48,tools.transpose+Number(key)));if(busy)updateToolLabels();else refresh();return;}
    if(name==='editorTransposeSet'&&configEditable()){tools.transpose=Number(key)||0;if(busy)updateToolLabels();else refresh();return;}
    if(name==='editorTransposeApply'){
      if(!editable()||rangeSidebarBusy())return;
      const choice=sidebarTargets(name);
      if(!choice.notes.length)return;
      const semitones=tools.transpose;
      const target=sidebarCommand('transpose',choice);
      toolEdit('transpose',{...target,semitones,policy:'reject'},(p,r)=>r.changed?
        `已移调 ${signed(semitones)}（${intervalName(semitones)}）· ${p.changed} 个音符；Ctrl+Z 撤销`:'移调量为 0，没有改动。',sidebarContext(choice));return;
    }
    if(name==='editorMeterAtPlayhead'&&editable()){window.AstraRhythmTimeline?.open('meter');return;}
    if(name==='editorLengthScale'){
      if(!editable()||rangeSidebarBusy())return;
      const choice=sidebarTargets(name);
      if(!choice.notes.length)return;
      toolEdit('length',{...sidebarCommand('length_scale',choice),percent:tools.scale},(p,r)=>r.changed?
        `长度已缩放到 ${p.percent}% · ${p.changed} 个音符；Ctrl+Z 撤销`:'长度没有改动。',sidebarContext(choice));return;
    }
    if(name==='editorPedalsToLength'){previewPedalLength();return;}
    if(name==='editorPedalsToLengthCommit'){commitPedalLength();return;}
    if(['editorLegato','editorLegatoFreeze','editorFixedLengths','editorDeleteOverlaps'].includes(name)){
      if(!editable()||rangeSidebarBusy())return;
      const freeze=name==='editorLegatoFreeze',choice=lengthChoice(name,freeze),view=sidebarContext(choice);
      if(!choice.notes.length)return;
      let command;
      if(name==='editorFixedLengths')command={...sidebarCommand('fixed_lengths',choice),preset:qState.preset,length:quantizeLength()};
      else if(name==='editorDeleteOverlaps'&&['mono','poly'].includes(key))command={...sidebarCommand('delete_overlaps',choice),mode:key};
      else if(name==='editorLegato'||freeze){
        const percent=freeze?tools.legatoAdjust:100;
        if(!percent||!lengthLegatoReady(choice,percent))return;
        command=lengthLegatoCommand(choice,percent);
      }else return;
      const was=tools.legatoAdjust;
      toolEdit('length',command,(_,receipt)=>{
        if(receipt?.changed&&tools.legatoAdjust===was){tools.legatoAdjust=0;lengthBox=null;}
        return '';
      },view);return;
    }
    if(name==='editorVelocityScale'&&selected.size&&editable()){
      toolEdit('velocity',{...semanticSelection('velocity'),mode:'dynamics',percent:tools.velocity},(p,r)=>{
        const tail=p.clipped?`；${p.clipped} 个限制在 1–127` : '';
        return r.changed?`力度动态已缩放到 ${p.percent}% · ${p.changed} 个音符${tail}；Ctrl+Z 撤销`:`力度没有改动${tail}。`;
      });return;
    }
    if(name==='editorQTarget'&&Q_TARGETS.some(([k])=>k===key)){qState.target=key;saveQuantize();if(busy)updateToolLabels();else refresh();return;}
    if(name==='editorLegatoMode'&&['selected','any'].includes(key)){qState.legato=key;saveQuantize();planLengthPreview();if(busy)updateToolLabels();else refresh();return;}
    if(name==='editorQuantize'){
      if(!editable()||rangeSidebarBusy())return;
      const target=Q_TARGETS.some(([k])=>k===key)?key:qState.target,choice=sidebarTargets(name,target),notes=choice.notes;
      if(!notes.length){setToolStatus('quantize','先选中要量化的音符；精度和强度可以先设好。','info');return;}
      toolEdit('quantize',quantizeCommand(target,choice),(p,r)=>r.changed?
        `${qTargetName(p.target)}已量化 · ${presetName(p.unit)} · 强度 ${p.strength}% · ${p.changed} 个${p.short?`；${p.short} 个短音保持`:''}${p.edge?`；${p.edge} 个越界未改`:''}；Ctrl+Z 撤销`:quantizeNoChange(p),sidebarContext(choice));
      return;
    }
    if(name==='editorReadOnly'){setReadOnly(true);return;}
    if(name==='editorEnter'){
      const sameWorking=workingView(),target=path(),project=state.project?.path,doc=editor();
      const at=typeof midiDisplayPosition==='function'?midiDisplayPosition():(state.midi.position||0),r={...range()};
      if(!setReadOnly(false))return;
      if(!sameWorking&&target&&state.midi.path!==target)dispatch('selectMidi',{path:target},()=>{
        const installed=editor();
        if(!active()||state.midi.path!==target||state.project?.path!==project||
          installed?.edit_stamp?.generation!==doc?.edit_stamp?.generation||
          installed?.source_key!==doc?.source_key)return;
        if(hasLocators(r))send('setEditorRange',r);
        dispatch('midiSeek',{position:at});
      });
      return;
    }
    if(name==='editorTogether')return; // Joint comparison is retired; ignore stale UI actions.
    if(name==='editorMetronome')dispatch('metronome',{enabled:!editor().metronome});
    if(name==='editorUndo'||name==='editorRedo'){
      if(deferNoteHistory(name))return;
      abortNoteGesture();abortSelectionGesture();
      const undo=name==='editorUndo';
      if(!window.AstraStructureEditor?.history(undo?'undo':'redo')&&(undo?editor()?.can_undo:editor()?.can_redo))send(undo?'undoProject':'redoProject');
      return;
    }
    if(name==='editorScore')send('exportEditor',{mode:'all',purpose:'mapped',include_time_signatures:true});
    if(name==='editorExport')exportMenu();
    if(name==='editorExportApply'){const purpose=$('editor-export-purpose').value,include_time_signatures=$('editor-export-meter').checked;$('dialog').close();send('exportEditor',{mode:'midi',purpose,include_time_signatures});}
    if(name==='editorFocusLeft'||name==='editorFocusRight'){const input=$(name==='editorFocusLeft'?'editor-music-start':'editor-music-end');input?.focus();input?.select();return;}
    if(name==='editorJumpLeft')withPlayer('seek','midiSeek',{position:(window.AstraTransportPanel?.range()||range()).start});
    if(name==='editorJumpRight')withPlayer('seek','midiSeek',{position:(window.AstraTransportPanel?.range()||range()).end});
    if(name==='editorTransportPlay'&&!(transportMidi?state.midi.playing||state.midi.play_pending:state.player.playing))withPlayer('playPause','midiPlayPause');
    if(name==='editorTransportStop')withPlayer('stop','midiStop');
    if(name==='editorLoop'){const r=window.AstraTransportPanel?.range()||range();if(hasLocators(r))send('setEditorRange',{...r,loop:!r.loop});}
    if(name==='editorLocateDiagnostic'){
      const hint=diagnosticRanges()[Number(key)];if(!editable()||!hint?.valid){notice='此分析范围当前无法定位；请检查是否仍在人工编辑版本。';refresh();return;}
      const start=Math.max(0,hint.start-2),end=Math.min(duration(),hint.end+2);
      const scroll=$('roll-scroll');if(scroll)scroll.scrollLeft=Math.max(0,midiX(start)-scroll.clientWidth*.15);
      send('setEditorRange',{start,end,loop:Boolean(range().loop)});requestRollDraw();return;
    }
    if(name==='editorRangeMenu')rangeMenu();
    if(name==='editorRhythmCandidates')rangeMenu('rhythm_candidate');
    if(name==='editorRhythmCPU')startPhasePreview('cpu');
    if(name==='editorRhythmGPU')startPhasePreview('gpu');
    if(name==='editorQualityLocate')locateQualityRange(key);
    if(name==='editorQualityPropose')startPhasePreview('prefer_gpu','propose');
    if(name==='editorQualityDiscover')startPhasePreview('prefer_gpu','discover');
    if(name==='editorScoreEnd')scoreEndMenu(false);
    if(name==='editorClearScoreEnd')scoreEndMenu(true);
    if(name==='editorConfirmScoreEnd'&&scoreEndDraft?.revision===editor().revision){const operation=scoreEndDraft.operation;scoreEndDraft=null;$('dialog').close();edit(operation);}
    if(name==='editorScoreRange')scoreRangeMenu();
    if(name==='editorScoreRangePrev')scoreRangePage(-1);
    if(name==='editorScoreRangeNext')scoreRangePage(1);
    if(name==='editorScoreRangeZoomIn')zoomScoreRange(1.25);
    if(name==='editorScoreRangeZoomOut')zoomScoreRange(.8);
    if(name==='editorScoreRangeFit')zoomScoreRange(0);
    if(name==='editorScoreRangeOpen'&&scoreRangeRequest&&scoreRangeAlive(scoreRangeRequest)&&scoreRangeRequest.result?.pdf&&scoreRangeRequest.result?.can_open_pdf===true)dispatch('openFile',{path:scoreRangeRequest.result.pdf});
    if(name==='editorMeterEvent'){
      const meter=editor()?.tempo?.time_signatures?.[Number(key)];
      if(meter&&editable())window.AstraRhythmTimeline?.open('meter',beatToSeconds(meter.beat));
    }
    if(name==='editorDirectMeter')window.AstraRhythmTimeline?.open('meter');
    if(name==='editorDirectRecount')window.AstraRhythmTimeline?.open('recount');
    if(name==='editorApplyDirectRhythm')window.AstraRhythmTimeline?.apply();
    if(name==='editorApplyRange')applyRange();
    if(name==='editorPreviewRange')previewRange();
    if(name==='editorNoteApply'){
      const note=inspectNotes()[0],names=['start','end','pitch','velocity'];
      if(!editable()||!note||names.some(f=>!$(`editor-note-${f}`)))return;
      const fields=Object.fromEntries(names.map(f=>[f,Number($(`editor-note-${f}`).value)]));
      submitSemantic({action:'form_assign',domain:'notes',id:note.id,fields});return;
    }
    if(name==='editorCCApply'){
      const old=ccEvents().find(c=>c.id===selectedCC),t=$('editor-cc-time'),v=$('editor-cc-value');
      if(!editable()||!old||!t||!v)return;
      const seconds=Number(t.value),value=Number(v.value);
      if(!String(t.value).trim()||!String(v.value).trim()||!Number.isFinite(seconds)||!Number.isFinite(value)){
        notice='无效踏板编辑；原值未修改';refresh();return;
      }
      submitSemantic({action:'form_assign',domain:'controls',channel:ccChannel,id:old.id,fields:{seconds,value}});return;
    }
    if(name==='editorDelete'){const op=deletionOperation();if(op)edit(op);return;}
    if(name==='editorCopy'){copySelection();return;}
    if(name==='editorPaste')pasteSelection('cursor');
  }
  function changed(id,value){
    if(id==='editor-insert-velocity'||id==='editor-insert-velocity-preset'){
      if(id==='editor-insert-velocity'||value)setInsertVelocity(value);
      if(id==='editor-insert-velocity-preset')$('editor-insert-velocity-preset').value='';
      return;
    }
    if(dialog==='editor-repeat'&&['editor-repeat-count','editor-repeat-step'].includes(id)){repeatPreview();return;}
    if(id==='editor-transpose-amount'){const v=Math.round(Number(value));if(String(value).trim()!==''&&Number.isFinite(v))tools.transpose=Math.max(-48,Math.min(48,v));refresh();return;}
    if(window.AstraRhythmTimeline?.changedOperation?.(id))return;
    if(dialog==='editor-rhythm'&&id.startsWith('editor-rhythm-')){window.AstraRhythmTimeline?.changed();return;}
    if(id==='editor-play-position'){try{const position=window.AstraTransportPanel?.positionMode()==='music'&&window.AstraTransportPanel?.hasMap()?window.AstraRhythmTimeline.parseMusicPosition(value):Number(value);if(!Number.isFinite(position)||position<0)throw new Error('请输入有效的非负秒位置。');withPlayer('seek','midiSeek',{position});}catch(error){window.AstraTransportPanel?.error(id,error.message);}return;}
    if(id==='editor-export-purpose'){const help=$('editor-export-help');if(help)help.textContent=exportPurposeHelp(value);return;}
    if(id==='editor-rhythm-choice'){chooseRhythmCandidate(value);return;}
    if(id==='editor-shortcut-command'){updateShortcutField();return;}
    if(id==='editor-cc-channel'){leaveSelectionView();cancelCCContinuation();selectionDomain='controls';selected.clear();ccChannel=Number(value);selectedCC=null;ccSelected.clear();refresh();}
    if(id==='editor-grid'||id==='editor-quantize'){setPrecision(value);return;}
    if(id==='editor-snap'){if(value!=='detected'||detectedBeats().available)snap=value;else notice=detectedBeats().reason;if(value==='detected'&&snap==='detected')notice='原始检测拍使用已有音频位置。开启吸附后生效。';refresh();}
    if(id==='editor-length'||id==='editor-q-length'){
      const v=noteValues.find(([n])=>Math.abs(n-Number(value))<1e-9);
      if(value==='link'||v){lengthQuantize=value==='link'?'link':String(v[0]);saveQuantize();for(const key of ['editor-length','editor-q-length']){const field=$(key);if(field)field.value=String(lengthQuantize);}updateToolLabels();requestRollDraw();}
      return;
    }
    if(id==='editor-q-preset'){setPrecision(value);return;}
    if(id==='editor-audition-rate'){auditionRate=Number(value);dispatch('auditionRate',{value:auditionRate});}
    if(id==='editor-operation'&&['meter_event','remove_meter_event'].includes(value))selectMeterEvent();
    if(id==='editor-operation'&&value==='rebar')loadRebarBoundaries();
    if(id==='editor-meter-event'){selectMeterEvent();explainOperation();requestRollDraw();}
    if(['editor-operation','editor-meter','editor-distribution','editor-quarters','editor-acoustic-snap'].includes(id))explainOperation();
    if(id==='editor-map-point')selectMapPoint(value);
    if(dialog==='editor-range' && id.startsWith('editor-'))invalidatePreview();
    if(['editor-reference-bpm','editor-distribution','editor-meter','editor-meter-numerator','editor-meter-denominator','editor-operation'].includes(id)&&$('editor-operation')?.value==='rebar'){if(referenceTimer)clearTimeout(referenceTimer);referenceTimer=setTimeout(()=>{referenceTimer=0;previewTempoReference();},180);}
    if(['editor-music-start','editor-music-end'].includes(id)){
      try{const seconds=window.AstraTransportPanel?.hasMap()===false?Number(value):window.AstraRhythmTimeline.parseMusicPosition(value),current=window.AstraTransportPanel?.range()||range(),start=id==='editor-music-start'?seconds:current.start,end=id==='editor-music-end'?seconds:current.end;
        if(!Number.isFinite(seconds)||seconds<0||seconds>duration())throw new Error('请输入曲目范围内的有效位置。');
        send('setEditorRange',{start,end,loop:Boolean(current.loop)});
      }catch(error){notice=error.message;const field=$(id);if(field)field.setAttribute?.('aria-invalid','true');const inline=$(`${id}-error`);if(inline){inline.textContent=notice;inline.hidden=false;}const label=$('editor-notice');if(label)label.textContent=notice;}
      return;
    }
    if(['editor-range-start','editor-range-end'].includes(id)){
      const current=window.AstraTransportPanel?.range()||range(),start=id==='editor-range-start'?Number(value):current.start,end=id==='editor-range-end'?Number(value):current.end;
      if(hasLocators({start,end}))send('setEditorRange',{start,end,loop:Boolean(current.loop)});
      else {notice='右定位点必须晚于左定位点，时间不能为负。';refresh();}
    }
  }
  function audioSource(){
    const source=state.player?.sources?.find(s=>s.key==='original'&&s.path);
    if(!source)return null;
    return {...source,peaks:source.peaks?.length?source.peaks:source.path===state.audio?.path?state.audio.peaks:null};
  }
  function timelineLanes(){
    const source=audioSource();
    return `<div class="editor-map-lane editor-opening"><span>开头</span><canvas id="opening-canvas" aria-label="第一完整小节：拖动标记或右键设置"></canvas></div><div class="editor-map-lane editor-beat-flags" ${showsBeatFlags()?'':'hidden'}><span title="拍号分母单位；拖动改变该拍对应音频位置，点击后可锁定">拍位</span><canvas id="beat-flags-canvas" aria-label="拍点：拍点笔单击空白新增；拖动已有拍点可校准"></canvas></div>${window.AstraBeatChecks?.markup()||''}<div class="editor-map-lane" id="mapping-lane" ${!active()||tool!=='map'?'hidden':''}><span title="当前生效图的映射约束，不是原始检测拍">校准点</span><canvas id="mapping-canvas" aria-label="拖动当前映射点；固定点不可移动；松手预览后确认"></canvas></div>${source?`<div class="editor-wave-lane" data-collapsed="${waveformCollapsed}"><button type="button" data-do="editorWaveform" aria-controls="waveform-canvas" aria-expanded="${!waveformCollapsed}" title="展开或折叠参考波形">原音频</button><div class="editor-wave-surface"><canvas id="waveform-canvas" aria-label="参考波形：点击或拖动定位播放头，不改变循环范围"></canvas><span id="waveform-cursor" class="playhead"></span></div></div>`:''}`;
  }
  function showsBeatFlags(){return workingView()||active();}
  function syncBeatFlagVisibility(){
    const lane=$('beat-flags-canvas')?.parentElement;
    if(lane){const hidden=!showsBeatFlags();if(lane.hidden!==hidden)lane.hidden=hidden;}
  }
  function meterSections(tempo){
    const meters=tempo?.time_signatures||[],pickup=Number(tempo?.pickup_beats)||0,firstFull=Number(tempo?.first_full_bar_beat)||0;
    if(!meters.length)return meters;
    const version=publication(),cached=meterSectionCache.get(tempo);
    if(cached&&cached.version===version&&cached.meters===meters&&cached.pickup===pickup&&cached.firstFull===firstFull&&cached.anchors===tempo.anchors&&cached.annotations===tempo.short_bar_annotations&&cached.opening===tempo.opening_mode&&cached.origin===tempo.grid_origin)return cached.sections;
    const first=meters[0],count=pickup*first.denominator/4;
    // The pickup display is synthetic; the first full bar uses the real meter
    // effective at its boundary (which may differ from the opening signature).
    const fullMeter=pickup?[...meters].reverse().find(m=>m.beat<=pickup+1e-7)||first:first;
    let sections=pickup?[{...first,beat:0,numerator:count,bar:0,pickup:true,numberOffset:first.numerator-count,flagPhase:pickup,flagNumerator:first.numerator},{...fullMeter,beat:pickup,bar:1,pickup:false},...meters.slice(1).filter(m=>m.beat>pickup+1e-7)]:meters.map(m=>({...m}));
    if(firstFull>0){
      const existing=sections.find(m=>Math.abs(m.beat-firstFull)<1e-7);
      if(existing)existing.bar=1;
      else {const source=[...sections].reverse().find(m=>m.beat<firstFull)||first;sections.push({...source,beat:firstFull,numerator:source.pickup?first.numerator:source.numerator,denominator:source.pickup?first.denominator:source.denominator,bar:1,pickup:false,intro:false,flagPhase:firstFull,flagNumerator:first.numerator});sections.sort((a,b)=>a.beat-b.beat);}
      sections=sections.map(m=>m.beat<firstFull?{...m,intro:tempo.opening_mode!=='normal',silence:tempo.opening_mode==='normal'}:m);
    }
    for(const mark of tempo.short_bar_annotations||[]){
      const map=tempo.anchors||[],toBeat=seconds=>{let i=0;while(i+1<map.length-1&&map[i+1].seconds<=seconds)i++;const a=map[i],b=map[i+1];return a&&b?a.beat+(seconds-a.seconds)*(b.beat-a.beat)/(b.seconds-a.seconds):NaN;};
      const start=toBeat(mark.start_seconds),end=toBeat(mark.end_seconds);if(!(end>start))continue;
      const source=[...sections].reverse().find(m=>m.beat<=start+1e-7)||first,unit=4/source.denominator;
      const restore={...source,beat:end,pickup:false,intro:false,silence:false,short:false};delete restore.bar;
      const short={...source,beat:start,numerator:(end-start)/unit,displayNumerator:source.displayNumerator||source.numerator,short:true,pickup:false};delete short.bar;
      const hasEnd=sections.some(m=>Math.abs(m.beat-end)<1e-7);
      sections=sections.filter(m=>m.beat<start-1e-7||m.beat>=end-1e-7);sections.push(short);if(!hasEnd)sections.push(restore);sections.sort((a,b)=>a.beat-b.beat);
    }
    sections=sections.map((m,i)=>{if(!m.free)return m;const end=sections[i+1]?.beat??tempo.anchors?.at(-1)?.beat;return end>m.beat?{...m,displayNumerator:m.displayNumerator||m.numerator,numerator:(end-m.beat)*m.denominator/4,flagNumerator:m.flagNumerator||m.numerator}:m;});
    const flagPhase=firstFull||Number(tempo?.grid_origin)||pickup||first.beat;
    if(sections[0]){sections[0].flagPhase=flagPhase;sections[0].flagNumerator=first.numerator;}
    meterSectionCache.set(tempo,{version,meters,pickup,firstFull,sections,anchors:tempo.anchors,annotations:tempo.short_bar_annotations,opening:tempo.opening_mode,origin:tempo.grid_origin});return sections;
  }
  function beatFlags(width,offset){
    // Effective musical grid only; no draft object substitutes the current map.
    const doc=editor();if(!doc)return [];
    const version=publication(),axis=typeof rollAxis==='function'?rollAxis():null;
    const key=width===midiWidth&&offset===0?'all':'view',cached=beatFlagCache?.[key];
    if(cached&&cached.revision===doc.revision&&cached.tempo===doc.tempo&&cached.version===version&&cached.axis===axis&&cached.width===width&&cached.offset===offset)return cached.rows;
    const remember=rows=>{(beatFlagCache??={})[key]={revision:doc.revision,tempo:doc.tempo,version,axis,width,offset,rows};return rows;};
    if(Array.isArray(doc.tempo.pulse_points)){
      const points=doc.tempo.pulse_points,stop=duration()-1e-7,rows=[];
      const start=midiSeconds(offset-21),end=midiSeconds(offset+width+21),project=axis?.x||midiX;
      for(let i=lowerBound(points,start,'seconds');i<points.length&&points[i].seconds<=end;i++){
        const p=points[i],x=project(p.seconds)-offset;if(x< -20||x>width+20)continue;
        const endpoint=p.seconds<1e-7||p.seconds>=stop;rows.push({...p,last:i===points.length-1,x,number:i+1,endpoint,locked:endpoint});
      }
      return remember(rows);
    }
    const rows=[],q0=secondsToBeat(midiSeconds(Math.max(0,offset-22))),q1=secondsToBeat(midiSeconds(offset+width+22));
    // Match pulse_state's detector-ordinal migration before a project has
    // explicit pulse IDs. A meter subdivision need not be a detected pulse.
    const projection=doc.tempo.pulse_projection,count=projection?.pulse_count,tail=doc.tempo.anchors.at(-1);
    const unit=projection?.effective_quarter_span_per_pulse===undefined?projection?.quarter_span_per_pulse:projection.effective_quarter_span_per_pulse;
    const rawOrigin=projection?.pulse_ordinal_quarter_origin,scale=projection?.post_projection_beat_scale;
    const origin=Number.isFinite(rawOrigin)?rawOrigin*(typeof scale==='number'?scale:1):NaN;
    if(Number.isInteger(count)&&count>0&&count<=1000000&&Number.isFinite(unit)&&unit>0&&Number.isFinite(origin)&&origin>=0){
      const first=Math.max(0,Math.floor((q0-origin)/unit)),last=Math.min(count-1,Math.ceil((q1-origin)/unit));
      for(let n=first;n<=last&&rows.length<4000;n++){
        const beat=origin+n*unit;if(beat>tail.beat+1e-8)break;
        const seconds=beatToSeconds(beat),x=midiX(seconds)-offset;if(x< -20||x>width+20)continue;
        const endpoint=seconds<1e-7||seconds>=tail.seconds-1e-7;
        rows.push({beat,seconds,x,number:n+1,step_quarters:unit,endpoint,locked:endpoint,last:n+1===count||origin+(n+1)*unit>tail.beat+1e-8});
      }
      return remember(rows);
    }
    const meters=meterSections(doc.tempo);
    for(let i=0;i<meters.length;i++){
      const m=meters[i],unit=4/m.denominator,end=Math.min(q1,meters[i+1]?.beat??doc.tempo.anchors.at(-1).beat);
      if(!(unit>0)||!(m.numerator>0))continue;
      const phase=m.flagPhase??m.beat,first=Math.ceil((Math.max(q0,m.beat)-phase)/unit-1e-7),last=Math.floor((end-phase)/unit+1e-7);
      for(let n=first;n<=last&&rows.length<4000;n++){
        const beat=phase+n*unit;if(i+1<meters.length&&beat>=meters[i+1].beat-1e-7)break;
        const seconds=beatToSeconds(beat),x=midiX(seconds)-offset,number=((n%(m.flagNumerator||m.numerator))+(m.flagNumerator||m.numerator))%(m.flagNumerator||m.numerator)+1;
        // Keep every logical beat for hit-testing and box selection.
        // drawTimeline handles visual density without deleting selectable data.
        const endpoint=seconds<1e-7||seconds>=doc.tempo.anchors.at(-1).seconds-1e-7;
        rows.push({beat,seconds,x,number,step_quarters:unit,endpoint,locked:endpoint});
      }
    }
    return remember(rows);
  }
  function flagAt(x,y){
    // Leave a top/bottom gutter for box selection, even at very dense zoom.
    if(Number.isFinite(y)&&(y<4||y>23))return null;
    let closest=null,distance=Infinity;
    const scroll=$('roll-scroll'),rows=beatFlags(scroll?.clientWidth||800,scroll?.scrollLeft||0);
    for(let index=lowerBound(rows,x-18,'x');index<rows.length&&rows[index].x<=x+5;index++){
      const flag=rows[index],spacing=Math.min(flag.x-(rows[index-1]?.x??-Infinity),(rows[index+1]?.x??Infinity)-flag.x);
      // Dense markers are tiny stems, not the wide labels used when zoomed in.
      const dense=spacing<28,radius=Math.min(5,Math.max(1,spacing*.28));
      if(dense?Math.abs(x-flag.x)>radius:x<flag.x-5||x>flag.x+18)continue;
      const gap=Math.abs(x-flag.x);
      if(gap<distance || (gap===distance&&flag.seconds===selectedBeatFlag?.seconds)){closest=flag;distance=gap;}
    }
    return closest;
  }
  function drawTimeline(width,offset){

    const opening=$('opening-canvas');if(opening)window.AstraRhythmTimeline?.drawOpening?.(canvasContext(opening,width,28),width,offset);
    const canvas=$('waveform-canvas'),source=audioSource(),r=range();
    const flags=$('beat-flags-canvas');if(flags&&showsBeatFlags()){
      const context=canvasContext(flags,width,26);context.fillStyle='#f4f7fa';context.fillRect(0,0,width,26);context.font='11px AstraSans';
      const flagsInView=beatFlags(width,offset),hidden=window.AstraRhythmTimeline?.eraseMask?.();
      const eraseTicks=active()&&(tool==='eraseBeat'||Boolean(hidden?.size)),overviewBins=new Map();
      for(let index=0;index<flagsInView.length;index++){
        const flag=flagsInView[index],x=Math.round(flag.x);
        if(hidden?.has(flag.seconds))continue;
        const chosen=active()&&Boolean(window.AstraRhythmTimeline?.selected('beat',flag.seconds));
        const nearby=(flagsInView[index+1]?.x??Infinity)-flag.x<28 || flag.x-(flagsInView[index-1]?.x??-Infinity)<28;
        const spacing=Math.min((flagsInView[index+1]?.x??Infinity)-flag.x,flag.x-(flagsInView[index-1]?.x??-Infinity));
        // During erasure group only coincident raster pixels, not displaced
        // six-pixel overview ticks. Hit/sweep still uses the unrounded centres.
        if(spacing<6){const bin=eraseTicks?x:Math.floor(flag.x/6),old=overviewBins.get(bin);overviewBins.set(bin,{x:eraseTicks?x:bin*6+1,chosen:chosen||old?.chosen});continue;}
        context.globalAlpha=1;
        // Pulse markers represent point positions, not the duration of the following interval.
        // Keep the regular beat ink for all pulse points; duration remains in the inspector.
        context.fillStyle=chosen?'#7d65ab':'#958b9f';
        const bar=musicalBar(flag.beat),downbeat=!bar.meter?.free&&Math.abs(flag.beat-bar.start)<1e-6;
        context.fillRect(x,8,chosen?2:1,14);if(downbeat){context.globalAlpha=1;context.fillStyle='#b97496';context.fillRect(x-2,3,5,2);context.fillStyle=chosen?'#7d65ab':'#958b9f';}
        if(!nearby){
          context.fillStyle=chosen?'#7560af':'#eee8f4';context.fillRect(x+1,5,16,13);context.fillStyle='#7d65ab';context.fillRect(x+1,5,16,2);
          context.fillStyle=chosen?'#ffffff':'#393645';
          context.fillText('♩',x+3,15);if(chosen){context.strokeStyle='#2b45a8';context.lineWidth=1.5;context.strokeRect(x-.75,3.25,19.5,20.5);context.lineWidth=1;}if(flag.last){context.fillStyle='#6b6578';context.fillText('…',x+18,16);}
        }else{
          context.fillRect(x,8,4,3);if(chosen){context.strokeStyle='#2b45a8';context.lineWidth=1.5;context.strokeRect(x-2.25,4.75,7.5,18.5);context.lineWidth=1;}
        }
      }
      context.globalAlpha=1;
      if(tool!=='eraseBeat')flags.title=overviewBins.size?'缩略显示：密集拍点已合并显示，放大可逐点编辑；框选仍选中全部拍点':'拍点：拖动移动，空白处框选';
      // Overview density ticks are not synthetic editable points. Hit testing and
      // box selection still use the complete, original beat list.
      for(const bin of overviewBins.values()){context.fillStyle=bin.chosen?'#7560af':'rgba(166,198,219,.55)';context.fillRect(bin.x,bin.chosen?7:12,eraseTicks?1:2,bin.chosen?15:8);}
      window.AstraRhythmTimeline?.drawSelection?.(context,width,offset);
      if(drag?.kind==='beat'){context.fillStyle='#7d65ab';context.fillRect(midiX(drag.to)-offset,0,2,26);}
    }
    const cursor=$('waveform-cursor');if(cursor)cursor.style.transform=`translateX(${midiX(typeof midiDisplayPosition==='function'?midiDisplayPosition():state.midi.position||0)-offset}px)`;
    if(canvas&&source){
      const height=canvas.parentElement.clientHeight, middle=height/2, amplitude=Math.max(1,middle-5);
      const ctx=canvasContext(canvas,width,height),peaks=source.peaks,length=Number(source.duration)||duration();
      ctx.fillStyle='#edf2f5';ctx.fillRect(0,0,width,height);
      if(Array.isArray(peaks)&&peaks.length){
        const axis=rollAxis(),motion=window.AstraRhythmMotion,motionStamp=motion?.progress()??1;
        if(!waveformCache||waveformCache.motionStamp!==motionStamp||waveformCache.peaks!==peaks||waveformCache.axis!==axis||waveformCache.offset!==offset||waveformCache.width!==width||waveformCache.height!==height||waveformCache.duration!==length){
          const visible=window.AstraRhythmMotion?.bounds(midiSeconds(offset),midiSeconds(offset+width))||{start:midiSeconds(offset),end:midiSeconds(offset+width)},start=Math.max(0,Math.floor(visible.start/length*peaks.length)-1),end=Math.min(peaks.length,Math.ceil(visible.end/length*peaks.length)+1),bars=[];
          const vx=motion?.active()?seconds=>motion.x(seconds,offset):axis.x;
          let left=vx(start/peaks.length*length)-offset;
          for(let i=start;i<end;i++){
            const right=vx((i+1)/peaks.length*length)-offset;
            const pair=Array.isArray(peaks[i])?peaks[i]:[-Number(peaks[i]),Number(peaks[i])];
            const low=Math.max(-1,Math.min(1,Number(pair[0])||0)),high=Math.max(-1,Math.min(1,Number(pair[1])||0));
            bars.push([left,middle-Math.max(low,high)*amplitude,Math.max(1,right-left),Math.max(1,Math.abs(high-low)*amplitude)]);left=right;
          }
          waveformCache={motionStamp,peaks,axis,offset,width,height,duration:length,bars};
        }
        ctx.fillStyle='#9aaac8';for(const bar of waveformCache.bars)ctx.fillRect(...bar);
      }else {ctx.fillStyle='#a6b6c5';ctx.font='11px AstraSans';ctx.fillText('波形缓存准备中…',10,middle+4);}
      if(r.end!==r.start&&(r.loop||localScope()||drag?.kind==='range'&&drag.moved)){ctx.fillStyle=r.loop?'rgba(125,186,230,.24)':'rgba(140,150,210,.16)';ctx.fillRect(midiX(r.start)-offset,0,midiX(r.end)-midiX(r.start),height);}
      if(['map','beat'].includes(drag?.kind)){ctx.fillStyle='#7d65ab';ctx.fillRect(midiX(drag.to)-offset,0,2,height);}
    }
    const mapping=$('mapping-canvas');if(mapping&&active()&&tool==='map'){
      const ctx=canvasContext(mapping,width,26),rows=editor().tempo.anchors,start=midiSeconds(offset-8),end=midiSeconds(offset+width+8);
      ctx.fillStyle='#f5f7f8';ctx.fillRect(0,0,width,26);
      for(let i=lowerBound(rows,start,'seconds');i<rows.length&&rows[i].seconds<=end;i++){
        const point=rows[i],x=midiX(point.seconds)-offset,locked=i===0||i===rows.length-1;
        ctx.fillStyle=locked?'#8f97a2':Math.abs((selectedMapPoint?.seconds??-1)-point.seconds)<1e-7?'#7d65ab':'#6a78c2';
        ctx.beginPath();ctx.moveTo(x,7);ctx.lineTo(x+5,13);ctx.lineTo(x,19);ctx.lineTo(x-5,13);ctx.closePath();ctx.fill();
        if(locked){ctx.fillStyle='#f5f7f8';ctx.fillRect(x-2,11,4,4);}
      }
      if(['map','beat'].includes(drag?.kind)){const x=midiX(drag.to)-offset;ctx.fillStyle='#7d65ab';ctx.fillRect(x-1,0,2,26);}
    }
  }
  function velocityGeometry(){const h=Number($('velocity-lane')?.clientHeight||65)-1;return {height:Math.max(40,h),top:6,bottom:Math.max(40,h)-3,span:Math.max(40,h)-9};}
  const velocityAtY=(y,g)=>Math.max(1,Math.min(127,Math.round((g.bottom-y)/g.span*127)));
  const velocityPreviewActive=()=>drag?.kind==='velocity'||drag?.kind==='velocityDraw';
  function velocityInk(note){
    const value=velocityPreviewActive()?drag.values.get(note.id):undefined;
    return value===undefined?note:{...note,velocity:value};
  }
  function velocityOnsetAt(rows,worldX,onlySelected){
    // Keep the press forgiving without also writing the neighbouring timing
    // slots. Same-onset chord notes are still edited as one stack.
    const radius=14;
    let best=null,distance=radius;
    for(let i=lowerBound(rows,midiSeconds(Math.max(0,worldX-radius)),'start');i<rows.length;i++){
      const n=rows[i].note,x=midiX(n.start);if(x>worldX+radius)break;
      if(onlySelected&&!selected.has(n.id))continue;
      const d=Math.abs(x-worldX);
      if(d<distance||d===distance&&best===null){best=x;distance=d;}
    }
    return best;
  }
  function velocityPaint(x0,y0,x1,y1){
    const d=drag,rows=d.index.entries,lo=Math.min(x0,x1)-.01,hi=Math.max(x0,x1)+.01;
    for(let i=lowerBound(rows,midiSeconds(Math.max(0,lo)),'start');i<rows.length;i++){
      const n=rows[i].note,x=midiX(n.start);if(x>hi)break;
      if(d.onlySelected&&!selected.has(n.id))continue;
      const t=x1===x0?1:Math.max(0,Math.min(1,(x-x0)/(x1-x0))),value=velocityAtY(y0+(y1-y0)*t,d.geometry);
      if(value!==n.velocity)d.values.set(n.id,value);else d.values.delete(n.id);
    }
  }
  function velocityFeedback(seconds,label){
    // Pointer samples update only the touched values. Paint and status each
    // happen at most once per frame, with a stable note index during the drag.
    const d=drag;d.velocityStatus={seconds,label};requestNoteFeedback();
  }
  function paintVelocityStatus(){
    const status=velocityPreviewActive()&&drag.velocityStatus;
    if(status){drag.velocityStatus=null;statusPosition(status.seconds,status.label);}
  }
  function drawVelocityLane(ctx,offset,width,height,gridLines=[],strip=null){
    const g=strip?{bottom:height-3,span:height-9}:velocityGeometry(),rows=workingView()?noteRenderIndex().entries:null;
    ctx.fillStyle='#edf2f5';ctx.fillRect(0,0,width,height);ctx.fillStyle='#ded8e5';for(const x of gridLines)ctx.fillRect(x,0,1,height);
    ctx.fillStyle='#d3d9e6';ctx.fillRect(0,Math.round(g.bottom),width,1);
    const chosen=[],plain=[];
    const notes=rows||state.midi.notes||[],left=Math.max(-4,strip?strip.left-4:-4),right=strip?Math.min(width+4,strip.right+4):width+4;
    for(let i=rows?lowerBound(rows,midiSeconds(Math.max(0,offset+left)),'start'):0;i<notes.length;i++){
      const n=velocityInk(rows?notes[i].note:notes[i]),x=Math.round(midiX(n.start)-offset);
      if(x>right&&rows)break;
      if(x<left||x>right)continue;
      if(selected.has(n.id)){chosen.push(n);continue;}
      plain.push({n,x,top:g.bottom-Math.max(1,n.velocity/127*g.span)});
    }
    // Stems: a 2px velocity-coloured core inside a 1px blue-grey edge (4px
    // total; 6px head), so pale low velocities stay legible on the lane.
    // Edges first, then cores, then heads: in chords and dense runs every
    // head stays visible over a neighbour's stem. Values and x are unchanged.
    ctx.fillStyle='rgba(52,64,104,.58)';
    for(const s of plain){ctx.fillRect(s.x-1,s.top-1,4,g.bottom-s.top+1);ctx.fillRect(s.x-2,s.top-1,6,4);}
    for(const s of plain){ctx.fillStyle=window.AstraVelocity?.color?.(s.n)||'#83a8bd';ctx.fillRect(s.x,s.top,2,g.bottom-s.top);}
    for(const s of plain){ctx.fillStyle=window.AstraVelocity?.color?.(s.n)||'#83a8bd';ctx.fillRect(s.x-1,s.top,4,2);}
    // Selected: white separation ring around a wider indigo stem and head.
    const picked=chosen.map(n=>({x:Math.round(midiX(n.start)-offset),top:g.bottom-Math.max(1,n.velocity/127*g.span)}));
    ctx.fillStyle='#ffffff';
    for(const s of picked){ctx.fillRect(s.x-2,s.top-1,6,g.bottom-s.top+1);ctx.fillRect(s.x-3,s.top-2,8,5);}
    ctx.fillStyle='#2b45a8';
    for(const s of picked){ctx.fillRect(s.x-1,s.top,3,g.bottom-s.top);ctx.fillRect(s.x-2,s.top-1,6,3);}
  }
  // Top of the tallest painted stem head at a note onset (lane CSS px), so
  // the velocity pencil guide can stop above it instead of covering it.
  function velocityHeadTop(worldX){
    const rows=noteRenderIndex().entries,g=drag?.kind==='velocityDraw'?drag.geometry:velocityGeometry();let top=Infinity;
    for(let i=lowerBound(rows,midiSeconds(Math.max(0,worldX-5)),'start');i<rows.length;i++){
      const n=velocityInk(rows[i].note),x=midiX(n.start);
      if(x>worldX+5)break;
      const chosen=selected.has(n.id),cap=chosen?5:4;
      if(Math.abs(x-worldX)<=cap)top=Math.min(top,g.bottom-Math.max(1,n.velocity/127*g.span)-(chosen?2:1));
    }
    return top;
  }
  function requestCCDraw(){if(typeof requestPedalDraw==='function')requestPedalDraw();else requestRollDraw();}
  function drawPedalOverlay(pedal,offset,width){
    if(!active())return;
    // One layout read per paint, not one DOM clientHeight read per point.
    const geometry=pedalGeometry(),paintY=value=>geometry.bottom-value/127*geometry.span;
    const a=midiSeconds(offset-4),b=midiSeconds(offset+width+4);
    const {start:startSeconds,end:endSeconds}=window.AstraRhythmMotion?.bounds(a,b)??{start:a,end:b};
    // Same selection language as notes: indigo core, white separation ring,
    // soft halo. Hover only adds a ring so it never reads as "selected".
    const pointGroups=new Map(),halos=[];
    const point=(cc,inkValue=cc.value)=>{
      const x=(window.AstraRhythmMotion?.x(cc.seconds,offset)??midiX(cc.seconds))-offset;
      if(x<0||x>width)return;
      const y=paintY(inkValue),selected=tool!=='rangeSelect'&&ccSelected.has(cc.id),hovered=cc.id===hoveredCC;
      // Hierarchy: envelope line < hollow handle (every point, on the line) < solid selected point.
      const conflict=drag?.ccConflicts?.size&&(cc.id?drag.ccConflicts.has(cc.id):false);
      const radius=selected?3.75:hovered?3.25:2.4;
      const fill=conflict?'#c54753':selected?'#2b45a8':'#ffffff';
      const stroke=selected?'#ffffff':hovered?'#2b45a8':conflict?null:'#5d6fb8',lineWidth=selected?1.5:hovered?1.25:1.1,key=`${radius}:${fill}:${stroke}`;
      if(selected)halos.push({x,y});
      if(!pointGroups.has(key))pointGroups.set(key,{radius,fill,stroke,lineWidth,points:[]});
      pointGroups.get(key).points.push({x,y});
    };
    const movingCC=new Set(drag?.kind==='control'?ccGhost.map(c=>c.id):[]),events=ccPaintEvents();for(let i=lowerBound(events,startSeconds,'seconds');i<events.length&&events[i].seconds<=endSeconds;i++)if(drag?.copy||(events[i].id!==ghost?.id&&!movingCC.has(events[i].id)))point(events[i]);
    if(drag?.kind==='control'&&ccGhost.length)(drag.ccPreviewRows||ccGhost).forEach(c=>{if(c.seconds>=startSeconds&&c.seconds<=endSeconds)point(c);});else if(ghost?.id)point(ghost);
    if(drag?.kind==='drawCC'){
      const ink=drag.ccPencilInk,now=performance.now(),last=drag.wire.samples.length-1;
      // The logical envelope and all already placed preview points stay at 0/127.
      // Animate only the live essential point's existing handle, at its final X.
      const tween=ink&&ink.from!==ink.target&&now<ink.at+ccPencilSettleMs&&noteMotionAllowed();
      let next=false;
      for(const cc of drag.ccPreviewRows||drag.essential||drag.points)if(cc.seconds>=startSeconds&&cc.seconds<=endSeconds){
        const live=tween&&cc.sample_index===last;
        point(cc,live?ccPencilDisplay(ink,now,geometry.span):cc.value);next ||= Boolean(live);
      }
      if(next)requestCCDraw();
    }
    // Keep every point and its exact geometry, but submit equal-style circles
    // together. Per-point fill/stroke submission dominated dense selections.
    if(halos.length){
      pedal.beginPath();for(const p of halos){pedal.moveTo(p.x+6.5,p.y);pedal.arc(p.x,p.y,6.5,0,Math.PI*2);}
      pedal.fillStyle='rgba(67,107,196,.20)';pedal.fill();
    }
    for(const group of [...pointGroups.values()].sort((a,b)=>a.radius-b.radius)){
      pedal.beginPath();for(const p of group.points){pedal.moveTo(p.x+group.radius,p.y);pedal.arc(p.x,p.y,group.radius,0,Math.PI*2);}
      pedal.fillStyle=group.fill;pedal.fill();
      if(group.stroke){pedal.lineWidth=group.lineWidth;pedal.strokeStyle=group.stroke;pedal.stroke();}
    }
    if(drag?.kind==='control'&&ccGhost.length){
      // Every selected point has its own resolved landing indicator. Keeping
      // only the grabbed point's ring hid the binary batch result of mixed
      // imported values, although their continuous ghosts stayed distinct.
      const previews=new Map(ccGhost.map(c=>[c.id,c])),targets=[],pixels=new Set();
      for(const [index,target] of ccCommitRows(drag,ccGhost).entries()){
        const preview=previews.get(target.id),tx=midiX(target.seconds)-offset;
        const conflictId=drag.copy||drag.creating?'__new_cc_'+index:target.id;
        if(!preview||tx<0||tx>width||drag.ccConflicts?.has(conflictId))continue;
        if(Math.abs(midiX(target.seconds)-midiX(preview.seconds))<=.5&&Math.abs(target.value-preview.value)<=.5)continue;
        const ty=paintY(target.value),pixel=`${Math.round(tx/4)}:${Math.round(ty/4)}`;
        // Dense events may share a few screen pixels. One hollow indicator
        // represents that visual cluster; event/commit precision is unchanged.
        if(pixels.has(pixel))continue;pixels.add(pixel);
        targets.push({tx,ty,x:midiX(preview.seconds)-offset,y:paintY(preview.value)});
      }
      if(targets.length){
        pedal.save();pedal.strokeStyle='#2b45a8';pedal.lineWidth=1;pedal.setLineDash?.([2,3]);pedal.beginPath();
        for(const t of targets){pedal.moveTo(t.x,t.y);pedal.lineTo(t.tx,t.ty);}pedal.stroke();pedal.setLineDash?.([]);pedal.beginPath();
        for(const t of targets){pedal.moveTo(t.tx+4,t.ty);pedal.arc(t.tx,t.ty,4,0,Math.PI*2);}pedal.stroke();pedal.restore();
      }
    }
    if(!drag&&ccInsertHover){const x=midiX(ccInsertHover.seconds)-offset,y=paintY(ccInsertHover.value);pedal.save();pedal.fillStyle='rgba(255,255,255,.9)';pedal.strokeStyle='#2b45a8';pedal.lineWidth=1.25;pedal.beginPath();pedal.arc(x,y,4.5,0,Math.PI*2);pedal.fill();pedal.stroke();pedal.beginPath();pedal.moveTo(x-2.5,y);pedal.lineTo(x+2.5,y);pedal.moveTo(x,y-2.5);pedal.lineTo(x,y+2.5);pedal.stroke();pedal.restore();}
    paintSelectionRange(pedal,'pedal-canvas',offset,geometry.height,width);
    if(drag?.kind==='ccBox'){
      const a=midiX(drag.from),b=midiX(drag.to),x=Math.min(a,b)-offset,w=Math.abs(b-a),y=drag.fullHeight?0:Math.min(drag.y,drag.y2),h=drag.fullHeight?geometry.height:Math.abs(drag.y2-drag.y);
      pedal.fillStyle='rgba(150,165,225,.15)';pedal.fillRect(x,y,w,h);pedal.strokeStyle='#6a78c2';pedal.strokeRect(x+.5,y+.5,w,h);
    }
  }
  function drawPedalEnvelope(pedal,offset,width){
    if(!workingView()&&!active())return false;
    if(active())ccPreviewCollision();
    let rows=ccPaintEvents();
    const g=pedalGeometry(),paintY=value=>g.bottom-value/127*g.span;
    const motion=window.AstraRhythmMotion,visual=motion?.active?.(),at=visual?seconds=>motion.x(seconds,offset)-offset:seconds=>midiX(seconds)-offset;
    const a=midiSeconds(offset),b=midiSeconds(offset+width),{start:left,end:right}=visual?motion.bounds(a,b):{start:a,end:b};
    if((drag?.kind==='control'&&ccGhost.length)||drag?.kind==='drawCC'){
      const previews=drag.ccPreviewRows||(drag.kind==='control'?ccGhost:drag.essential||drag.points),moving=new Set(drag.kind==='control'&&!drag.copy?ccGhost.map(c=>c.id):[]),visible=[];
      const included=c=>!moving.has(c.id)&&!drag.ccConflicts?.has(c.id);
      let i=lowerBound(rows,left,'seconds'),prior=i-1;
      while(prior>=0&&!included(rows[prior]))prior--;
      if(prior>=0)visible.push(rows[prior]);
      for(;i<rows.length&&rows[i].seconds<=right;i++)if(included(rows[i]))visible.push(rows[i]);
      let ghostPrior=null;
      for(const c of previews){if(c.seconds<left){if(!ghostPrior||c.seconds>=ghostPrior.seconds)ghostPrior=c;}else if(c.seconds<=right)visible.push(c);}
      if(ghostPrior)visible.push(ghostPrior);
      rows=visible.sort((a,b)=>a.seconds-b.seconds);
    }
    let i=lowerBound(rows,left,'seconds');
    if(visual)while(i<rows.length&&rows[i].seconds<=right&&at(rows[i].seconds)<0)i++;
    let value=i?rows[i-1].value:0,x=0;
    pedal.save();pedal.lineWidth=1.25;pedal.strokeStyle='#5d6fb8';pedal.fillStyle='rgba(92,108,190,.12)';
    pedal.beginPath();pedal.moveTo(0,paintY(value));
    for(;i<rows.length&&rows[i].seconds<=right;i++){
      const px=at(rows[i].seconds);if(visual&&px>width)break;
      const next=Math.max(0,Math.min(width,px)),y=paintY(value);
      if(next>x&&value>0)pedal.fillRect(x,y,next-x,g.bottom-y);
      pedal.lineTo(next,y);value=rows[i].value;pedal.lineTo(next,paintY(value));x=next;
    }
    if(width>x&&value>0)pedal.fillRect(x,paintY(value),width-x,g.bottom-paintY(value));
    pedal.lineTo(width,paintY(value));pedal.stroke();pedal.restore();
    // Live editor data, not the asynchronously regenerated MIDI preview, owns
    // the empty state. Both full and CC-only paints pass through here.
    if(!ccEvents().length&&!(drag?.kind==='drawCC'&&drag.points.length)){
      pedal.save();pedal.fillStyle='#6b6578';pedal.font='11px AstraSans';pedal.fillText('\u8fd8\u6ca1\u6709\u8e0f\u677f\u70b9',12,31);pedal.restore();
    }
    return true;
  }
  function drawBackdrop(ctx,offset,height,width){
    const r=range();
    if(r.end!==r.start&&(r.loop||localScope()||drag?.kind==='range'&&drag.moved)){
      ctx.save();ctx.fillStyle=r.loop?'rgba(99,141,173,.08)':'rgba(99,141,173,.06)';
      ctx.fillRect(midiX(r.start)-offset,0,midiX(r.end)-midiX(r.start),height);ctx.restore();
    }
    // Range tint is a grid layer, not a filter over velocity/note ink.
    if(tool==='rangeSelect')paintSelectionRange(ctx,'midi-canvas',offset,height,width,'wash');
  }
  function draw(ctx,ruler,pedal,offset,height,width){
    if(!state.midi.path&&!workingView())return;
    const startSeconds=midiSeconds(Math.max(0,offset-4)),endSeconds=midiSeconds(offset+width+4);
    // A note-only frame keeps the installed ruler/controller rasters.
    if(ruler&&pedal){
    const r=range(),x=midiX(r.start)-offset,end=midiX(r.end)-offset;
    ruler.fillStyle='#eaf0f4';ruler.fillRect(0,0,width,16);
    if(hasLocators(r)){
      const isScope=localScope()||drag?.kind==='range'&&drag.moved;
      if(r.loop||isScope){
        const shade=r.loop?'rgba(117,96,175,.10)':'rgba(117,96,175,.08)';
        // Note fills are never tinted by loop/scope overlays; background drawn before notes.
        pedal.fillStyle=shade;pedal.fillRect(x,0,end-x,pedalGeometry().height);
      }
      ruler.fillStyle=r.loop?'#b8a9d8':isScope?'#eae3f4':'#edf2f5';
      ruler.fillRect(x,1,end-x,14);
      ruler.save();ruler.font='600 11px AstraSans';
      for(const [at,label] of [[x,'L'],[end,'R']]){
        if(at<0||at>width)continue;
        const left=label==='L'?Math.max(0,Math.min(width-16,at)):Math.max(0,at-16);
        ruler.fillStyle=r.loop?'#7560af':'#7d65ab';
        ruler.fillRect(at-1,1,2,55);
        ruler.fillRect(left,1,16,14);
        ruler.fillStyle='#ffffff';ruler.fillText(label,left+5,12);
      }
      ruler.restore();
    }
    }
    if(ruler&&(active()||workingView())){
    const movingMeter=active()?window.AstraRhythmTimeline?.meterGhostSource?.():null;
    const meterLabels=window.AstraRhythmTimeline?.meterLayout(offset,width)||(editor().tempo.time_signatures||[]).map((m,index)=>({m,index,x:midiX(beatToSeconds(m.beat))-offset,label:true}));
    for(const {m,index:i,x:xx,label} of meterLabels){if(m===movingMeter||xx< -35||xx>width)continue;ruler.fillStyle=dialog==='editor-range'&&$('editor-operation')?.value==='meter_event'&&selectedMeterEvent?.index===i?'#7d65ab':'#e0d7eb';ruler.fillRect(xx,17,1,15);if(!label)continue;ruler.fillRect(xx+3,17,32,15);ruler.fillStyle='#191A1C';ruler.fillText(m.smart_measure?'智能':m.free?'自由':`${m.numerator}/${m.denominator}`,xx+6,28);}

    }
    if(!active())return;
    if(tempoPreview?.tempo && dialog==='editor-range'){
      const grid=previewGrid(tempoPreview.tempo);
      ctx.save();ctx.strokeStyle='#6a78c2';ctx.setLineDash([5,5]);
      for(let i=lowerBound(grid,startSeconds,'seconds');i<grid.length&&grid[i].seconds<=endSeconds;i++){const xx=midiX(grid[i].seconds)-offset;if(xx<0||xx>width)continue;ctx.beginPath();ctx.moveTo(xx,0);ctx.lineTo(xx,height);ctx.stroke();}
      ctx.restore();
    }
    if(dialog==='editor-range'&&selectedMeterEvent?.clicked_seconds!==undefined){const mark=selectedMeterEvent,a=midiX(mark.seconds)-offset,b=midiX(mark.end_seconds)-offset;ctx.fillStyle='rgba(150,160,220,.10)';ctx.fillRect(a,0,b-a,height);if(ruler){ruler.fillStyle='#6a78c2';ruler.fillRect(a,17,2,16);}}
    if(ruler&&dialog==='editor-range'&&tempoPreview?.tempo){for(const m of tempoPreview.tempo.time_signatures||[]){if((editor().tempo.time_signatures||[]).some(old=>Math.abs(old.beat-m.beat)<1e-7&&old.numerator===m.numerator&&old.denominator===m.denominator))continue;const rows=tempoPreview.tempo.anchors;let i=0;while(i+1<rows.length-1&&rows[i+1].beat<=m.beat)i++;const sec=rows[i].seconds+(m.beat-rows[i].beat)*(rows[i+1].seconds-rows[i].seconds)/(rows[i+1].beat-rows[i].beat),xx=midiX(sec)-offset;if(xx< -35||xx>width)continue;ruler.fillStyle='#6a78c2';ruler.fillRect(xx+3,17,32,15);ruler.fillStyle='#393645';ruler.fillText(m.smart_measure?'智能':m.free?'自由':`${m.numerator}/${m.denominator}`,xx+6,28);}}
    const scoreEnd=tempoPreview?.tempo&&dialog==='editor-range'?tempoPreview.tempo.score_end:editor().tempo.score_end;
    if(Number.isFinite(scoreEnd?.seconds)){const xx=midiX(scoreEnd.seconds)-offset;ctx.save();ctx.strokeStyle='#cfa3bb';ctx.setLineDash([6,3]);ctx.beginPath();ctx.moveTo(xx,0);ctx.lineTo(xx,height);ctx.stroke();ctx.restore();if(ruler){ruler.fillStyle='#d9b3c6';ruler.fillText('谱尾',Math.max(0,Math.min(width-32,xx-30)),12);ruler.fillRect(xx-2,0,3,44);}}
    if(tempoReference?.revision===editor().revision&&dialog==='editor-range'&&JSON.stringify(tempoReference.scope)===JSON.stringify(localScope())){
      const ref=tempoReference.reference_grid,step=Number(ref?.beat_unit_seconds),phase=Number(ref?.phase_seconds);
      if(step>0&&Number.isFinite(phase)){let first=Math.ceil((startSeconds-phase)/step),last=Math.floor((endSeconds-phase)/step);ctx.save();ctx.strokeStyle='#c88aa6';ctx.setLineDash([2,5]);for(let i=first;i<=last&&i-first<4096;i++){const xx=midiX(phase+i*step)-offset;ctx.beginPath();ctx.moveTo(xx,0);ctx.lineTo(xx,height);ctx.stroke();}ctx.restore();}
    }
    const mapPoint=currentMapPoint();
    if(mapPoint){
      const xx=midiX(mapPoint.seconds)-offset;
      ctx.save();ctx.strokeStyle='#a5577c';ctx.setLineDash([3,3]);ctx.beginPath();ctx.moveTo(xx,0);ctx.lineTo(xx,height);ctx.stroke();ctx.restore();
      if(ruler){ruler.fillStyle='#a5577c';ruler.fillRect(xx-1,16,2,46);}
      if(tempoPreview?.operation.action==='move_point'&&dialog==='editor-range'){const targetX=midiX(tempoPreview.operation.to_seconds)-offset;ctx.fillStyle='#3d55b0';ctx.fillRect(targetX,0,2,height);}
    }
    if(tempoPreview?.operation?.action==='move_beat'){ctx.fillStyle='#3d55b0';ctx.fillRect(midiX(tempoPreview.operation.to_seconds)-offset,0,2,height);}
    if(['map','beat'].includes(drag?.kind)){ctx.fillStyle='#7d65ab';ctx.fillRect(midiX(drag.to)-offset,0,2,height);}
    const resizing=activeNoteResize();
    const nudged=noteNudge?paintNoteNudge(ctx,offset,height,width,startSeconds,endSeconds):null;
    const pending=notePendingPreview;
    const earlyLanding=pending?.landing?.length&&ownNoteLandingPublication(pending);
    const pendingLanding=pending?.landing?.length&&(notePaintCurrent(pending)||earlyLanding)?pending.landing:null;
    if((selected.size||pendingLanding)&&tool!=='rangeSelect')paintNoteSelectionInk(ctx,ring=>{
      // Object IDs are owned here. The base raster may carry decoder rows
      // without those IDs; don't delegate working selection to that raster.
      const index=noteRenderIndex(),{high,row}=rollGeometry;
      const motion=window.AstraRhythmMotion,vx=motion?.active?.()?seconds=>motion.x(seconds,offset):midiX;
      const outline=n=>{
        if(nudged?.has(n.id))return;
        if(n.pitch>high||(high-n.pitch)*row>=height)return;
        const projected=resizing?.resizeInkIds.has(n.id)?resizing.noteMove:null;
        const first=vx(projected?movedStart(n,projected):n.start)-offset,last=vx(projected?movedEnd(n,projected):n.end)-offset;
        if(first>width||last<0)return;
        const x=Math.max(0,first),w=Math.max(2,Math.min(width,last)-x);
        ring(x,(high-n.pitch)*row+1,w,row-2,selectionNoteColor(n),true,true,first>=0?n.pitch:null);
      };
      if(pendingLanding){
        // A published receipt can supply exact IDs one frame before the selection callback.
        const receipt=state.editor_transaction;
        const ids=earlyLanding&&pending.copy&&receipt?.request===pending.request&&receipt.revision===editor()?.revision?receipt.added_ids:null;
        if(Array.isArray(ids)&&ids.length===pendingLanding.length&&ids.every(id=>index.byId.has(id))){
          for(const id of ids)outline(index.byId.get(id).note);
        }else{
          for(const n of pendingLanding)outline(earlyLanding&&!pending.copy?index.byId.get(n.id)?.note??n:n);
        }
      }else if(resizing){
        for(const id of resizing.resizeInkIds){const entry=index.byId.get(id);if(entry)outline(entry.note);}
        for(const id of selected){if(resizing.resizeInkIds.has(id))continue;const entry=index.byId.get(id);if(entry)outline(entry.note);}
      }else if(selected.size<32){
        for(const id of selected){const entry=index.byId.get(id);if(entry)outline(entry.note);}
      }else{
        const {start,end}=motion?.bounds(startSeconds,endSeconds)??{start:startSeconds,end:endSeconds},rows=index.entries;
        for(let i=lowerBound(rows,start,'maxEnd');i<rows.length&&rows[i].start<=end;i++)if(rows[i].end>=start&&selected.has(rows[i].note.id))outline(rows[i].note);
      }
    });
    // The ordinary held destination stays legible above selected source ink.
    if(drag?.kind==='note'&&drag.noteMove&&!drag.edge)drawNoteMove(ctx,drag,offset,height,startSeconds,endSeconds);
    drawQuantizePreview(ctx,offset,height,width);drawLengthPreview(ctx,offset,height,width);
    // Committed note pixels have now been drawn for this publication. Retire
    // pending boxes here, not in the receipt turn ahead of the raster.
    paintPendingNotes(true);
    const creating=drag?.kind==='drawNote'&&paintNoteCreation();
    if(drag?.kind!=='drawNote')paintNoteCreation();
    const ghostNotes=!creating&&Array.isArray(ghost)?ghost:[];
    // Canvas fallback for the live pencil note uses the same ghost outline.
    const drawingGhost=drag?.kind==='drawNote';
    ctx.save();ctx.fillStyle=drawingGhost?SELECTED_NOTE_FILL:'rgba(67,107,196,.30)';ctx.lineWidth=1.5;for(const n of ghostNotes){
      // Keep the complete ghost for the eventual edit; only rasterize what can
      // intersect this canvas. Thousands of offscreen fillRects still cost the
      // renderer time, even when Canvas clips their visible output to nothing.
      if(n.end<startSeconds||n.start>endSeconds)continue;
      const y=(rollGeometry.high-n.pitch)*rollGeometry.row+1;if(y+rollGeometry.row<0||y>height)continue;
      const x=midiX(n.start)-offset,w=Math.max(2,midiX(n.end)-offset-x);ctx.fillRect(x,y,w,rollGeometry.row-2);
      if(drawingGhost&&w>=2&&rollGeometry.row>4){
        ctx.strokeStyle=selectionNoteColor(n);ctx.strokeRect(x+.75,y+.75,w-1.5,rollGeometry.row-3.5);
        const text=x>=0&&selectionPitch(n.pitch,w,rollGeometry.row-2,ctx);
        if(text){
          ctx.fillStyle='#fff';ctx.textBaseline='middle';ctx.textAlign='left';
          drawSelectionPitch(ctx,text,x,y,rollGeometry.row-2);ctx.fillStyle=SELECTED_NOTE_FILL;
        }
      }
    }
    ctx.restore();
    drawNoteTarget(ctx,offset,width);
    if(drag?.kind==='note'&&drag.repeatEdge)drawNoteRepeat(ctx,drag,offset,height,width);
    paintSelectionRange(ctx,'midi-canvas',offset,height,width,'ink');
    if(drag?.kind==='box'&&drag.moved){const a=midiX(drag.from),b=midiX(drag.to),bx=Math.min(a,b)-offset,by=Math.min(drag.y,drag.y2),bw=Math.abs(b-a),bh=Math.abs(drag.y2-drag.y);ctx.fillStyle='rgba(67,107,196,.08)';ctx.fillRect(bx,by,bw,bh);ctx.strokeStyle='#4f6bc0';ctx.lineWidth=1;ctx.setLineDash?.([4,3]);ctx.strokeRect(bx+.5,by+.5,bw,bh);ctx.setLineDash?.([]);}
    if(pedal)drawPedalOverlay(pedal,offset,width);
    window.AstraRhythmTimeline?.draw(ctx,ruler,offset,height);
  }
  function previewGrid(tempo){
    if(previewGridCache?.tempo===tempo)return previewGridCache.points;
    if(tempoPreview?.operation?.action==='rhythm_candidate'&&Array.isArray(tempo.pulse_projection?.bars_seconds)){
      const points=tempo.pulse_projection.bars_seconds.filter(Number.isFinite).map(seconds=>({seconds}));
      previewGridCache={tempo,points};return points;
    }
    const map=tempo.anchors,meters=meterSections(tempo),points=[];let anchor=0;
    for(let i=0;i<meters.length;i++){
      const m=meters[i],step=m.numerator*4/m.denominator,end=meters[i+1]?.beat??map.at(-1).beat;
      if(!(step>0))continue;
      for(let beat=m.beat;beat<end;beat+=step){while(anchor<map.length-2&&map[anchor+1].beat<=beat)anchor++;const a=map[anchor],b=map[anchor+1];points.push({seconds:a.seconds+(beat-a.beat)*(b.seconds-a.seconds)/(b.beat-a.beat)});}
    }
    previewGridCache={tempo,points};return points;
  }
  const defaultMeter={beat:0,numerator:4,denominator:4},defaultMeters=[defaultMeter];
  function gridSections(){
    const tempo=editor()?.tempo,rows=tempo?meterSections(tempo):state.midi.time_signatures;
    return rows?.length?rows:defaultMeters;
  }
  function musicalBar(q,sections=gridSections(),i){
    if(i===undefined){let lo=0,hi=sections.length;while(lo+1<hi){const mid=(lo+hi)>>>1;if(sections[mid].beat<=q+1e-8)lo=mid;else hi=mid;}i=lo;}
    else while(i+1<sections.length&&sections[i+1].beat<=q+1e-8)i++;
    const m=sections[i]||defaultMeter,length=m.numerator*4/m.denominator;
    const start=m.beat+Math.max(0,Math.floor((q-m.beat+1e-8)/length))*length;
    return {start,end:Math.min(start+length,sections[i+1]?.beat??Infinity),meter:m,index:i};
  }
  function gridBeat(q,unit,sections){const bar=musicalBar(q,sections);return Math.max(0,Math.min(bar.end,bar.start+Math.round((q-bar.start)/unit)*unit));}
  function gridUnit(view){if(!state.midi.path)return null;return displayGridUnit(view);}
  function gridLines(line,view){
    if(!state.midi.path)return;
    const unit=view?.unit??gridUnit(view),offset=view?.offset??($('roll-scroll')?.scrollLeft||0),width=view?.width??($('roll-scroll')?.clientWidth||0);
    const axis=view?.axis,sections=gridSections(),project=axis?.beatX||(q=>midiX(beatToSeconds(q)));
    const invert=axis?.beat||(x=>secondsToBeat(midiSeconds(x)));
    const start=invert(offset),end=invert(offset+width);
    let bar=musicalBar(start,sections),guard=0,count=0;
    while(bar.start<end&&guard++<2000){
      const beatUnit=4/bar.meter.denominator,limit=Math.min(end,bar.end);
      // Free-meter spans have boundaries, not invented regular subdivisions.
      if(!bar.meter.free){
      for(let n=Math.max(1,Math.ceil((start-bar.start)/beatUnit-1e-8));bar.start+n*beatUnit<limit-1e-7;n++)line(project(bar.start+n*beatUnit),'',false,'beat');
      for(let n=Math.max(1,Math.ceil((start-bar.start)/unit-1e-8));bar.start+n*unit<limit-1e-7&&count++<12000;n++){
        const q=bar.start+n*unit;if(Math.abs((q-bar.start)/beatUnit-Math.round((q-bar.start)/beatUnit))<1e-7)continue;
        line(project(q),'',false,'subdivision');
      }
      }
      if(!Number.isFinite(bar.end)||bar.end<=bar.start)break;bar=musicalBar(bar.end,sections,bar.index);
    }
  }
  // Left/Right: previous/next note in (start, pitch, id) order (product rule for chords).
  // Shift extends from an anchor to a moving focus over the selection that existed when
  // the extension began: stepping back undoes it and keeps earlier discrete picks.
  let noteOrderCache=null;
  function noteOrder(){
    const doc=editor(),version=publication();
    if(noteOrderCache?.notes!==doc.notes||noteOrderCache.revision!==doc.revision||noteOrderCache.version!==version){
      const rows=[...doc.notes].sort((a,b)=>a.start-b.start||a.pitch-b.pitch||String(a.id).localeCompare(String(b.id)));
      noteOrderCache={notes:doc.notes,revision:doc.revision,version,rows,positions:new Map(rows.map((n,i)=>[n.id,i]))};
    }
    return noteOrderCache;
  }
  function navigateNotes(dir,extend){
    const sorted=noteOrder(),{rows:order,positions}=sorted;if(!order.length)return;
    const at=id=>positions.get(id)??-1,sig=()=>[...selected].sort().join(',');
    const pick=()=>{let result=-1;for(const id of selected){const i=at(id);if(i>=0&&(result<0||(dir>0?i>result:i<result)))result=i;}return result;};
    if(extend){
      if(!kbdSel||kbdSel.sig!==sig()||kbdSel.order!==sorted){
        let anchor=primaryNote&&selected.has(primaryNote)?at(primaryNote):-1;
        if(anchor<0)anchor=pick();
        if(anchor<0){navigateNotes(dir,false);return;}
        kbdSel={base:new Set(selected),anchor,focus:anchor};
      }
      kbdSel.focus=Math.max(0,Math.min(order.length-1,kbdSel.focus+dir));
      const a=Math.min(kbdSel.anchor,kbdSel.focus),b=Math.max(kbdSel.anchor,kbdSel.focus);
      selected=new Set(kbdSel.base);for(let i=a;i<=b;i++)selected.add(order[i].id);
      primaryNote=order[kbdSel.focus].id;kbdSel.sig=sig();kbdSel.order=sorted;
    }else{
      kbdSel=null;let from=primaryNote&&selected.has(primaryNote)?at(primaryNote):-1;
      if(from<0&&selected.size)from=pick();
      let next;if(from>=0)next=order[from+dir];
      else{const i=lowerBound(order,playheadSeconds()-1e-6,'start');next=order[dir>0?i:i-1];}
      if(!next)return;selected=new Set([next.id]);primaryNote=next.id;auditionVstNote(next);
    }
    selectionDomain='notes';selectedCC=null;ccSelected.clear();selectedBeatFlag=null;refresh();
  }
  function model(){
    if(!workingView())return state.midi;
    return {...state.midi,notes:noteRows(),pedals:null,pedal_events:ccEvents(),duration:duration(),time_signatures:editor().tempo.time_signatures};
  }
  function keydown(event){
    if(event.key==='Escape')cancelPencilGuide();
    if(event.key==='Escape'&&window.AstraRhythmTimeline?.cancelBeat?.()){event.preventDefault();event.stopImmediatePropagation();return;}
    if(event.key==='Escape'&&!event.isComposing&&!event.ctrlKey&&!event.metaKey&&!event.altKey&&!event.shiftKey&&!drag&&!tempoPreview&&!dialog&&
       !event.target.closest?.('input,textarea,select,dialog,[contenteditable=true]')&&!document.querySelector?.('.editor-command-menu[open]')&&dismissPendingRange()){
      meterClick=null;event.preventDefault();event.stopImmediatePropagation();return;
    }
    if(!active()&&state.midi.path&&tab==='notes'&&!event.isComposing&&!event.target.closest('input,textarea,select,dialog,[contenteditable=true]')){
      if(event.code==='KeyB'||event.key?.toLowerCase()==='b'){if(!event.repeat)setSnapBypass(true);event.preventDefault();return;}
      if(event.key==='Escape'&&drag){abortGesture();event.preventDefault();return;}
    }

    if(event.target.closest('[data-roll-lane-resize],#roll-time-scroll'))return;
    if(event.key==='Enter'&&event.target.closest('button,summary'))return;
    if(dialog==='editor-preferences'&&event.target.id==='editor-shortcut-binding'){const chord=window.AstraEditorShortcuts?.canonical(event);if(chord){event.target.value=chord;event.preventDefault();event.stopImmediatePropagation();}return;}
    if(tab==='notes'&&!event.isComposing&&!event.target.closest('input,textarea,select,dialog,[contenteditable=true]')){const viewCommand=window.AstraEditorShortcuts?.resolve(event);if(['editorPitchZoomIn','editorPitchZoomOut','editorCCZoomIn','editorCCZoomOut','editorCCLaneToggle'].includes(viewCommand)&&!drag){action(viewCommand);event.preventDefault();event.stopImmediatePropagation();return;}}
    if((active()||window.AstraTransportPanel?.readonlyCapable())&&tab==='notes'&&!event.isComposing&&!event.target.closest('input,textarea,select,dialog,[contenteditable=true]')){const transportCommand=window.AstraEditorShortcuts?.resolve(event);if(['editorTransportPlay','editorTransportStop','editorJumpLeft','editorJumpRight','editorFocusLeft','editorFocusRight','editorMetronome','editorLoop'].includes(transportCommand)){action(transportCommand);event.preventDefault();event.stopImmediatePropagation();return;}}
    if(tab==='notes'&&!event.isComposing&&!drag&&!event.target.closest('input,textarea,select,dialog,[contenteditable=true]')&&!window.AstraEditorShortcuts?.resolve(event)){
      const chord=window.AstraEditorShortcuts?.canonical?.(event)||'',zoomBy=k=>{if(typeof setMidiZoom==='function'&&typeof zoom==='number')setMidiZoom(k?zoom*k:1);};
      const view={'H':()=>zoomBy(1.5),'G':()=>zoomBy(1/1.5),'Shift+F':()=>zoomBy(0),'F':()=>document.querySelector('[data-do=follow]')?.click(),
        'Alt+I':()=>action('editorInspector'),'Ctrl+Alt+L':()=>action('editorInspector'),'Ctrl+I':()=>action('editorInfoLineToggle'),'Ctrl+Alt+T':()=>action('editorTransportFold')}[chord];
      if(view&&$('roll-scroll')){view();event.preventDefault();event.stopImmediatePropagation();return;}
    }
    if(tab==='notes'&&!event.isComposing&&!event.target.closest('input,textarea,select,dialog,[contenteditable=true]')&&(noteFlight||drag?.kind==='drawNote')){
      const ctrl=event.ctrlKey||event.metaKey,k=event.key.toLowerCase(),shortcuts=window.AstraEditorShortcuts;
      const history=shortcuts?shortcuts.resolve(event):ctrl&&k==='z'?(event.shiftKey?'editorRedo':'editorUndo'):ctrl&&k==='y'?'editorRedo':null;
      if(active()&&(history==='editorUndo'||history==='editorRedo')){
        if(!event.repeat)action(history);event.preventDefault();event.stopImmediatePropagation();return;
      }
      if(k==='escape'&&abortNoteGesture()){event.preventDefault();event.stopImmediatePropagation();return;}
      if((event.code==='KeyB'||k==='b')&&canDrawNote()){
        if(!event.repeat)setSnapBypass(true);event.preventDefault();event.stopImmediatePropagation();return;
      }
    }
    if(tab!=='notes'||event.isComposing||event.target.closest('input,textarea,select,dialog,[contenteditable=true]'))return;
    const ctrl=event.ctrlKey||event.metaKey,k=event.key.toLowerCase();
    const shortcuts=window.AstraEditorShortcuts,command=shortcuts?.resolve(event);
    // Beat selection is not a note/CC domain. Give only its Delete command
    // first refusal, including while a preceding erase is saving.
    if(selectionDomain==='none'&&(command==='editorDelete'||!command&&(!shortcuts&&!ctrl&&!event.altKey&&['delete','backspace'].includes(k)||shortcuts?.canonical?.(event)==='Backspace'))&&
       window.AstraRhythmTimeline?.action?.('editorDelete')){event.preventDefault();event.stopImmediatePropagation();return;}
    if(busy&&isHistoryCommand(command)&&historyOffered(command)){
      if(!event.repeat)action(command);event.preventDefault();event.stopImmediatePropagation();return;
    }
    if(busy&&ccPendingPreview){
      if(k==='escape'){cancelCCContinuation();event.preventDefault();event.stopImmediatePropagation();return;}
      if(drag?.continuation&&(event.code==='KeyB'||k==='b')){if(!event.repeat)setSnapBypass(true);event.preventDefault();return;}
    }
    if(!editable()){
      if(!drag&&!command&&noteNudge&&nudgeViewLive(noteNudge)&&positionArrow(event,k,ctrl)){event.preventDefault();event.stopImmediatePropagation();return;}
      if(!drag&&rangeKeyContinuation(command)){event.preventDefault();event.stopImmediatePropagation();}
      return;
    }
    if(tool!=='rangeSelect'&&(command==='editorSelectAll'||command==='editorInvertSelection'||(!shortcuts&&ctrl&&!event.shiftKey&&!event.altKey&&k==='a'))){
      selectionDomainFromTarget(event.target.id);
    }
    if(selectionDomain==='none'&&(command==='editorDelete'||(!shortcuts&&['delete','backspace'].includes(k)))){event.preventDefault();event.stopImmediatePropagation();return;}
    if(command&&!drag){action(command);event.preventDefault();event.stopImmediatePropagation();return;}
    if(!command&&!drag){
      const chord=shortcuts?.canonical?.(event)||'';
      const fixed={'2':()=>action('editorRangeTool'),'5':()=>action('editorBeatEraseTool'),
        'Backspace':()=>{if(selectionDomain!=='none')action('editorDelete');},
        'Ctrl+ArrowLeft':()=>nudge(-1,'move'),'Ctrl+ArrowRight':()=>nudge(1,'move'),'Alt+ArrowLeft':()=>nudge(-1,'start'),'Alt+Shift+ArrowRight':()=>nudge(1,'start'),'Alt+Shift+ArrowLeft':()=>nudge(-1,'end'),'Alt+ArrowRight':()=>nudge(1,'end'),
        'Ctrl+Numpad1':()=>setLocatorAtCursor('start'),'Ctrl+Numpad2':()=>setLocatorAtCursor('end')}[chord];
      if(fixed){fixed();event.preventDefault();event.stopImmediatePropagation();return;}
    }
    if(event.code==='KeyB'||k==='b'){if(!event.repeat)setSnapBypass(true);event.preventDefault();event.stopImmediatePropagation();return;}
    if(k==='escape'&&!drag){const found=document.querySelectorAll?.('.editor-command-menu[open]'),openMenus=found&&typeof found.length==='number'?Array.from(found):[];if(openMenus.length){openMenus.forEach(menu=>{menu.open=false;});openMenus[0].querySelector('summary')?.focus?.();event.preventDefault();event.stopImmediatePropagation();return;}}
    if(k==='escape'){meterClick=null;if(drag)abortGesture();else if(tempoPreview){invalidatePreview();}else if(tool==='rangeSelect'){action('editorSelectNone');}else{selected.clear();selectedCC=null;ccSelected.clear();selectedBeatFlag=null;}refresh();event.preventDefault();event.stopImmediatePropagation();return;}
    if(drag&&['arrowup','arrowdown','arrowleft','arrowright'].includes(k)){event.preventDefault();event.stopImmediatePropagation();return;}
    if(!ctrl&&!event.altKey&&['arrowup','arrowdown'].includes(k)&&selected.size){
      const semitones=(k==='arrowup'?1:-1)*(event.shiftKey?12:1);
      positionNudge({...semanticSelection('transpose'),semitones,policy:'clamp'});event.preventDefault();event.stopImmediatePropagation();return;
    }
    if(!ctrl&&!event.altKey&&['arrowleft','arrowright'].includes(k)){navigateNotes(k==='arrowright'?1:-1,event.shiftKey);event.preventDefault();event.stopImmediatePropagation();return;}
    if(!shortcuts&&!ctrl&&!event.altKey&&k==='p'&&selected.size){const notes=inspectNotes();send('setEditorRange',{start:Math.min(...notes.map(n=>n.start)),end:Math.max(...notes.map(n=>n.end)),loop:Boolean(range().loop)});event.preventDefault();return;}
    let name=null;if(!shortcuts&&ctrl&&k==='z')name=event.shiftKey?'editorRedo':'editorUndo';if(!shortcuts&&ctrl&&k==='y')name='editorRedo';if(!shortcuts&&ctrl&&k==='c')name='editorCopy';if(!shortcuts&&ctrl&&k==='v')name='editorPaste';if(!shortcuts&&(k==='delete'||k==='backspace'))name='editorDelete';
    if(!shortcuts&&ctrl&&!event.shiftKey&&!event.altKey&&k==='a'){action('editorSelectAll');event.preventDefault();event.stopImmediatePropagation();return;}
    if(name){action(name);event.preventDefault();event.stopImmediatePropagation();}
  }
  document.addEventListener('pointerdown',pointerDown,true);
  document.addEventListener('pointermove',pointerMove,true);
  document.addEventListener('pointerleave',event=>{if(event.target===pencilGuideTarget&&!drag)cancelPencilGuide();},true);
  document.addEventListener('scroll',()=>{if(pencilGuideTarget&&!drag)queueCursorProbe();},{capture:true,passive:true});
  window.addEventListener?.('resize',()=>{if(pencilGuideTarget&&!drag)queueCursorProbe();});

  document.addEventListener('pointerup',pointerUp,true);
  document.addEventListener('pointercancel',pointerUp,true);
  document.addEventListener('lostpointercapture',event=>{if(drag?.pointer===event.pointerId)abortGesture();},true);
  document.addEventListener('visibilitychange',()=>{if(document.hidden)abortGesture();});
  window.addEventListener?.('blur',abortGesture);
  window.addEventListener?.('pagehide',()=>{noteClosing=true;cancelNoteEntry('窗口已关闭',true);cancelCCContinuation();abortGesture();});
  window.addEventListener?.('pageshow',event=>{if(event.persisted)noteClosing=false;});
  window.addEventListener?.('resize',dockRangePanel);
  function positionCommandMenu(menu){
    if(!menu?.open)return;
    const body=menu.querySelector('.editor-command-body'),summary=menu.querySelector('summary');
    if(!body||!summary)return;
    const box=summary.getBoundingClientRect(),pad=10,gap=5;
    const height=window.innerHeight||document.documentElement.clientHeight;
    const width=window.innerWidth||document.documentElement.clientWidth;
    body.style.position='fixed';body.style.zIndex='600';
    body.style.maxHeight=Math.max(120,height-pad*2)+'px';
    const needed=Math.min(body.scrollHeight,height-pad*2);
    const below=height-box.bottom-gap-pad,above=box.top-gap-pad;
    const up=below<needed&&above>below;
    const available=Math.max(100,up?above:below);
    body.style.maxHeight=available+'px';
    body.style.top=(up?Math.max(pad,box.top-gap-Math.min(needed,available)):box.bottom+gap)+'px';
    body.style.left=Math.max(pad,Math.min(box.left,width-body.offsetWidth-pad))+'px';
  }
  // First placement + entrance happen in the `open` mutation microtask (before
  // the first frame); the later toggle task only re-places at the same spot.
  window.AstraPopup?.watch('.editor-command-menu','.editor-command-body',{name:'command-menu',position:positionCommandMenu});
  document.addEventListener('toggle',event=>{
    if(event.target?.matches?.('.editor-command-menu')){
      if(event.target.open&&event.target.dataset.disclosure==='editor-command-meter')refresh(false);
      positionCommandMenu(event.target);
    }
  },true);
  window.addEventListener?.('resize',()=>document.querySelectorAll?.('.editor-command-menu[open]').forEach(positionCommandMenu));
  document.addEventListener('click',event=>{
    const own=event.target?.closest?.('.editor-command-menu');
    document.querySelectorAll?.('.editor-command-menu[open]').forEach(menu=>{
      if(menu===own&&(!event.target?.closest?.('[data-do]')||menu.matches?.('.view-zoom-menu')&&event.target?.closest?.('[data-view-area]')))return;
      const refocus=menu===own&&menu.matches?.('.view-zoom-menu')&&menu.contains(document.activeElement);
      menu.open=false;
      if(refocus)menu.querySelector('summary')?.focus({preventScroll:true});
    });
  });
  document.addEventListener('keydown',event=>{if(event.key!=='Escape')return;const durationPicker=event.target?.closest?.('.editor-rhythm-command-body #editor-insert-duration');if(durationPicker&&durationPicker.closest?.('.editor-command-menu[open]')&&globalThis.CSS?.supports?.('selector(select:open)')&&durationPicker.matches?.(':open'))return;const menus=Array.from(document.querySelectorAll?.('.editor-command-menu[open]')||[]);if(!menus.length)return;const own=event.target?.closest?.('.editor-command-menu')||menus[0];menus.forEach(menu=>{menu.open=false;});own.querySelector('summary')?.focus?.();event.preventDefault();});
  document.addEventListener('dblclick',doubleClick,true);
  document.addEventListener('keydown',modifierCursor,true);
  document.addEventListener('keyup',modifierCursor,true);
  window.addEventListener?.('blur',resetModifierCursor);
  document.addEventListener('keydown',keydown,true);
  document.addEventListener('keyup',event=>{if((event.code==='KeyB'||event.key?.toLowerCase()==='b')&&temporarySnapBypass){setSnapBypass(false);event.preventDefault();event.stopImmediatePropagation();}},true);
  document.addEventListener('input',event=>{if(dialog==='editor-range'&&event.target.id.startsWith('editor-')&&event.target.id!=='editor-rhythm-choice'){invalidatePreview();if(event.target.id==='editor-reference-bpm')referenceTimer=setTimeout(()=>{referenceTimer=0;previewTempoReference();},180);}});
  document.addEventListener('close',event=>{if(event.target.id==='dialog'){stopScoreRange();invalidatePreview();dockRangePanel();}},true);
  return {panel,active,workingView,workingViewKey,editor,path,refreshData:refresh,patchMarkup:patchControlMarkup,drawPedalEnvelope,drawPedalOverlay,drawVelocityLane,velocityInk,velocityPreviewActive,paintVelocityStatus,ccLaneHidden:()=>ccLaneHidden,setReadOnly,canChangeView,rhythmClearNotes:()=>{selectionDomain="none";selected.clear();selectedCC=null;ccSelected.clear();selectedBeatFlag=null;},transportRate:()=>auditionRate,rhythmEditable:editable,rhythmTool:()=>tool,rhythmFlagAt:flagAt,rhythmFlags:()=>beatFlags(midiWidth,0),rhythmEraseFlags:beatFlags,
    rhythmNoteContinuation,cancelCCContinuation,abortSelectionGesture,leaveSelectionView,
    rhythmEraseNotice:text=>{notice=text;for(const id of ['editor-notice','editor-position']){const el=$(id);if(el&&el.textContent!==text)el.textContent=text;}},rhythmDetectedBeats:detectedBeats,rhythmSnap:snapped,rhythmCalibrationSnap:calibrationSnap,rhythmCalibrationOn:calibrationOn,rhythmPencilHover:updateCursor,rhythmCancelClick:pointer=>{cancelledClickPointer=pointer;},rhythmPrecision:precisionLabel,rhythmPosition:statusPosition,rhythmCommit:edit,rhythmSuppressClick:()=>{suppressClickUntil=performance.now()+350;},rhythmMessage:text=>{notice=text;refresh();},seekPosition:()=>drag?.kind==='seek'?drag.at:null,model,action,changed,draw,drawBackdrop,paintNoteSelectionInk,paintOrdinaryPitch,ordinaryPitchCover,drawTimeline,timelineLanes,syncBeatFlagVisibility,gridLines,inspectorMarkup,sync,dockRangePanel,initializeShortcuts,inspectorCollapsed:()=>inspectorCollapsed,inspectorCovered,infoLineHidden:()=>infoLineHidden,
    consumeClick:(event)=>{if(cancelledClickPointer!==null&&(event?.pointerId===undefined||event.pointerId===cancelledClickPointer)){cancelledClickPointer=null;return true;}return performance.now()<suppressClickUntil||Boolean(editLocked()&&event?.target?.id==='pedal-canvas')||Boolean(event?.altKey&&!editable()&&['midi-canvas','pedal-canvas'].includes(event.target?.id));},
    isDragging:()=>Boolean(drag),
    noteReceipt,noteEntryStatus,noteEntryMode,noteEntryBusy:()=>Boolean(noteFlight),paintNoteEntryStatus,recoverNoteEntry,suspendNoteEntry,cancelNoteEntry,abortNoteGesture,
    notePatchScene,noteAppendKeepsChrome,noteChromeMeter,noteResizeInkIds:()=>activeNoteResize()?.resizeInkIds,updateNoteCount,paintCommittedNotes:()=>{paintPendingNotes(true);paintNoteCreation();},
    syncVstFeedback,range,localScope,meterSections,gridUnit,axisChanged,infoLineMarkup,selected:note=>active()&&tool!=='rangeSelect'&&selected.has(note.id),hovered:note=>active()&&hoveredNote===note.id};
})();
