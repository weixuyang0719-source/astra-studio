"use strict";
// One compact transport surface for editable and read-only playback.
window.AstraTransportPanel=(()=>{
  let mounted=false,sessionAppliedKey="",positionMode="seconds",pendingSpeed=null;
  const sessions=new Map();
  // Deferred modules run while readyState is interactive, before app.js has
  // installed its state. The fold chip may query us during that interval.
  const runtimeReady=()=>{try{return Boolean(state)&&typeof tab==='string';}catch(error){if(error instanceof ReferenceError)return false;throw error;}};
  const readonlyCapable=()=>runtimeReady()&&tab==='notes'&&Boolean(state.midi?.path)&&!editor()?.active();
  const sessionKey=()=>state.project?.path ? 'project:'+state.project.path : 'midi:'+state.midi?.path;
  function range(){
    if(!runtimeReady())return {start:0,end:0,loop:false};
    if(editor()?.active()){const value=editor().range();sessions.set(sessionKey(),{...value});return value;}
    if(!readonlyCapable())return editor()?.editor()?.range||{start:0,end:0,loop:false};
    const key=sessionKey();
    if(!sessions.has(key)){
      const saved=state.project?.path&&editor()?.editor()?.range;
      if(saved)sessions.set(key,{...saved});
      else if(!state.midi.loading&&state.midi.duration>0)sessions.set(key,{start:0,end:state.midi.duration,loop:false});
    }
    return sessions.get(key)||{start:0,end:0,loop:false};
  }
  function setRange(next){if(!readonlyCapable())return;const length=state.midi.duration||0;if(!Number.isFinite(next.start)||!Number.isFinite(next.end)||Math.min(next.start,next.end)<0||Math.max(next.start,next.end)>length+1e-6)throw new Error('定位范围必须位于当前 MIDI 内，且 R 晚于 L。');const value={start:next.start,end:next.end,loop:Boolean(next.loop)};sessions.set(sessionKey(),value);render();dispatch('midiSetRange',{path:state.midi.path,...value});}
  function adoptRange(value){if(state.midi?.path)sessions.set(sessionKey(),{start:value.start,end:value.end,loop:Boolean(value.loop)});}
  const editor=()=>window.AstraProjectEditor;
  const hasMap=()=>Boolean(editor()?.editor()?.tempo?.anchors?.length>1&&editor()?.editor()?.tempo?.time_signatures?.length);
  const music=seconds=>hasMap()?(window.AstraRhythmTimeline?.musicPosition(seconds)||''):Number(seconds).toFixed(3);
  const paths={left:'M6 4v16M19 5l-10 7 10 7z',right:'M18 4v16M5 5l10 7-10 7z',loop:'M4 8h12l-3-3m3 3-3 3M20 16H8l3 3m-3-3 3-3',metronome:'M9 3h6l5 18H4zM12 16l5-10M8 17h8',speed:'M4 17a9 9 0 1 1 16 0M12 12l5-5M6 17h12',together:'M4 6h6v12H4zM14 6h6v12h-6zM10 12h4',settings:'M4 6h16M4 12h16M4 18h16M8 3v6M16 9v6M10 15v6'};
  function icon(name){return `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="${paths[name]}"/></svg>`;}
  function control(action,name,label){return `<button class="transport-icon" data-do="${action}" aria-label="${label}" title="${label}">${icon(name)}</button>`;}
  function field(id,label){return `<label class="transport-location"><span>${label}</span><input id="${id}" class="control" type="text" aria-label="${label}位置" aria-describedby="${id}-error">${id==='editor-play-position'?`<button type="button" id="${id}-seconds" class="transport-position-mode" title="切换秒 / 小节拍位显示">秒 ▾</button>`:`<small id="${id}-seconds"></small>`}<span id="${id}-error" class="editor-locator-error" role="alert" hidden></span></label>`;}
  const speedPresets=[50,75,90,100,125,150];
  function speedPercent(){
    const value=runtimeReady()?(state.player?.audition_rate??editor()?.transportRate()??1):1;
    return Math.round(100*value);
  }
  function shownSpeed(){
    if(pendingSpeed&&(!runtimeReady()||pendingSpeed.project!==sessionKey()||performance.now()>pendingSpeed.deadline))pendingSpeed=null;
    return pendingSpeed?.percent??speedPercent();
  }
  function paintSpeed(percent=shownSpeed(),force=false){
    const input=$('editor-audition-rate');
    if(force&&input){input.value=String(percent);input.removeAttribute('aria-invalid');$('editor-audition-rate-error').hidden=true;}
    else set('editor-audition-rate',percent);
    const summary=$('transport-speed-summary');if(!summary)return;
    summary.title='试听速度 · '+percent+'%';summary.setAttribute('aria-label',summary.title);
    summary.classList.toggle('is-adjusted',percent!==100);
    for(const preset of $('transport-speed-popup').querySelectorAll('[data-speed-percent]'))
      preset.setAttribute('aria-pressed',String(Number(preset.dataset.speedPercent)===percent));
  }
  function commitSpeed(value){
    const percent=Number(value);
    if(!String(value).trim()||!Number.isInteger(percent)||percent<25||percent>200){
      error('editor-audition-rate','请输入 25 至 200 的整数。');return false;
    }
    if(!runtimeReady())return false;
    if(percent===shownSpeed()){paintSpeed(percent,true);return true;}
    const request={percent,project:sessionKey(),deadline:performance.now()+1500};
    pendingSpeed=request;
    paintSpeed(percent,true);
    dispatch('auditionRate',{value:percent/100},()=>{
      if(pendingSpeed===request){pendingSpeed=null;paintSpeed(speedPercent(),true);}
    });
    return true;
  }
  function cancelSpeedEdit(){paintSpeed(shownSpeed(),true);}
  function render(){
    const container=$('musical-transport');if(!container)return;window.AstraTransportFoldStatus?.();
    const simple=tab==='tracks';container.hidden=simple;$('transport')?.classList.toggle('stem-transport',simple);if(simple){const volume=$('volume-popup');if(volume&&volume.parentNode!==container.parentNode)container.before(volume);$('transport')?.classList.remove('compact-transport');return;}
    container.hidden=false;$('transport')?.classList.add('compact-transport');$('transport')?.classList.remove('has-musical-transport');
    if(!mounted){container.innerHTML=`${field('editor-play-position','位置')}
      <div class="transport-locators" role="group" aria-label="左右定位点"><div>${control('editorJumpLeft','left','到左定位点 · 小键盘 1')}${field('editor-music-start','L')}</div><div>${control('editorJumpRight','right','到右定位点 · 小键盘 2')}${field('editor-music-end','R')}</div><details class="transport-options"><summary aria-label="按秒输入定位点" title="按秒输入定位点">⋮</summary><div class="transport-options-body"><label><span>L（秒）</span><input id="editor-range-start" class="control" type="number" min="0" step=".001"></label><label><span>R（秒）</span><input id="editor-range-end" class="control" type="number" min="0" step=".001"></label></div></details></div>
      <div class="transport-cycle-actions" role="group" aria-label="循环、节拍器与录拍">${control('editorLoop','loop','循环 · 小键盘 /')}${control('editorMetronome','metronome','节拍器 · C')}${window.AstraBeatRecorder?.transportMarkup?.()||''}</div>
      <div class="transport-musical-actions" role="group" aria-label="试听设置"><details id="transport-speed-popup" class="transport-speed-popup"><summary id="transport-speed-summary" aria-label="试听速度" title="试听速度 · 100%">${icon('speed')}</summary><div class="transport-speed-panel"><label class="transport-rate"><span>试听速度</span><input id="editor-audition-rate" class="control" type="number" inputmode="numeric" min="25" max="200" step="1" aria-label="试听速度（百分比）" aria-describedby="editor-audition-rate-error transport-speed-hint"><span aria-hidden="true">%</span></label><span id="editor-audition-rate-error" class="editor-locator-error" role="alert" hidden></span><div class="transport-speed-presets" role="group" aria-label="速度快捷档">${speedPresets.map(percent=>'<button type="button" data-speed-percent="'+percent+'">'+percent+'%</button>').join('')}</div><small id="transport-speed-hint">只改试听速度，不改工程 BPM</small></div></details></div>`;mounted=true;}
    const volume=$('volume-popup'),actions=container.querySelector('.transport-musical-actions');
    if(volume&&actions&&volume.parentNode!==actions)actions.append(volume);
    const doc=editor()?.editor(),mapped=hasMap(),canRange=Boolean(doc)||readonlyCapable(),locators=range();
    const note=readonlyCapable()?'只读 MIDI：L/R 与循环仅用于本次试听，不修改工程或源文件。'+(mapped?'使用已有工程拍位图。':'缺少拍位映射时按秒定位。'):mapped?'使用工程拍位图；只读模式也可定位与循环，不修改音符。':doc?'没有完整拍位图，位置按秒输入。':'没有可保存的定位范围；播放与秒定位仍可使用。';
    container.title=note;
    for(const id of ['editor-music-start','editor-music-end','editor-range-start','editor-range-end']){const input=$(id);input.disabled=!canRange;input.title=canRange?(mapped?'小节｜拍｜拍内细分，例如 2｜3｜1/2':'秒位置'):'当前文件没有定位范围数据';}
    for(const action of ['editorLoop','editorMetronome']){for(const button of container.querySelectorAll(`[data-do=${action}]`))button.disabled=!canRange||(action==='editorMetronome'&&!mapped);}
    if(canRange&&locators){set('editor-music-start',music(locators.start));set('editor-music-end',music(locators.end));set('editor-range-start',locators.start.toFixed(3));set('editor-range-end',locators.end.toFixed(3));
      for(const [id,seconds]of [['editor-music-start',locators.start],['editor-music-end',locators.end]])$(id+'-seconds').textContent=seconds.toFixed(3)+' 秒';
      for(const [name,on]of [['editorLoop',locators.loop],['editorMetronome',doc?.metronome]])for(const button of container.querySelectorAll(`[data-do=${name}]`))button.setAttribute('aria-pressed',String(Boolean(on)));
    }else for(const id of ['editor-music-start','editor-music-end','editor-range-start','editor-range-end'])set(id,'—');
    $('editor-play-position').title=mapped?'小节｜拍｜拍内细分；回车后定位':'秒位置；回车后定位';
    paintSpeed();
    syncSeekLock();
    window.AstraBeatRecorder?.syncUI?.();
    if(readonlyCapable()&&!state.midi.loading&&Number.isFinite(locators?.start)&&Number.isFinite(locators?.end)&&Math.min(locators.start,locators.end)>=0&&Math.max(locators.start,locators.end)<=(state.midi.duration||0)+1e-6){const key=JSON.stringify([sessionKey(),state.midi.path,locators]);if(sessionAppliedKey!==key){sessionAppliedKey=key;dispatch('midiSetRange',{path:state.midi.path,...locators});}}else sessionAppliedKey='';
    position(typeof transportMidi!=='undefined'&&transportMidi&&typeof midiDisplayPosition==='function'?midiDisplayPosition():(state.player?.position||state.midi?.position||0));
  }
  function set(id,value){const input=$(id);if(input&&document.activeElement!==input){if(input.value!==String(value))input.value=value;if(input.hasAttribute('aria-invalid'))input.removeAttribute('aria-invalid');const err=$(id+'-error');if(err&&!err.hidden)err.hidden=true;}}
  function position(seconds){if(!mounted)return;set('editor-play-position',positionMode==='music'&&hasMap()?music(seconds):Number(seconds).toFixed(3));const label=$('editor-play-position-seconds'),title=$('editor-play-position');const labelText=positionMode==='music'?'小节 · 拍 ▾':'秒 ▾';if(label.textContent!==labelText)label.textContent=labelText;const titleText=positionMode==='music'?'小节｜拍｜拍内细分，回车定位':'播放位置（秒），回车定位';if(title.title!==titleText)title.title=titleText;}
  function error(id,text){const el=$(id+'-error');if(el){el.textContent=text;el.hidden=false;}$(id)?.setAttribute('aria-invalid','true');}
  function syncSeekLock(){
    if(!mounted)return;
    const locked=typeof blocksRecordingSeek==='function'&&blocksRecordingSeek();
    const playPosition=$('editor-play-position');
    if(playPosition&&playPosition.disabled!==locked)playPosition.disabled=locked;
    const canRange=Boolean(editor()?.editor())||readonlyCapable();
    for(const el of document.querySelectorAll('#transport [data-do=editorJumpLeft],#transport [data-do=editorJumpRight]')){
      const disabled=locked||!canRange;
      if(el.disabled!==disabled)el.disabled=disabled;
    }
  }
  function togglePositionMode(){positionMode=positionMode==='seconds'&&hasMap()?'music':'seconds';render();}
  return {render,position,error,hasMap,range,syncSeekLock,setRange,adoptRange,readonlyCapable,togglePositionMode,commitSpeed,cancelSpeedEdit,speedPercent:shownSpeed,positionMode:()=>positionMode};
})();

// Close the speed popover without touching transport state.
if(typeof document.addEventListener==='function'){
 document.addEventListener('click',event=>{
   if(event.target.closest('#editor-play-position-seconds'))window.AstraTransportPanel.togglePositionMode();
   const preset=event.target.closest('[data-speed-percent]');
   if(preset)window.AstraTransportPanel.commitSpeed(preset.dataset.speedPercent);
 });
 document.addEventListener('pointerdown',event=>{const popup=$('transport-speed-popup');if(popup?.open&&!event.target.closest('.transport-speed-popup'))popup.open=false;});
 document.addEventListener('keydown',event=>{
   const popup=$('transport-speed-popup');
   if(!popup?.open)return;
   if(event.key==='Escape'){
     window.AstraTransportPanel.cancelSpeedEdit();popup.open=false;$('transport-speed-summary')?.focus();
     event.preventDefault();
   }else if(event.key==='Enter'&&event.target.id==='editor-audition-rate'){
     event.preventDefault();
     if(window.AstraTransportPanel.commitSpeed(event.target.value))$('transport-speed-summary')?.focus();
   }
 });
 // Fold the transport to one slim row. Only a data attribute changes: the
 // footer stays un-hidden, so Space / numpad transport shortcuts keep working
 // and play, stop and the position field stay reachable.
 (()=>{
  let folded=false;try{folded=window.localStorage?.getItem('astra-transport-folded')==='1';}catch{}
  const byId=id=>document.getElementById?.(id);
  // While folded, a status chip keeps what you are hearing visible: source,
  // cycle on, and a non-100% audition speed. Clicking it unfolds the transport.
  const status=()=>{
    const t=byId('transport');if(!t)return;
    let chip=byId('transport-folded-status');
    if(!chip){chip=document.createElement('button');chip.type='button';chip.id='transport-folded-status';chip.className='transport-folded-status';t.insertBefore(chip,byId('transport-fold'));}
    const sourceSelect=byId('source'),source=sourceSelect?.selectedOptions?.[0]?.textContent||'';
    const range=window.AstraTransportPanel?.range?.(),percent=window.AstraTransportPanel?.speedPercent?.()??100;
    const recPhase=window.AstraBeatRecorder?.phase?.();
    const parts=[source&&`<span class="fold-source">${source.replace(/[<&>]/g,'')}</span>`,recPhase&&recPhase!=='idle'&&`<span class="fold-flag fold-beat-rec">${recPhase==='recording'?'● 录拍中':recPhase==='review'?'核对拍点':'录拍待命'}</span>`,range?.loop&&'<span class="fold-flag fold-loop">循环</span>',percent!==100&&`<span class="fold-flag fold-rate">${percent}%</span>`].filter(Boolean);
    const html=parts.join('');if(chip.innerHTML!==html)chip.innerHTML=html;
    const label=`试听：${source||'—'}${range?.loop?' · 循环开启':''}${percent!==100?` · 速度 ${percent}%`:''}；点击展开走带`;
    if(chip.title!==label){chip.title=label;chip.setAttribute('aria-label',label);}
  };
  window.AstraTransportFoldStatus=()=>{status();setTimeout(status,0);};
  const apply=()=>{const t=byId('transport'),b=byId('transport-fold');if(!t||!b)return;t.dataset.folded=String(folded);b.setAttribute('aria-expanded',String(!folded));const label=folded?'展开走带 · Ctrl+Alt+T':'收起走带 · 保留播放、停止、位置与试听状态 · Ctrl+Alt+T';b.title=label;b.setAttribute('aria-label',label);status();};
  function toggleFold(){folded=!folded;try{window.localStorage?.setItem('astra-transport-folded',folded?'1':'0');}catch{}apply();window.dispatchEvent?.(new Event('resize'));}
  window.AstraTransportPanel.toggleFold=toggleFold;
  document.addEventListener('click',event=>{const chip=event.target?.closest?.('#transport-folded-status');if(!event.target?.closest?.('#transport-fold')&&!chip)return;if(chip&&!folded)return;toggleFold();});
  if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',apply);else apply();
 })();
}
