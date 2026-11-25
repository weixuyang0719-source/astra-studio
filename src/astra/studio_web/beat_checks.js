"use strict";
window.AstraBeatChecks=(()=>{
  let open=false,project='',doc={marks:[],revision:0,undo:null},selected=null,busy=false,loaded=false;
  const identity=()=>state.project?.path||'';
  function sync(){const key=identity();if(key!==project){project=key;doc={marks:[],revision:0,undo:null};selected=null;loaded=false;busy=false;}if(open&&project&&!loaded&&!busy)call('read');}
  function call(op,extra={}){
    if(busy||!project||typeof bridge?.beatCheckMarks!=='function')return;
    const key=project;busy=true;
    bridge.beatCheckMarks(JSON.stringify({project:key,revision:doc.revision,op,...extra}),raw=>{
      if(key!==identity()||key!==project)return;
      busy=false;let result;try{result=JSON.parse(raw);}catch{result={ok:false,error:'标记保存未确认。'};}
      if(result.ok){doc=result;loaded=true;if(!doc.marks.some(m=>m.id===selected))selected=null;}
      else {loaded=true;AstraProjectEditor.rhythmMessage(result.error||'标记保存失败。');}
      requestRollDraw();
    });
  }
  function menu(){return `<section class="rhythm-menu-group"><button type="button" data-do="editorBeatCheckToggle" aria-pressed="${open}">拍点检查</button>${open?'<button type="button" data-do="editorBeatCheckNext">下一个标记</button><button type="button" data-do="editorBeatCheckRemove">删除所选标记</button><button type="button" data-do="editorBeatCheckClear">清空本项目标记</button><button type="button" data-do="editorBeatCheckUndo">撤销标记操作</button>':''}</section>`;}
  function markup(){sync();return `<div class="editor-map-lane beat-check-lane" style="height:26px;min-height:26px;flex-shrink:0" ${open?'':'hidden'}><span title="仅供人工检查，不改变拍点或制谱">检查</span><canvas id="beat-check-canvas" tabindex="0" aria-label="拍点检查：右键空白添加，单击标记定位，Delete 删除所选标记"></canvas></div>`;}
  function locate(mark){if(!mark)return;selected=mark.id;withPlayer('seek','midiSeek',{position:mark.seconds});const scroll=$('roll-scroll'),x=midiX(mark.seconds);if(scroll&&(x<scroll.scrollLeft||x>scroll.scrollLeft+scroll.clientWidth))scroll.scrollLeft=Math.max(0,x-scroll.clientWidth/2);const canvas=$('beat-check-canvas');if(canvas)draw(canvas.clientWidth,scroll?.scrollLeft||0);requestRollDraw();}
  function action(name){
    if(!name.startsWith('editorBeatCheck'))return false;
    sync();
    if(name==='editorBeatCheckToggle'){open=!open;AstraProjectEditor.refreshData();}
    if(name==='editorBeatCheckNext'){const index=doc.marks.findIndex(m=>m.id===selected);locate(doc.marks[(index+1)%doc.marks.length]);}
    if(name==='editorBeatCheckRemove'&&selected)call('remove',{id:selected});
    if(name==='editorBeatCheckClear')call('clear');
    if(name==='editorBeatCheckUndo')call('undo');
    return true;
  }
  function draw(width,offset){
    sync();const canvas=$('beat-check-canvas');
    if(canvas)canvas.parentElement.hidden=!open;
    if(canvas&&open){const c=canvasContext(canvas,width,26);c.clearRect(0,0,width,26);c.strokeStyle='#8592a6';for(const m of doc.marks){const x=midiX(m.seconds)-offset;if(x < -6||x>width+6)continue;c.beginPath();c.moveTo(x,5);c.lineTo(x+5,13);c.lineTo(x,21);c.lineTo(x-5,13);c.closePath();if(m.id===selected){c.fillStyle='#657ca0';c.fill();}else c.stroke();}canvas.title=busy?'正在保存标记…':`${doc.marks.length} 个检查标记；右键空白添加，单击定位`;}
    let line=$('beat-check-guide');const content=$('roll-content');const m=open&&doc.marks.find(m=>m.id===selected);
    if(m&&content){if(!line){line=document.createElement('span');line.id='beat-check-guide';line.style.cssText='position:absolute;top:0;bottom:0;width:0;border-left:1px dashed #8297b080;pointer-events:none;z-index:3';content.appendChild(line);}line.style.left=`${midiX(m.seconds)}px`;line.hidden=false;}else if(line)line.hidden=true;
  }
  function hit(event){const c=event.target;if(c?.id!=='beat-check-canvas')return null;const x=event.clientX-c.getBoundingClientRect().left,offset=$('roll-scroll')?.scrollLeft||0;const mark=doc.marks.reduce((best,m)=>Math.abs(midiX(m.seconds)-offset-x)<=7&&(!best||Math.abs(midiX(m.seconds)-offset-x)<Math.abs(midiX(best.seconds)-offset-x))?m:best,null);return {mark,seconds:midiSeconds(x+offset)};}
  document.addEventListener('pointerdown',event=>{const h=hit(event);if(!h)return;event.stopImmediatePropagation();event.target.focus();if(event.button===0&&h.mark)locate(h.mark);},true);
  document.addEventListener('contextmenu',event=>{const h=hit(event);if(!h)return;event.preventDefault();event.stopImmediatePropagation();if(h.mark){selected=h.mark.id;requestRollDraw();}else if(Number.isFinite(h.seconds)&&h.seconds>=0)call('add',{seconds:h.seconds});},true);
  document.addEventListener('keydown',event=>{if(event.target?.id!=='beat-check-canvas')return;if(event.key==='Delete'||event.key==='Backspace'){event.preventDefault();event.stopImmediatePropagation();action('editorBeatCheckRemove');}},true);
  return {menu,markup,draw,action};
})();
