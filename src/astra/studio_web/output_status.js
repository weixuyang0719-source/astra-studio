"use strict";
// Audio output settings (RAW app only). Two parts that update independently:
//  · 当前生效 (.aos-live)  — what is actually playing now: backend, full device name, read-back
//    parameters, phase, last result. Only the visible projection triggers DOM work.
//  · 更改输出 (.aos-form)  — the pending choice. Never wiped by status updates: the unsubmitted
//    choice, focus and selection survive; only explicit form changes re-render it.
// Every choice (incl. 兼容输出) is applied the same way and saved by the backend.
window.AstraOutputStatus=(()=>{
 let state={phase:'idle',actual:null,label:'尚未开始播放'},bridge=null,lastStatus=null,lastOrder=-Infinity;
 const liveMarkupByNode=new WeakMap();
 let devices=null,choice=null,devicePending=false,result=null; // result: {tone:'ok'|'error'|'info',text}
 let panel=null,panelRequest=null,panelTimer=0,choiceInitialized=false,deviceRequest=null;
 const busyPanel=()=>Boolean(panelRequest||panel?.busy||['releasing','open'].includes(panel?.phase));
 const safe=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
 const PHASE={playing:'播放中',paused:'已暂停',preparing:'准备中',switching:'切换中',idle:'未播放',stopped:'已停止',error:'出错',blocked:'等待旧设备释放',unavailable:'没有输出设备'};
 const MODE={raw:'RAW','native-shared':'原生共享',ordinary:'兼容输出','native-pcm':'原生音频','native-joint':'音频 + MIDI','native-audio-click':'音频 + 节拍器'};
 const ASIO_STATE={'dll-missing':'驱动文件缺失',blacklisted:'旧式包装驱动，不支持','no-clsid':'注册信息不完整'};
 const NEEDS_DEVICE=new Set(['wasapi-shared','wasapi-raw','asio']);

 // ---- model (pure; exercised by tests/output_status_r14.cjs) ------------------------------
 function catalog(){return devices?.catalog||{backends:{},wasapi:[],asio:[],asio_runtime:{available:false}};}
 function current(){return devices?.current||{backend:'auto',device:null};}
 function pending(){return choice||current();}
 function deviceName(backend,id){
  if(backend==='asio')return id||'';
  if(!NEEDS_DEVICE.has(backend))return '';
  if(!id)return '系统默认输出';
  return catalog().wasapi.find(e=>e.id===id)?.name||'（设备已断开）';
 }
 function deviceOptions(backend){
  const c=catalog();
  if(backend==='asio'){
   const ready=Boolean(c.asio_runtime?.available);
   return [{value:'',label:'请选择驱动…',placeholder:true}].concat(c.asio.map(d=>({value:d.name,label:`${d.description||d.name}（${d.kind==='wrapper'?'通用/虚拟':'声卡驱动'}${d.state!=='registered'?'，'+(ASIO_STATE[d.state]||d.state):''}）`,disabled:!ready||d.state!=='registered'})));
  }
  if(!NEEDS_DEVICE.has(backend))return [];
  return [{value:'',label:'系统默认输出'}].concat(c.wasapi.filter(e=>e.id).map(e=>({value:e.id,label:e.name+(e.default?'（系统默认）':'')})));
 }
 function formModel(){
  const cur=current(),c=pending(),options=deviceOptions(c.backend);
  const device=c.backend==='asio'?(c.device||''):(c.device||'');
  const needsChoice=c.backend==='asio'&&!options.some(o=>o.value===device&&!o.disabled&&!o.placeholder);
  const buffer=c.backend==='asio'?(Number(c.buffer_frames)||null):null;
  const retry=c.backend==='asio'&&['failed','rolled-back','device-missing'].includes(devices?.state);
  const changed=retry||c.backend!==cur.backend||(c.device||null)!==(cur.device||null)||buffer!==(cur.buffer_frames||null);
  const panelBusy=busyPanel();
  const canApply=Boolean(devices)&&!devicePending&&!panelBusy&&changed&&!needsChoice;
  return {backend:c.backend,device,buffer,options,needsChoice,changed,canApply,panelBusy,canPanel:c.backend==='asio'&&!needsChoice&&!devicePending&&!panelBusy&&typeof bridge?.openAsioControlPanel==='function',asio:c.backend==='asio',
   applyLabel:'应用',
   payload:{backend:c.backend,device:NEEDS_DEVICE.has(c.backend)?(device||null):null,sample_rate:48000,...(buffer||cur.buffer_frames?{buffer_frames:buffer}:{}),confirm_open:c.backend==='asio'}};
 }
 function liveModel(){
  const d=state.device||{},backend=d.backend||current().backend,b=catalog().backends?.[backend];
  const name=backend==='auto'?(state.actual?`→ ${MODE[state.actual]||state.actual}`:''):backend==='compat'?'':deviceName(backend,d.device);
  let params=d.actual||'';const first=params.split(' · ')[0];if(first&&name&&name.includes(first))params=params.split(' · ').slice(1).join(' · ');
  return {title:[b?.label||'自动',name].filter(Boolean).join(' · '),phase:PHASE[state.phase]||state.phase||'',phaseKey:state.phase,params,
   midi:state.audition==='midi'||state.displayMode==='midi',deviceState:d.state,message:d.message||''};
 }

 // ---- markup ------------------------------------------------------------------------------
 function actualModel(){
  const d=state.device||{};
  const same=!d.backend||(d.backend===devices?.current?.backend&&(d.device||null)===(devices?.current?.device||null));
  const raw=d.actualParameters&&typeof d.actualParameters==='object'?d.actualParameters:d.actual&&typeof d.actual==='object'?d.actual:same&&devices?.state==='active'?devices?.actual:null;
  const n=(...values)=>values.find(v=>typeof v==='number'&&Number.isFinite(v)&&v>=0);
  return {rate:n(raw?.sample_rate,raw?.rate),frames:n(raw?.observed_frames,raw?.observed_block),
   latency:n(raw?.latency_ms,typeof raw?.output_latency==='number'?raw.output_latency*1000:undefined),name:raw?.device||null};
 }
 function liveMarkup(){
  const m=liveModel(),a=actualModel(),known=(v,unit)=>v===undefined?'未知':`${safe(v)} ${unit}`;
  const tone=result?.tone||(m.deviceState==='rolled-back'?'error':m.deviceState==='device-missing'?'info':'');
  const text=devicePending?'正在应用…':result?.text||(tone||m.phaseKey==='error'||m.phaseKey==='blocked'||m.phaseKey==='unavailable'?m.message:'');
  return `<div class="aos-head"><h4 class="aos-subhead">实际输出</h4><span class="aos-phase" data-phase="${safe(m.phaseKey)}">${safe(m.phase||'尚未激活')}</span></div>`
   +`<strong class="aos-device">${safe(a.name||m.title)}</strong>`
   +`<dl class="aos-metrics"><div><dt>实际采样率</dt><dd>${known(a.rate,'Hz')}</dd></div><div><dt>实际回调缓冲</dt><dd>${known(a.frames,'帧')}</dd></div><div><dt>设备输出延迟</dt><dd>${known(a.latency,'ms')}</dd></div></dl>`
   +(m.params?`<small class="aos-params">${safe(m.params)}</small>`:'')
   +`<p class="aos-result" role="status" aria-live="polite"${tone?` data-tone="${tone}"`:''}>${safe(text)}</p>`;
 }
 function formMarkup(){
  if(!devices)return `<p class="aos-hint">${safe(result?.text||'正在读取设备列表…')}</p>`;
  const f=formModel(),c=catalog(),rt=c.asio_runtime;
  const backends=Object.entries(c.backends).map(([k,v])=>`<option value="${k}" ${f.backend===k?'selected':''} ${k==='asio'&&!rt?.available?'disabled':''}>${safe(v.label)}${k==='asio'&&!rt?.available?'（未安装运行库）':''}</option>`).join('');
  const options=f.options.map(o=>`<option value="${safe(o.value)}" ${f.device===o.value?'selected':''} ${o.disabled||o.placeholder?'disabled':''}>${safe(o.label)}</option>`).join('');
  return `<div class="aos-config-head"><h4 class="aos-subhead">更改输出</h4><button type="button" class="control${f.canApply?' primary':''}" data-audio-device-apply ${f.canApply?'':'disabled'}>${safe(f.applyLabel)}</button></div>`
   +`<label class="preferences-row" for="preferences-audio-backend"><span><strong>后端</strong></span><select id="preferences-audio-backend" class="control" ${devicePending?'disabled':''}>${backends}</select></label>`
   +(f.options.length?`<label class="preferences-row aos-device-row" for="preferences-audio-device"><span><strong>${f.asio?'ASIO 驱动':'输出端口'}</strong><small class="aos-fullname">${safe(f.device?f.options.find(o=>o.value===f.device)?.label||f.device:f.asio?'':'系统默认输出')}</small></span><select id="preferences-audio-device" class="control" ${devicePending?'disabled':''}>${options}</select></label>`:'')
   +bufferMarkup(f);
 }
 function markup(){return `<section class="preferences-section audio-output-status"><h3>音频设备</h3><div class="aos-form">${formMarkup()}</div><div class="aos-live">${liveMarkup()}</div></section>`;}

 // ---- in-place updates ----------------------------------------------------------------------
 function refreshLive(){
  const el=document.querySelector('.audio-output-status .aos-live');if(!el)return;
  const next=liveMarkup();if(liveMarkupByNode.get(el)!==next){el.innerHTML=next;liveMarkupByNode.set(el,next);}
 }
 function refreshForm(){
  const el=document.querySelector('.audio-output-status .aos-form');if(!el)return;
  const focused=document.activeElement?.id;el.innerHTML=formMarkup();
  if(focused){const again=document.getElementById(focused);if(again&&again.focus)again.focus({preventScroll:true});}
 }
 function syncFormState(){ // device change only: no re-render, keep the open <select> untouched
  const f=formModel(),button=document.querySelector('.audio-output-status [data-audio-device-apply]');
  if(button){if(button.disabled===f.canApply)button.disabled=!f.canApply;button.classList?.toggle('primary',f.canApply);if(button.textContent!==f.applyLabel)button.textContent=f.applyLabel;}
  const panelButton=document.querySelector('.audio-output-status [data-asio-control-panel]');if(panelButton)panelButton.disabled=!f.canPanel;
  const bufferSelect=document.getElementById('preferences-audio-buffer');if(bufferSelect&&document.activeElement!==bufferSelect)bufferSelect.value=f.buffer||'';
  const full=document.querySelector('.audio-output-status .aos-fullname');if(full)full.textContent=f.device?f.options.find(o=>o.value===f.device)?.label||f.device:f.asio?'':'系统默认输出';
 }
 function loadDevices(){
  if(devices||deviceRequest)return;
  if(typeof bridge?.audioDevices!=='function'){result={tone:'error',text:'设备管理不可用。'};refreshForm();return;}
  const request=deviceRequest={};
  bridge.audioDevices(raw=>{if(deviceRequest!==request)return;deviceRequest=null;try{const r=JSON.parse(raw);if(r.ok){devices=r;panel=r.control_panel||null;seedChoice();if(busyPanel())schedulePanelPoll();}else result={tone:'error',text:r.error||'设备列表无法读取。'};}catch{result={tone:'error',text:'设备列表无法读取。'};}if(lastStatus)paintChip();refreshForm();refreshLive();});
 }
 function seedChoice(){
  if(choiceInitialized)return;
  choiceInitialized=true;
  const saved=devices?.remembered;
  // ASIO remains explicit each run. Prefill the last usable choice without
  // modifying the device manager or asserting it is already the live output.
  if(!choice&&current().backend==='auto'&&saved?.backend==='asio'&&deviceOptions('asio').some(o=>o.value===saved.device&&!o.disabled&&!o.placeholder))
   choice={backend:'asio',device:saved.device,buffer_frames:saved.buffer_frames};
 }
 function visibleInputs(s){
  const d=s.device;
  return [s.phase,s.actual,s.label,s.audition,s.displayMode,d?.backend,d?.device,d?.actual,JSON.stringify(d?.actualParameters||null),d?.state,d?.message,s.control_panel?.phase];
 }
 function paintChip(){
  // Output configuration belongs only in Global Settings.
  document.getElementById('output-status-chip')?.remove();
 }
 function update(value){
  const status=value?.outputStatus;if(!status)return;
  if(Number.isFinite(value.transport_order)){
   if(value.transport_order<lastOrder)return;
   lastOrder=value.transport_order;
  }
  const audition=value.audition??status.audition??state.audition;
  if(status===lastStatus&&audition===state.audition)return;
  const prior=lastStatus?visibleInputs(state):null;
  lastStatus=status;
  state={...status,audition};
  // Transport snapshots report actual playback, not the selected configuration.
  // Only audioDevices/setAudioDevice replies own the form's current setup.
  if(prior&&visibleInputs(state).every((item,index)=>item===prior[index]))return;
  paintChip();refreshLive();syncFormState();
 }
 function schedulePanelPoll(delay=320){
  clearTimeout(panelTimer);
  panelTimer=setTimeout(pollPanel,delay);
 }
 function panelStatus(p){
  const phase=p?.phase;
  return phase==='error'?{tone:'error',text:p.message||'控制面板未打开。'}:
   phase==='open'?{tone:'info',text:'控制面板已打开'}:
   phase==='releasing'?{tone:'info',text:'正在打开控制面板…'}:null;
 }
 function openPanel(){
  const f=formModel();if(!f.canPanel)return;
  const request=panelRequest={device:f.device};
  result={tone:'info',text:'正在打开控制面板…'};syncFormState();refreshLive();
  const done=raw=>{
   if(panelRequest!==request)return;
   panelRequest=null;
   let r;try{r=JSON.parse(raw);}catch{r={ok:false,error:'未收到控制面板结果。'};}
   if(!r.ok){result={tone:'error',text:r.error||'控制面板未打开。'};syncFormState();refreshLive();return;}
   panel=r.control_panel||(r.pending?{phase:'releasing',busy:true}:null);
   result=panelStatus(panel);syncFormState();refreshLive();
   schedulePanelPoll(0);
  };
  try{bridge.openAsioControlPanel(f.device,done);}catch{done('');}
  if(panelRequest===request)schedulePanelPoll(1800);
 }
 function pollPanel(){
  bridge.audioDevices(raw=>{
   let r;try{r=JSON.parse(raw);}catch{r=null;}
   if(!r?.ok){
    result={tone:'error',text:r?.error||'无法读取控制面板状态。'};refreshLive();
    if(busyPanel())schedulePanelPoll(800);
    return;
   }
   const unconfirmed=Boolean(panelRequest);
   panelRequest=null;devices=r;panel=r.control_panel||null;result=panelStatus(panel);
   if(unconfirmed&&(!panel||panel.phase==='idle'))result={tone:'error',text:'未收到控制面板打开结果。'};
   refreshLive();syncFormState();
   if(busyPanel())schedulePanelPoll();
  });
 }
 function bufferMarkup(f){
  if(!f.asio)return '';
  const values=[64,128,256,512,1024,2048];if(f.buffer&&!values.includes(f.buffer))values.push(f.buffer);values.sort((a,b)=>a-b);
  return `<div class="preferences-row aos-buffer-row"><label for="preferences-audio-buffer"><strong>请求缓冲</strong></label><span class="aos-buffer-controls"><select id="preferences-audio-buffer" class="control" ${devicePending?'disabled':''}><option value="" ${!f.buffer?'selected':''}>驱动默认</option>${values.map(v=>`<option value="${v}" ${f.buffer===v?'selected':''}>${v} 帧</option>`).join('')}</select><button type="button" class="control" data-asio-control-panel ${f.canPanel?'':'disabled'}>打开驱动控制面板</button></span></div>`;
 }
 function onChange(event){
  if(!devices)return;
  if(event.target.id==='preferences-audio-backend'){choice={backend:event.target.value,device:null};result=null;refreshForm();return;}
  if(event.target.id==='preferences-audio-buffer'){choice={...pending(),buffer_frames:Number(event.target.value)||null};syncFormState();return;}
  if(event.target.id==='preferences-audio-device'){choice={...pending(),device:event.target.value||null,buffer_frames:null};syncFormState();}
 }
 function onApply(event){
  if(event.target.dataset?.asioControlPanel!==undefined){openPanel();return;}
  if(!event.target.closest?.('[data-audio-device-apply]')||devicePending||!devices)return;
  const f=formModel();if(!f.canApply)return;
  if(typeof bridge?.setAudioDevice!=='function'){result={tone:'error',text:'当前运行版本不支持切换设备。'};refreshLive();return;}
  devicePending=true;result=null;refreshForm();refreshLive();
  bridge.setAudioDevice(JSON.stringify(f.payload),raw=>{
   let r;try{r=JSON.parse(raw);}catch{r={ok:false,error:'未收到有效结果，设备未确认。'};}
   devicePending=false;
   if(r.ok){choice=null;if(r.devices)devices=r.devices;result=null;}
   else result={tone:'error',text:r.error||'切换未完成，当前输出不变。'}; // keep the pending choice on refusal
   if(r.outputStatus)update({outputStatus:r.outputStatus});refreshForm();refreshLive();
   if(r.ok)followSwitch(Date.now()+4000,f.payload);
  });
 }
 document.addEventListener('change',onChange);
 document.addEventListener('click',onApply);
 // WebChannel signals can be coalesced (background/offscreen windows): read the result explicitly,
 // as app.js does after transport commands, until the new device reports or 4 s pass.
 function followSwitch(until,requested){
  if(typeof bridge?.poll!=='function')return;
  bridge.poll(raw=>{let s=null;try{s=JSON.parse(raw).player;}catch{}if(s)update(s);
   const st=s?.outputStatus?.device?.state;
   if(st==='active'&&s?.outputStatus?.device?.actual){result={tone:'ok',text:'已切换。'};refreshLive();}
   else if(st==='rolled-back'||st==='failed'){
    result={tone:'error',text:s.outputStatus.device.message||'切换未完成，当前输出不变。'};
    if(!choice&&requested)choice={backend:requested.backend,device:requested.device,buffer_frames:requested.buffer_frames};
    // Read the manager's actual rollback before offering the same choice again.
    bridge.audioDevices(raw=>{try{const r=JSON.parse(raw);if(r.ok){devices=r;panel=r.control_panel||panel;}}catch{}refreshForm();refreshLive();});
    refreshLive();
   }
   if((st==='switching'||!st)&&Date.now()<until)setTimeout(()=>followSwitch(until,requested),200);});
 }
 function attach(value){
  if(value!==bridge){clearTimeout(panelTimer);panel=null;panelRequest=null;choice=null;choiceInitialized=false;devices=null;deviceRequest=null;result=null;}
  bridge=value;
  update(window.AstraPlaybackPlayer?.());
 }
 return {attach,update,openSettings:loadDevices,settingsMarkup:markup,_test:{formModel,liveModel,setDevices:v=>{devices=v;},setChoice:v=>{choice=v;},getChoice:()=>choice,onChange,onApply,liveMarkup,formMarkup,markup,actualModel}};
})();
