"use strict";

const $ = (id) => document.getElementById(id);
const html = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"})[c]);
const icon = (name) => `<svg aria-hidden="true"><use href="#i-${name}"/></svg>`;
const instruments = {piano:"钢琴", guitar:"吉他"};
const formats = {all:"MIDI + 乐谱", midi:"仅 MIDI", score:"仅乐谱"};
const grids = {auto:"自动判断",straight:"直拍",triplet:"三连音",fine:"细网格"};
let pendingHomeOptions = null;
const defaults = {instruments:["piano","guitar"],mode:"all",output:"",keep_stems:true,piano_model:"xlance",rhythm_provider:"cubase_local",quantization:"auto",tempo_path:"",bpm:null,meter:"4/4",beat_scale:1};
const state = {audio:null,input_midi:null,project:null,separation_workflow:null,recent:[],options:{...defaults},job:{},player:{},midi:{},pdf:{},gpu_preflight:null};
const titles = {tracks:"分轨",notes:"音符",score:"乐谱",files:"文件"};
const tabIcons = {tracks:"track",notes:"piano",score:"score",files:"file"};
const stages = ["准备音频","分析节拍","分离分轨","转录音符","导出结果"];
let bridge, nativeWebTransport, tab = "tracks", dialog = "", tempoTab = "anchors", tempoDraft;
let meterQuick = {}, tempoUndo = [], tempoEditSerial = 0, tempoBaseExport = null, meterPreviewPending = false;
let structureKey = "", trackSourceKey = "", trackPaintKey = "", volumeAdjust = false;
let auditionSource = "midi", auditionProject = "";
let transportMidi = false, transportAvailable = false, transportKey = "", seekDrag = null, seekPending = null;
let seekFrame = 0, lastSeekCommit = null, lastPlaybackSignal = 0;
let deferredSeekRender = false;
let receivedSourcesVersion = null, outputStatusNeedsRead = false;
let previewRefreshPending = false;
// The output settings module consumes the already ordered Studio state. It
// must not create a second playback subscriber or request its own full snapshot.
window.AstraPlaybackPlayer = () => state.player;
// Pointer scrubbing previews locally; one backend seek is sent on release.
const TRANSPORT_ACTIONS = new Set(["selectAuditionSource","audition","selectAudio","playPause","midiPlayPause","seek","midiSeek","volume","midiVolume","stop","midiStop"]);
const STATE_ACTIONS = new Set(["selectMidi","selectPdf","pdfPage","new","chooseAudio","chooseMidi","chooseProject","linkProjectAudio","openRecent","chooseOutput","chooseTempo","updateOptions","saveTempo","run","continueScore","continueSeparation","runMidi","reexport","retry","cancel","keepCancelledJob","cleanCancelledJob","openCancelledJob","openInterruptedJob","setChordVisibility","analyzeHarmony","editProject","undoProject","redoProject","setEditorRange","exportEditor","adoptTranscriptionCandidate","applyBarPhaseCandidate","auditionRate","metronome","chooseExecutionDevice","importReview","readReview","closeReview","estimateConstantTempo"]);
let zoom = 1, midiWidth = 0, midiVolume = 65, pitchZoom = 1;
// Timeline display (R16, user: "把按时间显示重新加回来", switch in the grid menu). Display only:
// "bars" spaces the roll by musical beats; "seconds" spaces it by real time (waveform, notes, CC,
// velocity and playhead stay evenly timed, bar/beat lines sit at their real seconds). Project data,
// snapping (always musical) and playback never depend on this.
let rulerMode = "bars";
let pitchBase = null, laneResize = null;
const rollLanes={velocity:{height:65,min:40,folded:false,label:'力度'},pedal:{height:57,min:53,folded:false,label:'CC64'}};
let pitchView = null, pendingPitchFit = false;
let rollFrame = 0, followMode = "page", followSuspended = false, suspendFollowOnEdit = true, lastMidiPosition = 0;
let pendingRollZoom = null, pendingRollScroll = null, manualRollNavigation = null, rollWheelRemainder = null;
let savedRollViewport = null, savedWorkingInspector = null;
let zoomInitializedPath = "", zoomManuallyChanged = false;
let activeInstrument = "piano", scoreZoom = null, rollGeometry = null;
let midiSelected = "", pdfSelected = "", pendingTempoSave = null, dragDepth = 0;
let pendingScoreAudition = null;
let logs = [], seenStages = new Set(), advancedOriginal = null, advancedProject = null;
let pollTimer = 0, pollInFlight = false, refreshUntil = 0;
let midiAnimation = 0, midiClock = {path:null,position:0,playing:false,at:0};
let audioAnimation = 0, audioCursor, audioClock = {source:null,position:0,playing:false,at:0};
// Playback clock (R13). Positions are anchored at the backend's sample time (sampled_at, Python
// monotonic), mapped to performance.now() by a measured offset: the lower envelope of
// receive - send (transport_order), allowed to drift up 1 ms/s. Anchoring at arrival time made the
// cursor depend on signal/poll delivery jitter. After a pause/stop request the clock stops
// extrapolating until the backend's acknowledgement brings the real stop point (never clamped).
let clockOffset=null,clockOffsetAt=0,pauseRequest=null;let lastMidiFrame=0;
function backendAnchor(sampledAt,sentAt){
  const now=performance.now();
  if(Number.isFinite(sentAt)){const o=now-sentAt*1000;clockOffset=clockOffset===null?o:Math.min(o,clockOffset+Math.max(0,now-clockOffsetAt)*.001);clockOffsetAt=now;}
  return clockOffset!==null&&Number.isFinite(sampledAt)&&sampledAt>0?Math.min(now,sampledAt*1000+clockOffset):now;
}
function clockElapsed(clock,now,kind){
  if(!clock.playing)return 0;
  let end=now;
  if(pauseRequest&&pauseRequest.kind===kind&&now-pauseRequest.at<1500)end=Math.max(clock.at,Math.min(now,pauseRequest.at));
  return Math.min(.5,Math.max(0,(end-clock.at)/1000));
}
// Opt-in timing trace (backend TRACE_PLAYBACK flag): receive / displayed / request events.
let playbackTrace=[],playbackTraceTimer=0,playbackTraceBusy=false,playbackTraceDropped=0;
function schedulePlaybackTrace(){
  if(playbackTraceBusy||playbackTraceTimer||!playbackTrace.length||typeof bridge?.savePlaybackTrace!=='function')return;
  playbackTraceTimer=setTimeout(()=>{
    playbackTraceTimer=0;
    if(typeof bridge?.savePlaybackTrace!=='function')return;
    const events=playbackTrace,dropped=playbackTraceDropped;
    playbackTrace=[];playbackTraceDropped=0;playbackTraceBusy=true;
    const done=()=>{playbackTraceBusy=false;schedulePlaybackTrace();};
    try{bridge.savePlaybackTrace(JSON.stringify({events,dropped}),done);}catch{done();}
  },3000);
}
function tracePlayback(ev,fields){
  if(!state.player?.trace)return;
  if(playbackTrace.length>=4000){++playbackTraceDropped;schedulePlaybackTrace();return;}
  const pnow=performance.now();
  playbackTrace.push({ev,pnow:Math.round(pnow*1000)/1000,t:clockOffset===null?null:Math.round((pnow-clockOffset)*1000)/1e6,...fields});
  schedulePlaybackTrace();
}
let rollData = null;
let rollInputVersion=0, axisCache=null, timeGridCache=null, lastRollLayers=null, lastKeyboard=null;
let rollDamage=0,rollRaster=null,rollInsert=null,rollNotePatches=[];
const ROLL_NOTES=1, ROLL_CC=2, ROLL_ALL=4, ROLL_BEAT_AIM=8, ROLL_NOTE_PATCH=16;
let reviewNoticeToken = null, reviewNoticeTimer = 0;
let midiInputToken = null, midiTracks = new Set(), midiChannels = null;
let ncmQueueKey = "";
let transcriptionCandidate = null;
const compactEditor = window.matchMedia("(max-height: 470px)");

let audioSelectionRequest=0,audioSelectionPending=null;
function audioSelectionScope(){return JSON.stringify([tab,state.project?.path,state.project?.source,state.audio?.path,state.input_midi?.path,(state.player.sources||[]).map(s=>[s.key,s.path])]);}
function clearAudioSelection(paint=false){
 if(audioSelectionPending)clearTimeout(audioSelectionPending.timer);
 audioSelectionPending=null;++audioSelectionRequest;trackPaintKey=null;
 if(paint)renderTransport();
}
function audioSelectionValid(request){
 const pending=audioSelectionPending;
 if(!pending||pending.request!==request)return false;
 if(pending.bridge!==bridge||pending.scope!==audioSelectionScope()){clearAudioSelection();return false;}
 return true;
}
function selectedAudioSource(){
 if(audioSelectionPending&&!audioSelectionValid(audioSelectionPending.request))return state.player.source;
 if(audioSelectionPending?.acknowledged && state.player.source===audioSelectionPending.key)clearAudioSelection();
 return audioSelectionPending?.key ?? state.player.source;
}
function paintAudioSelectionFeedback(){
 // Update the card, source selector, A/B affordance and cursor in the same turn.
 // The playback icon/clock still describe acknowledged playback, not this request.
 renderTransport();
}
function beginAudioSelection(key){
 clearAudioSelection();const request=audioSelectionRequest;
 const timer=setTimeout(()=>{if(audioSelectionPending?.request===request)clearAudioSelection(true);},1800);
 audioSelectionPending={key,request,timer,bridge,scope:audioSelectionScope()};paintAudioSelectionFeedback();return request;
}
function finishAudioSelection(request){if(audioSelectionValid(request))clearAudioSelection(true);}
// Completion, not dispatch acceptance, owns an editor transaction's pending UI.
const editorTransactions=new Map();
let editorRequestSerial=0, editorProjectEpoch=0, editorRevisionEpoch=0;
const editorRequestSession=globalThis.crypto?.randomUUID?.()??`${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
const nextEditorRequest=()=>`edit-${editorRequestSession}-${++editorRequestSerial}`;
let studioNavigationSerial=0,studioSnapshotViewEpoch=0;
function snapshotView(s=state){
  const p=s.project;return [p?.path,p?.source,p?.export,p?.editor?.edit_stamp?.generation,s.input_midi?.token,s.audio?.path,s.midi?.path];
}
const studioSnapshotReads=new Set();
function readStudioSnapshot(receive=acceptSnapshot,owner=bridge){
  const navigation=studioNavigationSerial,epoch=studioSnapshotViewEpoch,view=snapshotView();
  const read={owner,navigation};studioSnapshotReads.add(read);
  try{owner.snapshot(value=>{
    studioSnapshotReads.delete(read);
    if(owner!==bridge)return;
    receive(value,next=>navigation===studioNavigationSerial&&Boolean(next)&&(
      epoch===studioSnapshotViewEpoch&&sameRollInputs(view,snapshotView())||sameRollInputs(snapshotView(next),snapshotView())));
  });}catch(error){studioSnapshotReads.delete(read);throw error;}
}

function dispatchNoteInsertion(head){
  const {base,request,intent}=head,action=intent.action;
  if(base.owner!==bridge||!bridge?.editorTransactionChanged?.connect||window.AstraStructureEditor?.guardAction(action))throw Error('当前编辑视图不允许发送此操作。');
  const editor_scope={project:base.project,generation:base.generation,version:base.version,source_key:base.sourceKey};
  // No acceptance read, and no immediate receipt read: the editor joins the
  // terminal receipt to the installed publication. Poll is recovery only.
  bridge.dispatch(JSON.stringify({action,editor_request:request,base_revision:base.revision,editor_scope,
    ...(intent.note?{note_delta:1,operation:intent.operation}:{})}));
  schedulePoll();
}
function beatTransactionCurrent(p){
  return p.bridge===bridge&&p.beat.project===state.project?.path&&p.beat.projectEpoch===editorProjectEpoch;
}
function scopedTransactionView(p){
  const s=p.scope,d=state.project?.editor;
  return p.bridge===bridge&&p.navigation===studioNavigationSerial&&state.project?.path===s.project&&
    d?.edit_stamp?.generation===s.generation&&(d.source_key??null)===s.source_key;
}
function installEditorTransactionSnapshot(p,value,allows){
  if(p.scope&&!scopedTransactionView(p))return;
  if(!p.beat){acceptSnapshot(value,allows);return;}
  // Recovery reads for a beat placement cannot reinstall a departed project or
  // take the editor back across a revision already published by another packet.
  if(beatTransactionCurrent(p))acceptSnapshot(value,next=>(!allows||allows(next))&&next?.project?.path===p.beat.project&&
    (p.beat.revisionEpoch===editorRevisionEpoch||next.project.editor?.revision===state.project?.editor?.revision));
}
function installSelectionPublication(event,pending){
  const patch=event.editor_delta,p=state.project,old=p?.editor,base=old?.edit_stamp,s=patch?.edit_stamp,h=patch?.head;
  if(!patch||!pending.scope||!scopedTransactionView(pending)||event.status!=='committed'||event.warning||event.error||
     patch.base_revision!==old.revision||base.version!==pending.scope.version||s?.generation!==base.generation||
     !Number.isSafeInteger(s.version)||s.version<=Math.max(lastEditorVersion,base.version)||s.request!==event.request||
     h?.revision!==event.revision||(h.source_key??null)!==(old.source_key??null))return false;
  // Ordered row splices are a view protocol. Never send the displayed rows back as edit input.
  const doc={...old,...patch.set};
  for(const k of patch.unset||[])delete doc[k];
  for(const part of patch.rows?[patch.rows]:patch.row_groups||[]){
    if(!['notes','controls'].includes(part.group))return false;
    const rows=old[part.group].slice();
    for(const i of part.erase.slice().reverse())rows.splice(i,1);
    for(const [i,insert,row] of part.write){if(insert)rows.splice(i,0,row);else rows[i]=row;}
    doc[part.group]=rows;
  }
  if(doc.notes.length!==h.notes||doc.controls.length!==h.controls)return false;
  const tombs=patch.rows||patch;
  if(tombs.deleted_prefix!==undefined)doc.deleted_ids=old.deleted_ids.slice(0,tombs.deleted_prefix).concat(tombs.deleted_tail);
  Object.assign(doc,{revision:h.revision,edit_stamp:s,range:h.range,duration:h.duration,can_undo:h.can_undo,
    can_redo:h.can_redo,dirty:patch.dirty,score_stale:patch.score_stale,preview_pending:false,preview_revision:h.revision});
  if(h.selection_range)doc.selection_range=h.selection_range;else delete doc.selection_range;
  state.project={...p,editor:doc,tempo:doc.tempo};lastEditorVersion=s.version;lastSnapshotText=null;
  if(h.revision!==old.revision){++editorRevisionEpoch;editorNoteChrome=null;invalidateRollInputs();}
  AstraProjectEditor.sync();
  window.AstraOrnamentReview?.sync();
  if(tab==='notes')scheduleEditorRender();else if(h.revision!==old.revision)render();
  return true;
}
function finishEditorTransaction(event,snapshotInstalled=false){
  if(typeof event==='string'){try{event=JSON.parse(event);}catch{return;}}
  if(!event||!['committed','failed'].includes(event.status))return;
  if(event.note_publication){
    const {note_publication,...receipt}=event;
    installNotePublication(receipt,note_publication);
    event=receipt;
  }
  if(window.AstraProjectEditor?.noteReceipt?.(event))return;
  const pending=editorTransactions.get(event.request);if(!pending)return;
  if(pending.beat&&!beatTransactionCurrent(pending)){editorTransactions.delete(event.request);return;}
  if(pending.beat&&event.project&&event.project!==pending.beat.project)return;
  if(pending.scope&&event.selection_edit&&event.action===pending.action&&event.project===pending.scope.project){
    if(event.editor_delta){
      const {editor_delta,...receipt}=event;
      if(installSelectionPublication(event,pending)){
        state.editor_transaction=receipt;snapshotInstalled=true;
      }
      event=receipt;
    }
  }
  if(pending.scope){
    if(event.action!==pending.action||event.project!==pending.scope.project)return;
    const d=state.project?.editor,s=d?.edit_stamp;
    const same=scopedTransactionView(pending),own=same&&s.request===event.request&&s.version>pending.scope.version;
    // A semantic no-op still acknowledges its request and may keep the same
    // content revision. Join on the installed publication, not revision order.
    if(!same||!own&&s.version!==pending.scope.version||event.status==='failed'||event.warning||event.cancelled||own&&d.revision===event.revision){
      snapshotInstalled=true;
    }else if(snapshotInstalled){pending.resolving=false;return;}
  }
  if(!snapshotInstalled){
    if(pending.resolving)return;
    pending.resolving=true;
    readStudioSnapshot((value,allows)=>{
      try{const snapshot=JSON.parse(value);if(!snapshot||typeof snapshot!=='object')throw new Error('Invalid snapshot');}
      catch{pending.resolving=false;schedulePoll();return;}
      installEditorTransactionSnapshot(pending,value,allows);finishEditorTransaction(event,true);
    },pending.bridge);
    return;
  }
  editorTransactions.delete(event.request);
  if(!pending.scope&&event.status==='failed'&&event.error&&(!pending.beat||pending.beat.revisionEpoch===editorRevisionEpoch))state.job={...state.job,error:event.error};
  pending.after?.(event);
}
function dispatchEditorTransaction(action,payload,after){
  const request=payload.editor_request??nextEditorRequest();
  const owner=bridge,op=payload.operation;
  const beat=action==='editProject'&&op?.type==='rhythm_intent'&&['move_beat','move_beats','add_beat','move_meter'].includes(op.action)
    ?{project:state.project?.path,projectEpoch:editorProjectEpoch,revisionEpoch:editorRevisionEpoch}:null;
  const pending={bridge:owner,after,resolving:false,beat,scope:payload.editor_scope,action,navigation:studioNavigationSerial};
  editorTransactions.set(request,pending);
  state.editor_transaction={request,status:'pending',pending:true};
  refreshUntil=Math.max(refreshUntil,performance.now()+3000);schedulePoll();
  try{
    const wire=JSON.stringify({action,...payload,editor_request:request});
    if(pending.scope){owner.dispatch(wire);return;}
    owner.dispatch(wire,()=>{
    // Also recover a completion whose signal was coalesced in a background view.
    if(owner===bridge&&(!beat||beatTransactionCurrent(pending)))readStudioSnapshot((value,allows)=>installEditorTransactionSnapshot(pending,value,allows),owner);
  });}catch(error){const receipt={request,action,status:'failed',project:state.project?.path,error:String(error)};editorTransactions.delete(request);state.editor_transaction=receipt;state.job={...state.job,error:receipt.error};after?.(receipt);}
}
function submitNativeTrack(action,payload,fallback) {
  const player=state.player,route=player.cpp_route;
  if(tab!=='tracks'||transportMidi||player.audition!=='audio'||!(route>0)||!nativeWebTransport?.command)return false;
  const kind={playPause:1,seek:2,selectAudio:3,audition:4}[action];
  if(!kind)return false;
  const lane=kind>=3?player.cpp_sources?.indexOf(payload.key):-1;
  const seconds=kind===2||kind===4?Number(payload.position):0;
  if(kind>=3&&!(lane>=0)||!Number.isFinite(seconds)||seconds<0)return false;
  const owner=bridge,navigation=studioNavigationSerial,toggle=kind===1?pauseRequest:null;
  const locator=kind===2||kind===4?seekPending:null;
  const selection=kind>=3?beginAudioSelection(payload.key):null;
  if(locator)locator.cpp={route,sequence:0};
  nativeWebTransport.command(route,kind,lane,seconds,sequence=>{
    if(owner!==bridge||navigation!==studioNavigationSerial)return;
    if(!(sequence>0)){
      if(selection!==null)finishAudioSelection(selection);
      if(locator&&seekPending===locator)delete locator.cpp;
      // A zero response guarantees that no command was admitted. A newer
      // drag or page change has already superseded its rejected intent.
      if(tab==='tracks'&&(!locator||seekPending===locator))fallback();
      return;
    }
    if(toggle&&pauseRequest===toggle){
      toggle.cpp={route,sequence};
      confirmNativePlay(state.player);
    }
    if(locator&&seekPending===locator){
      locator.cpp.sequence=sequence;
      if(locator.wave)sendWaveSeekVisual(2,locator.position,sequence,route);
      updatePlaybackPosition('player',!audioAnimation);
    }
    if(selection!==null&&audioSelectionValid(selection)){
      audioSelectionPending.acknowledged=true;
      if(state.player.source===payload.key)finishAudioSelection(selection);
    }
  });
  return true;
}

function confirmNativePlay(player) {
  const receipt=pauseRequest?.cpp;
  if(receipt&&receipt.route===player.cpp_route&&player.cpp_applied>=receipt.sequence&&player.play_intent)pauseRequest=null;
}

function dispatch(action, payload = {}, after, allowCpp=true, waveCommit=false) {
  if (!bridge || window.AstraStructureEditor?.guardAction(action)) return;
  if(['new','openRecent','chooseAudio','chooseMidi','chooseProject','selectMidi'].includes(action))window.AstraRhythmContextMenu?.close();
  if(['new','openRecent','chooseAudio','chooseMidi','chooseProject'].includes(action))clearAudioSelection(true);
  if(tab==='notes' && !transportMidi && action==='midiSeek') action='seek';
  if ((action === 'midiSeek' || action === 'seek') && typeof armSeekPreview === 'function') armSeekPreview(action,payload.position,waveCommit);
  if(action==='seek'||action==='stop'||action==='audition')nativeTrackMotion=null;
  if(['playPause','midiPlayPause','stop','midiStop'].includes(action)){
    const kind=action.startsWith('midi')?'midi':'player',wasPlaying=Boolean(state[kind]?.playing);
    pauseRequest=wasPlaying||action.endsWith('top')?{kind,at:performance.now()}:null;
    tracePlayback('req',{action,kind,playing:wasPlaying,shown:typeof midiDisplayPosition==='function'?midiDisplayPosition():null});
  }else if(['seek','midiSeek','audition'].includes(action))tracePlayback('req',{action,to:payload.position});
  if(['selectAudio','audition'].includes(action)&&payload.key)auditionSource=payload.key;
  if(action==='midiPlayPause')auditionSource='midi';
  if(action==='audition')armAuditionPreview(payload.key,payload.position);
  const transport = TRANSPORT_ACTIONS.has(action), structural=STATE_ACTIONS.has(action);
  if (transport || structural || after) {
    refreshUntil = Math.max(refreshUntil,performance.now() + (transport ? 1200 : 3000));
    schedulePoll();
  }
  if(['new','chooseAudio','chooseMidi','chooseProject'].includes(action))window.AstraProjectOpening?.cancel();
  const opening=action==='openRecent'?window.AstraProjectOpening?.begin(payload.path):null;
  const changesEditorView=['new','openRecent','openCancelledJob','openInterruptedJob','chooseAudio','chooseMidi','chooseProject'].includes(action)||action==='selectMidi'&&payload.path!==state.midi.path;
  if(changesEditorView)++studioNavigationSerial;
  const navigation=studioNavigationSerial,owner=bridge;
  const resumeNoteEntry=changesEditorView?window.AstraProjectEditor?.suspendNoteEntry?.():null;
  if(action==='undoProject'||action==='redoProject')window.AstraProjectEditor?.cancelNoteEntry?.();
  if(bridge.editorTransactionChanged?.connect&&['editProject','undoProject','redoProject','setEditorRange'].includes(action)){dispatchEditorTransaction(action,payload,after);return;}
  const request = JSON.stringify({action, ...payload, ...(opening?{open_request:opening.id}:{})});
  // Remember the previous audition source for in-place A/B comparison.
  if (action==='selectAudio' && payload.key && state.player?.source && payload.key!==state.player.source) abPrevious=state.player.source;
  if(allowCpp&&!after&&submitNativeTrack(action,payload,()=>dispatch(action,payload,undefined,false,waveCommit)))return;
  const fastReceipt=typeof bridge.dispatchPlayback==='function';
  if (!after && ['selectAudio','audition'].includes(action) && payload.key && (fastReceipt||typeof bridge.poll==='function')) {
    const selection=beginAudioSelection(payload.key);
    const owner=bridge;
    const confirm=value=>{
      if(!audioSelectionValid(selection))return;
      try {
        const next=JSON.parse(value);audioSelectionPending.acknowledged=true;
        if(fastReceipt)applyPlaybackPacket(next);
        else if(next.player)applyPlaybackUpdate('player',next.player);
        if(audioSelectionValid(selection) && state.player.source===audioSelectionPending.key)finishAudioSelection(selection);
      } catch(error){console.warn('Track selection confirmation unavailable');finishAudioSelection(selection);}
    };
    try {
      if(fastReceipt)owner.dispatchPlayback(request,confirm);
      else owner.dispatch(request,()=>{
        if(!audioSelectionValid(selection))return;
        try{owner.poll(confirm);}catch{finishAudioSelection(selection);}
      });
    } catch(error){finishAudioSelection(selection);throw error;}
    return;
  }
  if(!after && !structural && transport && fastReceipt){
    const togglePause=action==='playPause'?pauseRequest:null;
    owner.dispatchPlayback(request,value=>{
      if(owner===bridge&&navigation===studioNavigationSerial)acceptPlaybackReceipt(value,togglePause);
    });
    return;
  }
  // A structural command is a request/acknowledgement boundary. WebChannel
  // notifications can be coalesced in background windows: never rely on a
  // signal alone to replace the MIDI identity or release a loading state.
  if (!after && !structural && ["playPause","midiPlayPause","stop","midiStop"].includes(action) && typeof bridge.poll === "function") {
    // Read actual state at command completion, rather than wait for a 150–220 ms
    // recovery poll. Do not optimistically flip playing or collapse rapid toggles.
    bridge.dispatch(request, () => bridge.poll(value => {
      let next; try { next=JSON.parse(value); } catch { return; }
      if(next.player)applyPlaybackUpdate("player",next.player);
      if(next.midi)applyPlaybackUpdate("midi",next.midi);
    }));
  }
  else if (!after && !structural) bridge.dispatch(request);
  else {
    const send=()=>{
      // A paint opportunity is not authorization to discard a newer draft.
      if(owner!==bridge||navigation!==studioNavigationSerial||opening&&(state.job?.running||window.AstraStructureEditor?.guardAction(action))){opening?.finish();resumeNoteEntry?.();return;}
      try {
        owner.dispatch(request, () => readStudioSnapshot((snapshot,allows) => {
          try { if(navigation===studioNavigationSerial){acceptSnapshot(snapshot,allows);after?.();} }
          finally {
            resumeNoteEntry?.();
            if(opening){try{const status=JSON.parse(snapshot).project_opening;if(navigation!==studioNavigationSerial||!status||status.request!==opening.id)opening.finish();else window.AstraProjectOpening?.observe(status);}catch{opening.finish();}}
          }
        },owner));
      } catch(error) {resumeNoteEntry?.();throw error;}
    };
    if(opening)opening.schedule(send);else send();
  }
}

function time(seconds) {
  const value = Math.max(0, Math.floor(Number(seconds) || 0));
  const hours = Math.floor(value / 3600), minutes = Math.floor(value / 60) % 60;
  const rest = `${hours ? String(minutes).padStart(2,"0") : minutes}:${String(value % 60).padStart(2,"0")}`;
  return hours ? `${hours}:${rest}` : rest;
}

function button(text, action, {style="secondary", symbol="", key="", disabled=false, path="", label=""} = {}) {
  if(state.rhythm_candidate_running&&['run','continueScore','runMidi','reexport','retry','analyzeHarmony','setChordVisibility','editorScore','editorExport','editorExportApply','editorScoreRange','updateForHarmony'].includes(action))disabled=true;
  symbol ||= ({editorUndo:'undo',editorRedo:'redo',editorScore:'score',editorExport:'external',editorPreferences:'keyboard',editorOpening:'start',editorRhythmRange:'range',editorDirectRecount:'bars',editorScoreRange:'score',editorScoreEnd:'range',editorClearScoreEnd:'close'})[action] || '';
  return `<button type="button" class="${style}" data-do="${action}"${key ? ` data-key="${html(key)}"` : ""}${path ? ` data-path="${html(path)}"` : ""}${disabled ? " disabled" : ""}${label ? ` aria-label="${html(label)}" title="${html(label)}"` : ""}>${symbol ? icon(symbol) : ""}${html(text)}</button>`;
}

function renderNcm(raw) {
  let state;
  try { state = JSON.parse(raw); } catch { return; }
  const {sources, results, running, cancelling, cancelled, error, output, can_open_output:canOpen} = state;
  const trigger = $("ncm-tool-button");
  trigger.classList.toggle("ncm-running", running);
  trigger.title = running ? "NCM 正在转换" : "NCM 音频转换";
  const panel = $("ncm-dialog");
  if (!panel.open) return;
  const control = (action) => panel.querySelector(`[data-do="${action}"]`);
  const queueKey = JSON.stringify([sources, results, running, cancelled]);
  if (queueKey !== ncmQueueKey) {
    ncmQueueKey = queueKey;
    const bySource = new Map(results.map((row) => [row.source,row]));
    const basename = (path) => path.split(/[\\/]/).at(-1);
    const focusedRemoval = document.activeElement?.dataset.do === "ncmRemove";
    $("ncm-queue").innerHTML = sources.map((source, index) => {
      const row = bySource.get(source);
      const status = row?.status || (running && index === results.length ? "running" : "");
      const label = ({converted:"已转换",reused:"已复用",failed:"失败",running:"正在转换"})[status] || (cancelled ? "已停止" : "等待转换");
      const detail = row?.error || (row?.path ? basename(row.path) : "");
      return `<li class="ncm-row"><span class="ncm-row-icon">${icon("file")}</span><div class="ncm-file"><strong class="truncate" title="${html(source)}">${html(basename(source))}</strong>${detail ? `<small>${html(detail)}</small>` : ""}</div><span class="ncm-row-status ${html(status)}">${label}</span>${running ? "" : button("","ncmRemove",{style:"icon-button",symbol:"close",path:source,label:`移除 ${basename(source)}`})}</li>`;
    }).join("");
    if (focusedRemoval) control("ncmChooseFiles").focus({preventScroll:true});
  }
  $("ncm-output-path").textContent = output;
  $("ncm-output-path").title = output;
  $("ncm-count").textContent = sources.length;
  $("ncm-empty").hidden = Boolean(sources.length);
  $("ncm-progress-area").hidden = !running && !results.length && !cancelled;
  $("ncm-progress").max = Math.max(1,sources.length);
  $("ncm-progress").value = results.length;
  $("ncm-background-note").hidden = !running;
  const totals = {converted:0,reused:0,failed:0};
  for (const row of results) totals[row.status]++;
  const counts = `${totals.converted} 个已转换 · ${totals.reused} 个已复用 · ${totals.failed} 个失败`;
  $("ncm-status").textContent = running ? `${results.length} / ${sources.length} · ${cancelling ? "当前文件完成后停止" : "正在转换"}` : cancelled ? `已停止 · ${counts} · ${sources.length-results.length} 个未转换` : counts;
  $("ncm-error").hidden = !error;
  $("ncm-error").textContent = error;
  control("ncmChooseFiles").disabled = running;
  control("ncmChooseOutput").disabled = running;
  control("ncmClear").disabled = running || !sources.length;
  control("ncmOpenOutput").disabled = !canOpen;
  control("ncmStart").disabled = running || !sources.length;
  control("ncmStart").hidden = running;
  control("ncmCancel").hidden = !running;
  control("ncmCancel").disabled = cancelling;
}

function openNcm() {
  if (!bridge?.ncmSnapshot) return;
  const panel = $("ncm-dialog");
  if (!panel.open) {
    ncmReturnFocus = rememberDialogOpener(panel);
    panel.showModal();
  }
  bridge.ncmSnapshot(renderNcm);
}

function recentCards(all = false) {
  const projects = all ? state.recent : state.recent.slice(0, 4);
  if (!projects.length) return `<p class="surface-note">成功导出的项目会显示在这里。</p>`;
  return `<div class="recent-grid">${projects.map((project) => `
    <div class="recent-entry">
      <button type="button" class="recent-card" data-do="openRecent" data-path="${html(project.path)}" title="${html(project.name)}" ${state.job.running ? "disabled" : ""}>
        <span class="recent-icon">${icon("score")}</span><span class="recent-copy"><strong class="truncate">${html(project.name)}</strong><small>打开已有导出</small></span>${icon("chevron")}
      </button>
      ${button("","openRecentFolder",{style:"icon-button",symbol:"folder",path:project.path,label:"打开「"+project.name+"」的项目文件夹："+project.path})}
    </div>`).join("")}</div>`;
}

function notation() {
  return `<svg aria-hidden="true" viewBox="0 0 240 150" preserveAspectRatio="xMidYMid slice">
    <path class="staff-lines" d="M0 50H240M0 61H240M0 72H240M0 83H240M0 94H240"/>
    <path class="barline" d="M43 50v44m75-44v44m75-44v44"/>
    <text x="47" y="42" class="measure">1</text><text x="123" y="42" class="measure">2</text><text x="198" y="42" class="measure">3</text>
    <g class="note-mark"><ellipse cx="68" cy="83" rx="5.5" ry="3.6" transform="rotate(-20 68 83)"/><path d="M73 82V58"/>
    <ellipse cx="94" cy="72" rx="5.5" ry="3.6" transform="rotate(-20 94 72)"/><path d="M99 71V47"/>
    <ellipse cx="143" cy="66" rx="5.5" ry="3.6" transform="rotate(-20 143 66)"/><path d="M148 65V41"/>
    <ellipse cx="171" cy="77" rx="5.5" ry="3.6" transform="rotate(-20 171 77)"/><path d="M176 76V52"/></g></svg>`;
}

function guitarOnly(selection) {
  return selection?.includes("guitar") && !selection.includes("piano");
}

// What the 转换与导出 dialog currently holds, and when it applies.
function settingsSummaryValues(audioOnly) {
  const o=state.options||{},dir=String(o.output||"").replace(/[\\/]+$/,"").split(/[\\/]/).pop()||"—";
  const scope=state.project?"当前项目接下来的处理":"新任务默认";
  const parts=audioOnly?[`输出到 ${dir}`,"WAV 分轨"]:[`输出到 ${dir}`,`节拍 ${o.rhythm_provider==="beat_this"?"原节拍分析":"本机拍点算法"}`,`分轨 ${o.piano_model==="sw"?"SW 六分轨":"Xlance"}`,`导出量化 ${grids[o.quantization]||o.quantization||"—"}`,`制谱 ${o.score_engine==="music21"?"music21":"DuET"}`,o.score_chords?"和弦记号开":"和弦记号关"];
  return {scope,summary:parts.join(" · ")};
}

function settingsSummary(audioOnly) {
  const {scope,summary}=settingsSummaryValues(audioOnly);
  return `<p class="config-summary" title="到「设置 → 当前项目」查看或修改；${scope}"><span>${html(summary)}</span><small>${scope}</small></p>`;
}

function separationAudition() {
  const phase=state.separation_workflow?.phase;
  return ["separated","scoring","partial","resuming"].includes(phase);
}

function inlineSeparationAction() {
  return Boolean(state.audio && (
    state.separation_workflow && !separationAudition() ||
    state.job.cancelled_run?.phase==="choice"
  ));
}

function currentHomeOptions() {
  const pending=pendingHomeOptions;
  if(pending&&(pending.navigation!==studioNavigationSerial||pending.audio!==state.audio?.path||
    pending.project!==state.project?.path||state.input_midi||isResult()||separationAudition()))pendingHomeOptions=null;
  return pendingHomeOptions;
}

function homeOptions() {
  const pending=currentHomeOptions();
  return pending ? {...state.options,...pending.options} : state.options;
}

function homeRunDisabled(selected) {
  return !state.audio || state.audio.import_loading || Boolean(state.audio.import_error) ||
    !selected.length || state.job.running || Boolean(state.gpu_preflight) || state.rhythm_candidate_running;
}

function configOutput(audioOnly,mode) {
  const choices = audioOnly ? `<span>吉他分轨 · WAV</span>` : `<span>钢琴输出</span><div class="segments" aria-label="钢琴导出格式">${Object.entries(formats).map(([key,title]) => `<button type="button" data-do="format" data-key="${key}" aria-pressed="${mode === key}" ${state.job.running ? "disabled" : ""}>${title}</button>`).join("")}</div>`;
  return `<span class="step-tag">3 · 产物</span>${choices}`;
}

function config() {
  const {instruments:selected=[],mode}=homeOptions();
  const audioOnly=guitarOnly(selected);
  return `<section class="config" aria-label="转谱设置">
    <div class="section-head"><h2><span class="step-tag">2 · 任务</span>选择乐器</h2><p>钢琴转谱 · 吉他分轨</p></div>
    <div class="tiles">${Object.entries(instruments).map(([key,name]) => `<button type="button" class="tile" data-do="instrument" data-key="${key}" aria-pressed="${selected.includes(key)}" ${state.job.running ? "disabled" : ""}>
      <span class="tile-icon ${key}">${icon(key)}</span><span class="tile-label">${name}<span>${key === "piano" ? "转谱 · MIDI / 乐谱" : "音频分轨 · WAV"}</span></span><span class="tile-check">${icon("check")}</span></button>`).join("")}</div>
    <div class="config-footer"><div class="formats" data-config-output data-wav="${Boolean(audioOnly)}">${configOutput(audioOnly,mode)}</div>
      ${button("项目设置","advanced",{style:"quiet",symbol:"settings",disabled:state.job.running})}<span class="action-space"></span>
      <div class="workflow-action">${inlineSeparationAction()?jobPanel():`<button type="button" class="primary run-button" data-do="run"${homeRunDisabled(selected)?" disabled":""}>${icon("arrow")}<span data-config-run-label>${homeRunLabel(audioOnly)}</span></button>`}</div>
    </div>${settingsSummary(audioOnly)}</section>`;
}


function homeRunLabel(audioOnly) {
  return audioOnly ? "开始分轨" : "开始转谱";
}

function syncHomeConfig() {
  const config=$("workspace").querySelector(".home .config");
  if(!config)return;
  const {instruments:selected=[],mode}=homeOptions();
  const audioOnly=Boolean(guitarOnly(selected));
  const output=config.querySelector("[data-config-output]");
  if(output.dataset.wav!==String(audioOnly)){
    output.dataset.wav=String(audioOnly);
    output.innerHTML=configOutput(audioOnly,mode);
  }
  for(const button of config.querySelectorAll('button[data-do="instrument"],button[data-do="format"]')){
    const pressed=String(button.dataset.do==="instrument"?selected.includes(button.dataset.key):mode===button.dataset.key);
    if(button.getAttribute("aria-pressed")!==pressed)button.setAttribute("aria-pressed",pressed);
  }
  const label=config.querySelector("[data-config-run-label]");
  if(label){
    const run=label.parentElement,saving=Boolean(currentHomeOptions()?.run);
    const text=saving?"正在保存选项…":homeRunLabel(audioOnly),disabled=Boolean(homeRunDisabled(selected)||saving);
    if(label.textContent!==text)label.textContent=text;
    if(run.disabled!==disabled)run.disabled=disabled;
  }
  const summary=config.querySelector(".config-summary span"),text=settingsSummaryValues(audioOnly).summary;
  if(summary.textContent!==text)summary.textContent=text;
}

function phaseIndex(phase) {
  if (/核对记谱拍值|核对音乐拍值|核对变拍前后|节奏与记谱模型/.test(phase)) return 1;
  if (/导出|写入|制谱|量化|分析和弦/.test(phase)) return 4;
  if (/转录|识别音符/.test(phase)) return 3;
  if (/分离|分轨/.test(phase)) return 2;
  if (/节拍|强拍/.test(phase)) return 1;
  if (/读取|准备|启动|解码|检查缓存/.test(phase)) return 0;
  return -1;
}

function jobStructure(job){
  // A running phase is already updated by renderJobProgress, in place.
  return job.running ? {...job,phase:null} : job;
}

function renderJobProgress() {
  const target = $("job-progress");
  const livePhase = $("job-live-phase");
  if (livePhase) {
    const phase = state.job.phase || "正在启动任务";
    if (livePhase.textContent !== phase) livePhase.textContent = phase;
    const job = livePhase.closest('.job');
    const cancel = job.querySelector('[data-do="cancel"]');
    const cancelling = /取消/.test(phase);
    if (cancel.disabled !== cancelling) cancel.disabled = cancelling;
    const current = phaseIndex(phase);
    for (const step of job.querySelectorAll('[data-stage]')) {
      const index = Number(step.dataset.stage), active = index === current;
      step.classList.toggle('current', active);
      step.classList.toggle('done', !active && seenStages.has(index));
      if (active && !step.hasAttribute('aria-current')) step.setAttribute('aria-current', 'step');
      else if (!active && step.hasAttribute('aria-current')) step.removeAttribute('aria-current');
    }
  }
  const progress = state.job_progress;
  if (!target || !progress) return;
  const {detail,elapsed,done,total,unit,remaining} = progress;
  const measured=Number.isFinite(done) && Number.isFinite(total) && total>0 && done>=0 && done<=total;
  const label = measured ? `${detail} · ${done} / ${total} ${unit} · ${Math.floor(done*100/total)}%` : detail;
  const timing = `已用 ${time(elapsed)}${remaining != null ? ` · 本阶段约剩 ${time(remaining)}` : ""}`;
  const title = $("job-progress-detail"), times = $("job-progress-time"), meter = $("job-progress-meter");
  if (title.textContent !== label) title.textContent = label;
  if (times.textContent !== timing) times.textContent = timing;
  meter.hidden = !measured;
  if (measured) {
    if (meter.max !== total) meter.max = total;
    if (meter.value !== done) meter.value = done;
  }
}

function cancelledJobOutcome() {
  const run = state.job.cancelled_run;
  if (state.job.running || !run?.id) return "";
  const count = Number.isSafeInteger(run.count) && run.count > 0 ? run.count : 0;
  const error = run.error ? `<p class="form-note" role="alert">${html(run.error)}</p>` : "";
  if (run.phase === "choice") {
    const detail = count ? `<p role="status">已完成 ${count} 项，可保留供之后打开。</p>` : "";
    return `<section class="job" aria-label="取消结果"><div class="job-top"><strong>任务已停止</strong><div class="job-actions">${count?button("保留已完成","keepCancelledJob",{style:"primary",key:run.id}):""}${button("清理本次产物","cleanCancelledJob",{style:"secondary",key:run.id})}</div></div>${detail}${error}</section>`;
  }
  if (run.phase === "kept") {
    const available = count && state.job.cancelled_history?.some(item => item.id === run.id);
    const detail = available ? `已保留 ${count} 项。可在「最近项目」下找到。` : "已结束。本次没有可打开的已完成产物。";
    return `<section class="job" aria-label="取消结果"><div class="job-top"><strong>任务已停止</strong>${available?button("查看已保留产物","openCancelledJob",{key:run.id}):""}</div><p role="status">${detail}</p>${error}</section>`;
  }
  if (run.phase === "cleaned") {
    return `<section class="job" aria-label="取消结果"><p role="status">已清理本次产物。</p>${error}</section>`;
  }
  return "";
}

function cancelledJobHistory(interrupted=false) {
  const rows = interrupted ? state.job.interrupted_history : state.job.cancelled_history;
  const action = interrupted ? "openInterruptedJob" : "openCancelledJob";
  const title = interrupted ? "可恢复的任务" : "已保留产物";
  if (!Array.isArray(rows) || !rows.length) return "";
  const cards = rows.filter(row => row?.id && Array.isArray(row.files) && row.files.length).map(row => {
    const entries=row.files.map(file=>file.label||file.kind).filter(Boolean);
    if(interrupted)entries.push(row.phase==="score"?"待续制谱":"待续分轨");
    const files=entries.join(" · ");
    return `<button type="button" class="recent-card" data-do="${action}" data-key="${html(row.id)}" ${state.job.running||state.job.recovery_pending?"disabled":""}><span class="recent-icon">${icon("folder")}</span><span class="recent-copy"><strong>${html(row.label || "本次产物")}</strong><small>${html(files)}</small></span></button>`;
  }).join("");
  return cards ? `<section class="recent-section" aria-label="${title}"><div class="section-head"><h2>${title}</h2></div><div class="recent-grid">${cards}</div></section>` : "";
}

function jobPanel() {
  const {running,phase,error,kind} = state.job;
  if (state.gpu_preflight?.status === "probing" && !state.separation_workflow) return `<section class="job" aria-label="计算设备检查"><div class="job-top"><div class="job-phase"><span class="spinner" aria-hidden="true"></span><strong>正在检查缓存与计算设备…</strong></div>${button("取消","chooseExecutionDevice",{key:"cancel"})}</div></section>`;
  const cancelled = cancelledJobOutcome();
  if (cancelled) return cancelled;
  if (!running && !error) {
    if (kind === "harmony_analysis" && phase === "和弦分析已更新") return '<p class="form-note" role="status">和弦分析与当前导出已更新。</p>';
    if (kind !== "chord_visibility") return "";
    if (phase === "和弦记号已更新") return `<p class="form-note" role="status">已${state.project?.score_chords ? "开启" : "关闭"}当前导出的和弦记号。</p>`;
    if (phase === "已取消处理") return `<p class="form-note" role="status">已取消更新。${state.project?.export?'上一次的导出仍可查看。':''}</p>`;
    return "";
  }
  if (running) {
    const current = phaseIndex(phase || "");
    const audioStages = state.job.kind === "run";
    const steps = audioStages ? `<div class="phase-steps">${stages.map((name,index) => state.job.separation_only && (index === 1 || index === 3) ? "" : `<span data-stage="${index}" class="phase-step${index === current ? " current" : seenStages.has(index) ? " done" : ""}" ${index === current ? 'aria-current="step"' : ""}>${name}</span>`).join("")}</div>` : "";
    return `<section class="job" aria-label="处理进度"><div class="job-top"><div class="job-phase"><span class="spinner" aria-hidden="true"></span><strong id="job-live-phase">${html(phase || "正在启动任务")}</strong>${state.project?.export?'<small class="job-keep">上一次的成果仍可查看，完成后替换</small>':''}</div><div class="job-actions">${button("日志","logs",{symbol:"terminal",disabled:!state.job.log_path || state.separation_workflow?.phase==="preparing"})}${button("取消","cancel",{disabled:/取消/.test(phase || "")})}</div></div>
      ${steps}<div id="job-progress" class="job-progress"><div class="job-progress-caption"><span id="job-progress-detail"></span><span id="job-progress-time"></span></div><progress id="job-progress-meter" max="1" value="0" aria-label="当前阶段窗口进度" hidden></progress></div></section>`;
  }
  return `<section class="job error-banner" aria-label="处理错误"><div class="job-top"><strong>处理遇到问题</strong><div class="job-actions">${button("日志","logs",{symbol:"terminal"})}${button("重试","retry",{symbol:"retry",disabled:!state.job.log_path})}</div></div><p>${html(error)}</p><p class="job-next">${state.project?.export?'上一次的项目成果仍在，未被这次失败覆盖。':'这次没有生成新成果。'}可查看日志定位原因，调整设置后重试。</p></section>`;
}

function home() {
  return `<section class="home"><header class="page-heading"><h1>新建转谱</h1></header>${inlineSeparationAction() ? "" : jobPanel()}
    <section class="drop" id="drop" aria-label="第 1 步 输入：音频或 MIDI 导入区"><div class="drop-main"><div class="drop-icon">${icon("upload")}</div><div class="drop-copy"><span class="step-tag">1 · 输入</span><h2 class="truncate" title="${html(state.audio?.name||"")}">${state.audio ? html(state.audio.name) : "拖放音频或 MIDI 到这里"}</h2><p>WAV · FLAC · MP3 · NCM · MID，全部在本机处理</p><div class="input-actions">${button(state.audio ? "更换音频" : "选择音频","chooseAudio",{style:"primary",disabled:state.job.running})}${button("选择 MIDI","chooseMidi",{symbol:"piano",disabled:state.job.running})}</div></div></div><div class="drop-notation">${notation()}<kbd>Ctrl + O</kbd></div></section>
    ${config()}<details class="home-flow-more" data-disclosure="home-flow"><summary>处理流程与缓存</summary>${homeFlow()}</details><section class="recent-section" id="recent-section"><div class="section-head"><h2>最近项目</h2><div class="recent-head-actions">${button("打开项目文件夹","openProjectsFolder",{style:"quiet",symbol:"folder",label:"打开默认的项目文件夹"})}${button("打开项目","chooseProject",{style:"quiet",disabled:state.job.running})}</div></div>${recentCards()}</section>${cancelledJobHistory()}${cancelledJobHistory(true)}</section>`;
}

// The real local pipeline, shown on the start page so new users see what happens after import.
function homeFlow() {
  const steps = [["upload","导入","音频、NCM，或直接导入 MIDI"],["track","分轨","钢琴 / 吉他分离，可逐轨试听"],["piano","转录","复调音符生成 MIDI"],["clock","节拍","变速与拍号映射，原演奏秒不变"],["score","制谱","MusicXML 与 PDF 乐谱"]];
  return `<div class="home-flow-wrap"><ol class="home-flow" aria-label="处理流程">${steps.map(([symbol,title,text],index) => `<li><span class="home-flow-icon">${icon(symbol)}</span><span class="home-flow-copy"><strong><em>${index+1}</em>${title}</strong><small>${text}</small></span></li>`).join("")}</ol><p class="home-flow-note">全部在本机运行；已完成且未改动的阶段会复用缓存。</p></div>`;
}

function selectedMidiChannels() {
  const selected = (state.input_midi?.tracks || []).filter(track => midiTracks.has(track.index));
  return [...new Set(selected.flatMap(track => track.channels).filter(channel => channel !== 9))].sort((a,b) => a-b);
}

function syncMidiInput(input) {
  if (input?.token !== midiInputToken) {
    midiInputToken = input?.token;
    midiTracks = new Set();
    midiChannels = null;
  }
  state.input_midi = input;
}

function midiInput() {
  const input = state.input_midi, busy = Boolean(state.job.running);
  const tracks = input.tracks || [];
  const channels = selectedMidiChannels();
  const facts = [input.ppq ? `PPQ ${input.ppq}` : "", input.duration > 0 ? time(input.duration) : "",
    input.default_tempo ? "无速度事件，采用 120 BPM" : "", input.default_meter ? "无拍号事件，采用 4/4" : ""].filter(Boolean);
  const details = (track) => {
    const channels = track.channel_details || [];
    const melodic = channels.filter(row => !row.is_drum);
    const piano = melodic.length && melodic.every(row => row.is_piano);
    return [piano ? "钢琴" : melodic.length ? "含非钢琴声道" : "", track.has_drums ? "含鼓声道" : "",
      channels.length ? `声道 ${channels.map(row => row.channel + 1).join("、")}` : ""].filter(Boolean).join(" · ");
  };
  const content = input.loading ? `<p class="surface-note" role="status">正在读取 MIDI 轨道…</p>` : input.error ? `<p class="surface-note" role="alert">${html(input.error)}</p>` : `
    <div class="section-head"><h2>选择要制谱的钢琴轨道</h2><p>可多选；鼓声道不会进入钢琴谱</p></div>
    <div class="midi-track-picker">${tracks.map((track) => {
      const playable = track.note_count > 0 && track.channels.some(channel => channel !== 9);
      return `<label class="midi-track-option"><input type="checkbox" data-midi-track="${track.index}" ${midiTracks.has(track.index) ? "checked" : ""} ${!playable || busy ? "disabled" : ""}><span class="midi-track-number">${track.index+1}</span><span class="midi-track-name"><strong>${html(track.name || `轨道 ${track.index+1}`)}</strong><small>${html(playable ? details(track) : track.note_count ? "仅鼓轨" : "没有音符")}</small></span><span class="midi-note-count">${Number(track.note_count).toLocaleString()} 音符</span></label>`;
    }).join("") || '<p class="surface-note">此文件没有可用轨道。</p>'}</div>
    ${channels.length > 1 ? `<fieldset class="midi-channel-filter"><legend>声道筛选（合并轨道时使用）</legend>${channels.map(channel => `<label><input type="checkbox" data-midi-channel="${channel}" ${(midiChannels === null || midiChannels.has(channel)) ? "checked" : ""} ${busy ? "disabled" : ""}>声道 ${channel+1}</label>`).join("")}</fieldset>` : ""}
    <p class="midi-input-hint">沿用文件的速度、拍号和开头空白。非钢琴声道仅在你明确选中时按钢琴方式制谱。</p>
    <div class="config-footer"><div class="formats"><span>输出</span><div class="segments" aria-label="导出格式">${Object.entries(formats).map(([key,title]) => `<button type="button" data-do="format" data-key="${key}" aria-pressed="${state.options.mode === key}" ${busy ? "disabled" : ""}>${title}</button>`).join("")}</div></div>${button("制谱设置","advanced",{style:"quiet",symbol:"settings",disabled:busy})}<span class="action-space"></span>${button("从 MIDI 制谱","runMidi",{style:"primary",symbol:"arrow",disabled:busy || !midiTracks.size || midiChannels?.size === 0})}</div>`;
  return `<section class="home midi-input"><header class="session-head"><div class="session-title"><h1>${html(input.name)}</h1><p>${html(facts.join(" · ") || "直接 MIDI 制谱")}</p></div>${button("更换 MIDI","chooseMidi",{symbol:"folder",disabled:busy})}</header>${jobPanel()}${content}</section>`;
}

let abPrevious=null, selectedTrackRow=null;
function sourceLabel(key){const source=(state.player?.sources||[]).find(item=>item.key===key);return source?.label||instruments[key]||(key==='original'?'原曲':String(key||''));}
function abPresentation(current){
  const previous=abPrevious&&abPrevious!==current&&sourceList().some(item=>item.key===abPrevious)?abPrevious:null;
  const label=previous?sourceLabel(previous):'';
  const body=current?`${current===state.player.source?'试听音源':'正在切换'}：<strong>${html(sourceLabel(current))}</strong>`:'';
  return {previous,label,body};
}
function abControl(){
  const {previous,label,body}=abPresentation(selectedAudioSource());
  return `<span class="ab-status" role="status">${body}</span><button type="button" class="secondary ab-toggle" data-do="selectAudio" data-key="${html(previous||'')}" title="在当前位置切回 ${html(label)}，用于 A/B 对比" ${previous?'':'hidden'}>A/B · ${html(label)}</button>`;
}
function updateTrackAB(current){
  const status=document.querySelector('.track-hint .ab-status');if(!status)return;
  const {previous,label,body}=abPresentation(current),control=status.nextElementSibling;
  if(status.innerHTML!==body)status.innerHTML=body;
  if(!control)return;
  if(control.hidden===Boolean(previous))control.hidden=!previous;
  if(previous){
    if(control.dataset.key!==previous)control.dataset.key=previous;
    const title=`在当前位置切回 ${label}，用于 A/B 对比`,text=`A/B · ${label}`;
    if(control.title!==title)control.title=title;
    if(control.textContent!==text)control.textContent=text;
  }
}
function sourceList() {
  if (state.audio && !separationAudition() && !isResult()) return [];
  return state.player.sources || [];
}

function projectSource() {
  return state.project?.source || (state.player.sources || []).find((source) => source.key === "original")?.path;
}

function isResult() {
  return Boolean(!state.input_midi && state.project && (!state.audio || state.audio.path === projectSource()));
}

function resultInstruments() {
  return Object.keys(instruments).filter(key => resultFiles().some(file => file.instrument === key) || sourceList().some(source => source.key === key));
}

function instrumentFile(kind,key) {
  const candidates = resultFiles(kind).filter(file => file.instrument === key);
  if (kind === "midi") {
    const current = midiRendition(state.midi.name || state.midi.path || "");
    return candidates.find(file => file.path === state.midi.path)
      || candidates.find(file => midiRendition(file.name) === current)
      || candidates.find(file => !midiRendition(file.name)) || candidates[0];
  }
  return candidates[0];
}

let editorSaveRequestError=null;
function activeSaveStatus(){
  const saved=state.editor_save,p=state.project;
  return saved?.ready&&p?.editor&&saved.project===p.path&&saved.generation===p.editor.edit_stamp?.generation?saved:null;
}
function installSaveStatus(next){
  if(!next||!Number.isSafeInteger(next.update)||next.update<=(state.editor_save?.update??-1))return;
  state.editor_save=next;
  paintSaveStatus();
}
function saveStatusMarkup(result){
  if(!result||!state.project?.editor)return '';
  return `<span id="project-save-status" class="status-token" role="status"></span><span id="project-backup-status" class="status-token" role="status" hidden></span><button type="button" id="project-save-retry" class="quiet" data-do="retryEditorSave" hidden>重试保存</button><button type="button" id="project-backup-retry" class="quiet" data-do="retryEditorBackup" hidden>重试副本</button><button type="button" id="project-retention-retry" class="quiet" data-do="retryEditorRetention" hidden>重试清理</button>`;
}
function paintSaveStatus(){
  const saved=activeSaveStatus(),formal=saved?.formal,recovery=saved?.recovery,p=state.project;
  const local=editorSaveRequestError?.project===p?.path&&editorSaveRequestError?.generation===p?.editor?.edit_stamp?.generation?editorSaveRequestError:null;
  const problem=lane=>lane.outcome==='present_unconfirmed'?'写入未确认':lane.outcome==='indeterminate'?'文件状态待确认':'写入失败';
  const busy=lane=>Boolean(lane?.saving||lane?.queued);
  const text=(id,value,failed=false)=>{
    const el=$(id);if(!el)return;
    if(el.textContent!==value)el.textContent=value;
    el.hidden=!value;el.classList.toggle('failed',failed);
  };
  text('project-save-status', local?.kind==='formal'?local.text:formal?.error?`项目${problem(formal)}${saved.save_dirty?' · 修改未确认保存':''}`:
    busy(formal)?'正在保存项目…':saved?saved.save_dirty?'项目有未保存的修改':'项目已保存':'',Boolean(local?.kind==='formal'||formal?.error));
  const saveEl=$('project-save-status'),saveState=local?.kind==='formal'||formal?.error?'failed':busy(formal)?'saving':saved?(saved.save_dirty?'dirty':'saved'):'';
  if(saveEl&&saveEl.dataset.state!==saveState)saveEl.dataset.state=saveState;
  // Failures/warnings stay in the title row next to their retry; routine
  // backup progress is only a detail. Same text and conditions as before.
  // An old copy left out of the Recycle Bin is housekeeping, not a problem
  // with this project: it is a detail, retried from 设置 › 保存与恢复.
  const backupFailed=Boolean(['recovery','retention'].includes(local?.kind)||recovery?.error);
  const backupText=['recovery','retention'].includes(local?.kind)?local.text:recovery?.error?`恢复副本${problem(recovery)}`:
    recovery?.warning?'部分旧副本尚未移入回收站（可在 设置 › 保存与恢复 中重试）':busy(recovery)?'正在保存恢复副本…':saved?.recovery_current?'已有当前恢复副本':'';
  text('project-backup-status',backupFailed?backupText:'',backupFailed);
  const backupDetail=$('project-backup-detail');
  if(backupDetail){const value=backupFailed?'':backupText;if(backupDetail.textContent!==value)backupDetail.textContent=value;const row=backupDetail.parentElement;if(row&&row.hidden!==!value)row.hidden=!value;}
  for(const id of ['project-retention-retry','preferences-retention-retry']){
    const el=$(id);if(el){el.hidden=!recovery?.warning||(id==='project-retention-retry'&&local?.kind!=='retention');el.disabled=Boolean(recovery?.retention_busy);}
  }
  const f=$('project-save-retry'),r=$('project-backup-retry');
  if(f)f.hidden=!formal?.failed_save||busy(formal);
  if(r)r.hidden=!saved?.recovery_enabled||!recovery?.failed_save||busy(recovery);
}
function requestProjectSave(kind='formal',retry=false){
  const owner=bridge,p=state.project,generation=p?.editor?.edit_stamp?.generation;
  if(!isResult()||!Number.isSafeInteger(generation)||typeof owner?.requestEditorSave!=='function')return;
  const project=p.path;
  editorSaveRequestError=null;paintSaveStatus();
  refreshUntil=Math.max(refreshUntil,performance.now()+3000);schedulePoll();
  owner.requestEditorSave(JSON.stringify({project,generation,kind,retry}),raw=>{
    if(owner!==bridge||project!==state.project?.path||generation!==state.project?.editor?.edit_stamp?.generation)return;
    let result;try{result=JSON.parse(raw);}catch{result=null;}
    if(result?.save_status)installSaveStatus(result.save_status);
    if(!result?.ok&&!result?.save_status)editorSaveRequestError={project,generation,kind,text:kind==='retention'?'这次没有开始清理。请查看日志。':'这次没有开始保存。请查看日志。'};
    paintSaveStatus();schedulePoll();
  });
}
function projectRecoveryMarkup(result){
  const offer=result&&state.project?.editor_recovery;
  if(!offer)return '';
  return offer.options?.length?button('有可恢复的副本…','viewEditorBackups',{style:'status-token pending'}):
    offer.error?`<span class="status-token failed" role="status">${html(offer.error)}</span>`:'';
}
function openEditorRecovery(){
  const offer=state.project?.editor_recovery;
  if(!offer?.options?.length)return;
  const label=row=>row.kind==='previous'?'前一次正式保存':`恢复副本 · ${new Date(row.created_ms).toLocaleString('zh-CN')}`;
  modal('恢复项目',`<p>${offer.formal_available?'发现比正式版本更新的恢复副本。':'正式文件缺失或无法打开。'}</p><label class="field">选择副本<select class="control" id="editor-backup-choice">${offer.options.map(row=>`<option value="${html(row.id)}">${html(label(row))}</option>`).join('')}</select></label><p class="form-note">只有你确认后才会替换正式文件。现有正式文件会另存；已导出的 MIDI 和乐谱不变。</p><p id="editor-backup-error" role="alert"></p>`,button('稍后','closeDialog')+button('替换正式版本','restoreEditorBackup',{style:'primary'}),'editor-recovery');
}
function confirmEditorRecovery(control){
  const owner=bridge,p=state.project,offer=p?.editor_recovery,id=$('editor-backup-choice')?.value;
  if(dialog!=='editor-recovery'||!offer?.options.some(row=>row.id===id)||typeof owner?.recoverEditorBackup!=='function')return;
  if(activeSaveStatus()?.save_dirty){$('editor-backup-error').textContent='当前编辑尚未保存，不能用副本替换。';return;}
  control.disabled=true;
  owner.recoverEditorBackup(JSON.stringify({project:p.path,token:offer.token,id}),raw=>{
    if(owner!==bridge||dialog!=='editor-recovery'||!control.isConnected||!$('dialog').open)return;
    let result;try{result=JSON.parse(raw);}catch{result=null;}
    if(result?.ok){$('dialog').close();readStudioSnapshot();}
    else {control.disabled=false;$('editor-backup-error').textContent=result?.error||'未能确认恢复结果，请重新打开项目检查。';}
  });
}

function importedAudioTargets() {
  return state.audio?.requested_instruments ?? state.options.instruments ?? [];
}

function importActions() {
  const workflow=state.separation_workflow;
  const busy=Boolean(state.job.running || state.gpu_preflight);
  const continueSeparation=workflow?.phase==="partial" && state.job.cancelled_run?.phase!=="choice"
    && button("继续分轨","continueSeparation",{style:"primary",symbol:"arrow",disabled:busy});
  const continueScore=workflow?.phase==="separated" && workflow.can_score && !workflow.pending_score
    && button("继续制谱","continueScore",{style:"primary",symbol:"arrow",disabled:busy});
  return `${button("项目设置","advanced",{style:"quiet",symbol:"settings",disabled:busy})}${button("更换音频","chooseAudio",{symbol:"folder",disabled:busy})}${continueSeparation||continueScore||""}`;
}

function projectViewReady(key) {
  if (key==="notes") return midiFiles().length>0;
  if (key==="score") return Boolean(resultFiles("pdf").length||resultFiles("musicxml").length||state.project?.editor);
  return true;
}

function session() {
  const result = isResult();
  const fromMidi = result && state.project.input_kind === "midi";
  const audioOnly = result && guitarOnly(state.project.instruments);
  const guitarFolder = audioOnly ? instrumentFile("audio","guitar")?.path.replace(/[\\/][^\\/]+$/,"") : "";
  const title = result ? state.project.name : state.audio?.name;
  const tabs = Object.entries(titles).filter(([key]) => key==="tracks" ? !fromMidi : result && projectViewReady(key));
  if (fromMidi && tab === "tracks") tab = resultFiles("pdf").length ? "score" : "notes";
  if (!result && tab !== "tracks") tab = "tracks";
  const ext = state.audio?.name?.match(/\.([^.]+)$/)?.[1]?.toUpperCase();
  const duration = state.audio?.duration || state.player.duration;
  const meta = [duration > 0 ? time(duration) : "", fromMidi ? "源 MIDI · 直接制谱" : ext].filter(Boolean).join(" · ");
  return `<section class="session ${result ? "workbench" : "import-session"} ${tab === "notes" ? "note-session" : ""}" data-view="${tab}"><header class="session-head"><div class="session-title"><h1 title="${html(title || "项目结果")}">${html(title || "项目结果")}</h1><div class="project-facts">${projectFactsMarkup(result,meta)}</div></div>
      <div class="view-navigation"><div class="tabs" role="tablist" aria-label="项目视图">${tabs.map(([key,name]) => `<button type="button" role="tab" id="tab-${key}" aria-controls="session-body" aria-selected="${tab === key}" tabindex="${tab === key ? 0 : -1}" data-do="tab" data-key="${key}">${icon(tabIcons[key])}${name}</button>`).join("")}</div>${rhythmProviderNotice()}${tab !== "tracks" && resultInstruments().length > 1 ? `<div class="view-instruments" aria-label="声部">${resultInstruments().map(key=>button(instruments[key],"inspectInstrument",{key,style:key===activeInstrument?"secondary":"quiet"})).join("")}</div>` : ""}</div><div class="session-actions">${result ? `<details class="project-menu"><summary title="保存、分析或导出当前项目">${icon("file")}<span>项目</span></summary><div class="project-menu-body" role="group" aria-label="项目操作">${state.project.editor?`<span class="project-menu-heading" aria-hidden="true">文件</span><button type="button" class="quiet" data-do="saveEditor" aria-label="保存项目 · Ctrl+S" title="保存项目 · Ctrl+S">${icon("file")}<span>保存项目</span><kbd>Ctrl+S</kbd></button><span class="project-menu-heading" aria-hidden="true">工程</span>`:''}${state.project.tempo&&!midiFiles().some(file=>file.editor) ? button("速度图与拍号…","tempoAdvanced",{style:"quiet compact-tempo",symbol:"clock",disabled:state.job.running||state.gpu_preflight}) : ""}${fromMidi ? button("重新导入 MIDI","chooseMidi",{style:"quiet",symbol:"retry",disabled:state.job.running}) : button(audioOnly ? "重新分轨" : "重新分析音频","run",{style:"quiet",symbol:"retry",disabled:state.job.running})}</div></details>${button(guitarFolder ? "分轨文件夹" : "输出文件夹","openFolder",{symbol:"folder",path:guitarFolder})}${audioOnly ? "" : button("导出当前结果","reexport",{style:"primary",symbol:"external",disabled:state.job.running})}${button("日志","logs",{style:"quiet session-log",symbol:"terminal",label:"查看运行日志"})}` : importActions()}</div></header>
    ${jobPanel()}<div class="workbench-grid"><div class="editor-pane">
    <div class="session-body" id="session-body" role="tabpanel" aria-labelledby="tab-${tab}">${candidateNotice()}${tab === "tracks" ? tracks() : tab === "notes" ? midi() : tab === "score" ? score() : files()}</div></div></div></section>`;
}

function trackWavePath(peaks) {
  if (!Array.isArray(peaks) || !peaks.length) return "";
  const lines = peaks.map((peak,index) => {
    const x = 5 + index * 990 / Math.max(1, peaks.length - 1);
    const pair = Array.isArray(peak) ? peak : [-Number(peak),Number(peak)];
    const a = Math.min(1,Math.max(-1,Number(pair[0]) || 0)), b = Math.min(1,Math.max(-1,Number(pair[1]) || 0));
    return `M${x.toFixed(1)},${(32-a*28).toFixed(1)}V${(32-b*28).toFixed(1)}`;
  });
  return lines.join("");
}

function trackWave(source) {
  const path=trackWavePath(source.peaks || (source.path===state.audio?.path ? state.audio.peaks : null));
  return path ? '<svg class="wave" aria-hidden="true" viewBox="0 0 1000 64" preserveAspectRatio="none"><path d="'+path+'"/></svg>'
              : '<span data-wave-placeholder>波形暂未就绪 · 仍可定位试听</span>';
}

function trackDuration(sources) {
  return state.audio?.duration || state.player.duration || Math.max(0,...sources.map(source=>source.duration||0));
}

function syncTrackPreviews(remounted) {
  const sources=sourceList(),duration=trackDuration(sources);
  const next={sources:state.player.sources, peaks:state.audio?.peaks, duration};
  const previous=syncTrackPreviews.last;
  syncTrackPreviews.last=next;
  if(remounted || previous && previous.sources===next.sources && previous.peaks===next.peaks && previous.duration===duration)return;
  const lanes=$("workspace").querySelectorAll('.track-stack .clip[data-do="clip"]');
  for(const clip of lanes){
    const source=sources.find(row=>row.key===clip.dataset.key);
    if(!source)continue;
    const marker=clip.querySelector('.playhead');
    const length=String(source.duration||duration);
    if(marker&&marker.dataset.duration!==length)marker.dataset.duration=length;
    const oldWave=clip.firstElementChild;
    const path=trackWavePath(source.peaks || (source.path===state.audio?.path ? state.audio.peaks : null));
    const oldPath=oldWave?.matches('svg.wave')?oldWave.firstElementChild:null;
    if(oldPath && path){
      if(oldPath.getAttribute('d')!==path)oldPath.setAttribute('d',path);
    }else if(path || oldPath){
      oldWave?.insertAdjacentHTML('beforebegin',trackWave(source));
      oldWave?.remove();
    }
  }
  const labels=$("workspace").querySelectorAll('.track-ruler .ruler-ticks > span');
  for(const [index,label] of labels.entries()){
    const title=duration?time(duration*index/4):index===0?'0:00':'';
    if(label.textContent!==title)label.textContent=title;
  }
}

function tracks() {
  if (state.audio?.import_loading) return `<p class="surface-note" role="status">正在本地转换 NCM。原文件会保留。</p>`;
  if (state.audio?.import_error) return `<p class="surface-note" role="alert">无法打开这份 NCM。请选择另一份本地音频。</p>`;
  const sources = sourceList();
  const duration = trackDuration(sources);
  const hasStems = sources.some(source => source.key !== "original");
  const link=state.project?.original_link;
  const reconnect=link ? `<p class="surface-note" role="status">${link.status==='pending' ? "正在核对原曲…" : html(link.error || "原曲未连接。")+button("连接原曲…","linkProjectAudio",{style:"quiet",symbol:"folder",disabled:state.job.running||Boolean(state.gpu_preflight)})}</p>` : "";
  if (!sources.length) return reconnect || `<p class="surface-note">项目没有可试听的本地音频。</p>`;
  return `${reconnect}<div class="view-toolbar"><div><strong>音频对照</strong>${hasStems?`<span>选择轨道比较原曲与分轨</span>`:""}</div><span class="toolbar-shortcut"><kbd>Space</kbd> 播放 / 暂停</span></div><div class="track-stack"><div class="track-ruler"><span>轨道</span><div class="ruler-ticks">${[0,.25,.5,.75,1].map((f) => `<span>${duration ? time(duration*f) : f === 0 ? "0:00" : ""}</span>`).join("")}</div></div>
    ${sources.map((source) => {
      const key = source.key, label = source.label || instruments[key] || "原曲";
      return `<div class="track-row ${instruments[key] ? key : "original"}" data-track="${html(key)}"><div class="track-controls"><div class="track-actions">${button("","trackPlay",{style:"track-play",symbol:"play",key,label:`播放${label}`})}<button type="button" class="track-name" data-do="selectAudio" data-key="${html(key)}" aria-pressed="${state.player.source === key}"><span class="truncate"><strong>${html(label)}</strong><small>${key === "original" ? "参考音频" : "分离结果"}</small></span></button></div>${instrumentFile("midi",key) || instrumentFile("pdf",key) ? `<div class="track-links">${instrumentFile("midi",key) ? `<button class="quiet" data-do="instrumentView" data-key="${key}" data-view="notes">音符</button>` : ""}${instrumentFile("pdf",key) ? `<button class="quiet" data-do="instrumentView" data-key="${key}" data-view="score">乐谱</button>` : ""}</div>` : ""}</div>
      <button type="button" class="clip" data-do="clip" data-key="${html(key)}" aria-label="从点击位置播放${html(label)}">${trackWave(source)}<span class="wave-progress" aria-hidden="true"></span><span class="playhead" data-duration="${source.duration || duration}" hidden></span></button></div>`;
    }).join("")}</div><div class="track-hint"><span>${hasStems ? "点击波形定位；点轨道名称原位切换试听。" : isResult() || state.separation_workflow?.phase==="separated" ? "没有可试听的分轨" : "点击波形定位。"}</span>${abControl()}</div>`;
}

function fileKind(file) {
  return String(file.kind || file.name?.split(".").pop() || "").toLowerCase();
}

function resultFiles(kind) {
  return (state.project?.files || []).filter((file) => !kind || fileKind(file) === kind);
}

function fileTitle(file) {
  const prefix = file.historical ? "历史 · " : "";
  if (file.name.endsWith("_harmony.json")) return "调性与和弦分析";
  if (file.name === "tempo_map.json") return "速度图 JSON";
  if (fileKind(file) === "source_midi") return "导入前的完整源 MIDI";
  if (file.editor) return "人工编辑结果";
  if (fileKind(file) === "midi") return prefix + (midiRendition(file.name) || (state.project?.input_kind === "midi" ? "原始 MIDI（所选轨道）" : "原始 MIDI（转录）"));
  return prefix + (({pdf:"PDF 乐谱",musicxml:"MusicXML 乐谱",audio:"分轨音频",report:"导出报告",tempo:"速度与拍号轨"})[fileKind(file)] || file.name);
}

function midiRendition(name) {
  return /transcription/i.test(name) ? "原始转录 MIDI" : /(?:^|[\\/])piano_raw(?:\.midi?)?$/i.test(name || "") && state.project?.report?.editor_revision ? "当前演奏 MIDI（未量化）" : isScoreMidi(name) ? "记谱校正版 MIDI" : /reviewed/i.test(name) ? "审核修订 MIDI" : /quant/i.test(name) ? "量化 MIDI" : "";
}

function isScoreMidi(name) {
  return /(?:^|[\\/])piano_score(?:\.midi?)?$/i.test(name || "");
}

function choices(files, current) {
  return files.map((file) => `<option value="${html(file.path)}" ${file.path === current ? "selected" : ""}>${html([instruments[file.instrument], fileTitle(file)].filter(Boolean).join(" · "))}</option>`).join("");
}

function reviewActions() {
  const count = state.project?.reviews?.length || 0;
  return `<div class="review-actions" aria-label="项目审核意见">${button("导入审核意见","importReview",{symbol:"file",disabled:state.job.running})}${count ? button(`查看已导入意见（${count} 份）`,"viewReview",{style:"quiet"}) : ""}</div>`;
}

function guitarAudioHint() {
  return `<div class="surface-note"><strong>吉他仅保留音频分轨</strong><p>吉他转录和制谱暂时暂停。可在分轨页对照原曲试听。</p>${button("返回分轨试听","tab",{key:"tracks",symbol:"track"})}</div>`;
}

function candidateNotice() {
  const project=state.project, editor=project?.editor;
  if (!editor?.manual || !editor.source_key || !project.piano_analysis_key || editor.source_key===project.piano_analysis_key) return "";
  const candidate=resultFiles("midi").find(file=>file.instrument==="piano" && file.name==="piano_transcription.mid");
  return `<div class="editor-score-stale" role="status"><span><strong>当前使用人工编辑稿</strong> · 新转录候选已缓存，未覆盖已有音符、踏板和拍位编辑。${candidate ? "查看候选仅切换只读视图，不会采用。" : "本次未导出候选 MIDI，可用 MIDI 输出模式重导出查看。"}</span>${candidate?button("查看新转录候选","viewTranscriptionCandidate",{path:candidate.path}):""}${button("采用新转录…","prepareTranscriptionCandidate",{disabled:Boolean(state.job.running || state.gpu_preflight)})}</div>`;
}

function rhythmProviderNotice() {
  const project=state.project;
  if(!project?.editor || project.input_kind==='midi')return "";
  const preserved=project.editor.manual||project.tempo_kind==='manual';
  const clock=project.editor.tempo||{},origin=clock.adopted_candidate?({'A':'原拍值候选','B0':'半拍值候选 · 起点 1','B1':'半拍值候选 · 起点 2','C':'半拍值独立重新分组','D':'双拍值拆分小节'}[clock.adopted_candidate.id]||'已确认候选'):(clock.source==='cubase_local_reconstruction'?'本机拍点分析':clock.source==='beat_this'?'原节拍分析':'编辑或导入的拍位图');
  const current='拍点分析';
  return `<details class="rhythm-source"><summary title="查看拍点来源，或分析新的候选">${icon("beat")}${state.rhythm_candidate_running?'正在分析拍点候选':current}</summary><div><p>当前来源：${html(origin)}。${preserved?"保留了你的人工修改。":""}分析新候选不会立即覆盖当前拍点，比较后由你决定是否采用。</p>${button('分析新的拍点候选','openRhythmCandidates',{disabled:Boolean(state.job.running||state.gpu_preflight||state.rhythm_candidate_running)})}</div></details>`;
}

function rhythmProviderFields(opts) {
  return `<label class="field full">节拍来源<select class="control" id="option-rhythm_provider">${[["cubase_local","本机拍点算法"],["beat_this","兼容拍点分析"]].map(([value,text])=>`<option value="${value}" ${(opts.rhythm_provider||"cubase_local")===value?"selected":""}>${text}</option>`).join("")}</select></label>`;
}

function transcriptionCandidateCurrent(request) {
  return transcriptionCandidate===request && request.project===state.project?.path
    && request.base_revision===state.project?.editor?.revision
    && request.candidate_key===state.project?.piano_analysis_key
    && dialog==="transcription-candidate" && $("dialog").open;
}

function candidateError(message) {
  const error=$("candidate-error");if(error)error.textContent=message;
  const apply=document.querySelector('[data-do="confirmTranscriptionCandidate"]');if(apply)apply.disabled=true;
}

function openTranscriptionCandidate() {
  if(!state.project?.editor || state.job.running || state.gpu_preflight)return;
  const request={project:state.project.path,base_revision:state.project.editor.revision,candidate_key:state.project.piano_analysis_key};
  transcriptionCandidate=request;
  modal("采用新转录候选",'<p>正在核验已缓存的候选与当前编辑版本…</p><p id="candidate-error" role="alert"></p>',button("取消","closeDialog"),"transcription-candidate");
  if(typeof bridge?.previewTranscriptionCandidate!=="function") {candidateError("当前程序不支持候选采用，请更新应用后重试。");return;}
  bridge.previewTranscriptionCandidate(JSON.stringify({base_revision:request.base_revision}),raw=>{
    if(!transcriptionCandidateCurrent(request)){
      if(transcriptionCandidate===request && dialog==="transcription-candidate" && $("dialog").open)candidateError("工程或候选已变化，请重新打开确认。");
      return;
    }
    let result;try {result=JSON.parse(raw);}catch(_){candidateError("候选确认响应无法读取，请重新打开。");return;}
    if(!result.ok){candidateError(result.error||"无法核验候选。");return;}
    if(result.base_revision!==request.base_revision || result.candidate_key!==request.candidate_key){candidateError("候选或工程已变化，请重新打开确认。");return;}
    request.token=result.token;
    modal("采用新转录候选",`<p><strong>替换当前人工音符和全部 CC。</strong>不会猜测合并手动删音、改音或踏板修改。</p>
      <table class="harmony-table"><thead><tr><th>事件</th><th>当前人工稿</th><th>新转录</th></tr></thead><tbody><tr><td>音符</td><td>${html(result.old_notes)}</td><td>${html(result.new_notes)}</td></tr><tr><td>CC 控制器</td><td>${html(result.old_controls)}</td><td>${html(result.new_controls)}</td></tr></tbody></table>
      <p>保留当前生效拍位图、拍号、${html(result.fixed_anchors)} 个固定锚点与选区。新转录的原始秒时间保持不变。</p>
      <p>采用后可试听，再更新 MIDI / 谱面；已有导出不覆盖。<strong>一次撤销即可恢复当前人工稿。</strong></p><p id="candidate-error" role="alert"></p>`,
      button("取消","closeDialog")+button("替换音符与 CC","confirmTranscriptionCandidate",{style:"primary"}),"transcription-candidate");
  });
}

function confirmTranscriptionCandidate() {
  const request=transcriptionCandidate;
  if(!request || !transcriptionCandidateCurrent(request) || !request.token){candidateError("工程或候选已变化，请重新打开确认。");return;}
  if(state.job.running || state.gpu_preflight){candidateError("请等待当前任务完成，再重新打开确认。");return;}
  document.querySelector('[data-do="confirmTranscriptionCandidate"]').disabled=true;
  dispatch("adoptTranscriptionCandidate",{token:request.token,base_revision:request.base_revision},()=>{
    if(transcriptionCandidate!==request)return;
    transcriptionCandidate=null;
    if(state.project?.path===request.project && state.project.editor?.revision!==request.base_revision && !state.job.error){
      if(dialog==="transcription-candidate" && $("dialog").open)$("dialog").close();
      const working=midiFiles().find(file=>file.editor);
      if(working)changeTab("notes",working);else changeTab("notes");
    }else if(dialog==="transcription-candidate" && $("dialog").open)candidateError(state.job.error||"候选未应用，请重新打开确认。");
  });
}

function harmonyNeedsUpdate() { return Boolean(state.project?.editor?.dirty || (state.project?.score_export && state.project?.editor?.score_stale)); }

function harmonyUpdateRequest() {
  const editor=state.project?.editor;
  if (!editor || !editor.dirty || state.job.running || state.gpu_preflight) return null;
  const files=resultFiles().filter(file=>file.instrument==="piano");
  const hasScore=files.some(file=>["pdf","musicxml"].includes(file.kind));
  const hasMidi=files.some(file=>file.kind==="midi");
  return {base_revision:editor.revision,mode:hasScore?(hasMidi?"all":"score"):"midi",purpose:"mapped",include_time_signatures:true};
}

function harmonyUpdateNotice() {
  if (!harmonyNeedsUpdate()) return "";
  return `<div class="editor-score-stale" role="status"><span>谱面待更新。拍点或小节有改动时，更新会让 DuET 按当前拍点重新写谱（约 1–2 分钟），不会重新转录音符。</span>${button("更新谱面","updateForHarmony",{disabled:Boolean(state.job.running || state.gpu_preflight),style:"primary"})}</div>`;
}

function chordVisibilityControl() {
  const project=state.project, hasHarmonyCache=Boolean(project?.harmony?.piano?.chords?.length);
  if (activeInstrument!=="piano" || !(hasHarmonyCache || project?.score_chords)) return "";
  return button(project.score_chords?"关闭和弦记号":"开启和弦记号","setChordVisibility",{
    disabled:Boolean(state.job.running || state.gpu_preflight || harmonyNeedsUpdate() || (!project.score_chords && !hasHarmonyCache)),
    label:"修改已导出的钢琴 MIDI / 乐谱和弦记号；不重新分析音频"});
}

function audioHarmonyControl() {
  if (activeInstrument !== "piano" || state.project?.input_kind !== "audio" || !state.project.instruments?.includes("piano")) return "";
  return `<label class="harmony-engine-label">和弦模型 <select id="harmony-engine" class="control" ${state.job.running?'disabled':''}><option value="tsumugi" ${state.project.harmony_engine==='tsumugi'?'selected':''}>Tsumugi-Chords</option><option value="chordformer" ${state.project.harmony_engine!=='tsumugi'?'selected':''}>ChordFormer</option></select></label>` + button(state.project.harmony_engine ? "重新分析和弦" : "分析和弦","analyzeHarmony",{
    disabled:Boolean(state.job.running || state.gpu_preflight || harmonyNeedsUpdate()),
    label:"用项目音频分析和弦并更新当前导出"
  });
}

// Report notation freshness, not MIDI save/export state.
function scoreStateStrip(file) {
  const ed=state.project?.editor;if(!ed||activeInstrument!=="piano"||!file)return "";
  const running=Boolean(state.job?.running),fresh=ed.score_stale===false;
  const old=Boolean(file.historical||state.project?.score_pdf_issue||state.pdf.replacementPath);
  const status=running?"处理期间仍可查看当前谱面":old?"正在显示上一份可用 PDF":fresh?"与当前编辑一致":"待更新（之后有编辑）";
  return `<div class="score-state" role="status"><span class="score-state-chip" data-tone="${running?"working":!old&&fresh?"ok":"stale"}"><small>谱面</small>${status}</span></div>`;
}

// One project-level state token: derived only from what the snapshot states.
function projectStatusToken(result) {
  const ed=state.project?.editor,job=state.job||{};
  if(job.running)return `<span class="status-token working" title="任务进行中；当前显示的是上一次的结果">处理中…</span>`;
  if(job.error)return `<span class="status-token failed" title="${html(job.error)}">${result?"上次任务失败 · 旧成果保留":"处理失败"}</span>`;
  if(!result)return `<span class="status-token">待转谱</span>`;
  if(ed&&ed.dirty===true)return `<span class="status-token pending" title="已有导出；之后的编辑尚未导出为 MIDI">${icon("check")}已导出 · 有未导出修改</span>`;
  return `<span class="status-token ready">${icon("check")}已导出</span>`;
}

// Title row = what needs attention or action now: a running/failed task, the
// project save state, a recovery offer, a failed backup (with its retry).
// Source facts, export state, routine backup state sit in
// one 详情 popover. Save semantics and the elements' ids are unchanged.
function projectFactsMarkup(result,meta) {
  if(!result)return `${meta?`<span>${html(meta)}</span>`:""}${projectStatusToken(result)}`;
  const ed=state.project?.editor,job=state.job||{},urgent=Boolean(job.running||job.error);
  const exportElsewhere=Boolean(!urgent&&ed&&ed.dirty===true&&!["notes","score"].includes(tab));
  const row=(label,value,extra="")=>`<div class="project-detail-row"${extra}><dt>${label}</dt><dd>${value}</dd></div>`;
  const rows=[
    meta?row(fromMidiProject()?"来源":"音频",html(meta)):"",
    urgent?"":row("导出",projectStatusToken(result)),
    ed?`<div class="project-detail-row" hidden><dt>恢复副本</dt><dd id="project-backup-detail"></dd></div>`:""
  ].join("");
  const badges=`${exportElsewhere?'<span class="project-details-badge" data-tone="pending">未导出</span>':""}`;
  return `${urgent?projectStatusToken(result):""}${saveStatusMarkup(result)}${projectRecoveryMarkup(result)}<details class="editor-command-menu project-details" data-disclosure="project-details"><summary title="项目详情：来源、导出状态与恢复副本" aria-label="项目详情${exportElsewhere?"，有未导出修改":""}"><span>详情</span>${badges}</summary><div class="editor-command-body project-details-body"><dl class="project-detail-list">${rows}</dl></div></details>`;
}
function fromMidiProject(){return state.project?.input_kind==="midi";}

let scoreModeProject=null, scoreModeGeneration=null, scoreModeEditing=false;
function scorePageEditing(){
  const project=isResult()?state.project.path:null;
  const generation=project?state.project.editor?.edit_stamp?.generation??null:null;
  if(project!==scoreModeProject||generation!==scoreModeGeneration){
    scoreModeProject=project;scoreModeGeneration=generation;scoreModeEditing=false;
  }
  return scoreModeEditing;
}

function scoreWorkingDocument(){
  const doc=state.project?.editor;
  return isResult() && activeInstrument==="piano" && doc?.revision && doc.notes?.length ? doc : null;
}

function scoreBuildBlocked(){
  return Boolean(!scoreWorkingDocument() || state.job.running || state.gpu_preflight ||
    state.rhythm_candidate_running || state.project_opening?.pending);
}

function scoreModeMarkup(editing){
  const segment=(key,label,on,title)=>`<button type="button" data-do="scoreMode" data-key="${key}" aria-pressed="${on}" aria-controls="score-build-panel" title="${title}">${label}</button>`;
  return `<div id="score-mode" class="editor-heading-actions score-mode-actions"><div class="segments editor-mode-switch" role="group" aria-label="乐谱模式：编辑或只读浏览">${segment("edit","编辑",editing,"显示制谱操作，不会自动生成")}${segment("read","只读浏览",!editing,"只读浏览已有 PDF")}</div></div>`;
}

function scoreBuildPanel(editing,previous){
  const doc=scoreWorkingDocument();
  const body=doc
    ? `<p>按「音符」中的当前版本本地制谱，不重新转录或修改音符；新谱面与配套 MIDI 另存一版，不覆盖旧成果。</p>${button(previous?"更新谱面":"生成乐谱","scoreBuild",{style:"primary",symbol:"score",disabled:scoreBuildBlocked()})}`
    : `<p>${activeInstrument!=="piano"?"此声部当前不支持自动制谱。":"没有可制谱的钢琴工作稿。请先在「音符」中打开或准备可编辑的钢琴音符。"}</p>`;
  return `<section id="score-build-panel" class="score-build-panel" aria-label="本地制谱"${editing?"":" hidden"}>${body}</section>`;
}

function setScorePageMode(mode){
  if(tab!=="score" || !isResult() || !["read","edit"].includes(mode))return;
  const editing=mode==="edit";
  if(editing===scorePageEditing())return;
  scoreModeEditing=editing;
  const controls=$("score-build-panel"),specialists=$("score-specialist-tools"),hint=$("score-read-first-hint");
  if(controls)controls.hidden=!editing;
  if(specialists)specialists.hidden=!editing;
  if(hint)hint.hidden=editing;
  $("score-mode")?.querySelectorAll('[data-do="scoreMode"]').forEach(el=>el.setAttribute("aria-pressed",String(el.dataset.key===mode)));
  // Keep the exact paper/image/scroll nodes for an ordinary mode change. Only
  // an explicitly opened legacy ornament preview differs from the formal PDF.
  if(AstraOrnamentReview.hasPreview())render(true);
}

function requestScoreBuild(){
  if(tab!=="score" || !scorePageEditing() || scoreBuildBlocked())return;
  // Same exact-revision, busy/queue and publication path as Notes → 更新谱面.
  AstraProjectEditor.action("editorScore");
}

let retainedScoreImage=null;
function scorePreviewUrl(pdf){
  const url=pdf.previewUrl || "";
  return /^astra-score:\/\/page\/[0-9a-f]{32}$/.test(url) ? url : "";
}
function scorePreviewPlaceholder(pdf){
  return '<img id="score-page-image" decoding="async" alt="'+html(pdf.title || "PDF")+
    ' '+(pdf.index+1)+'" width="'+Number(pdf.width)+'" height="'+Number(pdf.height)+'" '+
    'style="height:auto;'+(scoreZoom===null?"":"width:"+Math.round(790*scoreZoom)+"px;max-width:none")+'">';
}
function hydrateScorePreview(){
  const url=scorePreviewUrl(state.pdf);
  if(retainedScoreImage && retainedScoreImage.getAttribute('src')!==url) retainedScoreImage=null;
  const fresh=$('score-page-image');
  if(!fresh || !url)return;
  if(retainedScoreImage){
    if(fresh===retainedScoreImage)return;
    retainedScoreImage.alt=fresh.alt;
    retainedScoreImage.style.cssText=fresh.style.cssText;
    fresh.replaceWith(retainedScoreImage);
    return;
  }
  retainedScoreImage=fresh;
  fresh.src=url;
}

function selectedScoreFile(files=resultFiles("pdf")){
  return files.find(file=>file.path===(pdfSelected||state.pdf.path)&&file.instrument===activeInstrument)
    || instrumentFile("pdf",activeInstrument);
}

function visibleScorePreview(file){
  const pdf=state.pdf;
  return file?.path===pdf.path ? pdf : {pages:0,index:0,loading:Boolean(file)};
}

function scoreOrnamentPreview(){return scorePageEditing() && AstraOrnamentReview.hasPreview();}

function scorePaperKey(pdf,file){return JSON.stringify([file?.path,pdf.index,scoreOrnamentPreview()]);}

function scorePaper(pdf){
  return (scoreOrnamentPreview()?AstraOrnamentReview.previewMarkup():"") || (scorePreviewUrl(pdf) ? scorePreviewPlaceholder(pdf) : /^data:image\/(png|jpeg);base64,[A-Za-z0-9+/=]+$/.test(pdf.dataUrl || "") ? `<img alt="${html(pdf.title || "乐谱")}，第 ${pdf.index+1} 页" src="${pdf.dataUrl}" style="${scoreZoom === null ? "" : `width:${Math.round(790*scoreZoom)}px;max-width:none`}">` : `<p role="status">${pdf.loading ? "正在读取 PDF 预览…" : "当前暂无页面预览，可打开 PDF 查看。"}</p>`);
}

function scorePageCount(pdf){
  return pdf.pages ? (pdf.index+1)+" / "+pdf.pages : "—";
}

let scorePreviewRendered=null;
function syncScorePreview(){
  const paper=$("workspace").querySelector(".paper-area");
  if(!paper)return;
  const file=selectedScoreFile(),pdf=visibleScorePreview(file);
  paper.dataset.scorePage=scorePaperKey(pdf,file);
  const view=[pdf.path,pdf.index,pdf.pages,pdf.title,pdf.loading,pdf.width,pdf.height,pdf.previewUrl,pdf.dataUrl];
  if(scorePreviewRendered?.paper===paper&&sameRollInputs(scorePreviewRendered.view,view))return;
  const samePaper=scorePreviewRendered?.paper===paper;
  scorePreviewRendered={paper,view};
  if(!samePaper)return;  // Fresh markup is already current.
  const count=$("score-page-count"),previous=$("workspace").querySelector('[data-do="pdfPrev"]'),
    next=$("workspace").querySelector('[data-do="pdfNext"]');
  count.textContent=scorePageCount(pdf);
  previous.disabled=!(pdf.index>0);
  next.disabled=!(pdf.index+1<pdf.pages);
  if(!scoreOrnamentPreview()){
    paper.innerHTML=scorePaper(pdf);
    hydrateScorePreview();
  }
}

function score() {
  const files=resultFiles("pdf"), selectedPdf=selectedScoreFile(files),editing=scorePageEditing();
  const pdf=visibleScorePreview(selectedPdf);
  const instrument=activeInstrument,xml=instrumentFile("musicxml",instrument);
  const scoreMidi=midiFiles().find(file=>file.instrument===instrument&&isScoreMidi(file.name)&&
    (!state.project?.score_export || file.path.replace(/\\/g,"/").slice(0,file.path.replace(/\\/g,"/").lastIndexOf("/"))===state.project.score_export.replace(/\\/g,"/")));
  const audition=scoreMidi?button("试听谱面","auditionScore",{symbol:"play",path:scoreMidi.path,label:"在音符页播放记谱校正版 MIDI"}):"";
  const temporary=scoreOrnamentPreview();
  const toolbar=`<div class="score-bar">${selectedPdf?`<select class="control" id="pdf-select" aria-label="选择乐谱"${temporary?' disabled':''}>${choices(files,selectedPdf.path)}</select><div class="score-tools"${temporary?' inert title="返回正式谱面以翻页或试听"':''}>
    ${button("","pdfPrev",{style:"icon-button previous",symbol:"chevron",label:"上一页",disabled:Boolean(pdf.replacementPath)||!(pdf.index>0)})}<span id="score-page-count">${scorePageCount(pdf)}</span>${button("","pdfNext",{style:"icon-button",symbol:"chevron",label:"下一页",disabled:Boolean(pdf.replacementPath)||!(pdf.index+1<pdf.pages)})}
    ${button("−","scoreZoomOut",{label:"缩小乐谱",disabled:scoreZoom!==null&&scoreZoom<=.5})}<output id="score-zoom-label">${scoreZoom===null?"适宽":`${Math.round(scoreZoom*100)}%`}</output>${button("+","scoreZoomIn",{label:"放大乐谱",disabled:scoreZoom>=2})}${button("适宽","scoreFit")}
    ${audition}${button("PDF","openFile",{symbol:"external",path:selectedPdf.path,label:"打开 PDF 文件"})}</div>`:'<span class="score-page-title">乐谱预览</span>'}${scoreModeMarkup(editing)}</div>`;
  const common=toolbar+scoreBuildPanel(editing,Boolean(selectedPdf||xml))+scoreStateStrip(selectedPdf);
  if(!selectedPdf){
    const hint=scoreWorkingDocument()?"切换到「编辑」可从当前音符生成 PDF。":"此处可浏览已生成的 PDF 乐谱。";
    return common+`<div class="surface-note"><strong>${instruments[instrument]||"此声部"}尚未生成 PDF 乐谱</strong><p id="score-read-first-hint"${editing?" hidden":""}>${hint}</p></div>`;
  }
  // Preserve previously available specialist tools without mixing them into
  // the one new generation action or surfacing them in PDF-only mode.
  const harmony=state.project?.harmony?.[instrument], chordControl=chordVisibilityControl(),analysis=audioHarmonyControl();
  const chords=chordControl||analysis?`<details class="score-extra" data-disclosure="chords"><summary>和弦${chordControl?(state.project.score_chords?" · 已开启":" · 已关闭"):""}</summary><div class="score-chord-controls">${chordControl}${analysis}${chordControl&&!harmony?.chords?.length?'<p class="form-note">此导出缺少和弦分析。</p>':""}${harmonyNeedsUpdate()?'<p class="form-note">请先用上方「更新谱面」同步当前版本。</p>':""}</div></details>`:"";
  const ornaments=AstraOrnamentReview.markup();
  return common+`<div id="score-specialist-tools" class="score-disclosures"${editing?"":" hidden"}>
    ${harmony?.key_regions?.length?button("和声","harmony",{label:"查看调性分段与和弦分析"}):""}${chords}
    <details class="score-extra" data-disclosure="audit"><summary>审核意见</summary><div>${reviewActions()}</div></details>${ornaments}
    </div><div class="paper-area" tabindex="0" aria-label="乐谱预览，可滚动查看" data-score-page="${html(scorePaperKey(pdf,selectedPdf))}">${scorePaper(pdf)}</div>${scoreMidi?'<p class="score-hint">试听按校正后的记谱节奏播放，位置可能与原曲不同。</p>':""}`;
}

function midiFiles() {
  const files=resultFiles("midi").filter((file) => file.instrument in instruments || /(?:piano|guitar)_/.test(file.name || ""));
  const path=AstraProjectEditor.path();
  if(path && !files.some(file=>file.path===path))files.unshift({path,name:'piano_edited.mid',instrument:'piano',kind:'midi',editor:true});
  return files;
}

// Keep routine sources first; all other available versions follow in the same list.
function mainMidiFiles(files) {
  return files.filter(file => {
    if (file.historical) return false;
    if (file.editor || /transcription/i.test(file.name || '')) return true;
    if (/quant|reviewed/i.test(file.name || '') || isScoreMidi(file.name || '')) return false;
    return !files.some(other => other.editor && other.instrument === file.instrument);
  });
}
// The concise piano-roll source list (not searchable). Entries come from the
// project's real MIDI files only; nothing is synthesized for an empty list.
function midiSourceEntries(files, active) {
  const primary = mainMidiFiles(files);
  // Projects without a working/raw rendition must still have a usable selector.
  if (!primary.length && files.length) primary.push(files.find(file => file.path === active) || files[0]);
  const extra = files.filter(file => !primary.includes(file));
  const entries = [...primary, ...extra].map(file=>{const full=[instruments[file.instrument],fileTitle(file)].filter(Boolean).join(' · ');return {path:file.path,full,compact:full.replace('当前演奏 MIDI（未量化）','演奏 · 未量化').replace('人工编辑结果','编辑结果').replace('原始转录 MIDI','原始转录').replace('记谱校正版 MIDI','记谱校正')};});
  return {entries,selected:entries.find(entry=>entry.path===active)||entries[0]||null};
}
function midiSourceMarkup(files, active) {
  const {selected} = midiSourceEntries(files, active);
  const selectedName=selected?.full||'MIDI';
  // App-owned trigger (same id, toolbar geometry and change route as the
  // former native <select>); its list is the shared #midi-source-menu popup.
  return `<button type="button" class="control midi-source-trigger" id="midi-select" data-do="midiSourceMenu" value="${html(selected?.path||active||'')}" data-working-path="${html(AstraProjectEditor.workingView()&&active===AstraProjectEditor.path()?active:'')}" aria-haspopup="listbox" aria-expanded="false" aria-controls="midi-source-menu" aria-label="钢琴窗显示对象：${html(selected?.compact||'MIDI')}" title="${html(selectedName)}&#10;只切换显示，不覆盖任何版本"><span class="midi-source-name">${html(selected?.compact||'MIDI')}</span><span class="midi-source-arrow" aria-hidden="true">▾</span></button>`;
}

// Same route as the former <select> change: navigation guard, then selectMidi.
// The trigger keeps showing the committed source until render() reflects the
// new state; nothing claims the switch before the bridge does.
function chooseMidiSource(value) {
  const file=midiFiles().find(candidate=>candidate.path===value);
  if(!file)return false;
  if(value!==state.midi.path && window.AstraStructureEditor?.guardNavigation())return false;
  pendingScoreAudition = null; midiSelected = value; activeInstrument=file.instrument || activeInstrument;
  dispatch("selectMidi",{path:value});
  return true;
}
const midiSourceMenu = (() => {
  let box=null,pop=null,painted='';
  function ensure(){
    if(box)return;
    box=document.createElement('div');
    box.id='midi-source-menu';box.className='astra-menu midi-source-menu';
    box.setAttribute('role','listbox');box.setAttribute('aria-label','钢琴窗显示对象');box.hidden=true;
    document.body.append(box);
    pop=window.AstraPopup.anchored(box,{name:'midi-source',width:r=>Math.min(320,Math.max(r.width,220)),minBelow:120});
    window.AstraPopup.roving(box,'[role=option]');
    box.addEventListener('click',event=>{
      const option=event.target.closest?.('[role=option]');if(!option||!box.contains(option))return;
      const path=option.dataset.path,trigger=pop.anchor;
      // Single choice: close at once and return focus, then route the choice.
      pop.hide(true);
      if(path&&path!==trigger?.value)chooseMidiSource(path);
    });
    box.addEventListener('keydown',event=>{if(event.key==='Tab'){event.preventDefault();pop.hide(true);}});
  }
  // Rebuilt only when the entries or the selection change; a repeat open of
  // an unchanged list builds no DOM.
  function paint(trigger){
    const {entries}=midiSourceEntries(midiFiles(),trigger.value);
    const key=JSON.stringify([trigger.value,entries]);
    if(painted===key)return;
    painted=key;
    const focused=box.contains(document.activeElement)?document.activeElement.dataset.path:null;
    box.replaceChildren(...entries.map(entry=>{
      const option=document.createElement('button');
      option.type='button';option.className='astra-menu-item';option.setAttribute('role','option');
      option.dataset.path=entry.path;option.title=entry.full;option.textContent=entry.compact;
      option.setAttribute('aria-selected',String(entry.path===trigger.value));
      return option;
    }));
    if(focused)[...box.children].find(option=>option.dataset.path===focused)?.focus({preventScroll:true});
  }
  function focusSelected(){(box.querySelector('[aria-selected=true]')||box.firstElementChild)?.focus({preventScroll:true});}
  return {
    get anchor(){return pop?.anchor||null;},
    // Shell and options are built synchronously from in-memory state (no
    // bridge call, no file scan), so the first frame already has the list.
    toggle(trigger){
      ensure();
      if(pop.anchor===trigger){pop.hide(true);return;}
      paint(trigger);
      pop.show(trigger);
      if(pop.anchor)focusSelected();
    },
    // After render(): follow a remounted trigger (no replayed entrance) or close.
    sync(){
      if(!pop?.anchor)return;
      const trigger=$('midi-select');
      if(!trigger||trigger.tagName!=='BUTTON'){pop.hide();return;}
      pop.reanchor(trigger);paint(trigger);
    },
    close(){pop?.hide();}
  };
})();
// ArrowUp/ArrowDown on the trigger open the list (as the native select did).
// Window capture: ahead of the editor's document-level note shortcuts.
window.addEventListener('keydown',event=>{
  const trigger=event.target;
  if(trigger?.id!=='midi-select'||trigger.tagName!=='BUTTON'||event.altKey||event.ctrlKey||event.metaKey||event.isComposing)return;
  if(event.key==='ArrowDown'||event.key==='ArrowUp'){event.preventDefault();event.stopPropagation();if(!midiSourceMenu.anchor)midiSourceMenu.toggle(trigger);}
},true);

// Editor top-right view tools (Cubase keeps window-layout toggles here):
// one zoom menu replaces the on-canvas +/−/全曲/音域 buttons, whose hidden
// elements stay for state sync; wheel and keyboard zoom are unchanged.
// The existing transport controller publishes its persisted fold state here.
// Neither the View menu nor this layout projection writes a second preference.
const viewAreaTargets = {editorInfoLineToggle:'editor-info-bar',editorCCLaneToggle:'pedal-lane pedal-divider',editorInspector:'editor-left-zone',editorTransportFold:'transport'};
const transportFolded = () => $("transport").dataset.folded === 'true';
function viewAreaState(action) {
  if(action==='editorInfoLineToggle')return {checked:!AstraProjectEditor.infoLineHidden()};
  if(action==='editorCCLaneToggle')return {checked:!AstraProjectEditor.ccLaneHidden()};
  if(action==='editorTransportFold')return {checked:transportAvailable&&!transportFolded(),disabled:!transportAvailable};
  const covered=AstraProjectEditor.inspectorCovered();
  return {checked:!covered&&!AstraProjectEditor.inspectorCollapsed(),disabled:covered};
}
function viewZoneTitle(action,{checked,disabled}) {
  if(action==='editorInspector')return disabled?'乐器与 MIDI 侧栏（当前不可用）':`${checked?'隐藏':'显示'}乐器与 MIDI 侧栏 · Alt+I`;
  if(action==='editorInfoLineToggle')return `${checked?'隐藏':'显示'}信息行 · Ctrl+I`;
  return disabled?'走带（当前不可用）':`${checked?'隐藏':'显示'}走带 · Ctrl+Alt+T`;
}
function syncEditorViewControls(action) {
  const selector=action?`[data-view-area][data-do="${action}"]`:'[data-view-area]';
  for(const control of document.querySelectorAll(selector)){
    const view=viewAreaState(control.dataset.do),pressed=String(view.checked),disabled=Boolean(view.disabled);
    if(control.getAttribute('aria-pressed')!==pressed)control.setAttribute('aria-pressed',pressed);
    if(control.disabled!==disabled)control.disabled=disabled;
    if(control.classList.contains('view-zone-toggle')){
      const title=viewZoneTitle(control.dataset.do,view);if(control.title!==title)control.title=title;
    }
  }
}
function viewTools() {
  const svg = (d) => `<svg class="line-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="${d}"/></svg>`;
  const zone=(action,label,name,shape)=>{
    const view=viewAreaState(action);
    return `<button type="button" class="quiet view-zone-toggle ${name}" data-do="${action}" data-view-area aria-label="${label}" aria-controls="${viewAreaTargets[action]}" aria-pressed="${view.checked}" title="${viewZoneTitle(action,view)}"${view.disabled?' disabled':''}>${svg(shape)}</button>`;
  };
  const sidebar=AstraProjectEditor.active()||AstraProjectEditor.workingView()
    ?zone('editorInspector','乐器与 MIDI 侧栏','inspector-switch','M4 5h16v14H4zM9.5 5v14'):''; 
  return `<div class="view-zone-tools" role="group" aria-label="界面区域">${sidebar}
    ${zone('editorTransportFold','走带','view-zone-transport','M4 5h16v14H4zM4 14h16')}
    ${zone('editorInfoLineToggle','信息行','view-zone-info','M4 5h16v14H4zM4 9h16M8 7h2m2 0h4')}</div>`;
}


function midi() {
  AstraNoteInspector.sync(state.midi.path);
  const files = midiFiles();
  if (!instrumentFile("midi",activeInstrument) && !(activeInstrument==="piano" && AstraProjectEditor.path())) return activeInstrument === "guitar" ? guitarAudioHint() : `<div class="surface-note"><strong>${instruments[activeInstrument] || "此声部"}尚未导出 MIDI</strong><p>在设置中选择对应乐器与 MIDI 输出，再重导出。</p>${button("导出设置","advanced",{symbol:"settings",disabled:state.job.running})}</div>`;
  let active = pendingScoreAudition?.path || midiSelected || state.midi.path || files[0].path;
  // Read-only browsing of the project working MIDI: a superseded working
  // preview path (no longer listed) is not a user choice, and a rebuilding
  // preview keeps its last decoded notes on screen instead of a placeholder
  // that would collapse the page and the roll viewport.
  const workingPath=state.project?.editor?AstraProjectEditor.path():null;
  const workingView=AstraProjectEditor.workingView();
  if(workingView&&!files.some(file=>file.path===active))active=workingPath;
  const keepWorkingRoll=workingView&&active===workingPath;
  if ((state.midi.loading || state.midi.path !== active) && !keepWorkingRoll && !AstraProjectEditor.active()) return `<div class="midi-bar"><div class="midi-file-group">${midiSourceMarkup(files,active)}</div></div><p class="surface-note" role="status">正在读取 MIDI 音符…</p>`;
  return `<div class="note-toolbar"${workingView?" data-stable-mode":""}><div class="midi-bar"><div class="midi-file-group">${midiSourceMarkup(files,active)}${button("打开 MIDI",active===AstraProjectEditor.path()?"editorExternalMidiOpen":"openFile",{symbol:"external",path:active})}</div><div class="midi-tools">${followMenu()}${workingView||AstraProjectEditor.active()?'':readonlyGridMenu()}${viewTools()}</div></div>
    ${state.project?.editor_setup_required ? button("编辑","enableExternalEditor",{disabled:state.job.running,style:"primary"}) : state.project?.editor || state.project?.editor_unavailable ? AstraProjectEditor.panel() : AstraRollMeter.panel()}</div><div class="editor-workspace"${workingView?` data-side-mode="${AstraProjectEditor.active()?"edit":"read"}"`:""}>${workingView?editorModeSidebarMarkup():AstraProjectEditor.active()?editorModeSidebarMarkup():""}<div class="editor-main"><div id="editor-info-bar"${AstraProjectEditor.infoLineHidden()?' hidden':''}>${AstraProjectEditor.infoLineMarkup?.()||""}</div><div class="midi-editor"><div class="roll-ruler"><span id="roll-ruler-unit">${rulerUnitLabel()}</span><canvas id="ruler-canvas" aria-label="上方拖动 L/R 选区；下方拖动播放头；Ctrl 点击设置 L，Alt 点击设置 R"></canvas><span id="ruler-playhead" role="slider" aria-label="拖动播放头" title="拖动播放头定位"></span></div>${state.midi.markers?.length ? `<div class="chord-lane"><span>和弦</span><canvas id="chord-canvas" aria-label="和弦标记，点击定位"></canvas></div>` : ""}${AstraProjectEditor.timelineLanes()}<div class="midi-roll"><div class="roll-keys"><canvas id="keys-canvas" width="52"></canvas></div><div class="roll-scroll" id="roll-scroll" tabindex="0" aria-label="钢琴卷帘：Ctrl 滚轮缩放，Shift 滚轮横移，Ctrl+Shift 滚轮纵向缩放"><div id="roll-content"><canvas id="midi-canvas" aria-label="钢琴卷帘音符"></canvas><span id="midi-cursor" class="playhead"></span></div></div><div class="pitch-zoom-rail" aria-label="音高纵向缩放" title="音高缩放；双击适合音域">${button("+","pitchZoomIn",{label:"放大音高；默认 Shift+H"})}<input id="pitch-zoom" type="range" min="0.5" max="4" step="0.05" value="${pitchZoom}" aria-label="音高纵向缩放" title="音高缩放；双击适合音域">${button("−","pitchZoomOut",{label:"缩小音高；默认 Shift+G"})}</div><div class="roll-navigation"><div class="zoom-group horizontal-zoom" role="group" aria-label="时间缩放">${button("−","zoomOut",{label:"缩小 Ctrl + −"})}<input id="midi-zoom" type="range" min="0" max="7" step="0.05" value="${Math.log2(zoom)}" aria-label="卷帘横向缩放" title="时间缩放；双击适合全曲；Ctrl + 滚轮">${button("+","zoomIn",{label:"放大 Ctrl + +"})}${button("全曲","zoomFit",{label:"适合全曲 Ctrl + 0"})}${button("音域","pitchFit",{label:"回到音符音域；上下滚动可编辑 MIDI 0–127"})}</div></div></div>${rollLaneSeparator('velocity')}
<div class="velocity-lane" id="velocity-lane" style="--lane-height:${rollLanes.velocity.height}px" data-folded="${rollLanes.velocity.folded}">${rollLaneLabel('velocity','1–127')}<canvas id="velocity-canvas" aria-label="音符力度：拖动绘制；有选中音符时只改所选"></canvas></div>${rollLaneSeparator('pedal')}<div class="pedal-lane" id="pedal-lane"${AstraProjectEditor.ccLaneHidden()?' hidden':''} style="--lane-height:${rollLanes.pedal.height}px" data-folded="${rollLanes.pedal.folded}">${rollLaneLabel('pedal','0–127')}<canvas id="pedal-canvas" aria-label="延音踏板控制信号"></canvas></div><div class="roll-time-scrollbar"><input id="roll-time-scroll" type="range" min="0" max="0" value="0" step="any" disabled aria-label="钢琴卷帘水平滚动" aria-controls="roll-scroll"></div></div><div class="midi-footer"><span><span id="midi-message"></span><span id="note-write-status" role="status" aria-live="polite" hidden></span></span></div></div></div>`;
}

function editorModeSidebarContents(){return `<section id="editor-vst-slot" class="vst-slot" aria-label="VST 乐器" hidden></section>${instrumentFaderMarkup()}${AstraProjectEditor.inspectorMarkup()}`;}
function editorModeSidebarMarkup(){
  const on=AstraProjectEditor.active();
  return `<aside id="editor-left-zone" class="editor-side" aria-label="乐器与 MIDI 侧栏" ${on?'':'inert aria-hidden="true"'}>${on?editorModeSidebarContents():''}</aside>`;
}
let modeProjection=null,modeViewport=null,modeSideAnimation=null,modeLayout=null;
function playModeSidebar(workspace,side,on,wasOpen){
  const old=modeSideAnimation,style=old?getComputedStyle(side):null;
  const from=style?{transform:style.transform,opacity:style.opacity}:null;
  if(old){old.cancel();modeSideAnimation=null;}
  workspace.removeAttribute('data-side-leaving');
  const wide=wasOpen?.width||side.getBoundingClientRect().width;
  if(!side.animate||wide<=1||!side.firstElementChild||AstraProjectEditor.inspectorCollapsed()||matchMedia('(prefers-reduced-motion: reduce)').matches){workspace.style.removeProperty('--mode-side-width');return;}
  if(!on){workspace.style.setProperty('--mode-side-width',`${wide}px`);workspace.setAttribute('data-side-leaving','');}
  const hidden={transform:'translateX(-100%)',opacity:.88},shown={transform:'translateX(0)',opacity:1};
  const motion=side.animate([from||(on?hidden:shown),on?shown:hidden],{duration:145,fill:'both',easing:'cubic-bezier(.2,.75,.3,1)'});
  modeSideAnimation=motion;
  const tidy=()=>{
    if(modeSideAnimation!==motion)return;
    modeSideAnimation=null;workspace.removeAttribute('data-side-leaving');workspace.style.removeProperty('--mode-side-width');
  };
  motion.onfinish=()=>{tidy();motion.cancel();};motion.oncancel=tidy;
}
function patchWorkingMode(on){
  const scroll=$('roll-scroll'),workspace=scroll?.closest('.editor-workspace'),side=workspace?.querySelector('.editor-side');
  if(!workspace||!side)return false;
  if(on&&!side.firstElementChild)side.innerHTML=editorModeSidebarContents();
  if(!on&&side.contains(document.activeElement))document.querySelector('[data-do=editorReadOnly]')?.focus({preventScroll:true});
  // Permissions change immediately. Commit the clip edge in the canvas frame
  // so no composed frame contains a new origin plus the old scroll/raster.
  side.inert=!on;
  if(on)side.removeAttribute('aria-hidden');else side.setAttribute('aria-hidden','true');
  modeLayout={workspace,side,on};
  requestRollDraw();
  return true;
}
function commitModeLayout(){
  const change=modeLayout;modeLayout=null;
  if(!change?.workspace.isConnected)return;
  const {workspace,side,on}=change,target=on?'edit':'read';
  if(workspace.dataset.sideMode===target)return;
  const width=workspace.dataset.sideMode==='edit'?side.getBoundingClientRect().width:parseFloat(workspace.style.getPropertyValue('--mode-side-width'))||0;
  workspace.dataset.sideMode=target;
  AstraProjectEditor.dockRangePanel?.();
  if(on&&savedWorkingInspector?.key===AstraProjectEditor.workingViewKey())side.scrollTop=savedWorkingInspector.top;
  queueMicrotask(()=>{if(workspace.isConnected&&workspace.dataset.sideMode===target)playModeSidebar(workspace,side,on,{width});});
}

function files() {
  const report = state.project.report || {}, info = report.instruments || {};
  const noteStats = Object.entries(info).filter(([,v]) => Number.isInteger(v.notes));
  const stats = Object.entries(info).map(([key,v]) => Number.isInteger(v.notes) ? `${instruments[key] || key} ${v.notes} 个音符` : `${instruments[key] || key}音频分轨`);
  const warnings = Array.isArray(report.warnings) ? report.warnings.length : null;
  const counts = (key) => noteStats.every(([,v]) => Number.isInteger(v[key])) ? noteStats.reduce((sum,[,v]) => sum+v[key],0) : "—";
  const summary = [...stats, ...(warnings === null ? [] : [`警告 ${warnings}`]), ...(noteStats.length ? [`量化偏移 >80 ms：起点 ${counts("large_quantization_shifts")} / 终点 ${counts("large_quantization_end_shifts")}`] : [])].join(" · ");
  const groups = new Map();
  for (const file of resultFiles()) {
    const key = instruments[file.instrument] || "项目文件";
    if (!groups.has(key)) groups.set(key,[]);
    groups.get(key).push(file);
  }
  const advancedTools = `<details class="editor-advanced-tools"><summary>高级工具${state.project?.reviews?.length ? ` · ${state.project.reviews.length} 份审核意见` : ""}</summary><h3>外部审核意见</h3><p>仅在收到审核意见文件时使用，不影响日常编辑与导出。</p>${reviewActions()}</details>`;
  if (!groups.size) return `<p class="surface-note">此导出没有可打开的文件。</p>${advancedTools}`;
  const rows=Array.from(groups).map(([group, files]) => `<section class="file-group"><h3>${html(group)}</h3>${files.map(file => `<button class="file-row" type="button" data-do="openFile" data-path="${html(file.path)}" title="${html(file.name)}">${icon(fileKind(file) === "pdf" ? "score" : "file")}<span>${html(fileTitle(file))}</span><small class="truncate">${html(file.name)}</small>${icon("external")}</button>`).join("")}</section>`).join("");
  return `<div class="files-header"><details class="analysis-warnings" id="analysis-warnings"><summary>分析详情${warnings ? ` · ${warnings} 条建议复核` : ""}</summary><p class="file-summary">${html(summary)}</p>${warnings ? `<ul>${report.warnings.map(w => `<li>${html(typeof w === "string" ? w : JSON.stringify(w))}</li>`).join("")}</ul>` : ""}</details></div>${rows}${advancedTools}`;
}

let editorRenderFrame=0;
function scheduleEditorRender(){
  if(!editorRenderFrame)editorRenderFrame=requestAnimationFrame(()=>{editorRenderFrame=0;if(tab==='notes')render();});
}
let nativeTracksPanel, nativeTracksActive, nativeTracksMountRequest=0;
let nativeSeekVisual=null, waveSeekPanel=null, waveSeekRevision=0, retiredSeekGeneration=-1;
let nativePlaybackClock=null,nativeTrackMotion=null;
const NATIVE_PLAYBACK_INTERPOLATION_MS=50;

function nativeOwnsTransport(){
  return nativeTracksActive && $("transport").inert;
}

function compareTrackClocks(a,b){
  return (a.heard||0)-(b.heard||0) || (a.sampledAt||0)-(b.sampledAt||0);
}

function trackAudioClock(){
  const native=nativePlaybackClock, fallback=audioClock;
  if(tab!=='tracks'||transportMidi||!nativeOwnsTransport()||!native||
     native.route!==(state.player.cpp_route||0))return fallback;
  if(fallback.route===native.route&&compareTrackClocks(fallback,native)>0)return fallback;
  return native;
}

function trackInterpolationPosition(motion,now){
  if(!motion.duration)return motion.target;
  const fraction=Math.max(0,Math.min(1,(now-motion.start)/motion.duration));
  return motion.from+(motion.target-motion.from)*fraction;
}

function confirmedTrackPosition(clock,now){
  const previous=nativeTrackMotion;
  if(previous)now=Math.max(now,previous.lastTime);
  const reset=!previous||previous.route!==clock.route||previous.heard!==clock.heard||
    !clock.playing||!previous.playing||clock.position<previous.target;
  if(reset||clock.position!==previous.target){
    // Glide only towards a confirmed DAC sample. A paused sample or clock
    // discontinuity takes effect immediately at its exact position.
    const duration=reset?0:Math.min(NATIVE_PLAYBACK_INTERPOLATION_MS,
      Math.max(0,(clock.sampledAt-previous.sampledAt)*1000));
    nativeTrackMotion={route:clock.route,heard:clock.heard,playing:clock.playing,
      sampledAt:clock.sampledAt,from:duration?trackInterpolationPosition(previous,now):clock.position,
      target:clock.position,start:now,duration,lastTime:now};
  }else{
    previous.sampledAt=clock.sampledAt;
    previous.lastTime=now;
  }
  return trackInterpolationPosition(nativeTrackMotion,now);
}

function acceptNativePlaybackVisual(generation,route,position,sampledAt,sentAt,moving,rate,heard=0){
  const previous=nativePlaybackClock,currentRoute=state.player.cpp_route||0;
  if(!nativeTracksActive||generation<=retiredSeekGeneration||
     generation<(nativeSeekVisual?.generation??0)||(currentRoute>0&&route!==currentRoute)||
     previous&&(generation<previous.generation||
       generation===previous.generation&&route===previous.route&&
       compareTrackClocks({heard,sampledAt},previous)<0)||
     !Number.isFinite(position)||!Number.isFinite(sampledAt)||sampledAt<=0||
     !Number.isFinite(rate)||rate<=0)return;
  nativePlaybackClock={generation,route,position,sampledAt,heard,playing:Boolean(moving),
    rate,at:backendAnchor(sampledAt,sentAt)};
  if(tab!=='tracks'||transportMidi||route!==(state.player.cpp_route||0))return;
  if(seekPending)updatePlaybackPosition('player',false);
  const now=performance.now();
  if(!audioAnimation||!trackAudioClock().playing)updateAudioCursor(now);
  syncAudioAnimation(now);
}

function waveSeekActive(){
  const position=window.AstraWaveSeek?.position?.();
  return position!==null&&position!==undefined;
}

function nativeSeekConfirmed(view){
  const clock=nativePlaybackClock;
  const heard=clock?.route===view.route?Math.max(clock.heard,state.player.cpp_heard||0):state.player.cpp_heard;
  return view.sequence>0&&view.route===state.player.cpp_route&&heard>=view.sequence;
}

function acceptNativeSeekVisual(generation,gesture,phase,route,seconds,sequence){
  const previous=nativeSeekVisual;
  if(!nativeTracksActive||generation<=retiredSeekGeneration
      ||previous&&(generation<previous.generation||generation===previous.generation&&gesture<previous.gesture))return;
  const same=previous?.generation===generation&&previous.gesture===gesture;
  if(same&&phase>0&&(previous.phase>phase||previous.finished))return;
  if(same&&phase===0&&previous.phase===3&&previous.sequence>0&&!nativeSeekConfirmed(previous)
      &&previous.route===state.player.cpp_route)return;
  // A new native pointer gesture supersedes a pending web commit. A passive
  // packet for the earlier native gesture can't take over an active wave drag.
  const blocked=waveSeekActive()||(same&&previous.blocked);
  if(!same&&phase>0&&!blocked&&seekPending?.wave){
    seekPending=null;waveSeekPanel=null;
  }
  const finished=phase===0&&same&&(previous.finished||previous.phase>0);
  const next={generation,gesture,phase,route,seconds,sequence,blocked,finished};
  nativeSeekVisual=next;
  if(phase===3&&!sequence){
    next.until=performance.now()+160;
    setTimeout(()=>{
      if(nativeSeekVisual===next){next.phase=0;repaintTrackSeek();}
    },165);
  }
  repaintTrackSeek();
}

function nativeSeekPosition(now){
  const view=nativeSeekVisual;
  if(!view||!view.phase||view.blocked||!nativeTracksActive||tab!=='tracks'||transportMidi)return null;
  if(view.route&&state.player.cpp_route&&view.route!==state.player.cpp_route)return null;
  if(view.phase>=2&&(nativeSeekConfirmed(view)||(view.until&&now>=view.until)))return null;
  return view.seconds;
}

function repaintTrackSeek(){
  if(tab!=='tracks'||transportMidi)return;
  const position=updateAudioCursor();
  if(position!==undefined)paintPlaybackPosition(position);
}

function sendWaveSeekVisual(phase,seconds,sequence=0,route=nativeSeekVisual?.route??state.player.cpp_route??0){
  if(!nativeTracksActive||tab!=='tracks'||transportMidi||!nativeTracksPanel?.previewSeek)return;
  if(!waveSeekPanel&&nativeSeekVisual)waveSeekPanel={generation:nativeSeekVisual.generation,gesture:nativeSeekVisual.gesture};
  if(waveSeekPanel)nativeTracksPanel.previewSeek(waveSeekPanel.generation,waveSeekPanel.gesture,
    ++waveSeekRevision,phase,route,seconds,sequence);
}

function clearWaveSeekVisual(){
  if(waveSeekPanel)sendWaveSeekVisual(0,0);
  waveSeekPanel=null;
}

function beginWaveSeek(){
  // Invalidate callbacks for the preceding wave commit before sending any
  // command for this gesture, including a click with no pointermove.
  seekPending=null;
  if(nativeSeekVisual)nativeSeekVisual.blocked=true;
  clearWaveSeekVisual();
}

function finishWaveSeek(){
  if(!seekPending?.wave)clearWaveSeekVisual();
  if(deferredSeekRender)requestAnimationFrame(()=>render());
}
function syncNativeTracksPanel(active) {
  if (!nativeTracksPanel || active === nativeTracksActive) return;
  nativeTracksActive = active;
  if(!active){
    retiredSeekGeneration=Math.max(retiredSeekGeneration,nativeSeekVisual?.generation??-1,
      nativePlaybackClock?.generation??-1);
    nativeSeekVisual=null;waveSeekPanel=null;nativePlaybackClock=null;nativeTrackMotion=null;
  }
  const request=++nativeTracksMountRequest, footer=$("transport");
  // Keep the DOM slot in flow. Paint the input-inert web bar until the
  // native overlay appears, and reveal the next page underneath it on exit.
  if(active){
    footer.dataset.nativeSlot='true';
    footer.inert=true;
    footer.setAttribute('aria-hidden','true');
  }else{
    delete footer.dataset.nativeVisible;
  }
  nativeTracksPanel.setTracks(active,ok=>{
    if(request!==nativeTracksMountRequest)return;
    if(active&&ok){
      footer.dataset.nativeVisible='true';
    }else{
      if(active)console.error('Unable to mount the native transport slot');
      delete footer.dataset.nativeSlot;
      delete footer.dataset.nativeVisible;
      footer.inert=false;
      footer.removeAttribute('aria-hidden');
    }
  });
}

function render(force = false) {
  window.AstraRhythmContextMenu?.reconcile();
  if(!force && AstraProjectEditor.isDragging()){render.deferred=true;renderTransport();return;}
  render.deferred=false;
  let restoreInspectorTop=null;
  const editorActive=AstraProjectEditor.active(),ed=state.project?.editor;
  const workingKey=tab==='notes'?AstraProjectEditor.workingViewKey():null;
  const workingRoll=Boolean(workingKey),incrementalEditor=workingRoll&&editorActive;
  const trackPage=tab==='tracks';
  const noteMeter=incrementalEditor&&editorNoteChrome?.version===ed?.edit_stamp?.version&&editorNoteChrome
    ?AstraProjectEditor.noteChromeMeter?.():null;
  const chromeRevision=Number.isInteger(noteMeter)?[editorNoteChrome.base,noteMeter]:ed?.revision;
  const result = isResult();
  const audioSession=separationAudition() || result;
  const homePage=!state.input_midi&&!audioSession;
  if(pendingHomeOptions)currentHomeOptions();
  // Import pages have no editor chrome. The VST rack is only part of Notes.
  const editorDataKey=result?JSON.stringify([chromeRevision,ed?.dirty,ed?.score_stale,ed?.range,ed?.metronome,ed?.audition_together,tab==='notes'?(state.midi.vst_view_revision??state.midi.vst_instrument):null,ed?.can_undo,ed?.can_redo,workingRoll?null:state.midi.notes?.length,workingRoll?Boolean(state.midi.loading&&!(state.midi.duration>0)):null,AstraProjectEditor.noteEntryMode?.(),state.job.running,state.job.editor_export,Boolean(state.gpu_preflight)]):null;
  const jobKey=jobStructure(state.job);
  const keys = [tab==='notes'&&!workingRoll?editorActive:null,state.audio?.path,audioSession?state.audio?.duration:null,!audioSession||trackPage||workingRoll?null:state.audio?.peaks?.length,state.audio?.import_loading,state.audio?.import_error,result||homePage?null:state.audio?.requested_instruments,state.separation_workflow,state.input_midi?.token,state.input_midi?.loading,state.input_midi?.error,state.project?.path,state.project?.export,homePage?null:state.options,
    tab==="notes"?[jobKey.running,jobKey.phase,jobKey.error,jobKey.kind,jobKey.separation_only,jobKey.log_path,jobKey.cancelled_run]:jobKey,state.gpu_preflight,state.rhythm_candidate_running,state.recent,state.project?.reviews,workingRoll?null:editorDataKey,state.project?.editor_unavailable,state.project?.editor_recovery?.token,trackPage?state.project?.original_link:null,Boolean(ed),tab==='notes'&&!workingRoll?midiSelected:null,tab==='tracks'?null:activeInstrument,scoreZoom,audioSession?state.player.sources?.map((source) => [source.key,source.path,source.label,trackPage?null:source.duration,trackPage||workingRoll?null:source.peaks?.length]):null,tab,
    tab === "notes" ? [workingRoll?null:state.midi.path,workingRoll?null:state.midi.notes?.length,workingRoll?null:state.midi.duration,workingRoll?null:state.midi.loading] : null,
    tab === "score" ? (pdfSelected||state.pdf.path) : null];
  const key = JSON.stringify(keys);
  const remounted=force || key !== structureKey;
  let viewMotion=null;
  if (remounted) {
    const currentKey = transportMidi ? `midi:${state.midi.path}` : `audio:${state.player.sources?.find(source=>source.key===state.player.source)?.path || ""}`;
    if (!force && (seekDrag?.key === currentKey || tab==='tracks'&&waveSeekActive())) {
      deferredSeekRender = true;
      renderTransport();renderJobProgress();renderLogs();
      return;
    }
    deferredSeekRender = false;
    AstraProjectEditor.abortNoteGesture?.();
    finishLaneResize();
    AstraProjectEditor.abortSelectionGesture?.();
    window.AstraRhythmContextMenu?.close(false);
    structureKey = key;
    const active = document.activeElement;
    const openDetails = [...$("workspace").querySelectorAll(".rhythm-source[open],.score-extra[open],.ornament-disclosure[open],.editor-command-menu[open]")].map(details => details.dataset.disclosure ? `[data-disclosure="${details.dataset.disclosure}"]` : `.${details.className}`);
    const focus = $("workspace").contains(active) && active?.dataset.do ? [active.dataset.do,active.dataset.key,active.dataset.path] : null;
    const previousRoll = $("roll-scroll");
    const previousPaper=tab==="score"?$("workspace").querySelector(".paper-area"):null;
    const paperScroll=previousPaper?{key:previousPaper.dataset.scorePage,left:previousPaper.scrollLeft,top:previousPaper.scrollTop}:null;
    const modeTransition=Boolean(previousRoll&&workingKey&&render.workingKey===workingKey&&render.workingMode!==editorActive);
    const rollSurfaces=modeTransition?['roll-scroll','roll-time-scroll','keys-canvas','ruler-canvas','pedal-canvas','velocity-canvas','chord-canvas'].map($).filter(Boolean):[];
    render.workingKey=workingKey;render.workingMode=editorActive;
    if(previousRoll){
      rollViewportResize.unobserve(previousRoll);
      if(pitchView)savedRollViewport=rollViewport(previousRoll,pitchView.identity);
    }
    window.AstraRhythmTimeline?.invalidateBeatView?.();
    $("breadcrumb").textContent = result ? state.project.name : state.input_midi ? "直接 MIDI 制谱" : separationAudition() ? "分轨" : "新建转谱";
    invalidateAudioVisuals();
    lastRollLayers=lastKeyboard=timeGridCache=null;
    rollGeometry=rollRaster=null;pendingRollZoom=null;
    const trace=window.AstraRenderStage,started=trace?performance.now():0;
    const markup=state.input_midi ? midiInput() : audioSession ? session() : home();
    const mount=trace?performance.now():0;
    trace?.('workspace.markup',mount-started);
    // A same-page remount (mode toggle, preview rebuild) keeps the page offset.
    const page=$("workspace"),pageKey=`${tab}\n${state.separation_workflow?.project||state.project?.path||''}`,pageTop=render.pageKey===pageKey?page.scrollTop:null;
    viewMotion=pendingViewMotion?.toTab===tab?pendingViewMotion
      :render.pageKey===pageKey?carryViewMotion(page,tab):null;
    render.pageKey=pageKey;
    pendingViewMotion=null;
    clearViewMotion();
    page.innerHTML=markup;
    hydrateScorePreview();
    const paper=paperScroll?page.querySelector(".paper-area"):null;
    if(paper && paper.dataset.scorePage===paperScroll.key){paper.scrollLeft=paperScroll.left;paper.scrollTop=paperScroll.top;}
    // Same working document, different permission chrome. Carry its painted
    // surfaces through the mount before repainting at the new layout anchor.
    for(const prior of rollSurfaces)$(prior.id)?.replaceWith(prior);
    if(pageTop!==null&&page.scrollTop!==pageTop)page.scrollTop=pageTop;
    trace?.('workspace.mount',performance.now()-mount);
    document.body.classList.toggle("project-open",result);
    // Restored menus are placed again but do not replay their entrance.
    for (const selector of openDetails) { const details=$("workspace").querySelector(selector); if(details&&!details.open) {details.dataset.popRestore="";details.open=true;} }
    midiSourceMenu.sync();
    trackPaintKey = "";
    document.querySelectorAll(".rail-button[data-do='new'],.rail-button[data-do='chooseProject']").forEach((b) => {
      b.disabled = Boolean(state.job.running || state.gpu_preflight);
      b.classList.toggle("active", b.dataset.do === (result ? "chooseProject" : "new"));
    });
    if (state.gpu_preflight) {
      $("workspace").querySelectorAll('[data-do="run"],[data-do="runMidi"],[data-do="reexport"],[data-do="chooseAudio"],[data-do="chooseMidi"],[data-do="advanced"],[data-do="tempo"],[data-do="tempoAdvanced"]').forEach(control=>{control.disabled=true;});
    }
    if (focus) [...$("workspace").querySelectorAll("button[data-do]")].find((b) => b.dataset.do === focus[0] && b.dataset.key === focus[1] && b.dataset.path === focus[2])?.focus({preventScroll:true});
    else if(rollSurfaces.includes(active)&&active.isConnected)active.focus({preventScroll:true});
    if (tab === "notes" && $("midi-canvas")) {
      AstraNoteInspector.sync(state.midi.path);
      const scroll = $("roll-scroll");
      rollViewportResize.observe(scroll);
      const identity=state.project?.editor? `${state.project.path}:notes` : state.midi.path;
      pendingRollScroll = savedRollViewport?.identity===identity ? {scroll,...savedRollViewport} : {scroll,left:0,top:0};
      if(scroll!==previousRoll){
        attachRollTimeScroll(scroll);
        scroll.addEventListener("scroll",event=>{if(manualRollNavigation?.scroll===scroll)finishManualRollNavigation(event);syncRollTimeScroll(scroll,false);requestRollDraw(event);});
        // Native scrollbars target their container. A press alone is not navigation.
        scroll.addEventListener("pointerdown",event=>{if(event.button===0&&event.target===scroll)manualRollNavigation={scroll,pointer:event.pointerId,left:scroll.scrollLeft,top:scroll.scrollTop};});
      }
      requestRollDraw();
    }
    if(modeTransition&&editorActive&&savedWorkingInspector?.key===workingKey)
      restoreInspectorTop=savedWorkingInspector.top;
  }
  if(homePage)syncHomeConfig();
  if(trackPage)syncTrackPreviews(remounted);
  else syncTrackPreviews.last=null;
  if(tab==="score")syncScorePreview();
  else scorePreviewRendered=null;
  if(workingKey&&render.workingKey===workingKey&&render.workingMode!==editorActive&&patchWorkingMode(editorActive))render.workingMode=editorActive;
  if(!workingRoll){modeViewport=null;modeProjection=null;}
  if(editorDataKey!==render.editorDataKey){render.editorDataKey=editorDataKey;if(workingRoll)AstraProjectEditor.refreshData?.(false);}
  if(workingRoll){
    // Waveform and decoder completions change paint data, not the working roll's chrome.
    const waveKey=JSON.stringify([state.audio?.path,state.audio?.peaks?.length,state.player.sources_version,
      state.player.sources?.map(source=>[source.key,source.path,source.duration,source.peaks?.length])]);
    if(render.editorWaveKey!==waveKey){render.editorWaveKey=waveKey;invalidateRollInputs();}
    syncWorkingMidiChoice();if(editorActive)AstraProjectEditor.updateNoteCount?.();
  }else render.editorWaveKey=null;
  AstraProjectEditor.paintNoteEntryStatus?.();
  paintSaveStatus();
  renderTransport();
  renderJobProgress();
  renderLogs();
  syncVstSelector();
  AstraProjectEditor.dockRangePanel?.();
  if(restoreInspectorTop!==null){
    const side=document.querySelector('.editor-side');
    if(side)side.scrollTop=restoreInspectorTop;
  }
  if(viewMotion)runViewTransition($("workspace"),viewMotion);
}

function syncVstSelector(){
  const slot=$('editor-vst-slot'),project=state.project?.path,generation=state.project?.editor?.edit_stamp?.generation;
  const enabled=Boolean(slot&&project&&generation!=null&&typeof bridge?.openVstSettings==='function');
  if(slot)slot.hidden=!enabled;
  window.AstraVstSelector?.sync(enabled?slot:null,state.midi?.vst_instrument,enabled?{project,generation,revision:state.midi.vst_view_revision}:null,
    (action,key,insert)=>{
      // Insert actions add only their declared scalar fields; never module paths or plugin state.
      const message={project,generation,action,key};
      if(insert)for(const name of ['slot_id','enabled','to_index'])if(Object.hasOwn(insert,name))message[name]=insert[name];
        if(insert&&['panel','panel_close','insert_editor','save_default_patch'].includes(action))for(const name of ['editor_route','rack_revision'])if(insert[name]!=null)message[name]=insert[name];
      return bridge?.openVstSettings?.(JSON.stringify(message));
    });
  // Mounted/gain updates can arrive as passive MIDI view packets, without transport chrome work.
  syncInstrumentFader(transportMidi);
}

function syncWorkingMidiChoice(){
  const select=$('midi-select'),prior=select?.dataset.workingPath,current=AstraProjectEditor.path?.();
  if(!prior||!current||prior===current||!AstraProjectEditor.workingView()||select.value!==prior)return;
  // An automatic preview swap is not a user selection and must not remount
  // the toolbar or fire another selectMidi. Keep the working choice current.
  select.value=current;select.dataset.workingPath=current;
  const open=select.parentElement.querySelector('[data-do="openFile"]');if(open?.dataset.path===prior)open.dataset.path=current;
  midiSourceMenu.sync();
}

// This is the VST instrument audio bus, not MIDI velocities, CC7 or the master.
const FADER_MIN_DB=-60,FADER_MAX_DB=6,FADER_MAX_GAIN=1.9952623149688795,FADER_UPDATE_MS=34;
let faderEdit=null,faderTimer=0;
function instrumentFaderMarkup(){
  return `<section id="editor-instrument-fader" class="editor-instrument-fader" hidden>
    <div class="instrument-fader-heading"><label for="instrument-gain-db">乐器音量</label><output id="instrument-gain-value" for="instrument-gain-db" aria-live="off">0.0 dB</output></div>
    <input id="instrument-gain-db" type="range" min="${FADER_MIN_DB}" max="${FADER_MAX_DB}" step="0.5" value="0" aria-valuetext="0.0 dB" title="只调整 VST 乐器，不改原音频、节拍器或总音量">
    <span class="instrument-fader-scale" aria-hidden="true"><span>−∞</span><span>0</span><span>+6</span></span>
  </section>`;
}
function getFaderScope(){
  const doc=state.project?.editor,model=state.midi.vst_instrument;
  if(!['notes','tracks'].includes(tab)||typeof bridge?.setVstInstrumentGain!=='function'||
     !model?.mounted||!model.key||!doc?.edit_stamp||!Number.isFinite(state.midi.vst_midi_gain))return null;
  return {owner:bridge,project:state.project.path,generation:doc.edit_stamp.generation,key:model.key};
}
const sameFaderScope=(a,b)=>Boolean(a&&b&&a.owner===b.owner&&a.project===b.project&&a.generation===b.generation&&a.key===b.key);
const faderGain=db=>db<=FADER_MIN_DB?0:Math.min(FADER_MAX_GAIN,10**(db/20));
const faderDb=gain=>gain<=0?FADER_MIN_DB:Math.min(FADER_MAX_DB,Math.max(FADER_MIN_DB+.5,20*Math.log10(gain)));
const faderText=db=>db<=FADER_MIN_DB?'−∞ dB':`${db>0?'+':''}${(Math.abs(db)<.05?0:db).toFixed(1)} dB`;
function paintFader(input,db){
  const output=$('instrument-gain-value'),text=faderText(db);
  if(output&&output.textContent!==text)output.textContent=text;
  if(input.getAttribute('aria-valuetext')!==text)input.setAttribute('aria-valuetext',text);
}
function clearFaderTimer(){if(faderTimer){clearTimeout(faderTimer);faderTimer=0;}}
function clearFaderEdit(){clearFaderTimer();faderEdit=null;}
const instrumentFaderHomes=new WeakMap();
function syncInstrumentFader(useMidi){
  const footer=$('volume-popup'),pop=footer?.querySelector('.volume-popup-panel'),percent=$('volume-percent');
  const slot=$('editor-vst-slot'),side=slot?.closest('#editor-left-zone'),scope=getFaderScope();
  const accessible=Boolean(scope&&slot&&!slot.hidden&&side&&!side.inert&&AstraProjectEditor.active()&&
    !AstraProjectEditor.inspectorCollapsed()&&!AstraProjectEditor.inspectorCovered());
  const fallback=Boolean(useMidi&&scope&&!accessible&&pop),inPopup=pop?.querySelector('.editor-instrument-fader');
  const inSlot=side?.querySelector('.editor-instrument-fader');
  if(fallback&&!inPopup){
    if(inSlot){instrumentFaderHomes.set(inSlot,inSlot.parentNode);pop.append(inSlot);}else pop.insertAdjacentHTML('beforeend',instrumentFaderMarkup());
  }else if(fallback&&inPopup&&inSlot){inSlot.remove();
  }else if(!fallback&&inPopup){
    const home=instrumentFaderHomes.get(inPopup);
    if(slot&&!inSlot&&AstraProjectEditor.active())(home?.isConnected&&side.contains(home)?home:slot).append(inPopup);
    else inPopup.remove();
  }
  if(percent)percent.hidden=fallback;
  const panel=$('editor-instrument-fader'),input=$('instrument-gain-db'),shown=Boolean(scope);
  if(faderEdit&&(faderEdit.input!==input||!sameFaderScope(faderEdit.scope,scope)))clearFaderEdit();
  if(panel&&panel.hidden===shown){
    if(!shown&&panel.contains(document.activeElement))$('editor-vst-slot')?.querySelector('button')?.focus({preventScroll:true});
    panel.hidden=!shown;
  }
  if(shown&&input){
    // Do not move a thumb the user is dragging, including during editor publications.
    if(!faderEdit){
      const db=faderDb(state.midi.vst_midi_gain);
      if(Math.abs(Number(input.value)-db)>.24)input.value=String(db);
      paintFader(input,db);
    }else paintFader(input,faderEdit.db);
  }
  // The original/RAW footer control stays available when auditioning PCM.
  // Non-VST, read-only and Tracks retain their own legacy volume behavior.
  const hide=accessible&&Boolean(useMidi);
  if(footer&&footer.hidden!==hide){
    const hadFocus=hide&&footer.contains(document.activeElement);
    if(hide)footer.open=false;
    footer.hidden=hide;
    if(hadFocus)input?.focus({preventScroll:true});
  }
  updateVolumeDisplay();
}
function sendInstrumentFader(edit){
  clearFaderTimer();
  if(faderEdit!==edit||!edit.input.isConnected||!sameFaderScope(edit.scope,getFaderScope()))return;
  const gain=faderGain(edit.db);
  if(edit.sent?.gain===gain){
    if(edit.committing&&edit.sent.done){clearFaderEdit();syncInstrumentFader(transportMidi);}
    return;
  }
  const sent=edit.sent={gain,done:false};edit.sentAt=performance.now();
  const {owner,...target}=edit.scope;
  const receive=value=>{
    if(faderEdit!==edit||edit.sent!==sent||!sameFaderScope(edit.scope,getFaderScope()))return;
    sent.done=true;
    try{
      const result=JSON.parse(value);
      if(result.ok&&Number.isFinite(result.gain))state.midi.vst_midi_gain=result.gain;
      else if(result.error&&$('announcement'))$('announcement').textContent=result.error;
    }catch{ /* The ordered bridge state remains authoritative. */ }
    if(edit.committing&&faderGain(edit.db)===sent.gain){clearFaderEdit();syncInstrumentFader(transportMidi);}
  };
  try{owner.setVstInstrumentGain(JSON.stringify({...target,gain}),receive);}
  catch{receive('');}
}
function editInstrumentFader(input,commit){
  const scope=getFaderScope(),db=Number(input.value);
  if(!scope||!Number.isFinite(db)||db<FADER_MIN_DB||db>FADER_MAX_DB)return;
  if(!faderEdit||faderEdit.input!==input||!sameFaderScope(faderEdit.scope,scope)){
    clearFaderEdit();faderEdit={scope,input,sentAt:-Infinity,sent:null};
  }
  const edit=faderEdit;edit.db=db;edit.committing=commit;
  paintFader(input,db);
  if(commit){sendInstrumentFader(edit);return;}
  // First sample immediately, then at most one unsent latest value. Release
  // applies its final absolute value without waiting for a stale drag step.
  const delay=FADER_UPDATE_MS-(performance.now()-edit.sentAt);
  if(delay<=0)sendInstrumentFader(edit);
  else if(!faderTimer)faderTimer=setTimeout(()=>sendInstrumentFader(edit),delay);
}
document.addEventListener('pointercancel',event=>{
  if(event.target===faderEdit?.input&&!faderEdit.committing){clearFaderEdit();syncInstrumentFader(transportMidi);}
},true);

function updateVolumeDisplay(){
  const value=Math.max(0,Math.min(100,Number($("volume")?.value ?? 65)));
  const instrument=Boolean($('volume-percent')?.hidden),gain=$('instrument-gain-db');
  const summary=$("volume-summary"),label=instrument?`乐器音量 ${gain?.getAttribute('aria-valuetext')||''}`.trim():value===0?'已静音':`音量 ${value}%`;
  if(summary){
    const title=`${label} · 点击调整`,aria=`${label}，展开音量`;
    if(summary.title!==title)summary.title=title;
    if(summary.getAttribute('aria-label')!==aria)summary.setAttribute('aria-label',aria);
    summary.classList.toggle('is-muted',instrument?Number(gain?.value)<=FADER_MIN_DB:value===0);
  }
  const output=$("volume-value"),text=`${value}%`;if(output&&output.textContent!==text)output.textContent=text;
}
// The bridge owns queued play/seek for the current preview revision. Do not
// retain and replay a toggle here: a later pause, stop or source change can cancel it.
function midiPreviewPreparing(player=state.midi){
  return Boolean(player.path&&(player.preparing||(player.loading&&typeof player.play_pending==='boolean')));
}
function syncTransportVisibility() {
  const footer=$("transport"),roll=$("roll-scroll");
  // A Notes layout preference must not remove the player on pages with no View menu.
  const folded=tab==='notes'&&Boolean(roll)&&transportFolded();
  const wasFolded=footer.dataset.viewHidden==='true';
  const unavailable=!transportAvailable,hidden=unavailable||folded;
  const changed=hidden!==(footer.hidden||wasFolded);
  const restoreFocus=changed&&hidden&&footer.contains(document.activeElement);
  // Keep the legacy availability contract for the panel. The independent view
  // attribute removes its entire box, not just the controls inside it.
  if(footer.hidden!==unavailable)footer.hidden=unavailable;
  if(wasFolded!==folded)footer.dataset.viewHidden=String(folded);
  if(nativeTracksPanel)syncNativeTracksPanel(!hidden&&tab==='tracks'&&!transportMidi&&!document.querySelector('dialog[open]'));
  const zone=$('workspace').querySelector('.view-zone-transport');
  if(changed&&roll){AstraProjectEditor.dockRangePanel?.();requestRollDraw();}
  syncEditorViewControls('editorTransportFold');
  if(restoreFocus)(folded&&zone&&!zone.disabled?zone:roll||$('workspace')).focus({preventScroll:true});
}
function renderTransport() {
  const currentAuditionProject=state.separation_workflow?.project || state.project?.path;
  if(auditionProject!==currentAuditionProject){auditionProject=currentAuditionProject;abPrevious=null;auditionSource=state.player.audition==="midi"?"midi":state.player.source||"midi";}
  const midiCapable = isResult() && ["notes","tracks"].includes(tab) && Boolean(state.midi.path||tab==="notes"&&AstraProjectEditor.workingView()) && Boolean(instrumentFile("midi",activeInstrument) || (activeInstrument==="piano" && AstraProjectEditor.path()))
    && (!pendingScoreAudition || pendingScoreAudition.path === state.midi.path);
  const sources = sourceList();
  if(auditionSource!=="midi"&&!sources.some(source=>source.key===auditionSource))auditionSource="midi";
  const vstEpoch=state.midi.vst_epoch||null;
  if(vstEpoch&&vstEpoch!==renderTransport.vstEpoch&&state.player.audition==='midi')auditionSource='midi';
  renderTransport.vstEpoch=vstEpoch;
  const useMidi = midiCapable && auditionSource==="midi";
  const player = useMidi ? state.midi : state.player;
  transportMidi = useMidi;
  AstraProjectEditor.syncVstFeedback?.();
  const vstVolume=Boolean(useMidi&&vstEpoch);
  $('volume').disabled=vstVolume;
  $('volume').title=vstVolume?'VST 音量请在乐器面板调节':'';
  transportAvailable = !(tab==='files'&&state.project?.path) && (midiCapable || sources.length > 0);
  window.AstraTransportPanel?.render();
  const selected = sources.find(source => source.key === player.source);
  const nextTransportKey = useMidi ? `midi:${player.path||AstraProjectEditor.path()}` : `audio:${selected?.path || ""}`;
  if (nextTransportKey !== transportKey) {
    transportKey = nextTransportKey;
    seekDrag = null;
    if(seekPending?.targetKey===nextTransportKey){seekPending.key=nextTransportKey;delete seekPending.targetKey;}
    else {if(seekPending?.wave)clearWaveSeekVisual();seekPending=null;}
  }
  syncTransportVisibility();
  const selector = $("source");
  const options=[...(midiCapable?[{label:'当前显示 MIDI',key:'midi'}]:[]),...sources];
  const key=JSON.stringify(options.map(source=>[source.key,source.label]));
  if(key!==trackSourceKey){trackSourceKey=key;selector.replaceChildren(...options.map(source=>new Option(source.label,source.key)));}
  if(selector.disabled!==(options.length<2))selector.disabled=options.length<2;
  const selectedSource=selectedAudioSource();
  const selectorSource=useMidi?'midi':selectedSource;
  if(selector.value!==String(selectorSource))selector.value=selectorSource;
  const sourceTitle=tab==='tracks'?'原位切换原曲与分轨':'切换试听音源（卷帘不变）';
  if(selector.title!==sourceTitle)selector.title=sourceTitle;
  const preparing=useMidi&&midiPreviewPreparing(player),playPending=useMidi&&!player.playing&&Boolean(player.play_pending);
  const playing=Boolean(player.playing),pauseIntent=playing||playPending;
  const playButton=$("play"),playIcon=playButton.firstElementChild.firstElementChild,playHref=`#i-${pauseIntent?'pause':'play'}`;
  if(playIcon.getAttribute('href')!==playHref)playIcon.setAttribute('href',playHref);
  const playLabel=playPending?'取消待播放':playing?'暂停':preparing?'准备就绪后播放':'播放';
  if(playButton.getAttribute('aria-label')!==playLabel)playButton.setAttribute('aria-label',playLabel);
  const playTitle=`${playLabel} · ${pauseIntent?'Space':'Space / Enter'}`;
  if(playButton.title!==playTitle)playButton.title=playTitle;
  const playDisabled=useMidi?!(player.available||preparing||pauseIntent):!sources.length;
  if(playButton.disabled!==playDisabled)playButton.disabled=playDisabled;
  const seek = $("seek");
  const seekDisabled=!(player.duration > 0);
  if(seek.disabled!==seekDisabled)seek.disabled=seekDisabled;
  const duration = Math.max(0,player.duration || 0);
  if (!seekDrag && Number(seek.max) !== duration) seek.max = duration;
  const durationText = time(duration);
  if ($("duration").textContent !== durationText) $("duration").textContent = durationText;
  if (!volumeAdjust) {const volumeValue=useMidi ? midiVolume : player.volume ?? 65;if($("volume").value!==String(volumeValue))$("volume").value=volumeValue;}
  syncInstrumentFader(useMidi);
  const playback = state.player;
  const nextPaintKey = `${selectedSource}|${playback.source}|${Boolean(playback.playing)}|${playback.duration > 0}`;
  if (trackPaintKey !== nextPaintKey) {
    trackPaintKey = nextPaintKey;
    for (const lane of document.querySelectorAll(".track-name")) lane.setAttribute("aria-pressed",String(lane.dataset.key === selectedSource));
    for (const control of document.querySelectorAll('[data-do="trackPlay"]')) {
      const on = playback.source === control.dataset.key && playback.playing;
      control.querySelector('use')?.setAttribute('href', `#i-${on ? "pause" : "play"}`);
      control.setAttribute('aria-label', `${on ? "暂停" : "播放"}${instruments[control.dataset.key] || "原曲"}`);
      control.setAttribute('aria-pressed',String(on));
    }
    let row=null;
    for(const lane of document.querySelectorAll('.track-row')){
      const chosen=lane.dataset.track===selectedSource;lane.classList.toggle('is-selected',chosen);if(chosen)row=lane;
    }
    if(row&&selectedTrackRow&&row!==selectedTrackRow&&row.parentElement===selectedTrackRow.parentElement)window.AstraRhythmMotion?.accentTrack(row);
    selectedTrackRow=row;
    if(tab==='tracks')updateTrackAB(selectedSource);
    for (const lane of document.querySelectorAll('.channel-row')) lane.classList.toggle('is-playing',lane.dataset.channel === playback.source && playback.playing);
    audioCursor=undefined;
    for (const clip of document.querySelectorAll(".clip")) {
      const cursor = clip.querySelector(".playhead");
      cursor.hidden = !(playback.duration > 0);
      if (!cursor.hidden) audioCursor=cursor;
    }
  }
  updatePlaybackPosition(useMidi ? "midi" : "player");
  updateAudioClock();
  updateMidiClock();
  updateAudioCursor();
  syncAudioAnimation();
  updateMidiCursor();
  updateFollowControl();
  if (tab==="notes" && player.playing && !midiAnimation) midiAnimation=requestAnimationFrame(animateMidiCursor);
  if ((tab!=="notes" || !player.playing) && midiAnimation) {cancelAnimationFrame(midiAnimation);midiAnimation=0;}
  if ($("midi-message")) {
    const notationClock = isScoreMidi(state.midi.path) ? " · 按记谱节奏，时间可能与原曲不同" : "";
    const model=AstraProjectEditor.model(),midi=state.midi;
    const previewStatus=midi.error?` · ${midi.error}`:midi.vst_epoch?(midi.message?` · ${midi.message}`:''):midi.play_pending&&!midi.playing?'':midi.loading||midi.preparing?'':midi.available===false?` · ${midi.message||'试听不可用'}`:'';
    const message=midi.path?`${model.notes?.length||0} 音符 · ${model.pedal_events?.some(e=>e.value>=64)?'含演奏踏板 CC64':'无延音踏板；听原演奏请选择原始 MIDI'}${notationClock}${previewStatus}`:'正在读取 MIDI';
    if ($("midi-message").textContent !== message) $("midi-message").textContent=message;
  }
}

function updateAudioClock() {
  const playback = state.player;
  const position=playback.position || 0, rate=playback.rate || state.project?.editor?.audition_rate || 1;
  const moving=Boolean(playback.playhead_moving ?? playback.playing);
  const sampledAt=Number(playback.sampled_at)||0,route=playback.cpp_route||0,heard=playback.cpp_heard||0;
  if (audioClock.rate!==rate || audioClock.source!==playback.source || audioClock.position!==position ||
      audioClock.playing!==moving || audioClock.sampledAt!==sampledAt || audioClock.route!==route ||
      audioClock.heard!==heard) {
    audioClock={source:playback.source,position,playing:moving,rate,sampledAt,route,heard,
      at:backendAnchor(playback.sampled_at,playback.transport_order)};
  }
}

// Frame-local visual work only: never changes audio commands or transport state.
let audioVisualCache=null,audioVisualFrame=0;
const audioVisualResize=typeof ResizeObserver==='function'?new ResizeObserver(entries=>{
  const cache=audioVisualCache;
  if(!cache)return;
  let changed=false;
  for(const entry of entries){
    const row=cache.rows.find(item=>item.clip===entry.target);
    const width=entry.contentRect.width;
    if(row&&row.clip.isConnected&&row.width!==width){row.width=width;changed=true;}
  }
  // Resize notifications also repaint a paused playhead. Use delivered sizes
  // so updating a clock or receiving a waveform never forces synchronous layout.
  if(changed&&!audioVisualFrame)audioVisualFrame=requestAnimationFrame(()=>{
    audioVisualFrame=0;
    if(audioVisualCache===cache)updateAudioCursor();
  });
}):null;
function invalidateAudioVisuals(){
  audioVisualResize?.disconnect();
  if(audioVisualFrame)cancelAnimationFrame(audioVisualFrame);
  audioVisualFrame=0;audioVisualCache=null;
}
function audioVisuals(){
  if(!audioVisualCache||audioVisualCache.cursor!==audioCursor||!audioCursor?.isConnected){
    invalidateAudioVisuals();
    const rows=[...document.querySelectorAll('.clip .playhead')].map(cursor=>{
      const clip=cursor.parentElement,progress=clip.querySelector('.wave-progress');
      if(progress){progress.style.width='100%';progress.style.transformOrigin='left center';}
      return {cursor,clip,progress,width:null};
    });
    audioVisualCache={cursor:audioCursor,rows};
    for(const row of rows)audioVisualResize?.observe(row.clip);
  }
  return audioVisualCache.rows;
}

function transportDisplayPosition(now=performance.now()){
  if(transportMidi){nativeTrackMotion=null;return midiDisplayPosition(now);}
  const held=localSeekPosition(now);
  if(held!=null){nativeTrackMotion=null;return held;}
  const clock=trackAudioClock();
  if(tab==='tracks'&&nativeOwnsTransport())return confirmedTrackPosition(clock,now);
  nativeTrackMotion=null;
  return clock.position+clockElapsed(clock,now,'player')*(clock.rate||1);
}
function updateAudioCursor(now=performance.now()) {
  if (!audioCursor) return;
  const position=transportDisplayPosition(now);
  paintAudioVisualPosition(position);
  return position;
}

function paintAudioVisualPosition(position,reveal=false){
  const duration=state.player.duration || 1;
  for(const {cursor,progress,width} of audioVisuals()){
    if(reveal)cursor.hidden=false;
    const ratio=Math.max(0,Math.min(1,position/(Number(cursor.dataset.duration)||duration)));
    // Before the first observer delivery (or without ResizeObserver),
    // percentage positioning follows the padding-free clip automatically.
    const left=width>0?'0px':(ratio*100)+'%';
    const head=width>0?'translateX('+(width*ratio)+'px)':'none',fill='scaleX('+ratio+')';
    if(cursor.style.left!==left)cursor.style.left=left;
    if(cursor.style.transform!==head)cursor.style.transform=head;
    if(progress&&progress.style.transform!==fill)progress.style.transform=fill;
  }
}

let lastAudioFrame=0;
function syncAudioAnimation(now=performance.now()){
  const motion=nativeTrackMotion;
  const moving=transportMidi?state.midi.playing:tab==='tracks'&&nativeOwnsTransport()
    ? Boolean(motion?.playing&&motion.from!==motion.target&&now<motion.start+motion.duration)
    : trackAudioClock().playing;
  if(audioCursor?.isConnected&&moving){
    if(!audioAnimation)audioAnimation=requestAnimationFrame(animateAudioCursor);
  }else{
    if(audioAnimation)cancelAnimationFrame(audioAnimation);
    audioAnimation=0;lastAudioFrame=0;
  }
}

function animateAudioCursor(now) {
  audioAnimation=0;
  if(!audioCursor?.isConnected){lastAudioFrame=0;return;}
  const audioTrace=Boolean(state.player?.trace),audioPaintStart=audioTrace?performance.now():0;
  const paintAt=performance.now();
  const shown=updateAudioCursor(paintAt);
  if(shown!==undefined)paintPlaybackPosition(shown);
  if(audioTrace){tracePlayback('audio_disp',{dt:lastAudioFrame?Math.round((now-lastAudioFrame)*100)/100:null,paint_ms:Math.round((performance.now()-audioPaintStart)*1000)/1000});lastAudioFrame=now;}
  syncAudioAnimation(paintAt);
}

function updateMidiClock() {
  const position=state.midi.position || 0, playing=Boolean(state.midi.playing), rate=state.midi.rate || state.project?.editor?.audition_rate || 1;
  // The backend owns playback. Interpolate only between its position reports;
  // scroll/resize renders must not reset this clock or wait for another poll.
  if (midiClock.rate!==rate || midiClock.path!==state.midi.path || midiClock.position!==position || midiClock.playing!==playing) {
    midiClock={path:state.midi.path,position,playing,rate,at:backendAnchor(state.midi.sampled_at,state.midi.transport_order)};
  }
}
function midiDisplayPosition(now=performance.now()) {
  const dragged=window.AstraProjectEditor?.seekPosition?.();if(dragged!==null&&dragged!==undefined)return dragged;
  if(tab==='notes'&&!transportMidi){const preview=localSeekPosition(now);if(preview!==null)return preview;const elapsed=clockElapsed(audioClock,now,'player');return audioClock.position+elapsed*(audioClock.rate||1);}
  if (transportMidi) {
    const preview=localSeekPosition(now);
    if (preview !== null) return preview;
  }
  const elapsed=clockElapsed(midiClock,now,'midi');
  return Math.min(state.midi.duration || Infinity,midiClock.position+elapsed*(midiClock.rate||1));
}
let rollHeadGeometry=null;
function paintMidiPlayhead(seconds,worldX) {
  const cursor=$("midi-cursor"),scroll=$("roll-scroll");
  if(!cursor||!scroll)return;
  const position=worldX??midiX(seconds),left=scroll.scrollLeft;
  const head=$("ruler-playhead"),waveCursor=$("waveform-cursor");
  // Measure the scroll-independent origin on layout changes, not after the
  // previous animation frame's style writes. Scroll itself is a translation.
  if(head&&(rollHeadGeometry?.cursor!==cursor||rollHeadGeometry.head!==head||rollHeadGeometry.scroll!==scroll)){
    const line=cursor.getBoundingClientRect(),parent=head.parentElement.getBoundingClientRect();
    const previous=Number.parseFloat(cursor.style.transform.slice('translateX('.length))||0;
    rollHeadGeometry={cursor,head,scroll,origin:line.left-previous+left+line.width/2-parent.left-head.offsetWidth/2};
  }
  const place=(el,x)=>{const value=`translateX(${x}px)`;if(el&&el.style.transform!==value)el.style.transform=value;};
  place(cursor,position);
  place(waveCursor,position-left);
  if(head){
    if(head.style.left!=='0px')head.style.left='0px';
    place(head,rollHeadGeometry.origin+position-left);
    const value=String(seconds);if(head.getAttribute('aria-valuenow')!==value)head.setAttribute('aria-valuenow',value);
  }
  window.AstraTransportPanel?.position(seconds);
}
function updateMidiCursor(now=performance.now()) {
  const cursor=$("midi-cursor"),scroll=$("roll-scroll");
  if (cursor && scroll) {
    const seconds=midiDisplayPosition(now),position=midiX(seconds);
    const moved = Math.abs(seconds - lastMidiPosition) > .002;
    const playing=rollIsPlaying();
    if (!playing) resumeRollFollow();
    if (!seekDrag && (!manualRollNavigation||manualRollNavigation.released) && followMode!=='off' && !followSuspended && playing && (followMode==='center'||moved)) {
      if (followRollViewport(scroll,position)) manualRollNavigation=null;
    }
    paintMidiPlayhead(seconds,position);
    lastMidiPosition = seconds;
    return seconds;
  }
}
function finishManualRollNavigation(event) {
  const drag=manualRollNavigation;if(!drag||event?.pointerId!==undefined&&event.pointerId!==drag.pointer)return;
  const moved=drag.scroll.scrollLeft!==drag.left||drag.scroll.scrollTop!==drag.top;
  if(moved)suspendRollFollow(false);
  // Chromium can deliver native scrollbar scroll after pointerup. Keep a released
  // candidate until its scroll, fresh input or an explicit programmatic scroll.
  if(event?.type==='pointerup')drag.released=true;
  if(moved||event?.type==='pointercancel'||event?.type==='blur')manualRollNavigation=null;
}
function rollIsPlaying(){return Boolean(transportMidi?state.midi?.playing:state.player?.playing);}
function followRollViewport(scroll,position) {
  if(!scroll||!Number.isFinite(position)||!scroll.clientWidth||followMode==='off')return false;
  const width=scroll.clientWidth,left=scroll.scrollLeft;
  if(followMode==='page'&&position>=left&&position<=left+width-25)return false;
  const next=Math.min(Math.max(0,scroll.scrollWidth-width),Math.max(0,position-width*(followMode==='center'?.5:.2)));
  if(Math.abs(left-next)<1)return false;
  scroll.scrollLeft=next;
  return true;
}
function suspendRollFollow(edit=true){
  if(followMode==='off'||followSuspended||!rollIsPlaying()||edit&&!suspendFollowOnEdit)return;
  followSuspended=true;updateFollowControl();
}
function resumeRollFollow(){
  if(followSuspended){followSuspended=false;updateFollowControl();}
}
const followLabels={off:'关闭',page:'翻页',center:'居中'};
function followMenu(){
  const rows=Object.entries(followLabels).map(([key,label])=>`<button type="button" class="quiet" role="menuitemradio" aria-checked="false" data-do="followMode" data-key="${key}"><span>${label}</span><span class="follow-mark" aria-hidden="true"></span></button>`).join('');
  return `<details class="editor-command-menu follow-menu" data-disclosure="roll-follow-menu"><summary class="quiet follow-button" aria-label="跟随播放位置">${icon('follow')}跟随</summary><div class="editor-command-body follow-menu-body" role="menu" aria-label="播放跟随">${rows}<div class="follow-divider" role="separator"></div><button type="button" class="quiet" role="menuitemcheckbox" aria-checked="false" data-do="followSuspend"><span>编辑时暂停跟随</span><span class="follow-mark" aria-hidden="true"></span></button></div></details>`;
}
function updateFollowControl() {
  const menu=document.querySelector('.follow-menu');
  if(!menu)return;
  const button=menu.querySelector('summary');
  const active=String(followMode!=='off'&&!followSuspended);
  if(button.dataset.followActive!==active)button.dataset.followActive=active;
  const title=followSuspended?'跟随已暂停，选择模式可恢复':`跟随：${followLabels[followMode]}`;
  if(button.title!==title)button.title=title;
  for(const row of menu.querySelectorAll('button[data-do]')){
    const option=row.dataset.do==='followSuspend',checked=option?suspendFollowOnEdit:row.dataset.key===followMode;
    if(row.getAttribute('aria-checked')!==String(checked))row.setAttribute('aria-checked',String(checked));
    const sign=row.querySelector('.follow-mark'),value=checked?'✓':option?'×':'';
    if(sign.textContent!==value)sign.textContent=value;
  }
}
function animateMidiCursor(now) {
  midiAnimation=0;
  if (!rollIsPlaying()) resumeRollFollow();
  if (tab!=="notes" || !(transportMidi?state.midi.playing:state.player.playing) || !$("midi-cursor")) return;
  const traceOn=state.player?.trace,p0=traceOn?performance.now():0;
  const shown=updateMidiCursor(now),seconds=shown??midiDisplayPosition(now);
  paintPlaybackPosition(seconds,shown!==undefined);
  // Trace: frame interval and the time spent painting the playhead (includes layout).
  if(traceOn){tracePlayback('disp',{shown:Math.round(seconds*1e6)/1e6,midi:transportMidi,dt:lastMidiFrame?Math.round((now-lastMidiFrame)*100)/100:null,paint_ms:Math.round((performance.now()-p0)*1000)/1000});lastMidiFrame=now;}
  midiAnimation=requestAnimationFrame(animateMidiCursor);
}

// Both directions use the same piecewise clock. Meter and presentation are
// separate from this clock; drafts are never installed by a view change.
function interpolateRollAnchors(rows,value,x,y){
  let lo=0,hi=rows.length-1;
  while(lo+1<hi){const mid=(lo+hi)>>1;if(rows[mid][x]<=value)lo=mid;else hi=mid;}
  const a=rows[lo],b=rows[hi];
  if(value===b[x])return b[y];
  return a[y]+(value-a[x])*(b[y]-a[y])/(b[x]-a[x]);
}
const defaultTempoPoint={seconds:0,beat:0,tempo:500000};
function rollTempoPoint(map,value,key){
  let lo=0,hi=map?.length||0;
  while(lo+1<hi){const mid=(lo+hi)>>1;if(map[mid][key]<=value)lo=mid;else hi=mid;}
  return map?.[lo]||defaultTempoPoint;
}
function rollAxis(){
  const doc=state.project?.editor,anchors=doc?.tempo?.anchors,map=anchors?.length>1?null:state.midi.tempo_map;
  const width=midiWidth,mode=rulerMode,version=rollInputVersion;
  const duration=AstraProjectEditor.workingView()?Math.max(doc.duration||0,1):doc?Math.max(doc.duration||0,state.midi.duration||0,1):state.midi.duration||1;
  const c=axisCache;
  if(c&&c.mode===mode&&c.width===width&&c.version===version&&c.anchors===anchors&&c.map===map&&c.duration===duration)return c;
  const timeLinear=mode==='seconds', anchored=anchors?.length>1;
  const toBeat=anchored?s=>interpolateRollAnchors(anchors,s,'seconds','beat'):s=>{const p=rollTempoPoint(map,s,'seconds');return p.beat+(s-p.seconds)*1e6/p.tempo;};
  const toSeconds=anchored?q=>interpolateRollAnchors(anchors,q,'beat','seconds'):q=>{const p=rollTempoPoint(map,q,'beat');return p.seconds+(q-p.beat)*p.tempo/1e6;};
  // The right edge is the same instant in both views, even when a MIDI
  // duration_beats field was separately rounded by the decoder.
  const beatExtent=Math.max(Number.EPSILON,toBeat(duration)),extent=timeLinear?duration:beatExtent;
  const unitAt=(px,pixels)=>Math.max(0,Math.min(extent,px*extent/Math.max(1,pixels)));
  return axisCache={mode,width,version,anchors,map,duration,beatExtent,extent,toBeat,toSeconds,
    x:(seconds,knownBeat,pixels=width)=>(timeLinear?seconds:(knownBeat??toBeat(seconds)))*pixels/extent,
    beatX:q=>(timeLinear?toSeconds(q):q)*width/extent,
    seconds:(px,pixels=width)=>{const unit=unitAt(px,pixels);return timeLinear?unit:toSeconds(unit);},
    beat:px=>{const unit=unitAt(px,width);return timeLinear?toBeat(unit):unit;}};
}
function secondsToBeat(seconds){return rollAxis().toBeat(seconds);}
function beatToSeconds(beat){return rollAxis().toSeconds(beat);}
function midiExtent(){return rollAxis().extent;}
function midiX(seconds){return rollAxis().x(seconds);}
function beatX(beat){return rollAxis().beatX(beat);}
function midiSeconds(x){return rollAxis().seconds(x);}
function midiVisualX(seconds,offset){return window.AstraRhythmMotion?.x(seconds,offset)??midiX(seconds);}
function rulerUnitLabel(){return rulerMode==='seconds'?'时间':'小节 / 拍';}
function rulerModeField(){
  return `<label for="roll-axis">显示轴</label><select id="roll-axis" class="control" title="只改变显示，不改变演奏时间或吸附设置"><option value="bars" ${rulerMode==='bars'?'selected':''}>按小节显示</option><option value="seconds" ${rulerMode==='seconds'?'selected':''}>按时间显示</option></select>`;
}
function readonlyGridMenu(){return `<details class="editor-command-menu editor-grid-menu" data-disclosure="editor-grid-menu"><summary>网格</summary><div class="editor-command-body">${rulerModeField()}</div></details>`;}
// Capture before mode-specific controls change the existing roll height.
// The first frame in the new layout consumes this anchor once; no delayed pullback.
function preserveRollModeViewport(){
  const key=AstraProjectEditor.workingViewKey(),side=document.querySelector('.editor-side');
  if(key&&side)savedWorkingInspector={key,top:side.scrollTop};
  const scroll=$("roll-scroll"),width=scroll?.clientWidth||0;
  if(!key||!width)return;
  const painted=pitchView&&rollGeometry?.canvas===$("midi-canvas");
  // An early click can precede the first RAF; seed the visible read-mode
  // layout width rather than let the new sidebar determine the initial scale.
  if(modeProjection?.key!==key)
    modeProjection={key,basis:painted&&pendingRollZoom?.scroll!==scroll?(midiWidth||width)/zoom:width};
  if(painted){
    const box=scroll.getBoundingClientRect(),pending=modeViewport?.scroll===scroll?modeViewport:null;
    modeViewport=pending||{scroll,key,left:scroll.scrollLeft,top:scroll.scrollTop,x:box.left,y:box.top};
  }
}
function rollViewport(scroll,identity){
  const waiting=pendingRollScroll?.scroll===scroll?pendingRollScroll:null;
  const rescale=pendingRollZoom?.scroll===scroll?pendingRollZoom:null;
  const axis=rollGeometry?.canvas===$("midi-canvas")?rollGeometry.axis:rollAxis();
  const anchor=rescale?.anchor??waiting?.anchor??(waiting?.seconds===undefined?.5:0);
  const top=waiting?.top??scroll.scrollTop;
  // Centre pitch is the vertical musical anchor; pixels alone break when the
  // remounted roll has a different viewport height or row size.
  const pitch=waiting?waiting.pitch:(pitchView?.row>0&&rollGeometry?.canvas===$("midi-canvas")?pitchView.high-(top+scroll.clientHeight/2)/pitchView.row:undefined);
  return {identity:waiting?.identity??identity,left:waiting?.left??scroll.scrollLeft,top,anchor,pitch,
    seconds:rescale?.seconds??waiting?.seconds??axis.seconds((waiting?.left??scroll.scrollLeft)+scroll.clientWidth*anchor)};
}
function setRulerMode(mode){
  if((mode!=='bars'&&mode!=='seconds')||mode===rulerMode)return;
  const scroll=$("roll-scroll");
  if(scroll&&(rollGeometry||pendingRollScroll?.scroll===scroll))pendingRollScroll={scroll,...rollViewport(scroll,pitchView?.identity)};
  pendingRollZoom=null;manualRollNavigation=null;rollWheelRemainder=null;
  rulerMode=mode;
  window.AstraRhythmContextMenu?.close();
  window.AstraRhythmMotion?.cancel?.();
  window.AstraRhythmTimeline?.invalidateBeatView?.();
  AstraProjectEditor.axisChanged?.();
  const field=$("roll-axis"),unit=$("roll-ruler-unit");
  if(field&&field.value!==mode)field.value=mode;
  if(unit)unit.textContent=rulerUnitLabel();
  requestRollDraw();
}
// CC pointer feedback is its own paint surface. Reuse the grid, not the notes.
let pedalPaintCache=null;
// CC64 value 0 is the lane floor: a soft band below it and a light hairline,
// painted under the envelope so value-0 segments and points stay on top.
function paintPedalFloor(pedal,width,height,bottom){pedal.fillStyle='rgba(61,62,76,.045)';pedal.fillRect(0,bottom,width,height-bottom);pedal.fillStyle='rgba(61,62,76,.22)';pedal.fillRect(0,Math.round(bottom),width,1);}
function rememberPedalBackground(canvas,width,height,offset,gridLines=[]){
  if(!window.AstraProjectEditor?.active()){pedalPaintCache=null;return;}
  // Retain cheap grid commands, never copy a GPU-backed canvas into another
  // canvas. Chromium/Qt D3D shared-image readback can stall or lose its context.
  pedalPaintCache={canvas,gridLines,width,height,offset,revision:state.project?.editor?.revision,path:state.midi.path,axis:rollAxis(),ratio:canvasRatio()};
}
// A CC-only frame samples the release motion and paints it in the SAME rAF.
// A simultaneous note and CC request promotes to a full frame; neither is lost.
function queueRollFrame(damage){
  rollDamage|=damage;
  if(!rollFrame)rollFrame=requestAnimationFrame(paintRollFrame);
}
function requestNotePatchRows(rows){
  if(rollNotePatches.length+rows.length>64){rollInsert=null;rollNotePatches=[];requestRollDraw();return;}
  rollNotePatches.push(...rows);queueRollFrame(ROLL_NOTE_PATCH);
}
function requestNoteInsertDraw(before,after,note){
  if(rollInsert&&rollInsert.after!==before){rollInsert=null;requestRollDraw();}
  rollInsert??={before,after,added:[]};rollInsert.after=after;rollInsert.added.push(note);
  requestNotePatchRows([note]);
}
function paintRollFrame(now){
  const damage=rollDamage,append=rollInsert,patches=rollNotePatches;
  rollFrame=rollDamage=0;rollInsert=null;rollNotePatches=[];
  const motion=window.AstraRhythmMotion,rhythm=window.AstraRhythmTimeline;
  motion?.advanceSettles(now);
  let base=damage&~ROLL_BEAT_AIM;
  // Native scroll may already have moved the viewport before its scroll event.
  if(!base&&rollGeometry&&rollGeometry.offset!==$('roll-scroll')?.scrollLeft)base=ROLL_ALL;
  const painted=base&&(base!==ROLL_CC||!paintPedalNow());
  let patch=false;
  if(painted){
    const started=state.player?.trace?performance.now():null;
    patch=base===ROLL_NOTE_PATCH&&paintNotePatch(patches,append);
    if(!patch)drawMidi(base===ROLL_NOTES,append);
    if(started!==null)tracePlayback('roll_draw',{ms:Math.round((performance.now()-started)*1000)/1000,damage:base,patch,follow:followMode!=='off'&&!followSuspended});
  }
  if(painted&&!patch||(damage&ROLL_BEAT_AIM))rhythm?.paintBeatAim?.();
  if(painted&&!patch)updateMidiCursor();
  motion?.scheduleSettles();
}
function requestPedalDraw(){queueRollFrame(ROLL_CC);}
function requestNoteDragDraw(){queueRollFrame(ROLL_NOTES);}
function requestBeatAimDraw(){queueRollFrame(ROLL_BEAT_AIM);}
function invalidateRollInputs(){
  ++rollInputVersion;
  if(tab==='notes'&&$('roll-scroll'))requestRollDraw();
}
function paintPedalNow(){
  const c=pedalPaintCache,canvas=$('pedal-canvas'),scroll=$('roll-scroll'),g=midiPedalGeometry();
  if(!c||!canvas||c.canvas!==canvas||c.path!==state.midi.path||c.revision!==state.project?.editor?.revision||c.offset!==scroll?.scrollLeft||c.width!==scroll?.clientWidth||c.axis!==rollAxis()||c.ratio!==canvasRatio()||c.height!==g.height||!AstraProjectEditor.active()||window.AstraRhythmMotion?.active())return false;
  const pedal=canvasContext(canvas,c.width,c.height);pedal.fillStyle='#edf2f5';pedal.fillRect(0,0,c.width,c.height);pedal.fillStyle='#ded8e5';for(const x of c.gridLines)pedal.fillRect(x,0,1,c.height);
  paintPedalFloor(pedal,c.width,c.height,g.bottom);
  AstraProjectEditor.drawPedalEnvelope(pedal,c.offset,c.width);
  const r=AstraProjectEditor.range();if(r.end!==r.start&&(r.loop||AstraProjectEditor.localScope?.())){const x=midiX(r.start);pedal.fillStyle=r.loop?'rgba(117,96,175,.10)':'rgba(117,96,175,.08)';pedal.fillRect(x-c.offset,0,midiX(r.end)-x,c.height);}
  AstraProjectEditor.drawPedalOverlay(pedal,c.offset,c.width);
  return true;
}
function requestRollDraw(event) {
  // Vertical movement is native canvas scrolling; only the separate keyboard
  // needs to move. Reallocating/rasterizing every canvas here causes jank.
  const scroll=$("roll-scroll");
  if(event?.type==='scroll'&&rollGeometry&&scroll&&rollGeometry.offset===scroll.scrollLeft&&rollGeometry.axis===rollAxis()){
    const keys=$("keys-canvas"),transform=`translateY(${-scroll.scrollTop}px)`;
    if(keys&&keys.style.transform!==transform)keys.style.transform=transform;
    if(window.AstraRhythmTimeline?.hasBeatView?.())requestBeatAimDraw();
    return;
  }
  queueRollFrame(ROLL_ALL);
}
function setMidiZoom(value, anchorX) {
  manualRollNavigation=null;
  const scroll=$("roll-scroll"); if (!scroll) return;
  const nextZoom=Math.max(1,Math.min(128,value));
  if(!Number.isFinite(nextZoom)||nextZoom===zoom)return;
  // Effective manual navigation owns the viewport until Follow is resumed.
  suspendRollFollow(false);
  const prior=pendingRollZoom?.scroll===scroll ? pendingRollZoom : null;
  const viewport=scroll.clientWidth;if(!viewport)return;
  const anchor=Math.max(0,Math.min(viewport,anchorX??viewport/2)),axis=rollAxis();
  const waiting=pendingRollScroll?.scroll===scroll?pendingRollScroll:null;
  const waitingLeft=waiting?.seconds===undefined?waiting?.left:axis.x(waiting.seconds)-viewport*(waiting.anchor??0);
  const seconds=axis.seconds((prior?.left??waitingLeft??scroll.scrollLeft)+anchor,prior?.width??(midiWidth||viewport));
  window.AstraRhythmContextMenu?.close();
  zoomManuallyChanged=true;
  zoom=nextZoom;
  window.AstraRhythmMotion?.cancel?.();
  window.AstraRhythmTimeline?.invalidateBeatView?.();
  const width=Math.max(viewport,Math.round((modeProjection?.basis??viewport)*zoom));
  const left=Math.max(0,Math.min(width-viewport,axis.x(seconds,undefined,width)-anchor));
  pendingRollZoom={scroll,width,left,seconds,anchor:anchor/viewport};
  requestRollDraw();
}
function canvasRatio(){return Math.min(2,window.devicePixelRatio||1);}
function canvasContext(canvas,width,height) {
  const ratio=canvasRatio();
  const pixelsWide=Math.max(1,Math.round(width*ratio)),pixelsHigh=Math.max(1,Math.round(height*ratio));
  if(canvas.style.width!==`${width}px`)canvas.style.width=`${width}px`;
  if(canvas.style.height!==`${height}px`)canvas.style.height=`${height}px`;
  if(canvas.width!==pixelsWide)canvas.width=pixelsWide;
  if(canvas.height!==pixelsHigh)canvas.height=pixelsHigh;
  const ctx=canvas.getContext("2d");ctx.setTransform(ratio,0,0,ratio,0,0);return ctx;
}
function midiDrawingData(append=null) {
  const m=AstraProjectEditor.model(),{anchors,map,toBeat}=rollAxis(),revision=state.project?.editor?.revision;
  // The live CC envelope owns the editable lane, not decoded preview segments.
  const fallbackPedals=AstraProjectEditor.workingView()||AstraProjectEditor.active()?null:m.pedals;
  const c=rollData,stable=c&&c.version===rollInputVersion&&c.anchors===anchors&&c.map===map&&
    c.events===m.pedal_events&&c.pedals===fallbackPedals&&c.duration===m.duration;
  if(stable&&c.source===m.notes&&c.revision===revision)return c;
  if(stable&&append&&c.source===append.before&&m.notes===append.after){
    const notes=c.notes.slice(),entries=c.entries.slice();let low=c.low,high=c.high;
    for(const note of append.added){
      // The full path is a stable start-time sort. New equal-start notes go
      // after old ones. Touch the suffix maximum only while it changes.
      let lo=0,hi=entries.length;
      while(lo<hi){const mid=(lo+hi)>>>1;if(entries[mid].note.start<=note.start)lo=mid+1;else hi=mid;}
      const end=Math.max(note.end,entries[lo-1]?.latestEnd??0),bottom=Math.max(0,note.pitch-2),top=Math.min(127,note.pitch+2);
      low=notes.length?Math.min(low,bottom):bottom;high=notes.length?Math.max(high,top):top;
      notes.splice(lo,0,note);entries.splice(lo,0,{note,latestEnd:end,startBeat:toBeat(note.start),endBeat:toBeat(note.end)});
      for(let i=lo+1;i<entries.length&&entries[i].latestEnd<end;i++)entries[i]={...entries[i],latestEnd:end};
    }
    return rollData={...c,revision,source:m.notes,notes,entries,low,high};
  }
  const notes=(m.notes || []).filter(n=>Number.isFinite(n.pitch)&&Number.isFinite(n.start)&&Number.isFinite(n.end)).sort((a,b)=>a.start-b.start);
  let low=notes.length?127:60,high=notes.length?0:71,latestEnd=0;
  const entries=notes.map(note=>{low=Math.min(low,note.pitch);high=Math.max(high,note.pitch);latestEnd=Math.max(latestEnd,note.end);return {note,latestEnd,startBeat:toBeat(note.start),endBeat:toBeat(note.end)};});
  const channelValues=new Map(),envelope=[],events=m.pedal_events||[];
  let previous=0,level=0;
  for(let i=0;i<events.length;){const at=events[i].seconds;if(at>previous&&level>0)envelope.push({start:previous,end:at,value:level});while(i<events.length&&events[i].seconds===at){channelValues.set(events[i].channel,events[i].value);i++;}level=Math.max(0,...channelValues.values());previous=at;}
  if(level&&previous<m.duration)envelope.push({start:previous,end:m.duration,value:level});
  const pedals=(events.length?envelope:fallbackPedals||[]).map(segment=>({...segment,startBeat:toBeat(segment.start),endBeat:toBeat(segment.end)}));
  rollData={version:rollInputVersion,anchors,map,revision,source:m.notes,events:m.pedal_events,pedals:fallbackPedals,duration:m.duration,notes,entries,low:Math.max(0,low-2),high:Math.min(127,high+2),envelope:pedals};
  return rollData;
}
function midiPedalGeometry(){const height=Math.max(52,Number($("pedal-lane")?.clientHeight||rollLanes.pedal.height)-1);return {height,top:12,bottom:height-6,span:height-18};}
function setPitchZoom(value){const next=Math.max(.5,Math.min(4,value));if(next!==pitchZoom)window.AstraRhythmContextMenu?.close();pitchZoom=next;if($("pitch-zoom"))$("pitch-zoom").value=pitchZoom;requestRollDraw();}
function rollLaneLabel(id,range){
  const {folded,label}=rollLanes[id];
  return `<span><button type="button" class="roll-lane-fold" data-roll-lane-toggle="${id}" aria-controls="${id}-canvas" aria-expanded="${!folded}"><span class="roll-lane-chevron" aria-hidden="true"></span>${label}</button><small>${range}</small></span>`;
}
function rollLaneSeparator(id){
  const {height,min,folded,label}=rollLanes[id];
  return `<div id="${id}-divider" data-roll-lane-resize="${id}" class="roll-lane-divider" role="separator" aria-orientation="horizontal" aria-label="调整${label}泳道高度" aria-controls="${id}-lane" aria-valuemin="${min}" aria-valuemax="320" aria-valuenow="${height}" tabindex="0"${folded||(id==='pedal'&&AstraProjectEditor.ccLaneHidden())?' hidden':''}></div>`;
}
function rollLaneMax(id){
  const lane=$(`${id}-lane`),editor=lane?.parentElement,roll=editor?.querySelector('.midi-roll');
  const remaining=editor&&roll?roll.clientHeight+lane.offsetHeight-80:320;
  return Math.max(rollLanes[id].min,Math.min(320,remaining));
}
function setRollLaneHeight(id,value,maximum=rollLaneMax(id)){
  const view=rollLanes[id],next=Math.max(view.min,Math.min(maximum,Math.round(value)));
  if(next===view.height)return;
  view.height=next;
  $(`${id}-lane`)?.style.setProperty('--lane-height',`${next}px`);
  $(`${id}-divider`)?.setAttribute('aria-valuenow',next);
  rollLayoutChanged();
}
function setPedalHeight(value){setRollLaneHeight('pedal',value);}
function toggleRollLane(id){
  const view=rollLanes[id],lane=$(`${id}-lane`),divider=$(`${id}-divider`);
  if(!lane||!divider)return;
  view.folded=!view.folded;lane.dataset.folded=String(view.folded);
  divider.hidden=view.folded||lane.hidden;
  lane.querySelector('[data-roll-lane-toggle]').setAttribute('aria-expanded',String(!view.folded));
  rollLayoutChanged();
}
function syncRollTimeScroll(scroll,extent=true){
  const bar=$('roll-time-scroll');if(!bar)return;
  if(extent){
    const width=scroll.scrollWidth,viewport=scroll.clientWidth;
    const gutter=`${scroll.offsetWidth-viewport}px`,max=String(Math.max(0,width-viewport));
    if(bar.parentElement.style.paddingRight!==gutter)bar.parentElement.style.paddingRight=gutter;
    if(bar.max!==max){
      bar.max=max;
      bar.style.setProperty('--roll-thumb-width',`clamp(24px,${100*viewport/Math.max(1,width)}%,100%)`);
      bar.disabled=max==='0';
    }
  }
  if(Math.abs(bar.valueAsNumber-scroll.scrollLeft)>.001)bar.value=scroll.scrollLeft;
}
function attachRollTimeScroll(scroll){
  const bar=$('roll-time-scroll');if(!bar)return;
  const navigate=left=>{
    if(bar!==$('roll-time-scroll')||scroll!==$('roll-scroll'))return;
    const before=scroll.scrollLeft;
    scroll.scrollLeft=left;
    syncRollTimeScroll(scroll,false);
    if(scroll.scrollLeft!==before)suspendRollFollow(false);
  };
  bar.addEventListener('input',()=>navigate(bar.valueAsNumber));
  bar.addEventListener('keydown',event=>{
    if(event.ctrlKey||event.shiftKey||event.altKey||event.metaKey)return;
    const {key}=event;
    if(!['ArrowLeft','ArrowRight','Home','End','PageUp','PageDown'].includes(key))return;
    event.preventDefault();
    const delta=key==='ArrowLeft'?-40:key==='ArrowRight'?40:(key==='PageUp'?-1:1)*bar.clientWidth*.9;
    navigate(key==='Home'?0:key==='End'?Number(bar.max):bar.valueAsNumber+delta);
  });
}
function midiPitchGeometry(data, viewportHeight, editable) {
  // Keep readable rows around the musical range; full MIDI range is scrollable,
  // never squeezed into one screen just because editing permits all 128 pitches.
  const low=editable?0:data.low,high=editable?127:data.high;
  const fitRow=Math.max(12,Math.floor(viewportHeight/(data.high-data.low+1)));
  const row=Math.round(Math.max(6,Math.min(32,fitRow*pitchZoom)));
  const height=Math.max(viewportHeight,(high-low+1)*row);
  const fitTop=Math.max(0,Math.min(height-viewportHeight,
    (high-(data.low+data.high)/2+.5)*row-viewportHeight/2));
  return {low,high,row,height,fitTop};
}
function sameRollInputs(previous,next){return previous?.length===next.length&&next.every((value,i)=>value===previous[i]);}

function secondsRulerTicks(axis,offset,width){
  if(axis.mode!=='seconds')return null;
  const raw=78*axis.extent/Math.max(1,axis.width),base=10**Math.floor(Math.log10(raw));
  const step=base*(raw<=base?1:raw<=2*base?2:raw<=5*base?5:10);
  const places=Math.max(0,-Math.floor(Math.log10(step)+1e-8)),scale=10**places;
  const ticks=[],first=Math.ceil(axis.seconds(offset)/step-1e-8),last=Math.floor(axis.seconds(offset+width)/step+1e-8);
  for(let i=first;i<=last;i++){
    const s=i*step,stamp=Math.round(s*scale);
    ticks.push([Math.round(axis.x(s)-offset),time(Math.floor(stamp/scale))+(places?'.'+String(stamp%scale).padStart(places,'0'):'')]);
  }
  return ticks;
}

// Shared viewport command list. The seconds ruler's labels are time ticks;
// note/controller lanes still show the projected musical snap grid.
function timeGridForView(width,offset,axis,ratio){
  const doc=state.project?.editor,active=AstraProjectEditor.active();
  const sourceMeters=doc?AstraProjectEditor.meterSections(doc.tempo):state.midi.time_signatures;
  const view={width,offset,axis};view.unit=AstraProjectEditor.gridUnit(view);
  const preview=!doc&&AstraRollMeter.isPreview();
  const inputs=[rollInputVersion,state.project?.path,state.midi.path,doc?.revision,doc?.tempo,sourceMeters,axis,active,width,offset,ratio,view.unit,AstraRollMeter.gridVersion(),preview];
  if(sameRollInputs(timeGridCache?.inputs,inputs))return timeGridCache;
  const trace=window.AstraRenderStage,started=trace?performance.now():0,lines=[];
  function line(x,label,major=true,kind=''){
    x=Math.round(x-offset);if(x>=-1&&x<=width)lines.push([x,label,major?3:kind==='beat'?2:1]);
  }
  if(doc||!AstraRollMeter.grid(line,view)){
    const start=axis.beat(offset),end=axis.beat(offset+width),total=axis.beatExtent;
    const meters=sourceMeters?.length?sourceMeters:[{beat:0,numerator:4,denominator:4,bar:1}];
    let number=1;
    for(let i=0;i<meters.length;i++){
      const m=meters[i],prior=meters[i-1],next=meters[i+1]?.beat??Infinity;
      if(prior)number+=Math.ceil((m.beat-prior.beat)/(prior.numerator*4/prior.denominator)-1e-7);
      if(m.bar!==undefined)number=m.bar;
      const unit=4/m.denominator,length=m.numerator*unit;
      if(!(length>0)||!Number.isFinite(length)||next<start||m.beat>end+1e-6)continue;
      const limit=Math.min(next,total,end+length),first=Math.max(0,Math.floor((start-m.beat)/length));
      const bars=axis.mode==='bars',linearWidth=length*axis.width/axis.beatExtent;
      const stride=bars?Math.max(1,Math.ceil(65/linearWidth)):1;
      for(let n=first;m.beat+n*length<=limit+1e-6;n++){
        const q=m.beat+n*length;if(q>=next-1e-6)break;
        const x=axis.beatX(q),barPixels=bars?linearWidth:axis.beatX(Math.min(q+length,next))-x;
        if(n===0||n%stride===0&&(bars||barPixels>12)){
          const label=m.free?`${number+n} · 自由`:m.silence?'静音':m.pickup?'弱起':m.intro?`引${number+n}`:`${number+n}${n===0&&!active?` · ${m.displayNumerator||m.numerator}/${m.denominator}`:''}`;
          line(x,label);
        }else if(barPixels>12)line(x,'');
        if(!m.free&&!active){
          let previousX=x;
          for(let b=1;b<m.numerator&&q+b*unit<limit;b++){
            const bx=axis.beatX(q+b*unit),pixels=bx-previousX;previousX=bx;
            if(pixels>23)line(bx,pixels>65?`${number+n}.${b+1}`:'',false);
          }
        }
      }
    }
  }
  AstraProjectEditor.gridLines(line,view);
  // At integer DPR opaque columns are disjoint; last writer wins. At fractional
  // DPR preserve the ordered coverage of all fillRects, including repetitions.
  const columns=Number.isInteger(ratio)?new Uint8Array(width):null,pedal=[];
  if(columns){
    for(const [x,,ink] of lines)if(x>=0&&x<width)columns[x]=ink;
    for(let x=0;x<width;x++)if(columns[x])pedal.push(x);
  }else for(const [x] of lines)pedal.push(x);
  const ticks=secondsRulerTicks(axis,offset,width);
  trace?.('roll.grid.build',performance.now()-started);
  return timeGridCache={inputs,lines,columns,pedal,preview,ticks,paths:null};
}
function paintNoteTimeGrid(ctx,grid,height){
  const colors=['','#eeedf0','#e1dfe5',grid.preview?'#7a86c8':'#becbd7'];
  if(grid.columns&&typeof Path2D==='function'){
    if(grid.paths?.height!==height){
      const shapes=[null,null,null,null],columns=grid.columns;
      for(let x=0;x<columns.length;){
        const ink=columns[x];if(!ink){x++;continue;}
        let end=x+1;while(end<columns.length&&columns[end]===ink)end++;
        (shapes[ink]??=new Path2D()).rect(x,0,end-x,height);x=end;
      }
      grid.paths={height,shapes};
    }
    for(let ink=1;ink<=3;ink++)if(grid.paths.shapes[ink]){ctx.fillStyle=colors[ink];ctx.fill(grid.paths.shapes[ink]);}
  }else{
    let last=0;
    for(const [x,,ink] of grid.lines){if(last!==ink){ctx.fillStyle=colors[ink];last=ink;}ctx.fillRect(x,0,1,height);}
  }
}
function paintRulerTimeGrid(ruler,grid){
  let right=-Infinity,style='';
  const color=major=>{const ink=major?'#6b6578':'#81798e';if(style!==ink){ruler.fillStyle=ink;style=ink;}};
  const tick=(x,major)=>{color(major);ruler.fillRect(x,major?34:51,1,22);};
  const text=(x,label,major)=>{if(label&&x+4>right){
    const left=x<=10?x+11:x+4;color(major);ruler.fillText(label,left,51);right=left+4+ruler.measureText(label).width;
  }};
  if(grid.ticks){for(const [x,label] of grid.ticks){tick(x,true);text(x,label,true);}return;}
  if(!grid.columns){for(const [x,label,ink] of grid.lines){tick(x,ink===3);text(x,label,ink===3);}return;}
  // Opaque ticks can coalesce within a run, never across text painting.
  const run=new Map();
  const flush=()=>{for(const [x,bands] of run){if(bands&1)tick(x,true);if(bands&2)tick(x,false);}run.clear();};
  for(const [x,label,ink] of grid.lines){run.set(x,ink===3?1:((run.get(x)||0)&1)|2);if(label){flush();text(x,label,ink===3);}}
  flush();
}

function paintNoteRows(ctx,width,height,low,high,row){
  ctx.fillStyle='#edf2f5';ctx.fillRect(0,0,width,height);
  for(let pitch=high;pitch>=low;pitch--){
    const pc=pitch%12,y=(high-pitch)*row,black=Boolean(0x54a&(1<<pc));
    ctx.fillStyle=black?'#f4f5fb':'#ffffff';ctx.fillRect(0,y,width,row-1);
    if(pc===0){ctx.fillStyle='#dde0ee';ctx.fillRect(0,y+row-1,width,1);}
  }
}

function paintNoteInk(ctx,data,view,strip=null){
  const {axis,offset,width,height,high,row,motion,moving,startSeconds,endSeconds}=view;
  // Search beyond the clip for the minimum-width bodies and their rings.
  const first=strip?Math.max(startSeconds,axis.seconds(offset+strip.left-8)):startSeconds;
  const last=strip?Math.min(endSeconds,axis.seconds(offset+strip.right+8)):endSeconds;
  let lo=0,hi=data.entries.length;
  while(lo<hi){const mid=(lo+hi)>>1;if(data.entries[mid].latestEnd<first)lo=mid+1;else hi=mid;}
  const marked=[],covered=AstraProjectEditor.ordinaryPitchCover?.(),resizing=AstraProjectEditor.noteResizeInkIds?.();
  for(let index=lo;index<data.entries.length;index++){
    const entry=data.entries[index],note=entry.note;if(note.start>last)break;
    // Only the selected resize overlay owns these bodies until the end is released.
    if(resizing?.has(note.id))continue;
    if(note.pitch>high||(high-note.pitch)*row>=height)continue;
    const x=(moving?motion.x(note.start,offset):axis.x(note.start,entry.startBeat))-offset;
    const end=(moving?motion.x(note.end,offset):axis.x(note.end,entry.endBeat))-offset;
    if(end<0||x>width)continue;
    const left=Math.max(0,x),w=Math.max(2,Math.min(width,end)-left),top=(high-note.pitch)*row+1,h=row-2;
    if(strip&&(left-5>strip.right||left+w+5<strip.left))continue;
    const selected=AstraNoteInspector.selected(note);
    const hovered=!selected&&!AstraProjectEditor.selected(note)&&Boolean(AstraProjectEditor.hovered?.(note));
    const color=window.AstraVelocity.color(AstraProjectEditor.velocityInk?.(note)??note);
    ctx.fillStyle=color;ctx.fillRect(left,top,w,h);
    ctx.strokeStyle='rgba(46,56,88,.5)';ctx.lineWidth=1;ctx.strokeRect(left+.5,top+.5,Math.max(1,w-1),Math.max(1,h-1));
    if(w>=5&&h>=7){ctx.fillStyle='rgba(255,255,255,.16)';ctx.fillRect(left,top,w,1);}
    if(!selected&&x>=0&&w>=10&&h>=8&&!covered?.(note))AstraProjectEditor.paintOrdinaryPitch?.(ctx,note.pitch,left,top,w,h,color);
    if(selected||hovered)marked.push([selected,left,top,w,h,color,x>=0?note.pitch:null]);
  }
  // The second pass keeps choice ink above overlapping bodies. Range slices
  // use the same renderer and geometry, with only their cut sides left open.
  if(marked.length)AstraProjectEditor.paintNoteSelectionInk(ctx,outline=>{
    for(const [chosen,left,top,w,h,color,pitch] of marked)if(chosen)outline(left,top,w,h,color,true,true,pitch);
  });
  for(const [chosen,left,top,w,h] of marked)if(!chosen){
    ctx.strokeStyle='rgba(43,69,168,.72)';ctx.lineWidth=1;ctx.strokeRect(left-.5,top-.5,w+1,h+1);
  }
}

function withNoteClip(ctx,canvas,ratio,strip,paint){
  ctx.save();
  try{
    // Clip on backing-pixel edges, even on fractional-DPR Qt displays. Replay
    // the original fills rather than copying a GPU-backed bitmap.
    ctx.setTransform(1,0,0,1,0,0);ctx.beginPath();
    const left=Math.floor(strip.left*ratio),right=Math.min(canvas.width,Math.ceil(strip.right*ratio));
    ctx.rect(left,0,Math.max(0,right-left),canvas.height);ctx.clip();ctx.beginPath();
    ctx.setTransform(ratio,0,0,ratio,0,0);paint();
  }finally{ctx.restore();}
}

function paintNotePatch(rows,append){
  const r=rollRaster,scene=AstraProjectEditor.notePatchScene?.(),scroll=$('roll-scroll');
  if(!r||!scene||!rows.length||!sameRollInputs(r.scene,scene)||!scroll||r.scroll!==scroll||
     r.canvas!==$('midi-canvas')||r.velocityCanvas!==$('velocity-canvas')||r.path!==state.midi.path||
     r.ratio!==canvasRatio()||r.axis!==rollAxis()||r.zoom!==zoom||r.pitchZoom!==pitchZoom||
     pendingRollScroll||pendingRollZoom||pendingPitchFit||window.AstraRhythmMotion?.active()||
     r.offset!==scroll.scrollLeft||r.width!==scroll.clientWidth||r.viewportHeight!==scroll.clientHeight||
     (r.velocityCanvas&&r.vh!==Math.max(40,Number($('velocity-lane')?.clientHeight||65)-1)))return false;
  const source=AstraProjectEditor.model().notes;
  if(source!==r.data.source&&(!append||append.before!==r.data.source||append.after!==source))return false;
  const data=midiDrawingData(append);
  const {axis,offset,width,height,high,low,row,canvas,velocityCanvas,ratio,grid,vh}=r;
  const ctx=canvas.getContext('2d'),velocity=velocityCanvas?.getContext('2d');
  if(!ctx||ctx.isContextLost?.()||velocityCanvas&&(!velocity||velocity.isContextLost?.()))return false;
  let left=width,right=0;
  for(const n of rows){
    const x=axis.x(n.start)-offset,end=axis.x(n.end)-offset;
    const a=Math.max(0,x-6),b=Math.min(width,Math.max(x+8,end+6));
    if(b>a){left=Math.min(left,a);right=Math.max(right,b);}
  }
  if(right-left>width*.55)return false;
  const trace=window.AstraRenderStage,started=trace?performance.now():0;
  const strip={left:Math.floor(left*ratio)/ratio,right:Math.ceil(right*ratio)/ratio};
  rollGeometry={canvas,notes:data.notes,high,row,offset,axis,viewport:width,xForSeconds:axis.x};
  if(right>left){
    if(velocity)withNoteClip(velocity,velocityCanvas,ratio,strip,()=>AstraProjectEditor.drawVelocityLane(velocity,offset,width,vh,grid.pedal,strip));
    withNoteClip(ctx,canvas,ratio,strip,()=>{
      paintNoteRows(ctx,width,height,low,high,row);paintNoteTimeGrid(ctx,grid,height);
      AstraProjectEditor.drawBackdrop?.(ctx,offset,height,width);
      paintNoteInk(ctx,data,{axis,offset,width,height,high,row,moving:false,
        startSeconds:axis.seconds(offset),endSeconds:axis.seconds(offset+width)},strip);
      AstraProjectEditor.draw(ctx,null,null,offset,height,width);
    });
  }else AstraProjectEditor.paintCommittedNotes?.();
  // The clipped repaint did not touch ancillary rasters. Their proven inputs
  // are unchanged; let existing CC-only gestures still use that backing grid.
  if(pedalPaintCache?.revision===r.data.revision&&pedalPaintCache.axis===axis&&pedalPaintCache.path===r.path)pedalPaintCache.revision=data.revision;
  r.data=data;
  trace?.('roll.note_patch',performance.now()-started);
  return true;
}

function drawMidi(notePaintOnly=false,append=null) {
  const canvas=$("midi-canvas"),keys=$("keys-canvas"),scroll=$("roll-scroll");
  if (!canvas || !keys || !scroll) return;
  commitModeLayout();
  const trace=window.AstraRenderStage;
  let markedAt=trace?performance.now():0;
  const stage=trace?name=>{const now=performance.now();trace(name,now-markedAt);markedAt=now;}:null;
  AstraProjectEditor.syncBeatFlagVisibility?.();
  // One project-notes viewport for edit and read-only browsing: a mode toggle or
  // a rebuilt preview file is not a new document and must not refit zoom.
  const zoomKey=state.project?.editor?`${state.project.path}:notes:${state.project.editor.edit_stamp?.generation??''}`:state.midi.path;
  // In-place layout changes (side column, docked panels) rescale midiWidth
  // with the viewport. Capture the last painted anchor before any write.
  const priorGeometry=rollGeometry?.canvas===canvas&&rollGeometry.viewport>0?rollGeometry:null,liveLeft=scroll.scrollLeft;
  if (zoomKey && zoomInitializedPath !== zoomKey) {
    zoomInitializedPath=zoomKey;
    if (!zoomManuallyChanged) {
      const owner=AstraProjectEditor.workingView()?state.project?.editor:null;
      const meters=owner?.tempo?.time_signatures||state.midi.time_signatures||[];
      const meter=meters.find(m=>m.beat>0) || meters[0] || {numerator:4,denominator:4};
      const span=16*meter.numerator*4/meter.denominator;
      zoom=Math.max(1,Math.min(128,(owner?secondsToBeat(owner.duration||1):(state.midi.duration_beats||secondsToBeat(state.midi.duration||1)))/span));
      $("midi-zoom").value=Math.log2(zoom);
    }
  }
  const data=midiDrawingData(append),{notes}=data;
  const editable=Boolean(state.project?.editor);
  const pitchIdentity=editable?`${state.project.path}:notes`:state.midi.path;
  // Refit explicitly, not when an insertion expands the note range by one row,
  // nor when edit/read-only toggles or an async preview rebuild changes the
  // toolbar height. A base fitted to a still-empty preview refits once.
  if(!pitchBase||pitchBase.identity!==pitchIdentity||pendingPitchFit||pitchBase.empty&&data.notes.length)pitchBase={identity:pitchIdentity,height:scroll.clientHeight,low:data.low,high:data.high,empty:!data.notes.length};
  const fitted=midiPitchGeometry(editable?pitchBase:data,pitchBase.height,editable);
  const {low,high,row}=fitted,height=Math.max(scroll.clientHeight,fitted.height),fitTop=Math.max(0,Math.min(height-scroll.clientHeight,(high-(data.low+data.high)/2+.5)*row-scroll.clientHeight/2));
  const width=scroll.clientWidth;
  if (!width) return;
  const modeAnchor=modeViewport?.scroll===scroll&&modeViewport.key===AstraProjectEditor.workingViewKey()?modeViewport:null;
  modeViewport=null;
  if(modeProjection?.key!==AstraProjectEditor.workingViewKey())modeProjection=null;
  midiWidth=Math.max(modeProjection?1:width,Math.round((modeProjection?.basis??width)*zoom));
  const content=$("roll-content");
  const contentWidth=Math.max(width,midiWidth);
  if(content.style.width!==`${contentWidth}px`){manualRollNavigation=null;content.style.width=`${contentWidth}px`;}
  if(content.style.height!==`${height}px`)content.style.height=`${height}px`;
  const restore=pendingRollScroll?.scroll===scroll ? pendingRollScroll : null;
  const rescale=pendingRollZoom?.scroll===scroll ? pendingRollZoom : null;
  pendingRollScroll=pendingRollZoom=null;
  const maxTop=Math.max(0,height-scroll.clientHeight);
  let pitchRestored=false;
  if(modeAnchor&&!restore&&!rescale){
    const box=scroll.getBoundingClientRect();
    // Moving the clip edge should not reproject a visible beat/pitch. Let native
    // scroll bounds clamp only when the beginning or end is already at the edge.
    scroll.scrollLeft=modeAnchor.left<1?0:Math.max(0,modeAnchor.left+box.left-modeAnchor.x);
    scroll.scrollTop=Math.max(0,Math.min(maxTop,modeAnchor.top+box.top-modeAnchor.y));
    pitchRestored=true;
  } else if (restore) {
    manualRollNavigation=null;
    scroll.scrollLeft=restore.seconds===undefined?restore.left:Math.max(0,midiX(restore.seconds)-(restore.anchor??0)*width);
    // Restore the centre pitch, not old pixels: row height and viewport
    // height can differ between the unmounted and remounted roll.
    if(Number.isFinite(restore.pitch)&&pitchView?.identity===pitchIdentity){
      scroll.scrollTop=Math.max(0,Math.min(maxTop,(high-restore.pitch)*row-scroll.clientHeight/2));pitchRestored=true;
    }else scroll.scrollTop=restore.top;
  } else if (!rescale&&priorGeometry&&priorGeometry.axis.width!==midiWidth) {
    // Same roll, new viewport width: keep the musical time at the viewport
    // centre. A wider viewport can have already clamped the native offset.
    const clamped=liveLeft<priorGeometry.offset-.5&&liveLeft>=priorGeometry.axis.width-width-1;
    const centre=priorGeometry.axis.seconds((clamped?priorGeometry.offset:liveLeft)+priorGeometry.viewport/2);
    if(Number.isFinite(centre))scroll.scrollLeft=Math.max(0,midiX(centre)-width/2);
  }
  if (rescale) {
    manualRollNavigation=null;
    $("midi-zoom").value=Math.log2(zoom);
    scroll.scrollLeft=Math.max(0,midiX(rescale.seconds)-rescale.anchor*width);
  }
  if (pendingPitchFit || pitchView?.identity!==pitchIdentity) {
    scroll.scrollTop=fitTop;
  } else if (pitchRestored) {
    // Already anchored above.
  } else if (pitchView.row!==row) {
    // Vertical zoom keeps the pitch under the viewport centre in view.
    const centre=pitchView.high-(scroll.scrollTop+scroll.clientHeight/2)/pitchView.row;
    scroll.scrollTop=Math.max(0,Math.min(height-scroll.clientHeight,(high-centre)*row-scroll.clientHeight/2));
  }
  pendingPitchFit=false;
  pitchView={identity:pitchIdentity,row,high};
  window.AstraRhythmTimeline?.restoreViewport?.();
  window.AstraRhythmMotion?.tick();
  syncRollTimeScroll(scroll);
  const offset=scroll.scrollLeft;
  const axis=rollAxis();
  rollGeometry={canvas,notes,high,row,offset,axis,viewport:width,xForSeconds:axis.x};
  // Measure lanes before canvasContext writes canvas sizes/styles.
  const cc=midiPedalGeometry(),velocityCanvas=$("velocity-canvas");
  const vh=velocityCanvas?Math.max(40,Number($("velocity-lane")?.clientHeight||65)-1):0;
  const keyPosition=`translateY(${-scroll.scrollTop}px)`,viewportHeight=scroll.clientHeight;
  const ctx=canvasContext(canvas,width,height);
  stage?.('roll.geometry');
  const ratio=canvasRatio(),gridFrame=timeGridForView(width,offset,axis,ratio);
  stage?.('roll.grid.prepare');
  const motion=window.AstraRhythmMotion,moving=Boolean(motion?.active());
  const rulerCanvas=$("ruler-canvas"),pedalCanvas=$("pedal-canvas"),chordCanvas=$("chord-canvas");
  const layers=[canvas,scroll,rulerCanvas,pedalCanvas,chordCanvas,velocityCanvas,gridFrame,data,height,cc.height,vh,row,low,high];
  const retain=notePaintOnly&&!moving&&AstraProjectEditor.active()&&sameRollInputs(lastRollLayers,layers);
  const ruler=retain?null:canvasContext(rulerCanvas,width,56),pedal=retain?null:canvasContext(pedalCanvas,width,cc.height);
  const keyboardInputs=[keys,height,row,low,high,ratio];
  const keyboard=sameRollInputs(lastKeyboard,keyboardInputs)?null:canvasContext(keys,52,height);
  if(keys.style.transform!==keyPosition)keys.style.transform=keyPosition;
  paintNoteRows(ctx,width,height,low,high,row);
  if(keyboard){keyboard.fillStyle="#e2deea";keyboard.fillRect(0,0,52,height);keyboard.font="10px AstraSans";}
  if(ruler){ruler.fillStyle="#edf2f5";ruler.fillRect(0,0,width,56);ruler.font="11px AstraSans";}
  if(pedal){pedal.fillStyle="#edf2f5";pedal.fillRect(0,0,width,cc.height);}
  if(chordCanvas&&!retain){
    const harmony=canvasContext(chordCanvas,width,24);
    harmony.fillStyle="#edf2f5";harmony.fillRect(0,0,width,24);
    harmony.font="11px AstraSans";let end=-Infinity;
    for(const mark of state.midi.markers||[]){
      const x=axis.x(mark.seconds)-offset;if(x<0||x>width)continue;
      harmony.fillStyle="#6f7fc0";harmony.fillRect(x,0,1,24);
      if(x>=end){harmony.fillStyle="#393645";harmony.fillText(mark.label,x+4,16);end=x+12+harmony.measureText(mark.label).width;}
    }
  }
  if(keyboard)for(let pitch=high;pitch>=low;pitch--){
    const pc=pitch%12,y=(high-pitch)*row,black=Boolean(0x54a&(1<<pc));
    keyboard.fillStyle=black?"#7d8098":"#ffffff";keyboard.fillRect(0,y,black?35:52,row-1);
    if(pc===0){keyboard.fillStyle="#5e6072";keyboard.fillText(`C${Math.floor(pitch/12)-1}`,25,y+Math.min(row-1,11));}
  }
  lastKeyboard=keyboardInputs;
  stage?.('roll.rows');
  paintNoteTimeGrid(ctx,gridFrame,height);
  if(ruler)paintRulerTimeGrid(ruler,gridFrame);
  const pedalGridLines=gridFrame.pedal;
  if(pedal){pedal.fillStyle='#ded8e5';for(const x of pedalGridLines)pedal.fillRect(x,0,1,cc.height);}
  stage?.('roll.grid.paint');
  AstraProjectEditor.drawBackdrop?.(ctx,offset,height,width);
  const leftSeconds=axis.seconds(offset),rightSeconds=axis.seconds(offset+width);
  const {start:startSeconds,end:endSeconds}=moving?motion.bounds(leftSeconds,rightSeconds):{start:leftSeconds,end:rightSeconds};
  paintNoteInk(ctx,data,{axis,offset,width,height,high,row,motion,moving,startSeconds,endSeconds});
  stage?.('roll.notes');
  if(velocityCanvas&&(!retain||AstraProjectEditor.velocityPreviewActive?.()))AstraProjectEditor.drawVelocityLane?.(canvasContext(velocityCanvas,width,vh),offset,width,vh,pedalGridLines);
  AstraProjectEditor.paintVelocityStatus?.();
  if(!retain){
    rememberPedalBackground(pedalCanvas,width,cc.height,offset,pedalGridLines);
    paintPedalFloor(pedal,width,cc.height,cc.bottom);
    const livePedal=Boolean(AstraProjectEditor.drawPedalEnvelope?.(pedal,offset,width));
    // Read-only exports can duplicate sustain across overlapping channels.
    for(const segment of livePedal?[]:data.envelope){
      if(segment.end<startSeconds||segment.start>endSeconds)continue;
      const x=(moving?motion.x(segment.start,offset):axis.x(segment.start,segment.startBeat))-offset;
      const end=(moving?motion.x(segment.end,offset):axis.x(segment.end,segment.endBeat))-offset;
      if(end<0||x>width)continue;
      const y=cc.bottom-(segment.value??127)/127*cc.span;
      pedal.fillStyle="rgba(92,108,190,.12)";pedal.fillRect(Math.max(0,x),y,Math.min(width,end)-Math.max(0,x),cc.bottom-y);
      pedal.strokeStyle="#5d6fb8";pedal.beginPath();pedal.moveTo(Math.max(0,x),cc.bottom);pedal.lineTo(Math.max(0,x),y);pedal.lineTo(Math.min(width,end),y);pedal.lineTo(Math.min(width,end),cc.bottom);pedal.stroke();
    }
    if(!livePedal&&!data.envelope.length){pedal.fillStyle="#6b6578";pedal.font="11px AstraSans";pedal.fillText("此 MIDI 没有 CC64 踏板信号",12,31);}
    if(!state.project?.editor)AstraRollMeter.marker(ctx,ruler,offset,height);
  }
  stage?.('roll.lanes');
  AstraProjectEditor.draw(ctx,ruler,pedal,offset,height,width);
  stage?.('roll.overlay');
  if(!retain){AstraProjectEditor.drawTimeline(width,offset);stage?.('roll.timeline');}
  window.AstraBeatChecks?.draw(width,offset);
  lastRollLayers=layers;
  const scene=!moving&&AstraProjectEditor.notePatchScene?.();
  rollRaster=scene?{scene,scroll,canvas,velocityCanvas,vh,axis,offset,width,height,viewportHeight,low,high,row,
    ratio,zoom,pitchZoom,path:state.midi.path,grid:gridFrame,data}:null;
}

function activateInstrument(key,next = tab) {
  if (key!==activeInstrument && window.AstraStructureEditor?.guardNavigation()) return;
  if (!instruments[key]) return;
  if(key!==activeInstrument){AstraProjectEditor.cancelNoteEntry?.();AstraProjectEditor.cancelCCContinuation?.();}
  activeInstrument=key;
  changeTab(next);
}

function playScoreMidi(path) {
  const file = midiFiles().find(candidate => candidate.path === path && isScoreMidi(candidate.name));
  if (!file) return;
  activeInstrument = file.instrument || "piano";
  pendingScoreAudition = {path, export:state.project?.export, seen:false};
  changeTab("notes",file);
}

function playPendingScoreMidi() {
  const pending = pendingScoreAudition;
  if (!pending) return;
  if (tab !== "notes" || pending.export !== state.project?.export || (pending.seen && !state.midi.path)) {
    pendingScoreAudition = null;
    return;
  }
  if (pending.path !== state.midi.path) return;
  pending.seen = true;
  if(state.midi.loading||state.midi.preparing)return;
  pendingScoreAudition=null;
  if(state.midi.available&&!state.midi.playing&&!state.midi.play_pending)dispatch("midiPlayPause");
}

// View-tab, left-rail and workspace transition helpers (presentation only;
// navigation stays immediate, non-blocking, and never replays on snapshots).
const VIEW_EASE = "cubic-bezier(.22,1,.36,1)";
const TAB_ORDER = ["tracks","notes","score","files"];
let pendingViewMotion = null, activeViewMotion = null;
let dialogReturnFocus = null, dialogSkipFocusRestore = false, ncmReturnFocus = null;
function prefersReducedMotion() {
  try { return matchMedia("(prefers-reduced-motion: reduce)").matches; } catch { return false; }
}
function clearViewMotion() {
  // The bound stop still owns this slot when it performs cleanup.
  activeViewMotion?.stop();
}
function pulseRailButton(button) {
  if (prefersReducedMotion() || !button) return;
  const iconEl = button.querySelector("svg");
  if (!iconEl?.animate) return;
  for (const anim of iconEl.getAnimations?.() || []) if (anim.id === "astra-rail-pulse") anim.cancel();
  const anim = iconEl.animate(
    [{transform:"scale(1)"},{transform:"scale(.88)",offset:.32},{transform:"scale(1.05)",offset:.72},{transform:"scale(1)"}],
    {duration:210,easing:VIEW_EASE}
  );
  anim.id = "astra-rail-pulse";
  anim.finished?.catch(() => {});
}
function readViewPill(page, moving = false) {
  const tabs = page?.querySelector(".view-navigation .tabs");
  const node = (moving && tabs?.querySelector(".tab-pill-glide")) || tabs?.querySelector('button[aria-selected="true"]');
  if (!node) return null;
  const rect = node.getBoundingClientRect(), frame = tabs.getBoundingClientRect();
  return rect.width > 0 && rect.height > 0
    ? {x:rect.left - frame.left - tabs.clientLeft, y:rect.top - frame.top - tabs.clientTop, w:rect.width, h:rect.height}
    : null;
}
function readViewBody(page) {
  const body = page?.querySelector("#session-body");
  if (!body) return null;
  const style = getComputedStyle(body);
  return {opacity:Number(style.opacity), transform:style.transform};
}
function remainingViewMotion(part) {
  return part ? Math.max(0, part.duration - (part.anim.currentTime ?? 0)) : 0;
}
function carryViewMotion(page, toTab) {
  if (activeViewMotion?.toTab !== toTab) return null;
  const pillMs = remainingViewMotion(activeViewMotion.pill);
  const bodyMs = remainingViewMotion(activeViewMotion.body);
  return {
    toTab, pillMs, bodyMs,
    pill:pillMs > 0 ? readViewPill(page, true) : null,
    body:bodyMs > 0 ? readViewBody(page) : null,
  };
}
function armViewTransition(fromTab, toTab) {
  if (!fromTab || !toTab || fromTab === toTab || prefersReducedMotion() || !Element.prototype.animate) {
    pendingViewMotion = null;
    return;
  }
  const page = $("workspace");
  const dir = TAB_ORDER.indexOf(toTab) < TAB_ORDER.indexOf(fromTab) ? -1 : 1;
  pendingViewMotion = {
    toTab, pillMs:190, bodyMs:185, pill:readViewPill(page,true),
    body:(activeViewMotion?.body && readViewBody(page)) || {opacity:.62,transform:`translate3d(${10 * dir}px,0,0)`},
  };
}
function runViewTransition(page, motion) {
  clearViewMotion();
  if (prefersReducedMotion() || !Element.prototype.animate) return;
  const owner = {toTab:motion.toTab, pill:null, body:null, stop};
  const tabs = page.querySelector(".view-navigation .tabs");
  const target = readViewPill(page);
  if (tabs && target && motion.pillMs > 0 && motion.pill && !matchMedia("(forced-colors: active)").matches) {
    const {x,y,w,h} = target;
    const dx = motion.pill.x - x, dy = motion.pill.y - y;
    if (Math.hypot(dx, dy) > 1 || Math.abs(motion.pill.w - w) > 1 || Math.abs(motion.pill.h - h) > 1) {
      const glide = document.createElement("span");
      glide.className = "tab-pill-glide";
      glide.setAttribute("aria-hidden","true");
      glide.style.cssText = `left:${x}px;top:${y}px;width:${w}px;height:${h}px`;
      tabs.classList.add("tabs-gliding");
      tabs.prepend(glide);
      const anim = glide.animate(
        [{transform:`translate3d(${dx}px,${dy}px,0)`,width:`${motion.pill.w}px`,height:`${motion.pill.h}px`},
         {transform:"none",width:`${w}px`,height:`${h}px`}],
        {duration:motion.pillMs,easing:VIEW_EASE,fill:"both"}
      );
      own("pill", anim, motion.pillMs, () => {
        glide.remove();
        tabs.classList.remove("tabs-gliding");
      });
    }
  }
  const body = page.querySelector("#session-body");
  const pane = page.querySelector(".editor-pane");
  if (body && motion.bodyMs > 0 && motion.body) {
    pane?.classList.add("view-swapping");
    const anim = body.animate(
      [motion.body,{opacity:1,transform:"none"}],
      {duration:motion.bodyMs,easing:VIEW_EASE,fill:"both"}
    );
    own("body", anim, motion.bodyMs, () => pane?.classList.remove("view-swapping"));
  }
  if (owner.pill || owner.body) activeViewMotion = owner;
  function own(key, anim, duration, cleanup) {
    owner[key] = {anim,duration,cleanup};
    const complete = () => {
      if (activeViewMotion !== owner) return;
      release(key);
      if (!owner.pill && !owner.body) activeViewMotion = null;
    };
    anim.finished.then(complete,complete);
  }
  function release(key) {
    const part = owner[key];
    if (!part) return;
    owner[key] = null;
    part.anim.cancel();
    part.cleanup();
  }
  function stop() {
    if (activeViewMotion !== owner) return;
    activeViewMotion = null;
    release("pill");
    release("body");
  }
}
function rememberDialogOpener(el) {
  const opener = document.activeElement;
  return opener instanceof HTMLElement && opener !== document.body && !el.contains(opener) ? opener : null;
}
function restoreDialogOpener(el, opener) {
  if (opener?.isConnected && !opener.disabled && (!document.activeElement || document.activeElement === document.body || el.contains(document.activeElement))) {
    opener.focus({preventScroll:true});
  }
}


let pageTransportSerial=0;
function changeTab(next, requestedMidi = null) {
  if (!projectViewReady(next)) return;
  if (next!==tab && window.AstraStructureEditor?.guardNavigation()) return;
  if (!requestedMidi) pendingScoreAudition = null;
  if (!resultInstruments().includes(activeInstrument)) activeInstrument=resultInstruments()[0] || "piano";
  // Editor previews live in midiFiles(), not resultFiles(): preserve the chosen rendition across tabs.
  const candidates = next === "notes" ? midiFiles().filter(file => file.instrument === activeInstrument) : [];
  const retainedMidi = candidates.find(file => file.path === state.midi.path);
  const workingMidi = candidates.find(file => file.editor);
  const nextMidi = next === "notes" && (requestedMidi || retainedMidi || workingMidi || instrumentFile("midi",activeInstrument) || candidates[0]);
  if (nextMidi && nextMidi.path!==state.midi.path && window.AstraStructureEditor?.guardNavigation()) return;
  // Pages share an output engine, not an implicit audition selection. Navigation
  // pauses at the current position; the roll defaults to its MIDI, tracks to PCM.
  const changingPage=tab!==next;
  const pageScope=changingPage&&state.project?.path&&['tracks','notes','score','files'].includes(tab)&&['tracks','notes','score','files'].includes(next)
    ? {project:state.project.path,fromPage:tab,toPage:next,midiPath:state.midi.path,nextMidi:nextMidi?.path||null,serial:++pageTransportSerial}
    : null;
  if(pageScope)dispatch('navigateTransportPage',pageScope);
  const leaveNativeTracks=Boolean(changingPage && tab==='tracks' && nativeTracksActive);
  const navigationPosition=changingPage
    ? Math.max(0, Number(transportMidi ? state.midi.position : trackAudioClock().position) || 0)
    : 0;
  if (leaveNativeTracks) syncNativeTracksPanel(false);
  if(changingPage){armViewTransition(tab,next);window.AstraWaveSeek?.cancel?.();clearAudioSelection();AstraProjectEditor.cancelNoteEntry?.();AstraProjectEditor.cancelCCContinuation?.();AstraProjectEditor.leaveSelectionView?.();}
  tab = next;
  if (next === "notes") {
    if (nextMidi && nextMidi.path !== state.midi.path) {
      midiSelected = nextMidi.path;
      const pending = pendingScoreAudition;
      dispatch("selectMidi",{path:nextMidi.path},pending ? () => {
        if (pendingScoreAudition === pending && state.midi.path !== pending.path) {
          pendingScoreAudition = null;
          render(true);
        }
      } : undefined);
    }
  }
  if (next === "score") {
    const file=instrumentFile("pdf",activeInstrument);
    if (file && (file.path !== pdfSelected || file.path !== state.pdf.path)) {pdfSelected=file.path;dispatch("selectPdf",{path:file.path});}
  }
  if(changingPage&&(!pageScope||next!=='files')){
    const sources=sourceList();
    const targetSource=next==='notes'&&(nextMidi||state.midi.path)?'midi':
      (sources.find(source=>source.key===state.player.source)?.key||sources[0]?.key);
    if(targetSource){
      auditionSource=targetSource;
      dispatch('selectAuditionSource',{key:targetSource,position:navigationPosition,playing:false,navigation:true,
        ...(pageScope?{pageScope}:{}),
        ...(leaveNativeTracks?{leaveNativeTracks}:{}),
        range:next==='notes'?(window.AstraTransportPanel?.range()||{}):{start:0,end:0,loop:false}});
    }
  }
  playPendingScoreMidi();
  if(changingPage)window.AstraRhythmMotion?.cancel?.();
  render(changingPage||next!=='tracks');
  pendingViewMotion=null;
  $(`tab-${next}`)?.focus({preventScroll:true});
  revealCompactEditor();
}

function revealCompactEditor() {
  if (!compactEditor.matches || !["notes","score"].includes(tab)) return;
  const workspace=$("workspace"), pane=workspace.querySelector(".editor-pane");
  if (pane) workspace.scrollTop+=pane.getBoundingClientRect().top-workspace.getBoundingClientRect().top;
}

compactEditor.addEventListener("change",revealCompactEditor);

function modal(title, body, footer, kind) {
  window.AstraRhythmContextMenu?.close();
  const el = $("dialog");
  const docked=["editor-range","editor-score-range","editor-structure"].includes(kind);
  if(el.open && el.classList.contains("editor-docked") !== docked) el.close();
  if(!el.open && !docked){dialogReturnFocus=rememberDialogOpener(el);dialogSkipFocusRestore=false;}
  dialog = kind;
  el.classList.toggle("editor-docked",docked);
  el.classList.toggle("tempo-dialog",kind === "tempo");
  el.innerHTML = `<header class="dialog-head"><h2 id="dialog-title">${html(title)}</h2>${button("","closeDialog",{style:"icon-button",symbol:"close",label:"关闭"})}</header><div class="dialog-body">${body}</div><footer class="dialog-foot">${footer}</footer>`;
  if (!el.open) { if(docked) el.show(); else el.showModal(); }
  AstraProjectEditor.dockRangePanel?.();
}

function renderGpuChoice() {
  const prompt=state.gpu_preflight, element=$("gpu-choice");
  if (prompt?.status !== "choice") {
    if (element.open) element.close();
    return;
  }
  if (element.open && element.dataset.token === prompt.token) return;
  element.dataset.token=prompt.token;
  const title=prompt.available ? "GPU 正在使用" : "GPU 当前不可用";
  const details=[prompt.name,prompt.available && Number.isFinite(prompt.memory_free_mb) ? `可用显存 ${(prompt.memory_free_mb/1024).toFixed(1)} GB` : ""].filter(Boolean).join(" · ");
  element.innerHTML=`<header class="dialog-head"><h2 id="gpu-title">${title}</h2></header><div class="dialog-body"><p>${html(prompt.reason || "请选择本次任务的计算设备。")}</p>${details ? `<p class="form-note">${html(details)}</p>` : ""}<p class="form-note">本次可改用 CPU，处理时间通常更长。</p></div><footer class="dialog-foot">${button("稍后","chooseExecutionDevice",{key:"cancel",style:"quiet"})}${button("本次使用 CPU","chooseExecutionDevice",{key:"cpu",style:prompt.available ? "secondary" : "primary"})}${prompt.available ? button("继续使用 GPU","chooseExecutionDevice",{key:"gpu",style:"primary"}) : ""}</footer>`;
  if (!element.open) element.showModal();
}

function chooseExecutionDevice(device) {
  const token=state.gpu_preflight?.token;
  if (!token) return;
  if (device === "cancel") pendingTempoSave=null;
  $("gpu-choice").querySelectorAll("button").forEach(control=>{control.disabled=true;});
  dispatch("chooseExecutionDevice",{token,device});
}

$("gpu-choice").addEventListener("cancel",(event) => {
  event.preventDefault();
  chooseExecutionDevice("cancel");
});

function openReview(path = state.project?.reviews?.[0]?.path) {
  if (!path) return;
  dispatch("readReview",{path},() => {
    const review=state.review_document;
    if (review?.path !== path) return;
    modal("已导入的审核意见",`<label class="field">审核版本<select class="control" id="review-select"></select></label><p class="form-note">审核意见只作记录。该导出和原始 MIDI 保持原样。</p><div id="review-groups" class="review-groups"></div>`,button("关闭","closeDialog"),"review");
    $("review-select").replaceChildren(...state.project.reviews.map((record) => new Option(`${record.audit_export} · ${record.group_count} 组 / ${record.comment_count} 组有文字`,record.path,false,record.path===path)));
    const decisions = {keep:"差不多",shorter:"拖得太长",longer:"结束太早",unsure:"不确定",other:"其他"};
    for (const group of review.groups) {
      const row=document.createElement("article");
      const heading=document.createElement("h3");
      const comment=document.createElement("p");
      heading.textContent=`第 ${group.group_id} 组 · ${decisions[group.decision] || "仅文字反馈"}`;
      comment.textContent=group.comment || "";
      row.append(heading);
      if (comment.textContent) row.append(comment);
      $("review-groups").append(row);
    }
  });
}

function showReviewNotice(notice) {
  if (!notice || notice.token === reviewNoticeToken) return;
  reviewNoticeToken=notice.token;
  const toast=$("review-toast");
  toast.textContent=notice.text;
  toast.hidden=false;
  clearTimeout(reviewNoticeTimer);
  reviewNoticeTimer=setTimeout(() => { toast.hidden=true; },7000);
}

function openHarmony() {
  const data=state.project?.harmony?.[activeInstrument];
  if (!data?.key_regions?.length) return;
  const regions=data.key_regions.map(region=>`<tr><td>${html(region.start_measure)}–${html(region.end_measure)}</td><td>${html(region.label)}</td><td>${region.uncertain ? "待确认" : "多种调性轮廓支持"}</td></tr>`).join("");
  const chords=(data.chords || []).map(chord=>`<tr><td>${html(chord.measure)} · ${html(chord.beat)}</td><td>${html(chord.label)}${chord.uncertain ? "（候选）" : ""}</td><td>${html(chord.roman || "—")}</td></tr>`).join("");
  const file=state.project.files.find(file=>file.name===`${activeInstrument}_harmony.json`);
  const review=(data.notation_review || []).map(item=>`<li>第 ${html(item.bars)} 小节：${html(item.message)}</li>`).join("");
  const confirmation=data.global_key?.user_confirmation;
  const confirmedMode={major:"大调",minor:"小调"}[confirmation?.mode];
  const confirmed=confirmation?.source === "user" && confirmedMode && confirmation.tonic;
  modal("调性与和弦",`${confirmed ? `<div class="harmony-confirmed"><strong>主调 ${html(confirmed)} ${confirmedMode}</strong><span class="status-token ready">${icon("check")}用户已确认</span></div>` : ""}<p class="form-note">调性分段与和弦来自转录声部的分析，仍可按听感复核。主调确认仅适用于整首，分段结果独立判断。</p>
    <h3>调性分段</h3><table class="harmony-table"><thead><tr><th>小节</th><th>调性</th><th>依据</th></tr></thead><tbody>${regions}</tbody></table>
    ${review ? `<h3>需要核对</h3><ul class="form-note">${review}</ul>` : ""}
    <h3>和弦</h3><p class="form-note">分析当前钢琴声部；分解和弦、转位与变化位置仍可能遗漏或误判。乐谱页可直接开启或关闭已有和弦记号，无需重新分析音频。</p>
    ${chords ? `<table class="harmony-table"><thead><tr><th>小节 · 拍</th><th>和弦</th><th>级数参考</th></tr></thead><tbody>${chords}</tbody></table>` : `<p>当前声部没有足够完整的和弦证据。</p>`}`,
    `${file ? button("完整分析","openFile",{path:file.path,symbol:"external"}) : ""}${tab==="score"?"":harmonyUpdateNotice()}${audioHarmonyControl()}${button("关闭","closeDialog")}`,"harmony");
}

function projectSettingsMarkup(busy = false) {
  advancedProject=state.project?.path??null;
  const opts = state.options;
  const directMidi = Boolean(state.input_midi || state.project?.input_kind==="midi");
  const audioOnly = !directMidi && guitarOnly(isResult() ? state.project.instruments : importedAudioTargets());
  advancedOriginal = directMidi || audioOnly ? {output:opts.output} : {output:opts.output,tempo_path:opts.tempo_path,bpm:opts.bpm};
  const option = (value, label, current) => `<option value="${html(value)}" ${String(value) === String(current) ? "selected" : ""}>${html(label)}</option>`;
  const output = `<label class="field full">输出目录<span class="field-line"><input class="control" id="option-output" value="${html(opts.output)}">${button("浏览","chooseOutput")}</span></label>`;
  const group = (title,body,tag="") => `<fieldset class="settings-group full"><legend>${title}${tag?`<span class="scope-tag">${tag}</span>`:""}</legend><div class="dialog-grid">${body}</div></fieldset>`;
  const analysisFields = directMidi ? "" : rhythmProviderFields(opts);
  const apply=button("应用项目设置","saveAdvanced",{style:"primary",disabled:busy});
  const end=`<p class="form-error" id="project-settings-error" role="alert"></p><p id="project-settings-status" role="status"></p><div class="settings-page-actions">${apply}</div>`;
  if (audioOnly) return `<fieldset class="preferences-section" ${busy?"disabled":""}>${group("输出",output)}</fieldset>${end}`;
  return `<fieldset class="preferences-section" ${busy?"disabled":""}>${group("输出",output)}
    ${directMidi ? "" : group("分析",`${analysisFields}<label class="field">钢琴分轨模型<select class="control" id="option-piano_model">${[["xlance","Xlance · 专用钢琴（默认）"],["sw","BS-RoFormer SW · 六分轨"]].map(([value,text])=>option(value,text,opts.piano_model)).join("")}</select></label><label class="check-line"><input id="option-keep_stems" type="checkbox" ${opts.keep_stems ? "checked" : ""}>保留钢琴分轨音频</label>`)}
    ${directMidi ? "" : group("速度与拍号",`<label class="field full">速度图 JSON 或 MIDI<span class="field-line"><input class="control" id="option-tempo_path" value="${html(opts.tempo_path)}" placeholder="沿用项目或自动分析">${button("浏览","chooseTempo")}</span></label>
    <label class="field">固定 BPM<input class="control" id="option-bpm" type="number" step="any" min="10" max="600" value="${html(opts.bpm)}" placeholder="可选"></label>
    <label class="field">固定拍号<input class="control" id="option-meter" value="${html(opts.meter)}" placeholder="4/4"></label>
    <label class="field">节拍倍率<select class="control" id="option-beat_scale" title="只改变节拍解释，不改变播放时长">${[[1,"保持 ×1"],[.5,"半速解释 ×0.5"],[2,"倍速解释 ×2"]].map(([value,text])=>option(value,text,opts.beat_scale)).join("")}</select></label>`,"仅本次")}
    ${group("导出与记谱",`<label class="field">导出量化<select class="control" id="option-quantization" title="写入导出 MIDI / 乐谱的量化；与卷帘拖动吸附网格无关">${Object.entries(grids).map(([value,text])=>option(value,text,opts.quantization)).join("")}</select></label>
    <label class="field">钢琴导出格式<select class="control" id="option-mode">${Object.entries(formats).map(([value,text])=>option(value,text,opts.mode)).join("")}</select></label>
    <label class="field full">钢琴制谱<select class="control" id="option-score_engine">${[["duet","DuET · AI 记谱（本地）"],["music21","music21 · 原有制谱"]].map(([value,text])=>option(value,text,opts.score_engine)).join("")}</select></label>
    <label class="field full" title="只影响运行速度，写出的谱完全相同">DuET 运行设备<select class="control" id="option-score_duet_device">${[["cpu","CPU（推荐，本机实测更快）"],["cuda","显卡 GPU"]].map(([value,text])=>option(value,text,opts.score_duet_device||"cpu")).join("")}</select></label>
    <label class="check-line full"><input id="option-score_pedal" type="checkbox" ${opts.score_pedal ? "checked" : ""}>DuET 踏板记号</label>
    <label class="check-line full"><input id="option-score_chords" type="checkbox" ${opts.score_chords ? "checked" : ""}>导出和弦记号（乐谱、MIDI）</label>
`)}
    </fieldset>${end}`;
}

function saveAdvanced() {
  const error=$("project-settings-error"),status=$("project-settings-status");
  if(dialog!=="app-preferences"||!$("dialog").open||!error)return;
  error.textContent="";
  status.textContent="";
  const options = Object.fromEntries(["output","tempo_path","bpm","meter","beat_scale","quantization","mode","piano_model","rhythm_provider","score_engine","score_voice_mode","score_duet_device"].filter(key => $(`option-${key}`)).map(key => [key,$(`option-${key}`).value.trim()]));
  if (options.tempo_path && options.bpm) { error.textContent = "请选择速度图或固定 BPM 中的一项。"; return; }
  if (options.tempo_path && !/\.(?:json|midi?)$/i.test(options.tempo_path)) { error.textContent = "速度图须为 JSON 或 MIDI 文件。"; return; }
  if ("meter" in options) options.meter ||= "4/4";
  if ((options.bpm && !(Number(options.bpm) >= 10 && Number(options.bpm) <= 600)) || ("meter" in options && !/^(?:[1-9]|[12]\d|3[0-2])\/(?:1|2|4|8|16|32|64|128)$/.test(options.meter))) {
    error.textContent = "固定 BPM 应在 10 到 600 之间，拍号示例为 4/4 或 6/8。"; return;
  }
  if ("bpm" in options) options.bpm = options.bpm ? Number(options.bpm) : null;
  if ("tempo_path" in options) options.tempo_path ||= null;
  if ("beat_scale" in options) options.beat_scale = Number(options.beat_scale);
  if ($("option-keep_stems")) options.keep_stems = $("option-keep_stems").checked;
  if ($("option-score_pedal")) options.score_pedal = $("option-score_pedal").checked;
  if ($("option-score_chords")) options.score_chords = $("option-score_chords").checked;
  dispatch("updateOptions",{options,project_scope:advancedProject},() => {
    if (dialog !== "app-preferences" || !$("dialog").open || !$("project-settings-error")) return;
    if (state.job.error) error.textContent = state.job.error;
    else { if(advancedOriginal)for(const key of Object.keys(advancedOriginal))advancedOriginal[key]=state.options[key]; status.textContent="已应用"; }
  });
}

function showTempo(fresh = false) {
  if (!isResult() || !state.project?.tempo || state.job.running || state.gpu_preflight) return;
  if (fresh) {
    tempoBaseExport = state.project.export;
    tempoDraft = JSON.parse(JSON.stringify(state.project.tempo));
    tempoTab = "quick";
    tempoUndo = [];
    meterPreviewPending = false;
    const input = {seconds:audioMeterPosition(),next:"",previous:""};
    meterQuick = {...input,accepted:input,notice:""};
  }
  const tabs = `<div class="tempo-tabs segments" aria-label="速度图编辑方式">${[["quick","快速修正"],["anchors","速度锚点"],["meter","拍号与弱起"],["json","专家 JSON"]].map(([key,title]) => `<button type="button" data-do="tempoTab" data-key="${key}" aria-pressed="${tempoTab === key}">${title}</button>`).join("")}</div>`;
  const footer = `${button("取消","closeDialog")}${button("应用并仅重导出","saveTempo",{style:"primary",symbol:"retry",disabled:meterPreviewPending || meterInputChanged()})}`;
  if (tempoTab === "quick") {
    const select = (current, choices) => choices.map(([value,label]) => `<option value="${value}" ${current === value ? "selected" : ""}>${label}</option>`).join("");
    const meters = ["2","3","4"].map(value=>[value,`${value}/4`]);
    modal("结果修正 · 节拍与拍号",`${tabs}<section class="tempo-quick" aria-label="快速修正小节线">
      <h3>从这里开始新的小节</h3>
      <p class="form-note">在分轨页定位，或手填原音频秒数。修正保留后面的变速，只重导出已有结果。</p>
      <label class="field">时间（秒）<span class="field-line"><input class="control" id="quick-meter-seconds" type="number" min="0" step="0.001" value="${html(meterQuick.seconds)}" placeholder="例如 42.375">${button("用原音频位置","captureMeterPosition",{symbol:"clock",disabled:!audioMeterPosition()})}</span></label>
      <label class="field">从这里开始<select class="control" id="quick-meter-next">${select(meterQuick.next,[["","只设为小节第一拍"],...meters])}</select></label>
      <details id="quick-meter-details"${meterQuick.previous ? " open" : ""}><summary>前一小节长度需要调整</summary>
        <label class="field">前一小节<select class="control" id="quick-meter-previous">${select(meterQuick.previous,[["","沿用已有拍号"],...meters,["pickup","曲首弱起"]])}</select></label>
      </details>
      <div class="tempo-quick-actions">${button("预览修正","previewMeterEdit",{symbol:"check"})}${button("撤销上一步","undoMeterEdit",{disabled:!tempoUndo.length})}</div>
      <p class="form-note tempo-quick-notice" role="status">${html(meterInputChanged() ? "输入已更改，请先预览修正。" : meterQuick.notice)}</p>
      <p class="form-error" id="form-error" role="alert"></p>
    </section>`,footer,"tempo");
    return;
  }
  const isAnchor = tempoTab === "anchors";
  const key = isAnchor ? "anchors" : "time_signatures";
  const rows = tempoDraft[key] || [];
  const numeric = (field,value,index,readonly=false) => `<input class="control tempo-input" type="number" step="any" data-field="${field}" data-index="${index}" aria-label="第 ${index+1} 行 ${html(({seconds:"时间秒",beat:"四分拍位",numerator:"拍号分子",denominator:"拍号分母"})[field])}" value="${html(value)}" ${readonly ? "readonly" : ""}>`;
  const bpm = (index) => {
    const a = rows[index], b = rows[index+1];
    return b && b.seconds > a.seconds && b.beat > a.beat ? (60*(b.beat-a.beat)/(b.seconds-a.seconds)).toFixed(1) : "—";
  };
  const table = tempoTab === "json" ? `<textarea class="control tempo-json" id="tempo-json" aria-label="速度图 JSON" spellcheck="false">${html(JSON.stringify(tempoDraft,null,2))}</textarea>` : `<div class="tempo-table-wrap"><table class="tempo-table"><thead><tr>${isAnchor ? "<th>时间（秒）</th><th>拍位（四分拍）</th><th>区间 BPM</th>" : "<th>起始拍位</th><th>拍号分子</th><th>拍号分母</th>"}<th></th></tr></thead><tbody>${rows.map((row,index) => `<tr>${isAnchor ? `<td>${numeric("seconds",row.seconds,index,!index)}</td><td>${numeric("beat",row.beat,index,!index)}</td><td id="anchor-bpm-${index}">${bpm(index)}</td>` : `<td>${numeric("beat",row.beat,index,!index)}</td><td>${numeric("numerator",row.numerator,index)}</td><td>${numeric("denominator",row.denominator,index)}</td>`}<td>${button("","removeTempoRow",{style:"icon-button",symbol:"close",key:String(index),disabled:!index || rows.length <= (isAnchor ? 2 : 1),label:`删除第 ${index+1} 行`})}</td></tr>`).join("")}</tbody></table></div><div class="tempo-add">${button(isAnchor ? "添加锚点" : "添加拍号","addTempoRow",{symbol:"plus"})}</div>${isAnchor ? "" : `<label class="field check-line">弱起长度（四分拍）<input class="control" id="pickup" type="number" min="0" step="any" value="${html(tempoDraft.pickup_beats ?? 0)}"></label>`}`;
  const hint = isAnchor ? "首锚点固定为 0 秒、0 拍。后续秒数与拍位须逐行增加。" : tempoTab === "meter" ? "变拍号应落在小节线上。没有弱起时填 0。" : "可直接修改完整速度图。保存前会验证锚点和拍号。";
  const fixedMeter = tempoTab === "meter" ? `<div class="tempo-fit-row">${button("整曲固定为首行拍号","fixMeter",{disabled:rows.length<2})}<span>只清除后续拍号变化，保留速度锚点和弱起。</span></div>` : "";
  const estimate = tempoDraft.constant_bpm && tempoDraft.fit ? `<p class="form-note">恒速估算 ${html(tempoDraft.constant_bpm)} BPM · 95% 节拍误差 ${Math.round(tempoDraft.fit.p95_seconds*1000)} ms。首行短前导段用于音频对齐。</p>` : "";
  modal("结果修正 · 节拍与拍号",`${tabs}<div class="tempo-fit-row">${button("估算恒定 BPM","estimateConstantTempo",{symbol:"clock",disabled:state.job.running})}<span>仅用于你已确认不变速的曲目；保存后才应用。</span></div>${estimate}${table}${fixedMeter}<p class="form-note">${hint}</p>${meterInputChanged() ? '<p class="form-note">快速修正有新输入，请返回预览后再应用。</p>' : ""}<p class="form-error" id="form-error" role="alert"></p>`,footer,"tempo");
}

function audioMeterPosition() {
  const {duration,position,playing} = state.player;
  if (transportMidi || state.midi.playing || !(duration > 0)) return "";
  const local = localSeekPosition();
  if (local !== null) return Math.min(duration,local).toFixed(3);
  const elapsed = playing && audioClock.source === state.player.source ? Math.max(0,(performance.now()-audioClock.at)/1000) : 0;
  return Math.min(duration,(position || 0)+Math.min(.5,elapsed)).toFixed(3);
}

function meterInputChanged() {
  const {accepted,seconds,next,previous} = meterQuick;
  return !!accepted && (accepted.seconds !== seconds || accepted.next !== next || accepted.previous !== previous);
}

function updateMeterControls() {
  const root = $("dialog");
  const setDisabled = (action,disabled) => { root.querySelector(`[data-do="${action}"]`).disabled = disabled; };
  for (const field of ["seconds","next","previous"]) $(`quick-meter-${field}`).disabled = meterPreviewPending;
  setDisabled("captureMeterPosition",meterPreviewPending || !audioMeterPosition());
  setDisabled("undoMeterEdit",meterPreviewPending || !tempoUndo.length);
  setDisabled("previewMeterEdit",meterPreviewPending);
  setDisabled("saveTempo",meterPreviewPending || meterInputChanged());
  root.querySelector(".tempo-quick-notice").textContent = meterPreviewPending ? "正在预览修正…" :
    meterInputChanged() ? "输入已更改，请先预览修正。" : meterQuick.notice;
}

function quickMeterAction(action) {
  if (meterPreviewPending || pendingTempoSave || tempoBaseExport !== state.project?.export) return;
  readTempo();
  if (action === "captureMeterPosition") {
    const seconds = audioMeterPosition();
    if (!seconds) return;
    $("quick-meter-seconds").value = meterQuick.seconds = seconds;
    $("form-error").textContent = "";
    updateMeterControls();
    return;
  }
  if (action === "undoMeterEdit") {
    ++tempoEditSerial;
    const previous = tempoUndo.pop();
    if (previous) {
      tempoDraft = previous.draft;
      meterQuick = {...previous.input,accepted:previous.input,notice:"已撤销上一次快捷修正。"};
      showTempo();
    }
    return;
  }
  if (!meterQuick.seconds.trim() || !Number.isFinite(Number(meterQuick.seconds))) {
    $("form-error").textContent = "请输入有效的时间（秒）。";
    return;
  }
  if (!bridge?.previewMeterEdit) return;
  const serial = ++tempoEditSerial;
  const {seconds,next,previous} = meterQuick;
  const request = {export:tempoBaseExport,data:tempoDraft,seconds:Number(seconds),
    numerator:meterQuick.next ? Number(meterQuick.next) : null,
    previous:meterQuick.previous === "pickup" ? "pickup" : Number(meterQuick.previous) || null};
  meterPreviewPending = true;
  $("form-error").textContent = "";
  updateMeterControls();
  bridge.previewMeterEdit(JSON.stringify(request), raw => {
    if (serial !== tempoEditSerial || dialog !== "tempo" || !$("dialog").open || tempoTab !== "quick") return;
    meterPreviewPending = false;
    readTempo();
    updateMeterControls();
    if (seconds !== meterQuick.seconds || next !== meterQuick.next || previous !== meterQuick.previous) return;
    let result;
    try { result = JSON.parse(raw); } catch { result = {error:"无法读取修正预览。"}; }
    if (result.error) {
      $("form-error").textContent = result.error;
      if (result.error.includes("前一小节")) $("quick-meter-details").open = true;
      return;
    }
    tempoUndo.push({draft:tempoDraft,input:meterQuick.accepted});
    if (tempoUndo.length > 24) tempoUndo.shift();
    tempoDraft = result.data;
    meterQuick.accepted = {seconds,next,previous};
    const edit = result.edit;
    const shift = Number(edit.beat_adjustment?.toFixed(3));
    meterQuick.notice = edit.pickup_beats == null ?
      `${edit.seconds.toFixed(3)} 秒已设为小节第一拍，从这里使用 ${edit.numerator}/${edit.denominator}。尚未应用。` :
      `预计弱起 ${edit.pickup_beats} 拍${shift ? `（拍长调整 ${shift > 0 ? "+" : ""}${shift} 拍）` : ""}。${edit.seconds.toFixed(3)} 秒为首个完整小节第一拍。后续变速保留，尚未应用。`;
    showTempo();
  });
}

function readTempo() {
  if (tempoTab === "quick") {
    meterQuick.seconds = $("quick-meter-seconds").value;
    meterQuick.next = $("quick-meter-next").value;
    meterQuick.previous = $("quick-meter-previous").value;
    return;
  }
  if (tempoTab === "json") { tempoDraft = JSON.parse($("tempo-json").value); return; }
  const rows = tempoDraft[tempoTab === "anchors" ? "anchors" : "time_signatures"];
  for (const input of document.querySelectorAll(".tempo-input")) rows[input.dataset.index][input.dataset.field] = input.value.trim() === "" ? null : Number(input.value);
  if ($("pickup")) tempoDraft.pickup_beats = $("pickup").value.trim() === "" ? null : Number($("pickup").value);
}

function validTempo() {
  const {anchors,time_signatures:meters,pickup_beats:pickup=0} = tempoDraft || {};
  if (!Array.isArray(anchors) || anchors.length < 2 || anchors[0]?.seconds !== 0 || anchors[0]?.beat !== 0 || anchors.some((a,i) => !a || !Number.isFinite(a.seconds) || !Number.isFinite(a.beat) || (i > 0 && (a.seconds <= anchors[i-1].seconds || a.beat <= anchors[i-1].beat)))) throw Error("至少需要两个锚点。首锚点固定为 0 秒、0 拍，后续值须严格递增。");
  if (!Array.isArray(meters) || !meters.length || meters[0]?.beat !== 0 || meters.some((m,i) => !m || !Number.isFinite(m.beat) || (i && m.beat <= meters[i-1].beat) || !Number.isInteger(m.numerator) || m.numerator < 1 || m.numerator > 32 || ![1,2,4,8,16,32,64,128].includes(m.denominator))) throw Error("拍号表必须从第 0 拍开始，拍位严格递增，拍号格式须有效。");
  if (!Number.isFinite(pickup) || pickup < 0 || pickup >= 4*meters[0].numerator/meters[0].denominator) throw Error("弱起应在 0 到首小节长度之间。");
}

function tempoAction(action,key) {
  try {
    readTempo();
    if (action === "saveTempo") {
      if (meterPreviewPending || pendingTempoSave || tempoBaseExport !== state.project?.export) return;
      if (meterInputChanged()) throw Error("输入已更改，请先预览修正。");
      validTempo();
      if (tempoTab === "quick" && !tempoUndo.length && JSON.stringify(tempoDraft) === JSON.stringify(state.project.tempo)) {
        throw Error("请先预览一处修正，再应用。");
      }
      pendingTempoSave = tempoBaseExport;
      dispatch("saveTempo",{data:tempoDraft,export:tempoBaseExport});
      return;
    }
    if (action === "tempoTab") { if (tempoTab === "json") validTempo(); ++tempoEditSerial; meterPreviewPending = false; tempoTab = key; }
    const list = tempoDraft[tempoTab === "anchors" ? "anchors" : "time_signatures"];
    if (action === "removeTempoRow") list.splice(Number(key),1);
    if (action === "fixMeter" && tempoTab === "meter") list.splice(1);
    if (action === "addTempoRow") {
      const last = list.at(-1);
      if (tempoTab === "anchors") {
        const previous = list.at(-2);
        list.push({seconds:last.seconds+(last.seconds-previous.seconds),beat:last.beat+(last.beat-previous.beat)});
      } else {
        const beat = list.length === 1 && tempoDraft.pickup_beats > 0 ? tempoDraft.pickup_beats : last.beat+4*last.numerator/last.denominator;
        list.push({...last,beat});
      }
    }
    showTempo();
  } catch (error) { $("form-error").textContent = error.message; }
}

function renderLogs() {
  if (!$("logs").open) return;
  const pre = $("log-lines");
  const value = logs.length ? logs.join("\n") : "处理步骤会显示在这里。";
  if (pre.textContent !== value) { pre.textContent = value; pre.scrollTop = pre.scrollHeight; }
  $("logs").querySelector("[data-do='openLog']").disabled = !state.job.log_path;
}

function patchOptions(options) {
  if(!bridge)return;
  if(state.input_midi||isResult()||separationAudition()){
    Object.assign(state.options,options);
    dispatch("updateOptions",{options});
    render();
    return;
  }
  let pending=currentHomeOptions();
  if(!pending)pending=pendingHomeOptions={navigation:studioNavigationSerial,audio:state.audio?.path,
    project:state.project?.path,options:{},revision:0,run:false};
  Object.assign(pending.options,options);
  const revision=++pending.revision;
  const expected={...pending.options};
  syncHomeConfig();
  dispatch("updateOptions",{options:expected},()=>{
    if(currentHomeOptions()!==pending||pending.revision!==revision)return;
    const confirmed=!state.job.error&&Object.entries(expected).every(([key,value])=>
      Array.isArray(value)?value.join()===state.options[key]?.join():value===state.options[key]);
    pendingHomeOptions=null;
    syncHomeConfig();
    if(confirmed&&pending.run&&!homeRunDisabled(state.options.instruments))dispatch("run");
  });
}

function queueHomeRun() {
  const pending=currentHomeOptions();
  if(!pending)return false;
  pending.run=true;
  syncHomeConfig();
  return true;
}

function withPlayer(audioAction, midiAction, payload) {
  if(tab==='files'&&state.project?.path)return;
  dispatch(transportMidi ? midiAction : audioAction,payload);
}

function armSeekPreview(action, requested, waveCommit=false) {
  const kind=action==='midiSeek'?'midi':'player', player=state[kind];
  if ((kind==='midi')!==transportMidi || !Number.isFinite(Number(requested))) return;
  const selecting=kind==='player' && audioSelectionPending && audioSelectionValid(audioSelectionPending.request)
    ? sourceList().find(source=>source.key===audioSelectionPending.key) : null;
  const now=performance.now(),position=Math.max(0,Math.min(selecting?.duration||player.duration||Infinity,Number(requested)));
  seekPending={key:transportKey,position,sent:now,until:now+1500,order:player.transport_order};
  if(kind==='player'&&player.native_audition){
    seekPending.nativeRevision=player.seek_requested;
    seekPending.until=now+5500;
  }
  if(selecting){const targetKey=`audio:${selecting.path||''}`;if(targetKey!==transportKey)seekPending.targetKey=targetKey;}
  if(waveCommit){
    const commit=seekPending;
    commit.wave=true;
    sendWaveSeekVisual(2,position);
    setTimeout(()=>{
      if(seekPending===commit){
        seekPending=null;clearWaveSeekVisual();repaintTrackSeek();
      }
    },Math.max(0,commit.until-now)+20);
  }
  paintPlaybackPosition(position);
  if (kind==='midi') updateMidiCursor(now); else updateAudioCursor(now);
}

function armAuditionPreview(key,position) {
  if(transportMidi)return;
  const source=sourceList().find(source=>source.key===key);
  if(!source)return;
  armSeekPreview('seek',position);
  if(seekPending){
    const targetKey=`audio:${source.path || ""}`;
    if(targetKey!==transportKey)seekPending.targetKey=targetKey;
  }
}

function orderedPlayback(kind, update) {
  const previous=state[kind];
  // A poll/snapshot captured before a newer transport signal must not rewind it.
  if(kind==='player'&&update?.cpp_route>0&&update.cpp_route===previous?.cpp_route&&
     Number.isFinite(update.cpp_heard)&&Number.isFinite(previous.cpp_heard)&&
     Number.isFinite(update.sampled_at)&&Number.isFinite(previous.sampled_at)){
    const order=compareTrackClocks({heard:update.cpp_heard,sampledAt:update.sampled_at},
      {heard:previous.cpp_heard,sampledAt:previous.sampled_at});
    if(order)return order<0?previous:update;
  }
  if (Number.isFinite(update?.transport_order) && Number.isFinite(previous?.transport_order)
      && update.transport_order < previous.transport_order) return previous;
  return update;
}

function localSeekPosition(now = performance.now()) {
  const wavePosition=window.AstraWaveSeek?.position?.();
  if(!transportMidi && wavePosition!==null && wavePosition!==undefined)return wavePosition;
  const editorPosition=transportMidi ? window.AstraProjectEditor?.seekPosition?.() : null;
  if(editorPosition!==null && editorPosition!==undefined)return editorPosition;
  if (seekDrag?.key === transportKey) return seekDrag.position;
  if (seekPending?.key === transportKey && now < seekPending.until) return seekPending.position;
  return nativeSeekPosition(now);
}

function paintPlaybackPosition(position,panelAlreadyPainted=false) {
  // The Win32 rail paints its own clock. The hidden HTML transport has no
  // visible state to update each frame while the native slot owns Tracks.
  if(tab==='tracks'&&!transportMidi&&nativeOwnsTransport())return;
  if(!panelAlreadyPainted)window.AstraTransportPanel?.position(position);
  const text=time(position), label=$("time"), seek=$("seek");
  if (label.textContent !== text) label.textContent=text;
  if (!seekDrag && !seek.disabled && Math.abs(Number(seek.value)-position) >= .01) seek.value=position;
}

function updatePlaybackPosition(kind,paint=true) {
  if ((kind === "midi") !== transportMidi) return;
  const player=state[kind], position=player.position || 0, now=performance.now();
  if (seekPending?.key === transportKey) {
    const elapsed=Math.max(0,(now-seekPending.sent)/1000),tolerance=player.playing ? .03+elapsed*(player.rate||1) : .003;
    const newer=seekPending.order===undefined || (player.transport_order??-Infinity)>seekPending.order;
    const tracked=seekPending.nativeRevision!==undefined&&player.native_audition;
    const direct=seekPending.cpp;
    const heard=direct
      ? nativeSeekConfirmed(direct)
      : tracked
      ? player.seek_requested>seekPending.nativeRevision && player.seek_heard>=player.seek_requested
        && Math.abs(player.seek_target-seekPending.position)<.001
      : Math.abs(position-seekPending.position)<=tolerance;
    if (now >= seekPending.until || (tracked&&player.seek_failed) || (!seekPending.targetKey && (direct || newer) && heard)){
      if(seekPending.wave&&!waveSeekActive())clearWaveSeekVisual();
      seekPending=null;
    }
  }
  if(paint)paintPlaybackPosition(localSeekPosition(now) ?? position);
}

// The two Notes seek surfaces commit on release through this same ordered
// WebChannel dispatch. Pointer previews never update the playback run anchor.
function commitNotesSeek(position) {
  if(tab!=='notes'||!Number.isFinite(position)||position<0)return;
  dispatch('midiSeek',{position});
}

function sendSeek(position, final) {
  if (!bridge || $("seek").disabled) return;
  const now=performance.now();
  if (final) {
    if (lastSeekCommit?.key === transportKey && lastSeekCommit.position === position && now-lastSeekCommit.sent < 80) return;
    lastSeekCommit={key:transportKey,position,sent:now};
    if(tab==='notes'&&!scoreTransportPage())return commitNotesSeek(position);
    armSeekPreview(transportMidi ? "midiSeek" : "seek",position);
    refreshUntil=Math.max(refreshUntil,now+1050);
    schedulePoll();
  }
  const request=JSON.stringify({action:transportMidi ? "midiSeek" : "seek",position});
  const send=()=>{
    if(final&&typeof bridge.dispatchPlayback==='function')bridge.dispatchPlayback(request,acceptPlaybackReceipt);
    else bridge.dispatch(request);
  };
  if(!final||!submitNativeTrack('seek',{position},send))send();
}

function previewSeek(position) {
  if (!seekDrag) return;
  seekDrag.position=position;
  if (!seekFrame) seekFrame=requestAnimationFrame(() => {
    seekFrame=0;
    if (!seekDrag) return;
    paintPlaybackPosition(seekDrag.position);
    if (transportMidi) updateMidiCursor();
    else updateAudioCursor();
  });

}

function finishSeek() {
  if (!seekDrag) return;
  const position=Number($("seek").value);
  seekDrag=null;
  sendSeek(position,true);
  paintPlaybackPosition(position);
  if (transportMidi) updateMidiCursor();
  else updateAudioCursor();
  if (deferredSeekRender) requestAnimationFrame(() => render());
}

$("seek").addEventListener("pointerdown",(event) => {
  if (!event.isPrimary || event.button !== 0 || $("seek").disabled) return;
  seekDrag={key:transportKey,pointer:event.pointerId,position:Number($("seek").value),lastSent:performance.now()};
});
for (const event of ["pointerup","pointercancel"]) {
  window.addEventListener(event,(release) => {
    const gesture=seekDrag;
    if(gesture?.pointer!==release.pointerId)return;
    if(tab==='notes'){
      if(event==='pointerup')finishSeek();
      else seekDrag=null;
      return;
    }
    requestAnimationFrame(() => {
      if(seekDrag===gesture)finishSeek();
    });
  },true);
}
window.addEventListener("blur",()=>{
  if(tab==='notes')seekDrag=null;
  else finishSeek();
});

// The whole track label area selects its source. Keep child buttons independent
// and retain one native keyboard tab stop (the track-name button), not nested buttons.
function trackControlTarget(element){
  const explicit=element?.closest?.('[data-do]');
  if(explicit)return explicit;
  if(element?.closest?.('button,a,input,select,textarea,[contenteditable]'))return null;
  return element?.closest?.('.track-controls')?.querySelector('.track-name[data-do="selectAudio"]')||null;
}
document.addEventListener("click", (event) => {
  if (["midi-canvas","ruler-canvas","pedal-canvas","chord-canvas","waveform-canvas"].includes(event.target.id)) {
    if(AstraProjectEditor.consumeClick(event))return;
    const rect = event.target.getBoundingClientRect();
    let note=null;
    if (event.target.id === "midi-canvas" && rollGeometry) {note=AstraNoteInspector.selectAt(event,rollGeometry);requestRollDraw();}
    const position=midiSeconds(event.clientX-rect.left+($("roll-scroll")?.scrollLeft||0));
    if (!state.project?.editor && ["midi-canvas","ruler-canvas"].includes(event.target.id) && AstraRollMeter.pick(note,position)) {
      suspendRollFollow();
      dispatch("midiSeek",{position:note?.start??position});
    } else if(event.target.id==="ruler-canvas"&&tab==="notes")commitNotesSeek(position);
    else dispatch("midiSeek",{position});
    return;
  }
  const target = trackControlTarget(event.target);
  if (!target || target.disabled) return;
  if (target.classList.contains("rail-button")) pulseRailButton(target);
  const {do:action,key,path} = target.dataset;
  // Opening the source list is not navigation; the choice itself is guarded.
  if (action === "midiSourceMenu") { midiSourceMenu.toggle(target); return; }
  if (window.AstraStructureEditor?.guardAction(action)) return;
  if (action === "tab") {if(key!==tab)changeTab(key);}
  else if(action==='openRhythmCandidates'){
    const path=state.project?.editor?.preview_path||state.project?.editor?.path;if(!path)return;
    tab='notes';activeInstrument='piano';midiSelected=path;
    dispatch('selectMidi',{path},()=>{render(true);AstraProjectEditor.action('editorRhythmCandidates');});
  }
  else if (action.startsWith("structure")) AstraStructureEditor.action(action,key);
  else if (action.startsWith("ornament")) AstraOrnamentReview.action(action,key);
  else if (action.startsWith("editor")) AstraProjectEditor.action(action,key);
  else if (action.startsWith("rollMeter")) AstraRollMeter.action(action);
  else if (action === "viewTranscriptionCandidate") {
    const candidate=resultFiles("midi").find(file=>file.path===path && file.name==="piano_transcription.mid");
    if(candidate)changeTab("notes",candidate);
  }
  else if (action === "prepareTranscriptionCandidate") openTranscriptionCandidate();
  else if (action === "confirmTranscriptionCandidate") confirmTranscriptionCandidate();
  else if (action === "updateForHarmony") {
    const request=harmonyUpdateRequest();
    if(!request)return;
    if(dialog === "harmony" && $("dialog").open)$("dialog").close();
    target.disabled=true;
    dispatch("exportEditor",request);
  }
  else if (action === "setChordVisibility") {
    if(harmonyNeedsUpdate())return;
    target.disabled = true;
    dispatch(action,{base_export:state.project.export,show_chords:!state.project.score_chords});
  }
  else if (action === "analyzeHarmony") {
    if(harmonyNeedsUpdate())return;
    target.disabled = true;
    const engine=(document.querySelector('dialog[open] #harmony-engine')||$("harmony-engine"))?.value||state.project.harmony_engine||"chordformer";
    if (dialog === "harmony" && $("dialog").open) $("dialog").close();
    dispatch(action,{base_export:state.project.export,engine});
  }
  else if (action === "scoreMode") setScorePageMode(key);
  else if (action === "scoreBuild") requestScoreBuild();
  else if (action === "auditionScore") playScoreMidi(path);
  else if (action === "ncmTool") openNcm();
  else if (action === "ncmClose") $("ncm-dialog").close();
  else if (action.startsWith("ncm")) bridge?.dispatch(JSON.stringify({action,path}));
  else if (action === "chooseExecutionDevice") chooseExecutionDevice(key);
  else if (action === "runMidi") dispatch(action,{tracks:[...midiTracks],channels:midiChannels ? [...midiChannels] : null});
  else if (action === "referenceTrack") { changeTab("tracks"); dispatch("selectAudio",{key}); }
  else if (action === "inspectInstrument") activateInstrument(key);
  else if (action === "instrumentView") activateInstrument(key,target.dataset.view || "notes");
  else if (action === "trackPlay") {
    const position=tab === "notes" ? state.midi.position || 0 : state.player.position || 0;
    const pause=state.player.source === key && state.player.playing;
    const resumeInPlace=tab==='tracks' && state.player.source===key;
    // Switching a running audio lane keeps the heard clock; audition uses a position.
    const switchWhilePlaying=tab==='tracks' && state.player.audition==='audio'
      && state.player.playing && state.player.source!==key;
    const changedView=tab!=='tracks',changedInstrument=Boolean(instruments[key]&&activeInstrument!==key);
    if(changedView||changedInstrument){AstraProjectEditor.cancelNoteEntry?.();AstraProjectEditor.cancelCCContinuation?.();}
    if(instruments[key])activeInstrument=key;
    tab='tracks';
    if(changedView)render(true);
    else if(changedInstrument&&(pause||resumeInPlace))renderTransport();
    if(switchWhilePlaying)dispatch("selectAudio",{key});
    else dispatch(pause || resumeInPlace ? "playPause" : "audition",pause || resumeInPlace ? {} : {key,position});
  }
  else if (["scoreZoomIn","scoreZoomOut","scoreFit"].includes(action)) {
    scoreZoom=action === "scoreFit" ? null : Math.max(.5,Math.min(2,(scoreZoom ?? Math.min(1,((document.querySelector('.paper-area')?.clientWidth || 822)-32)/790)) + (action === "scoreZoomIn" ? .25 : -.25)));
    render(true);
  }
  else if (action === "zoomIn" || action === "zoomOut") setMidiZoom(zoom*(action==="zoomIn"?1.5:1/1.5));
  else if (action === "zoomFit") {modeProjection=null;if(zoom===1)requestRollDraw();else setMidiZoom(1);}
  else if (action === "pitchZoomIn" || action === "pitchZoomOut") setPitchZoom(pitchZoom*(action==="pitchZoomIn"?1.25:.8));
  else if (action === "pedalZoomIn" || action === "pedalZoomOut") setPedalHeight(rollLanes.pedal.height+(action==="pedalZoomIn"?24:-24));
  else if (action === "pitchFit") {pitchZoom=1;pendingPitchFit=true;requestRollDraw();}
  else if (action === "followMode" && Object.hasOwn(followLabels,key)) {
    followMode=key;followSuspended=false;manualRollNavigation=null;
    if(followRollViewport($("roll-scroll"),midiX(midiDisplayPosition()))) updateMidiCursor();
    const menu=target.closest('.follow-menu');
    if(menu){menu.open=false;menu.querySelector('summary')?.focus({preventScroll:true});}
    updateFollowControl();
  }
  else if (action === "followSuspend") {
    suspendFollowOnEdit=!suspendFollowOnEdit;
    updateFollowControl();
  }
  else if (action === "instrument") {
    const selected = new Set(homeOptions().instruments);
    if (selected.has(key)) selected.delete(key); else selected.add(key);
    patchOptions({instruments:Object.keys(instruments).filter(item=>selected.has(item))});
  } else if (action === "format") {
    if(homeOptions().mode!==key)patchOptions({mode:key});
  } else if (action === "run" && target.closest(".config") && queueHomeRun()) return;
  else if (action === "enableExternalEditor") {
    const project=state.project?.path;
    if(project)dispatch(action,{project},()=>{if(project===state.project?.path&&state.project?.editor)AstraProjectEditor.action("editorEnter");});
  }
  else if (action === "linkProjectAudio") dispatch(action,{project:state.project?.path});
  else if (action === 'saveEditor') requestProjectSave();
  else if (action === 'retryEditorSave') requestProjectSave('formal',true);
  else if (action === 'retryEditorBackup') requestProjectSave('recovery',true);
  else if (action === 'retryEditorRetention') requestProjectSave('retention',true);
  else if (action === 'viewEditorBackups') openEditorRecovery();
  else if (action === 'restoreEditorBackup') confirmEditorRecovery(target);
  else if (action === "appPreferences") window.AstraPreferences.open(key);
  else if (action === "advanced") window.AstraPreferences.open("project");
  else if (action === "harmony") openHarmony();
  else if (action === "viewReview") openReview();
  else if (action === "saveAdvanced") saveAdvanced();
  else if (action === "tempo") {
    const working=midiFiles().find(file=>file.editor);
    if(working)changeTab("notes",working);else showTempo(true);
  }
  else if (action === "tempoAdvanced") showTempo(true);
  else if (["captureMeterPosition","previewMeterEdit","undoMeterEdit"].includes(action)) quickMeterAction(action);
  else if (action === "estimateConstantTempo") {
    const serial = ++tempoEditSerial;
    target.disabled=true;
    dispatch(action,{},()=>{
      if (serial !== tempoEditSerial || dialog !== "tempo" || !$("dialog").open) return;
      if (state.tempo_estimate) {tempoDraft=JSON.parse(JSON.stringify(state.tempo_estimate));tempoTab="anchors";showTempo();}
      else {target.disabled=false;$("form-error").textContent=state.job.error || "未能可靠估算恒速，请手动修正。";}
    });
  }
  else if (["saveTempo","tempoTab","removeTempoRow","addTempoRow","fixMeter"].includes(action)) tempoAction(action,key);
  else if (action === "closeDialog") { if(dialog==="editor-structure" && window.AstraStructureEditor?.guardNavigation())return;$("dialog").close(); }
  else if (action === "logs") {
    if (!$("logs").open) $("logs").showModal();
    renderLogs();
    refreshUntil = performance.now() + 900;
    schedulePoll();
  }
  else if (action === "closeLogs") $("logs").close();
  else if (action === "recent") modal("最近项目",recentCards(true)+cancelledJobHistory()+cancelledJobHistory(true),button("打开项目文件夹","openProjectsFolder",{style:"quiet",symbol:"folder",label:"打开默认的项目文件夹"})+button("完成","closeDialog",{style:"primary"}),"recent");
  else if (["keepCancelledJob","cleanCancelledJob","openCancelledJob","openInterruptedJob"].includes(action)) {
    if (!key) return;
    if (action === "openCancelledJob" || action === "openInterruptedJob") {
      if (dialog === "recent" && $("dialog").open) { dialogSkipFocusRestore = true; $("dialog").close(); }
      tab = "tracks";
    } else target.disabled = true;
    dispatch(action,{id:key});
  }
  else if (action === "openRecent") { if ($("dialog").open) { dialogSkipFocusRestore = true; $("dialog").close(); } tab="tracks"; dispatch(action,{path}); }
  else if (action === "new") { pendingScoreAudition=null; midiSelected=pdfSelected=""; tab="tracks"; dispatch(action); }
  else if (action === "pdfPrev" || action === "pdfNext") dispatch("pdfPage",{index:state.pdf.index + (action === "pdfNext" ? 1 : -1)});
  else if (action === "playPause") withPlayer(action,"midiPlayPause");
  else if (action === "stop") withPlayer(action,"midiStop");
  else if (action === "clip") {
    const bounds = target.getBoundingClientRect(), duration = state.audio?.duration || state.player.duration;
    dispatch("audition",{key,position:duration > 0 && event.detail ? Math.max(0,(event.clientX-bounds.left)/bounds.width*duration) : state.player.position || 0});
  } else dispatch(action,{...(path ? {path} : {}),...(key ? {key} : {})});
});

document.addEventListener("change", (event) => {
  const {id,value} = event.target;
  if(id==='roll-axis'){setRulerMode(value);return;}
  if (id.startsWith("editor-")) AstraProjectEditor.changed(id,value);
  if (id.startsWith("roll-meter-")) AstraRollMeter.changed(id,value);
  if (event.target.dataset.midiTrack !== undefined) {
    const key = Number(event.target.dataset.midiTrack);
    if (event.target.checked) midiTracks.add(key); else midiTracks.delete(key);
    midiChannels = null;
    render(true);
    document.querySelector(`[data-midi-track="${key}"]`)?.focus({preventScroll:true});
  }
  if (event.target.dataset.midiChannel !== undefined) {
    const key = Number(event.target.dataset.midiChannel);
    midiChannels ??= new Set(selectedMidiChannels());
    if (event.target.checked) midiChannels.add(key); else midiChannels.delete(key);
    render(true);
    document.querySelector(`[data-midi-channel="${key}"]`)?.focus({preventScroll:true});
  }
  if (id === "source") {
    if(tab==='notes'){
      const position=midiDisplayPosition(),playing=Boolean((transportMidi?state.midi:state.player).playing);
      auditionSource=value;
      dispatch('selectAuditionSource',{key:value,position,playing,preservePosition:true,range:window.AstraTransportPanel?.range()},()=>{renderTransport();requestRollDraw();});
    }else dispatch("selectAudio",{key:value});
  }
  if (id === "review-select") openReview(value);
  if (id === "pdf-select") { pdfSelected = value; activeInstrument=resultFiles("pdf").find(file=>file.path===value)?.instrument || activeInstrument; dispatch("selectPdf",{path:value}); }
  if (id === "midi-select") { if(!chooseMidiSource(value)){event.target.value=state.midi.path;return;} }
  if (id === "seek") {
    if (seekDrag) finishSeek();
    else sendSeek(Number(value),true);
  }
  if (id === "instrument-gain-db") {editInstrumentFader(event.target,true);return;}
  if (id === "volume") {
    volumeAdjust = false;
    if (tab === "notes") midiVolume = Number(value);
    withPlayer("volume","midiVolume",{value:Number(value)});
  }
});

document.addEventListener("input", (event) => {
  const {id,value} = event.target;
  if (id === "seek") {
    if (seekDrag) previewSeek(Number(value));
    else paintPlaybackPosition(Number(value));
  }
  if (id === "instrument-gain-db") {editInstrumentFader(event.target,false);return;}
  if (id === "volume") {volumeAdjust = true;updateVolumeDisplay();}
  if (id === "pitch-zoom") setPitchZoom(Number(value));
  if (id === "midi-zoom") setMidiZoom(2**Number(value));
  if (dialog === "tempo" && tempoTab === "quick" && ["quick-meter-seconds","quick-meter-next","quick-meter-previous"].includes(id)) {
    readTempo();
    $("form-error").textContent = "";
    updateMeterControls();
  }
  if(dialog==='tempo'&&tempoTab==='anchors'&&event.target.classList.contains('tempo-input'))updateTempoAnchorCell(event.target);

});

function updateTempoAnchorCell(input){
  const rows=tempoDraft.anchors,index=Number(input.dataset.index),field=input.dataset.field;
  if(!Number.isInteger(index)||!rows[index]||(field!=='seconds'&&field!=='beat'))return;
  rows[index][field]=input.value.trim()===''?null:Number(input.value);
  // Save/tab navigation still reads and validates the whole draft.
  for(let i=Math.max(0,index-1);i<=index;i++){
    const a=rows[i],b=rows[i+1],cell=$(`anchor-bpm-${i}`);if(!cell)continue;
    const value=b&&a.seconds!==null&&a.beat!==null&&b.seconds>a.seconds&&b.beat>a.beat?(60*(b.beat-a.beat)/(b.seconds-a.seconds)).toFixed(1):'—';
    if(cell.textContent!==value)cell.textContent=value;
  }
}

document.addEventListener('pointerdown',()=>{manualRollNavigation=null;},true);
document.addEventListener('keydown',()=>{manualRollNavigation=null;},true);
document.addEventListener('pointerup',finishManualRollNavigation,true);
document.addEventListener('pointercancel',finishManualRollNavigation,true);
window.addEventListener('blur',finishManualRollNavigation);
document.addEventListener("wheel",event=>{
  manualRollNavigation=null;
  if(event.ctrlKey||event.metaKey||event.altKey||event.shiftKey)rollWheelRemainder=null;
  if(!event.target.closest(".midi-editor")) return;
  const scroll=$("roll-scroll");if(!scroll) return;
  const stopFollow=()=>{suspendRollFollow(false);};
  if((event.ctrlKey||event.metaKey)&&event.shiftKey){
    event.preventDefault();
    const lane=event.target.closest('#pedal-lane,#velocity-lane'),id=lane?.id.split('-')[0];
    if(id){if(!rollLanes[id].folded)setRollLaneHeight(id,lane.offsetHeight-event.deltaY*.2);}
    else setPitchZoom(pitchZoom*Math.exp(-event.deltaY*.003));
  }
  else if(event.ctrlKey||event.metaKey){event.preventDefault();setMidiZoom(zoom*Math.exp(-event.deltaY*.003),event.clientX-scroll.getBoundingClientRect().left);}
  else if(event.altKey){event.preventDefault();const before=event.target.closest(".pedal-lane")?rollLanes.pedal.height:pitchZoom;if(event.target.closest(".pedal-lane")){setPedalHeight(rollLanes.pedal.height-event.deltaY*.2);if(before!==rollLanes.pedal.height)stopFollow();}else{setPitchZoom(pitchZoom*Math.exp(-event.deltaY*.003));if(before!==pitchZoom)stopFollow();}}
  else if(event.shiftKey){event.preventDefault();const before=scroll.scrollLeft;scroll.scrollLeft+=event.deltaY||event.deltaX;if(scroll.scrollLeft!==before)stopFollow();}
  else if(event.target.closest('#roll-scroll')){
    // Own the wheel transaction so compositor scrolling cannot race the follow
    // frame or viewport restore. Only an actual viewport change pauses follow.
    event.preventDefault();
    const left=scroll.scrollLeft,top=scroll.scrollTop;
    const style=event.deltaMode===1?getComputedStyle(scroll):null;
    const line=style?(parseFloat(style.lineHeight)||parseFloat(style.fontSize)*1.2||16):1;
    const unit=event.deltaMode===2?scroll.clientHeight:line;
    const prior=rollWheelRemainder;
    const carry=prior?.scroll===scroll&&prior.left===left&&prior.top===top?prior:{x:0,y:0};
    const x=left+event.deltaX*(event.deltaMode===2?scroll.clientWidth:unit)+carry.x;
    const y=top+event.deltaY*unit+carry.y;
    const targetX=Math.max(0,Math.min(scroll.scrollWidth-scroll.clientWidth,x));
    const targetY=Math.max(0,Math.min(scroll.scrollHeight-scroll.clientHeight,y));
    scroll.scrollLeft=targetX;scroll.scrollTop=targetY;
    rollWheelRemainder={scroll,left:scroll.scrollLeft,top:scroll.scrollTop,x:targetX-scroll.scrollLeft,y:targetY-scroll.scrollTop};
    if(scroll.scrollLeft!==left||scroll.scrollTop!==top)stopFollow();
  }
},{passive:false});
window.AstraTransportKeys.install({
  available: () => transportAvailable,
  toggle: () => withPlayer("playPause","midiPlayPause"),
});
document.addEventListener("keydown", (event) => {
  if ($("ncm-dialog").open) return;
  if ((event.ctrlKey!==event.metaKey)&&!event.shiftKey&&!event.altKey&&event.key.toLowerCase()==='s') {
    const blocked=event.defaultPrevented||event.isComposing||event.keyCode===229||event.getModifierState?.('AltGraph')||
      event.composedPath().some(el=>el?.isContentEditable||el?.matches?.('input,textarea,select,[contenteditable]:not([contenteditable="false"]),[role="textbox"],[role="menu"],[role="menuitem"],dialog'))||
      document.querySelector('dialog[open]:not(.editor-docked),[role="dialog"][aria-modal="true"]:not([hidden]):not([aria-hidden="true"])');
    if(!blocked&&document.hasFocus()&&isResult()&&state.project?.editor&&typeof bridge?.requestEditorSave==='function'){
      event.preventDefault();if(!event.repeat)requestProjectSave();
    }
    return;
  }
  if(tab === "notes" && !event.target.closest("input:not([type=range]),textarea,select,dialog") && (event.ctrlKey||event.metaKey) && ["+","=","-","0"].includes(event.key)) {
    event.preventDefault();setMidiZoom(event.key==="0"?1:zoom*(event.key==="-"?1/1.5:1.5));return;
  }
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "o") { event.preventDefault(); if (!state.job.running) dispatch("chooseAudio"); }
  if ((event.ctrlKey || event.metaKey) && event.key === "Enter" && !$("dialog").open && !$("logs").open) {
    const start = document.querySelector("button:is([data-do='run'],[data-do='runMidi']):not(:disabled)");
    if (start) { event.preventDefault(); start.click(); }
  }
  const focus = event.target.closest("[role=tab]");
  if (focus && ["ArrowLeft","ArrowRight","Home","End"].includes(event.key)) {
    event.preventDefault();
    const tabs = [...document.querySelectorAll("[role=tab]")], index = tabs.indexOf(focus);
    const next = event.key === "Home" ? 0 : event.key === "End" ? tabs.length-1 : (index+(event.key === "ArrowRight" ? 1 : -1)+tabs.length)%tabs.length;
    changeTab(tabs[next].dataset.key);
  }
});

$("dialog").addEventListener("close", () => {
  if($("dialog").open) return; // A different panel may already have reopened the shared node.
  const opener = dialogSkipFocusRestore ? null : dialogReturnFocus;
  dialogReturnFocus = null;
  dialogSkipFocusRestore = false;
  if (dialog === "review") dispatch("closeReview");
  if (dialog === "app-preferences" && advancedOriginal && Object.entries(advancedOriginal).some(([key,value]) => value !== state.options[key])) {
    dispatch("updateOptions",{options:advancedOriginal,project_scope:advancedProject});
  }
  advancedOriginal = null;
  ++tempoEditSerial;
  tempoBaseExport = null;
  meterPreviewPending = false;
  dialog = "";
  pendingTempoSave = null;
  AstraProjectEditor.dockRangePanel?.();
  restoreDialogOpener($("dialog"), opener);
});
$("ncm-dialog").addEventListener("close", () => {
  if($("ncm-dialog").open) return;
  const opener = ncmReturnFocus;
  ncmReturnFocus = null;
  restoreDialogOpener($("ncm-dialog"), opener);
});
function rollLayoutChanged(){rollHeadGeometry=null;requestRollDraw();}
window.addEventListener("resize", rollLayoutChanged);
window.addEventListener("astra-note-selection-change", requestRollDraw);
// Lanes, docked panels and native scrollbar changes can resize the real roll
// viewport without resizing the workspace. Reuse the normal geometry/paint frame.
const rollViewportResize=new ResizeObserver(rollLayoutChanged);
rollViewportResize.observe($("workspace"));
if(typeof MutationObserver==='function')new MutationObserver(syncTransportVisibility).observe($("transport"),{attributes:true,attributeFilter:['data-folded']});
document.addEventListener('toggle',event=>{
  if(event.target.open&&event.target.matches?.('.view-zoom-menu'))syncEditorViewControls();
},true);
function invalidateRollRaster(){lastKeyboard=lastRollLayers=rollRaster=null;rollLayoutChanged();}
document.addEventListener('contextrestored',event=>{if(event.target?.closest?.('.midi-editor'))invalidateRollRaster();},true);
for(const name of ['loadingdone','loadingerror'])document.fonts?.addEventListener?.(name,()=>{if($('roll-scroll'))invalidateRollRaster();});
document.addEventListener("dragenter", () => { dragDepth++; $("drop")?.classList.add("dragging"); });
document.addEventListener("dragleave", () => { if (--dragDepth <= 0) { dragDepth = 0; $("drop")?.classList.remove("dragging"); } });
document.addEventListener("dragover", (event) => event.preventDefault());
document.addEventListener("drop", (event) => { event.preventDefault(); dragDepth = 0; $("drop")?.classList.remove("dragging"); });

// R16: an edit/undo produced ~6 full snapshots (0.5-0.8 MB each), many byte-identical. An identical
// one carries no backend change: skip the parse and full re-render (callers still continue).
let lastSnapshotText=null,lastEditorVersion=0,editorNoteChrome=null;
const NOTE_FIELDS=['revision','edit_stamp','operation','manual','can_undo','can_redo','dirty','score_stale',
  'exported_revision','preview_pending','metronome','audition_together'];
function installNotePublication(receipt,patch){
  const trace=window.AstraRenderStage,started=trace?performance.now():0;
  const p=state.project,old=p?.editor,base=old?.edit_stamp,fields=patch.fields,stamp=fields?.edit_stamp;
  if(receipt.status!=='committed'||receipt.error||receipt.warning||receipt.cancelled||receipt.action!=='editProject'||
     !base||receipt.project!==p.path||stamp?.generation!==base.generation||!Number.isSafeInteger(stamp.version)||
     stamp.version<=Math.max(lastEditorVersion,base.version))return;
  const n=patch.note,validInt=(v,a,b)=>Number.isInteger(v)&&v>=a&&v<=b;
  if(patch.base_version!==base.version||patch.base_revision!==old.revision||(old.source_key??null)!==patch.source_key||
     fields.revision!==receipt.revision||fields.revision===old.revision||stamp.request!==receipt.request||
     !Array.isArray(old.notes)||patch.count!==old.notes.length+1||
     !n||typeof n.id!=='string'||!n.id||receipt.added_ids?.length!==1||receipt.added_ids[0]!==n.id||
     old.notes.some(row=>row.id===n.id)||n.origin!=='manual'||!Number.isFinite(n.start)||!Number.isFinite(n.end)||
     n.start<0||n.end<=n.start||n.end>old.duration+1e-6||!validInt(n.pitch,0,127)||
     !validInt(n.velocity,1,127)||!validInt(n.channel,0,15)){
    // Real generation-local gap, not a late duplicate. Use the same guarded
    // full-read recovery as other structural publications. Never resend an edit.
    refreshPlaybackSnapshot();return;
  }
  const working=tab==='notes'&&AstraProjectEditor.active();
  const reuseChrome=working&&AstraProjectEditor.noteAppendKeepsChrome?.(n);
  const chromeBase=editorNoteChrome?.version===base.version?editorNoteChrome.base:old.revision;
  const doc={...old,notes:old.notes.concat(n)};
  for(const k of NOTE_FIELDS)if(Object.prototype.hasOwnProperty.call(fields,k))doc[k]=fields[k];
  delete doc.preview_error;
  state.project={...p,editor:doc,tempo:doc.tempo};
  state.editor_transaction=receipt;
  lastEditorVersion=stamp.version;lastSnapshotText=null;++editorRevisionEpoch;
  const inRoll=working&&AstraProjectEditor.active()&&Boolean($('midi-canvas'));
  editorNoteChrome=reuseChrome&&inRoll?{version:stamp.version,base:chromeBase}:null;
  if(inRoll)requestNoteInsertDraw(old.notes,doc.notes,n);
  else invalidateRollInputs();
  AstraProjectEditor.sync();
  AstraOrnamentReview.sync();
  if(inRoll)scheduleEditorRender();else render();
  trace?.('snapshot.note_insert',performance.now()-started);
  schedulePoll();
}
function sameWorkingRollInputs(midi,player,audio){
  const doc=state.project.editor,extent=m=>Math.max(doc.duration||0,m.duration||0,1);
  if(!(doc.tempo?.anchors?.length>1)||Boolean(midi.path)!==Boolean(state.midi.path)||extent(midi)!==extent(state.midi))return false;
  const a=midi.markers||[],b=state.midi.markers||[];
  if(a.length!==b.length||!a.every((m,i)=>m.seconds===b[i].seconds&&m.label===b[i].label))return false;
  const original=p=>p.sources?.find(s=>s.key==='original'&&s.path),old=original(player),next=original(state.player);
  if(old?.path!==next?.path||old?.duration!==next?.duration)return false;
  if(!next)return true;
  const peaks=(s,input)=>s.peaks?.length?s.peaks:s.path===input?.path?input.peaks:null;
  const x=peaks(old,audio),y=peaks(next,state.audio);
  return x===y||(!x?.length&&!y?.length)||Boolean(old.peaks?.length&&old.peaks.length===next.peaks?.length&&player.sources_version!=null&&player.sources_version===state.player.sources_version);
}
function acceptSnapshot(serialized,mayInstall=null) {
  if (typeof serialized === "string" && serialized === lastSnapshotText) return;
  const trace=window.AstraRenderStage,record=Boolean(state.player?.trace),started=trace||record?performance.now():0;
  let next;
  try { next = JSON.parse(serialized); } catch { return; }
  const parsed=trace||record?performance.now():0;
  if(!next||typeof next!=='object'||Array.isArray(next)||typeof mayInstall==='function'&&!mayInstall(next))return;
  trace?.('snapshot.parse',parsed-started);
  const previousView=snapshotView();
  const old=state.project?.editor,published=next.project?.editor,stamp=published?.edit_stamp;
  // An older recovery/preview reply cannot rewind a published editor, even
  // when an undo returns to a content revision we have seen before.
  if(stamp&&(!Number.isSafeInteger(stamp.version)||!Number.isSafeInteger(stamp.generation)||stamp.version<lastEditorVersion||stamp.version===lastEditorVersion&&old?.edit_stamp?.version!==stamp.version))return;
  const sameDocument=Boolean(stamp&&old?.edit_stamp&&next.project.path===state.project?.path&&stamp.generation===old.edit_stamp.generation&&stamp.version===old.edit_stamp.version&&published.revision===old.revision);
  const wasWorking=sameDocument&&AstraProjectEditor.workingView(),previousPlayer=state.player,previousAudio=state.audio;
  // Preview/transport publications can carry fresh JSON arrays for the very
  // same immutable working document. Keep its paint/index inputs by reference.
  if(sameDocument)next.project={...next.project,editor:{...published,notes:old.notes,controls:old.controls,tempo:old.tempo}};
  if(!sameDocument)editorNoteChrome=null;
  if(stamp)lastEditorVersion=stamp.version;
  lastSnapshotText = typeof serialized === "string" ? serialized : null;
  window.AstraProjectOpening?.observe(next.project_opening);
  const previousJob = state.job;
  const previousProject = state.project;
  const previousPreview = state.separation_workflow?.project;
  const previousInput = state.input_midi;
  const priorOptions = state.options;
  if(next.midi)next.midi=orderedPlayback("midi",next.midi);
  const incomingPlayer=next.player;
  if(incomingPlayer)next.player=orderedPlayback('player',incomingPlayer);
  // A newer clock can already refer to this catalog. Install its rows without
  // adopting the snapshot's older position, playing status or output error.
  if(incomingPlayer?.sources&&(next.player===incomingPlayer||
      Number.isFinite(incomingPlayer.sources_version)&&next.player.sources_version===incomingPlayer.sources_version)){
    receivedSourcesVersion=incomingPlayer.sources_version??null;
    if(next.player!==incomingPlayer)next.player={...next.player,sources:incomingPlayer.sources};
  }
  if(incomingPlayer?.outputStatus&&next.player===incomingPlayer)outputStatusNeedsRead=false;
  const previousMidi=state.midi;
  const saveStatus=next.editor_save;
  delete next.editor_save;
  Object.assign(state,next);
  scorePageEditing();
  installSaveStatus(saveStatus);
  if(incomingPlayer)window.AstraOutputStatus?.update(state.player);
  if(!sameRollInputs(previousView,snapshotView()))++studioSnapshotViewEpoch;
  if(previousProject?.path!==state.project?.path||previousProject?.editor?.edit_stamp?.generation!==state.project?.editor?.edit_stamp?.generation)++editorProjectEpoch;
  if(previousProject?.editor?.revision!==state.project?.editor?.revision)++editorRevisionEpoch;
  if(('project' in next||midiPaintInputsChanged(previousMidi,state.midi))&&!(wasWorking&&AstraProjectEditor.workingView()&&sameWorkingRollInputs(previousMidi,previousPlayer,previousAudio)))invalidateRollInputs();
  AstraProjectEditor.initializeShortcuts?.(next.editor_shortcuts);
  window.AstraPreferences?.hydrate(next.ui_preferences,next.editor_autosave);
  const syncAt=trace?performance.now():0;
  AstraProjectEditor.sync();
  trace?.('snapshot.editor_sync',performance.now()-syncAt);
  AstraOrnamentReview.sync();
  if(state.midi.path)midiSelected=state.midi.path;
  if (dialog === "tempo" && $("dialog").open && state.project?.export !== tempoBaseExport) {
    ++tempoEditSerial;
    pendingTempoSave = null;
    $("dialog").close();
  }
  if ("input_midi" in next) syncMidiInput(next.input_midi);
  state.options = {...defaults,...state.options};
  if (state.project?.export !== previousProject?.export) {
    pendingScoreAudition = null; midiSelected = ""; pdfSelected = "";
    if (state.project?.path !== previousProject?.path) {activeInstrument=resultInstruments()[0] || "piano";scoreZoom=null;}
    if (state.project?.input_kind === "midi" && (previousInput || state.project.path !== previousProject?.path)) tab = resultFiles("pdf").length ? "score" : "notes";
    else if (state.project && !previousProject && state.project.path!==previousPreview) tab = "tracks";
    if (state.project && tab === "notes" && !state.midi.path && midiFiles().length) {
      midiSelected = midiFiles()[0].path;
      dispatch("selectMidi",{path:midiSelected});
    }
  }
  playPendingScoreMidi();
  trackJob(previousJob);
  if (dialog === "app-preferences" && $("dialog").open) {
    for (const key of ["output","tempo_path","bpm"]) if ($(`option-${key}`) && state.options[key] !== priorOptions[key]) $(`option-${key}`).value = state.options[key] ?? "";
  }
  if (pendingTempoSave && (state.job.running || state.project?.export !== pendingTempoSave)) { pendingTempoSave = null; $("dialog").close(); }
  else if (pendingTempoSave && state.job.error) { pendingTempoSave = null; if ($("form-error")) $("form-error").textContent = state.job.error; }
  const current=state.project?.editor?.edit_stamp;
  // Install and reconcile the transaction in this turn; coalesce only the DOM
  // work. New projects and read-only/source layouts still mount immediately.
  if(tab==='notes'&&old?.edit_stamp&&current?.generation===old.edit_stamp.generation&&previousProject?.path===state.project?.path&&AstraProjectEditor.workingView())scheduleEditorRender();
  else render();
  finishEditorTransaction(next.editor_transaction,true);
  showReviewNotice(state.review_notice);
  renderGpuChoice();
  if(record)tracePlayback('snapshot_apply',{chars:serialized.length,parse_ms:parsed-started,ms:performance.now()-started});
  schedulePoll();
}

function trackJob(previous) {
  if (state.job.running && !previous.running) {
    seenStages = new Set();
    if (state.job.log_path !== previous.log_path) logs = [];
  }
  const stage = phaseIndex(state.job.phase || "");
  if (state.job.running && stage >= 0) seenStages.add(stage);
  if (previous.phase !== state.job.phase && state.job.phase) $("announcement").textContent = state.job.phase;
}

function schedulePoll() {
  if (!bridge || pollTimer || pollInFlight) return;
  const playing = state.player.playing || state.midi.playing;
  const needsRefresh=() => Boolean(activeSaveStatus()?.busy) || outputStatusNeedsRead || window.AstraProjectEditor?.noteEntryBusy?.() || state.editor_transaction?.pending || state.editor_transaction?.status==='pending' || state.project_opening?.pending || state.job.running || state.gpu_preflight?.status === "probing" || state.input_midi?.loading || state.audio?.import_loading || state.pdf.loading || state.midi.loading || state.midi.preparing || state.midi.play_pending || performance.now() < refreshUntil;
  if (!playing && !needsRefresh()) return;
  pollTimer = setTimeout(() => {
    pollTimer = 0;
    if (!needsRefresh() && performance.now()-lastPlaybackSignal < 480) { schedulePoll(); return; }
    if (!needsRefresh() && !state.player.playing && !state.midi.playing) return;
    pollInFlight = true;
    const owner=bridge,navigation=studioNavigationSerial,projectEpoch=editorProjectEpoch;
    if(typeof owner.poll==='function')owner.poll(value=>{
      if(owner===bridge&&navigation===studioNavigationSerial&&projectEpoch===editorProjectEpoch)acceptPoll(value);
      else {pollInFlight=false;schedulePoll();}
    });
    else readStudioSnapshot((snapshot,allows)=>{pollInFlight=false;acceptSnapshot(snapshot,allows);schedulePoll();},owner);
  }, playing ? 150 : state.job.running ? 450 : 220);
}

function playbackSourcesKey(sources) {
  return JSON.stringify(sources?.map(source=>[source.key,source.path,source.duration,source.peaks?.length]));
}

function midiPaintInputsChanged(previous,next){
  return previous!==next&&['path','notes','pedals','pedal_events','tempo_map','time_signatures','duration','duration_beats','markers'].some(key=>next[key]!==previous[key]);
}

function refreshPlaybackSnapshot(preview=false){
  if(preview&&tab==='notes'&&window.AstraProjectEditor?.active()&&state.project?.editor?.edit_stamp&&typeof bridge?.poll==='function'){
    refreshUntil=Math.max(refreshUntil,performance.now()+900);schedulePoll();return;
  }
  if(previewRefreshPending||!bridge)return;
  const owner=bridge;
  previewRefreshPending=true;
  readStudioSnapshot((value,allows)=>{previewRefreshPending=false;if(bridge===owner)acceptSnapshot(value,allows);});
}

const OUTPUT_DIAGNOSTICS=['midiDiagnostics','midiDiagnosticsGeneration','midiDevice','metronomeDiagnostics'];
const PLAYBACK_CONTROLS=['source','audition','playing','duration','volume','available','message','loading','preparing','play_pending'];
function installOutputDiagnostics(previous, update){
  if(!('output_diagnostics' in update)){
    if(update.outputStatus)outputStatusNeedsRead=false;
    return;
  }
  const diagnostics=update.output_diagnostics;
  delete update.output_diagnostics;
  const status=update.outputStatus||(update.output_status_revision===previous.output_status_revision?previous.outputStatus:null);
  if(!status){
    // Still accept the clock. Recover the missing status from the lightweight
    // poll, not by relabeling old diagnostics or requesting the whole project.
    delete update.output_status_revision;
    outputStatusNeedsRead=true;
    return;
  }
  outputStatusNeedsRead=false;
  // Only this diagnostic subrecord is mutable. Its absence in a paired packet
  // clears the old route's counters; it never changes status presentation.
  for(const key of OUTPUT_DIAGNOSTICS){
    if(diagnostics&&Object.prototype.hasOwnProperty.call(diagnostics,key))status[key]=diagnostics[key];
    else if(Object.prototype.hasOwnProperty.call(status,key))delete status[key];
  }
}

function applyPlaybackUpdate(kind, update, signal = false, renderLater=false) {
  if(!update||orderedPlayback(kind,update)!==update)return 0;
  const previous=state[kind];
  // Keep structural identity separate from the clock, including a same-path
  // reprepare and the transition from placeholder notes to installed notes.
  if(kind==='midi'&&['path','view_token','view_loading'].some(key=>key in update&&(update[key]??null)!==(previous[key]??null))){
    refreshPlaybackSnapshot(true);
    return 0;
  }
  if(pauseRequest?.kind===kind&&update.playing===false)pauseRequest=null;
  if(state.player?.trace)tracePlayback('recv',{kind,signal,pos:update.position,playing:update.playing,sampled_at:update.sampled_at,sent:update.transport_order,source:update.source,audition:update.audition});
  if(signal)lastPlaybackSignal=performance.now();
  if(kind==='player')installOutputDiagnostics(previous,update);
  const next=state[kind]={...previous,...update};
  if(kind==='player'){
    confirmNativePlay(next);
    if(audioSelectionPending?.acknowledged&&next.source===audioSelectionPending.key)finishAudioSelection(audioSelectionPending.request);
  }
  if(kind==='midi'&&'vst_instrument' in update)syncVstSelector();
  if(kind==='player'){
    const version=update.sources_version;
    if(update.sources)receivedSourcesVersion=version??null;
    else if(version!==undefined&&version!==receivedSourcesVersion)refreshPlaybackSnapshot();
    if(update.outputStatus)window.AstraOutputStatus?.update(next);
  }else if(midiPaintInputsChanged(previous,next))invalidateRollInputs();
  const change=update.sources&&playbackSourcesKey(update.sources)!==playbackSourcesKey(previous.sources)?3:
    PLAYBACK_CONTROLS.some(key=>next[key]!==previous[key])?2:1;
  if(!renderLater)presentPlaybackUpdates(kind==='player'?change:0,kind==='midi'?change:0);
  return change;
}

function presentPlaybackUpdates(player, midi){
  const change=Math.max(player,midi);
  if(change===3){render();return;}
  if(change===2){renderTransport();return;}
  if(!change)return;
  // Reconcile seeks immediately, but don't overwrite a running frame with a
  // raw packet position. Each animation retains its authoritative anchor.
  const animating=Boolean(tab==='notes'?midiAnimation:tab==='tracks'?audioAnimation:0);
  if(player){updatePlaybackPosition('player',!animating);updateAudioClock();}
  if(midi){updatePlaybackPosition('midi',!animating);updateMidiClock();}
  if(animating)return;
  if(player||midi&&tab==='tracks'&&transportMidi){updateAudioCursor();syncAudioAnimation();}
  if(midi||player&&tab==='notes'&&!transportMidi)updateMidiCursor();
}

function applyPlaybackPacket(next, signal=false, renderLater=false){
  if(!next||typeof next!=='object')return;
  const player=next.player?applyPlaybackUpdate('player',next.player,signal,true):0;
  const midi=next.midi?applyPlaybackUpdate('midi',next.midi,signal,true):0;
  if(!renderLater)presentPlaybackUpdates(player,midi);
}

function acceptPlaybackReceipt(serialized,togglePause=null){
  try{
    const packet=JSON.parse(serialized);
    // A rapid second Play can reverse a pending pause before a silent DAC packet exists.
    if(togglePause&&pauseRequest===togglePause&&packet.player?.native_audition&&packet.player.play_intent)pauseRequest=null;
    applyPlaybackPacket(packet);
  }catch{}
  schedulePoll();
}

function acceptPlaybackFrame(serialized){
  try{
    const packet=JSON.parse(serialized),core=packet.player?.outputStatus;
    applyPlaybackPacket(packet,true);
    // Acknowledge only an installed core, not a discarded stale clock packet.
    if(core&&state.player.outputStatus===core)bridge.ackPlaybackStatus(packet.stream_epoch,packet.player.output_status_revision);
    if(packet.midi?.vst_instrument&&state.midi.vst_instrument===packet.midi.vst_instrument)
      bridge.ackVstView?.(packet.stream_epoch,packet.midi.vst_view_revision);
  }catch{}
  schedulePoll();
}

function acceptPoll(serialized) {
  pollInFlight = false;
  let next;
  try { next = JSON.parse(serialized); } catch { schedulePoll(); return; }
  if((next.project_opening?.version||0)>(state.project_opening?.version||0)){state.project_opening=next.project_opening;readStudioSnapshot();}
  installSaveStatus(next.editor_save);
  if(next.editor_transaction)state.editor_transaction=next.editor_transaction;
  const previous = state.job;
  const previousInput=state.input_midi, previousAudio=state.audio;
  const previousGpu=state.gpu_preflight;
  const previousRhythm=state.rhythm_candidate_running;
  if('rhythm_candidate_running' in next)state.rhythm_candidate_running=next.rhythm_candidate_running;
  if (next.job) state.job = next.job;
  if ("job_progress" in next) state.job_progress = next.job_progress;
  if ("gpu_preflight" in next) state.gpu_preflight=next.gpu_preflight;
  if ("input_midi" in next && !(previous.running && !state.job.running)) syncMidiInput(next.input_midi);
  if (next.audio_import && state.audio?.import_token === next.audio_import.import_token) state.audio = next.audio_import;
  trackJob(previous);
  const logChanged=Array.isArray(next.logs) && (logs.length!==next.logs.length || next.logs.some((line,index)=>line!==logs[index]));
  if (logChanged) logs = next.logs;
  const structural=previousRhythm!==state.rhythm_candidate_running || JSON.stringify(jobStructure(previous))!==JSON.stringify(jobStructure(state.job)) ||
    previousGpu?.token!==state.gpu_preflight?.token || previousGpu?.status!==state.gpu_preflight?.status ||
    ["token","loading","error"].some(key=>previousInput?.[key]!==state.input_midi?.[key]) ||
    ["path","duration","import_loading","import_error"].some(key=>previousAudio?.[key]!==state.audio?.[key]);
  const previewReady=(state.pdf.loading && next.preview?.pdf_loading === false) || (state.midi.loading && next.preview?.midi_loading === false)
    || (next.preview && 'midi_path' in next.preview && next.preview.midi_path!==state.midi.path)
    || (next.preview?.pdf_path && (next.preview.pdf_path!==state.pdf.path || next.preview.pdf_index!==state.pdf.index));
  if ((previous.running && !state.job.running) || (previewReady && !previewRefreshPending)) {
    previewRefreshPending=true;
    readStudioSnapshot((value,allows)=>{
      previewRefreshPending=false;
      acceptSnapshot(value,allows);
    });
  }
  else if (structural) {
    applyPlaybackPacket(next,false,true);
    render();
  } else {
    applyPlaybackPacket(next);
    if (logChanged) renderLogs();
  }
  if(next.editor_transaction)finishEditorTransaction(next.editor_transaction);
  const catalogNeedsRead=state.player.sources_version!==undefined&&state.player.sources_version!==receivedSourcesVersion;
  const currentSnapshotPending=[...studioSnapshotReads].some(read=>read.owner===bridge&&read.navigation===studioNavigationSerial);
  window.AstraProjectEditor?.recoverNoteEntry?.(next.editor_transaction,previewRefreshPending||catalogNeedsRead||currentSnapshotPending);
  renderGpuChoice();
  renderJobProgress();
  schedulePoll();
}

render(true);
function connectStudioBridge(){
  new QWebChannel(qt.webChannelTransport, (channel) => {
    const owner=bridge=channel.objects.bridge;
    nativeWebTransport = channel.objects.nativeWebTransport;
    nativeTracksPanel = channel.objects.nativeTracksPanel;
    nativeTracksActive = null;
    if(nativeTracksPanel){
      nativeSeekVisual=null;waveSeekPanel=null;nativePlaybackClock=null;nativeTrackMotion=null;retiredSeekGeneration=-1;
      nativeTracksPanel.seekVisual?.connect((...args)=>{if(owner===bridge)acceptNativeSeekVisual(...args);});
      nativeTracksPanel.playbackVisual?.connect((...args)=>{if(owner===bridge)acceptNativePlaybackVisual(...args);});
      const visibility=new MutationObserver(syncTransportVisibility);
      document.querySelectorAll('dialog').forEach(node=>visibility.observe(node,{attributes:true,attributeFilter:['open']}));
    }
    state.editor_save=null;editorSaveRequestError=null;
    owner.editorSaveChanged?.connect(value=>{
      if(owner!==bridge)return;
      try{installSaveStatus(JSON.parse(value));schedulePoll();}catch{}
    });
    owner.stateChanged.connect(value=>{if(owner===bridge)acceptSnapshot(value);});
    window.AstraOutputStatus?.attach(bridge);
  owner.externalMidiChanged?.connect(value=>{
    if(owner!==bridge)return;
    let report;try{report=JSON.parse(value);}catch{return;}
    if(report.uri!==AstraProjectEditor.path())return;
    let status=$('external-midi-status');
    if(!status){status=document.createElement('span');status.id='external-midi-status';status.setAttribute('role','status');document.querySelector('.session-title')?.append(status);}
    status.textContent=report.message;status.className=report.status==='error'?'form-error':'form-note';
  });
  owner.editorTransactionChanged?.connect(value=>{if(owner===bridge)finishEditorTransaction(value);});
    (bridge.logs || bridge.log)?.connect((line) => { logs.push(String(line)); if (logs.length > 500) logs.shift(); renderLogs(); });
    if(typeof bridge.enablePlaybackStream==='function'&&typeof bridge.ackPlaybackStatus==='function'&&bridge.playbackChanged?.connect){
      bridge.playbackChanged.connect(acceptPlaybackFrame);
      bridge.enablePlaybackStream();
    }else{
      bridge.playerChanged?.connect((value) => { try { applyPlaybackUpdate("player",JSON.parse(value),true); schedulePoll(); } catch {} });
      bridge.midiChanged?.connect((value) => { try { applyPlaybackUpdate("midi",JSON.parse(value),true); schedulePoll(); } catch {} });
    }
    bridge.ncmChanged?.connect(renderNcm);
    refreshPlaybackSnapshot();
  });
}
if(typeof QWebChannel==='function'&&typeof qt!=='undefined'){
  if(window.AstraNoteEntryProtocol)connectStudioBridge();
  else {
    // Load beside the production app script, before opening its WebChannel.
    // No module loader or guessed entry-document script order is required.
    const script=document.createElement('script');
    script.src=new URL('note_entry.js',document.currentScript?.src||document.baseURI).href;
    script.onload=connectStudioBridge;
    script.onerror=()=>{console.error('Astra note-entry protocol could not load; using single-edit mode.');connectStudioBridge();};
    document.head.appendChild(script);
  }
}

// Lane heights/folds are view-only. Never dispatch a project edit here.
document.addEventListener('click',event=>{
  const id=event.target.closest('[data-roll-lane-toggle]')?.dataset.rollLaneToggle;
  if(id)toggleRollLane(id);
});
document.addEventListener('pointerdown',event=>{
  const target=event.target.closest('[data-roll-lane-resize]'),id=target?.dataset.rollLaneResize;
  if(!id||event.button!==0||laneResize)return;
  laneResize={id,pointer:event.pointerId,y:event.clientY,height:$(`${id}-lane`).offsetHeight,previous:rollLanes[id].height,max:rollLaneMax(id),target,frame:0,value:null};
  target.setPointerCapture(event.pointerId);
  event.preventDefault();
});
document.addEventListener('pointermove',event=>{
  const drag=laneResize;if(drag?.pointer!==event.pointerId)return;
  drag.value=drag.height+drag.y-event.clientY;
  if(!drag.frame)drag.frame=requestAnimationFrame(()=>{drag.frame=0;if(laneResize===drag)setRollLaneHeight(drag.id,drag.value,drag.max);});
  event.preventDefault();
});
function finishLaneResize(event){
  const drag=laneResize;if(!drag||event?.pointerId!==undefined&&event.pointerId!==drag.pointer)return;
  laneResize=null;
  if(drag.frame)cancelAnimationFrame(drag.frame);
  if(event?.type==='pointerup'&&drag.value!==null)setRollLaneHeight(drag.id,drag.height+drag.y-event.clientY,drag.max);
  else if(event?.type!=='pointerup')setRollLaneHeight(drag.id,drag.previous,320);
  if(drag.target.hasPointerCapture?.(drag.pointer))drag.target.releasePointerCapture(drag.pointer);
}
for(const type of ['pointerup','pointercancel','lostpointercapture'])document.addEventListener(type,finishLaneResize);
window.addEventListener('blur',finishLaneResize);
document.addEventListener('keydown',event=>{
  const id=event.target.dataset?.rollLaneResize;
  if(event.key==='Escape'&&laneResize){finishLaneResize(event);event.preventDefault();return;}
  if(!id||!['ArrowUp','ArrowDown','Home','End'].includes(event.key))return;
  event.preventDefault();
  const max=rollLaneMax(id),height=$(`${id}-lane`).offsetHeight;
  setRollLaneHeight(id,event.key==='Home'?rollLanes[id].min:event.key==='End'?max:height+(event.key==='ArrowUp'?16:-16),max);
});

// Project commands share a small disclosure; close after selection or dismissal.
window.AstraPopup?.watch('.project-menu','.project-menu-body',{name:'project-menu'});
document.addEventListener('click',event=>{
 const menu=document.querySelector('.project-menu[open]');
 if(menu&&(!menu.contains(event.target)||event.target.closest('.project-menu-body button')))menu.open=false;
});
document.addEventListener('keydown',event=>{
 if(event.key==='Escape'){const menu=document.querySelector('.project-menu[open]');if(menu){menu.open=false;menu.querySelector('summary')?.focus();}}
});

// A compact volume popover never takes space from the transport timeline.
document.addEventListener('pointerdown',event=>{const popup=$('volume-popup');if(popup?.open&&!event.target.closest('.volume-popover'))popup.open=false;});
document.addEventListener('keydown',event=>{if(event.key==='Escape'&&$('volume-popup')?.open){$('volume-popup').open=false;$('volume-summary')?.focus();}});

// Scrollbar-end zooms retain the existing zoom anchor and keyboard commands.
document.addEventListener("dblclick",event=>{if(event.target.id==="midi-zoom")document.querySelector('[data-do="zoomFit"]')?.click();if(event.target.id==="pitch-zoom")document.querySelector('[data-do="pitchFit"]')?.click();});

window.AstraPopup?.watch('.follow-menu','.follow-menu-body',{name:'roll-follow'});
document.addEventListener('pointerdown',event=>{
  const menu=document.querySelector('.follow-menu[open]');
  if(menu&&!menu.contains(event.target))menu.open=false;
  if(event.button===0&&event.target.closest?.('#midi-canvas,#beat-canvas,#velocity-canvas,#pedal-canvas'))suspendRollFollow();
},true);
window.addEventListener('keydown',event=>{
  const menu=document.querySelector('.follow-menu[open]');
  const inFollow=event.target.closest?.('.follow-menu');
  if(inFollow&&[' ','Enter'].includes(event.key)){
    event.stopPropagation();return; // native disclosure/button activation; no transport shortcut
  }
  if(!menu)return;
  if(event.key==='Escape'){
    menu.open=false;menu.querySelector('summary')?.focus({preventScroll:true});
    event.preventDefault();event.stopPropagation();
  }else if(menu.contains(event.target)&&!event.target.closest('summary')&&[' ','Enter'].includes(event.key)){
    event.stopPropagation();
  }else if(menu.contains(event.target)&&['ArrowUp','ArrowDown','Home','End'].includes(event.key)){
    const rows=[...menu.querySelectorAll('button')],at=rows.indexOf(event.target);
    const index=event.key==='Home'?0:event.key==='End'?rows.length-1:event.key==='ArrowDown'?Math.min(rows.length-1,at+1):Math.max(0,at-1);
    rows[index]?.focus({preventScroll:true});
    event.preventDefault();event.stopPropagation();
  }
},true);
