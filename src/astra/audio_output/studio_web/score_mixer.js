/* Bottom mixer for the integrated native piano score only. */
window.AstraScoreMixer = (() => {
  let current;
  const host = () => document.querySelector('#workspace>.workbench[data-view="score"] #session-body>.astra-native-score');
  const fmt = db => db <= -60 ? '−∞ dB' : (db > 0 ? '+' : '') + db.toFixed(1) + ' dB';
  function make(root, project){
    const menu=root.querySelector('[data-score-dock-toggle="palettes"]')?.parentElement;
    const bar=root.querySelector('.astra-native-statusbar');
    if(!menu||!bar)return null;
    const toggle=document.createElement('button');
    toggle.type='button';toggle.className='astra-ms-menu-plain';toggle.textContent='混音台';
    toggle.title='混音台  F10';toggle.setAttribute('aria-keyshortcuts','F10');
    toggle.setAttribute('aria-controls','astra-score-mixer');menu.append(toggle);
    const dock=document.createElement('section');
    dock.id='astra-score-mixer';dock.hidden=true;dock.setAttribute('aria-label','混音台');
    dock.innerHTML='<header><strong>混音台</strong><button type="button" data-mixer-close aria-label="关闭混音台">×</button></header><div class="ast-mix-channels"><div><strong>钢琴</strong><div class="vst-slot" data-score-mixer-rack aria-label="钢琴乐器和插入效果"></div><label>乐器增益 <input data-mix-piano type="range" min="-60" max="6" step="0.5" value="0"><output data-mix-piano-label></output></label></div><div><strong>主输出</strong><label>音量 <input data-mix-master type="range" min="0" max="100" step="1" value="65"><output data-mix-master-label></output></label></div></div>';
    bar.before(dock);
    const m={root,project,toggle,dock,rack:dock.querySelector('[data-score-mixer-rack]'),model:null,open:false,lease:null,intents:{}};
    function set(open){
      if(m.open===open)return;
      m.open=open;m.lease=open?{}:null;m.intents={};dock.hidden=!open;toggle.setAttribute('aria-pressed',String(open));
      if(!open && dock.contains(document.activeElement))
        root.querySelector('.astra-native-paper')?.focus({preventScroll:true});
      m.model?.changed();
    }
    m.set=set;
    toggle.addEventListener('click',()=>{const parent=toggle.closest('details');set(!m.open);if(parent){parent.open=false;parent.querySelector('summary')?.focus({preventScroll:true});}});
    dock.querySelector('[data-mixer-close]').addEventListener('click',()=>set(false));
    for(const key of ['piano','master']){
      const input=m[key]=dock.querySelector('[data-mix-'+key+']');
      const out=m[key+'Label']=dock.querySelector('[data-mix-'+key+'-label]');
      const hold=()=>{if(current===m&&m.open&&!input.disabled)m.intents[key]??=m.model?.faderScope;};
      input.addEventListener('pointerdown',hold);
      input.addEventListener('pointercancel',()=>{delete m.intents[key];});
      input.addEventListener('blur',()=>{delete m.intents[key];});
      input.addEventListener('input',()=>{hold();out.value=key==='piano'?fmt(Number(input.value)):input.value+'%';});
      input.addEventListener('change',()=>{
        const held=m.intents?.[key];if(m.intents)delete m.intents[key];
        if(current!==m||!m.open||!m.model||input.disabled||!m.model.faderScope||
           (held!=null&&held!==m.model.faderScope))return;
        m.model[key+'Change'](Number(input.value));
      });
    }
    toggle.setAttribute('aria-pressed','false');
    return m;
  }
  function sync(model){
    const root=model&&host();
    if(current && (current.root!==root || current.project!==model?.project)){
      current.toggle.remove();current.dock.remove();current=null;
    }
    if(!root)return null;
    current ||= make(root,model.project);
    if(!current)return null;
    current.model=model;
    for(const k of ['piano','master']){
      const n=model[k], input=current[k];
      const disabled=!Number.isFinite(n);
      if(input.disabled!==disabled)input.disabled=disabled;
      if(!disabled&&document.activeElement!==input&&Number(input.value)!==n)input.value=String(n);
      const label=input.disabled?'—':k==='piano'?fmt(Number(input.value)):input.value+'%';
      if(current[k+'Label'].value!==label)current[k+'Label'].value=label;
    }
    return current.open?current.rack:null;
  }
  document.addEventListener('keydown',ev=>{
    if(ev.key!=='F10'||ev.altKey||ev.ctrlKey||ev.metaKey||ev.shiftKey||ev.repeat||ev.isComposing||
       ev.target.closest?.('input,textarea,select,dialog,[contenteditable=true]')||document.querySelector('dialog[open]')||
       !current?.root.isConnected||current.root!==host())return;
    ev.preventDefault();ev.stopPropagation();current.set(!current.open);
  },true);
  return Object.freeze({sync,lease:()=>current?.lease});
})();
