"use strict";
// Detached structural objects; the effective seconds map remains owned by the project editor.
window.AstraStructureEditor=(()=>{
  let session=null,project='',serial=0,busy=false,stale=false,message='',selected=null,rows=[],flagCache=null,closeRequested=false,blankDown=null,blankClick=null,blankDouble=null;
  const doc=()=>state.project?.editor;
  const enabled=()=>Boolean(session&&!stale&&project===state.project?.path&&session.base_revision===doc()?.revision&&(window.AstraProjectEditor?.active?.()??true));
  const fmt=s=>Number(s).toFixed(3);
  const sameScope=(a,b)=>JSON.stringify(a??null)===JSON.stringify(b??null);
  function clear(){serial++;session=null;rows=[];selected=null;busy=false;stale=false;closeRequested=false;blankDown=blankClick=blankDouble=null;message='';window.AstraRhythmContextMenu?.reconcile();refreshHistory();requestRollDraw();}
  function canHistory(kind){return enabled()&&!busy&&Boolean(session?.[kind==='undo'?'can_undo':'can_redo']);}
  function refreshHistory(){for(const [kind,name] of [['undo','editorUndo'],['redo','editorRedo']]){const b=document.querySelector(`[data-do=${name}]`);if(b){b.disabled=session?!canHistory(kind):!doc()?.['can_'+kind];b.title=session?`结构草稿${kind==='undo'?'撤销':'重做'}（尚未写入工程）`:kind==='undo'?'撤销':'重做';}}}
  function history(kind){if(!session)return false;if(canHistory(kind))call(kind);return true;}
  function sync(){
    if(!session)return;
    if(project!==state.project?.path){clear();return;}
    if(!stale&&(session.base_revision!==doc()?.revision||!sameScope(session.draft.scope,window.AstraProjectEditor?.localScope?.()))){stale=true;message='工程版本或明确编辑范围已改变，草稿已失效；当前工程没有被覆盖。请丢弃后重新进入。';paint();}
  }
  function rebuild(){
    flagCache=null;const d=session.draft,bars=d.display?.bars||d.bars,flags=d.display?.beat_flags||d.pulses;let bi=0,number=0;
    rows=flags.map(f=>{while(bi+1<bars.length&&bars[bi+1].seconds<=f.seconds+1e-7){bi++;number=0;}number++;return {...f,number:bars[bi]?.exception==='opening'?'弱':number,bar:Math.abs(bars[bi]?.seconds-f.seconds)<1e-7,locked:f.seconds<=1e-7||Math.abs(f.seconds-bars.at(-1).seconds)<1e-7,beat:typeof secondsToBeat==='function'?secondsToBeat(f.seconds):0,draft:true};});
    if(selected){const group=selected.kind==='label'?d.labels:selected.kind==='bar'?d.bars:selected.kind==='pulse'?rows:null;if(group&&!group.some(r=>Math.abs(r.seconds-selected.seconds)<1e-7))selected=null;}
  }
  function body(){
    const d=session?.draft,off=busy||stale,range=d?.scope,conflicts=d?.conflicts||[];
    return `<label class="checkbox-line"><input id="structure-preserve" type="checkbox" ${d?.preserve_unedited_positions!==false?'checked':''} ${off?'disabled':''}>锁定现有状态</label><p class="form-note">保留未明确编辑的位置；手动锁点始终受保护。小节重新编号不是冲突。</p><p class="structure-scope">${range?`仅框内 ${fmt(range.start)} – ${fmt(range.end)} 秒；框外位置和拍号保持。`:'未框选：修改明确选中的拍位或标签，预览其后续影响。'}</p><p role="status" class="structure-status ${stale?'error':''}">${html(message||session?.draft.summary||'点击旗帜选择；空白双击补拍，Delete 删除所选拍。')}</p><small>${session?.operation_count||0} 项待应用 · 试听保持不变，尚未保存</small>
    ${conflicts.length&&range&&d?.preserve_unedited_positions===false?button('按当前拍号重分组选区','structureRegroup',{disabled:off}):''}${conflicts.map((c,i)=>`<section class="structure-conflict"><strong>${html(c.message)}</strong>${c.suggestions?.length?`<p>${html(c.suggestions.join('；'))}</p>`:''}<div>${Number.isFinite(c.seconds)?button('定位','structureLocate',{key:String(i),disabled:busy}):''}${c.proposed_operation?button('采用此解决方式','structureResolve',{key:String(i),disabled:off}):''}</div></section>`).join('')}
    <div class="structure-target">${selected?`<strong>${{pulse:'拍位',bar:'小节线',label:'拍号标签',empty:'拟补拍位置'}[selected.kind]} ${fmt(selected.seconds)} 秒</strong><div class="structure-actions">${selected.kind==='empty'?button('补一拍','structureAddPulse',{disabled:off}):''}${selected.kind==='pulse'?button('删除此拍','structureDeletePulse',{disabled:off}):''}${selected.kind==='pulse'?button('设为小节起点','structureAddBar',{disabled:off}):''}${selected.kind==='bar'?button('删除小节线','structureDeleteBar',{disabled:off}):''}${selected.kind==='label'?button('删除拍号标签','structureDeleteMeter',{disabled:off}):''}</div>${selected.kind!=='empty'?`<label class="field">拍号<div class="structure-meter"><input id="structure-numerator" class="control" type="number" min="1" max="32" value="${selected.numerator||4}" ${off?'disabled':''}><span>/</span><select id="structure-denominator" class="control" ${off?'disabled':''}>${[1,2,4,8,16,32,64,128].map(n=>`<option ${n===(selected.denominator||4)?'selected':''}>${n}</option>`).join('')}</select></div></label><label class="field">作用<select id="structure-meter-extent" class="control" ${off?'disabled':''}><option value="set_meter">仅此小节，下一小节恢复</option><option value="add_meter_label" ${selected.kind==='label'?'selected':''}>到下一拍号标签</option></select></label>${button('写入拍号草稿','structureSetMeter',{disabled:off})}`:''}`:'点击卷帘拍位旗或拍号标签选择对象。'}</div>
    <details><summary>如何操作</summary><p class="form-note">拖动旗帜只校准时间。双击拍位行空白补一拍，选中后 Delete 删一拍；拍号只分组。可保留不闭合草稿连续修改，所有冲突解决后一次应用。源音符和 CC64 秒时间不改变。</p></details>`;
  }
  function paint(){
    window.AstraRhythmContextMenu?.reconcile();
    refreshHistory();
    if(dialog!=='editor-structure'||!$('dialog')?.open)return;
    const target=$('structure-body');if(target)target.innerHTML=body();
    const apply=document.querySelector('[data-do=structureApply]');if(apply)apply.disabled=busy||stale||!session?.can_apply;
    const discard=document.querySelector('[data-do=structureDiscard]');if(discard)discard.disabled=busy;
    window.AstraProjectEditor?.dockRangePanel?.();requestRollDraw();
  }
  function shell(){modal('拍位结构草稿','<div id="structure-body"></div>',button('丢弃草稿','structureDiscard')+button('应用整份草稿','structureApply',{style:'primary',disabled:true}),'editor-structure');const close=$('dialog')?.querySelector?.('.dialog-head [data-do=closeDialog]');if(close)close.title='关闭（有待应用修改时，请先应用或丢弃）';paint();}
  function call(action,extra={},after){
    if(busy)return;if(typeof bridge?.structureDraft!=='function'){message='当前运行版本没有结构草稿服务。';paint();return;}
    const version=++serial,key=state.project?.path,request={action,base_revision:doc()?.revision,...(session?{token:session.token,draft_version:session.draft_version,base_revision:session.base_revision}:{}),...extra};busy=true;paint();
    bridge.structureDraft(JSON.stringify(request),raw=>{
      if(version!==serial||key!==state.project?.path)return;
      busy=false;let result;try{result=typeof raw==='string'?JSON.parse(raw):raw;}catch{result={ok:false,error:'结构草稿返回格式无效。'};}
      if(closeRequested){const cap=result.token?result:session;if(cap&&typeof bridge?.structureDraft==='function')bridge.structureDraft(JSON.stringify({action:'discard',token:cap.token,draft_version:cap.draft_version,base_revision:cap.base_revision}),()=>{});clear();return;}
      if(!result.ok){message=result.error||'操作未写入；原有草稿仍保留。';paint();return;}
      if(result.applied||result.discarded){clear();if($('dialog')?.open&&dialog==='editor-structure')$('dialog').close();if(result.applied&&typeof bridge.snapshot==='function')bridge.snapshot(acceptSnapshot);return;}
      session=result;project=key;stale=false;message=result.draft.summary||(result.can_apply?'草稿已通过检查，可一次应用。':result.draft.conflicts?.length?'修改已保留，请解决下方冲突。':'选中拍位开始修改。');rebuild();paint();after?.();
    });
  }
  function open(){if(session){shell();return;}if(!doc()||state.job.running)return;project=state.project.path;stale=false;selected=null;shell();call('begin',{scope:window.AstraProjectEditor?.localScope?.()||null,preserve_unedited_positions:true});}
  function choose(seconds,kind='pulse'){if(!enabled())return false;const d=session.draft,flags=rows.find(r=>Math.abs(r.seconds-seconds)<1e-7),m=[...d.labels].reverse().find(r=>r.seconds<=seconds+1e-7);selected={seconds,kind:kind==='pulse'&&flags?.bar?'bar':kind,numerator:m?.numerator,denominator:m?.denominator};paint();return true;}
  function edit(operation){if(!enabled()||busy)return;call('edit',{operation});}
  function action(name,key){
    if(name==='structureOpen'){open();return true;}if(!name.startsWith('structure'))return false;
    if(name==='structureDiscard'){call('discard');return true;}if(!enabled())return true;
    if(name==='structureApply'){if(session.can_apply)call('apply');return true;}
    if(name==='structureRegroup'){const r=session.draft.scope;if(r&&!session.draft.preserve_unedited_positions)edit({type:'regroup_bars',start:r.start,end:r.end});return true;}
    if(name==='structureResolve'){const op=session.draft.conflicts[Number(key)]?.proposed_operation;if(op)edit(op);return true;}
    if(name==='structureLocate'){const at=session.draft.conflicts[Number(key)]?.seconds;if(Number.isFinite(at)){const scroll=$('roll-scroll');if(scroll)scroll.scrollLeft=Math.max(0,midiX(at)-scroll.clientWidth/3);dispatch('midiSeek',{position:at});requestRollDraw();}return true;}
    if(!selected)return true;
    const type={structureAddPulse:'add_pulse',structureDeletePulse:'delete_pulse',structureAddBar:'add_bar',structureDeleteBar:'delete_bar',structureDeleteMeter:'delete_meter_label'}[name];
    if(type)edit({type,seconds:selected.seconds});
    if(name==='structureSetMeter')edit({type:$('structure-meter-extent').value,seconds:selected.seconds,numerator:Number($('structure-numerator').value),denominator:Number($('structure-denominator').value)});
    return true;
  }
  function flags(width,offset){if(!enabled())return null;const key=[session.draft_version,width,offset,typeof midiWidth==='number'?midiWidth:0,typeof rulerMode==='string'?rulerMode:''];if(flagCache&&JSON.stringify(flagCache.key)===JSON.stringify(key))return flagCache.rows;let lo=0,hi=rows.length;const start=midiSeconds(offset-22),end=midiSeconds(offset+width+22);while(lo<hi){const mid=(lo+hi)>>>1;if(rows[mid].seconds<start)lo=mid+1;else hi=mid;}const visible=[];for(let i=lo;i<rows.length&&rows[i].seconds<=end;i++){const f=rows[i],x=midiX(f.seconds)-offset;if(visible.length&&x-visible.at(-1).x<22&&f.number!==1)continue;visible.push({...f,x});}flagCache={key,rows:visible};return visible;}
  function draw(ctx,ruler,offset,height,width){if(!enabled())return;const d=session.draft,color=d.valid?'#A4D7CC':'#d5ac65';ctx.save();ctx.strokeStyle=color;ctx.setLineDash([3,5]);for(const row of d.bars){const x=midiX(row.seconds)-offset;if(x<0||x>width)continue;ctx.beginPath();ctx.moveTo(x,0);ctx.lineTo(x,height);ctx.stroke();}ctx.restore();ruler.save();ruler.fillStyle='#282A2D';ruler.fillRect(0,16,width,18);for(const m of d.labels){const x=midiX(m.seconds)-offset;if(x< -35||x>width)continue;ruler.fillStyle=color;ruler.fillRect(x+3,17,34,15);ruler.fillStyle='#14222B';ruler.fillText(`${m.numerator}/${m.denominator}`,x+5,29);}if(typeof rulerMode==='undefined'||rulerMode==='bars'){ruler.fillStyle='#282A2D';ruler.fillRect(0,34,width,28);for(const [i,b] of (d.display?.bars||d.bars).entries()){const x=midiX(b.seconds)-offset;if(x< -25||x>width)continue;ruler.fillStyle=color;ruler.fillRect(x,35,1,26);ruler.fillText((b.bar_number??i+1)<=0?'引子':String(b.bar_number??i+1),x+4,50);}}ruler.restore();}
  function labelAt(x,offset){if(!enabled())return null;return session.draft.labels.find(m=>{const left=midiX(m.seconds)-offset;return x>=left+3&&x<=left+37;});}
  function nearestFlag(x,offset){
    let nearest=null,distance=Infinity;
    for(const flag of flags($('roll-scroll')?.clientWidth||800,offset)||[]){
      if(x<flag.x-5||x>flag.x+18)continue;
      const gap=Math.abs(x-flag.x);
      if(gap<distance||(gap===distance&&selected?.seconds===flag.seconds)){
        nearest=flag;distance=gap;
      }
    }
    return nearest;
  }
  function contextTarget(event){
    if(!enabled()||busy||dialog!=='editor-structure'||!$('dialog')?.open||typeof bridge?.structureDraft!=='function'||window.AstraProjectEditor?.isDragging?.()||window.AstraRhythmTimeline?.isDragging?.())return null;
    const id=event.target.id,rect=event.target.getBoundingClientRect(),offset=$('roll-scroll')?.scrollLeft||0,x=event.clientX-rect.left,y=event.clientY-rect.top;
    if(id==='ruler-canvas'){
      const m=y>=16&&y<=33?labelAt(x,offset):null;
      return m?{kind:'meter',index:session.draft.labels.indexOf(m),seconds:m.seconds}:null;
    }
    const f=id==='beat-flags-canvas'&&y>=4&&y<=23?nearestFlag(x,offset):null;
    return f?{kind:'beat',seconds:f.seconds,bar:f.bar,canDelete:!f.locked}:null;
  }
  function runContext(target,item){
    if(!enabled()||busy||dialog!=='editor-structure'||!$('dialog')?.open)return;
    if(item.form){
      if(choose(target.seconds,target.kind==='meter'?'label':'pulse'))$('structure-numerator')?.focus({preventScroll:true});
    }else edit(item.operation);
  }
  function blankPosition(event){if(!enabled()||busy||event.button!==0||event.target.id!=='beat-flags-canvas')return null;const rect=event.target.getBoundingClientRect(),offset=$('roll-scroll')?.scrollLeft||0,x=event.clientX-rect.left,y=event.clientY-rect.top;if(x<0||x>rect.width||y<0||y>rect.height||flags($('roll-scroll')?.clientWidth||800,offset).some(f=>x>=f.x-5&&x<=f.x+18))return null;return {seconds:midiSeconds(x+offset),x:event.clientX,y:event.clientY,revision:session.draft_version,token:session.token,offset,target:event.target,pointer:event.pointerId};}
  const sameBlank=(a,b)=>a&&b&&a.token===b.token&&a.revision===b.revision&&a.target===b.target&&a.offset===b.offset&&Math.hypot(a.x-b.x,a.y-b.y)<5;
  function addAtBlank(event,p){blankDouble={time:performance.now(),x:p.x,y:p.y,target:p.target};blankClick=null;choose(p.seconds,'empty');edit({type:'add_pulse',seconds:p.seconds});event.preventDefault();event.stopImmediatePropagation();}
  function dbl(event){if(blankDouble&&event.target===blankDouble.target&&performance.now()-blankDouble.time<600&&Math.hypot(event.clientX-blankDouble.x,event.clientY-blankDouble.y)<5)return;const p=blankPosition(event);if(p)addAtBlank(event,p);}
  // WebEngine may omit compatibility dblclick after a canvas pointer handler.
  // Use two completed, stationary clicks; never turn an aborted drag into a beat.
  document.addEventListener('pointerdown',event=>{blankDown=event.button===0?blankPosition(event):null;if(!blankDown)blankClick=null;},true);
  document.addEventListener('pointermove',event=>{if(blankDown&&(event.target!==blankDown.target||event.pointerId!==blankDown.pointer||Math.hypot(event.clientX-blankDown.x,event.clientY-blankDown.y)>5))blankDown=blankClick=null;},true);
  document.addEventListener('pointerup',event=>{const down=blankDown;blankDown=null;const p=blankPosition(event),now=performance.now();if(!sameBlank(p,down)||p.pointer!==down.pointer){blankClick=null;return;}if(sameBlank(p,blankClick)&&now-blankClick.time<450)addAtBlank(event,p);else blankClick={...p,time:now};},true);
  document.addEventListener('pointercancel',()=>{blankDown=blankClick=null;},true);
  document.addEventListener('scroll',()=>{blankDown=blankClick=null;},true);
  document.addEventListener('keydown',event=>{if(event.key==='Escape')blankDown=blankClick=null;},true);
  window.addEventListener?.('blur',()=>{blankDown=blankClick=null;});
  document.addEventListener('change',event=>{if(event.target.id==='structure-preserve')edit({type:'preserve_positions',enabled:event.target.checked});});
  document.addEventListener('close',event=>{if(event.target.id==='dialog'&&dialog==='editor-structure'){if(busy){closeRequested=true;return;}if(!session)return;const old=session;clear();if(typeof bridge?.structureDraft==='function')bridge.structureDraft(JSON.stringify({action:'discard',token:old.token,draft_version:old.draft_version,base_revision:old.base_revision}),()=>{});}},true);
  document.addEventListener('dblclick',dbl,true);
  document.addEventListener('keydown',event=>{if(!session||event.isComposing||event.target.closest('input,textarea,select,[contenteditable=true]'))return;const command=window.AstraEditorShortcuts?.resolve(event);if(command==='editorUndo'||command==='editorRedo'){history(command==='editorUndo'?'undo':'redo');event.preventDefault();event.stopImmediatePropagation();return;}if(!enabled()||!selected||!['Delete','Backspace'].includes(event.key))return;action({pulse:'structureDeletePulse',bar:'structureDeleteBar',label:'structureDeleteMeter'}[selected.kind]||'structureNone');event.preventDefault();event.stopImmediatePropagation();},true);
  function guardNavigation(){if(session?.operation_count||busy){message='请先应用或明确丢弃当前结构草稿，再离开或编辑音符 / CC。试听、播放定位和音频切换仍可使用。';paint();return true;}return false;}
  const guardedActions=new Set(['new','recent','openRecent','chooseProject','chooseAudio','chooseMidi','advanced','harmony','viewReview','tempoAdvanced','prepareTranscriptionCandidate','openRhythmCandidates','editorScoreEnd','editorClearScoreEnd','editorRhythmCandidates','editProject','exportEditor']);
  function guardAction(name){return guardedActions.has(name)&&guardNavigation();}
  document.addEventListener('cancel',event=>{if(event.target.id==='dialog'&&dialog==='editor-structure'&&guardNavigation())event.preventDefault();},true);
  return {open,action,sync,enabled,history,canHistory,hasSession:()=>Boolean(session),guardNavigation,guardAction,flags,draw,labelAt,choose,edit,contextTarget,runContext,contextVersion:()=>[session?.token,session?.draft_version,stale,busy],clearSelection:()=>{selected=null;},busy:()=>busy,selected:()=>selected};
})();

