"use strict";
(() => {
  let currentPath="",selectedKey="",selectedNote=null,selectionRows=null,hitCache=null;
  const noteKey=note=>JSON.stringify([currentPath,note.start,note.end,note.pitch,note.channel]);
  const number=(value,places=3)=>Number.isFinite(value)?(places?value.toFixed(places).replace(/\.?0+$/,""):value.toFixed(0))||"0":"—";
  const pitchName=p=>`${["C","C♯","D","D♯","E","F","F♯","G","G♯","A","A♯","B"][((p%12)+12)%12]}${Math.floor(p/12)-1}`;
  const readonly=()=>!window.AstraProjectEditor?.active?.();
  const reading=()=>window.AstraProjectEditor?.model?.()||state.midi;
  // Same reading structure as the editable info line (head · cells), but every
  // value is plain text: nothing here looks or behaves like an input.
  function infoLineMarkup(){
    const note=selectedNote;
    const head=`<div class="info-head" data-kind="${note?'notes':'empty'}"><span class="info-kind">${note?'音符':'未选择'}</span>${window.AstraProjectEditor?.workingView?.()?"":"<small>只读</small>"}</div>`;
    const cell=(key,label,value,unit='')=>`<div class="info-field info-read" data-info-field="${key}"><span>${label}</span><strong class="info-value">${html(value)}</strong>${unit?`<small>${unit}</small>`:''}</div>`;
    let body;
    if(note){
      const fields=[['start','起点',number(note.start),'s'],['end','终点',number(note.end),'s'],['length','长度',number(Math.max(0,note.end-note.start)),'s'],['pitch','音高',pitchName(note.pitch),''],['velocity','力度',number(note.velocity,0),'']];
      body=`<div class="info-fields">${fields.map(([key,label,value,unit])=>cell(key,label,value,unit)).join('')}</div>${cell('channel','通道',Number.isInteger(note.channel)?note.channel+1:'—')}<button type="button" class="quiet info-clear" id="note-inspector-clear" aria-label="取消音符选择" title="取消选择">×</button>`;
    }else{
      const notes=reading()?.notes||[],cc=reading()?.pedal_events||[],length=Number(reading()?.duration);
      body=cell('count','音符总数',notes.length)+cell('cc','CC64',cc.filter(c=>c.number===undefined||c.number===64).length)+(Number.isFinite(length)&&length>0?cell('duration','总时长',number(length),'s'):'');
    }
    return `<div id="editor-info-line" class="editor-info-line" data-kind="${note?'notes':'empty'}" data-readonly="true" role="region" aria-label="信息行：只读" tabindex="0">${head}${body}</div>`;
  }
  function refresh(){
    const rows=reading()?.notes||[];
    if(selectedKey&&state.midi?.path===currentPath&&selectionRows!==rows){selectedNote=rows.find(note=>noteKey(note)===selectedKey)||null;if(!selectedNote)selectedKey='';}
    selectionRows=rows;
    const line=document.getElementById('editor-info-line');
    // Patch in place (keyed by data-info-field) instead of remounting the strip.
    if(readonly()&&line&&!line.hidden){const next=window.AstraProjectEditor?.workingView?.()&&!selectedNote&&window.AstraProjectEditor?.infoLineMarkup?window.AstraProjectEditor.infoLineMarkup():infoLineMarkup();if(line.outerHTML!==next){const patch=window.AstraProjectEditor?.patchMarkup;if(typeof patch==='function')patch(line,next);else line.outerHTML=next;}}
  }
  function sync(path){if(currentPath!==String(path||'')){currentPath=String(path||'');selectedKey='';selectedNote=null;selectionRows=null;hitCache=null;}refresh();}
  function selected(note){return Boolean(selectedNote&&note===selectedNote);}
  function indexFor(notes){
    if(hitCache?.notes===notes)return hitCache;
    const pitches=new Map();notes.forEach((note,order)=>{if(![note.pitch,note.start,note.end].every(Number.isFinite))return;if(!pitches.has(note.pitch))pitches.set(note.pitch,[]);pitches.get(note.pitch).push({note,order});});
    for(const rows of pitches.values()){rows.sort((a,b)=>a.note.start-b.note.start);let end=-Infinity;for(const entry of rows)entry.maxEnd=end=Math.max(end,entry.note.end);}
    hitCache={notes,pitches};return hitCache;
  }
  function selectAt(event,{canvas,notes,high,row,offset,xForSeconds}){
    const bounds=rollPointerRect(canvas),x=event.clientX-bounds.left,y=event.clientY-bounds.top,pitch=high-Math.floor(y/row),rows=indexFor(notes).pitches.get(pitch)||[];
    // Binary-search by pixel conversion, then inspect only overlapping notes of
    // this pitch. Preserve reverse paint-order precedence for equal-time notes.
    let lo=0,hi=rows.length;while(lo<hi){const mid=(lo+hi)>>>1;if(xForSeconds(rows[mid].maxEnd)-offset<x-3)lo=mid+1;else hi=mid;}
    let hit=null;for(let i=lo;i<rows.length;i++){const entry=rows[i],left=xForSeconds(entry.note.start)-offset;if(left>x)break;const right=xForSeconds(entry.note.end)-offset;if(x<=Math.max(left+3,right)&&(!hit||entry.order>hit.order))hit=entry;}
    selectedNote=hit?.note||null;selectedKey=selectedNote?noteKey(selectedNote):'';selectionRows=reading()?.notes;refresh();return selectedNote;
  }
  document.addEventListener('click',event=>{
    if(!event.target.closest('#note-inspector-clear'))return;
    selectedKey='';selectedNote=null;refresh();window.dispatchEvent(new CustomEvent('astra-note-selection-change'));
  });
  window.AstraNoteInspector={infoLineMarkup,sync,selected,selectAt,refresh,hasSelection:()=>Boolean(selectedNote)};
})();
