/* SPDX-License-Identifier: GPL-3.0-only
 * Score transport presentation port: MuseScore Studio 4.6.5, 653f38d8e.
 * See SCORE-TRANSPORT-PROVENANCE.md. This module has no sound owner.
 */
(() => {
  "use strict";
  const GLYPH = Object.freeze({grip:0xf3a2, rewind:0xef26, play:0xef1d,pause:0xf3c9,
    loop:0xef1f,loopIn:0xf3c7,loopOut:0xf3c8,metronome:0xef20,settings:0xef55});
  const LABEL = Object.freeze({grip:"拖动或双击播放工具栏",rewind:"返回播放起点",
    playPause:"播放",loop:"循环播放",loopIn:"设置左循环标记",loopOut:"设置右循环标记",
    metronome:"节拍器",settings:"播放设置",repeat:"播放反复记号",chordSymbols:"播放和弦记号",
    audition:"编辑时试听",pan:"自动跟随谱面",flatTempo:"忽略谱面速度",countIn:"预备拍",midiInput:"启用 MIDI 输入",
    writtenPitch:"记谱音高",soundingPitch:"实际音高",speed:"速度"});
  const SETTINGS = ["midiInput","pitch","repeat","chordSymbols","audition","pan","countIn","flatTempo"];
  const BUTTONS = ["rewind","playPause","loop","loopIn","loopOut","metronome"];
  const TOGGLES = ["loop","metronome","repeat","chordSymbols","audition","pan","countIn","flatTempo","midiInput","writtenPitch","soundingPitch"];
  const FLAGS = new Set([...BUTTONS,"seek","seekBeat","speed",...TOGGLES]);
  const N = (v, min, max) => typeof v==="number" && Number.isFinite(v) && v>=min && v<=max;
  const I = (v, min, max) => Number.isInteger(v) && N(v,min,max);
  const durationMax = 86399.9;
  const positive = (v, limit) => I(v,1,limit) ? v : null;
  const glyph = (k) => String.fromCodePoint(GLYPH[k]);
  const note = (k) => ({whole:"𝅝",half:"𝅗𝅥",quarter:"♩",eighth:"♪",sixteenth:"𝅘𝅥𝅯"})[k] || "♩";
  const clockParts = (s) => {
    const ds = Math.floor((s+1e-8)*10);
    return [Math.floor(ds/36000),Math.floor(ds/600)%60,Math.floor(ds/10)%60,ds%10];
  };
  function normalise(s = {}) {
    s=s||{};
    const duration=N(s.duration,0,durationMax)?s.duration:0;
    const measure=positive(s.measure,12000),maxMeasure=positive(s.maxMeasure,12000),beat=positive(s.beat,64),maxBeat=positive(s.maxBeat,64);
    const flags=Array.isArray(s.capabilities)?new Set(s.capabilities.filter(k=>FLAGS.has(k))):new Set();
    const boolMap = Object.fromEntries(TOGGLES.map(k=>[k,s[k]===true]));
    const hasRate=N(s.speedMin,.1,3)&&N(s.speedMax,.1,3)&&s.speedMin<s.speedMax;
    const speedMin=hasRate?s.speedMin:.1, speedMax=hasRate?s.speedMax:3;
    if(!hasRate)flags.delete("speed");
    const validLoop=N(s.start,0,duration)&&N(s.end,0,duration)&&s.start<s.end;
    if(!duration) for(const f of ["seek","seekBeat","loop","loopIn","loopOut"])flags.delete(f);
    if(!measure||!maxMeasure||!beat||!maxBeat||measure>maxMeasure||beat>maxBeat)flags.delete("seekBeat");
    const epoch=positive(s.epoch,2147483647);
    const revision=positive(s.revision,2147483647);
    if(!epoch||!revision)flags.clear();
    return {epoch,revision,duration,position:N(s.position,0,duration)?s.position:0,
      state:["playing","pending","counting"].includes(s.state)?s.state:"paused",
      ...boolMap,start:validLoop?s.start:0,end:validLoop?s.end:duration,
      measure,maxMeasure,beat,maxBeat,
      tempoBpm:N(s.tempoBpm,1,1000)?s.tempoBpm:null,tempoUnit:typeof s.tempoUnit==="string"&&["whole","half","quarter","eighth","sixteenth"].includes(s.tempoUnit)?s.tempoUnit:"quarter",
      tempoDotted:s.tempoDotted===true,speed:N(s.speed,speedMin,speedMax)?s.speed:1,
      speedMin,speedMax,requestedSpeed:N(s.requestedSpeed,speedMin,speedMax)?s.requestedSpeed:null,
      requestedMetronome:typeof s.requestedMetronome==="boolean"?s.requestedMetronome:null,
      capabilities:[...flags]};
  }
  const can=(s,k)=>s.capabilities.includes(k);
  function command(s, req) {
    if(!req||req.epoch!==s.epoch||req.revision!==s.revision||!can(s,req.action))return null;
    const a=req.action, v=req.value;
    if(a==="seek")return N(v,0,s.duration)?{action:a,value:{position:s.loop?Math.max(s.start,v):v}}:null;
    if(a==="seekBeat")return v&&I(v.measure,1,s.maxMeasure)&&I(v.beat,1,64)&&
      (v.measure!==s.measure||v.beat<=s.maxBeat)?{action:a,value:{measure:v.measure,beat:v.beat}}:null;
    if(a==="speed")return I(v,Math.ceil(s.speedMin*100),Math.floor(s.speedMax*100))?
      {action:a,value:{multiplier:v/100}}:null;
    if(a==="metronome")return {action:a,value:{enabled:!(s.requestedMetronome??s.metronome)}};
    if(a==="countIn")return {action:a,value:{enabled:!s.countIn}};
    if(a==="flatTempo")return {action:a,value:{enabled:!s.flatTempo}};
    return {action:a,value:{}};
  }
  function dockedDrag(origin, point, safeRect) {
    return Math.hypot(point.x-origin.x,point.y-origin.y)>7 &&
      (point.x<safeRect.left-4||point.x>safeRect.right+4||point.y<safeRect.top-5||point.y>safeRect.bottom+5);
  }
  function template(floating) {
    const button=k=>'<button type="button" class="ast-sp-button" data-sp-action="'+k+
      '" aria-label="'+LABEL[k]+'" title="'+LABEL[k]+'"'+(TOGGLES.includes(k)?' aria-pressed="false"':'')+
      (k==="playPause"?' aria-keyshortcuts="Space"':'')+'><span aria-hidden="true" class="ast-sp-icon">'+
      glyph(k==="playPause"?"play":k)+'</span></button>';
    const settingsItem=k=>'<button type="button" class="ast-sp-settings-item" data-sp-action="'+k+
      '" aria-pressed="false">'+LABEL[k]+'</button>';
    const hr='<hr>';
    const settings = '<details class="ast-sp-pop"><summary class="ast-sp-button" data-sp-name="settings" title="'+
      LABEL.settings+'" aria-label="'+LABEL.settings+'"><span aria-hidden="true" class="ast-sp-icon">'+glyph("settings")+
      '</span></summary><div class="ast-sp-popup" role="menu" aria-label="播放设置">'+settingsItem("midiInput")+
      '<details class="ast-sp-pitch"><summary>MIDI 输入音高</summary>'+settingsItem("writtenPitch")+settingsItem("soundingPitch")+
      '</details>'+hr+SETTINGS.slice(2).map(settingsItem).join("")+(floating?"":hr+["loopIn","loopOut"].map(k=>
        '<button type="button" class="ast-sp-settings-item" data-sp-action="'+k+'">'+LABEL[k]+'</button>').join(""))+'</div></details>';
    const speed='<div class="ast-sp-speed-row">'+
      '<span>速度</span><label class="ast-sp-speed-number"><input type="number" min="10" max="300" step="5" '+
      'aria-label="速度百分比" data-sp-input="speedNumber" value="100">%</label>'+
      '<input type="range" min="10" max="300" step="5" aria-label="播放速度" data-sp-input="speedSlider" value="100"></div>';
    const timeField=(idx, label)=>'<input class="ast-sp-time" type="text" inputmode="numeric" autocomplete="off" '+
      'spellcheck="false" aria-label="'+label+'" data-sp-time="'+idx+'" maxlength="'+(idx===3?1:2)+'" disabled>';
    const measureField=(k,label,max)=>'<input type="text" inputmode="numeric" autocomplete="off" spellcheck="false" '+
      'aria-label="'+label+'" data-sp-input="'+k+'" maxlength="'+max+'" disabled>';
    const tempoLabel='<span data-sp-tempo>♩ = —</span>';
    return '<div class="ast-sp-main" role="toolbar" aria-label="播放控制">'+button("grip")+
      '<div class="ast-sp-commands">'+BUTTONS.filter(k=>floating||!["loopIn","loopOut"].includes(k)).map(button).join("")+
      settings+'</div><div class="ast-sp-clock" role="group" aria-label="播放时间">'+
      ["时","分","秒","十分之一秒"].map((k,i)=>timeField(i,k)).join("<span aria-hidden=\"true\">:</span>")+
      '</div><div class="ast-sp-measure">'+measureField("measure","小节",5)+
      '<span aria-hidden="true">.</span>'+measureField("beat","拍",3)+'</div>'+
      (floating?'<span class="ast-sp-tempo">'+tempoLabel+'</span>':
        '<details class="ast-sp-pop"><summary class="ast-sp-button ast-sp-tempo" data-sp-name="speed" aria-label="速度" title="速度">'+
        tempoLabel+'</summary><div class="ast-sp-popup ast-sp-speed-pop" role="dialog" aria-modal="false" aria-label="速度"><p>速度</p>'+speed+'</div></details>')+
      '</div>'+(floating?'<div class="ast-sp-lower"><input type="range" min="0" max="0" step="0.001" '+
      'aria-label="播放位置" data-sp-input="progress" disabled>'+speed+'</div>':'');
  }
  function createView(host,{floating=false,send,onGripDouble,onGripStart,onGripMove,onGripEnd,gripEnabled=()=>true}) {
    host.classList.add("ast-sp-toolbar");
    host.classList.toggle("ast-sp-floating",floating);
    host.innerHTML=template(floating);
    let live=normalise(), drag=null, queued=null, frame=0, lastRange=null, glide=0, paintedAt=0;
    const engagedSliders=new Set(), beatFields=new WeakMap();
    const group=host.querySelector(".ast-sp-main");
    const actions=Object.fromEntries([...host.querySelectorAll("[data-sp-action]")].map(e=>[e.dataset.spAction,e]));
    const inputs=Object.fromEntries([...host.querySelectorAll("[data-sp-input]")].map(e=>[e.dataset.spInput,e]));
    const clocks=[...host.querySelectorAll("[data-sp-time]")],tempo=host.querySelector("[data-sp-tempo]");
    const settings=host.querySelector('[data-sp-name="settings"]').parentElement;
    const tempoDetails=host.querySelector('[data-sp-name="speed"]')?.parentElement;
    const grip=actions.grip;
    const focused=()=>host.ownerDocument.activeElement;
    const active=()=>host.isConnected;
    const currentReq=(action,value)=>({epoch:live.epoch,revision:live.revision,action,value});
    function fire(req) {if(active()&&command(live,req))send(req);}
    function dispatch(action,value){fire(currentReq(action,value));}
    function commitRange() {
      frame=0;
      const req=queued;queued=null;
      if(!req)return;
      const key=JSON.stringify(req);
      if(key===lastRange)return;
      if(command(live,req)){lastRange=key;fire(req);}
    }
    function queueRange(action,value,final) {
      queued=currentReq(action,value);
      if(final){
        if(frame)cancelAnimationFrame(frame);
        commitRange();
      } else if(!frame) frame=requestAnimationFrame(commitRange);
    }
    function setVal(e,value){if(e && e.value!==String(value))e.value=value;}
    function glideProgress(){
      glide=0;
      const bar=inputs.progress;
      if(!bar||!active()||live.state!=="playing")return;
      if(!engagedSliders.has(bar)){
        const ahead=Math.min(.25,(performance.now()-paintedAt)/1000)*live.speed;
        bar.value=Math.min(live.duration,live.position+ahead);
      }
      glide=requestAnimationFrame(glideProgress);
    }
    function paint(model) {
      const previous=live;
      live=normalise(model);
      if(previous.epoch!==live.epoch||previous.revision!==live.revision) {
        queued=lastRange=null;
        if(frame)cancelAnimationFrame(frame);
        frame=0;
      }
      for (const [k,el] of Object.entries(actions)) {
        const disabled=k==="grip"?!gripEnabled():!can(live,k);
        if(el.disabled!==disabled)el.disabled=disabled;
        if(TOGGLES.includes(k)){
          const pressed=String(live[k]);
          if(el.getAttribute("aria-pressed")!==pressed)el.setAttribute("aria-pressed",pressed);
        }
      }
      const playing=live.state==="playing"||live.state==="pending"||live.state==="counting";
      const play=actions.playPause, title=live.state==="counting"?"取消预备拍":live.state==="pending"?"取消待播放":playing?"暂停":"播放";
      if(play.getAttribute("aria-label")!==title)play.setAttribute("aria-label",title);
      if(play.title!==title+" · Space")play.title=title+" · Space";
      const icon=glyph(playing?"pause":"play");
      if(play.firstElementChild.textContent!==icon)play.firstElementChild.textContent=icon;
      for(const e of clocks)e.disabled=!can(live,"seek");
      if(!clocks.includes(focused())||previous.epoch!==live.epoch||previous.revision!==live.revision){
        clockParts(live.position).forEach((v,i)=>setVal(clocks[i],i===1||i===2?String(v).padStart(2,"0"):v));
      }
      const beatReady=can(live,"seekBeat");
      for(const [k, v] of [["measure",live.measure],["beat",live.beat]]) {
        inputs[k].disabled=!beatReady;
        if(inputs[k]!==focused())setVal(inputs[k],v??"—");
      }
      if(inputs.progress){
        inputs.progress.disabled=!can(live,"seek");
        if(inputs.progress.max!==String(live.duration))inputs.progress.max=live.duration;
        if(!engagedSliders.has(inputs.progress))setVal(inputs.progress,live.position);
        // The packet position only arrives ~20 times a second: glide between them.
        paintedAt=performance.now();
        if(playing&&!glide)glide=requestAnimationFrame(glideProgress);
      }
      for(const k of ["speedNumber","speedSlider"]){
        const el=inputs[k];
        el.disabled=!can(live,"speed");
        const low=String(Math.ceil(live.speedMin*100)),high=String(Math.floor(live.speedMax*100));
        if(el.min!==low)el.min=low;if(el.max!==high)el.max=high;
        if(el!==focused()||k==="speedSlider"&&!engagedSliders.has(el))
          setVal(el,Math.round((live.requestedSpeed??live.speed)*100));
      }
      const bpm=note(live.tempoUnit)+(live.tempoDotted?"·":"")+" = "+(live.tempoBpm===null?"—":Math.round(live.tempoBpm));
      if(tempo.textContent!==bpm)tempo.textContent=bpm;
      if(tempoDetails){
        const el=tempoDetails.firstElementChild;
        el.setAttribute("aria-disabled",String(!can(live,"speed")));
        if(!can(live,"speed"))tempoDetails.open=false;
      }
    }
    host.addEventListener("click",event=>{
      const el=event.target.closest("[data-sp-action]");
      if(el&&!el.disabled&&el!==grip){dispatch(el.dataset.spAction);settings.open=false;return;}
      if(tempoDetails&&event.target.closest('[data-sp-name="speed"]')){
        if(!can(live,"speed"))event.preventDefault(); else settings.open=false;
      }
      if(event.target.closest('[data-sp-name="settings"]')&&tempoDetails)tempoDetails.open=false;
    });
    const closeOnOutside=e=>{if(!active())return;for(const d of [settings,tempoDetails])if(d?.open&&!d.contains(e.target))d.open=false;};
    host.ownerDocument.addEventListener("pointerdown",closeOnOutside,true);
    host.addEventListener("focusin",event=>{
      const target=event.target;
      if(target===inputs.measure||target===inputs.beat)beatFields.set(target,{epoch:live.epoch,revision:live.revision});
      if(target.matches?.('input[type="text"]'))target.select();
    });
    host.addEventListener("pointerdown",e=>{
      if(e.target.matches?.('input[type="range"]')){
        engagedSliders.add(e.target);lastRange=null;
      }
    });
    for(const kind of ["pointerup","pointercancel","blur"])host.addEventListener(kind,e=>engagedSliders.delete(e.target),true);
    host.addEventListener("input",e=>{
      if(e.target===inputs.progress)queueRange("seek",Number(e.target.value),false);
      if(e.target===inputs.speedSlider)queueRange("speed",Number(e.target.value),false);
    });
    host.addEventListener("change",e=>{
      const t=e.target;
      if(clocks.includes(t)){
        const values=clocks.map(x=>/^\d{1,2}$/.test(x.value)?Number(x.value):NaN),
          [h,m,s,d]=values;
        if([h,m,s,d].every(Number.isFinite)&&h<=23&&m<=59&&s<=59&&d<=9)
          dispatch("seek",h*3600+m*60+s+d/10);
        else clockParts(live.position).forEach((v,i)=>setVal(clocks[i],i===1||i===2?String(v).padStart(2,"0"):v));
      }
      if(t===inputs.measure||t===inputs.beat){
        const typed=/^\d{1,5}$/.test(t.value)?Number(t.value):null;
        const stamp=beatFields.get(t)??{epoch:live.epoch,revision:live.revision};
        const measure=t===inputs.measure?typed:live.measure, beat=t===inputs.beat?typed:live.beat;
        fire({epoch:stamp.epoch,revision:stamp.revision,action:"seekBeat",value:{measure,beat}});
        // Only the playback authority publishes a new position.
        setVal(t,(t===inputs.measure?live.measure:live.beat)??"—");
      }
      if(t===inputs.progress){queueRange("seek",Number(t.value),true);lastRange=null;}
      if(t===inputs.speedSlider){queueRange("speed",Number(t.value),true);lastRange=null;}
      if(t===inputs.speedNumber)dispatch("speed",Number(t.value));
    });
    host.addEventListener("keydown",event=>{
      if(event.isComposing||event.keyCode===229)return;
      const t=event.target;
      if(event.key==="Enter"&&t.matches?.('input:not([type="range"])')){
        event.preventDefault();t.blur();
      }else if(event.key==="Escape"&&(settings.open||tempoDetails?.open)){
        event.preventDefault();event.stopPropagation();const target=settings.open?settings:tempoDetails;target.open=false;target.firstElementChild.focus();
      }else if(event.key==="Enter"&&t===grip&&!grip.disabled){
        event.preventDefault(); if(!event.repeat)onGripDouble?.(group.getBoundingClientRect());
      }
    });
    grip.addEventListener("dblclick",e=>{if(!grip.disabled&&e.button===0){e.preventDefault();onGripDouble?.(group.getBoundingClientRect());}});
    const xy=e=>({x:e.clientX,y:e.clientY});
    grip.addEventListener("pointerdown",e=>{
      if(grip.disabled||e.button!==0||!e.isPrimary)return;
      const point=xy(e);
      drag={id:e.pointerId,origin:point,latest:point,rect:group.getBoundingClientRect()};
      grip.setPointerCapture(e.pointerId);
      onGripStart?.(drag,point);
    });
    grip.addEventListener("pointermove",e=>{if(drag?.id===e.pointerId){drag.latest=xy(e);onGripMove?.(drag,drag.latest);}});
    const finish=(e,cancel)=>{if(drag?.id!==e.pointerId)return;const prev=drag;drag=null;onGripEnd?.(prev,xy(e),cancel);};
    grip.addEventListener("pointerup",e=>finish(e,false));
    grip.addEventListener("pointercancel",e=>finish(e,true));
    grip.addEventListener("lostpointercapture",e=>finish(e,true));
    paint(null);
    return {paint,group,grip,destroy(){host.ownerDocument.removeEventListener("pointerdown",closeOnOutside,true);if(frame)cancelAnimationFrame(frame);if(glide)cancelAnimationFrame(glide);glide=0;}};
  }
  const api=Object.freeze({normalise,command,clockParts,dockedDrag,can,template,createView,FLAGS:[...FLAGS]});
  if(typeof module!=="undefined"&&module.exports)module.exports=api;
  else globalThis.AstraScorePlaybackChrome=api;
})();
