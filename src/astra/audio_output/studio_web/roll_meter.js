"use strict";

// A single export-bound preview. Playback and note selection remain available.
window.AstraRollMeter = (() => {
  let key="", enabled=false, selected=null, mode="opening", numerator="", draft=null;
  let serial=0, pending=false, submitting=false, notice="", lastCanvas=null, lastGeometry="";
  function sync() {
    const next=JSON.stringify([state.project?.export,state.midi.path]);
    if(key!==next){key=next;serial++;selected=draft=null;pending=submitting=false;notice="";}
    if(submitting && state.job.error && !state.job.running){submitting=false;notice=state.job.error;}
  }
  function available(){return Boolean(state.project?.tempo && state.midi.path && !state.midi.loading && !state.job.running && !state.gpu_preflight);}
  function panel(){
    sync();
    return `<section id="roll-meter-panel" class="roll-meter-panel${enabled?' active':''}" aria-label="卷帘拍位修正">${content()}</section>`;
  }
  function content(){
    const lock=!available()||pending||submitting;
    if(!enabled)return `${button("修正拍位","rollMeterToggle",{symbol:"clock",disabled:!available()})}<span>点音符或标尺，直接校正引子和小节线</span>`;
    return `<div class="roll-meter-controls"><strong>修正拍位</strong><label>修正方式<select id="roll-meter-mode" class="control" ${lock?'disabled':''}><option value="opening" ${mode==='opening'?'selected':''}>曲首引子：从这里对齐小节</option><option value="bar" ${mode==='bar'?'selected':''}>局部小节线 / 变拍</option></select></label><label>拍号<select id="roll-meter-numerator" class="control" ${lock?'disabled':''}><option value="">沿用当前</option>${[2,3,4].map(n=>`<option value="${n}" ${String(n)===numerator?'selected':''}>${n}/4</option>`).join('')}</select></label>${button("应用并更新谱面","rollMeterApply",{style:"primary",disabled:lock||!draft})}${button("撤销预览","rollMeterUndo",{disabled:pending||submitting||!selected})}${button("收起","rollMeterToggle",{disabled:pending||submitting})}</div><p class="roll-meter-status" role="status">${html(pending?'正在预览小节线…':submitting?'正在更新 MIDI、谱面和已有和弦…':notice||'点击作为小节第一拍的音符，自动吸附音符起点；也可点击上方标尺。绿色小节线是预览，应用前不改文件。')}</p>`;
  }
  function refresh(){const el=document.getElementById('roll-meter-panel');if(el){el.classList.toggle('active',enabled);el.innerHTML=content();}requestRollDraw();}
  function pick(note,seconds){
    if(!enabled||!available()||submitting)return false;
    selected={seconds:note?note.start:seconds,beat:note?.start_beat??secondsToBeat(seconds),pitch:note?.pitch};
    preview();return true;
  }
  function preview(){
    draft=null;notice="";
    if(!selected){refresh();return;}
    const token=++serial,base=state.project.export,path=state.midi.path;
    pending=true;refresh();
    bridge.previewRollMeterEdit(JSON.stringify({export:base,midi:path,beat:selected.beat,mode,numerator:numerator?Number(numerator):null}),raw=>{
      if(token!==serial||base!==state.project?.export||path!==state.midi.path)return;
      pending=false;let result;
      try{result=JSON.parse(raw);}catch{result={error:'无法读取修正预览。'};}
      if(result.error)notice=result.error;
      else{draft=result.data;notice=`已选 ${selected.seconds.toFixed(3)} 秒${mode==='opening'?'：前方音符保留，小节从所选音重新对齐':'：设为小节第一拍'}。${result.edit.numerator}/${result.edit.denominator}，后续变速保留；尚未应用。`;}
      refresh();
    });
  }
  function action(name){
    sync();
    if(name==='rollMeterToggle'){if(pending||submitting)return;enabled=!enabled;serial++;draft=selected=null;notice="";refresh();}
    if(name==='rollMeterUndo'){serial++;pending=false;draft=selected=null;notice="已撤销预览，文件未改。";refresh();}
    if(name==='rollMeterApply'&&draft&&available()&&!pending&&!submitting){
      submitting=true;refresh();
      dispatch('saveTempo',{export:state.project.export,data:draft},()=>{sync();refresh();});
    }
  }
  function changed(id,value){
    if(id==='roll-meter-mode')mode=value;
    else if(id==='roll-meter-numerator')numerator=value;
    else return;
    preview();
  }
  function grid(line,view){
    if(!enabled||!draft||!selected||!(draft.anchors?.length>1))return false;
    // Preview bars have a draft musical clock, but are painted at their true
    // seconds through the same installed axis as notes, selection and playhead.
    const anchors=draft.anchors,axis=view?.axis??rollAxis(),offset=view?.offset??0,width=view?.width??axis.width;
    const start=interpolateRollAnchors(anchors,axis.seconds(offset-1),'seconds','beat');
    const end=interpolateRollAnchors(anchors,axis.seconds(offset+width+1),'seconds','beat');
    const project=q=>axis.x(interpolateRollAnchors(anchors,q,'beat','seconds'));
    const sections=AstraProjectEditor.meterSections(draft);let number=1,lastLabel=-Infinity;
    for(let i=0;i<sections.length;i++){
      const m=sections[i],unit=4/m.denominator,length=m.numerator*unit,stop=Math.min(sections[i+1]?.beat??Infinity,anchors.at(-1).beat);
      if(!(length>0))continue;if(m.bar!==undefined)number=m.bar;
      const count=Math.max(0,Math.ceil((stop-m.beat-1e-6)/length));
      const first=Math.max(0,Math.floor((start-m.beat)/length));
      for(let n=first;n<count;n++){
        const q=m.beat+n*length;if(q>end)break;
        const x=project(q),label=x>=offset-1&&(n===0||x>=lastLabel+65);
        if(label)lastLabel=x;
        line(x,label?(m.pickup?'引子':m.free?`${number+n} · 自由`:`${number+n}${n===0?` · ${m.displayNumerator||m.numerator}/${m.denominator}`:''}`):'');
        if(!m.free){let previous=x;for(let b=1;b<m.numerator&&q+b*unit<stop;b++){
          const bx=project(q+b*unit);if(bx-previous>23)line(bx,'',false);previous=bx;
        }}
      }
      number+=count;
    }
    return true;
  }
  function marker(ctx,ruler,offset,height){
    if(!enabled||!selected)return;
    const scroll=document.getElementById('roll-scroll');
    const geometry=rollGeometry?`${rollGeometry.high}:${rollGeometry.row}:${scroll.clientHeight}`:"";
    if((lastCanvas!==ctx.canvas||lastGeometry!==geometry) && Number.isFinite(selected.pitch) && rollGeometry){
      scroll.scrollTop=Math.max(0,(rollGeometry.high-selected.pitch)*rollGeometry.row-scroll.clientHeight/2);
    }
    lastCanvas=ctx.canvas;lastGeometry=geometry;
    const x=midiX(selected.seconds)-offset;
    ctx.save();ctx.strokeStyle=draft?'#a9b4e8':'#A6C6DB';ctx.setLineDash([5,4]);ctx.beginPath();ctx.moveTo(x,0);ctx.lineTo(x,height);ctx.stroke();ctx.restore();
    ruler.fillStyle=draft?'#a9b4e8':'#A6C6DB';ruler.fillRect(x-2,0,4,28);
  }
  return {panel,pick,action,changed,grid,marker,gridVersion:()=>serial,isPreview:()=>Boolean(enabled&&draft)};
})();
