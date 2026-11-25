(() => {
  'use strict';
  // Shared app-owned popup primitives: anchored placement, dismissal, entrance
  // motion, arrow-key movement and first-frame instrumentation. Used by the VST
  // chooser, the insert chooser, the MIDI source list and the toolbar <details>
  // menus. Native plugin windows and native <select>s elsewhere are untouched.
  const REDUCE='(prefers-reduced-motion: reduce)';
  const MOTION='astra-pop';
  const reduced=()=>typeof matchMedia==='function'&&matchMedia(REDUCE).matches;

  // Instrumentation only; never gates showing, hiding or selection.
  // performance marks: astra.popup.<name>.shown (sync, after the shell is in
  // the DOM and placed) and astra.popup.<name>.frame (next animation frame,
  // i.e. the frame that first composes the shell); measure astra.popup.<name>
  // spans input timestamp → that frame. AstraRenderStage receives the same.
  let lastInput=0;
  for(const type of ['pointerdown','click','keydown'])
    document.addEventListener(type,event=>{lastInput=event.timeStamp;},true);
  function startedAt(){const now=performance.now();return lastInput&&now-lastInput<1000?lastInput:now;}
  function trace(name,started=startedAt()){
    const shown=`astra.popup.${name}.shown`,frame=`astra.popup.${name}.frame`,span=`astra.popup.${name}`;
    try{performance.clearMarks(shown);performance.mark(shown);}catch{}
    requestAnimationFrame(()=>{
      const at=performance.now();
      try{performance.clearMarks(frame);performance.mark(frame);performance.clearMeasures(span);performance.measure(span,{start:started,end:at});}catch{}
      window.AstraRenderStage?.(`popup.${name}.first_frame`,at-started);
    });
  }

  function stop(box){for(const motion of box?.getAnimations?.()||[])if(motion.id===MOTION)motion.cancel();}
  // Short entrance from the trigger edge: transform/opacity only (compositor),
  // never on the trigger, so its hitbox does not move. Content is interactive
  // from the first frame and opacity starts well above zero, so the motion
  // cannot hide a slow first paint. Reduced motion: no animation at all.
  function enter(box,side,anchor){
    stop(box);
    if(!box)return null;
    box.dataset.popSide=side==='up'?'up':'down';
    if(reduced()||typeof box.animate!=='function')return null;
    if(anchor){
      const a=anchor.getBoundingClientRect(),b=box.getBoundingClientRect();
      const x=Math.round(Math.min(Math.max(a.left+a.width/2-b.left,0),b.width));
      box.style.transformOrigin=`${x}px ${side==='up'?'100%':'0'}`;
    }
    const dy=side==='up'?3:-3;
    const motion=box.animate([{opacity:.6,transform:`translateY(${dy}px) scale(.985)`},{opacity:1,transform:'none'}],
      {duration:120,easing:'cubic-bezier(.2,.8,.2,1)'});
    motion.id=MOTION;
    return motion;
  }

  // Fixed popover anchored to a trigger. Outside pointer and Escape close at
  // once (Escape returns focus); there is no exit animation to wait for.
  function anchored(box,{name='popup',width=300,minWidth=0,minBelow=180,gap=4,onHide}={}){
    let anchor=null;
    function place(){
      if(!anchor||!anchor.isConnected||!box.isConnected){hide();return null;}
      const r=anchor.getBoundingClientRect();
      const want=Math.max(typeof width==='function'?width(r):width,minWidth);
      const w=Math.max(1,Math.min(want,innerWidth-16));
      box.style.width=w+'px';
      box.style.left=Math.min(Math.max(8,r.left),innerWidth-w-8)+'px';
      const below=innerHeight-r.bottom-12,above=r.top-12,down=below>=minBelow||below>=above;
      box.style.maxHeight=Math.max(48,down?below:above)+'px';
      if(down){box.style.top=(r.bottom+gap)+'px';box.style.bottom='';}
      else{box.style.top='';box.style.bottom=(innerHeight-r.top+gap)+'px';}
      return down?'down':'up';
    }
    function outside(e){if(!box.contains(e.target)&&!anchor?.contains(e.target))hide();}
    function escape(e){if(e.key==='Escape'){e.preventDefault();e.stopImmediatePropagation();hide(true);}}
    // Scrolling the popup's own list does not move its anchor.
    function scrolled(e){if(e.target instanceof Node&&box.contains(e.target))return;place();}
    function show(next,{motion=true,started}={}){
      hide();anchor=next;box.hidden=false;next.setAttribute('aria-expanded','true');
      const side=place();
      if(!anchor)return;
      document.addEventListener('pointerdown',outside,true);
      window.addEventListener('keydown',escape,true);
      window.addEventListener('resize',place);
      document.addEventListener('scroll',scrolled,true);
      if(motion&&side)enter(box,side,next);
      trace(name,started);
    }
    function hide(focus=false){
      if(!anchor)return;
      const prior=anchor;anchor=null;
      stop(box);
      prior.setAttribute('aria-expanded','false');box.hidden=true;
      document.removeEventListener('pointerdown',outside,true);
      window.removeEventListener('keydown',escape,true);
      window.removeEventListener('resize',place);
      document.removeEventListener('scroll',scrolled,true);
      onHide?.(prior);
      if(focus&&prior.isConnected)prior.focus({preventScroll:true});
    }
    // A remounted trigger (same control, new node) keeps the open popup
    // without replaying its entrance.
    function reanchor(next){
      if(!anchor)return;
      if(!next){hide();return;}
      if(next!==anchor){anchor.setAttribute('aria-expanded','false');anchor=next;next.setAttribute('aria-expanded','true');}
      place();
    }
    return {show,hide,place,reanchor,get anchor(){return anchor;}};
  }

  // ArrowUp/ArrowDown/Home/End over enabled, rendered items. From a search
  // field only ArrowDown enters the list; ArrowUp from the first item returns.
  // Window capture runs before the editor's document-capture shortcuts, which
  // only exempt input/select/<dialog> targets: keys typed inside a popup are
  // the popup's (default actions such as button activation and typing still
  // happen). Escape and Tab continue to the popup's own handlers, and
  // Enter/Space on a <summary> still reach its fold listener.
  const scopes=[];
  function roving(box,selector,entry=null){
    for(let i=scopes.length-1;i>=0;i--)if(!scopes[i].box.isConnected)scopes.splice(i,1);
    scopes.push({box,selector,entry});
  }
  window.addEventListener('keydown',e=>{
    const t=e.target;
    const scope=t instanceof Node?scopes.find(s=>!s.box.hidden&&s.box.contains(t)):null;
    if(!scope||e.key==='Escape'||e.key==='Tab'||e.isComposing)return;
    if(['ArrowDown','ArrowUp','Home','End'].includes(e.key)&&!e.altKey&&!e.ctrlKey&&!e.metaKey){
      const items=[...scope.box.querySelectorAll(scope.selector)].filter(n=>!n.disabled&&n.getClientRects().length);
      const at=items.indexOf(document.activeElement);
      const caret=at<0&&t.matches?.('input')&&e.key!=='ArrowDown';
      if(!caret&&items.length){
        e.preventDefault();
        let i=e.key==='Home'?0:e.key==='End'?items.length-1:e.key==='ArrowDown'?Math.min(items.length-1,at+1):at-1;
        if(e.key==='ArrowUp'&&at<0)i=0;
        if(i<0){if(scope.entry)scope.entry.focus({preventScroll:true});else items[0].focus();}
        else items[i].focus();
      }
      e.stopPropagation();return;
    }
    if((e.key==='Enter'||e.key===' ')&&t.closest?.('summary'))return;
    e.stopPropagation();
  },true);

  // <details> menus: place and start the entrance in the mutation microtask
  // right after `open` flips, i.e. before the first frame. (The `toggle`
  // event is a later task, so placing there could compose one frame at the
  // stale in-flow position.) A render() that restores an already open menu
  // sets data-pop-restore and gets placement without a replayed entrance.
  const disclosures=[];
  let observer=null;
  function watch(selector,body,{name='disclosure',position=null}={}){
    disclosures.push({selector,body,name,position});
    if(observer||typeof MutationObserver!=='function')return;
    observer=new MutationObserver(records=>{
      const seen=new Set();
      for(const {target} of records){
        if(seen.has(target)||target?.tagName!=='DETAILS')continue;
        seen.add(target);
        const spec=disclosures.find(d=>target.matches(d.selector));if(!spec)continue;
        const panel=target.querySelector(spec.body);
        const quiet=Object.hasOwn(target.dataset,'popRestore');
        if(quiet)delete target.dataset.popRestore;
        if(!target.open){stop(panel);continue;}
        if(!panel)continue;
        spec.position?.(target);
        if(quiet)continue;
        const summary=target.querySelector(':scope>summary');
        const p=panel.getBoundingClientRect(),s=summary?.getBoundingClientRect();
        enter(panel,s&&p.bottom<=s.top+1?'up':'down',summary);
        trace(spec.name);
      }
    });
    observer.observe(document.documentElement,{subtree:true,attributes:true,attributeFilter:['open']});
  }

  window.AstraPopup={anchored,enter,stop,roving,watch,trace,reduced};
})();
