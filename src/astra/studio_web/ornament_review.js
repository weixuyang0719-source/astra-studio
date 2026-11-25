"use strict";
// Saved decisions and preview capabilities are separate from the formal PDF.
window.AstraOrnamentReview=(()=>{
  let identity='',serial=0,timer=0,request=null,result=null,message='',busy=false,previewed=new Set();
  function flourishRange(){const r=window.AstraProjectEditor?.localScope?.();return r&&Number.isFinite(r.start)&&Number.isFinite(r.end)&&r.start>=0&&r.end>r.start?{start_seconds:r.start,end_seconds:r.end}:null;}
  const key=()=>JSON.stringify([state.project?.path,state.project?.score_export||state.project?.export,state.project?.editor?.revision]);
  const dirty=()=>Boolean(state.project?.editor?.score_stale??state.project?.editor?.dirty);
  function stop(){if(timer)clearTimeout(timer);timer=0;serial++;request=null;busy=false;}
  function sync(){if(identity!==key()){stop();identity=key();result=null;message='';previewed.clear();}}
  function body(){
    const disabled=busy||state.job.running||dirty(),buttons=(text,action,id,extra={})=>button(text,action,{key:id,disabled,...extra});
    const selection=flourishRange();
    return `<h3>装饰音与花奏</h3><p class="form-note">只影响记谱。短引音也可能是提前低音，请先比较再采用。</p>
      ${dirty()?'<p role="status">编辑尚未更新到谱面，请先更新谱面再读取候选。</p>':''}
      ${buttons(result?'刷新建议':'查找建议','ornamentOpen','')}
      <p id="ornament-message" role="status">${html(message)}</p>
      ${result?.needs_update?'<p class="ornament-pending">决定已保存，待更新谱面；当前 PDF 尚未改变。</p>':''}
      ${result&&!result.available?`<p>${html(result.reason)}</p>`:''}
      ${result?.available&&!result.candidates.length?'<p>本版没有满足结构条件的短引音候选。</p>':''}
      ${result?.available?`<details class="ornament-flourish"><summary>将范围记为花奏</summary><p class="form-note">人工确认后粗记谱，Astra MIDI 保持原演奏。限同小节、同手、同声部的 4–16 个连续单音；普通有节奏跑动不自动转换。</p><p>${selection?`上方标尺范围 ${selection.start_seconds.toFixed(3)} – ${selection.end_seconds.toFixed(3)} 秒`:'先在音符页的上方标尺指定花奏范围，再回到此处；试听循环不代替编辑选区。'}</p><label>手部<select id="ornament-flourish-staff" class="control"><option value="1">右手</option><option value="2">左手</option></select></label>${buttons('用此范围创建建议','ornamentCreateFlourish','',{disabled:disabled||!selection})}</details>`:''}
      ${(result?.candidates||[]).map(c=>`<section class="ornament-candidate"><strong>第 ${html(c.measure)} 小节 · ${String(c.staff)==='1'?'右手':'左手'}</strong><p>${html(c.label)}</p><details><summary>判断依据</summary><p>${html(c.explanation)}</p></details><span class="form-note">${c.decision==='accept'?'已采用':c.decision==='reject'?'不采用':'未决定'}</span><div class="ornament-actions">${buttons('预览','ornamentPreview',c.id)}${buttons(c.kind==='free_run'&&!previewed.has(c.id)?'先预览再采用':'采用','ornamentAccept',c.id,{disabled:disabled||(c.kind==='free_run'&&!previewed.has(c.id))})}${buttons('不采用','ornamentReject',c.id)}${buttons('重置决定','ornamentClear',c.id,{disabled:disabled||!c.decision,label:'恢复为未决定，更新谱面后生效'})}</div></section>`).join('')}
      ${result?.can_undo?buttons('撤销上次决定','ornamentUndo',''):''}
      ${result?.pending_count?`<p>有 ${result.pending_count} 项历史决定不再符合当前候选，已保留待复核。</p>`:''}
      ${result?.preview?button("返回正式谱面","ornamentOriginal"):''}`;
  }
  function markup(){sync();return activeInstrument==='piano'&&state.project?.editor?`<details class="ornament-disclosure" ${result?.candidates?.length||result?.preview?'open':''}><summary>装饰音与花奏</summary><aside id="ornament-review" class="ornament-review" aria-label="装饰音与花奏">${body()}</aside></details>`:'';}
  function paint(){const host=$('ornament-review');if(host)host.innerHTML=body();if(typeof render==='function')render(true);}
  function previewMarkup(){return result?.preview?`<div class="ornament-preview-main"><p role="status">装饰采用效果 · 所选小节及邻近小节 · 仅预览，未保存${button('返回正式谱面','ornamentOriginal')}</p><img alt="所选装饰引音及相邻小节采用预览" src="${html(result.preview.dataUrl)}"></div>`:'';}
  function action(name,id){
    sync();if(name==='ornamentOriginal'){if(result)delete result.preview;paint();return;}if(busy||state.job.running||dirty())return;
    const operation={ornamentOpen:'open',ornamentPreview:'preview',ornamentAccept:'accept',ornamentReject:'reject',ornamentClear:'clear',ornamentUndo:'undo',ornamentCreateFlourish:'create_flourish'}[name];if(!operation)return;
    if(typeof bridge?.startOrnamentReview!=='function'){message='此运行版本尚未提供装饰审阅，请重启新版应用后使用。';paint();return;}
    if(operation==='accept'&&result?.candidates.find(c=>c.id===id)?.kind==='free_run'&&!previewed.has(id)){message='请先预览这组人工花奏的记谱效果，再明确采用。';paint();return;}
    const payload={action:operation,base_revision:state.project.editor.revision};
    if(operation==='create_flourish'){const selection=flourishRange(),staff=Number($('ornament-flourish-staff')?.value);if(!result?.available||!selection||![1,2].includes(staff)){message='请先在卷帘明确框选范围，并选择左右手。';paint();return;}payload.selection={...selection,staff};}
    if(operation!=='open')Object.assign(payload,{token:request?.token,overlay_revision:result?.overlay_revision,candidate_id:id});
    const scope=identity,version=++serial;busy=true;message=operation==='preview'?'正在生成局部采用效果，正式谱保持…':'正在读取或保存装饰决定…';paint();
    const alive=()=>version===serial&&scope===key();
    const fail=error=>{busy=false;message=error||'装饰审阅失败，正式谱面保持。';paint();};
    const poll=()=>{timer=0;if(!alive())return;bridge.ornamentReviewStatus(request.token,raw=>{
      if(!alive())return;const value=JSON.parse(raw);
      if(value.ok===false||value.error||['failed','stale'].includes(value.status)){fail(value.error);return;}
      if(value.status==='running'){timer=setTimeout(poll,400);return;}
      if(value.status!=='ready'||!value.result){fail('没有返回有效装饰候选。');return;}
      const priorOverlay=result?.overlay_revision;result=value.result;if(priorOverlay!==result.overlay_revision||operation==='open')previewed.clear();if(operation==='preview'&&result.preview)previewed.add(id);busy=false;message=result.message||'候选已读取；不会自动采用。';paint();
    });};
    bridge.startOrnamentReview(JSON.stringify(payload),raw=>{if(!alive())return;const value=JSON.parse(raw);if(!value.ok){fail(value.error);return;}request={token:value.token};timer=setTimeout(poll,300);});
  }
  return {markup,sync,action,previewMarkup,hasPreview:()=>Boolean(result?.preview)};
})();
