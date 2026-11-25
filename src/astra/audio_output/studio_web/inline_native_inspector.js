"use strict";

// An in-place view of native PropertyItems. No browser score or property model.
window.AstraNativeInspector = (() => {
  const tags = new Set(["boolean","color","text","number","point","choice"]);
  const make = (tag, cls, label) => {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (label != null) node.textContent = label;
    return node;
  };
  function btn(label, action) {
    const button=make("button","",label);
    button.type="button";
    button.addEventListener("click",action);
    return button;
  }
  const clean=(text,limit=160)=>typeof text==="string"?text.trim().slice(0,limit):"";
  const valueRange=(r,n)=>typeof n==="number"&&Number.isFinite(n)&&n>=r.min&&n<=r.max&&(!r.integer||Number.isInteger(n));
  const valueNumber=el=>el.value.trim()!==""?Number(el.value):NaN;

  function create({send}) {
    let area,panel,toggle,headline,content,notice,state={},shown=false,current=null,loading=null;
    let timer=null,bound=null,report="",retryAfter=0,loadingSince=0,slowTimer=null;
    // Fields already on screen, keyed by native property id (MuseScore's
    // inspector rebinds values in place when the selection changes).
    let built=new Map(),builtShape="";
    const stamp=()=>JSON.stringify([state.project,state.documentGeneration,state.selectionStamp,state.revision]);
    const allowed=()=>state.ready && state.editing && !state.input && !state.textEditing && state.selected;
    const canSend=()=>shown && allowed() && !state.waiting;
    const active=()=>current?.stamp===stamp();
    const writeNotice=s=>{if(notice)notice.textContent=s||"";};
    const paintAvailability=()=>{
      if(!toggle)return;
      toggle.disabled=!state.ready;
      toggle.setAttribute("aria-expanded",String(shown && !toggle.disabled));
      panel.hidden=!shown||toggle.disabled;
      panel.setAttribute("aria-busy",String(!!loading||!!state.waiting));
      if(!shown)return;
      content.inert=!active()||!allowed()||!!state.waiting;
      if(report){writeNotice(report);return;}
      // Keep the previous fields quietly while the next read is in flight;
      // only a slow read (or an empty panel) says so.
      const slow=!content.firstChild||(loading&&performance.now()-loadingSince>400);
      writeNotice(!allowed()?(state.input||state.textEditing?"先完成录入或文字编辑。":"选择谱面元素以编辑属性。"):
                  loading||!active()?(slow?"正在读取原生属性…":""):!current.rows.length?"当前选择没有可编辑属性。":"");
    };
    function schedule() {
      if(timer!==null||!area?.isConnected||!canSend()||active()||loading||Date.now()<retryAfter)return;
      // Do not enqueue reads while the user drags, selects, or enters notes.
      timer=setTimeout(()=>{
        timer=null;
        if(!area?.isConnected||!canSend()||active()||loading)return;
        const expected=stamp();
        if(send("properties")){
          loading=expected;loadingSince=performance.now();paintAvailability();
          clearTimeout(slowTimer);slowTimer=setTimeout(()=>{if(loading)paintAvailability();},420);
        }
      },30);
    }
    function edit(row,value,wantsReset=false) {
      if(!canSend()||!active()||!row.enabled||(wantsReset&&!row.resettable))return;
      report="";
      const args={id:row.id};
      if(wantsReset)args.reset=true;else args.value=value;
      if(send("property-set",args))paintAvailability();
    }
    function numberInput(row,label,value,apply) {
      const input=make("input");
      input.type="number";input.min=String(row.min);input.max=String(row.max);input.step=row.integer?"1":"any";
      input.value=typeof value==="number"?String(value):"";
      input.placeholder=row.mixed?"混合值":"";
      input.setAttribute("aria-label",label);
      if(apply)input.addEventListener("change",()=>{
        const n=valueNumber(input);
        if(valueRange(row,n))apply(n);
        else {report="请输入 "+row.min+" 至 "+row.max+" 的数值。";paintAvailability();}
      });
      input.addEventListener("keydown",event=>{
        if(event.key!=="ArrowUp"&&event.key!=="ArrowDown")return;
        const n=valueNumber(input);
        if(!Number.isFinite(n)||!Number.isFinite(row.step))return;
        event.preventDefault();
        input.value=String(Math.max(row.min,Math.min(row.max,Number((n+(event.key==="ArrowUp"?row.step:-row.step)).toFixed(5)))));
      });
      return input;
    }
    // MuseScore Inspector names for the controls a piano score shows; native
    // property names that come through untranslated get these.
    const LABELS={isVisible:"可见",isSmall:"小号",isAutoPlaceAllowed:"自动放置",isPlayable:"播放",color:"颜色",offset:"偏移",
      leadingSpace:"前导空白",measureWidth:"拉伸",minimumDistance:"最小距离",isStemless:"无符干",showStemSlash:"符干斜线",
      isHeadHidden:"隐藏符头",isHeadSmall:"小符头",hasHeadParentheses:"符头括号",headDirection:"符头方向",dotPosition:"附点位置",
      stemDirection:"符干方向",directionType:"方向",direction:"方向",thickness:"粗细",length:"长度",forceHorizontal:"强制水平",
      featheringHeightLeft:"羽化高度（左）",featheringHeightRight:"羽化高度（右）",numberType:"数字",bracketType:"括号",
      lineThickness:"线条粗细",placement:"位置",style:"样式",position:"位置",tiePlacement:"位置",lineStyle:"线型",minLength:"最小长度",
      startHookType:"起始钩",endHookType:"结束钩",lineType:"线型",startHookHeight:"起始钩高度",endHookHeight:"结束钩高度",
      dashLineLength:"虚线长度",dashGapLength:"虚线间距",gapBetweenTextAndLine:"文本与线条间距",beginningText:"起始文本",
      continuousText:"延续文本",endText:"结束文本",beginningTextOffset:"起始文本偏移",continuousTextOffset:"延续文本偏移",
      endTextOffset:"结束文本偏移",allowDiagonal:"允许倾斜",isLineVisible:"显示线条",height:"高度",continuousHeight:"延续高度",
      snapBefore:"吸附到前一个",snapAfter:"吸附到后一个",avoidBarLines:"避开小节线",avoidBarlines:"避开小节线",
      centerOnNotehead:"居中于符头",dynamicSize:"尺寸",ottavaType:"类型",showText:"显示文本",text:"文本",followText:"跟随文本",
      tempo:"速度",pauseDuration:"停顿时长",type:"类型",mode:"模式",verse:"段",textType:"文本类型",fontFamily:"字体",fontSize:"字号",
      frameType:"边框",frameBorderColor:"边框颜色",frameFillColor:"填充颜色",frameThickness:"边框粗细",frameMargin:"边框边距",
      frameCornerRadius:"圆角",horizontalScale:"水平缩放",verticalScale:"垂直缩放",playCount:"演奏次数",showCueNote:"显示提示音符",
      startOnUpperNote:"从上方音开始",shouldShowCourtesy:"显示提示拍号",hasToShowCourtesy:"显示提示调号",showCourtesySignatures:"显示提示记号",textLineSpacing:"行距",symbolSize:"符号大小",label:"标签"};
    const GENERAL_FLAGS=["isVisible","isSmall","isAutoPlaceAllowed","isPlayable"];
    const label=row=>LABELS[row.field]||row.name||row.field;
    const collapsed=new Map();   // section title -> collapsed (MuseScore keeps sub-blocks closed by default)
    const activeTab=new Map();
    function glyph(name){
      const s=make("span","astra-ms-glyph");s.setAttribute("aria-hidden","true");
      const code=window.AstraMuseScoreStock?.iconCodes?.[name];s.textContent=code?String.fromCodePoint(code):"";return s;
    }
    function spin(row,name,value,apply){
      const wrap=make("span","astra-ms-spin");
      const input=numberInput(row,name,value,apply);input.disabled=!row.enabled;
      const step=dir=>{
        const n=valueNumber(input), base=Number.isFinite(n)?n:0, st=Number.isFinite(row.step)?row.step:1;
        const next=Math.max(row.min,Math.min(row.max,Number((base+dir*st).toFixed(5))));
        input.value=String(next);apply(next);
      };
      const arrows=make("span","astra-ms-spin-arrows");
      for(const [dir,icon,t] of [[1,"SMALL_ARROW_UP","增加"],[-1,"SMALL_ARROW_DOWN","减少"]]){
        const b=make("button");b.type="button";b.tabIndex=-1;b.title=t;b.setAttribute("aria-label",name+t);b.append(glyph(icon));
        b.disabled=!row.enabled;b.addEventListener("click",()=>step(dir));arrows.append(b);
      }
      wrap.append(input);
      if(row.unit)wrap.append(make("span","astra-ms-spin-unit",row.unit));
      wrap.append(arrows);
      return wrap;
    }
    function resetButton(row,name){
      const reset=make("button","astra-inspector-reset");reset.type="button";reset.append(glyph("UNDO"));
      reset.setAttribute("aria-label","恢复 "+name+" 默认值");reset.title="恢复默认值";
      reset.disabled=!row.enabled||!row.resettable;
      reset.addEventListener("click",()=>edit(row,undefined,true));
      return reset;
    }
    // One MuseScore property control: title above, control, reset at the right.
    function field(row) {
      const name=label(row);
      const el=make("div","astra-inspector-field");
      el.dataset.property=row.field;el.dataset.type=row.type;
      const controls=make("div","astra-inspector-control");
      if(row.type==="boolean") {
        const box=make("label","astra-ms-check");
        const input=make("input");
        input.type="checkbox";input.checked=row.value===true;input.indeterminate=row.mixed;input.disabled=!row.enabled;
        input.addEventListener("change",()=>edit(row,input.checked));
        box.append(input,make("span","",name));controls.append(box);
      } else {
        el.append(make("div","astra-inspector-label",name));
        if(row.type==="number") controls.append(spin(row,name,row.value,n=>edit(row,n)));
        else if(row.type==="point") {
          const pair=make("div","astra-ms-pair");
          let x=row.value?.x,y=row.value?.y;
          const send=()=>{if(valueRange(row,x)&&(!row.vertical||valueRange(row,y)))edit(row,row.vertical?{x,y}:{x});};
          const hx=make("label","astra-ms-pair-item");hx.append(make("span","","水平"),spin(row,name+" 水平",x,n=>{x=n;send();}));
          pair.append(hx);
          if(row.vertical){const vy=make("label","astra-ms-pair-item");vy.append(make("span","","垂直"),spin(row,name+" 垂直",y,n=>{y=n;send();}));pair.append(vy);}
          controls.append(pair);
        } else if(row.type==="choice") {
          const select=make("select","astra-ms-dropdown");
          select.setAttribute("aria-label",name);
          if(!row.choices.some(c=>c.value===row.value)){
            const placeholder=make("option","",row.mixed?"混合值":"请选择");
            placeholder.value="";placeholder.disabled=true;placeholder.selected=true;select.append(placeholder);
          }
          for(const choice of row.choices){
            const option=make("option","",choice.label);
            option.value=String(choice.value);option.selected=choice.value===row.value;select.append(option);
          }
          select.disabled=!row.enabled;
          select.addEventListener("change",()=>{
            const v=Number(select.value);
            if(select.value!==""&&row.choices.some(c=>c.value===v))edit(row,v);
          });
          controls.append(select);
        } else if(row.type==="color") {
          // MuseScore's ColorPicker: a swatch; the alpha channel is kept.
          const value=typeof row.value==="string"&&/^#[a-fA-F0-9]{8}$/.test(row.value)?row.value:"#ff000000";
          const alpha=value.slice(1,3), rgb="#"+value.slice(3);
          const swatch=make("label","astra-ms-color");
          const picker=make("input");picker.type="color";picker.value=rgb;picker.disabled=!row.enabled;
          picker.setAttribute("aria-label",name);
          const chip=make("span","astra-ms-color-chip");chip.style.background=row.mixed?"repeating-linear-gradient(45deg,#ccc 0 4px,#fff 4px 8px)":rgb;
          picker.addEventListener("change",()=>edit(row,"#"+alpha+picker.value.slice(1)));
          swatch.append(chip,picker);controls.append(swatch);
        } else {
          const input=make("input");
          input.type="text";input.setAttribute("aria-label",name);input.maxLength=512;
          input.value=typeof row.value==="string"?row.value:"";input.placeholder=row.mixed?"混合值":"";
          input.disabled=!row.enabled;
          input.addEventListener("change",()=>edit(row,input.value));
          controls.append(input);
        }
      }
      controls.append(resetButton(row,name));
      el.append(controls);
      return el;
    }
    function expander(title,body,initiallyOpen,level){
      const closed=collapsed.has(title)?collapsed.get(title):!initiallyOpen;
      const box=make("section","astra-ms-section"+(level?" is-sub":""));
      const head=make("button","astra-ms-section-head");head.type="button";
      head.setAttribute("aria-expanded",String(!closed));
      head.append(glyph(closed?"SMALL_ARROW_RIGHT":"SMALL_ARROW_DOWN"),make("span","",title));
      body.hidden=closed;
      head.addEventListener("click",()=>{
        const now=!body.hidden;body.hidden=now;collapsed.set(title,now);
        head.setAttribute("aria-expanded",String(!now));
        head.firstChild.replaceWith(glyph(now?"SMALL_ARROW_RIGHT":"SMALL_ARROW_DOWN"));
      });
      box.append(head,body);
      return box;
    }
    function place(row,into){const el=field(row);built.set(row.id,{key:JSON.stringify(row),el});into.append(el);}
    // MuseScore Inspector layout: one collapsible block per top-level section;
    // General shows its four flags as a 2x2 grid and Playback / Appearance as
    // closed sub-blocks; a section with several element models shows tabs.
    function build(rows){
      const groups=new Map();
      for(const row of rows){
        const g=row.group||row.section||"属性";
        if(!groups.has(g))groups.set(g,{own:[],subs:new Map(),general:false});
        const grp=groups.get(g);
        if(!row.depth){grp.own.push(row);if(GENERAL_FLAGS.includes(row.field))grp.general=true;}
        else{if(!grp.subs.has(row.section))grp.subs.set(row.section,[]);grp.subs.get(row.section).push(row);}
      }
      const order=[...groups.entries()].sort((a,b)=>Number(b[1].general)-Number(a[1].general));
      for(const [title,grp] of order){
        const body=make("div","astra-ms-section-body");
        const flags=grp.own.filter(r=>r.type==="boolean"&&GENERAL_FLAGS.includes(r.field))
          .sort((a,b)=>GENERAL_FLAGS.indexOf(a.field)-GENERAL_FLAGS.indexOf(b.field));
        if(flags.length){const grid=make("div","astra-ms-flag-grid");for(const r of flags)place(r,grid);body.append(grid);}
        for(const r of grp.own)if(!flags.includes(r))place(r,body);
        // NoteSettingsProxyModel order: notehead, chord, stem, beam, hook.
        const TAB_ORDER=["符头","和弦","符干","符杠","符尾"];
        const rank=n=>{const i=TAB_ORDER.indexOf(n);return i<0?TAB_ORDER.length:i;};
        const subs=[...grp.subs.entries()].sort((a,b)=>rank(a[0])-rank(b[0]));
        if(grp.general){
          for(const [sub,list] of subs){const b=make("div","astra-ms-section-body");for(const r of list)place(r,b);body.append(expander(sub,b,false,1));}
        }else if(subs.length>1){
          const bar=make("div","astra-ms-tabs");bar.setAttribute("role","tablist");
          const pages=[];
          const pick=activeTab.get(title)&&subs.some(([n])=>n===activeTab.get(title))?activeTab.get(title):subs[0][0];
          for(const [sub,list] of subs){
            const tab=make("button","astra-ms-tab",sub);tab.type="button";tab.setAttribute("role","tab");
            tab.setAttribute("aria-selected",String(sub===pick));
            const page=make("div","astra-ms-tab-page");page.hidden=sub!==pick;for(const r of list)place(r,page);
            tab.addEventListener("click",()=>{activeTab.set(title,sub);
              for(const t of bar.children)t.setAttribute("aria-selected",String(t===tab));
              pages.forEach(p=>{p.hidden=p!==page;});});
            bar.append(tab);pages.push(page);
          }
          body.append(bar,...pages);
        }else if(subs.length===1){for(const r of subs[0][1])place(r,body);}
        content.append(expander(title,body,true,0));
      }
    }
    function paint() {
      if(!content)return;
      // Clearing an already empty (often hidden) panel used to read scrollTop,
      // forcing a synchronous layout of the whole page after every note.
      if(!active()&&!content.firstChild){paintAvailability();return;}
      const shapeOf=rows=>rows.map(r=>[r.group,r.depth,r.section,r.id,r.type,r.name,r.field].join("\u0001")).join("\u0002");
      if(active()&&content.firstChild){
        if(shapeOf(current.rows)===builtShape){
          for(const row of current.rows){
            const key=JSON.stringify(row),was=built.get(row.id);
            if(!was)continue;
            if(was.key===key){was.stale=null;continue;}
            // Never rewrite the box being typed in; apply the engine value on leaving it.
            if(was.el.contains(document.activeElement)){was.stale=row;continue;}
            const el=field(row);was.el.replaceWith(el);built.set(row.id,{key,el});
          }
          paintAvailability();return;
        }
      }
      const old=content.firstChild&&!panel.hidden?content.scrollTop:0;
      content.replaceChildren();built=new Map();builtShape="";
      if(active()){builtShape=shapeOf(current.rows);build(current.rows);}
      if(old)content.scrollTop=old;
      paintAvailability();
    }
    function mount(root){
      if(!root||root===bound)return;
      bound=root;area=root.querySelector(".astra-native-workspace");
      if(!area)return;
      toggle=root.querySelector("[role=tab][data-score-dock=properties]");
      toggle.setAttribute("aria-controls","astra-native-inspector");
      panel=make("aside","astra-inline-inspector");panel.id="astra-native-inspector";
      panel.tabIndex=-1;panel.setAttribute("aria-label","乐谱属性");
      headline=make("header","astra-inspector-head");content=make("div","astra-inspector-fields");
      notice=make("p","astra-inspector-notice");notice.setAttribute("role","status");
      const close=btn("×",()=>{bound.querySelector("[role=tab][data-score-dock=palettes]").click();});
      close.setAttribute("aria-label","关闭属性面板");
      headline.append(make("h3","","乐谱属性"),close);
      panel.append(headline,content,notice);root.querySelector(".astra-ms-inspector-dock").append(panel);
      content.addEventListener("focusout",()=>setTimeout(()=>{
        for(const [id,was] of built){
          if(!was.stale||was.el.contains(document.activeElement))continue;
          const row=was.stale, el=field(row);
          was.el.replaceWith(el);built.set(id,{key:JSON.stringify(row),el});
        }
      },0));
      panel.addEventListener("keydown",e=>{
        e.stopPropagation();
        if(e.key==="Escape"){e.preventDefault();bound.querySelector("[role=tab][data-score-dock=palettes]").click();}
      });
      paint();
    }
    function update(root,next) {
      const prior=stamp();
      const priorDocument=JSON.stringify([state.project,state.documentGeneration]);
      state=next;mount(root);
      if(prior!==stamp()){
        loading=null;report="";retryAfter=0;
        if(priorDocument!==JSON.stringify([state.project,state.documentGeneration])||!allowed()){
          current=null;paint();
        }else{
          // Keep the panel visible while its old descriptors are inert. The
          // native redraw doesn't flash or displace either panel or score.
          paintAvailability();
        }
      }else paintAvailability();
      schedule();
    }
    function receive(kind,response) {
      if(kind==="properties"){
        const req=loading;
        loading=null;
        if(req!==stamp()){schedule();return;}
        if(response.error||!Array.isArray(response.properties)){
          retryAfter=Infinity;report=clean(response.error)||"未能读取原生谱面属性。";paint();return;
        }
        const rows=[];
        for(const row of response.properties.slice(0,250)){
          if(!row || !/^\d{1,10}:\d{1,4}$/.test(row.id||"")||!tags.has(row.type)||!clean(row.field))continue;
          const choices=Array.isArray(row.choices)?row.choices.filter(v=>v&&Number.isSafeInteger(v.value)&&clean(v.label)).slice(0,150):[];
          if(row.type==="choice"&&!choices.length)continue;
          if((row.type==="number"||row.type==="point")&&(!Number.isFinite(row.min)||!Number.isFinite(row.max)||row.min>row.max))continue;
          rows.push({...row,section:clean(row.section),group:clean(row.group),depth:Number.isInteger(row.depth)?row.depth:0,
            name:clean(row.name),field:clean(row.field),unit:clean(row.unit,12),choices});
        }
        current={stamp:req,rows};paint();
      } else if(kind==="property-set"&&response.error){
        report=clean(response.error);paintAvailability();
      }
    }
    return {update,receive,setShown(next){shown=next;report="";retryAfter=0;paintAvailability();schedule();}};
  }
  return {create};
})();
