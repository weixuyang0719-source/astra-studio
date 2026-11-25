"use strict";
window.AstraProjectOpening=(()=>{
 let serial=0,active=null;
 function panel(){let el=document.getElementById('project-opening-status');if(!el){el=document.createElement('div');el.id='project-opening-status';el.setAttribute('role','status');el.setAttribute('aria-live','polite');el.style.cssText='position:fixed;top:76px;left:50%;transform:translateX(-50%);z-index:1000;padding:10px 18px;border:1px solid var(--ui-line,#d6dde5);border-radius:12px;background:var(--ui-surface,#fff);color:var(--ui-text,#3e444b);box-shadow:var(--shadow-pop,0 5px 22px #29303714);pointer-events:none;max-width:70vw;white-space:nowrap;overflow:hidden;text-overflow:ellipsis';document.body.appendChild(el);}return el;}
 function begin(path){
   if(active?.path===path&&!active.timedOut)return {schedule(){},finish(){}};
   if(active){cancelAnimationFrame(active.frame);clearTimeout(active.timer);}
   const ticket={id:++serial,path,frame:0,timer:0};active=ticket;
   panel().hidden=false;panel().textContent='正在打开项目…';
   document.getElementById('workspace')?.setAttribute('aria-busy','true');
   ticket.schedule=send=>{ticket.frame=requestAnimationFrame(()=>{ticket.frame=requestAnimationFrame(()=>{if(active!==ticket)return;try{send();}catch(error){ticket.finish();throw error;}});});};
   ticket.finish=()=>{if(active!==ticket)return;clearTimeout(ticket.timer);active=null;panel().hidden=true;document.getElementById('workspace')?.removeAttribute('aria-busy');};
   // A stalled channel must not trap navigation or claim that opening succeeded.
   ticket.timer=setTimeout(()=>{if(active===ticket){panel().textContent='项目仍在打开，可重新选择项目';document.getElementById('workspace')?.removeAttribute('aria-busy');ticket.timedOut=true;}},15000);
   return ticket;
 }
 function cancel(){if(!active)return;cancelAnimationFrame(active.frame);clearTimeout(active.timer);active.finish();}
 function observe(status){if(active&&status&&status.request===active.id&&!status.pending)active.finish();}
 return {begin,cancel,observe};
})();
