/* Semantic cursor adapter. No mouse follower, animation, capture or event interception. */
(function(){
 const roles={default:'arrow',pointer:'pointer',crosshair:'range',grab:'grab',grabbing:'grabbing','ew-resize':'resize-x','ns-resize':'resize-y','not-allowed':'blocked',copy:'copy','zoom-in':'zoom-in','zoom-out':'zoom-out',pen:'pen',beat:'beat',erase:'erase',range:'range',velocity:'velocity',cut:'cut'};
 const fallback={pen:'crosshair',beat:'crosshair',erase:'crosshair',range:'crosshair',velocity:'ns-resize',cut:'col-resize'};
 function css(role){return roles[role]?`var(--astra-cursor-${roles[role]}, ${fallback[role]||role})`:role;}
 function set(target,role){if(target?.style)target.style.cursor=css(role);}
 function theme(value){value=value==='system'?'system':'soft';document.documentElement.dataset.cursorTheme=value;try{localStorage.setItem('astra.cursorTheme',value);}catch(_){}return value;}
 let initial='soft';try{initial=localStorage.getItem('astra.cursorTheme')||initial;}catch(_){}theme(initial);
 document.addEventListener('change',event=>{if(event.target.id==='astra-cursor-theme')theme(event.target.value);});
 window.AstraCursors={css,set,theme};
})();
