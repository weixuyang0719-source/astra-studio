/* SPDX-License-Identifier: GPL-3.0-only */
(() => {
  "use strict";
  const Chrome=window.AstraScorePlaybackChrome;
  let live=Chrome.normalise();
  new QWebChannel(qt.webChannelTransport,({objects})=>{
    const bridge=objects.scoreFloat;
    const send=req=>bridge.command(JSON.stringify(req));
    let dragging=false,moved=false,start=null;
    const view=Chrome.createView(document.getElementById("standalone-score-playback"),{floating:true,send,
      onGripDouble:()=>{if(live.epoch)bridge.returnToDock(live.epoch);},
      onGripStart:(d,p)=>{start=p;dragging=false;moved=false;},
      onGripMove:(d,p)=>{
        if(!live.epoch||!start)return;
        if(!moved&&Math.hypot(p.x-start.x,p.y-start.y)>6){moved=true;dragging=true;bridge.startDrag(live.epoch,start.x,start.y);}
        if(dragging)bridge.moveDrag(live.epoch);
      },
      onGripEnd:(d,p,cancelled)=>{
        if(dragging&&live.epoch)bridge.endDrag(live.epoch,cancelled);
        start=null;dragging=moved=false;
      }
    });
    // Only the popup grows the tool surface; docked and floating rows never change size on status updates.
    document.addEventListener("toggle",event=>{
      if(event.target.matches?.(".ast-sp-pop")&&live.epoch)bridge.showMenu(live.epoch,event.target.open);
    },true);
    document.addEventListener("keydown",event=>{
      const editable=event.target.closest("input,select,textarea,summary,button,[contenteditable]");
      if(event.code!=="Space"||editable||event.repeat||event.isComposing||event.ctrlKey||event.metaKey||
         event.altKey||event.shiftKey||!Chrome.can(live,"playPause"))return;
      event.preventDefault();send({epoch:live.epoch,revision:live.revision,action:"playPause"});
    });
    bridge.modelChanged.connect(value=>{
      try{live=Chrome.normalise(JSON.parse(value));view.paint(live);}
      catch{}
    });
    bridge.ready();
  });
})();
