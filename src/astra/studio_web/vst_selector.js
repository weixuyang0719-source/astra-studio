(() => {
  'use strict';
  const norm=s=>String(s??'').normalize('NFKC').trim().toLowerCase();
  const groups=(rows,mode,query='')=>{
    const found=new Map();
    const words=norm(query).split(/\s+/).filter(Boolean);
    for(const row of rows??[]){
      if(typeof row?.key!=='string'||!row.key||typeof row.name!=='string'||!row.name)continue;
      if(!words.every(word=>norm(row.name+' '+(row.category??'')).includes(word)))continue;
      const label=String(row[mode]??'').trim()||(mode==='vendor'?'未知厂商':'未分类');
      if(!found.has(label))found.set(label,[]);
      found.get(label).push(row);
    }
    return [...found].sort(([a],[b])=>a.localeCompare(b,'zh-CN'));
  };
  // Display-only fallback before the backend has returned any canonical rack.
  const DEFAULT_SLOTS=['slot-1','slot-2','slot-3','slot-4'];
  // Returned rows are authoritative: keep every ID, order and empty row.
  const insertRows=inserts=>Array.isArray(inserts)&&inserts.length
    ?inserts.filter(row=>typeof row?.slot_id==='string'&&row.slot_id)
    :DEFAULT_SLOTS.map((slot_id,index)=>({slot_id,index,key:null,bypassed:false,missing:false,editor_available:false,status:'default'}));
  // Catalog rows as returned: no invented category/vendor, no bundle-name grouping.
  const effectRows=(catalog,query='')=>{
    const words=norm(query).split(/\s+/).filter(Boolean);
    return (Array.isArray(catalog)?catalog:[])
      .filter(row=>typeof row?.key==='string'&&row.key&&typeof row.name==='string'&&row.name)
      .filter(row=>words.every(word=>norm([row.name,row.vendor,row.category].filter(Boolean).join(' ')).includes(word)))
      .sort((a,b)=>a.name.localeCompare(b.name,'zh-CN'));
  };
  if(typeof module!=='undefined'&&module.exports){module.exports={groups,insertRows,effectRows,DEFAULT_SLOTS};return;}
  const SVG_NS='http://www.w3.org/2000/svg';
  const ICONS={
    piano:[['rect',{x:1.5,y:3,width:13,height:10,rx:1.5,fill:'none',stroke:'currentColor','stroke-width':1.2}],
      ['path',{d:'M4.75 8.5V13M8 8.5V13M11.25 3V13',fill:'none',stroke:'currentColor','stroke-width':1}],
      ['path',{d:'M3.85 3h1.8v5.5h-1.8zM7.1 3h1.8v5.5h-1.8z',fill:'currentColor'}]],
    bypass:[['path',{d:'M8 2.5v5',fill:'none',stroke:'currentColor','stroke-width':1.4,'stroke-linecap':'round'}],
      ['path',{d:'M4.6 4.6a4.8 4.8 0 1 0 6.8 0',fill:'none',stroke:'currentColor','stroke-width':1.4,'stroke-linecap':'round'}]],
    editor:[['rect',{x:2,y:3,width:12,height:10,rx:1.5,fill:'none',stroke:'currentColor','stroke-width':1.2}],
      ['path',{d:'M2 6h12M5.5 8.5v2.5M8 8v3M10.5 9v2',fill:'none',stroke:'currentColor','stroke-width':1.1,'stroke-linecap':'round'}]]
  };
  const icon=name=>{
    const svg=document.createElementNS(SVG_NS,'svg');
    svg.setAttribute('viewBox','0 0 16 16');svg.setAttribute('aria-hidden','true');svg.setAttribute('focusable','false');
    for(const [tag,attrs] of ICONS[name]){
      const node=document.createElementNS(SVG_NS,tag);
      for(const [k,v] of Object.entries(attrs))node.setAttribute(k,String(v));
      svg.append(node);
    }
    return svg;
  };
  let view=null;
  function sync(slot,state,context,send){
    if(!slot||!context||!send){view?.close();view=null;return;}
    const scope=JSON.stringify([context.project,context.generation]);
    if(view?.slot!==slot||view?.scope!==scope){
      view?.close();view=build(slot,scope,send);
    }
    if(context.revision!=null&&context.revision===view.revision)return;
    view.revision=context.revision;
    view.update(state??{});
  }
  function build(slot,scope,send){
    const el=(tag,cls,text)=>{
      const node=document.createElement(tag);
      if(cls)node.className=cls;
      if(text!==undefined)node.textContent=text;
      return node;
    };
    const btn=(label,cls)=>{const b=el('button',cls,label);b.type='button';return b;};
    const setText=(node,text)=>{if(node.textContent!==text)node.textContent=text;};
    const setAttr=(node,name,value)=>{if(node.getAttribute(name)!==value)node.setAttribute(name,value);};
    // Anchored fixed popover (shared AstraPopup primitive): outside pointer
    // closes, Escape closes and returns focus, no exit wait; 300px, flips up
    // when fewer than 180px remain below. Arrow keys move between choices.
    const popover=(box,name,entry)=>{
      const pop=window.AstraPopup.anchored(box,{name,width:300,minBelow:180});
      window.AstraPopup.roving(box,'button.vst-slot-choice',entry);
      return pop;
    };

    // Instrument row: name selector plus the independent current-instance panel button.
    const trigger=btn('', 'vst-slot-trigger');
    const label=el('span','vst-slot-name'), arrow=el('span','vst-slot-arrow','▾');
    arrow.setAttribute('aria-hidden','true');
    trigger.append(label,arrow);
    trigger.setAttribute('aria-haspopup','dialog');
    trigger.setAttribute('aria-expanded','false');
    trigger.setAttribute('aria-controls','vst-slot-picker');
    const piano=btn('','vst-slot-panel');
    piano.append(icon('piano'));piano.setAttribute('aria-label','打开乐器面板');
    const head=el('div','vst-slot-row');
    head.append(el('span','vst-slot-caption','音源'),trigger,piano);
    const picker=el('div','vst-slot-picker');
    picker.id='vst-slot-picker';picker.setAttribute('role','dialog');picker.setAttribute('aria-label','选择 VST 乐器');
    picker.hidden=true;
    const search=el('input','control vst-slot-search');
    search.type='search';search.placeholder='搜索名称或类别';search.setAttribute('aria-label',search.placeholder);
    const sort=el('div','segments vst-slot-sort');sort.setAttribute('role','group');sort.setAttribute('aria-label','乐器分组');
    const byCategory=btn('按类别'),byVendor=btn('按厂商');sort.append(byCategory,byVendor);
    const none=btn('无乐器','quiet vst-slot-choice vst-slot-none');none.dataset.vstNone='';
    const list=el('div','vst-slot-groups'), menuActions=el('div','vst-slot-actions');
    const panel=btn('打开乐器面板','quiet');
    const savePatch=btn('设为本机初始音色','quiet');
    menuActions.append(panel,savePatch);
    const status=el('p','vst-slot-status');
    status.setAttribute('role','status');status.hidden=true;
    picker.append(search,sort,none,list,menuActions);

    // Fixed insert rows. Rows are keyed by canonical slot_id and patched in place.
    const fx=el('section','vst-inserts');fx.setAttribute('aria-label','插入效果器');
    const fxHeader=el('div','vst-inserts-head'),fxDetails=el('div','vst-inserts-details');
    fxDetails.id='vst-inserts-details';
    const disclose=btn('','vst-inserts-fold');
    disclose.setAttribute('aria-controls',fxDetails.id);disclose.setAttribute('aria-expanded','false');
    const chevron=el('span','vst-inserts-chevron'),fxCount=el('span','vst-inserts-count');
    chevron.setAttribute('aria-hidden','true');
    disclose.append(chevron,el('span','','插入效果'),fxCount);
    const add=btn('+ 添加','vst-inserts-add');
    add.setAttribute('aria-label','添加效果器');add.setAttribute('aria-haspopup','dialog');
    add.setAttribute('aria-controls','vst-insert-picker');add.setAttribute('aria-expanded','false');
    fxHeader.append(disclose,add);
    const fxList=el('div','vst-insert-list');
    const fxLatency=el('p','vst-insert-latency'),fxNote=el('p','vst-insert-note');
    fxLatency.hidden=fxNote.hidden=true;
    fxDetails.append(fxList,fxLatency,fxNote);fx.append(fxHeader,fxDetails);
    const fxPicker=el('div','vst-slot-picker vst-insert-picker');
    fxPicker.id='vst-insert-picker';fxPicker.setAttribute('role','dialog');fxPicker.setAttribute('aria-label','选择插入效果器');
    fxPicker.hidden=true;
    const fxSearch=el('input','control vst-slot-search');
    fxSearch.type='search';fxSearch.placeholder='搜索效果器';fxSearch.setAttribute('aria-label',fxSearch.placeholder);
    const fxNone=btn('无效果器','quiet vst-slot-choice vst-insert-choice vst-insert-none');fxNone.dataset.effectNone='';
    const fxChoices=el('div','vst-insert-choices');
    const preference=el('label','vst-fx-preference');
    const preferenceName=el('span','','提前准备'),preferenceControl=el('select','control');
    preference.title='乐器就绪后在后台加载一个常用效果器，首次插入时使用。准备期间占用插件内存。';
    preferenceControl.setAttribute('aria-label','提前准备的常用效果器');
    const preferenceStatus=el('span','vst-insert-meta');
    preferenceStatus.setAttribute('aria-live','polite');
    preference.append(preferenceName,preferenceControl,preferenceStatus);
    preferenceControl.onchange=()=>send('choose_preferred_effect',preferenceControl.value);
    let preferenceOptions='';
    fxPicker.append(fxSearch,preference,fxNone,fxChoices);

    const fader=slot.querySelector('#editor-instrument-fader');
    // One compact native rack; pickers stay outside so anchoring is unchanged.
    const rack=el('details','vst-rack');rack.open=true;
    const rackTitle=el('summary','vst-rack-title','乐器与效果');
    rack.append(rackTitle,head,...(fader?[fader]:[]),status,fx);
    rack.addEventListener('toggle',()=>{if(!rack.open)closeAll();});
    slot.replaceChildren(rack,picker,fxPicker);
    let current={},mode='category',signature='',fxSignature='',fxTarget=null,listed=false;
    let showAll=false,firstEmpty=null;
    const folds=new Map(),rows=new Map();
    const inst=popover(picker,'vst-instrument',search),fxPop=popover(fxPicker,'vst-insert',fxSearch);
    const busy=()=>Boolean(current.busy);
    let catalogSource,byEffectKey=new Map();
    const catalogMap=()=>{
      if(catalogSource!==current.effect_catalog){
        catalogSource=current.effect_catalog;
        byEffectKey=new Map((catalogSource??[]).map(row=>[row.key,row]));
      }
      return byEffectKey;
    };
    function preparing(name){
      setText(status,`正在准备 ${name}…`);
      status.hidden=false;
    }

    function close(focus=false){inst.hide(focus);}
    function closeAll(){inst.hide();fxPop.hide();fxTarget=null;}
    function draw(){
      const rowsIn=Array.isArray(current.catalog)?current.catalog:[];
      const waiting=!rowsIn.length&&(current.catalog_loading===true||!Array.isArray(current.catalog));
      const next=JSON.stringify([mode,search.value,current.key,Boolean(current.mounted),busy(),waiting,rowsIn]);
      if(next===signature)return;
      signature=next;
      const focus=document.activeElement?.dataset?.vstKey;
      byCategory.setAttribute('aria-pressed',String(mode==='category'));
      byVendor.setAttribute('aria-pressed',String(mode==='vendor'));
      none.setAttribute('aria-pressed',String(!current.mounted));
      none.disabled=busy();
      const nodes=[];
      for(const [group,entries] of groups(rowsIn,mode,search.value)){
        const d=el('details','vst-slot-group'),fold=JSON.stringify([mode,group]);
        d.open=search.value.trim()!==''||folds.get(fold)!==false;
        const s=el('summary','',group);d.append(s);
        s.addEventListener('click',()=>folds.set(fold,!d.open));
        s.addEventListener('keydown',e=>{if(e.key==='Enter'||e.key===' ')folds.set(fold,!d.open);});
        for(const row of entries){
          const b=btn(row.name,'quiet vst-slot-choice');b.dataset.vstKey=row.key;
          b.setAttribute('aria-pressed',String(current.mounted&&current.key===row.key));
          b.disabled=busy();
          b.onclick=()=>{if(!(current.mounted&&current.key===row.key)){preparing(row.name);act('mount',row.key);}else close(true);};
          d.append(b);
        }
        nodes.push(d);
      }
      if(!nodes.length)nodes.push(el('p','vst-slot-empty',search.value.trim()?'没有匹配的乐器':waiting?'正在读取乐器列表…':'没有可用的 VST 乐器'));
      list.replaceChildren(...nodes);
      if(focus){
        const target=[...list.querySelectorAll('button')].find(b=>b.dataset.vstKey===focus&&!b.disabled);
        (target??search).focus({preventScroll:true});
      }
    }
    function act(action,key){close(true);send(action,key);}

    function makeRow(id){
      const row=el('div','vst-insert');row.dataset.slotId=id;
      const num=el('span','vst-insert-index');num.setAttribute('aria-hidden','true');
      const pick=btn('','vst-insert-trigger');
      pick.setAttribute('aria-haspopup','dialog');pick.setAttribute('aria-expanded','false');pick.setAttribute('aria-controls','vst-insert-picker');
      const name=el('span','vst-insert-name'),tag=el('span','vst-insert-tag');tag.hidden=true;
      pick.append(name,tag);
      const bypass=btn('','vst-insert-bypass');const bypassText=el('span','vst-insert-bypass-text');bypassText.textContent='生效';bypass.append(bypassText);bypass.setAttribute('aria-label','绕过');
      const editor=btn('','vst-insert-editor');editor.append(icon('editor'));editor.setAttribute('aria-label','打开效果器面板');
      row.append(num,pick,bypass,editor);
      const item={id,row,num,pick,name,tag,bypass,editor,data:null};
      pick.onclick=()=>openFx(item,pick);
      // enabled=true means bypassed; the row only changes when the backend reports it.
      bypass.onclick=()=>{const d=item.data;if(d?.key!=null)send('insert_bypass',undefined,{slot_id:id,enabled:!d.bypassed});};
      editor.onclick=()=>{const d=item.data;if(!editor.disabled&&d?.editor_available===true)send('insert_editor',d.key,{slot_id:id,enabled:d.editor_open!==true,editor_route:current.editor_route,rack_revision:d.rack_revision});};
      return item;
    }
    function paintRow(item,d,position,catalog){
      item.data=d;
      const key=d.key??null,entry=key==null?null:catalog.get(key);
      const missing=d.missing===true,unavailable=key!=null&&!missing&&entry!=null&&entry.available!==true;
      const preparing=d.status==='preparing',bypassed=key!=null&&d.bypassed===true;
      const text=key==null?'空':(entry?.name||key);
      const stateName=key==null?'empty':missing?'missing':unavailable?'unavailable':String(d.status||'dormant');
      if(item.row.dataset.state!==stateName)item.row.dataset.state=stateName;
      item.row.classList.toggle('bypassed',bypassed);
      setText(item.num,String(position+1));
      setText(item.name,text);
      const tagText=missing?'缺失':unavailable?'不可用':preparing?'准备中':'';
      setText(item.tag,tagText);item.tag.hidden=!tagText;
      item.tag.classList.toggle('warn',missing||unavailable);
      const reason=missing?'插件缺失：可替换或清除此槽位':unavailable?(entry.error||'效果器不可用：可替换或清除此槽位'):'';
      item.pick.title=key==null?`插入 ${position+1}：选择效果器`:`插入 ${position+1}：${text}${reason?' · '+reason:''}`;
      setAttr(item.pick,'aria-label',item.pick.title);
      item.bypass.disabled=key==null;
      setAttr(item.bypass,'aria-pressed',String(bypassed));
      setText(item.bypass.firstChild,bypassed?'绕过':'生效');
      item.bypass.title=key==null?'空槽位':bypassed?'已绕过，点击恢复':'绕过此效果器';
      const editable=d.editor_available===true;
      const opened=editable&&d.editor_open===true,pending=d.editor_pending===true;
      item.editor.disabled=!editable||pending||busy();
      setAttr(item.editor,'aria-pressed',String(opened));
      setAttr(item.editor,'aria-busy',String(pending));
      item.editor.title=editable?(d.editor_error||(opened?'关闭效果器面板':'打开效果器面板')):key==null?'空槽位':missing?'插件缺失，无法打开面板':(current.insert_editor_reason||'效果器面板暂不可用');
      setAttr(item.editor,'aria-label',item.editor.title);
    }
    function openFx(item,anchor){
      if(!item)return;
      if(fxPop.anchor===anchor){fxPop.hide(true);fxTarget=null;return;}
      // The shortcut uses the first empty canonical slot; the normal row picker
      // continues to address the exact slot even when the rack is folded.
      inst.hide();fxTarget=item;fxSearch.value='';
      fxPop.show(anchor);drawFx();fxPop.place();fxSearch.focus({preventScroll:true});
      send('list_effects');
    }
    function updateInserts(){
      const data=insertRows(current.inserts),catalog=catalogMap(),ids=data.map(d=>d.slot_id);
      for(const [id,item] of rows)if(!ids.includes(id)){
        const restore=item.row.contains(document.activeElement)||fxTarget===item&&Boolean(fxPop.anchor);
        if(fxTarget===item){fxPop.hide();fxTarget=null;}
        if(restore)disclose.focus({preventScroll:true});
        rows.delete(id);
      }
      data.forEach((d,i)=>{if(!rows.has(d.slot_id))rows.set(d.slot_id,makeRow(d.slot_id));paintRow(rows.get(d.slot_id),d,i,catalog);});
      const want=ids.map(id=>rows.get(id).row),have=fxList.children;
      if(want.length!==have.length||want.some((row,i)=>have[i]!==row)){
        const focus=fxList.contains(document.activeElement)?document.activeElement:null;
        fxList.replaceChildren(...want);
        if(focus?.isConnected&&document.activeElement!==focus)focus.focus({preventScroll:true});
      }
      const occupied=data.filter(d=>d.key!=null).length;
      const empty=data.find(d=>d.key==null);
      firstEmpty=empty?rows.get(empty.slot_id):null;
      setText(fxCount,`${occupied}/${data.length}`);
      setAttr(disclose,'aria-expanded',String(showAll));
      disclose.title=showAll?'收起空插槽和详情':'显示全部插槽和详情';
      if(add.disabled===Boolean(firstEmpty))add.disabled=!firstEmpty;
      add.title=firstEmpty?'在第一个空插槽中添加效果器':'插槽已满，点击已有的效果器可替换';
      for(const d of data){
        const item=rows.get(d.slot_id),hidden=!showAll&&d.key==null;
        if(hidden&&item.row.contains(document.activeElement))disclose.focus({preventScroll:true});
        if(item.row.hidden!==hidden)item.row.hidden=hidden;
      }
      const listHidden=!showAll&&occupied===0;
      if(fxList.hidden!==listHidden)fxList.hidden=listHidden;
      if(fxPop.anchor&&(fxPop.anchor===add?fxTarget?.data?.key!=null:fxTarget?.row.hidden)){
        fxPop.hide();fxTarget=null;
        (add.disabled?disclose:add).focus({preventScroll:true});
      }
      const latency=current.insert_latency_samples;
      const latencyText=Number.isFinite(latency)?`效果链延迟 ${latency} 样本 · ${current.insert_latency_compensated===true?'已补偿':'未补偿'}`:'';
      setText(fxLatency,latencyText);fxLatency.hidden=!showAll||!latencyText;
      const reason=typeof current.insert_editor_reason==='string'?current.insert_editor_reason:'';
      const noteText=reason&&data.some(d=>d.key!=null&&d.editor_available!==true&&d.missing!==true)?'效果器面板：'+reason:'';
      setText(fxNote,noteText);fxNote.hidden=!showAll||!noteText;
      // Saved rows carry keys only; read the trusted catalog once per scope for their names.
      if(!listed&&!catalog.size&&data.some(d=>d.key!=null)){listed=true;send('list_inventory');}
    }
    function drawFx(){
      const item=fxTarget;if(!item)return;
      const options=(current.effect_catalog||[]).filter(row=>row.available);
      const optionsKey=JSON.stringify(options.map(row=>[row.key,row.name]));
      if(optionsKey!==preferenceOptions){
        preferenceOptions=optionsKey;
        preferenceControl.replaceChildren(...[['','关闭'],...options.map(row=>[row.key,row.name])].map(([key,name])=>{
          const option=el('option','',name);option.value=key;return option;
        }));
      }
      preferenceControl.value=current.preferred_effect||'';
      const prep=current.preferred_status?.key===current.preferred_effect?current.preferred_status:null;
      const mounted=current.inserts?.some(row=>row.key===current.preferred_effect);
      setText(preferenceStatus,!current.preferred_effect?'':mounted?'已挂载':prep?.phase==='ready'?'已准备':
        prep?.phase==='loading'?'准备中…':prep?.phase==='failed'?'准备失败':'等待乐器');
      preferenceStatus.title=prep?.error||'';
      const d=item.data||{},slotKey=d.key??null,rowsIn=effectRows(current.effect_catalog,fxSearch.value);
      const waiting=!rowsIn.length&&current.catalog_loading===true;
      const next=JSON.stringify([item.id,slotKey,d.missing===true,fxSearch.value,waiting,rowsIn]);
      if(next===fxSignature)return;
      fxSignature=next;
      const focus=document.activeElement?.dataset?.effectKey;
      fxNone.setAttribute('aria-pressed',String(slotKey==null));
      fxNone.disabled=slotKey==null;
      const nodes=rowsIn.map(row=>{
        const usable=row.available===true;
        const b=btn('','quiet vst-slot-choice vst-insert-choice');b.dataset.effectKey=row.key;
        b.append(el('span','vst-insert-choice-name',row.name));
        const meta=[row.vendor,row.category].filter(v=>typeof v==='string'&&v.trim()).join(' · ');
        if(meta)b.append(el('span','vst-insert-meta',meta));
        if(!usable)b.append(el('span','vst-insert-why',row.error||'不可用'));
        b.setAttribute('aria-pressed',String(slotKey===row.key&&d.missing!==true));
        b.disabled=!usable;
        b.onclick=()=>{
          if(!usable)return;
          if(slotKey===row.key&&d.missing!==true){fxPop.hide(true);fxTarget=null;return;}
          actFx('insert_add',row.key);
        };
        return b;
      });
      if(!nodes.length)nodes.push(el('p','vst-slot-empty',fxSearch.value.trim()?'没有匹配的效果器':waiting?'正在读取效果器列表…':'没有已配置的效果器'));
      fxChoices.replaceChildren(...nodes);
      if(focus){
        const target=[...fxChoices.querySelectorAll('button')].find(b=>b.dataset.effectKey===focus&&!b.disabled);
        (target??fxSearch).focus({preventScroll:true});
      }
    }
    function actFx(action,key){
      const item=fxTarget;if(!item)return;
      if(action==='insert_add')preparing(catalogMap().get(key)?.name??key);
      fxPop.hide(true);fxTarget=null;
      send(action,key,{slot_id:item.id});
    }

    function update(state){
      current=state;
      const text=state.mounted?(state.name||state.key||'已挂载'):'选择音源…';
      setText(label,text);
      trigger.title=text;
      setAttr(trigger,'aria-label',state.mounted?`更换 VST 乐器，当前：${text}`:'选择 VST 乐器');
      const ready=Boolean(state.mounted&&state.ready);
      panel.disabled=piano.disabled=!ready||busy();
      savePatch.disabled=!state.can_save_patch||busy();
      savePatch.title=savePatch.disabled?'暂停后可保存当前音色':'新挂载此乐器时载入当前音色。已有工程优先使用工程音色。';
      const open=Boolean(ready&&state.editor_open===true);
      const pending=state.editor_pending===true;
      piano.classList.toggle('is-live',open);
      setAttr(piano,'aria-label',open?'关闭乐器面板':'打开乐器面板');
      setAttr(piano,'aria-pressed',String(open));
      setAttr(piano,'aria-busy',String(pending));
      setText(panel,open?'关闭乐器面板':'打开乐器面板');
      piano.title=!state.mounted?'未挂载乐器，无法打开面板':!state.ready?'乐器尚未就绪：请选择 MIDI 播放并等待乐器就绪':busy()?'正在切换乐器…':open?'关闭乐器面板':'打开乐器面板';
      menuActions.hidden=!state.mounted;
      const message=state.preparing_name?`正在准备 ${state.preparing_name}…`:state.error|| (busy()?'正在更新机架…':state.patch_notice||'');
      setText(status,message);
      status.hidden=!message;
      updateInserts();
      if(inst.anchor)draw();
      if(fxPop.anchor)drawFx();
    }
    trigger.onclick=()=>{
      if(inst.anchor){close(true);return;}
      // Shell first: show + draw from the last catalog; 'list' refreshes it
      // asynchronously (backend submits to its dispatcher). An unchanged
      // signature reuses the existing list DOM on repeat opens.
      fxPop.hide();fxTarget=null;
      inst.show(trigger);draw();inst.place();search.focus({preventScroll:true});
      send('list');
    };
    // Current route instance only; never a fresh standalone plugin.
    const panelAction=()=>current.editor_open===true?'panel_close':'panel';
    const panelRequest=()=>send(panelAction(),current.key,{editor_route:current.editor_route});
    piano.onclick=()=>{if(!piano.disabled&&current.editor_pending!==true){closeAll();panelRequest();}};
    disclose.onclick=()=>{showAll=!showAll;updateInserts();};
    add.onclick=()=>openFx(firstEmpty,add);
    search.addEventListener('input',draw);
    fxSearch.addEventListener('input',drawFx);
    byCategory.onclick=()=>{mode='category';draw();};
    byVendor.onclick=()=>{mode='vendor';draw();};
    none.onclick=()=>{if(current.mounted)act('unmount');else close(true);};
    fxNone.onclick=()=>{if(fxTarget?.data?.key!=null)actFx('insert_remove');};
    panel.onclick=()=>{if(current.editor_pending!==true){close(true);panelRequest();}};
    savePatch.onclick=()=>send('save_default_patch',current.key,{editor_route:current.editor_route});
    return {slot,scope,close:closeAll,update};
  }
  window.AstraVstSelector={sync};
})();
