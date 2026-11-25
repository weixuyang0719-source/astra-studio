"use strict";
window.AstraBeatChecks = (() => {
  const checkActions = 'editorBeatCheck';
  const autoLabels = {missing:'疑似漏拍', extra:'疑似多拍', gap:'拍距异常'};
  const newDoc = () => ({marks:[], revision:0, undo:null});
  let open=false, project='', projectEpoch=0, doc=newDoc(), busy=false, loaded=false;
  let automatic=[], scan=null, pending=false, feedback='', selected=null;

  const identity = () => state.project?.path || '';
  function scanInputs() {
    const {editor,detected_beats:evidence}=state.project || {};
    return {project:identity(), revision:editor?.revision, cacheKey:evidence?.cache_key,
      source:evidence?.source, provider:evidence?.provider, hash:evidence?.result_sha256, available:evidence?.available};
  }
  function scanIsCurrent(scope) {
    const now=scanInputs();
    return Object.keys(now).every(key=>now[key]===scope[key]);
  }
  function resetAutomatic(message='') {
    if(selected?.startsWith('auto:')) selected=null;
    automatic=[];
    scan=null;
    pending=false;
    feedback=message;
  }
  function sync() {
    const key=identity();
    if(key!==project){
      project=key;
      projectEpoch++;
      doc=newDoc();
      selected=null;
      loaded=false;
      busy=false;
      resetAutomatic();
      updateControls();
    }else if(scan&&!scanIsCurrent(scan)){
      resetAutomatic('节奏或拍点证据已更新，请重新查找。');
      updateControls();
      requestRollDraw();
    }
    if(open&&project&&!loaded&&!busy) call('read');
  }
  function call(op, extra={}) {
    if(busy||!project||typeof bridge?.beatCheckMarks!=='function') return;
    const key=project, token=projectEpoch;
    busy=true;
    updateControls();
    bridge.beatCheckMarks(JSON.stringify({project:key,revision:doc.revision,op,...extra}),raw=>{
      if(token!==projectEpoch||key!==identity()) return;
      busy=false;
      let result;
      try { result=JSON.parse(raw); }
      catch { result={ok:false,error:'标记保存未确认。'}; }
      loaded=true;
      if(result.ok){
        doc=result;
        if(selected&&!marksContain(selected)) selected=null;
      }else{
        AstraProjectEditor.rhythmMessage(result.error||'标记保存失败。');
      }
      updateControls();
      requestRollDraw();
    });
  }

  function find() {
    if(pending||!project||!AstraProjectEditor.editor()?.tempo) return;
    const scope=scanInputs();
    resetAutomatic('正在查找可疑拍点…');
    scan=scope;
    pending=true;
    open=true;
    updateControls();
    requestRollDraw();
    setTimeout(()=>{
      if(scan!==scope||!scanIsCurrent(scope)) return;
      const tempo=AstraProjectEditor.editor().tempo;
      try{
        const {points,downbeats}=AstraProjectEditor.rhythmDetectedBeats();
        const sections=AstraProjectEditor.meterSections(tempo);
        const result=window.AstraBeatDetector.scan({tempo,points,downbeats,sections});
        const unique=new Map();
        for(const mark of result.marks){
          if(!Number.isFinite(mark.seconds)||mark.seconds<0||!Object.hasOwn(autoLabels,mark.kind)) continue;
          const id='auto:'+mark.kind+':'+mark.seconds.toFixed(6);
          unique.set(id,{...mark,id});
        }
        automatic=[...unique.values()].sort((a,b)=>a.seconds-b.seconds);
        feedback=automatic.length?'找到 '+automatic.length+' 处可疑拍点。':
          result.hasEvidence?'未发现可疑拍点。':'拍点证据不足，未生成自动标记。';
      }catch(error){
        resetAutomatic('拍点检查未完成，请重试。');
        console.warn('beat check failed',error);
      }
      pending=false;
      updateControls();
      requestRollDraw();
    },0);
  }
  const marksContain = id => doc.marks.some(m=>m.id===id)||automatic.some(m=>m.id===id);
  function statusText() {
    const index=automatic.findIndex(m=>m.id===selected), mark=automatic[index];
    return mark ? (index+1)+'/'+automatic.length+' · '+autoLabels[mark.kind]+' · '+mark.seconds.toFixed(2)+' 秒' : feedback;
  }
  function canFind() {
    return Boolean(identity()&&AstraProjectEditor.editor()?.tempo)&&(!pending||project!==identity());
  }
  function findEntry() {
    return button('查找可疑拍点','editorBeatCheckFind',{style:'quiet beat-check-action',symbol:'beat',disabled:!canFind()});
  }
  function manualActions() {
    const ready=open&&loaded&&!busy&&project===identity();
    return {
      editorBeatCheckNextManual:ready&&doc.marks.length>0,
      editorBeatCheckRemove:ready&&doc.marks.some(mark=>mark.id===selected),
      editorBeatCheckUndo:ready&&Array.isArray(doc.undo),
      editorBeatCheckClear:ready&&doc.marks.length>0
    };
  }
  function inspectionMenu() {
    const enabled=manualActions();
    const row=(label,name,symbol='',destructive=false)=>button(label,name,{
      style:'quiet beat-check-action'+(destructive?' beat-check-destructive':''),symbol,disabled:!enabled[name]
    });
    return [
      '<details class="editor-command-menu beat-check-menu" data-disclosure="editor-command-beat-check">',
      '<summary><svg aria-hidden="true"><use href="#i-beat"/></svg><span>检查轨道</span></summary>',
      '<div class="editor-command-body beat-check-menu-body">',
      '<button type="button" class="quiet beat-check-action beat-check-visibility" data-do="editorBeatCheckToggle" aria-controls="beat-check-lane" aria-pressed="'+open+'">',
      '<span>显示检查轨</span><svg class="beat-check-visible-indicator" aria-hidden="true"><use href="#i-check"/></svg></button>',
      '<section class="beat-check-manual-actions" aria-label="手动标记"><strong>手动标记</strong>',
      row('下一个手动标记','editorBeatCheckNextManual','start'),
      row('删除所选标记','editorBeatCheckRemove','close'),
      row('撤销上次标记操作','editorBeatCheckUndo','undo'),
      row('清空手动标记','editorBeatCheckClear','close',true),
      '</section></div></details>',
    ].join('');
  }
  function markup() {
    sync();
    return [
      '<div id="beat-check-controls" class="beat-check-controls" '+(open&&feedback?'':'hidden')+'>',
      '<span id="editor-beat-check-status" role="status" data-count="'+automatic.length+'">'+statusText()+'</span>',
      '<div id="beat-check-navigation" role="group" aria-label="可疑拍点" '+(automatic.length?'':'hidden')+'>',
      button('上一个','editorBeatCheckPrev',{style:'quiet',label:'上一个疑点'}),
      button('下一个','editorBeatCheckNext',{style:'quiet',label:'下一个疑点'}),
      button('清除自动标记','editorBeatCheckClearAutomatic',{style:'quiet'}),
      '</div></div>',
      '<div id="beat-check-lane" class="editor-map-lane beat-check-lane" '+(open?'':'hidden')+'>',
      '<span title="仅供复核，右键空白处可添加手动标记">检查</span>',
      '<canvas id="beat-check-canvas" tabindex="0" aria-label="拍点检查：右键空白添加手动标记，单击标记定位，Delete 删除所选手动标记"></canvas>',
      '</div>',
    ].join('');
  }
  function updateControls() {
    const controls=$('beat-check-controls'), status=$('editor-beat-check-status');
    const navigation=$('beat-check-navigation'), lane=$('beat-check-lane');
    if(controls) controls.hidden=!open||!feedback;
    if(status){
      status.textContent=statusText();
      status.dataset.count=String(automatic.length);
    }
    if(navigation) navigation.hidden=!automatic.length;
    if(lane) lane.hidden=!open;
    const toggle=document.querySelector('[data-do="editorBeatCheckToggle"]');
    if(toggle) toggle.setAttribute('aria-pressed',String(open));
    const find=document.querySelector('[data-do="editorBeatCheckFind"]');
    if(find) find.disabled=!canFind();
    for(const [name,enabled] of Object.entries(manualActions())){
      const item=document.querySelector('[data-do="'+name+'"]');
      if(item) item.disabled=!enabled;
    }
  }
  function selectMark(mark) {
    if(!mark) return;
    selected=mark.id;
    updateControls();
    requestRollDraw();
  }
  function locate(mark) {
    if(!mark) return;
    selectMark(mark);
    withPlayer('seek','midiSeek',{position:mark.seconds});
    const scroll=$('roll-scroll'), x=midiX(mark.seconds);
    if(scroll&&(x<scroll.scrollLeft||x>scroll.scrollLeft+scroll.clientWidth))
      scroll.scrollLeft=Math.max(0,x-scroll.clientWidth/2);
  }
  function step(marks, direction) {
    if(!marks.length) return;
    const index=marks.findIndex(m=>m.id===selected);
    locate(marks[index<0?(direction<0?marks.length-1:0):(index+direction+marks.length)%marks.length]);
  }
  function action(name) {
    if(!name.startsWith(checkActions)) return false;
    sync();
    switch(name){
      case 'editorBeatCheckFind': find(); break;
      case 'editorBeatCheckToggle':
        open=!open;
        updateControls();
        AstraProjectEditor.refreshData();
        break;
      case 'editorBeatCheckNext': step(automatic,1); break;
      case 'editorBeatCheckPrev': step(automatic,-1); break;
      case 'editorBeatCheckClearAutomatic':
        resetAutomatic('已清除自动标记。');
        updateControls();
        requestRollDraw();
        break;
      case 'editorBeatCheckNextManual': step(doc.marks,1); break;
      case 'editorBeatCheckRemove':
        if(selected&&!selected.startsWith('auto:')) call('remove',{id:selected});
        break;
      case 'editorBeatCheckClear': call('clear'); break;
      case 'editorBeatCheckUndo': call('undo'); break;
      default: return false;
    }
    return true;
  }

  function drawMark(c, mark, x) {
    const active=mark.id===selected, kind=mark.kind;
    c.beginPath();
    if(kind==='gap'){
      c.arc(x,13,5,0,Math.PI*2);
      c.strokeStyle='#986b36';
    }else if(kind){
      const tip=kind==='missing'?6:20, base=26-tip;
      c.moveTo(x,tip);
      c.lineTo(x+6,base);
      c.lineTo(x-6,base);
      c.strokeStyle=kind==='missing'?'#b27529':'#a75269';
    }else{
      c.moveTo(x,5);
      c.lineTo(x+5,13);
      c.lineTo(x,21);
      c.lineTo(x-5,13);
      c.strokeStyle='#8592a6';
    }
    c.closePath();
    if(active){
      c.fillStyle=kind?c.strokeStyle:'#657ca0';
      c.fill();
    }else c.stroke();
  }
  function draw(width, offset) {
    sync();
    const canvas=$('beat-check-canvas');
    if(canvas&&open){
      const c=canvasContext(canvas,width,26);
      c.clearRect(0,0,width,26);
      for(const list of [automatic,doc.marks]) for(const m of list){
        const x=midiX(m.seconds)-offset;
        if(x>=-6&&x<=width+6) drawMark(c,m,x);
      }
      const title=busy?'正在保存标记…':
        doc.marks.length+' 个手动标记，'+automatic.length+' 个疑点；右键空白添加手动标记，单击定位';
      if(canvas.title!==title) canvas.title=title;
    }
    let line=$('beat-check-guide');
    const content=$('roll-content');
    const mark=open&&(doc.marks.find(m=>m.id===selected)||automatic.find(m=>m.id===selected));
    if(mark&&content){
      if(!line){
        line=document.createElement('span');
        line.id='beat-check-guide';
        line.style.cssText='position:absolute;top:0;bottom:0;width:0;border-left:1px dashed #8297b080;pointer-events:none;z-index:3';
        content.appendChild(line);
      }
      line.style.left=midiX(mark.seconds)+'px';
      line.hidden=false;
    }else if(line) line.hidden=true;
  }
  function hit(event, manualOnly=false) {
    const canvas=event.target;
    if(canvas?.id!=='beat-check-canvas') return null;
    sync();
    const offset=$('roll-scroll')?.scrollLeft||0;
    const x=event.clientX-canvas.getBoundingClientRect().left+offset;
    let mark=null, distance=8;
    for(const list of manualOnly?[doc.marks]:[doc.marks,automatic]) for(const m of list){
      const current=Math.abs(midiX(m.seconds)-x);
      if(current<=7&&current<distance){
        mark=m;
        distance=current;
      }
    }
    return {mark,seconds:midiSeconds(x)};
  }
  document.addEventListener('pointerdown',event=>{
    const h=hit(event);
    if(!h) return;
    event.stopImmediatePropagation();
    event.target.focus();
    if(event.button===0&&h.mark) locate(h.mark);
  },true);
  document.addEventListener('contextmenu',event=>{
    const h=hit(event,true);
    if(!h) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    if(h.mark) selectMark(h.mark);
    else if(Number.isFinite(h.seconds)&&h.seconds>=0) call('add',{seconds:h.seconds});
  },true);
  document.addEventListener('keydown',event=>{
    if(event.target?.id!=='beat-check-canvas'||!['Delete','Backspace'].includes(event.key)) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    action('editorBeatCheckRemove');
  },true);
  return {findEntry,inspectionMenu,markup,draw,action,sync};
})();
