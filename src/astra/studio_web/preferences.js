"use strict";
// One Settings destination. Left: categories that exist in Astra today (no empty
// or placeholder pages). Right: the selected category. Every control keeps its
// original id, value source, scope and save semantics:
//   外观与操作 / 保存与恢复 — saved immediately on change (unchanged bridges);
//   音频输出 — pending choice, applied with its own 「应用」 (output_status.js);
//   快捷键 — read-only overview here, edited in the existing shortcut editor;
//   转换与导出 — project/run scope, only listed when a session exists.
window.AstraPreferences=(()=>{
  const defaults={cursor_theme:'soft',rhythm_motion:true,notes_return_on_pause:false};
  const backupDefaults={enabled:true,interval_minutes:5,retention_count:10};
  let backup={...backupDefaults},backupReady=false;
  let current={...defaults},initialized=false,pending=false,page='appearance';
  function apply(value){current={...defaults,...value};window.AstraCursors?.theme(current.cursor_theme);window.AstraRhythmMotion?.setEnabled(current.rhythm_motion);}
  function hydrate(value,settings){
    if(!initialized&&value){initialized=true;apply(value);}
    if(!backupReady&&settings){backupReady=true;backup={...backupDefaults,...settings};controls();}
  }
  function status(message,error=false){const el=document.getElementById('preferences-status');if(el){el.textContent=message;el.dataset.error=String(error);}}
  function controls(){
    for(const el of document.querySelectorAll('[data-ui-preference],#preferences-reset'))el.disabled=pending;
    for(const el of document.querySelectorAll('[data-backup-preference]')){
      const key=el.dataset.backupPreference;
      if(key==='enabled')el.checked=backup.enabled;else el.value=backup[key];
      el.disabled=pending||!backupReady||(key==='interval_minutes'&&!backup.enabled);
    }
  }
  function saveBackup(el){
    const key=el.dataset.backupPreference;
    if(pending||!backupReady||!Object.hasOwn(backupDefaults,key))return;
    const value=key==='enabled'?el.checked:el.valueAsNumber;
    if(key!=='enabled'&&(!el.validity.valid||!Number.isInteger(value))){
      controls();status('请输入范围内的整数。',true);return;
    }
    if(typeof bridge?.saveEditorAutosaveSettings!=='function'){
      controls();status('当前版本无法保存这项设置。',true);return;
    }
    pending=true;controls();status('正在保存…');
    bridge.saveEditorAutosaveSettings(JSON.stringify({[key]:value}),raw=>{
      let result;try{result=typeof raw==='string'?JSON.parse(raw):raw;}catch{result={ok:false};}
      pending=false;
      if(result?.ok){backup={...backupDefaults,...result.settings};status('已保存，适用于所有项目。');}
      else status('未能保存设置。'+(result?.error||'请稍后重试。'),true);
      controls();
    });
  }
  function save(patch){
    if(pending)return;
    if(typeof bridge?.saveUiPreferences!=='function'){status('当前版本尚不能保存设置，请重新启动应用。',true);return;}
    pending=true;controls();status('正在保存…');
    bridge.saveUiPreferences(JSON.stringify(patch),raw=>{
      let result;try{result=typeof raw==='string'?JSON.parse(raw):raw;}catch{result={ok:false};}
      pending=false;
      if(result?.ok){apply(result.preferences);status('已保存，下次启动继续使用。');}
      else status('未保存，原设置未改变。'+(result?.error||'请稍后重试。'),true);
      const cursor=document.getElementById('preferences-cursor');if(cursor)cursor.value=current.cursor_theme;
      const motion=document.getElementById('preferences-motion');if(motion)motion.checked=current.rhythm_motion;
      const resetPause=document.getElementById('preferences-notes-return-on-pause');if(resetPause)resetPause.checked=current.notes_return_on_pause;
      controls();
    });
  }

  // ---- pages -------------------------------------------------------------------
  const esc=value=>typeof html==='function'?html(value):String(value??'');
  const APP='应用全局 · 本机所有项目',PROJECT='工程 · 当前项目';
  function sessionOpen(){try{return Boolean(state.project||state.audio||state.input_midi);}catch{return false;}}
  function jobBusy(){try{return Boolean(state.job?.running||state.gpu_preflight);}catch{return false;}}
  function pages(){
    return [
      {id:'appearance',label:'外观与操作',scope:'app'},
      ...(window.AstraOutputStatus?.settingsMarkup?[{id:'audio',label:'音频输出',scope:'app'}]:[]),
      ...(pluginAvailable()?[{id:'plugins',label:'插件管理',scope:'app'}]:[]),
      {id:'recovery',label:'保存与恢复',scope:'app'},
      ...(window.AstraEditorShortcuts?[{id:'keys',label:'快捷键',scope:'app'}]:[]),
      ...(sessionOpen()?[{id:'project',label:'当前项目',scope:'project'}]:[]),
    ];
  }
  const pageHead=(title,scope,note)=>`<header class="settings-page-head"><h3>${esc(title)}</h3><span class="settings-scope" data-scope="${scope}">${scope==='project'?PROJECT:APP}</span>${note?`<p class="settings-page-note">${esc(note)}</p>`:''}</header>`;
  function appearancePage(){
    return pageHead('外观与操作','app','更改后立即保存，不改变音乐项目。')+`<section class="preferences-section" aria-labelledby="preferences-appearance"><h4 id="preferences-appearance" class="sr-only">外观与操作</h4>
        <label class="preferences-row" for="preferences-cursor"><span><strong>鼠标样式</strong><small>只影响应用内部；文本输入仍使用系统光标。</small></span><select id="preferences-cursor" data-ui-preference="cursor_theme" class="control"><option value="soft" ${current.cursor_theme==='soft'?'selected':''}>柔和工具光标</option><option value="system" ${current.cursor_theme==='system'?'selected':''}>系统光标</option></select></label>
        <label class="preferences-row" for="preferences-motion"><span><strong>拍位调整过渡</strong><small>调整拍点后平滑移动画面，不延迟编辑和播放。</small></span><input type="checkbox" id="preferences-motion" data-ui-preference="rhythm_motion" ${current.rhythm_motion?'checked':''}></label>
        ${window.AstraRhythmMotion?.reduced()?'<p class="preferences-caption">系统已减少动画，因此过渡暂不播放。</p>':''}
        <label class="preferences-row" for="preferences-notes-return-on-pause"><span><strong>音符页暂停时回到播放起点</strong><small>关闭时停在当前位置。只影响音符页的当前 MIDI，不影响曲谱和分轨。</small></span><input type="checkbox" id="preferences-notes-return-on-pause" data-ui-preference="notes_return_on_pause" ${current.notes_return_on_pause?'checked':''}></label>
        <div class="settings-page-actions">${button('恢复外观默认','preferencesReset',{style:'quiet'})}</div>
      </section>`;
  }
  function audioPage(){
    return pageHead('音频输出','app')+(window.AstraOutputStatus?.settingsMarkup?.()||'');
  }
  function recoveryPage(){
    return pageHead('保存与恢复','app','更改后立即保存，适用于所有项目。正式项目仍用 Ctrl+S 保存。')+`<section class="preferences-section" aria-labelledby="preferences-backups"><h4 id="preferences-backups" class="sr-only">保存与恢复</h4>
        <label class="preferences-row"><span><strong>自动保存恢复副本</strong><small>为每个项目在后台保留独立副本。正式项目用 Ctrl+S 保存。</small></span><input type="checkbox" data-backup-preference="enabled"></label>
        <label class="preferences-row"><span><strong>保存间隔（分钟）</strong></span><input class="control" type="number" min="1" max="120" step="1" required data-backup-preference="interval_minutes"></label>
        <label class="preferences-row"><span><strong>最多保留的自动恢复副本数</strong><small>超额自动副本移至系统回收站；恢复前保护的正式版本另行保留。</small></span><input class="control" type="number" min="1" max="100" step="1" required data-backup-preference="retention_count"></label>
        <button type="button" id="preferences-retention-retry" class="quiet" data-do="retryEditorRetention" hidden>重试清理旧恢复副本</button>
      </section>`;
  }
  // Categories follow the existing command sources; nothing is invented.
  function keyGroup(command){
    const source=String(command.source||'');
    return /transport_category/.test(source)?'走带':/zoom_category|midi_category|^Astra/.test(source)?'视图':/tool_category/.test(source)?'工具':'编辑';
  }
  function keysPage(){
    const api=window.AstraEditorShortcuts,bindings=api.bindings(),groups=new Map([['编辑',[]],['工具',[]],['视图',[]],['走带',[]]]);
    for(const command of api.commands)groups.get(keyGroup(command)).push(command);
    const renderGroup=(name,rows)=>rows?.length?`<section class="settings-keys-group" aria-label="${name}"><h4>${name}</h4><dl>${rows.map(c=>`<div><dt>${esc(c.label)}</dt><dd>${bindings[c.action]?`<kbd>${esc(bindings[c.action])}</kbd>`:'<span class="settings-key-none">未分配</span>'}</dd></div>`).join('')}</dl></section>`:'';
    const lists=[['编辑'],['工具','视图','走带']].map(column=>`<div class="settings-keys-column">${column.map(name=>renderGroup(name,groups.get(name))).join('')}</div>`).join('');
    return pageHead('快捷键','app','分配后保存到本机，不改工程。默认主命令参照 Cubase 16。')+`<section class="preferences-section settings-keys">
        <div class="preferences-row"><span><strong>编辑与走带快捷键</strong><small>在编辑器中录制新组合键、清除或恢复默认。</small></span>${button('修改快捷键…','editorKeyCommands',{symbol:'keyboard'})}</div>
        <div class="settings-keys-grid">${lists}</div>
        <p class="preferences-caption">固定按键：Space 试听 · Ctrl+S 保存项目 · Esc 取消 · H / G 时间缩放 · Shift+F 适合全曲 · F 跟随 · Ctrl+I 信息行 · Alt+I 乐器与 MIDI 侧栏 · Ctrl+Alt+T 走带。</p>
      </section>`;
  }
  function projectPage(){
    return pageHead('当前项目','project')+projectSettingsMarkup(jobBusy());
  }
  const pluginTabs=[['instrument','乐器'],['effect','效果器'],['failed','扫描失败']];
  let pluginView='instrument', pluginFilter='', pluginState=null, pluginTimer=0, pluginPending=false, pluginLastResults=null, pluginLastTable='';
  function pluginAvailable(){return typeof bridge!=='undefined'&&typeof bridge?.vstPluginManager==='function';}
  function pluginVisible(){const panel=document.getElementById('settings-page-plugins');return Boolean(panel&&!panel.hidden&&panel.closest('dialog')?.open);}
  function pluginsPage(){
    return pageHead('插件管理','app')+`<section class="preferences-section plugin-manager" aria-label="VST 插件">
      <div class="plugin-manager-tools">
        <input id="plugin-manager-filter" class="control" type="search" placeholder="搜索插件、厂商或类别" aria-label="搜索插件、厂商或类别" value="${esc(pluginFilter)}">
        <button type="button" id="plugin-manager-refresh" class="secondary">刷新插件</button>
      </div>
      <div class="plugin-manager-views" role="group" aria-label="插件类型">${pluginTabs.map(([key,label])=>`<button type="button" data-plugin-view="${key}" aria-pressed="${key===pluginView}">${label}</button>`).join('')}</div>
      <p id="plugin-manager-status" role="status" aria-live="polite"></p>
      <div id="plugin-manager-results"></div>
    </section>`;
  }
  function pluginPaint(){
    if(!pluginVisible())return;
    const s=pluginState||{}, refresh=document.getElementById('plugin-manager-refresh'), status=document.getElementById('plugin-manager-status'), results=document.getElementById('plugin-manager-results');
    if(!refresh||!status||!results)return;
    refresh.disabled=Boolean(s.running||pluginPending);
    const active=Boolean(s.running);
    const msg=s.error?String(s.error):active?(s.total>0?`正在扫描 VST3 插件（${s.completed??0}/${s.total}）${s.module?` · ${s.module}`:''}`:'正在准备扫描 VST3 插件…'):pluginState?'':'正在读取插件列表…';
    if(status.textContent!==msg)status.textContent=msg;
    status.dataset.error=String(Boolean(s.error));
    const list=pluginView==='failed'?(s.failures||[]):(s.inventory||[]).filter(x=>x.kind===(pluginView==='effect'?'fx':'instrument'));
    const filter=pluginFilter.trim().toLocaleLowerCase();
    const rows=list.filter(x=>[x.name,x.vendor,x.category,x.format||"VST3",x.module,x.error].some(v=>String(v??'').toLocaleLowerCase().includes(filter)));
    const failed=pluginView==='failed';
    const values=failed?['name','reason']:['name','vendor','category','format'];
    const headings=failed?['名称','原因']:['名称','厂商','类别','格式'];
    const table=rows.length?`<table aria-label="${pluginTabs.find(x=>x[0]===pluginView)[1]}"><thead><tr>${headings.map(h=>`<th scope="col">${h}</th>`).join('')}</tr></thead><tbody>${rows.map(r=>`<tr>${values.map(k=>`<td>${esc(r[k]||(k==='name'?r.module:k==='reason'?r.error:k==='format'?'VST3':'')||'—')}</td>`).join('')}</tr>`).join('')}</tbody></table>`:`<p class="plugin-manager-empty">${filter?'未找到匹配的插件':active?'扫描中…':failed?'没有扫描失败的插件':'暂无此类插件'}</p>`;
    if(results!==pluginLastResults||pluginLastTable!==table){results.innerHTML=table;pluginLastResults=results;pluginLastTable=table;}
  }
  function pluginRequest(action='status'){
    clearTimeout(pluginTimer);pluginTimer=0;
    if(!pluginVisible()||!pluginAvailable()||pluginPending)return;
    pluginPending=true;pluginPaint();
    bridge.vstPluginManager(JSON.stringify({action}),raw=>{
      pluginPending=false;
      try{
        const value=typeof raw==='string'?JSON.parse(raw):raw;
        if(!value||typeof value!=='object')throw Error('bad response');
        pluginState=value;
      }catch{pluginState={...pluginState,running:false,error:'无法读取插件扫描状态。'};}
      pluginPaint();
      if(pluginVisible()&&pluginState?.running)pluginTimer=setTimeout(()=>pluginRequest(),450);
    });
  }

  const RENDER={appearance:appearancePage,audio:audioPage,plugins:pluginsPage,recovery:recoveryPage,keys:keysPage,project:projectPage};
  function activate(){
    const panel=document.getElementById("settings-page-"+page);
    if(panel&&!panel.hasChildNodes())panel.innerHTML=RENDER[page]();
    const reset=panel?.querySelector('[data-do="preferencesReset"]');
    if(reset)reset.id='preferences-reset';
    controls();
    paintSaveStatus();
    if(page==='audio')window.AstraOutputStatus?.openSettings?.();
    if(page==='plugins'){pluginPaint();pluginRequest();}else{clearTimeout(pluginTimer);pluginTimer=0;}
  }
  // Page change motion (presentation only; the switch itself stays immediate and synchronous).
  // The page area eases from the old height to the new one while the old page fades out in place
  // and the new page's blocks settle in, in navigation direction. Rapid switches continue from the
  // currently painted opacity so an unfinished page never flashes to full opacity before fading out.
  let swapCleanup=null,swapState=null;
  const EASE='cubic-bezier(.22,1,.36,1)';
  function reducedMotion(){try{return matchMedia('(prefers-reduced-motion: reduce)').matches;}catch{return false;}}
  function clearInlineOpacity(blocks){for(const b of blocks)b.style.removeProperty('opacity');}
  function resetLeavingPanel(panel,blocks,keepVisible=false){
    if(!panel)return;
    if(!keepVisible&&panel.id!=='settings-page-'+page)panel.hidden=true;
    panel.classList.remove('settings-page-leaving');
    panel.removeAttribute('aria-hidden');
    panel.inert=false;
    if(blocks)clearInlineOpacity(blocks);
  }
  function samplePanel(panel){
    if(!panel||panel.hidden)return null;
    const cs=getComputedStyle(panel),opacity=parseFloat(cs.opacity);
    if(!(opacity>.02))return null;
    const blocks=[...panel.children].slice(0,7);
    const blockOpacities=blocks.map(b=>parseFloat(getComputedStyle(b).opacity));
    const maxBlock=blockOpacities.length?Math.max(...blockOpacities):1;
    if(!(opacity*maxBlock>.02))return null;
    return {panel,opacity,transform:cs.transform&&cs.transform!=='none'?cs.transform:'none',blocks,blockOpacities,maxBlock};
  }
  function animateSwap(area,leavingSnapshots,entering,from,dir,enterSnapshot=null){
    if(!area?.animate||reducedMotion()||!entering)return;
    const leavingEntries=[],enterMotions=[];let heightMotion=null,observer=null,naturalHeight=from;
    const done=keep=>{
      if(swapCleanup!==done)return;swapCleanup=null;swapState=null;
      observer?.disconnect();
      for(const entry of leavingEntries){
        if(entry.done)continue;
        entry.done=true;
        resetLeavingPanel(entry.panel,entry.frozenBlocks,Boolean(keep?.has(entry.panel)));
        entry.motion.cancel();
      }
      heightMotion?.cancel();
      for(const m of enterMotions)m.cancel();
      area.classList.remove('settings-swapping');
    };
    const settle=()=>{if(swapCleanup===done&&leavingEntries.every(e=>e.done)&&enterMotions.every(m=>m.playState==='finished')&&(!heightMotion||heightMotion.playState==='finished'))done();};
    // Ease from the current painted height to the entering panel's natural height without
    // cancelling the active height animation beforehand (avoids mid-frame snapping in ResizeObserver).
    const easeHeight=(start,target)=>{
      const prev=heightMotion;
      heightMotion=null;
      naturalHeight=target;
      const distance=Math.abs(target-start);
      if(distance<=1){prev?.cancel();settle();return;}
      const motion=heightMotion=area.animate([{height:`${start}px`},{height:`${target}px`}],{duration:Math.round(Math.min(420,220+distance*.22)),easing:EASE,fill:'forwards'});
      prev?.cancel();
      motion.finished.then(settle,()=>{});
    };
    swapCleanup=done;
    swapState={leavingPanels:leavingSnapshots.map(s=>s.panel),entering};
    area.classList.add('settings-swapping');
    for(const snap of leavingSnapshots){
      const panel=snap.panel;
      if(!panel||panel===entering)continue;
      const frozenBlocks=[];
      snap.blocks.forEach((b,i)=>{
        const op=snap.blockOpacities[i];
        if(Number.isFinite(op)&&op<.999){b.style.opacity=String(op);frozenBlocks.push(b);}
      });
      panel.hidden=false;panel.inert=true;panel.setAttribute('aria-hidden','true');panel.classList.add('settings-page-leaving');
      const duration=Math.max(50,Math.round(150*Math.min(1,snap.opacity*snap.maxBlock)));
      const motion=panel.animate([{opacity:snap.opacity,transform:snap.transform},{opacity:0,transform:`translateY(${-10*dir}px)`}],{duration,easing:'cubic-bezier(.4,0,1,1)',fill:'forwards'});
      const entry={panel,frozenBlocks,motion,done:false};
      leavingEntries.push(entry);
      // Hide each leaving panel immediately once its fade completes so it never lingers at opacity:0.
      motion.finished.then(()=>{
        if(swapCleanup!==done||entry.done)return;
        entry.done=true;
        resetLeavingPanel(panel,frozenBlocks,false);
        motion.cancel();
        settle();
      },()=>{});
    }
    if(enterSnapshot){
      const duration=Math.max(120,Math.round(120+200*(1-Math.min(1,enterSnapshot.opacity*enterSnapshot.maxBlock))));
      if(enterSnapshot.blocks.length){
        enterSnapshot.blocks.forEach((block,i)=>{
          const startOp=Math.min(1,enterSnapshot.opacity*(Number.isFinite(enterSnapshot.blockOpacities[i])?enterSnapshot.blockOpacities[i]:1));
          enterMotions.push(block.animate([{opacity:startOp,transform:enterSnapshot.transform},{opacity:1,transform:'none'}],{duration,easing:EASE,fill:'backwards'}));
        });
      }else{
        enterMotions.push(entering.animate([{opacity:enterSnapshot.opacity,transform:enterSnapshot.transform},{opacity:1,transform:'none'}],{duration,easing:EASE,fill:'backwards'}));
      }
    }else{
      const blocks=[...entering.children].slice(0,7);
      blocks.forEach((block,i)=>enterMotions.push(block.animate([{opacity:0,transform:`translateY(${12*dir}px)`},{opacity:1,transform:'none'}],{duration:300,delay:70+i*38,easing:EASE,fill:'backwards'})));
    }
    let panelHeight=entering.getBoundingClientRect().height;
    easeHeight(from,panelHeight);
    if(typeof ResizeObserver==='function'){
      // Observe the active panel rather than the animated wrapper to avoid a feedback loop.
      observer=new ResizeObserver(()=>{
        if(swapCleanup!==done)return;
        const nextHeight=entering.getBoundingClientRect().height;
        if(Math.abs(panelHeight-nextHeight)<=.5)return;
        panelHeight=nextHeight;
        easeHeight(heightMotion?area.getBoundingClientRect().height:naturalHeight,nextHeight);
      });
      observer.observe(entering);
    }
    for(const m of enterMotions)m.finished.then(settle,()=>{});
    if(!leavingEntries.length&&!enterMotions.length&&!heightMotion)done();
  }
  function show(next,focus=false){
    const nav=document.querySelector('.settings-nav');if(!nav)return;
    const tabs=[...nav.querySelectorAll('[data-settings-page]')];
    const target=tabs.find(tab=>tab.dataset.settingsPage===next)||tabs[0];if(!target)return;
    const previous=tabs.find(tab=>tab.getAttribute('aria-selected')==='true');
    if(previous===target){if(focus)target.focus({preventScroll:true});return;}
    const area=nav.parentElement?.querySelector('.settings-pages');
    const leaving=previous?document.getElementById(previous.getAttribute('aria-controls')):null;
    const entering=document.getElementById(target.getAttribute('aria-controls'));
    const canAnimate=Boolean(leaving&&entering&&area?.animate&&!reducedMotion());
    // Sample the currently painted height and live opacities before cancelling an active swap.
    const from=canAnimate?area.getBoundingClientRect().height:0;
    const leavingSnapshots=[];
    let enterSnapshot=null;
    if(canAnimate){
      const candidates=new Set([leaving,...(swapState?.leavingPanels||[])]);
      for(const panel of candidates){
        if(!panel)continue;
        const snap=samplePanel(panel);
        if(!snap)continue;
        if(panel===entering)enterSnapshot=snap;
        else leavingSnapshots.push(snap);
      }
    }
    const keepVisible=canAnimate?new Set([entering,...leavingSnapshots.map(s=>s.panel)]):null;
    swapCleanup?.(keepVisible);
    const dir=previous&&tabs.indexOf(target)<tabs.indexOf(previous)?-1:1;
    page=target.dataset.settingsPage;
    for(const tab of tabs){
      const on=tab===target;
      if(tab.getAttribute('aria-selected')!==String(on))tab.setAttribute('aria-selected',String(on));
      tab.tabIndex=on?0:-1;
      const panel=document.getElementById(tab.getAttribute('aria-controls'));
      if(!panel)continue;
      const stayVisible=on||Boolean(keepVisible?.has(panel));
      if(panel.hidden===stayVisible)panel.hidden=!stayVisible;
    }
    activate();
    if(canAnimate)animateSwap(area,leavingSnapshots,entering,from,dir,enterSnapshot);
    if(focus)target.focus({preventScroll:true});
  }
  function open(requested){
    swapCleanup?.();
    const list=pages();
    if(requested&&list.some(p=>p.id===requested))page=requested;
    if(!list.some(p=>p.id===page))page=list[0].id;
    const nav=list.map(p=>`<button type="button" role="tab" id="settings-tab-${p.id}" aria-controls="settings-page-${p.id}" aria-selected="${p.id===page}" tabindex="${p.id===page?0:-1}" data-settings-page="${p.id}"><span>${esc(p.label)}</span><small data-scope="${p.scope}">${p.scope==='project'?'工程':'全局'}</small></button>`).join('');
    const panels=list.map(p=>`<div class="settings-page" role="tabpanel" id="settings-page-${p.id}" aria-labelledby="settings-tab-${p.id}" tabindex="-1"${p.id===page?'':' hidden'}>${p.id===page?RENDER[p.id]():''}</div>`).join('');
    const hint=sessionOpen()?'':'<p class="settings-nav-note">工程设置在打开项目后显示。</p>';
    modal('设置',`<div class="settings-layout"><nav class="settings-nav" role="tablist" aria-orientation="vertical" aria-label="设置分类">${nav}${hint}</nav><div class="settings-pages">${panels}</div></div>
      <p id="preferences-status" class="preferences-status" role="status" aria-live="polite">${pending?'正在保存…':'应用全局设置保存在这台电脑，不改变音乐项目。'}</p>`,`${button('完成','closeDialog',{style:'primary'})}`,'app-preferences');
    activate();
  }
  document.addEventListener('click',event=>{
    const tab=event.target.closest?.('.settings-nav [data-settings-page]');
    if(tab){show(tab.dataset.settingsPage);return;}
    const view=event.target.closest?.('#settings-page-plugins button[data-plugin-view]');
    if(view){pluginView=view.dataset.pluginView;for(const item of view.parentElement.children)item.setAttribute('aria-pressed',String(item===view));pluginPaint();return;}
    if(event.target.id==='plugin-manager-refresh'){pluginRequest('refresh');return;}
    if(event.target.closest?.('[data-do="preferencesReset"]')){event.preventDefault();event.stopImmediatePropagation();save({...defaults});}
  });
  // Vertical tablist: arrows/Home/End move selection; content switch is immediate.
  document.addEventListener('keydown',event=>{
    const tab=event.target.closest?.('.settings-nav [data-settings-page]');if(!tab||event.altKey||event.ctrlKey||event.metaKey)return;
    const tabs=[...tab.parentElement.querySelectorAll('[data-settings-page]')],index=tabs.indexOf(tab);
    const next=({ArrowDown:index+1,ArrowRight:index+1,ArrowUp:index-1,ArrowLeft:index-1,Home:0,End:tabs.length-1})[event.key];
    if(next===undefined)return;
    event.preventDefault();event.stopPropagation();
    show(tabs[(next+tabs.length)%tabs.length].dataset.settingsPage,true);
  });
  document.addEventListener('input',event=>{if(event.target.id==='plugin-manager-filter'){pluginFilter=event.target.value;pluginPaint();}});
  document.addEventListener('change',event=>{
    if(event.target.dataset?.backupPreference){saveBackup(event.target);return;}
    const key=event.target.dataset?.uiPreference;
    if(key)save({[key]:event.target.type==='checkbox'?event.target.checked:event.target.value});
  });
  return {open,hydrate,save,values:()=>({...current}),page:()=>page};
})();
