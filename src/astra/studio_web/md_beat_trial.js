"use strict";
window.AstraMdTrial=(()=>{
 let dialogNode=null,timer=0,serial=0,identity='',scope=null,reference=[];
 const ident=()=>JSON.stringify([state.project?.path,state.project?.editor?.revision]);
 const esc=s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
 function close(){serial++;clearTimeout(timer);dialogNode?.close();dialogNode?.remove();dialogNode=null;}
 function open(){
  close();scope=window.AstraProjectEditor?.localScope?.();
  if(!scope||scope.end<=scope.start||scope.end-scope.start>24){alert('请先在标尺设置不超过24秒的 L/R 范围。两端应是你确认的拍点。');return;}
  identity=ident();reference=(state.project.editor.tempo.pulse_points?.length?state.project.editor.tempo.pulse_points:(window.AstraProjectEditor?.rhythmFlags?.()||[])).filter(p=>Number.isFinite(p.seconds)&&p.seconds>scope.start&&p.seconds<scope.end);
  dialogNode=document.createElement('dialog');dialogNode.className='dialog';dialogNode.style.maxWidth='700px';
  dialogNode.innerHTML=`<div class="dialog-head"><h2>条件试算 · 实验</h2><button type="button" class="icon-button" id="md-close" aria-label="关闭">×</button></div><div class="dialog-body"><p>用确认的拍点引导 MD Beat This。仅预览，不改工程。</p><p class="form-note">范围 ${scope.start.toFixed(3)}–${scope.end.toFixed(3)} 秒，两端自动作为参考。拍号不是模型输入；模型感知拍也不一定等于四分或八分音符。</p><label>期望间隔数 <input class="control" id="md-count" type="number" min="1" max="128" value="${Math.max(1,reference.length+1)}"></label><small>只核对结果，不强制模型生成指定拍数。参考点只表示“这里有拍”，不表示两点之间没有其他拍。</small><details><summary>选择内部参考拍点（默认不选）</summary><div style="max-height:150px;overflow:auto">${reference.map((p,i)=>`<label style="display:inline-flex;margin:6px"><input type="checkbox" name="md-reference" value="${i}"> ${p.seconds.toFixed(3)} s</label>`).join('')||'此范围没有内部拍点'}</div></details><label style="display:flex;gap:8px;margin-top:12px"><input type="checkbox" id="md-compare">上下文稳定性对照（额外一次推理，耗时更长）</label><label style="display:flex;gap:8px;margin-top:16px"><input type="checkbox" id="md-license">我用于个人研究 / 非商业用途（CC BY-NC 4.0）</label><p class="form-note">使用当前工程原音频，仅本机推理，不上传音频。请只使用有权处理的素材。关闭窗口不应用结果；后台计算可能继续完成。</p><div id="md-status" role="status"></div><canvas id="md-preview" width="620" height="100" style="width:100%;display:none" aria-label="上排原拍点，下排模型建议拍点"></canvas><div id="md-result"></div></div><div class="dialog-foot"><button class="secondary" id="md-dismiss">关闭</button><button class="primary" id="md-run">开始试算</button></div>`;
  document.body.appendChild(dialogNode);dialogNode.showModal();
  dialogNode.querySelector('#md-close').onclick=close;dialogNode.querySelector('#md-dismiss').onclick=close;
  dialogNode.addEventListener('cancel',()=>{serial++;clearTimeout(timer);});
  dialogNode.querySelector('#md-run').onclick=start;
 }
 function message(text){if(dialogNode)dialogNode.querySelector('#md-status').textContent=text;}
 function start(){
  if(identity!==ident()){message('工程已改变，请关闭后重新选择范围');return;}
  if(!dialogNode.querySelector('#md-license').checked){message('请先确认非商业研究用途');return;}
  if(typeof bridge?.mdBeatTrial!=='function'){message('当前运行程序尚未加载试算入口，请重新启动应用');return;}
  const count=Number(dialogNode.querySelector('#md-count').value);
  if(!Number.isInteger(count)||count<1||count>128){message('请输入1至128的整数间隔数');return;}
  const known=[...dialogNode.querySelectorAll('[name="md-reference"]:checked')].map(e=>reference[Number(e.value)].seconds);
  const my=++serial;dialogNode.querySelector('#md-run').disabled=true;message('正在本机后台试算…实际拍点保持不变');
  dialogNode.querySelector('#md-result').textContent='';dialogNode.querySelector('#md-preview').style.display='none';
  const payload={action:'start',base_revision:state.project.editor.revision,request:{research_noncommercial:true,start:scope.start,end:scope.end,known_beats:known,interval_count:count,steps:8,audit:true,compare_context:dialogNode.querySelector('#md-compare').checked}};
  function receive(raw){if(my!==serial||!dialogNode)return;let r;try{r=JSON.parse(raw);}catch{r={ok:false,error:'返回格式无效'};}
   if(identity!==ident()){message('工程已改变，旧预览已失效');dialogNode.querySelector('#md-run').disabled=false;return;}
   if(!r.ok||['failed','stale','timed_out'].includes(r.status)){message(r.error||'试算失败，原拍点未改变');dialogNode.querySelector('#md-run').disabled=false;return;}
   if(r.status==='running'){timer=setTimeout(()=>bridge.mdBeatTrial(JSON.stringify({action:'status',token:r.token}),receive),500);return;}
   dialogNode.querySelector('#md-run').disabled=false;const result=r.result;
   if(!result){message('试算没有返回结果');return;}
   message(`真实上下文 ${result.audio_context?.real_audio_seconds?.toFixed(1)||'?'} 秒 · 参考 ${result.conditioning.length} 点 · 期望 ${count} 个间隔 · 模型 ${result.interval_count} 个间隔。${result.count_matches?'数量相符，但仍须判断拍级。':'数量不符，不能应用。'}`);
   dialogNode.querySelector('#md-result').innerHTML='<p>仅预览 · 未改变工程或谱面</p><small>上排：原拍点；下排：模型建议。模型不会学习或记住本次修改。</small><p style="max-height:80px;overflow:auto">'+esc(result.beat_seconds.map(t=>t.toFixed(3)).join(' · '))+'</p>';
   const comparison=result.context_comparison;
   if(comparison?.requested){
    const row=document.createElement('p');row.className='form-note';
    row.textContent=!comparison.comparable?'两份实际音频窗口相同，无法比较；未重复推理。':`独立窗口对照：主窗口 ${result.interval_count} 间隔 / 对照窗口 ${comparison.alternate.interval_count} 间隔。${comparison.window_sensitive?'拍级或位置随上下文改变，请人工判断。':'本次结果接近，但不代表拍级一定正确。'} 不按期望拍数挑选答案。`;
    dialogNode.querySelector('#md-result').appendChild(row);
    if(comparison.comparable){const list=document.createElement('details');const label=document.createElement('summary');label.textContent='查看对照窗口原始拍点';list.appendChild(label);const content=document.createElement('p');content.textContent=comparison.alternate.beat_seconds.map(t=>t.toFixed(3)).join(' · ');content.style.maxHeight='70px';content.style.overflow='auto';list.appendChild(content);dialogNode.querySelector('#md-result').appendChild(list);}
   }
   const canvas=dialogNode.querySelector('#md-preview');canvas.style.display='block';const ctx=canvas.getContext('2d');ctx.clearRect(0,0,620,100);
   const draw=(points,y,color)=>{ctx.strokeStyle=color;ctx.beginPath();ctx.moveTo(12,y);ctx.lineTo(608,y);ctx.stroke();for(const t of points){const x=12+(t-scope.start)/(scope.end-scope.start)*596;ctx.fillStyle=color;ctx.fillRect(x-1,y-9,2,18);}};
   draw([scope.start,...reference.map(p=>p.seconds),scope.end],28,'#8292a5');draw(result.beat_seconds,72,'#537faa');
  }
  bridge.mdBeatTrial(JSON.stringify(payload),receive);
 }
 document.addEventListener('click',e=>{if(e.target.closest('[data-do="editorMdTrial"]')){e.preventDefault();e.stopImmediatePropagation();open();}},true);
 return {open,close};
})();
