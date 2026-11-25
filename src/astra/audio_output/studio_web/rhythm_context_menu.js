"use strict";

(() => {
  const pad=6;
  function placeMenu(x,y,width,height,vw,vh){
    const axis=(at,size,limit)=>{
      const min=Math.min(pad,Math.max(0,(limit-size)/2)),max=Math.max(min,limit-min-size);
      const forward=at+2,back=at-size-2;
      return Math.max(min,Math.min(max,forward>max&&back>=min?back:forward));
    };
    return {left:axis(x,width,vw),top:axis(y,height,vh)};
  }
  const form=(id,label,kind)=>({id,label,form:kind});
  const direct=(id,label,operation)=>({id,label,operation});
  const intent=(action,fields)=>({type:'rhythm_intent',action,...fields});
  const opening=()=>form('opening','第 1 小节起点…','opening');
  function itemsFor({scope,target:t}){
    if(t.kind==='meter'){
      const items=[form('change-meter','修改拍号…','meter')];
      if(!scope.draft)items.push(form('free-meter','自由拍号…','free_meter'));
      if(t.index>0)items.push(direct('remove-meter','删除拍号',scope.draft?{type:'delete_meter_label',seconds:t.seconds}:intent('delete_meter',{seconds:t.seconds})));
      return items;
    }
    if(scope.draft){
      if(t.kind!=='beat')return [];
      const items=[form('set-meter','设置拍号…','meter')];
      if(t.canDelete)items.push(direct(t.bar?'delete-bar':'delete-pulse',t.bar?'删除小节线':'删除此拍',{type:t.bar?'delete_bar':'delete_pulse',seconds:t.seconds}));
      return items;
    }
    if(t.kind==='opening')return [opening()];
    if(t.kind!=='beat')return [];
    const items=[];
    if(!t.terminal)items.push(form('set-meter','设置拍号…','meter'),form('free-meter','自由拍号…','free_meter'));
    if(t.points.length===2)items.push(form('divide','两点均分…','divide'));
    if(!t.terminal)items.push(opening());
    if(t.canDelete)items.push(direct('delete-beats','删除所选拍点',intent('delete_beats',{seconds:t.points.slice(),meter_resolution:'delete'})));
    return items;
  }
  // Scope and target are value snapshots. A retired handle cannot act on a replacement menu.
  const same=(a,b)=>JSON.stringify(a)===JSON.stringify(b);
  function createMenuModel(run){
    let active=null;
    return {
      current:()=>active,
      show(context){
        const items=context?itemsFor(context):[];
        return active=items.length?{context,items}:null;
      },
      close(){active=null;},
      pick(entry,id,fresh){
        if(!entry||entry!==active)return false;
        active=null;
        const item=entry.items.find(i=>i.id===id);
        if(!item||!same(entry.context,fresh))return false;
        run(item,entry.context);
        return true;
      }
    };
  }

  if(typeof module==='object'&&module.exports){module.exports={placeMenu,createMenuModel};return;}
  const menu=document.getElementById('rhythm-context-menu');
  if(!menu)return;
  let anchor=null;
  const inside=target=>Boolean(target?.nodeType&&menu.contains(target));
  const owner=stamp=>stamp.draft?window.AstraStructureEditor:window.AstraRhythmTimeline;
  function scope(){
    const structure=window.AstraStructureEditor,draft=Boolean(structure?.hasSession?.()||structure?.busy?.());
    const editStamp=state.project?.editor?.edit_stamp;
    return {project:state.project?.path,revision:state.project?.editor?.revision,midi:state.midi.path,view:tab,
      generation:editStamp?.generation??null,version:editStamp?.version??null,
      opening:Boolean(state.project_opening?.pending),draft,draftVersion:draft?structure.contextVersion?.():null};
  }
  function hit(point){
    const element=point.target;
    if(!element?.isConnected||!['ruler-canvas','beat-flags-canvas','opening-canvas'].includes(element.id))return null;
    const stamp=scope();
    if(stamp.view!=='notes'||!stamp.project||stamp.revision==null||!stamp.midi||stamp.opening)return null;
    const r=element.getBoundingClientRect();
    if(!(point.clientX>=r.left&&point.clientX<r.right&&point.clientY>=r.top&&point.clientY<r.bottom))return null;
    // A draft owns the event even when stale, busy, or without a matching object.
    const target=owner(stamp)?.contextTarget?.(point);
    return target?{scope:stamp,target}:null;
  }
  const model=createMenuModel((item,context)=>{
    close();
    if(same(context.scope,scope()))owner(context.scope)?.runContext?.(context.target,item,context.scope);
  });
  function close(restore=true){
    if(!anchor)return;
    const previous=anchor.focus,restoreFocus=restore&&inside(document.activeElement);
    anchor=null;model.close();menu.hidden=true;menu.replaceChildren();
    if(restoreFocus)(previous?.isConnected?previous:document.getElementById('roll-scroll'))?.focus({preventScroll:true});
  }
  function reconcile(){
    if(anchor&&(!anchor.target.isConnected||!same(model.current()?.context.scope,scope())))close();
  }
  window.AstraRhythmContextMenu={close,reconcile};

  window.addEventListener('contextmenu',event=>{
    close();
    if(event.defaultPrevented||window.AstraBeatRecorder?.locksRhythm?.())return;
    const point={target:event.target,clientX:event.clientX,clientY:event.clientY};
    const entry=model.show(hit(point));if(!entry)return;
    const previous=document.activeElement;
    anchor={...point,focus:previous&&previous!==document.body?previous:document.getElementById('roll-scroll')};
    menu.setAttribute('aria-label',(entry.context.scope.draft?'结构草稿 · ':'')+{meter:'拍号',beat:'拍点',opening:'开头'}[entry.context.target.kind]);
    menu.replaceChildren(...entry.items.map(item=>{
      const control=document.createElement('button');
      control.type='button';control.tabIndex=-1;control.setAttribute('role','menuitem');control.textContent=item.label;
      control.addEventListener('click',e=>{
        if(e.button!==0||model.current()!==entry)return;
        e.stopPropagation();
        if(!model.pick(entry,item.id,hit(anchor)))close();
      });
      return control;
    }));
    const vw=document.documentElement.clientWidth||window.innerWidth,vh=document.documentElement.clientHeight||window.innerHeight;
    menu.style.width=`${Math.max(0,Math.min(180,vw-2*pad))}px`;
    menu.style.maxHeight=`${Math.max(0,vh-2*pad)}px`;
    menu.hidden=false;
    const {width,height}=menu.getBoundingClientRect();
    const {left,top}=placeMenu(point.clientX,point.clientY,width,height,vw,vh);
    menu.style.left=`${left}px`;menu.style.top=`${top}px`;
    event.preventDefault();event.stopImmediatePropagation();
    menu.firstElementChild.focus({preventScroll:true});
  },true);
  for(const name of ['pointerdown','click','focusin'])window.addEventListener(name,e=>{
    if(anchor&&!inside(e.target))close(false);
  },true);
  for(const name of ['scroll','wheel'])window.addEventListener(name,e=>{
    if(anchor&&!inside(e.target))close();
  },{capture:true,passive:true});
  window.addEventListener('resize',()=>close());
  window.addEventListener('blur',()=>close(false));
  document.addEventListener('visibilitychange',()=>{if(document.hidden)close(false);});
  window.addEventListener('keydown',event=>{
    if(!anchor||!inside(event.target)||event.isComposing)return;
    const key=event.key;
    if(key==='Tab'){close();event.stopImmediatePropagation();return;} // Allow normal traversal from the restored focus.
    if(key==='Escape')close();
    else if(!event.ctrlKey&&!event.metaKey&&!event.altKey&&['ArrowUp','ArrowDown','Home','End','Enter',' '].includes(key)){
      const rows=Array.from(menu.children),i=rows.indexOf(document.activeElement);
      if(key==='Enter'||key===' '){if(!event.repeat)rows[i]?.click();}
      else rows[key==='Home'?0:key==='End'?rows.length-1:(i+(key==='ArrowDown'?1:-1)+rows.length)%rows.length]?.focus();
    }else{
      // Other editor shortcuts, including zoom, still go through their normal handlers.
      if(!['Control','Meta','Alt','Shift'].includes(key))close();
      return;
    }
    event.preventDefault();event.stopImmediatePropagation();
  },true);
})();
