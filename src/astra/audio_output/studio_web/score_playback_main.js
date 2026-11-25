/* SPDX-License-Identifier: GPL-3.0-only
 * Score-only controller. No channel, project, edit or audio state is copied to the floating surface.
 *
 * Docking follows MuseScore 4.6.5 DockToolBar/DropController the same way the
 * score's input toolbar does (inline_native_input_dock.js): drag the grip out
 * of the score tool row to float the playback toolbar inside the window, drag
 * it back over the row to see the blue drop hint and release to dock it.
 * Double-click or Enter on the grip toggles; Escape or a lost pointer cancels.
 */
(() => {
  "use strict";
  const Chrome=window.AstraScorePlaybackChrome;
  const normal=Chrome.normalise,parse=Chrome.command;
  let slot=null,view=null,callback=null,raw=null,model=normal(),key=null,epoch=0,row=null;
  // Dock state survives remounts of the score tab, like the input toolbar's.
  let docked=true,floatX=0,floatY=0,drag=null,preview=null,spacer=null;
  const finitePosition=n=>Number.isFinite(n);
  const visible=()=>slot?.isConnected&&raw&&Boolean(key);
  const clamp=(n,min,max)=>Math.max(min,Math.min(n,Math.max(min,max)));
  const scoreRoot=()=>row?.closest("#astra-native-score")||row?.parentElement;

  function bounds(){
    const r=scoreRoot()?.getBoundingClientRect();
    if(!r)return {left:0,top:0,right:innerWidth,bottom:innerHeight};
    return {left:Math.max(0,r.left),top:Math.max(0,r.top),right:Math.min(innerWidth,r.right),bottom:Math.min(innerHeight,r.bottom)};
  }
  function moveFloat(x,y){
    if(!slot)return;
    const b=bounds();
    floatX=clamp(x,b.left+5,b.right-slot.offsetWidth-5);
    floatY=clamp(y,b.top+5,b.bottom-slot.offsetHeight-5);
    slot.style.left=floatX+"px";slot.style.top=floatY+"px";
  }
  function applyDock(){
    if(!slot)return;
    slot.dataset.dock=docked?"top":"float";
    if(docked){slot.style.removeProperty("left");slot.style.removeProperty("top");}
    else moveFloat(floatX,floatY);
  }
  // Where the bar sits when docked: the right end of the score tool row, just
  // before undo/redo. While dragging out of the row the spacer holds that place.
  function homeRect(){
    if(!row)return null;
    const r=row.getBoundingClientRect();
    if(!r.width||!r.height)return null;
    if(spacer?.isConnected&&!spacer.hidden){
      const s=spacer.getBoundingClientRect();
      return {x:s.left,y:r.top,w:s.width,h:r.height};
    }
    const history=row.querySelector(".astra-native-history"), w=slot?.offsetWidth||320;
    const right=history?history.getBoundingClientRect().left:r.right;
    return {x:Math.max(r.left,right-w),y:r.top,w:Math.min(w,right-r.left),h:r.height};
  }
  function target(x,y){
    const r=row?.getBoundingClientRect(), b=bounds();
    if(!r||x<b.left||x>=b.right||y<b.top||y>=b.bottom)return null;
    // Same tolerance band as the input toolbar's top slot.
    if(x>=r.left&&x<=r.right&&y>=r.top-14&&y<=r.bottom+14)return homeRect();
    return null;
  }
  function drawHint(hit){
    if(!preview)return;
    preview.hidden=!hit;
    if(hit)Object.assign(preview.style,{left:hit.x+"px",top:hit.y+"px",width:hit.w+"px",height:hit.h+"px"});
  }
  function endDrag(commit,p){
    const d=drag;drag=null;
    if(!d)return;
    drawHint(null);
    if(!d.started)return;
    delete slot.dataset.dockDragging;
    if(spacer){spacer.hidden=true;spacer.remove();}
    if(!commit){
      docked=d.wasDocked;floatX=d.floatX;floatY=d.floatY;applyDock();return;
    }
    docked=Boolean(target(p.x,p.y));
    if(!docked)moveFloat(p.x-d.dx,p.y-d.dy);
    applyDock();
    view?.grip.focus({preventScroll:true});
  }
  function toggleDock(){
    if(!slot||drag)return;
    if(docked){
      const r=slot.getBoundingClientRect();
      floatX=r.left;floatY=r.bottom+8;docked=false;
    }else docked=true;
    applyDock();
    view?.grip.focus({preventScroll:true});
  }
  function gripStart(d,p){
    if(!slot)return;
    const r=slot.getBoundingClientRect(), g=view.grip.getBoundingClientRect();
    drag={origin:p,started:false,wasDocked:docked,floatX,floatY,width:r.width,height:r.height,
      dx:p.x-r.left,dy:p.y-r.top,gripDx:p.x-g.left};
  }
  function gripMove(d,p){
    const a=drag;
    if(!a||!slot)return;
    if(!a.started){
      if(Math.hypot(p.x-a.origin.x,p.y-a.origin.y)<5)return;
      a.started=true;
      for(const menu of slot.querySelectorAll("details[open]"))menu.open=false;
      if(docked){
        spacer.style.width=a.width+"px";spacer.style.height=a.height+"px";
        slot.before(spacer);spacer.hidden=false;
        docked=false;slot.dataset.dock="float";
      }
      slot.dataset.dockDragging="";
    }
    moveFloat(p.x-a.dx,p.y-a.dy);
    drawHint(target(p.x,p.y));
  }
  function onKey(e){
    if(e.key==="Escape"&&drag?.started){e.preventDefault();e.stopImmediatePropagation();endDrag(false);}
  }
  window.addEventListener("keydown",onKey,true);
  window.addEventListener("blur",()=>endDrag(false));
  window.addEventListener("resize",()=>{if(drag)endDrag(false);if(!docked)moveFloat(floatX,floatY);});

  // Kept for the host wiring: the native floating window is no longer used.
  function attach(){ if(visible())view?.paint(model); }
  function acceptNativeNotice(){}
  function deactivate(){
    endDrag(false);
    view?.destroy();view=null;slot?.remove();slot=null;spacer?.remove();preview?.remove();
    spacer=preview=row=null;raw=callback=key=null;model=normal();
  }
  function mount(root) {
    const next=root?.querySelector(":scope > .astra-native-score-tools");
    if(!next)return false;
    if(slot?.parentElement===next&&slot.isConnected)return "kept";
    endDrag(false);
    view?.destroy();slot?.remove();spacer?.remove();preview?.remove();
    row=next;
    slot=document.createElement("div");slot.className="ast-sp-slot";
    spacer=document.createElement("div");spacer.className="ast-sp-dock-spacer";spacer.hidden=true;spacer.inert=true;
    preview=document.createElement("div");preview.className="ast-sp-dock-preview";preview.hidden=true;
    preview.inert=true;preview.setAttribute("aria-hidden","true");
    (scoreRoot()||document.body).append(preview);
    const history=row.querySelector(".astra-native-history");
    row.insertBefore(slot,history);
    view=Chrome.createView(slot,{send:submit,gripEnabled:()=>true,
      onGripDouble:()=>toggleDock(),
      onGripStart:gripStart,
      onGripMove:gripMove,
      onGripEnd:(d,p,cancelled)=>endDrag(!cancelled,p)
    });
    applyDock();
    return "fresh";
  }
  function present({root, scope, snapshot},send) {
    if(typeof scope!=="string"||!scope||!root?.isConnected||root.closest(".temp-right")||
       typeof send!=="function"){deactivate();return false;}
    if(key!==scope){
      deactivate();
      key=scope;epoch=(epoch%2147483646)+1;
    }
    const mounted=mount(root);
    if(!mounted){deactivate();return false;}
    raw=snapshot;callback=send;
    const next=normal({...snapshot,epoch});
    const changed=mounted==="fresh"||JSON.stringify(next)!==JSON.stringify(model);
    model=next;
    if(changed)view.paint(model);
    return true;
  }
  function position(seconds){
    if(!visible()||!finitePosition(seconds)||seconds<0||seconds>model.duration||raw.position===seconds)return;
    raw={...raw,position:seconds};model=normal({...raw,epoch});
    view.paint(model);
  }
  function submit(req){
    if(!visible())return false;
    const action=parse(model,req);
    if(!action)return false;
    callback(action,model.revision);
    return true;
  }
  function play(){
    return submit({epoch:model.epoch,revision:model.revision,action:"playPause"});
  }
  window.AstraScoreTransport=Object.freeze({attach,present,deactivate,position,play,acceptNativeNotice,
    active:()=>Boolean(visible()),canPlay:()=>Boolean(visible()&&Chrome.can(model,"playPause")),
    dockState:()=>visible()?{docked,dragging:Boolean(drag?.started)}:null});
})();
