"use strict";
window.AstraAnalysisHints=(()=>{
 function model(project){
  const report=project?.report||{},raw=(report.warnings||[]).map(w=>typeof w==='string'?w:JSON.stringify(w)),unique=[...new Set(raw)];
  // Merge only exact duplicates or the known prefixed copy of the same local-timing warning.
  const messages=unique.filter(w=>!unique.some(other=>other!==w&&other.length>w.length&&other.endsWith(w)&&other.startsWith('拍值证据尚不足，保留当前拍点图。')));
  const groups=messages.map(text=>{
   if(text.startsWith('首小节相位暂取'))return {kind:'opening',title:'第一小节从哪里开始，还没确认',body:'模型先把检测到的第一个强拍当作起点。请结合前奏或弱起检查；这不代表第一小节一定有错。',raw:text};
   if(text.includes('尚未采用新拍值')&&text.includes('局部节奏仍需检查'))return {kind:'local',title:'部分片段的节奏划分不确定',body:'分析没有足够把握调整这些片段，因此保留了原拍点。音符和踏板的实际时间没有被移动。',raw:text};
   if(text.startsWith('DuET 有 ')&&text.includes('核对时值倍率'))return {kind:'duet',title:'部分音符的谱面时值需要核对',body:'制谱模型对一些音符该写成长音还是短音仍不确定。它们不一定是错误，也不意味着播放速度已经改变。具体区间保存在制谱时间复核报告中。',raw:text};
   return {kind:'other',title:'导出说明',body:text,raw:text};
  });
  const exported=report.editor_revision,current=project?.editor?.revision;
  const known=exported!==undefined&&exported!==null&&current!==undefined&&current!==null;
  return {groups,raw,exported,current,freshness:known?(String(exported)===String(current)?'same':'stale'):'unknown'};
 }
 return {model};
})();
