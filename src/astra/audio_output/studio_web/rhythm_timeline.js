"use strict";
// Translate form choices only; seconds and the backend's opening mode are independent.
const rhythmOpeningForm={
  barBeat:tempo=>Number(tempo?.first_full_bar_beat??tempo?.pickup_beats??0)||0,
  keep:tempo=>typeof tempo?.opening_policy?.keep_before_bar1==='boolean'?tempo.opening_policy.keep_before_bar1:
    tempo?.opening_mode==='normal'?false:Boolean((tempo?.first_full_bar_beat||tempo?.pickup_beats||0)>0),
  operation:(seconds,keep)=>({type:'rhythm_intent',action:'set_opening',seconds,keep_before_bar1:Boolean(keep)})
};
if(typeof module==='object'&&module.exports)module.exports=rhythmOpeningForm;
// The effective timeline is not the detached structure draft.
else window.AstraRhythmTimeline=(()=>{
  let hoverAim=null,multi=[],gesture=null,aimPending=null,selection=null,serial=0,pending=false,lastMeterClick=null,doubleOpened=-Infinity;
  const host=()=>window.AstraProjectEditor;
  const editor=()=>host()?.editor();
  const editable=()=>{reconcileOpening();return host()?.rhythmEditable()&&!pending&&!beatRequest&&!erasePending()&&gesture?.kind!=='erase';};
  const range=()=>host().range();
  const hasRange=()=>range().end>range().start+1e-7;
  const duration=()=>editor()?.duration||1;
  const fmt=x=>Number(x||0).toFixed(3);
  const options=(list,value)=>list.map(([v,label])=>`<option value="${v}" ${String(v)===String(value)?'selected':''}>${label}</option>`).join('');
  let insertUnit=1;
  const durationValues=[[4,'𝅝','全音符 1/1','#7d65ab'],[2,'𝅗𝅥','二分音符 1/2','#786995'],[1.5,'♩.','附点四分 3/8','#b86785'],[1,'♩','四分音符 1/4','#7d65ab'],[.75,'♪.','附点八分 3/16','#5f7fc0'],[.5,'♪','八分音符 1/8','#3f8fae'],[.25,'𝅘𝅥𝅯','十六分音符 1/16','#a5577c'],[.125,'𝅘𝅥𝅰','三十二分音符 1/32','#a96570']];
  function durationInfo(point){const value=Number(point?.step_quarters),entry=durationValues.find(d=>Math.abs(d[0]-value)<1e-8);return {value:Number.isFinite(value)&&value>0?value:null,symbol:entry?.[1]||'◇',label:entry?.[2]||(Number.isFinite(value)&&value>0?`${Number(value.toFixed(6))} 四分拍`:'时值待定'),color:entry?.[3]||'#756c83'};}
  const durationOptions=()=>durationValues.map(d=>[d[0],d[1]+' '+d[2]]);
  const legalDuration=value=>durationValues.some(d=>d[0]===Number(value));
  function durationToolbar(){return '';}
  function durationField(id,points,label){
    const rows=pulses(),first=durationInfo(rows.find(p=>samePoint(p.seconds,points[0]))).value;
    const same=points.every(t=>durationInfo(rows.find(p=>samePoint(p.seconds,t))).value===first);
    const known=same&&legalDuration(first),initial=same&&first?`当前 ${durationInfo({step_quarters:first}).label}`:same?'选择时值':'混合时值 · 选择新时值';
    const available=editable()&&points.length&&points.every(t=>t<duration()-1e-7);
    return `<label class="tool-field" for="${id}" title="设置实际选择的拍点与下一点之间的谱面时值；音符、踏板和音频的秒位置不动。"><span>${label}</span><select id="${id}" class="control" ${available?'':'disabled'}>${known?'':`<option value="" selected disabled>${html(initial)}</option>`}${options(durationOptions(),known?first:'')}</select></label>`;
  }
  function durationOperation(points,value){
    if(!legalDuration(value)||!points.length||points.some(t=>t>=duration()-1e-7))return null;
    const rows=pulses(),unit=Number(value);
    const targets=points.map(t=>rows.find(p=>samePoint(p.seconds,t)));
    if(targets.some(p=>!p)||targets.every(p=>durationInfo(p).value===unit))return null;
    return {type:'rhythm_intent',action:'set_beat_duration',seconds:points.slice(),duration_quarters:unit};
  }
  let selectionRemap=null;
  let viewAnchor=null,addQueue=[],queueTimer=0,adding=null,pencilClick=null;
  let beatRequest=null,beatRestore=null,beatLayer=null,beatAccent=null;
  function edit(operation,done=null){
    if(validSelection()&&multi.length&&['move_beats','move_beat','set_beat_duration','set_meter','set_free_meter'].includes(operation.action))selectionRemap={project:state.project.path,revision:editor().revision,operation:JSON.stringify(operation),points:multi.map(t=>operation.action==='move_beats'?t+operation.delta_seconds:operation.action==='move_beat'&&Math.abs(t-operation.seconds)<1e-7?operation.to_seconds:t)};
    else if(validSelection()&&selection.kind==='meter'&&samePoint(selection.seconds,operation.seconds)&&['set_meter','set_free_meter'].includes(operation.action))
      selectionRemap={project:state.project.path,revision:editor().revision,operation:JSON.stringify(operation),keepMeter:selection.seconds};
    const motion=window.AstraRhythmMotion,token=motion?.capture(),scroll=$('roll-scroll');
    const seconds=operation.to_seconds??(operation.action==='move_beats'?operation.seconds[0]+operation.delta_seconds:undefined)??(Number.isFinite(operation.seconds)?operation.seconds:Array.isArray(operation.seconds)?operation.seconds[0]:operation.start);
    const a=scroll&&Number.isFinite(seconds)?{seconds,pixel:midiX(seconds)-scroll.scrollLeft,left:scroll.scrollLeft,scroll,width:scroll.clientWidth,pixels:midiWidth,mode:rulerMode,revision:editor().revision,project:state.project.path}:null;
    viewAnchor=a;
    const discard=()=>{if(viewAnchor===a)viewAnchor=null;motion?.discardCapture(token);};
    const result=host().rhythmCommit(operation,(error,receipt)=>{if(error||receipt?.status==='failed')discard();done?.(error,receipt);});
    if(result===false)discard();return result;
  }
  function restoreViewport(){
    restoreBeatViewport();
    const a=viewAnchor;if(!a)return;
    const scroll=$('roll-scroll');
    if(a.project!==state.project?.path||a.scroll!==scroll||a.width!==scroll.clientWidth||a.pixels!==midiWidth||a.left!==scroll.scrollLeft||a.mode!==rulerMode){viewAnchor=null;return;}
    if(a.revision!==editor()?.revision){viewAnchor=null;scroll.scrollLeft=Math.max(0,midiX(a.seconds)-a.pixel);}
  }
  function canPlace(){return tab==='notes'&&host()?.active()&&(!state.job.running||state.job.editor_export)&&!state.gpu_preflight;}
  const samePoint=(a,b)=>Math.abs(a-b)<1e-7;
  function queueAdd(point){
    if([adding,...addQueue].some(p=>p?.project===point.project&&samePoint(p.seconds,point.seconds)))return;
    if(addQueue.length>=64){message('待应用拍点较多，请稍候。');return;}
    addQueue.push(point);pumpAdds();
  }
  function pumpAdds(){
    if(queueTimer||!addQueue.length)return;
    if(!canPlace()){addQueue=[];return;}
    while(addQueue.length&&addQueue[0].project!==state.project.path)addQueue.shift();
    if(!adding&&addQueue.length&&editable()){
      const point=addQueue.shift();
      // Recheck against the newly published pulse list, not an old pixel or revision.
      if(!samePoint(nearestPulse(point.seconds)?.seconds,point.seconds)){
        adding=point;
        // Queued pencil clicks keep their exact released seconds. A previous
        // add may have published a new map; never reuse its old note/pixel cue.
        const aim=point.aim?.revision===editor().revision&&beatInputAlive(point.aim)?point.aim:null;
        submitBeat({type:'rhythm_intent',action:'add_beat',seconds:point.seconds,pulse_unit:point.pulse_unit},aim,()=>{if(adding===point)adding=null;pumpAdds();});
      }
    }
    if(addQueue.length&&typeof setTimeout==='function'&&!queueTimer)queueTimer=setTimeout(()=>{queueTimer=0;pumpAdds();},40);
  }

  function message(value){host().rhythmMessage(value);}
  function position(event,target=event.target){
    const rect=rollPointerRect(target),offset=$('roll-scroll')?.scrollLeft||0,x=event.clientX-rect.left,worldX=x+offset;
    return {x,y:event.clientY-rect.top,worldX,seconds:midiSeconds(worldX)};
  }
  // Labels share exactly the same layout as their hit targets. Every event keeps
  // its true-time tick; only text that cannot fit before the next event is omitted.
  function meterLayout(offset=0,width=Infinity){
    const rows=(editor()?.tempo?.time_signatures||[]).map((m,index)=>({m,index,x:midiX(beatToSeconds(m.beat))-offset})).sort((a,b)=>a.x-b.x);
    return rows.map((row,i)=>({...row,label:row.x>=-2&&row.x+38<=Math.min(width,rows[i+1]?.x??Infinity),radius:Math.max(.5,Math.min(3,(row.x-(rows[i-1]?.x??-Infinity))*.3,((rows[i+1]?.x??Infinity)-row.x)*.3))}));
  }
  function meterRowAt(x){
    const rows=meterLayout($('roll-scroll')?.scrollLeft||0,$('roll-scroll')?.clientWidth||800);
    const tick=rows.filter(row=>Math.abs(x-row.x)<=row.radius).sort((a,b)=>Math.abs(x-a.x)-Math.abs(x-b.x))[0];
    return tick||rows.find(row=>row.label&&x>=row.x+3&&x<=row.x+35);
  }
  function markerAt(x){return meterRowAt(x)?.m;}
  // Both ordinary project flags and pending recorder drafts use these
  // gesture primitives; only their mutation/transaction owners differ.
  function sharedFlagHit(rows,x,y,isSelected=()=>false){
    if(Number.isFinite(y)&&(y<4||y>23))return null;
    let lo=0,hi=rows.length;
    while(lo<hi){const m=(lo+hi)>>>1;if(rows[m].x<x-18)lo=m+1;else hi=m;}
    let closest=null,distance=Infinity;
    for(let i=lo;i<rows.length&&rows[i].x<=x+5;i++){
      const row=rows[i],dx=x-row.x;
      const spacing=Math.min(row.x-(rows[i-1]?.x??-Infinity),(rows[i+1]?.x??Infinity)-row.x);
      const radius=Math.min(5,Math.max(1,spacing*.28));
      if(spacing<28?Math.abs(dx)>radius:dx< -5||dx>18)continue;
      const gap=Math.abs(dx);
      if(gap<distance||(gap===distance&&isSelected(row))){closest=row;distance=gap;}
    }
    return closest;
  }
  function sharedFlagChoose(multi,value,extend=false){
    return extend?(multi.includes(value)?multi.filter(x=>x!==value):[...multi,value]):[value];
  }
  function sharedFlagBox(rows,original,lo,hi){
    const next=new Set(original);
    for(let i=pulseLower(rows,lo);i<rows.length&&rows[i].seconds<=hi;i++)next.add(rows[i].key??rows[i].seconds);
    return [...next];
  }
  function sharedFlagDragged(startX,clientX){return Math.abs(clientX-startX)>3;}
  function sharedFlagDestination(raw,event,{axis,noteHit,min,max,previous,blocked=false,still=false}={}){
    const to=Math.max(min,Math.min(max,raw)),limited=to!==raw;
    if(still||blocked||limited)return {to,limited,aim:null};
    const aim=host().rhythmCalibrationSnap(raw,event,{axis,noteHit,min,max,previous});
    return {to:aim.seconds,limited,aim};
  }

  function choose(item,extend=false){
    if(item)host().rhythmClearNotes?.();
    if(item?.kind==='beat')multi=sharedFlagChoose(multi,item.seconds,extend);else multi=[];
    selection=item?{...item,revision:editor().revision,project:state.project.path}:null;
    message(item?`${item.kind==='beat'?'拍点':'拍号'} · ${fmt(item.seconds)} 秒；Shift 多选，右键操作。`:'');requestRollDraw();
  }
  function meterSelectionCurrent(r){
    const g=r.g;
    return selection===r.selection&&g.owner===bridge&&g.project===state.project?.path&&
      g.stamp===editor()?.edit_stamp?.generation&&g.midi===state.midi.path&&tab==='notes';
  }
  function reconcileSelection(){
    const r=selectionRemap;if(!r)return;
    if(r.project!==state.project?.path){selectionRemap=null;return;}
    if(r.meter){
      if(!meterSelectionCurrent(r)){selectionRemap=null;return;}
      // Publication and terminal callback can arrive in either order. The
      // exact receipt (not two structurally plausible marker positions)
      // authorizes the selection rebind and the resulting landed notice.
      if(r.revision===editor()?.revision||r.ackRevision===undefined)return;
      selectionRemap=null;
      if(r.ackRevision!==editor()?.revision)return;
      const list=editor()?.tempo?.time_signatures||[],at=t=>list.find(m=>m.beat>0&&samePoint(beatToSeconds(m.beat),t));
      const landed=at(r.to),source=r.g.item,target=r.g.pulse;
      if(!landed||at(r.from)||target?.id&&landed.pulse_id!==target.id||
         landed.numerator!==source.numerator||landed.denominator!==source.denominator||Boolean(landed.free)!==Boolean(source.free))return;
      selection={kind:'meter',seconds:beatToSeconds(landed.beat),project:r.project,revision:r.ackRevision};
      if(!r.warning)message('拍号 · '+fmt(selection.seconds)+' 秒；Shift 多选，右键操作。');
      return;
    }

    if(r.revision===editor()?.revision)return;
    selectionRemap=null;
    const last=editor()?.tempo?.last_tempo_operation;
    if(r.beat?selection!==r.selection||!beatPublished(r.beat):JSON.stringify(last)!==r.operation)return;
    if(r.keepMeter!==undefined){
      if((editor()?.tempo?.time_signatures||[]).some(m=>samePoint(beatToSeconds(m.beat),r.keepMeter)))
        selection={kind:'meter',seconds:r.keepMeter,project:r.project,revision:editor().revision};
      return;
    }
    const rows=pulses(),next=[];
    for(const t of r.points){const p=nearestPulse(t,rows);if(samePoint(p?.seconds,t))next.push(p.seconds);}
    multi=next;
    const lead=nearestPulse(r.beat?.operation.to_seconds??r.lead??multi[0],rows);
    selection=multi.length?{kind:'beat',seconds:lead&&multi.includes(lead.seconds)?lead.seconds:multi[0],project:r.project,revision:editor().revision}:null;
  }
  function validSelection(){reconcileSelection();return Boolean(selection&&selection.project===state.project?.path&&selection.revision===editor()?.revision);}
  let indexedMulti=null,sortedMulti=[];
  function selected(kind,seconds){
    if(!validSelection())return false;
    if(kind!=='beat')return selection.kind===kind&&samePoint(selection.seconds,seconds);
    if(multi.length<=1)return multi[0]===seconds;
    if(indexedMulti!==multi){indexedMulti=multi;sortedMulti=[...multi].sort((a,b)=>a-b);}
    let lo=0,hi=sortedMulti.length;
    while(lo<hi){const mid=(lo+hi)>>>1;if(sortedMulti[mid]<seconds)lo=mid+1;else hi=mid;}
    return sortedMulti[lo]===seconds;
  }
  function pulses(){return editor()?.tempo?.pulse_points||host().rhythmFlags?.()||[];}
  function pulseLower(rows,seconds){
    let lo=0,hi=rows.length;
    while(lo<hi){const mid=(lo+hi)>>>1;if(rows[mid].seconds<seconds)lo=mid+1;else hi=mid;}
    return lo;
  }
  function nearestPulse(seconds,rows=pulses()){
    const i=pulseLower(rows,seconds),a=rows[i-1],b=rows[i];return !a?b:!b||seconds-a.seconds<=b.seconds-seconds?a:b;
  }
  // Erase owns a presentation mask, not a speculative tempo map. Seconds are
  // exact flag keys; the save also carries IDs wherever the map has them.
  const ERASE_EPS=1e-8,ERASE_POINTS=4096,ERASE_BATCHES=32;
  const eraseStale='工程或节拍版本已更新；尚未提交的擦除已取消。';
  const eraseDesync='删除已保存，但工作台尚未同步；后续擦除已取消，请重新打开工程。';
  const eraseTooMany=`单次最多擦除 ${ERASE_POINTS} 个拍点；本次未提交，请分次擦除。`;
  const eraseQueueFull='待保存的擦除手势过多；本次未提交，请稍候。';
  let erase=null,eraseSerial=0;
  const eraseOwner=s=>s.owner===bridge&&s.project===state.project?.path;
  const erasePending=()=>Boolean(erase&&(erase.queue.length||erase.flight&&!erase.flight.passive));
  const eraseNotice=text=>host().rhythmEraseNotice(text);
  const eraseText=(text,batch)=>[text,...batch.blocked].filter(Boolean).join(' ');
  function eraseScope(){
    const structure=window.AstraStructureEditor;
    return canPlace()&&editor()?.revision!=null&&!document.hidden&&!state.project_opening?.pending&&
      !structure?.hasSession?.()&&!structure?.busy?.();
  }
  const eraseLimits=()=>({end:editor()?.tempo?.anchors?.at(-1)?.seconds??duration(),opening:openingSeconds()});
  // Visible pulses are not the retained mathematical mapping anchors.
  function eraseBlock(){return '';}
  function eraseLane(target){
    const rect=rollPointerRect(target),scroll=$('roll-scroll'),width=scroll?.clientWidth||800,offset=scroll?.scrollLeft||0;
    return {left:rect.left,top:rect.top,width,offset,rows:host().rhythmEraseFlags(width,offset)};
  }
  function eraseIndex(rows,x){
    let lo=0,hi=rows.length;
    while(lo<hi){const mid=(lo+hi)>>>1;if(rows[mid].x<x)lo=mid+1;else hi=mid;}
    return lo;
  }
  function eraseHit({rows,width},x,y){
    if(!(x>=0&&x<=width&&y>=4&&y<=23))return null;
    // In either projection only the two bracketing real flags can win. A wide
    // label is hittable only for an isolated flag. Never prefer a selection,
    // an overview bin or the next unmasked neighbour over the actual hit.
    const i=eraseIndex(rows,x);let hit=null,distance=Infinity;
    for(let j=Math.max(0,i-1);j<=i&&j<rows.length;j++){
      const p=rows[j],gap=Math.abs(x-p.x),spacing=Math.min(p.x-(rows[j-1]?.x??-Infinity),(rows[j+1]?.x??Infinity)-p.x);
      if(gap<distance&&(gap<=5||spacing>=28&&x>=p.x&&x<=p.x+18)){hit=p;distance=gap;}
    }
    return hit;
  }
  function eraseBlockAt(event){
    const v=eraseLane(event.target),p=eraseHit(v,event.clientX-v.left,event.clientY-v.top);
    return p?eraseBlock(p.seconds,eraseLimits()):'';
  }
  function markErased(batch,p,limits){
    if(!p||!Number.isFinite(p.seconds))return true;
    const t=p.seconds,s=batch.session;
    if(s.mask.has(t)){if(!batch.points.has(t))batch.duplicate=true;return true;}
    const reason=eraseBlock(t,limits);if(reason){batch.blocked.add(reason);return true;}
    if(batch.points.size>=ERASE_POINTS){batch.overflow=true;return false;}
    batch.points.set(t,p.id??null);s.mask.add(t);return true;
  }
  function sweepErase(g,v,a,b,limits){
    const dx=b.x-a.x,dy=b.y-a.y;if(!dx)return;
    let from=0,to=1;
    if(dy){const f=(4-a.y)/dy,t=(23-a.y)/dy;from=Math.max(from,Math.min(f,t));to=Math.min(to,Math.max(f,t));}
    else if(a.y<4||a.y>23)return;
    if(from>to)return;
    const x=a.x+dx*from,y=a.x+dx*to,lo=Math.max(0,Math.min(x,y)),hi=Math.min(v.width,Math.max(x,y));
    for(let i=eraseIndex(v.rows,lo);i<v.rows.length&&v.rows[i].x<=hi;i++)if(!markErased(g,v.rows[i],limits))break;
  }
  function sampleErase(g,event){
    const v=eraseLane(g.target),limits=eraseLimits(),count=g.points.size,blocked=g.blocked.size;
    const basis=[g.session.revision,v.left,v.top,v.offset,v.width,midiWidth];
    const continuous=g.basis?.every((value,i)=>value===basis[i]);g.basis=basis;
    function sample(e){
      const {clientX,clientY}=e;if(!Number.isFinite(clientX)||!Number.isFinite(clientY))return;
      const prior=g.last,p=g.last={x:clientX-v.left,y:clientY-v.top,clientX,clientY};
      if(prior&&prior.clientX===clientX&&prior.clientY===clientY)return;
      if(continuous&&prior)sweepErase(g,v,prior,p,limits);
      if(!g.overflow)markErased(g,eraseHit(v,p.x,p.y),limits);
    }
    // Don't turn a scroll/reprojection into a swept interval. Use its latest
    // real pointer sample, and resume connecting samples in the new projection.
    if(continuous)for(const e of event.getCoalescedEvents?.()||[]){sample(e);if(g.overflow)break;}
    if(!g.overflow)sample(event);
    if(g.overflow){stopEraseStroke(eraseTooMany);return;}
    if(count!==g.points.size)requestRollDraw();
    if(count!==g.points.size||blocked!==g.blocked.size)eraseNotice(eraseText(g.points.size?`预览擦除 ${g.points.size} 个拍点；松开保存。`:'',g));
  }
  function eraseInputAlive(g){
    const scroll=$('roll-scroll');
    return g.session===erase&&eraseScope()&&host().rhythmTool()==='eraseBeat'&&g.target.isConnected!==false&&
      !g.target.parentElement?.hidden&&g.scroll===scroll&&g.width===scroll?.clientWidth&&g.pixels===midiWidth&&g.mode===rulerMode;
  }
  function releaseErase(g,cancelled=false){
    gesture=null;
    if(cancelled)host().rhythmCancelClick?.(g.pointer);
    if(g.target.hasPointerCapture?.(g.pointer))g.target.releasePointerCapture(g.pointer);
  }
  function removeEraseMask(batch){for(const t of batch.points.keys())batch.session.mask.delete(t);}
  function retireErase(s){if(erase===s&&!s.flight&&!s.queue.length&&gesture?.session!==s)erase=null;}
  function stopEraseStroke(text=''){
    const g=gesture;if(g?.kind!=='erase')return false;
    removeEraseMask(g);releaseErase(g,true);retireErase(g.session);requestRollDraw();
    if(text)eraseNotice(text);return true;
  }
  function clearUnsentErase(s){
    const hadQueue=s.queue.length>0;
    for(const b of s.queue)removeEraseMask(b);
    s.queue.length=0;if(s.pump)clearTimeout(s.pump);s.pump=0;
    const hadStroke=gesture?.session===s&&stopEraseStroke();
    if(hadQueue)requestRollDraw();return Boolean(hadQueue||hadStroke);
  }
  function forgetErase(s,text=''){
    if(erase!==s)return;
    erase=null;clearUnsentErase(s);if(s.flight?.timer)clearTimeout(s.flight.timer);
    if(text)eraseNotice(text);requestRollDraw();
  }
  function newEraseBatch(){
    syncErase();
    if(!eraseScope()){eraseNotice('当前不可编辑；未擦除拍点。');return null;}
    if(gesture||host().isDragging?.()){eraseNotice('请先结束当前手势。');return null;}
    if(erase?.flight?.passive){eraseNotice(eraseDesync);return null;}
    if(erase?.queue.length>=ERASE_BATCHES){eraseNotice(eraseQueueFull);return null;}
    const session=erase??={owner:bridge,project:state.project.path,revision:editor().revision,noteContinuation:host().rhythmNoteContinuation?.(),tempoJSON:JSON.stringify(editor().tempo),duration:editor().duration,mask:new Set(),queue:[],flight:null,pump:0,after:null};
    return {session,points:new Map(),blocked:new Set()};
  }
  function beginErase(event){
    event.preventDefault();event.stopImmediatePropagation();host().rhythmSuppressClick();
    const b=newEraseBatch();if(!b)return true;
    const g=gesture=beatGrip(event,{...b,kind:'erase'});
    try{g.target.setPointerCapture(g.pointer);}catch{stopEraseStroke('无法开始擦除手势；未保存。');return true;}
    clearBeatHover();lastMeterClick=null;host().rhythmClearNotes?.();
    suspendRollFollow();
    sampleErase(g,event);return true;
  }
  function queueErase(batch){
    const s=batch.session;
    if(!batch.points.size){
      const text=eraseText(batch.duplicate?'该拍点已在待保存的擦除中。':'',batch);if(text)eraseNotice(text);
      retireErase(s);return;
    }
    s.queue.push({session:s,points:batch.points,blocked:batch.blocked});
    eraseNotice(eraseText(`待保存：擦除 ${batch.points.size} 个拍点及其附属拍号。`,batch));
    requestRollDraw();wakeErase(s);
  }
  function erasePicked(times){
    if(!times?.length)return;
    const b=newEraseBatch();if(!b)return;
    const rows=editor().tempo.pulse_points,limits=eraseLimits();
    for(const t of times){
      const p=rows?rows[pulseLower(rows,t)]:{seconds:t};
      if(!p||p.seconds!==t){removeEraseMask(b);retireErase(b.session);eraseNotice('所选拍点已改变，请重新选择。');return;}
      if(!markErased(b,p,limits)){removeEraseMask(b);retireErase(b.session);eraseNotice(eraseTooMany);return;}
    }
    queueErase(b);
  }
  function erasePublication(r,doc){
    const last=doc.tempo?.last_tempo_operation,rows=doc.tempo?.pulse_points,op=r.operation;
    if(!Array.isArray(rows)||last?.action!=='delete_beats'||last.erase_token!==op.erase_token||last.meter_resolution!=='delete'||
       r.receipt&&r.receipt.revision!==doc.revision||!Array.isArray(last.seconds)||last.seconds.length!==op.seconds.length||
       op.seconds.some((t,i)=>t!==last.seconds[i]))return false;
    // Check the authoritative survivor list once per publication, not per
    // pointer sample. Beat numbers can change; identities and seconds cannot.
    if(r.before){
      if(rows.length!==r.before.length-r.batch.points.size)return false;
      let i=0;for(const p of r.before){if(r.batch.points.has(p.seconds))continue;const q=rows[i++];if(q.id!==p.id||q.seconds!==p.seconds)return false;}
      return true;
    }
    return op.seconds.every(t=>rows[pulseLower(rows,t)]?.seconds!==t);
  }
  function bindMigratedErase(s,rows){
    // Legacy flags have no IDs. Only on our own first materialization, allow
    // the same unique sub-EPS time match as the backend; never re-hit a pixel.
    const batches=[...s.queue],next=[],mask=new Set(),limits=eraseLimits();
    if(gesture?.session===s)batches.push(gesture);
    for(const b of batches){
      const points=new Map();
      for(const [t,id] of b.points){
        const i=pulseLower(rows,t);let p=rows[i]?.seconds===t?rows[i]:null;
        if(!p&&id===null){const near=[rows[i-1],rows[i]].filter(q=>q&&Math.abs(q.seconds-t)<ERASE_EPS);if(near.length===1)p=near[0];}
        if(!p||id!==null&&p.id!==id||mask.has(p.seconds)||eraseBlock(p.seconds,limits))return false;
        mask.add(p.seconds);points.set(p.seconds,p.id??null);
      }
      next.push(points);
    }
    batches.forEach((b,i)=>{b.points=next[i];});s.mask=mask;return true;
  }
  function syncErase(wake=true){
    const s=erase;if(!s)return;
    const doc=editor(),r=s.flight;
    if(!eraseOwner(s)||!doc){forgetErase(s,tab==='notes'&&(s.queue.length||gesture?.session===s)?eraseStale:'');return;}
    if(doc.revision!==s.revision&&!r&&s.noteContinuation){
      const proof=s.noteContinuation();
      if(doc.duration===s.duration&&JSON.stringify(doc.tempo)===s.tempoJSON){
        if(proof==='wait')return; // Keep the preview; never guess a CAS head.
        if(proof==='saved'){s.revision=doc.revision;s.noteContinuation=null;}
      }
    }
    if(doc.revision!==s.revision){
      if(!r||r.published!==undefined||!erasePublication(r,doc)){forgetErase(s,tab==='notes'?eraseStale:'');return;}
      s.revision=r.published=doc.revision;removeEraseMask(r.batch);
      if(!r.before&&!bindMigratedErase(s,doc.tempo.pulse_points)){
        r.cancelledTail=clearUnsentErase(s);eraseNotice('拍点已重新生成；后续擦除无法精确匹配，已取消。');
      }
      requestRollDraw();
    }
    if(!eraseScope()){
      if(clearUnsentErase(s)){if(r)r.cancelledTail=true;if(tab==='notes')eraseNotice(eraseStale);}
    }else if(gesture?.session===s&&!eraseInputAlive(gesture))stopEraseStroke('编辑视图已改变；本次擦除未提交。');
    if(r?.published!==undefined&&r.receipt){
      if(r.receipt.revision!==r.published){forgetErase(s,eraseStale);return;}
      if(r.timer)clearTimeout(r.timer);
      s.flight=null;s.after=r.passive?null:r.receipt.request;
      if(tab==='notes')eraseNotice(eraseText([`已删除 ${r.batch.points.size} 个拍点及其附属拍号；可撤销。`,
        r.receipt.warning,r.cancelledTail?'尚未提交的后续擦除已取消。':'',s.queue.length?`还有 ${s.queue.length} 次擦除待保存。`:''].filter(Boolean).join(' '),r.batch));
    }
    if(wake)wakeErase(s);retireErase(s);
  }
  function eraseMask(){syncErase();return tab==='notes'?erase?.mask:null;}
  function eraseDone(s,r,error,receipt){
    if(erase!==s||s.flight!==r)return;
    if(!eraseOwner(s)){syncErase();return;}
    if(receipt?.project&&receipt.project!==s.project){forgetErase(s,eraseStale);return;}
    if(receipt?.status!=='committed'){
      forgetErase(s,`${receipt?.error||error||'未收到拍点保存确认。'} 尚未提交的后续擦除已取消。`);return;
    }
    if(receipt.project!==s.project||receipt.action!=='editProject'||!receipt.request||receipt.revision==null||receipt.revision===r.base){forgetErase(s,'拍点保存回执与请求不符；请检查当前工程。');return;}
    r.receipt=receipt;
    if(receipt.warning){r.passive=true;r.cancelledTail=clearUnsentErase(s)||r.cancelledTail;eraseNotice(`${receipt.warning} ${eraseDesync}`);}
    syncErase();
    if(erase!==s||s.flight!==r||r.passive)return;
    eraseNotice('删除已保存，正在同步节拍版本…');
    // A durable receipt can precede the snapshot (or the GUI refresh can
    // fail). Don't expose the obsolete flags or replay the disk write.
    r.timer=setTimeout(()=>{
      syncErase();if(erase!==s||s.flight!==r)return;
      r.passive=true;r.cancelledTail=clearUnsentErase(s)||r.cancelledTail;eraseNotice(eraseDesync);
    },5000);
  }
  function wakeErase(s){
    if(erase!==s||editor()?.revision!==s.revision||s.pump||s.flight||!s.queue.length||pending||beatRequest||!host().rhythmEditable())return;
    // Leave the editor completion turn (including any queued locator write)
    // before using its now-published head. No optimistic CAS revision.
    s.pump=setTimeout(()=>{s.pump=0;flushErase(s);},0);
  }
  function flushErase(s){
    syncErase(false);
    if(erase!==s||editor()?.revision!==s.revision||s.flight||!s.queue.length||pending||beatRequest||!host().rhythmEditable())return;
    const batch=s.queue.shift(),entries=[...batch.points].sort((a,b)=>a[0]-b[0]);
    const operation={type:'rhythm_intent',action:'delete_beats',seconds:entries.map(e=>e[0]),meter_resolution:'delete',
      erase_token:`${Date.now()}-${Math.random()}-${++eraseSerial}`};
    if(entries.every(e=>typeof e[1]==='string'&&e[1]))operation.pulse_ids=entries.map(e=>e[1]);
    const r=s.flight={batch,base:s.revision,before:editor().tempo.pulse_points,operation};
    try{
      const ok=host().rhythmCommit(operation,(error,receipt)=>eraseDone(s,r,error,receipt),null,s.after?{after:s.after}:null);
      if(erase!==s||s.flight!==r)return;
      if(ok===false)forgetErase(s,'当前编辑状态不允许保存；待保存的擦除已取消。');
      else if(!r.receipt)eraseNotice(eraseText(`正在保存：擦除 ${entries.length} 个拍点及其附属拍号。`,batch));
    }catch{if(erase===s&&s.flight===r)forgetErase(s,'无法确认擦除提交；后续擦除已取消，请检查当前工程。');}
  }
  function openingHit(c){const x=midiX(openingSeconds())-($('roll-scroll')?.scrollLeft||0);return c.x>=x-7&&c.x<=x+82;}
  const isBeatAim=g=>g?.kind==='beat'||g?.kind==='insert';
  const beatFrame=()=>typeof requestBeatAimDraw==='function'?requestBeatAimDraw():requestRollDraw();
  function clearBeatHover(){if(hoverAim){hoverAim=null;beatFrame();}}
  function hideBeatLayer(){
    if(beatLayer)beatLayer.style.display='none';
    if(beatAccent){window.AstraRhythmMotion?.stopSettle('beat-aim');beatAccent=null;}
  }
  function rememberBeatInput(g,event){
    const {clientX,clientY}=event,ctrlKey=Boolean(event.ctrlKey),metaKey=Boolean(event.metaKey);
    const prior=g.lastEvent;
    if(prior&&(ctrlKey!==prior.ctrlKey||metaKey!==prior.metaKey))g.resetSnap=true;
    g.lastEvent={target:g.target,pointerId:g.pointer,clientX,clientY,ctrlKey,metaKey,altKey:Boolean(event.altKey),shiftKey:Boolean(event.shiftKey)};
    g.dirty=true;
  }
  function beatInputAlive(g){
    const scroll=$('roll-scroll');
    if(!g||g.project!==state.project?.path||!canPlace()||g.target.isConnected===false||g.scroll!==scroll||
       g.mode!==rulerMode||g.pixels!==midiWidth||g.width!==scroll?.clientWidth)return false;
    // A pencil click may overlap publication of an earlier pencil click. Use
    // the new map at release, never the old hover identity or an old pixel.
    return g.kind==='insert'?host().rhythmTool()==='beat'&&(g.midi===state.midi.path||g.revision!==editor().revision)
      :g.midi===state.midi.path&&g.revision===editor().revision&&editable();
  }
  function beatGrip(event,data){
    const scroll=$('roll-scroll');
    return {...data,target:event.target,pointer:event.pointerId,project:state.project.path,revision:editor().revision,midi:state.midi.path,
      scroll,width:scroll?.clientWidth,pixels:midiWidth,mode:rulerMode,x:event.clientX,moved:false};
  }
  function captureBeat(event,c,data){
    clearBeatHover();discardBeatRestore(beatRestore);
    const g=gesture=beatGrip(event,{...data,pressWorld:c.worldX,raw:data.seconds,to:data.seconds});
    rememberBeatInput(g,event);
    event.target.setPointerCapture(event.pointerId);
    if(g.kind==='beat'){
      if(g.target.style)g.target.style.cursor=g.bounds.error?'not-allowed':window.AstraCursors?.css('grabbing')||'grabbing';
      if(!g.bounds.error)suspendRollFollow();
      event.preventDefault();
    }
    event.stopImmediatePropagation();beatFrame();
  }
  function releaseBeatPointer(g,cancelled=false){
    gesture=null;
    if(g.kind==='beat'&&g.target.style)g.target.style.cursor='';
    if(cancelled){host().rhythmCancelClick?.(g.pointer);hideBeatLayer();}
    if(g.target.hasPointerCapture?.(g.pointer))g.target.releasePointerCapture(g.pointer);
    beatFrame();
  }
  function intersection(a,b){
    if(!a||!b)return null;
    const left=Math.max(a.left,b.left),top=Math.max(a.top,b.top),right=Math.min(a.right,b.right),bottom=Math.min(a.bottom,b.bottom);
    return right>left&&bottom>top?{left,top,right,bottom}:null;
  }
  function beatView(target){
    const scroll=$('roll-scroll'),canvas=$('midi-canvas'),flags=$('beat-flags-canvas'),rect=rollPointerRect(target);
    const read=node=>node===target?rect:rollPointerRect(node);
    const sc=read(scroll),note=read(canvas),flag=flags?.parentElement?.hidden?null:read(flags),workspace=read($('workspace'));
    const left=sc?.left+(scroll?.clientLeft||0),top=sc?.top+(scroll?.clientTop||0);
    const inside=sc?{left,top,right:left+scroll.clientWidth,bottom:top+scroll.clientHeight}:null;
    let rollClip=intersection(inside,note),flagClip=flag?intersection(flag,{...flag,right:flag.left+(scroll?.clientWidth||0),bottom:flag.top+26}):null;
    if(workspace){rollClip=intersection(rollClip,workspace);flagClip=intersection(flagClip,workspace);}
    return {axis:typeof rollAxis==='function'?rollAxis():null,offset:scroll?.scrollLeft||0,rect,note,flag,rollClip,flagClip};
  }
  function resolveBeatAim(g,view){
    const event=g.lastEvent;if(!event)return;
    const {axis,offset,rect,note,rollClip}=view,project=axis?.x??midiX,invert=axis?.seconds??midiSeconds;
    const geometry=typeof rollGeometry==='undefined'?null:rollGeometry;
    const basis=[axis,editor()?.revision,editor()?.notes,offset,rect.left,geometry?.row,geometry?.high,note?.left,note?.top,
      rollClip?.left,rollClip?.top,rollClip?.right,rollClip?.bottom,host().rhythmCalibrationOn(event)];
    const sameBasis=g.basis&&basis.every((v,i)=>v===g.basis[i]);
    if(sameBasis&&!g.dirty&&!g.resetSnap)return;
    const previous=sameBasis&&!g.resetSnap?g.aim:null;
    g.basis=basis;g.dirty=g.resetSnap=false;
    const {clientX:x,clientY:y}=event,world=x-rect.left+offset,shift=world-g.pressWorld;
    const still=g.kind==='beat'&&shift===0;
    const raw=g.raw=still?g.seconds:Math.max(0,Math.min(duration(),invert(g.kind==='beat'?project(g.seconds)+shift:world)));
    const min=g.bounds?.min??0,max=g.bounds?.max??duration();
    g.blocked=g.bounds?.error;
    const noteHit=rollClip&&x>=rollClip.left&&x<rollClip.right&&y>=rollClip.top&&y<rollClip.bottom?{worldX:x-note.left+offset,y:y-note.top}:null;
    const result=sharedFlagDestination(raw,event,{axis,noteHit,min,max,previous,blocked:g.blocked,still});
    Object.assign(g,result);
  }

  // A meter move still attaches to an existing pulse, not to a new time in
  // the score. Only the presentation is attracted; the owner validates the
  // single exact hit on release. Distances are measured in the display axis.
  const METER_MAGNET_PX=7,METER_EXACT_PX=2,METER_DRAG_PX=3;
  let meterLanding=null;
  function meterView(g){
    const scroll=$('roll-scroll'),rect=rollPointerRect(g.target),offset=scroll?.scrollLeft||0;
    const axis=typeof rollAxis==='function'?rollAxis():null;
    return {rect,offset,axis,project:axis?.x??midiX,invert:axis?.seconds??midiSeconds};
  }
  function meterInputAlive(g){
    return g.owner===bridge&&g.stamp===editor()?.edit_stamp?.generation&&g.version===editor()?.edit_stamp?.version&&!document.hidden&&!dialog&&
      !g.target.parentElement?.hidden&&beatInputAlive(g);
  }
  function meterLandingCurrent(g){
    const scroll=$('roll-scroll');
    return g.owner===bridge&&g.project===state.project?.path&&g.revision===editor()?.revision&&g.stamp===editor()?.edit_stamp?.generation&&
      g.midi===state.midi.path&&tab==='notes'&&!document.hidden&&!dialog&&g.target.isConnected!==false&&
      g.scroll===scroll&&g.width===scroll?.clientWidth&&g.mode===rulerMode&&g.pixels===midiWidth;
  }
  function meterDisplay(){
    if(gesture?.kind==='meter'&&gesture.moved&&!gesture.fixed&&meterInputAlive(gesture))return gesture;
    if(meterLanding&&meterLandingCurrent(meterLanding.g))return meterLanding.g;
    return null;
  }
  function meterCandidate(rows,raw,x,project,magnet,previous){
    const i=pulseLower(rows,raw);
    // Display x is monotone in seconds on both axes. A direct stem wins in a
    // cluster; a subpixel tie may retain the previous target, never a hit.
    const pick=j=>{
      const p=rows[j];if(!p)return null;
      const x0=project(p.seconds),gap=Math.abs(x-x0);
      const spacing=Math.min(rows[j-1]?x0-project(rows[j-1].seconds):Infinity,rows[j+1]?project(rows[j+1].seconds)-x0:Infinity);
      return {p,x:x0,gap,direct:gap<=Math.min(.75,spacing*.32)};
    };
    const a=pick(i-1),b=pick(i);
    let best=!a?b:(!b||a.gap<=b.gap?a:b);const other=best===a?b:a;
    if(!best||!magnet&&!best.direct||magnet&&best.gap>=METER_MAGNET_PX)return null;
    if(magnet&&!best.direct&&other&&other.p===previous&&other.gap<METER_MAGNET_PX&&
      other.gap-best.gap<=Math.min(.45,Math.abs(other.x-best.x)*.18))best=other;
    return best;
  }
  function resolveMeterAim(g,v){
    const e=g.lastEvent;if(!e)return;
    const magnet=host().rhythmCalibrationOn?.(e)??!(e.ctrlKey||e.metaKey),{rect,axis,offset,project,invert}=v;
    const basis=[axis,project,invert,offset,rect.left,magnet],same=g.basis&&basis.every((value,i)=>value===g.basis[i]);
    if(same&&!g.dirty&&!g.resetSnap)return;
    const previous=same&&!g.resetSnap?g.pulse:null;
    g.basis=basis;g.dirty=g.resetSnap=false;
    const shift=e.clientX-rect.left+offset-g.pressWorld,x=project(g.seconds)+shift;
    const raw=g.raw=shift===0?g.seconds:Math.max(0,Math.min(duration(),invert(x)));
    g.drawX=project(raw);g.to=raw;g.pulse=null;g.blocked=null;g.strength=0;g.magnet=magnet;
    if(!g.moved||g.fixed)return;
    const hit=meterCandidate(g.pulseRows,raw,x,project,magnet,previous);
    if(!hit)return;
    const {p,x:target,gap}=hit;
    g.pulse=p;g.to=p.seconds;
    const signatures=g.signatures;let lo=0,hi=signatures.length;
    while(lo<hi){const mid=(lo+hi)>>>1;if(signatures[mid].beat<p.beat-1e-8)lo=mid+1;else hi=mid;}
    if(signatures[lo]!==g.item&&Math.abs(signatures[lo]?.beat-p.beat)<1e-8)g.blocked='目标拍点已有拍号，未覆盖';
    // Smooth spatial attraction, with zero pull at the edge. No time-based
    // smoothing/lag and no change in lane height. The thin pin is exact.
    const t=Math.max(0,Math.min(1,(METER_MAGNET_PX-gap)/(METER_MAGNET_PX-METER_EXACT_PX)));
    g.strength=magnet?t*t*(3-2*t):1;
    if(magnet&&!g.blocked)g.drawX+=(target-g.drawX)*g.strength;
  }
  function sampleMeter(g,event){
    if(!Number.isFinite(event.clientX))return;
    const wasMoved=g.moved;
    rememberBeatInput(g,event);
    g.moved||=Math.abs(event.clientX-g.x)>METER_DRAG_PX;
    // Invalidate the durable source only when masking starts; subsequent
    // input is folded into the lightweight rhythm-guide frame.
    if(!wasMoved&&g.moved&&!g.fixed){
      suspendRollFollow();
      requestRollDraw();
    }
    beatFrame();
  }
  function meterCursor(g,role){
    const css=window.AstraCursors?.css(role)||role;
    if(g.target.style&&g.target.style.cursor!==css)g.target.style.cursor=css;
  }
  function releaseMeterPointer(g,cancelled=false,landed=false){
    gesture=null;
    if(g.target.style)g.target.style.cursor='';
    if(cancelled)host().rhythmCancelClick?.(g.pointer);
    if(g.target.hasPointerCapture?.(g.pointer))g.target.releasePointerCapture(g.pointer);
    if(g.moved&&!landed)requestRollDraw();
    beatFrame();
  }
  function submitMeter(g){
    const r=meterLanding={g};
    // The pressed meter stays selected after it lands, so the ruler outline,
    // inspector and info line follow it instead of falling back to the playhead.
    const remap=selection?.kind==='meter'&&selection.project===g.project&&selection.revision===g.revision&&samePoint(selection.seconds,g.seconds)
      ?{meter:true,g,selection,project:g.project,revision:g.revision,from:g.seconds,to:g.to}:null;
    if(remap)selectionRemap=remap;
    const finish=(error='',receipt=null)=>{
      // syncBeat may already have retired the visual landing when the new
      // document published. The exact request's selection state is separate.
      if(meterLanding===r)meterLanding=null;
      if(remap&&selectionRemap===remap){
        const confirmed=receipt?.status==='committed'&&receipt.changed!==false&&receipt.revision!=null&&
          receipt.revision!==g.revision&&(!receipt.project||receipt.project===g.project);
        if(!confirmed)selectionRemap=null;
        else{remap.ackRevision=receipt.revision;remap.warning=receipt.warning||error;reconcileSelection();}
      }
      requestRollDraw();
    };
    try{
      if(edit({type:'rhythm_intent',action:'move_meter',seconds:g.seconds,to_seconds:g.to},finish)===false){
        finish();message('编辑状态已改变；拍号未移动。');
      }
    }catch{finish();message('无法提交拍号移动。');}
  }
  function upMeter(event,g){
    const valid=event.type==='pointerup'&&Number.isFinite(event.clientX)&&Number.isFinite(event.clientY)&&meterInputAlive(g);
    if(valid){sampleMeter(g,event);resolveMeterAim(g,meterView(g));}
    const landed=valid&&g.moved&&!g.fixed&&!g.blocked&&g.pulse&&!samePoint(g.seconds,g.to);
    releaseMeterPointer(g,!valid,landed);
    if(landed)submitMeter(g);
    event.preventDefault();event.stopImmediatePropagation();host().rhythmSuppressClick();
    if(!valid){lastMeterClick=null;return true;}
    if(!g.moved){
      const now=performance.now(),prior=lastMeterClick;
      if(prior&&prior.project===g.project&&prior.revision===g.revision&&samePoint(prior.seconds,g.seconds)&&now-prior.time<450){
        lastMeterClick=null;doubleOpened=now;directRhythmMenu('meter',g.seconds);
      }else lastMeterClick={project:g.project,revision:g.revision,seconds:g.seconds,time:now};
    }else{
      lastMeterClick=null;
      if(g.fixed)message('初始拍号不能移动，只能修改。');
      else if(g.blocked)message(g.blocked);
      else if(!g.pulse)message('拍号未移动：请选择已有拍点。');
    }
    return true;
  }
  function down(event,emptyRoll=false,coordinates=null){
    pencilClick=null;
    if(event.button!==0)return false;
    if(window.AstraBeatRecorder?.locksRhythm?.())return false;
    const id=event.target.id,beatLane=id==='beat-flags-canvas';
    if(!beatLane&&!emptyRoll&&id!=='ruler-canvas'&&id!=='opening-canvas')return false;
    if(beatLane&&host().rhythmTool()==='eraseBeat')return beginErase(event);
    const c=coordinates??position(event),meterLane=id==='ruler-canvas'&&c.y>=16&&c.y<=33;
    const tool=host().rhythmTool(),flag=beatLane&&tool!=='rangeSelect'&&canPlace()?host().rhythmFlagAt(c.x,c.y):null;
    if((beatLane&&!flag||emptyRoll)&&tool==='beat'&&!event.altKey&&canPlace()){
      // Keep native click delivery/counting. Pointerup arms exactly one click;
      // neither pointerdown, pointerup nor dblclick submits an insertion.
      captureBeat(event,c,{kind:'insert',seconds:c.seconds,pulse_unit:insertUnit});return true;
    }
    if(!editable()){
      if(beatLane&&canPlace()){event.preventDefault();event.stopImmediatePropagation();return true;}
      return false;
    }
    if(id==='opening-canvas'&&(event.ctrlKey||event.metaKey||event.altKey))return false;
    if(id==='opening-canvas'){
      if(!openingHit(c))return false;
      const at=openingSeconds();gesture={kind:'opening',seconds:at,to:at,revision:editor().revision,project:state.project.path,target:event.target,pointer:event.pointerId,x:event.clientX,moved:false,pressed:c.seconds};
      // Keep the browser's native mouse click sequence (and system double-click
      // interval). Cancelling pointerdown suppresses its compatibility mousedown.
      event.target.setPointerCapture(event.pointerId);event.stopImmediatePropagation();return true;
    }
    if(!beatLane&&!meterLane)return false;
    if(event.altKey||((event.ctrlKey||event.metaKey)&&!beatLane))return false;
    const item=beatLane?flag:markerAt(c.x),kind=beatLane?'beat':'meter';
    if(!item){
      if(beatLane){
        if(!event.shiftKey)choose(null);host().rhythmClearNotes?.();
        gesture={kind:'box',seconds:c.seconds,to:c.seconds,original:[...multi],revision:editor().revision,project:state.project.path,target:event.target,pointer:event.pointerId,x:event.clientX,moved:false};
        event.target.setPointerCapture(event.pointerId);event.preventDefault();event.stopImmediatePropagation();return true;
      }
      return false;
    }
    const seconds=beatLane?item.seconds:beatToSeconds(item.beat);if(!(beatLane&&!event.shiftKey&&selected('beat',seconds)))choose({kind,seconds},Boolean(event.shiftKey));
    if(tool==='eraseBeat'){applyGesture({type:'rhythm_intent',action:'delete_meter',seconds},editor().revision,state.project.path);event.preventDefault();event.stopImmediatePropagation();return true;}
    if(event.shiftKey&&beatLane){event.preventDefault();event.stopImmediatePropagation();return true;}
    if(beatLane){
      const points=[...multi].sort((a,b)=>a-b);
      captureBeat(event,c,{kind,seconds,points,bounds:moveBounds(points,seconds,item.locked)});return true;
    }
    clearBeatHover();
    const g=gesture=beatGrip(event,{kind,seconds,to:seconds,item,owner:bridge,stamp:editor()?.edit_stamp?.generation,
      version:editor()?.edit_stamp?.version,fixed:item.beat===0,pressWorld:c.worldX,pulseRows:pulses(),signatures:editor().tempo.time_signatures});
    event.target.setPointerCapture(event.pointerId);
    if(g.target.style)g.target.style.cursor=g.fixed?'not-allowed':window.AstraCursors?.css('grabbing')||'grabbing';
    rememberBeatInput(g,event);event.preventDefault();event.stopImmediatePropagation();return true;
  }
  function move(event){
    if(!gesture){
      if(window.AstraBeatRecorder?.locksRhythm?.()||window.AstraBeatRecorder?.ownsPointer?.(event)){clearBeatHover();return false;}
      if(event.target.id==='beat-flags-canvas'&&!dialog&&editable()&&host().rhythmTool()==='beat'&&canPlace()){
        if(hoverAim?.target!==event.target||!beatInputAlive(hoverAim))hoverAim=beatGrip(event,{kind:'insert',hover:true});
        rememberBeatInput(hoverAim,event);beatFrame();return true;
      }
      clearBeatHover();return false;
    }
    if(gesture.pointer!==event.pointerId)return false;
    const g=gesture;
    if(g.kind==='erase'){
      syncErase();if(gesture===g)sampleErase(g,event);
      event.preventDefault();event.stopImmediatePropagation();return true;
    }
    if(isBeatAim(g)){
      rememberBeatInput(g,event);g.moved||=sharedFlagDragged(g.x,event.clientX);
      beatFrame();if(g.kind==='beat')event.preventDefault();return true;
    }
    if(g.kind==='meter'){
      if(!meterInputAlive(g))cancel();else sampleMeter(g,event);
      event.preventDefault();return true;
    }
    const c=position(event,g.target);
    if(g.project!==state.project?.path||tab!=='notes'||g.revision!==editor()?.revision){cancel();return true;}
    g.lastEvent=event;g.moved||=sharedFlagDragged(g.x,event.clientX);
    if(g.target?.style)g.target.style.cursor=window.AstraCursors?.css(g.kind==='box'?'range':'grabbing')||(g.kind==='box'?'crosshair':'grabbing');
    const raw=g.pressed===undefined||c.seconds===g.pressed?g.pressed===undefined?c.seconds:g.seconds:midiSeconds(midiX(g.seconds)+c.worldX-midiX(g.pressed));
    if(g.kind==='box'){
      g.to=c.seconds;const lo=Math.min(g.seconds,g.to),hi=Math.max(g.seconds,g.to);
      multi=sharedFlagBox(pulses(),g.original,lo,hi);selection=multi.length?{kind:'beat',seconds:multi[0],revision:g.revision,project:g.project}:null;requestRollDraw();event.preventDefault();return true;
    }
    if(g.kind==='opening'){const aim=Math.max(0,Math.min(duration()-1e-7,raw));g.to=event.ctrlKey||event.metaKey?aim:(nearestPulse(aim)?.seconds??aim);g.to=Math.max(0,Math.min(duration()-1e-7,g.to));requestRollDraw();event.preventDefault();return true;}
    return false;
  }
  function sharedFlagBounds(rows,points,at,locked,{start=0,end=duration(),documentEnd=end}={}){
    const epsilon=1e-5;
    const blocked=error=>({min:at,max:at,error});
    const endpoint='选中包含工程边界，请取消选中边界点';
    if(locked||points.some(t=>t<1e-7||t>=documentEnd-1e-7))return blocked(endpoint);
    let min=-Infinity,max=Infinity;
    // Only the outside neighbours of each contiguous selected run constrain
    // its delta. Don't enumerate all the unselected beats to build a wall list.
    for(let i=0;i<points.length;){
      const first=points[i];let j=pulseLower(rows,first),last=first;
      if(!samePoint(rows[j]?.seconds,first)&&samePoint(rows[j-1]?.seconds,first))j--;
      if(!samePoint(rows[j]?.seconds,first))return blocked('拍点已更新，请重新选择');
      if(rows[j].endpoint)return blocked(endpoint);
      const prior=rows[j-1],left=prior?Math.max(prior.seconds,prior.rawSeconds??prior.seconds):start-epsilon;
      while(++i<points.length&&samePoint(rows[j+1]?.seconds,points[i])){
        if(rows[++j].endpoint)return blocked(endpoint);
        last=points[i];
      }
      const after=rows[j+1],right=after?Math.min(after.seconds,after.rawSeconds??after.seconds):end+epsilon;
      // The existing separation margin is a limit on travel, not a command
      // to jump on press when an imported pair is already inside that margin.
      min=Math.max(min,Math.min(0,left+epsilon-first));
      max=Math.min(max,Math.max(0,right-epsilon-last));
    }
    return min===max?blocked('相邻拍点之间没有可移动空间'):{min:at+min,max:at+max};
  }
  function moveBounds(points,at,locked){
    // The ordinary document bounds still carry their original needle margin.
    const eps=1e-5,end=duration();
    return sharedFlagBounds(pulses(),points,at,locked,{start:eps,end:end-eps,documentEnd:end});
  }
  function refreshAimModifier(event){
    if(event.type==='keydown'&&event.key==='Enter'&&event.target?.id==='opening-canvas'&&!event.ctrlKey&&!event.metaKey&&!event.altKey&&!event.shiftKey&&editable()){operationMenu('opening',openingSeconds());event.preventDefault();event.stopImmediatePropagation();return;}
    if(event.type==='keydown'&&event.key==='Escape'&&(cancelBeat()||gesture||openingRequest?.stage==='preview')){if(gesture||openingRequest?.stage==='preview')cancel();event.preventDefault();event.stopImmediatePropagation();return;}
    const g=gesture;if(!['beat','opening','insert','meter'].includes(g?.kind)||!g.lastEvent)return;
    if(!['Control','Meta'].includes(event.key)||event.repeat||event.isComposing)return;
    const prior=g.lastEvent;
    move({target:prior.target,pointerId:prior.pointerId,clientX:prior.clientX,clientY:prior.clientY,
      ctrlKey:event.key==='Control'?event.type==='keydown':Boolean(event.ctrlKey),
      metaKey:event.key==='Meta'?event.type==='keydown':Boolean(event.metaKey),altKey:Boolean(event.altKey),shiftKey:Boolean(event.shiftKey),preventDefault(){}});
  }
  document.addEventListener('keydown',refreshAimModifier,true);
  document.addEventListener('keyup',refreshAimModifier,true);
  function cancel(){
    pencilClick=null;clearBeatHover();if(openingRequest?.stage==='preview')finishOpening(openingRequest);
    if(stopEraseStroke('已取消本次擦除；尚未提交。'))return;
    const g=gesture;
    if(isBeatAim(g)){releaseBeatPointer(g,true);return;}
    if(g?.kind==='meter'){lastMeterClick=null;releaseMeterPointer(g,true);return;}
    gesture=null;if(g?.kind==='opening')host().rhythmSuppressClick();if(g?.target?.style)g.target.style.cursor='';if(g?.target.hasPointerCapture(g.pointer))g.target.releasePointerCapture(g.pointer);requestRollDraw();
  }
  function cancelBeat(){
    if(stopEraseStroke('已取消本次擦除；尚未提交。'))return true;
    const s=erase;
    if(s?.queue.length){clearUnsentErase(s);retireErase(s);eraseNotice('已取消排队的擦除；已发出的保存不受影响。');return true;}
    if(isBeatAim(gesture)||gesture?.kind==='meter'||pencilClick){cancel();return true;}
    if(beatRequest?.stage==='preview'){finishBeat(beatRequest,'',true);hideBeatLayer();return true;}
    return false;
  }
  function up(event){
    if(!gesture||gesture.pointer!==event.pointerId)return false;
    if(gesture.kind==='erase'){
      const g=gesture;syncErase();event.preventDefault();event.stopImmediatePropagation();host().rhythmSuppressClick();
      if(gesture!==g)return true;
      if(event.type!=='pointerup'||!Number.isFinite(event.clientX)||!Number.isFinite(event.clientY)){stopEraseStroke('已取消本次擦除；尚未提交。');return true;}
      sampleErase(g,event);
      if(gesture===g){releaseErase(g);queueErase(g);}
      return true;
    }
    if(isBeatAim(gesture)){
      const g=gesture,valid=event.type==='pointerup'&&Number.isFinite(event.clientX)&&beatInputAlive(g);
      // The last rAF may not have run. Resolve the actual release with its
      // current modifiers and scroll; never commit a previously painted target.
      if(valid){rememberBeatInput(g,event);resolveBeatAim(g,beatView(g.target));g.moved||=sharedFlagDragged(g.x,event.clientX);}
      releaseBeatPointer(g,!valid);event.stopImmediatePropagation();host().rhythmSuppressClick();
      if(!valid)return true;
      if(g.kind==='insert'){
        g.revision=editor().revision;g.midi=state.midi.path;
        pencilClick={target:g.target,pointer:g.pointer,seconds:g.to,pulse_unit:g.pulse_unit,project:g.project,aim:g};return true;
      }
      event.preventDefault();lastMeterClick=null;
      if(!g.moved)return true;
      if(g.blocked){message(g.blocked);return true;}
      if(samePoint(g.to,g.seconds))return true;
      const operation=g.points.length>1?{type:'rhythm_intent',action:'move_beats',seconds:g.points,delta_seconds:g.to-g.seconds}
        :{type:'rhythm_intent',action:'move_beat',seconds:g.seconds,to_seconds:g.to};
      submitBeat(operation,g);return true;
    }
    if(gesture.kind==='meter')return upMeter(event,gesture);
    if(event.type==='pointerup'&&Number.isFinite(event.clientX)&&(gesture.kind==='opening'||gesture.lastEvent||gesture.x!==event.clientX))move(event);
    if(!gesture){event.preventDefault();event.stopImmediatePropagation();return true;}
    const g=gesture;cancel();event.preventDefault();event.stopImmediatePropagation();host().rhythmSuppressClick();
    if(event.type==='pointercancel'||g.revision!==editor()?.revision||g.project!==state.project?.path)return true;
    if(g.kind==='box'){message(`已选择 ${multi.length} 个拍点；Delete 删除，选两点可均分。`);return true;}
    if(g.kind==='opening'){if(g.moved&&Math.abs(g.to-g.seconds)>1e-8)applyOpeningGesture(g);return true;}
    return true;
  }
  document.addEventListener('click',event=>{
    const point=pencilClick;pencilClick=null;if(!point)return;
    if(event.target!==point.target&&event.target!==point.target.parentElement)return;
    event.preventDefault();event.stopImmediatePropagation();
    if(event.detail!==1||!canPlace()||host().rhythmTool()!=='beat'||state.project.path!==point.project)return;
    queueAdd({seconds:point.seconds,pulse_unit:point.pulse_unit,project:point.project,aim:point.aim});
  },true);
  let openingRequest=null;
  function finishOpening(request){
    if(openingRequest!==request)return;
    openingRequest=null;pending=false;if(aimPending===request.g)aimPending=null;
    requestRollDraw();
  }
  function reconcileOpening(){
    const r=openingRequest;if(!r)return;
    if(r.g.project!==state.project?.path||r.g.revision!==editor()?.revision||
       (r.stage==='preview'&&host()?.active?.()===false))finishOpening(r);
  }
  function applyOpeningGesture(g){
    // Keep the target through preview AND the actual edit acknowledgement. A
    // preview is not a save receipt; clearing here flashes the old marker.
    const operation=rhythmOpeningForm.operation(g.to,rhythmOpeningForm.keep(editor()?.tempo));
    const request={g,stage:'preview'};openingRequest=request;pending=true;aimPending=g;requestRollDraw();
    bridge.previewProject(JSON.stringify({base_revision:g.revision,operation}),raw=>{
      reconcileOpening();if(openingRequest!==request)return;
      if(!host()?.rhythmEditable()){finishOpening(request);return;}
      let result;try{result=JSON.parse(raw);}catch{result={error:'无法读取开头设置结果'};}
      if(result.error){finishOpening(request);operationMenu('opening',g.to);return;}
      request.stage='commit';
      try{if(edit(operation,()=>finishOpening(request))===false)finishOpening(request);}
      catch(error){finishOpening(request);throw error;}
    });
  }
  function beatProjectCurrent(r){return r.owner===bridge&&r.project===state.project?.path;}
  function beatPreviewCurrent(r){return beatProjectCurrent(r)&&r.revision===editor()?.revision&&r.midi===state.midi.path&&canPlace();}
  function beatPublished(r){
    if(!beatProjectCurrent(r)||r.revision===editor()?.revision)return false;
    if(r.ackRevision!==undefined)return r.ackRevision===editor()?.revision;
    const op=r.operation,last=editor()?.tempo?.last_tempo_operation;
    if(last?.action!==op.action)return false;
    if(op.action==='move_beats')return last.delta_seconds===op.delta_seconds&&Array.isArray(last.seconds)&&
      last.seconds.length===op.seconds.length&&op.seconds.every((t,i)=>t===last.seconds[i]);
    return last.seconds===op.seconds&&(op.action==='move_beat'?last.to_seconds===op.to_seconds:last.pulse_unit===op.pulse_unit);
  }
  function beatAnchor(aim){
    const scroll=$('roll-scroll');if(!scroll||!aim||!beatInputAlive(aim))return null;
    const left=scroll.scrollLeft;
    return {scroll,left,top:scroll.scrollTop,width:scroll.clientWidth,pixels:midiWidth,mode:rulerMode,seconds:aim.to,pixel:midiX(aim.to)-left};
  }
  function beatAnchorCurrent(r){
    const a=r.anchor,scroll=$('roll-scroll');
    return a&&beatProjectCurrent(r)&&tab==='notes'&&host().active()&&a.scroll===scroll&&scroll.clientWidth===a.width&&
      scroll.scrollLeft===a.left&&scroll.scrollTop===a.top&&a.mode===rulerMode&&a.pixels===midiWidth;
  }
  function discardBeatRestore(r){
    if(!r)return;
    if(beatRestore===r)beatRestore=null;
    r.anchor=null;window.AstraRhythmMotion?.discardCapture(r.motionToken);
  }
  function restoreBeatViewport(){
    const r=beatRestore;if(!r)return;
    if(!beatAnchorCurrent(r)){discardBeatRestore(r);return;}
    if(r.revision===editor()?.revision)return;
    if(!beatPublished(r)){discardBeatRestore(r);return;}
    const a=r.anchor;beatRestore=null;
    // App calls this before RhythmMotion.tick. Use the grabbed landing in the
    // new clock; motion only interpolates presentation from the old viewport.
    if(a.mode==='bars')a.scroll.scrollLeft=Math.max(0,midiX(a.seconds)-a.pixel);
  }
  function finishBeat(r,error='',discard=false){
    if(beatRequest!==r)return;
    beatRequest=null;
    if(discard||error){discardBeatRestore(r);if(selectionRemap?.beat===r)selectionRemap=null;}
    if(r.aim)beatFrame();
    if(error&&beatProjectCurrent(r)&&tab==='notes')message(error);
    r.done?.();syncErase();
  }
  function submitBeat(operation,aim,done=null){
    const r={operation,aim,done,owner:bridge,project:state.project.path,revision:editor().revision,midi:state.midi.path,
      stage:'preview',show:Boolean(aim),anchor:beatAnchor(aim)};
    beatRequest=r;if(aim)beatFrame();
    const unavailable='无法校验拍点操作；未保存修改。';
    try{r.owner.previewProject(JSON.stringify({base_revision:r.revision,operation}),raw=>{
      if(beatRequest!==r||r.stage!=='preview')return;
      if(!beatPreviewCurrent(r)){finishBeat(r,'',true);return;}
      let result;try{result=JSON.parse(raw);}catch{result=null;}
      if(!result||result.ok===false||result.error){finishBeat(r,result?.error||unavailable,true);return;}
      if(!host().rhythmEditable()){finishBeat(r,'编辑状态已改变；未保存此次拍点操作。',true);return;}
      r.stage='commit';
      if(aim?.kind==='beat'&&validSelection()&&multi.length){
        selectionRemap={beat:r,project:r.project,revision:r.revision,selection,lead:aim.to,
          points:multi.map(t=>operation.action==='move_beats'?t+operation.delta_seconds:aim.to)};
      }
      if(beatAnchorCurrent(r)){
        viewAnchor=null;discardBeatRestore(beatRestore);beatRestore=r;r.motionToken=window.AstraRhythmMotion?.capture();
      }
      try{
        if(host().rhythmCommit(operation,(error,receipt)=>{
          if(beatRequest!==r)return;
          if(!beatProjectCurrent(r)||receipt?.project&&receipt.project!==r.project){finishBeat(r,'',true);return;}
          if(receipt?.status==='failed'||!receipt&&error){
            finishBeat(r,r.revision===editor()?.revision?(error||'拍点未保存。'):'',true);return;
          }
          if(receipt?.revision!==undefined){
            if(receipt.revision!==editor()?.revision){finishBeat(r,'',true);return;}
            r.ackRevision=receipt.revision;
            if(r.ackRevision===r.revision)discardBeatRestore(r);
          }
          finishBeat(r);
        })===false)finishBeat(r,'未保存此次拍点操作。',true);
      }catch{finishBeat(r,'无法提交拍点操作。',true);}
    });}catch{finishBeat(r,unavailable,true);}
  }
  function syncBeat(){
    syncErase();
    if(isBeatAim(gesture)&&!beatInputAlive(gesture))releaseBeatPointer(gesture,true);
    if(gesture?.kind==='meter'&&!meterInputAlive(gesture))cancel();
    if(meterLanding&&!meterLandingCurrent(meterLanding.g)){meterLanding=null;beatFrame();}
    if(hoverAim&&(!beatInputAlive(hoverAim)||hoverAim.revision!==editor()?.revision))clearBeatHover();
    const r=beatRequest;
    if(r&&(!beatProjectCurrent(r)||tab!=='notes'||r.stage==='preview'&&!beatPreviewCurrent(r)))finishBeat(r,'',true);
  }
  function invalidateBeatView(){
    const repaint=hasBeatView();pencilClick=null;clearBeatHover();
    if(isBeatAim(gesture))releaseBeatPointer(gesture,true);
    if(gesture?.kind==='meter')cancel();
    if(meterLanding){meterLanding=null;requestRollDraw();}
    if(beatRequest){beatRequest.show=false;discardBeatRestore(beatRequest);}
    discardBeatRestore(beatRestore);hideBeatLayer();syncBeat();if(repaint)beatFrame();
  }
  function applyGesture(operation,revision,project,done=null,stillCurrent=null){
    if(beatRequest)return;
    const ticket=++serial;pending=true;
    if(operation.action!=='add_beat')message('正在应用拍点操作…');
    bridge.previewProject(JSON.stringify({base_revision:revision,operation}),raw=>{
      if(ticket!==serial){done?.();return;}pending=false;
      if(revision!==editor()?.revision||project!==state.project?.path||stillCurrent?.()===false){aimPending=null;if(project===state.project?.path)message('工程或编辑视图已更新，本次操作未应用。');done?.();return;}
      let result;try{result=JSON.parse(raw);}catch{result={error:'无法读取拍点校验结果。'};}
      if(result.error){aimPending=null;message(result.error);done?.();return;}
      if(edit(operation,done)===false){aimPending=null;requestRollDraw();done?.();}
    });
  }
  function double(event){
    if(window.AstraBeatRecorder?.locksRhythm?.())return false;
    if(editable()&&event.target.id==='opening-canvas'&&!event.ctrlKey&&!event.metaKey&&!event.altKey&&!event.shiftKey&&openingHit(position(event))){operationMenu('opening',openingSeconds());event.preventDefault();event.stopImmediatePropagation();return true;}
    if(event.ctrlKey||event.metaKey||event.altKey||event.shiftKey)return false;
    if(!editable()||event.target.id!=='ruler-canvas')return false;
    if(performance.now()-doubleOpened<180){event.preventDefault();event.stopImmediatePropagation();return true;}
    const c=position(event);if(c.y<16||c.y>33)return false;const m=markerAt(c.x);
    if(!m)return false;directRhythmMenu('meter',beatToSeconds(m.beat));event.preventDefault();event.stopImmediatePropagation();return true;
  }
  let operationDraft=null,operationSerial=0,trends=null;
  const mainStamp=()=>{const st=state.project?.editor?.edit_stamp;return {revision:editor()?.revision,project:state.project?.path,midi:state.midi.path,generation:st?.generation??null,version:st?.version??null};};
  function mainCurrent(stamp){
    const structure=window.AstraStructureEditor,st=state.project?.editor?.edit_stamp;
    return tab==='notes'&&stamp.project===state.project?.path&&stamp.revision===editor()?.revision&&stamp.midi===state.midi.path&&
      (stamp.generation===null||stamp.generation===(st?.generation??null))&&
      (stamp.version===null||stamp.version===(st?.version??null))&&
      !window.AstraBeatRecorder?.locksRhythm?.()&&
      !state.project_opening?.pending&&!structure?.hasSession?.()&&!structure?.busy?.();
  }
  function openingSeconds(){return beatToSeconds(rhythmOpeningForm.barBeat(editor()?.tempo));}
  function openingUnconfirmed(t){if(t?.opening_policy?.source==='user')return false;return t?.opening_phase?.confirmed===false||t?.opening_phase?.semantic_first_full_bar_confirmed===false||t?.semantic_first_full_bar_confirmed===false;}
  function initialOpeningDescription(){
    const initial=editor()?.tempo?.rhythm_initial,t=initial?.tempo;if(!initial?.available||!t)return '模型初稿尚待后端核验。';
    const q=rhythmOpeningForm.barBeat(t),rows=t.anchors||[];let seconds=0;
    for(let i=0;i+1<rows.length;i++)if(q>=rows[i].beat&&q<=rows[i+1].beat){seconds=rows[i].seconds+(q-rows[i].beat)*(rows[i+1].seconds-rows[i].seconds)/(rows[i+1].beat-rows[i].beat);break;}
    const keep=rhythmOpeningForm.keep(t);
    return `${initial.source==='model_export'?'模型初稿':initial.source==='initial_revision'?'初始工程版本':'本轮编辑前版本'}：${openingUnconfirmed(t)?'开头待确认，候选首强拍':(keep?'保留前段':'不保留前段')+'，第 1 小节'} ${fmt(seconds)} 秒。`;
  }
  function drawOpening(ctx,width,offset){
    reconcileOpening();
    ctx.fillStyle='#edf2f5';ctx.fillRect(0,0,width,28);ctx.font='11px AstraSans';
    const activeOpening=gesture?.kind==='opening'?gesture:aimPending?.kind==='opening'?aimPending:null;
    const seconds=activeOpening?activeOpening.to:openingSeconds(),x=midiX(seconds)-offset;
    ctx.fillStyle='#7560af';ctx.fillRect(x,1,2,26);ctx.beginPath();ctx.moveTo(x,2);ctx.lineTo(x+9,7);ctx.lineTo(x,12);ctx.fill();
    const unconfirmed=openingUnconfirmed(editor()?.tempo),keep=rhythmOpeningForm.keep(editor()?.tempo);
    const opening=$('opening-canvas');if(opening){opening.tabIndex=host()?.active?.()===false?-1:0;opening.setAttribute?.('aria-label','第1小节起点，按 Enter 设置');}if(opening)opening.title=`${unconfirmed?'检测强拍，尚未确认第1小节':'第1小节起点'} · ${fmt(seconds)} 秒\n${seconds>0?(keep?'保留前段谱面':'前段不纳入谱面')+'；':''}拖动标记，双击或右键设置`;
    if(x>=-1&&x<=width)ctx.fillText(unconfirmed?'首拍待确认':'第1小节',x+14,18);
  }
  function openingReferences(seconds){
    const source=host()?.rhythmDetectedBeats?.(),rows=source?.available?source.downbeats||[]:[];
    return {previous:[...rows].reverse().find(t=>t<seconds-1e-9),next:rows.find(t=>t>seconds+1e-9),available:rows.length>0};
  }
  function openingInputSeconds(){const d=operationDraft,text=$('editor-pulse-opening-seconds')?.value;if(d?.fixedOpening)return d.seconds;return !d?.secondsEdited&&text===d?.displaySeconds?d.seconds:text?.trim()?Number(text):NaN;}
  function openingFormOperation(){
    return rhythmOpeningForm.operation(openingInputSeconds(),$('editor-pulse-opening-keep')?.checked);
  }
  function updateOpeningReferences(seconds){
    const refs=openingReferences(seconds);
    for(const side of ['previous','next']){const b=$('editor-opening-'+side);if(b)b.disabled=!Number.isFinite(seconds)||!Number.isFinite(refs[side]);}
  }
  function stepOpeningReference(side){
    const d=operationDraft;if(!editable()||dialog!=='editor-pulse-operation'||!$('dialog')?.open||d?.kind!=='opening'||d.fixedOpening||!mainCurrent(d))return;
    const seconds=openingReferences(openingInputSeconds())[side];if(!Number.isFinite(seconds))return;
    d.seconds=seconds;d.displaySeconds=fmt(seconds);d.secondsEdited=false;
    $('editor-pulse-opening-seconds').value=d.displaySeconds;previewOperation();
  }
  function operationMenu(kind,seconds,{fixedOpening=false}={}){
    if(kind==='delete'){if(validSelection())erasePicked(multi);return;}
    const stamp=mainStamp();if(!editable()||!mainCurrent(stamp))return;
    hoverAim=null;const points=validSelection()?[...multi].sort((a,b)=>a-b):[];
    if(kind==='meter_bars'&&!hasRange()){message('先在上方标尺拖选 L/R 范围。');return;}
    if(kind==='divide'&&points.length!==2){message('请用 Shift 选择两个端点后均分。');return;}
    if(kind==='short_bar'){directRhythmMenu('meter',Number.isFinite(seconds)?seconds:validSelection()?selection.seconds:range().start,'free');return;}
    // Beat durations are owned by the governing meter; no per-beat duration editing.
    if(kind==='duration')return;
    operationDraft={...stamp,kind,points,range:{...range()},seconds:Number.isFinite(seconds)?seconds:openingSeconds(),fixedOpening:kind==='opening'&&fixedOpening&&Number.isFinite(seconds),operation:null};
    const title={delete:'删除所选拍点',divide:'两点均分',opening:'第 1 小节起点',restore:'恢复节拍分析',meter_bars:'选区改拍号 · 保留小节线',duration:'设置拍点时值'}[kind];
    let body=['divide','opening','restore'].includes(kind)?'':'<p class="form-note">原始演奏不变，可撤销。</p>';
    if(kind==='divide'){
      const [left,right]=points,inside=pulses().filter(p=>p.seconds>left+1e-7&&p.seconds<right-1e-7).length;
      const defaultN=Math.min(4096,inside+1);
      body+=`<div class="form-grid"><label class="field">间隔数<input id="editor-pulse-count" class="control" type="number" min="1" max="4096" step="1" value="${defaultN}"></label><label class="checkbox-line"><input id="editor-pulse-midi-snap" type="checkbox">吸附音头</label></div>`;
    }
    if(kind==='meter_bars'){
      const q=secondsToBeat(range().start),m=[...(editor().tempo.time_signatures||[])].reverse().find(m=>m.beat<=q)||{numerator:4,denominator:4};
      body+=`<p class="form-note">L · ${html(musicPosition(operationDraft.range.start))} · ${fmt(operationDraft.range.start)} 秒<br>R · ${html(musicPosition(operationDraft.range.end))} · ${fmt(operationDraft.range.end)} 秒</p><div class="form-grid"><label class="field">新拍号<input id="editor-pulse-numerator" class="control" type="number" min="1" max="32" value="${m.numerator}" aria-label="新拍号分子"></label><label class="field">分母<select id="editor-pulse-denominator" class="control">${options([1,2,4,8,16,32,64,128].map(x=>[x,x]),m.denominator)}</select></label></div><p class="form-note">小节线的位置不变。能对齐新拍号的旧拍点会保留；需要重新均分的小节会在预览中标出。</p>`;
    }
    if(kind==='duration')body+=durationField('editor-pulse-duration',points,'选中拍点时值');
    if(kind==='opening'){
      operationDraft.displaySeconds=fmt(operationDraft.seconds);operationDraft.secondsEdited=false;
      const keep=rhythmOpeningForm.keep(editor()?.tempo),fixed=operationDraft.fixedOpening;
      const time=fixed?`<p class="form-note">第 1 小节从 ${html(operationDraft.displaySeconds)} 秒开始。</p>`:
        `<label class="field">第 1 小节起点（秒）<input id="editor-pulse-opening-seconds" class="control" type="number" min="0" step=".001" value="${html(operationDraft.displaySeconds)}"></label>`;
      const references=fixed?null:openingReferences(operationDraft.seconds);
      const referenceButtons=references?.available?`<details><summary>改用检测到的强拍</summary><div class="form-grid"><button type="button" class="secondary" id="editor-opening-previous" data-do="editorOpeningPrevious">移到上一强拍</button><button type="button" class="secondary" id="editor-opening-next" data-do="editorOpeningNext">移到下一强拍</button></div></details>`:'';
      body+=`<div class="rhythm-opening-form">${time}<label class="checkbox-line"><input id="editor-pulse-opening-keep" type="checkbox" ${keep?'checked':''} autofocus>保留第 1 小节前的内容</label>${referenceButtons}</div>`;
    }
    if(kind==='restore')body+=`<p>${html(initialOpeningDescription())}</p><p>将替换整曲的拍点、速度映射、拍号和开头设置，包括人工修改。音符、踏板和原音频不变；完成后可以撤销。</p><label class="checkbox-line"><input id="editor-pulse-confirm" type="checkbox">确认替换当前节拍修改</label>`;
    const feedback=kind==='divide'?'<p id="editor-pulse-status" role="status" hidden></p>':'<p id="editor-pulse-status" role="status">正在检查…</p>';
    modal(title,body+feedback,`${button('取消','closeDialog')}${button('应用','editorPulseApply',{disabled:true,style:'primary'})}`,'editor-pulse-operation');previewOperation();
  }
  function divideRequest(d){
    const n=Number($('editor-pulse-count')?.value);
    if(!Number.isInteger(n)||n<1||n>4096)return null;
    return {type:'rhythm_intent',action:'divide_beats',start:d.points[0],end:d.points[1],interval_count:n,snap_to_midi:Boolean($('editor-pulse-midi-snap')?.checked)};
  }
  function previewDivide(d,status,apply){
    const op=divideRequest(d);
    d.operation=null;
    apply.disabled=true;
    function feedback(text){
      if(status.textContent!==text)status.textContent=text;
      status.hidden=!text;
    }
    if(!mainCurrent(d)){feedback('工程已更新，请重新打开。');return;}
    if(!op){feedback('间隔数应为 1–4096 的整数。');return;}
    feedback('');
    if(d.divideFlight)return; // Its receipt checks the latest input before publishing.
    const ticket=d.divideFlight=++operationSerial;
    // Keep the last picture while awaiting the latest validated preview. Its
    // command is cleared above, so what is visible can never be applied stale.
    bridge.previewProject(JSON.stringify({base_revision:d.revision,operation:op}),raw=>{
      if(ticket!==operationSerial||operationDraft!==d||dialog!=='editor-pulse-operation'||!$('dialog')?.open)return;
      d.divideFlight=0;
      if(!mainCurrent(d)){feedback('工程已更新，请重新打开。');return;}
      const live=divideRequest(d);
      if(live?.interval_count!==op.interval_count||live?.snap_to_midi!==op.snap_to_midi){previewDivide(d,status,apply);return;}
      let result;try{result=JSON.parse(raw);}catch{result=null;}
      if(result?.error){feedback(result.error);return;}
      const report=result?.impact?.division_preview;
      if(report?.interval_count!==op.interval_count||!Array.isArray(report.points)||!Array.isArray(result?.tempo?.pulse_points)){
        feedback('无法预览，请重试。');return;
      }
      d.operation=op;
      d.preview=result.tempo;
      apply.disabled=false;
      requestRollDraw();
    });
  }
  function previewOperation(){
    const d=operationDraft;if(!d||dialog!=='editor-pulse-operation'||!$('dialog')?.open)return;
    const status=$('editor-pulse-status'),apply=document.querySelector('[data-do=editorPulseApply]');
    if(d.kind==='divide'){previewDivide(d,status,apply);return;}
    const ticket=++operationSerial;d.operation=null;d.preview=null;requestRollDraw();apply.disabled=true;
    if(!mainCurrent(d)){status.textContent='工程或编辑视图已更新，请重新打开操作。';return;}
    let op={type:'rhythm_intent'};
    if(d.kind==='opening'){
      op=openingFormOperation();
      if(!d.fixedOpening)updateOpeningReferences(op.seconds);
      if(!Number.isFinite(op.seconds)||op.seconds<0||op.seconds>duration()){status.textContent='请选择曲目范围内的有效位置。';return;}
    }
    if(d.kind==='meter_bars')Object.assign(op,{action:'meter_bars',start:d.range.start,end:d.range.end,numerator:Number($('editor-pulse-numerator').value),denominator:Number($('editor-pulse-denominator').value)});
    if(d.kind==='duration'){
      op=durationOperation(d.points,$('editor-pulse-duration')?.value);
      if(!op){status.textContent='选择新的谱面时值。';return;}
    }
    if(d.kind==='restore')op.action='restore_initial';
    status.textContent='正在检查…';bridge.previewProject(JSON.stringify({base_revision:d.revision,operation:op}),raw=>{
      if(ticket!==operationSerial||operationDraft!==d||dialog!=='editor-pulse-operation'||!$('dialog')?.open)return;
      let result;try{result=JSON.parse(raw);}catch{result={error:'无法读取检查结果。'};}
      if(!mainCurrent(d)){status.textContent='工程或编辑视图已更新，请重新打开操作。';return;}
      if(result.error){status.textContent=result.error;return;}
      d.operation=op;d.preview=result.tempo||null;
      const detail=[];
      if(d.kind==='meter_bars'){
        const impact=result.impact||{}, bars=impact.repartitioned_bars||[];
        detail.push(`保留小节线；保留 ${impact.preserved_point_count??0} 个原拍点，新增 ${impact.inserted_point_count??0} 个拍点。`);
        if(bars.length){
          const positions=bars.slice(0,4).map(b=>musicPosition(b.start)).join('、');
          detail.push(`${bars.length} 个小节的旧拍点无法对齐新拍号，将在原小节线之间重新均分（${positions}${bars.length>4?'等':''}），替换 ${impact.replaced_point_count??0} 个旧拍点。应用前可查看预览。`);
        }

      }
      if(d.kind==='opening')status.textContent='';else status.textContent=[...detail,...(result.warnings||[])].join(' ')||'可以应用。';
      if(d.kind==='restore'){apply.textContent='恢复节拍分析';apply.disabled=!$('editor-pulse-confirm')?.checked;}else apply.disabled=false;
      requestRollDraw();
    });
  }
  function changedOperation(id){if(id==='editor-pulse-opening-seconds'&&operationDraft)operationDraft.secondsEdited=true;if(id==='editor-pulse-confirm'){const d=operationDraft,b=document.querySelector('[data-do=editorPulseApply]');if(b)b.disabled=!(d?.operation&&$('editor-pulse-confirm')?.checked);return true;}if(id==='editor-trend-candidate'){if(trends)trends.selected=Number($('editor-trend-candidate')?.value)||0;requestRollDraw();return true;}if(id==='editor-insert-duration'){const value=Number($('editor-insert-duration')?.value);if(legalDuration(value))insertUnit=value;return true;}if(id==='editor-selected-beat-duration'){if(editable()&&validSelection()&&selection.kind==='beat'){const op=durationOperation(multi,$(id)?.value);if(op){const stamp=mainStamp();applyGesture(op,stamp.revision,stamp.project,null,()=>mainCurrent(stamp)&&validSelection()&&selection.kind==='beat'&&op.seconds.length===multi.length&&op.seconds.every(t=>multi.includes(t)));}}return true;}if(!id.startsWith('editor-pulse-'))return false;previewOperation();return true;}
  function detectTrends(){
    if(!editable())return;
    const r=hasRange()?range():{start:0,end:duration()},revision=editor().revision,project=state.project.path;
    trends={revision,project,candidates:[],selected:0};
    modal('速度趋势候选','<p class="form-note">识别已有拍点映射中的渐快 / 渐慢。确认只添加表达标记，不改变实际播放或拍点。</p><div id="editor-trend-list" role="status">正在识别…</div>',`${button('关闭','closeDialog')}${button('采用候选','editorApplyTrend',{disabled:true,style:'primary'})}`,'editor-trends');
    bridge.previewProject(JSON.stringify({base_revision:revision,operation:{type:'rhythm_intent',action:'detect_trends',start:r.start,end:r.end}}),raw=>{
      if(dialog!=='editor-trends'||trends?.revision!==revision||editor()?.revision!==revision||state.project.path!==project)return;
      let result;try{result=JSON.parse(raw);}catch{result={error:'无法读取候选。'};}
      const el=$('editor-trend-list');if(result.error){el.textContent=result.error;return;}
      trends.candidates=(result.impact?.candidates||[]).filter(c=>c.start_seconds>=r.start-1e-7&&c.end_seconds<=r.end+1e-7);
      const suspicious=result.impact?.suspicious_intervals||[];
      el.innerHTML=trends.candidates.length?`<label class="field">待确认<select class="control" id="editor-trend-candidate">${options(trends.candidates.map((c,i)=>[i,`${c.direction==='accelerando'||c.direction==='accelerating'||c.direction==='up'?'渐快':'渐慢'} · ${fmt(c.start_seconds)}–${fmt(c.end_seconds)} 秒`]),0)}</select></label><p class="form-note">色带为候选，不会自动采用。${suspicious.length?`另有 ${suspicious.length} 处突变，不归为平滑趋势。`:''}</p>`:'没有足够明确的渐快 / 渐慢候选；不作修改。';
      document.querySelector('[data-do=editorApplyTrend]').disabled=!trends.candidates.length;requestRollDraw();
    });
  }
  function applyTrend(){
    const t=trends,c=t?.candidates[Number($('editor-trend-candidate')?.value)||0];if(!c||t.revision!==editor()?.revision||t.project!==state.project.path)return;
    const op={type:'rhythm_intent',action:'apply_trend',start:c.start_seconds,end:c.end_seconds,direction:c.direction};
    const button=document.querySelector('[data-do=editorApplyTrend]');button.disabled=true;
    bridge.previewProject(JSON.stringify({base_revision:t.revision,operation:op}),raw=>{
      if(dialog!=='editor-trends'||trends!==t||t.revision!==editor()?.revision)return;
      let result;try{result=JSON.parse(raw);}catch{result={error:'无法读取检查结果。'};}
      if(result.error){$('editor-trend-list').textContent=result.error;return;}
      trends=null;$('dialog').close();edit(op);
    });
  }
  function rangeMenu(){
    if(!editable())return;
    modal('选区节拍',`<p>${hasRange()?html(musicPosition(range().start)+' → '+musicPosition(range().end)):'先在上方标尺拖选 L/R 范围。'}</p><div class="form-grid">${button('改拍号 · 保小节线','editorMeterBars',{disabled:!hasRange(),label:'保留小节线并尽量保留原拍点，先预览再应用'})}${button('自由拍号…','editorShortBar',{disabled:!hasRange(),label:'从所选拍点或左定位点到下一拍号；不改变演奏时间'})}${button('识别速度趋势','editorDetectTrends',{label:'只生成渐快渐慢候选，不自动修改播放或拍点'})}</div>`,button('关闭','closeDialog'),'editor-rhythm-range-menu');
  }
  function rhythmAction(name){
    if(name==='editorOpeningPrevious'||name==='editorOpeningNext'){stepOpeningReference(name==='editorOpeningPrevious'?'previous':'next');return true;}
    if(name==='editorRhythmRange'){rangeMenu();return true;}
    if(name==='editorDetectTrends'){detectTrends();return true;}
    if(name==='editorApplyTrend'){applyTrend();return true;}
    if(name==='editorMeterBars'){operationMenu('meter_bars');return true;}
    if(name==='editorShortBar'){operationMenu('short_bar');return true;}

    if(name==='editorDelete'&&validSelection()){
      if(selection.kind==='beat'&&multi.length){operationMenu('delete');return true;}
      if(selection.kind==='meter'){
        const stamp=mainStamp(),seconds=selection.seconds;
        const m=(editor()?.tempo?.time_signatures||[]).find(row=>row.beat>0&&samePoint(beatToSeconds(row.beat),seconds));
        if(m&&editable()&&mainCurrent(stamp))
          runContext({kind:'meter',seconds},{operation:{type:'rhythm_intent',action:'delete_meter',seconds}},stamp);
        return true;
      }
    }
    if(!['editorBeatDelete','editorBeatDivide','editorBeatMeter','editorOpening','editorOpeningRestore','editorPulseApply','editorPointOpening'].includes(name))return false;
    if(name==='editorBeatDelete')operationMenu('delete');if(name==='editorBeatDivide')operationMenu('divide');
    if(name==='editorBeatMeter'&&validSelection())directRhythmMenu('meter',selection.seconds);
    if(name==='editorOpening')operationMenu('opening');if(name==='editorPointOpening')operationMenu('opening',selection?.seconds,{fixedOpening:true});
    if(name==='editorOpeningRestore')operationMenu('restore');
    if(name==='editorPulseApply'){
      const d=operationDraft,op=d?.operation;
      if(dialog!=='editor-pulse-operation'||!$('dialog')?.open||!op||!mainCurrent(d)||!editable()||(d.kind==='restore'&&!$('editor-pulse-confirm')?.checked))return true;
      if(d.kind==='divide'){
        const current=divideRequest(d);
        if(current?.interval_count!==op.interval_count||current?.snap_to_midi!==op.snap_to_midi){previewOperation();return true;}
      }
      if(d.kind==='opening'){
        const current=openingFormOperation();
        if(current.seconds!==op.seconds||current.keep_before_bar1!==op.keep_before_bar1){previewOperation();return true;}
      }
      operationDraft=null;operationSerial++;$('dialog').close();edit(op);
    }
    return true;
  }
  function contextTarget(event){
    const id=event.target.id;
    if(gesture||host().isDragging?.()||(id==='beat-flags-canvas'?!eraseScope():!editable()||typeof bridge?.previewProject!=='function'))return null;
    const c=position(event);
    if(id==='ruler-canvas'){
      if(c.y<16||c.y>33)return null;
      const row=meterRowAt(c.x);
      return row?{kind:'meter',index:row.index,seconds:beatToSeconds(row.m.beat)}:null;
    }
    if(id==='opening-canvas')return {kind:'opening',seconds:openingHit(c)?openingSeconds():(nearestPulse(c.seconds)?.seconds??c.seconds)};
    if(id!=='beat-flags-canvas')return null;
    const flag=host().rhythmTool()==='eraseBeat'?eraseHit(eraseLane(event.target),c.x,c.y):host().rhythmFlagAt(c.x,c.y);if(!flag)return null;
    const seconds=flag.seconds,points=selected('beat',seconds)?[...multi].sort((a,b)=>a-b):[seconds];
    const end=duration()-1e-7,limits=eraseLimits(),mask=eraseMask();
    return {kind:'beat',seconds,points,terminal:seconds>=end,canSetDuration:editable()&&points.every(t=>t<end),
      canDelete:points.some(t=>!eraseBlock(t,limits)&&!mask?.has(t))};
  }
  function runContext(target,item,stamp){
    if(target.kind==='beat'&&(item.form==='delete'||['delete_beats','delete_beat'].includes(item.operation?.action))){
      if(stamp.project!==state.project?.path||stamp.revision!==editor()?.revision){eraseNotice(eraseStale);return;}
      erasePicked(target.points??[target.seconds]);return;
    }
    if(!mainCurrent(stamp)||!editable()){if(erasePending())eraseNotice('请等擦除保存完成后再进行其他节拍操作。');return;}
    // The clicked point anchors single-point commands; a selected member keeps the group.
    if(target.kind==='meter'||(target.kind==='beat'&&!selected('beat',target.seconds)))choose({kind:target.kind,seconds:target.seconds});
    if(item.form==='meter'||item.form==='free_meter')directRhythmMenu('meter',target.seconds,item.form==='free_meter'?'free':undefined);
    else if(item.form)operationMenu(item.form,target.seconds,{fixedOpening:item.form==='opening'});
    else applyGesture(item.operation,stamp.revision,stamp.project,null,()=>mainCurrent(stamp));
  }
  let positionCache=null;
  function positionBars(){
    const tempo=editor()?.tempo;if(!tempo?.anchors?.length)return [];
    const end=tempo.anchors.at(-1).beat,version=typeof rollInputVersion==='number'?rollInputVersion:0;
    if(positionCache?.tempo===tempo&&positionCache.end===end&&positionCache.version===version)return positionCache.rows;
    const sections=host().meterSections(tempo),rows=[];let number=1,intro=1;
    for(let i=0;i<sections.length;i++){
      const m=sections[i],unit=4/m.denominator,length=m.numerator*unit,limit=Math.min(end,sections[i+1]?.beat??end);
      if(!(length>0)||!(unit>0))continue;if(m.bar!==undefined)number=m.bar;
      for(let start=m.beat;start<limit-1e-9&&rows.length<100000;start+=length){
        const stop=Math.min(start+length,limit),key=m.opening_prefix?(m.smart_measure?'智能临时小节':m.free?'自由小节':`${m.displayNumerator||m.numerator}/${m.denominator}`):m.silence?`静音${intro++}`:m.pickup?'弱起':m.intro?`引${intro++}`:String(number);
        rows.push({key,start,end:stop,unit,offset:m.pickup?(m.numberOffset||0):0,number,full:Math.abs(stop-start-length)<1e-8});number++;
      }
    }
    const last=rows.at(-1);if(last?.full&&!last.key.startsWith('引')&&last.key!=='弱起')rows.push({key:String(last.number+1),start:end,end,unit:last.unit,offset:0,number:last.number+1,full:false});
    positionCache={tempo,end,version,rows};return rows;
  }
  function fractionText(value){
    if(Math.abs(value)<1e-9)return '0';
    for(const d of [2,3,4,6,8,12,16,24,32,64]){const n=Math.round(value*d);if(Math.abs(value-n/d)<1e-9)return `${n}/${d}`;}
    return String(Number(value.toFixed(9)));
  }
  function musicPosition(seconds){
    const q=secondsToBeat(seconds),rows=positionBars();if(!rows.length)return '';
    let lo=0,hi=rows.length;
    while(lo<hi){const mid=(lo+hi)>>>1;if(q<rows[mid].end-1e-8)hi=mid;else lo=mid+1;}
    const first=rows[lo],bar=first&&q>=first.start-1e-8?first:rows.at(-1);
    const local=(q-bar.start)/bar.unit+bar.offset,nearest=Math.round(local),whole=Math.abs(local-nearest)<1e-8?nearest:Math.floor(local),fraction=Math.abs(local-nearest)<1e-8?0:local-whole;
    return `${bar.key} | ${whole+1} | ${fractionText(fraction)}`;
  }
  function parseMusicPosition(text){
    const parts=String(text).trim().split(/[|｜]/).map(x=>x.trim());
    if(parts.length!==3)throw new Error('请输入 小节｜拍｜拍内细分，例如 2｜3｜1/2；0 表示拍头。');
    const key=/^\d+$/.test(parts[0])?String(Number(parts[0])):parts[0];
    const bar=positionBars().find(b=>b.key===key);if(!bar)throw new Error('找不到该小节。弱起请写“弱起”，引子请写“引1”等标记。');
    if(!/^\d+$/.test(parts[1])||Number(parts[1])<1)throw new Error('拍数必须为从 1 开始的整数。');
    let fraction;
    if(/^\d+\s*\/\s*\d+$/.test(parts[2])){const [n,d]=parts[2].split('/').map(Number);fraction=d?n/d:NaN;}
    else if(/^(?:\d+(?:\.\d*)?|\.\d+)$/.test(parts[2]))fraction=Number(parts[2]);
    if(!Number.isFinite(fraction)||fraction<0||fraction>=1)throw new Error('拍内细分须在 0（含）到 1（不含）之间，例如 0、1/2、1/3。');
    const q=bar.start+(Number(parts[1])-1+fraction-bar.offset)*bar.unit,final=bar===positionBars().at(-1);
    if(q<bar.start-1e-8||q>bar.end+1e-8||(!final&&q>=bar.end-1e-8))throw new Error('该拍位不在此小节内：请检查变拍、短小节或弱起的实际范围。');
    const seconds=beatToSeconds(Math.min(bar.end,Math.max(bar.start,q)));
    if(!Number.isFinite(seconds)||seconds<0||seconds>duration()+1e-6)throw new Error('音乐位置超出当前曲目。');
    return seconds;
  }
  function selectionMarkup(){
    if(!validSelection()||(selection.kind==='beat'&&!multi.length))return '';
    const isBeat=selection.kind==='beat',count=multi.length,allowed=editable();
    const meter=(editor().tempo.time_signatures||[]).find(m=>Math.abs(beatToSeconds(m.beat)-selection.seconds)<1e-7);
    const title=isBeat?(count===1?'拍点':count+' 个拍点'):meter?.smart_measure?'智能临时小节':meter?.free?'自由小节':meter?`${meter.numerator}/${meter.denominator}`:'拍号';
    const time=isBeat&&count>1?`${fmt(Math.min(...multi))}–${fmt(Math.max(...multi))} 秒`:`${fmt(selection.seconds)} 秒`;
    return `<div class="rhythm-selection-card"><div class="rhythm-summary"><div><strong>${html(title)}</strong><small>${html(time)}</small></div></div>
      <div class="rhythm-object-actions">
      ${isBeat?button('删除拍点','editorBeatDelete',{disabled:!allowed||!count}):''}</div></div>`;
  }
  function drawSelection(ctx,width,offset){
    const g=gesture;if(g?.kind!=='box')return;
    const x=Math.min(midiX(g.seconds),midiX(g.to))-offset,w=Math.abs(midiX(g.to)-midiX(g.seconds));
    ctx.fillStyle='rgba(117,96,175,.12)';ctx.fillRect(x,0,w,26);ctx.strokeStyle='#7560af';ctx.strokeRect(x+.5,.5,w,25);
    ctx.fillStyle='#393645';ctx.font='10px AstraSans';ctx.fillText(`${multi.length} 拍点`,Math.max(2,Math.min(width-55,x+4)),12);
  }
  function pendingBeatAim(){
    const r=beatRequest,g=r?.aim;
    return r?.show&&g&&r.revision===editor()?.revision&&beatProjectCurrent(r)&&g.target.isConnected!==false?g:null;
  }
  function hasBeatView(){return Boolean(isBeatAim(gesture)||meterDisplay()||hoverAim||pendingBeatAim()||window.AstraBeatRecorder?.wantsOverlay?.()||beatLayer&&beatLayer.style.display!=='none');}
  function beatEmphasis(g,saving){
    const note=g.aim?.note?.id,edge=g.aim?.edge;
    if(g.hover||(!g.aim?.note&&!saving)){
      if(beatAccent){window.AstraRhythmMotion?.stopSettle('beat-aim');beatAccent=null;}
      return 0;
    }
    if(beatAccent?.g!==g||beatAccent.seconds!==g.to||beatAccent.note!==note||beatAccent.edge!==edge||beatAccent.saving!==saving){
      const pulse=beatAccent={g,seconds:g.to,note,edge,saving,amount:0};
      // A small elastic accent on the ring, not a spring on its coordinate.
      window.AstraRhythmMotion?.settle('beat-aim',100,(_ease,t)=>{pulse.amount=Math.sin(Math.PI*t)*(1-t);},beatFrame);
    }
    return beatAccent.amount;
  }

  function paintMeterAim(g,saving){
    const view=meterView(g),{rect,offset,project}=view;
    if(!saving)resolveMeterAim(g,view);
    const workspace=$('workspace')?.getBoundingClientRect?.();
    const maxRight=rect.left+(g.scroll?.clientWidth||0);
    const rulerClip=intersection({left:rect.left,right:Math.min(rect.right,maxRight),top:rect.top+16,bottom:Math.min(rect.bottom,rect.top+33)},workspace??rect);
    const flags=$('beat-flags-canvas'),flag=flags?.parentElement?.hidden?null:rollPointerRect(flags);
    const flagClip=flag?intersection({left:flag.left,right:Math.min(flag.right,flag.left+g.width),top:flag.top+4,bottom:Math.min(flag.bottom,flag.top+23)},workspace??flag):null;
    if(!rulerClip){hideBeatLayer();return;}
    const left=Math.floor(Math.min(rulerClip.left,flagClip?.left??Infinity)),top=Math.floor(Math.min(rulerClip.top,flagClip?.top??Infinity));
    const width=Math.ceil(Math.max(rulerClip.right,flagClip?.right??-Infinity))-left,height=Math.ceil(Math.max(rulerClip.bottom,flagClip?.bottom??-Infinity))-top;
    const ctx=guideLayer(left,top,width,height),y=rect.top+17-top;
    const x=rect.left-left+(saving?project(g.to):g.drawX)-offset;
    const canLand=g.pulse&&!g.blocked,ink=g.blocked?'#a13f55':canLand?'#5543a0':'#655976';
    const label=g.item.smart_measure?'智能':g.item.free?'自由':g.item.numerator+'/'+g.item.denominator;
    // Say what releasing here does, before release. Text follows the stable
    // hit state only; it is never a transient "pending" cue.
    const home=canLand&&!saving&&samePoint(g.to,g.seconds);
    const detail=g.blocked?'已有拍号 · 松开不移动':home?'原位 · 不移动':canLand?'落点 '+fmt(g.to)+' s':g.magnet?'空白 · 松开不移动':'自由 · 对准拍点才可落';
    // Only an occupied target is a definite refusal; blank space keeps the
    // grab cursor so crossing gaps between beats never flickers the pointer.
    if(!saving&&gesture===g)meterCursor(g,g.blocked?'not-allowed':'grabbing');
    const exact=rect.left-left+project(g.to)-offset;
    ctx.save();ctx.beginPath();ctx.rect(rulerClip.left-left,rulerClip.top-top,rulerClip.right-rulerClip.left,rulerClip.bottom-rulerClip.top);ctx.clip();
    // One line follows the existing release target, with a muted aim on
    // blank space where release still does not edit the time signature.
    ctx.fillStyle=ink;ctx.globalAlpha=saving?1:(g.pulse ? .3+.7*g.strength : .45);
    ctx.fillRect(exact,y,pencilGuideInk.width,15);ctx.globalAlpha=1;
    ctx.fillStyle=canLand?'#e0d7eb':'#f2ebf6';ctx.fillRect(x+3,y,32,15);
    ctx.fillStyle=ink;ctx.font='11px AstraSans';ctx.fillText(label,x+6,y+11);
    const start=x+41,end=rulerClip.right-left,minimum=rulerClip.left-left;
    const span=ctx.measureText(detail).width+8,tx=start+span<=end?start:x-5-span>=minimum?x-5-span:null;
    if(tx!==null){ctx.fillStyle='rgba(248,248,255,.96)';ctx.fillRect(tx,y,span,15);ctx.fillStyle=ink;ctx.fillText(detail,tx+4,y+11);}
    ctx.restore();
    if(g.pulse&&flagClip){
      const px=flag.left-left+project(g.to)-offset,py=flag.top+4-top;
      ctx.save();ctx.beginPath();ctx.rect(flagClip.left-left,py,flagClip.right-flagClip.left,flagClip.bottom-flagClip.top);ctx.clip();
      ctx.fillStyle=ink;ctx.globalAlpha=saving?1:.25+.75*g.strength;ctx.fillRect(px,py,1,flagClip.bottom-flagClip.top);
      ctx.fillRect(px-2,py+9,5,2);ctx.restore();
    }
  }
  const pencilGuideInk=Object.freeze({color:"#5543a0",width:1,rawOpacity:.38,splitThreshold:.5});
  function guideLayer(left,top,width,height){
    if(!beatLayer){
      beatLayer=document.createElement('canvas');beatLayer.id='beat-aim-overlay';beatLayer.setAttribute('aria-hidden','true');
      beatLayer.style.cssText='position:fixed;pointer-events:none;z-index:3;contain:strict;';document.body.appendChild(beatLayer);
    }
    const style=beatLayer.style;
    if(style.left!==left+'px')style.left=left+'px';
    if(style.top!==top+'px')style.top=top+'px';
    if(style.display!=='block')style.display='block';
    const ctx=canvasContext(beatLayer,width,height);ctx.clearRect(0,0,width,height);
    return ctx;
  }
  function paintBeatAim(){
    syncBeat();
    const meter=meterDisplay();if(meter){paintMeterAim(meter,meter===meterLanding?.g);return;}
    const live=isBeatAim(gesture)?gesture:null,saving=!live&&pendingBeatAim();
    const recAim=!live&&!saving?window.AstraBeatRecorder?.activeBeatAim?.():null;
    const g=live||saving||recAim||(!gesture&&!pending&&!beatRequest&&!dialog?hoverAim:null);
    if(!g){
      if(window.AstraBeatRecorder?.wantsOverlay?.()||window.AstraBeatRecorder?.recording?.()){
        const v=beatView();
        if(window.AstraBeatRecorder.paintOverlay(guideLayer,v.rollClip,v.flagClip,v.note,v.flag,v.offset,v.axis?.x??midiX))return;
      }
      hideBeatLayer();return;
    }
    const v=beatView(g.target),{axis,offset,rect,note,flag,rollClip,flagClip}=v,project=axis?.x??midiX,invert=axis?.seconds??midiSeconds;
    if(g.hover){
      const e=g.lastEvent,hit=host().rhythmFlagAt(e.clientX-rect.left,e.clientY-rect.top);
      host().rhythmPencilHover(e,hit||null);
      if(hit){hideBeatLayer();return;}
    }
    if(!saving&&!g.fromRecorder)resolveBeatAim(g,v);
    const clips=[rollClip,flagClip].filter(Boolean);if(!clips.length){hideBeatLayer();return;}
    const left=Math.floor(Math.min(...clips.map(c=>c.left))),top=Math.floor(Math.min(...clips.map(c=>c.top)));
    const width=Math.ceil(Math.max(...clips.map(c=>c.right)))-left,height=Math.ceil(Math.max(...clips.map(c=>c.bottom)))-top;
    const ctx=guideLayer(left,top,width,height);
    const accent=beatEmphasis(g,Boolean(saving)),ink=g.blocked||g.limited?'#a13f55':pencilGuideInk.color;
    const split=!saving&&Math.abs(project(g.raw)-project(g.to))>pencilGuideInk.splitThreshold;
    const label=g.blocked?'固定边界 · 不移动':g.limited?'相邻边界':g.aim?.note?(g.aim.edge==='end'?'音尾':'音首'):g.kind==='insert'?'新增拍点':'校准目标';
    const text=`${saving?'保存中 · ':''}${label} ${fmt(g.to)} s${g.points?.length>1?' · '+g.points.length+' 点':''}`;
    for(const [clip,origin,lane] of [[rollClip,note,false],[flagClip,flag,true]]){
      if(!clip)continue;
      const x0=clip.left-left,y0=clip.top-top,w=clip.right-clip.left,h=clip.bottom-clip.top;
      const xFor=t=>origin.left-left+project(t)-offset,x=xFor(g.to),raw=xFor(g.raw);
      ctx.save();ctx.beginPath();ctx.rect(x0,y0,w,h);ctx.clip();ctx.fillStyle=ink;
      if(split){
        ctx.globalAlpha=pencilGuideInk.rawOpacity;ctx.fillRect(raw,y0,pencilGuideInk.width,h);
        if(lane){ctx.fillRect(Math.min(raw,x),y0+16,Math.abs(raw-x),1);ctx.fillRect(raw-2,y0+14,5,5);}
        ctx.globalAlpha=1;
      }
      ctx.fillRect(x,y0,pencilGuideInk.width,h);
      if(lane){
        if(g.points?.length>1&&!g.blocked){
          const shift=g.to-g.seconds,from=invert(offset+clip.left-origin.left)-shift,to=invert(offset+clip.right-origin.left)-shift,rows=g.points;
          let lo=0,hi=rows.length;while(lo<hi){const mid=(lo+hi)>>>1;if(rows[mid]<from)lo=mid+1;else hi=mid;}
          let last=-Infinity;
          for(let i=lo;i<rows.length&&rows[i]<=to;i++){
            const px=Math.round(xFor(rows[i]+shift));if(px===last)continue;last=px;ctx.fillRect(px,y0+20,1,6);
          }
        }
        const radius=3+2*accent;
        ctx.strokeStyle=ink;ctx.lineWidth=1.25;ctx.beginPath();ctx.arc(x,y0+16,radius,0,2*Math.PI);ctx.stroke();
      }else if(g.aim?.note&&rollGeometry){
        const y=origin.top-top+(rollGeometry.high-g.aim.note.pitch)*rollGeometry.row;
        // Mark the actual edge, not the entire note or its minimum-width fill.
        ctx.fillRect(x-1,y,3,rollGeometry.row);ctx.fillRect(x-3,y,7,1);ctx.fillRect(x-3,y+rollGeometry.row-1,7,1);
      }
      if((lane||!flagClip)&&w>20&&x>=x0-1&&x<=x0+w){
        ctx.font='11px AstraSans';const tw=Math.min(w-4,ctx.measureText(text).width+8),tx=Math.max(x0+2,Math.min(x+7,x0+w-tw-2));
        ctx.fillStyle='rgba(248,248,255,.96)';ctx.fillRect(tx,y0+1,tw,14);
        ctx.fillStyle=ink;ctx.fillText(text,tx+4,y0+12,Math.max(1,tw-8));
      }
      ctx.restore();
    }
  }
  function previewAim(){
    if(gesture?.kind==='erase')return null;
    if(gesture?.kind==='meter'||meterLanding){
      const g=gesture?.kind==='meter'?gesture:meterLanding.g,saving=g!==gesture;
      if(saving?!meterLandingCurrent(g):!meterInputAlive(g))return null;
      if(!saving)resolveMeterAim(g,meterView(g));
      return {seconds:g.to,source_seconds:g.seconds,raw_seconds:g.raw,visual_x:g.drawX,
        target_seconds:g.pulse?.seconds??null,blocked:Boolean(g.blocked||g.fixed),magnet:g.magnet,pending:saving};
    }
    const live=isBeatAim(gesture)?gesture:null,g=live||pendingBeatAim()||gesture||aimPending;
    if(!g)return null;
    if(live){if(!beatInputAlive(g))return null;resolveBeatAim(g,beatView(g.target));}
    return {seconds:g.to,source_seconds:g.seconds,raw_seconds:g.raw,note_id:g.aim?.note?.id??null,edge:g.aim?.edge??null,
      limited:Boolean(g.limited),blocked:Boolean(g.blocked),pending:g!==gesture};
  }
  function draw(ctx,ruler,offset,height){
    if(ruler){
    const candidate=dialog==='editor-trends'&&trends?.revision===editor()?.revision?trends.candidates:[];
    const saved=editor()?.tempo?.tempo_expression_annotations||[];
    for(const c of [...saved,...candidate]){
      const start=c.start_seconds??c.start,end=c.end_seconds??c.end;
      if(!Number.isFinite(start)||!Number.isFinite(end))continue;
      const up=['accelerando','accelerating','up'].includes(c.direction),x=midiX(start)-offset,w=midiX(end)-midiX(start);
      const picked=candidate.includes(c)&&candidate.indexOf(c)===(trends?.selected||0);ruler.fillStyle=up?(picked?'rgba(230,184,112,.42)':'rgba(230,184,112,.22)'):(picked?'rgba(130,181,214,.42)':'rgba(130,181,214,.22)');ruler.fillRect(x,0,w,15);
      ruler.fillStyle=up?'#7560af':'#7d65ab';ruler.fillText((candidate.includes(c)?'候选 ':'')+(up?'↗ 渐快':'↘ 渐慢'),x+4,12);
    }

    for(const bar of editor()?.tempo?.short_bar_annotations||[]){
      const x=midiX(bar.start_seconds)-offset,w=midiX(bar.end_seconds)-midiX(bar.start_seconds);
      ruler.fillStyle='#786995';ruler.fillRect(x,53,w,2);if(x>=0){ruler.save();ruler.font='11px AstraSans';const label='短小节',tw=ruler.measureText(label).width;ruler.fillStyle='#eae3f4';ruler.fillRect(x+3,16,tw+8,15);ruler.fillStyle='#584080';ruler.fillText(label,x+7,28);ruler.restore();}
    }
    const recMask=window.AstraBeatRecorder?.previewGridMask?.();
    const rhythmPrev=!recMask&&dialog==='editor-pulse-operation'&&$('dialog')?.open&&operationDraft?.preview&&mainCurrent(operationDraft)&&['divide','meter_bars'].includes(operationDraft.kind)?operationDraft.preview:null;
    const meterMap=rhythmPrev||recMask?.tempo||null;
    const meterRows=(meterMap?.time_signatures||editor()?.tempo?.time_signatures)||[];
    const mapBeatSec=meterMap?.anchors?.length>1?(q=>{const rows=meterMap.anchors;let i=0;while(i+1<rows.length-1&&rows[i+1].beat<=q)i++;const sp=rows[i+1].beat-rows[i].beat;return sp>0?rows[i].seconds+(q-rows[i].beat)*(rows[i+1].seconds-rows[i].seconds)/sp:rows[i].seconds;}):beatToSeconds;
    meterRows.forEach((m,i)=>{if(!m.free)return;const start=mapBeatSec(m.beat),end=i+1<meterRows.length?mapBeatSec(meterRows[i+1].beat):duration();if(recMask?.regions?.some(r=>r.start<=start+1e-6&&r.end>=end-1e-6))return;const x=midiX(start)-offset,w=midiX(end)-midiX(start);ruler.fillStyle='rgba(182,166,217,.2)';ruler.fillRect(x,16,w,17);});
    const draft=dialog==='editor-pulse-operation'?operationDraft:null;
    if(draft?.preview&&draft.revision===editor()?.revision&&['divide','meter_bars'].includes(draft.kind)){
      const op=draft.operation;
      ruler.fillStyle='rgba(230,184,112,.18)';ruler.fillRect(midiX(op.start)-offset,34,midiX(op.end)-midiX(op.start),22);
      for(const p of draft.preview.pulse_points||[]){if(p.seconds<op.start-1e-7||p.seconds>op.end+1e-7)continue;const x=midiX(p.seconds)-offset;ruler.fillStyle='#7560af';ruler.fillRect(x,36,2,18);}
    }

    }
    reconcileSelection();
    const stale=selection&&(selection.revision!==editor()?.revision||selection.project!==state.project?.path);
    // Keep only the inert identity until this request acknowledges; a user
    // picking anything else replaces it. Never paint the stale source.
    if(stale&&!(selectionRemap?.meter&&meterSelectionCurrent(selectionRemap))){selection=null;multi=[];}
    if(ruler&&!stale&&selection?.kind==='meter'&&!meterDisplay()){const x=midiX(selection.seconds)-offset;ruler.strokeStyle='#7d65ab';ruler.strokeRect(x+2,16,35,17);}
    if(aimPending&&(aimPending.revision!==editor()?.revision||aimPending.project!==state.project?.path))aimPending=null;
    // Beat and pencil guides have their own damage bit; the durable raster is
    // unchanged by aiming. The opening retains its existing paint path.
    const aiming=gesture?.kind==='opening'?gesture:!gesture?aimPending:null;
    if(aiming){
      const x=midiX(aiming.to)-offset;
      ctx.fillStyle='#7560af';ctx.fillRect(x,0,2,height);
      if(ruler){ruler.fillStyle='#7560af';ruler.fillRect(x,16,2,40);}
    }
  }
  let directRhythm=null,directSerial=0;
  function directRhythmMenu(kind='meter',at,initialMode){
    const stamp=mainStamp();if(!editable()||!mainCurrent(stamp))return;
    const r=range(),explicit=Number.isFinite(at),useRange=kind!=='meter'&&!explicit&&hasRange(),start=explicit?at:kind==='meter'?(validSelection()?selection.seconds:nearestPulse(state.midi.position||0)?.seconds??0):useRange?r.start:(state.midi.position||0),q=secondsToBeat(start);
    const m=[...(editor().tempo.time_signatures||[])].reverse().find(m=>m.beat<=q+1e-7)||{numerator:4,denominator:4};
    if(kind==='recount'&&!hasRange())return;
    directRhythm={...stamp,kind,operation:null,scopeRange:kind==='recount'?{start:r.start,end:r.end}:null};
    modal(kind==='meter'?'修改拍号':'将选区设为指定小节数',`<p class="form-note">音符、CC64 与音频的实际时间保持不变。${kind==='meter'?'只改变小节分组，不改变速度。':'只改变记拍方式与 BPM 映射；音频和 MIDI 的实际播放速度不变。'}</p>
      <div id="editor-rhythm-scope" data-scope="${kind==='meter'?'point':'range'}"></div>
      ${kind==='meter'?`<label class="field">拍号类型<select class="control" id="editor-rhythm-meter-kind">${options([['metered','常规拍号'],['free','自由（到下一拍号）']],initialMode||(Math.abs(m.beat-q)<1e-7&&m.free?'free':'metered'))}</select></label>`:''}
      <div class="form-grid"><label class="field">${kind==='meter'?'拍号位置':'L'} · 秒<input class="control" id="editor-rhythm-start" type="number" min="0" step="any" readonly value="${String(kind==='recount'?r.start:start)}"></label><label class="field" id="editor-rhythm-end-field" ${kind==='meter'?'hidden':''}>R · 秒<input class="control" id="editor-rhythm-end" type="number" min="0" step="any" readonly value="${String(r.end)}"></label><label class="field" id="editor-rhythm-numerator-field">拍号分子<input class="control" id="editor-rhythm-numerator" type="number" min="1" max="32" value="${m.numerator}"></label><label class="field" id="editor-rhythm-denominator-field">拍号分母<select class="control" id="editor-rhythm-denominator">${options([1,2,4,8,16,32,64,128].map(x=>[x,x]),m.denominator)}</select></label>${kind==='recount'?`<label class="field">小节数<input class="control" id="editor-rhythm-bars" type="number" min="1" step="1" value="1"></label><label class="field">内部拍点<select class="control" id="editor-rhythm-distribution"><option value="preserve">保留已有速度起伏</option><option value="uniform">均匀分布</option></select></label>`:''}</div>
      <p class="form-note" id="editor-rhythm-scope-help">${kind==='meter'?'当前起点之前保持不变；可直接在拍号行双击选择位置。':'将替换选区内部的拍号变化；选区外保留。'}</p><p role="status" aria-live="polite" id="editor-rhythm-status">正在检查…</p>`,`${button('取消','closeDialog')}${button('应用','editorApplyDirectRhythm',{style:'primary',disabled:true})}`,'editor-rhythm');
    previewDirectRhythm();
  }
  function previewDirectRhythm(){
    if(dialog!=='editor-rhythm'||!$('dialog')?.open||!directRhythm)return;
    const d=directRhythm,scope=$('editor-rhythm-scope').dataset.scope,isRange=scope==='range',endField=$('editor-rhythm-end-field'),
      free=d.kind==='meter'&&$('editor-rhythm-meter-kind').value==='free';
    for(const key of ['numerator','denominator']){
      $(`editor-rhythm-${key}-field`).hidden=free;
      $(`editor-rhythm-${key}`).disabled=free;
    }
    if(endField)endField.hidden=!isRange;
    $('editor-rhythm-scope-help').textContent=isRange?'替换 L/R 内的拍号标记，终点恢复原拍号。':free?'不要求固定拍数；保留拍点、速度与演奏时间。MIDI 仍使用数字拍号并另标记自由段。':'从此拍点持续到下一拍号；只改变小节分组。';
    const start=d.scopeRange?.start??Number($('editor-rhythm-start').value),end=d.scopeRange?.end??Number($('editor-rhythm-end').value),
      numerator=Number($('editor-rhythm-numerator').value),denominator=Number($('editor-rhythm-denominator').value);
    const status=$('editor-rhythm-status'),apply=document.querySelector('[data-do=editorApplyDirectRhythm]');apply.disabled=true;d.operation=null;const serial=++directSerial;
    if(!mainCurrent(d)){status.textContent='工程或编辑视图已更新，请重新打开操作。';return;}
    if(!Number.isFinite(start)||start<0||start>duration()||(isRange&&(!Number.isFinite(end)||end<=start||end>duration()))){status.textContent='请选择有效时间；右边界必须晚于左边界且不超过曲末。';return;}
    const operation=d.kind==='meter'?(free?{type:'rhythm_intent',action:'set_free_meter',seconds:start}:{type:'rhythm_intent',action:'set_meter',seconds:start,numerator,denominator}):{type:'rhythm_intent',action:'recount',start,end,numerator,denominator,bar_count:Number($('editor-rhythm-bars').value),distribution:$('editor-rhythm-distribution').value};
    status.textContent='正在检查边界与固定拍点…';
    bridge.previewProject(JSON.stringify({base_revision:d.revision,operation}),raw=>{
      if(serial!==directSerial||dialog!=='editor-rhythm'||!$('dialog')?.open||directRhythm!==d)return;
      let result;try{result=JSON.parse(raw);}catch{result={error:'无法读取检查结果，请重试。'};}
      if(!mainCurrent(d)){status.textContent='工程或编辑视图已更新，请重新打开操作。';return;}
      if(result.error){status.textContent=result.error;return;}
      const detail=[];
      const smart=result.impact?.smart_measures||[];
      if(!free&&smart.length)detail.push(`将保留 ${smart.length} 个不足一小节的片段为智能临时小节，不移动拍点。`);
      if(free&&Number.isFinite(result.impact?.end_seconds)){
        const end=result.impact.end_seconds;
        detail.push(`自由区间：${fmt(start)} – ${fmt(end)} 秒（${end>=duration()-1e-7?'曲末':'到下一拍号'}）。`);
        const existing=(editor().tempo.short_bar_annotations||[]).length,after=(result.tempo?.short_bar_annotations||[]).length;
        if(existing>after)detail.push(`覆盖 ${existing-after} 处已有短小节标记。`);
      }
      d.operation=operation;status.textContent=[...detail,...(result.warnings||[])].join(' ')||'可以应用，可用撤销恢复。';apply.disabled=false;
    });
  }
  function applyDirectRhythm(){
    const d=directRhythm;if(dialog!=='editor-rhythm'||!$('dialog')?.open||!d?.operation||!mainCurrent(d)||!editable())return;
    const operation=d.operation;directRhythm=null;directSerial++;$('dialog').close();edit(operation);
  }

  // Dismiss only a time-signature selection; the original press still reaches
  // the ruler, notes, or controller lane and owns its usual gesture.
  document.addEventListener('pointerdown',event=>{
    if(event.button!==0||selection?.kind!=='meter'||!validSelection()||gesture?.kind==='meter')return;
    const target=event.target;
    if(target.closest?.('#dialog, #rhythm-context-menu, [data-disclosure="editor-command-meter"], [data-do="editorMeterEvent"], .rhythm-object-actions [data-do="editorBeatMeter"]'))return;
    if(target.id==='ruler-canvas'){
      const {x,y}=position(event);
      if(y>=16&&y<=33&&markerAt(x))return;
    }
    selectionRemap=null;
    lastMeterClick=null;
    choose(null);
  },true);
  window.addEventListener('blur',()=>{cancel();invalidateBeatView();});
  window.addEventListener('pagehide',()=>{const s=erase;if(s){clearUnsentErase(s);retireErase(s);}invalidateBeatView();});
  document.addEventListener('visibilitychange',()=>{if(document.hidden)invalidateBeatView();});
  document.addEventListener('lostpointercapture',event=>{if(gesture?.pointer===event.pointerId)cancel();},true);
  // Roll scrolling is covered by app.js; a scrolled ancestor can also move the
  // fixed overlay without changing the roll's own scrollLeft/scrollTop.
  document.addEventListener('scroll',event=>{if(hasBeatView()&&event.target!==$('roll-scroll'))beatFrame();},true);
  return {contextTarget,runContext,selectionInfo:()=>validSelection()?{kind:selection.kind,seconds:selection.seconds,beats:multi.slice()}:null,drawSelection,meterLayout,meterGhostSource:()=>meterDisplay()?.item,
    // Hover hit for the ruler cursor only; press still re-hits with live geometry.
    meterHit:x=>{if(gesture||!editable()||window.AstraBeatRecorder?.locksRhythm?.())return null;const m=markerAt(x);return m?{fixed:m.beat===0,name:m.smart_measure?'智能临时小节':m.free?'自由小节':''}:null;},
    openingHitAt:event=>Boolean(editable()&&!window.AstraBeatRecorder?.locksRhythm?.()&&openingHit(position(event))),
    sharedFlagHit,sharedFlagChoose,sharedFlagBox,sharedFlagDragged,sharedFlagBounds,sharedFlagDestination,insertBeatUnit:()=>insertUnit,
    previewTempo:()=>(dialog==='editor-pulse-operation'&&$('dialog')?.open&&operationDraft?.preview&&mainCurrent(operationDraft)&&['divide','meter_bars'].includes(operationDraft.kind))?operationDraft.preview:null,
    pencilGuideInk,previewAim,paintBeatAim,hasBeatView,invalidateBeatView,syncBeat,cancelBeat,eraseMask,eraseBlockAt,
    refreshSnap:()=>{const g=gesture||hoverAim;if(isBeatAim(g)||g?.kind==='meter'){g.resetSnap=g.dirty=true;beatFrame();}else if(g?.lastEvent)move(g.lastEvent);},
    clearSelection:()=>{selection=null;multi=[];selectionRemap=null;},clearHover:clearBeatHover,hasPending:()=>Boolean(pending||beatRequest||addQueue.length||erasePending()),action:rhythmAction,changedOperation,drawOpening,restoreViewport,durationToolbar,durationInfo,musicPosition,parseMusicPosition,positionBars,selectionMarkup,down,move,up,double,cancel,draw,selected,open:directRhythmMenu,changed:previewDirectRhythm,apply:applyDirectRhythm,isDragging:()=>Boolean(gesture)};
})();
