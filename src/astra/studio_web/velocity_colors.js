"use strict";
window.AstraVelocity=(()=>{
 const stops=[[1,[230,239,246]],[32,[161,202,220]],[56,[114,164,195]],[72,[132,140,185]],[88,[174,113,151]],[104,[173,85,113]],[127,[136,58,79]]];
 const luminance=rgb=>rgb.map(x=>x/255).map(x=>x<=.04045?x/12.92:((x+.055)/1.055)**2.4).reduce((n,x,i)=>n+x*[.2126,.7152,.0722][i],0);
 const colors=[];let previous=1;
 for(let v=1;v<=127;v++){
   let j=0;while(j<stops.length-2&&v>stops[j+1][0])j++;
   const [a,c]=stops[j],[b,d]=stops[j+1],t=(v-a)/(b-a);
   let rgb=c.map((x,k)=>Math.round(x+(d[k]-x)*t));
   // Keep visual weight ordered even at 8-bit channel-rounding plateaus.
   while(luminance(rgb)>previous){const choices=rgb.map((x,i)=>rgb.map((y,k)=>i===k?Math.max(0,y-1):y));const safe=choices.filter(x=>luminance(x)<=previous);rgb=(safe.length?safe:choices).sort((a,b)=>safe.length?luminance(b)-luminance(a):luminance(a)-luminance(b))[0];}
   previous=luminance(rgb);colors.push('#'+rgb.map(x=>x.toString(16).padStart(2,'0')).join(''));
 }
 function value(note){const raw=note?.velocity;return raw===null||raw===undefined||!Number.isFinite(Number(raw))?64:Math.max(1,Math.min(127,Math.round(Number(raw))));}
 return Object.freeze({color:note=>colors[value(note)-1],value,colors:Object.freeze(colors)});
})();
