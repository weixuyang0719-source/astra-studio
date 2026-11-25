/* In-app adaptation of MuseScore 4.6.5 DockToolBar.qml and DropController.
 * Geometry only: no score bridge, transport, or undo calls. */
(() => {
  let current;
  const points=(x,y,w,h)=>({x,y,w,h});
  const clamp=(n,min,max)=>Math.max(min,Math.min(n,Math.max(min,max)));
  function attach(root) {
    const bar=root.querySelector(".astra-native-notation-tools");
    const workspace=root.querySelector(".astra-native-workspace");
    const status=root.querySelector(".astra-native-statusbar");
    if(!bar||!workspace||!status)return;
    const top=document.createComment("input toolbar home");
    bar.before(top);
    const spacer=document.createElement("div"), preview=document.createElement("div");
    spacer.className="astra-input-dock-spacer"; spacer.hidden=true; spacer.inert=true;
    preview.className="astra-input-dock-preview"; preview.hidden=true; preview.inert=true;
    preview.setAttribute("aria-hidden","true"); root.append(preview);
    let slot="top", active=null, floatX=0, floatY=0, lastDock="top";
    bar.dataset.inputDock=slot;
    const place=(el,x,y,w,h)=>Object.assign(el.style,{left:x+"px",top:y+"px",
      ...(w!==undefined?{width:w+"px",height:h+"px"}:{})});
    function bounds(){
      const r=root.getBoundingClientRect(), left=Math.max(0,r.left), right=Math.min(innerWidth,r.right);
      return {left,right,top:Math.max(0,r.top),bottom:Math.min(innerHeight,r.bottom)};
    }
    function moveFloat(x,y) {
      const r=bounds(), maxW=Math.max(1,r.right-r.left-10), maxH=Math.max(1,r.bottom-r.top-10);
      bar.style.setProperty("--input-dock-max-w",maxW+"px");
      bar.style.setProperty("--input-dock-max-h",maxH+"px");
      floatX=clamp(x,r.left+5,r.right-bar.offsetWidth-5);
      floatY=clamp(y,r.top+5,r.bottom-bar.offsetHeight-5);
      place(bar,floatX,floatY);
    }
    function dock(next){
      slot=next;
      if(next!=="float")lastDock=next;
      bar.dataset.inputDock=next;
      workspace.classList.toggle("astra-input-docked-left",next==="left");
      if(next==="top")top.after(bar);
      else if(next==="left")workspace.prepend(bar);
      else status.before(bar);
      if(next!=="float"){
        bar.style.removeProperty("left"); bar.style.removeProperty("top");
      } else moveFloat(floatX,floatY);
    }
    function target(x,y) {
      const a=workspace.getBoundingClientRect(), p=top.parentElement.querySelector(".astra-native-score-tools").getBoundingClientRect();
      const r=bounds();
      if(x<r.left||x>=r.right||y<r.top||y>=r.bottom)return null;
      const l=Math.max(a.left,r.left), right=Math.min(a.right,r.right), w=right-l;
      if(w<160)return null;
      const ty=p.bottom, by=a.bottom;
      if(y>=ty-12&&y<=ty+26&&ty>=r.top&&ty+38<=r.bottom)return ["top",points(l,ty,w,38)];
      if(y>=by-26&&y<=by+12&&by-38>=r.top&&by<=r.bottom)return ["bottom",points(l,by-38,w,38)];
      if(x>=l&&x<=l+25&&y>=Math.max(r.top,a.top)+32&&y<=Math.min(r.bottom,a.bottom)-32)
        return ["left",points(l,Math.max(r.top,a.top),78,Math.min(r.bottom,a.bottom)-Math.max(r.top,a.top))];
      return null;
    }
    function draw(hit){
      preview.hidden=!hit;
      if(hit){
        const [next,box]=hit; preview.dataset.slot=next;
        place(preview,box.x,box.y,box.w,box.h);
      }else delete preview.dataset.slot;
    }
    function finish(commit,x,y){
      if(!active)return;
      const a=active; active=null;
      a.abort.abort();
      draw(null);
      if(!a.started)return;
      // Clear guarded pointer state before release/reparent may emit lostpointercapture.
      if(a.handle.hasPointerCapture(a.id))a.handle.releasePointerCapture(a.id);
      spacer.hidden=true;
      delete bar.dataset.inputDragging;
      if(commit){
        const hit=target(x,y);
        dock(hit?.[0]||"float");
      }else{
        floatX=a.floatX; floatY=a.floatY;
        if(slot==="float")moveFloat(floatX,floatY);
        else {bar.style.removeProperty("left");bar.style.removeProperty("top");}
      }
    }
    function gesture(e){
      const handle=e.target.closest(".astra-ms-grip");
      if(!handle||!bar.contains(handle)||bar.inert||e.button!==0||!e.isPrimary||active)return;
      const b=bar.getBoundingClientRect(), g=handle.getBoundingClientRect();
      const abort=new AbortController(), signal=abort.signal;
      active={abort,handle,id:e.pointerId,x:e.clientX,y:e.clientY,
        width:b.width,height:b.height, floatX,floatY,
        dx:e.clientX-(slot==="left"?g.left:b.left),dy:e.clientY-(slot==="left"?g.top:b.top),started:false};
      // Pointer stays captured by the same node until the drop decision.
      handle.setPointerCapture(e.pointerId);
      handle.addEventListener("pointermove",ev=>{
        const a=active;
        if(!a||ev.pointerId!==a.id)return;
        if(!a.started){
          if(Math.hypot(ev.clientX-a.x,ev.clientY-a.y)<5)return;
          a.started=true;
          if(slot!=="float"){
            bar.before(spacer);
            Object.assign(spacer.style,{height:a.height+"px",width:a.width+"px"});
            spacer.hidden=false;
          }
          for(const menu of bar.querySelectorAll("details[open]"))menu.open=false;
          bar.dataset.inputDragging="";
        }
        ev.preventDefault();
        moveFloat(ev.clientX-a.dx,ev.clientY-a.dy);
        draw(target(ev.clientX,ev.clientY));
      },{signal});
      handle.addEventListener("pointerup",ev=>{if(ev.pointerId===active?.id)finish(true,ev.clientX,ev.clientY);},{signal});
      const cancel=ev=>{if(ev.pointerId===active?.id)finish(false);};
      handle.addEventListener("pointercancel",cancel,{signal});
      handle.addEventListener("lostpointercapture",cancel,{signal});
      window.addEventListener("blur",()=>finish(false),{signal});
      window.addEventListener("keydown",ev=>{
        if(ev.key==="Escape"&&active?.started){ev.preventDefault();ev.stopImmediatePropagation();finish(false);}
      },{signal,capture:true});
    }
    bar.addEventListener("pointerdown",gesture);
    function toggle(e){
      if(!e.target.closest(".astra-ms-grip")||bar.inert)return;
      if(e.type==="keydown"&&e.key!=="Enter")return;
      if(active)return;
      e.preventDefault();e.stopPropagation();
      const grip=e.target.closest(".astra-ms-grip");
      if(slot==="float")dock(lastDock);
      else{
        const r=bar.getBoundingClientRect(), g=grip.getBoundingClientRect();
        floatX=g.x+12;floatY=r.y+16;dock("float");
      }
      grip.focus({preventScroll:true});
    }
    bar.addEventListener("dblclick",toggle);
    bar.addEventListener("keydown",toggle);
    current={root,reflow:()=>{if(active)finish(false);if(slot==="float")moveFloat(floatX,floatY);}};
  }
  window.addEventListener("resize",()=>{if(current?.root.isConnected)current.reflow();});
  window.AstraScoreInputDock={attach};
})();
