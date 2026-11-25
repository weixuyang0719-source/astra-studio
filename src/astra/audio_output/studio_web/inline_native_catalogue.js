"use strict";

// The catalog and glyphs are native PaletteCreator + PaletteCellIconEngine.
// The browser owns only the left dock state and QML-equivalent grid geometry.
window.AstraNativeCatalogue = (() => {
  const dragType="application/x-astra-native-pal-cell";
  const validId=x=>typeof x==="string"&&/^\d{1,5}(?::\d{1,5})?$/.test(x);
  const png=x=>typeof x==="string"&&x.startsWith("iVBOR")&&x.length<=60_000&&/^[A-Za-z0-9+/]+={0,2}$/.test(x);
  const clean=x=>typeof x==="string"?x.trim().slice(0,180):"";
  const lookup={"Clefs":"clefs","Key signatures":"keysig","Time signatures":"timesig","Tempo":"tempo",
    "Pitch":"pitch","Accidentals":"accidentals","Dynamics":"dynamics","Articulations":"articulations",
    "Text":"text","Keyboard":"keyboard","Repeats & jumps":"repeats","Barlines":"barlines","Layout":"layout"};
  const stock=window.AstraMuseScoreStock;
  const VISIBILITY_KEY="astra-score-piano-palette-visibility-v1";
  const HEADING_CLICK_KEY="astra-score-palette-single-click-v1";
  const reducedMotion=()=>matchMedia("(prefers-reduced-motion: reduce)").matches;
  const softDialogs=()=>!reducedMotion()&&CSS.supports("transition-behavior","allow-discrete");
  const paletteType=x=>typeof x==="string"&&/^[A-Za-z][A-Za-z0-9]{0,31}$/.test(x);
  const node=(tag,cls,text)=>{
    const el=document.createElement(tag);
    if(cls)el.className=cls;
    if(text!=null)el.textContent=text;
    return el;
  };
  function icon(name){
    const i=node("span","astra-ms-glyph",String.fromCodePoint(stock.iconCodes[name]));
    i.dataset.musescoreIcon=name;i.setAttribute("aria-hidden","true");return i;
  }
  function button(cls,title,text){
    const b=node("button",cls,text);b.type="button";if(title){b.title=title;b.setAttribute("aria-label",title);}return b;
  }

  function create({send}) {
    let aside,pane,search,notice,paper,image,observe,resize,state={},groups=null,failed="";
    let source=null,requested=null,wantsMore=null,pendingIcons=[],firstReadAfter=0,wake=null,intended=null;
    let width=0,dialog=null,dialogAnchor=null,dialogReturn=null,one=false,singleClick=true,modeHint=false;
    const open=new Set(), cards=new Map(), namedVisible=new Map(), moreByGroup=new Map(), additions=new Map(), ids=new Map();
    try {
      singleClick=localStorage.getItem(HEADING_CLICK_KEY)!=="false";
      for (const [type,show] of JSON.parse(localStorage.getItem(VISIBILITY_KEY)||"[]"))
        if(paletteType(type)&&typeof show==="boolean")namedVisible.set(type,show);
    } catch {}
    const icons=new Map(), visible=new Map();
    const available=()=>state.ready&&state.editing&&!state.input;

    function announce(text) {
      if(notice){notice.hidden=!text;notice.textContent=text||"";}
    }
    function pulse(delay=0) {
      if(wake!==null)return;
      wake=setTimeout(()=>{wake=null;pump();},delay);
    }
    function attachGlyph(b) {
      const src=icons.get(b.dataset.paintKey);
      if(typeof src!=="string"||b.querySelector("img"))return;
      const im=node("img","astra-native-catalogue-icon");
      im.alt="";im.setAttribute("aria-hidden","true");im.draggable=false;
      im.style.width=b.dataset.paintWidth+"px";
      im.style.height=b.dataset.paintHeight+"px";
      im.style.objectFit="fill";
      im.src="data:image/png;base64,"+src;
      b.append(im);b.classList.add("has-native-icon");
    }
    function pump() {
      if(!aside?.isConnected||!state.ready||state.waiting)return;
      if(intended){
        const next=intended;intended=null;
        if(available()&&next.source===source&&next.revision===state.revision&&next.page===state.page){
          if(send(next.kind,next.args))return;
          // The worker is answering something else (e.g. a background page
          // paint): keep the person's click and send it as soon as it's free.
          intended=next;pulse(16);return;
        }
      }
      if(!groups&&!requested&&!failed){
        const wait=firstReadAfter-performance.now();
        if(wait>0){pulse(wait);return;}
        requested="default";
        if(!send("palette-catalog"))requested=null;
        return;
      }
      if(!groups||failed||requested||pendingIcons.length)return;
      if(wantsMore&&!moreByGroup.has(wantsMore.id)) {
        requested=wantsMore.id;
        if(!send("palette-catalog",{more:requested}))requested=null;
        return;
      }
      const used=new Set(), batch=[];
      for(const [key,b] of visible) {
        const id=b.dataset.nativePaletteId;
        if(!b.isConnected||icons.has(key)||used.has(id))continue;
        used.add(id);
        batch.push({key,id,width:Number(b.dataset.paintWidth),height:Number(b.dataset.paintHeight),rasterScale:Number(b.dataset.paintScale)});
        if(batch.length===12)break;
      }
      if(batch.length) {
        pendingIcons=batch;
        if(!send("palette-icons",{ids:batch.map(({id,width,height,rasterScale})=>({id,width,height,rasterScale}))}))pendingIcons=[];
      }
    }
    const visibleGroup=g=>namedVisible.get(g.type)??g.visible;
    function selectVisibility(g,show,persist=true) {
      if(show===g.visible)namedVisible.delete(g.type);else namedVisible.set(g.type,show);
      if(!show)open.delete(g.id);
      if(persist)try{localStorage.setItem(VISIBILITY_KEY,JSON.stringify([...namedVisible]));}catch{}
    }
    const activeCells=g=>[...g.cells,...(additions.get(g.id)||[])];
    function parseCells(rows) {
      const cells=[];
      for(const r of Array.isArray(rows)?rows:[]){
        if(!r||!validId(r.id)||!r.id.includes(":")||!clean(r.name))continue;
        const kind=clean(r.kind);
        let name=clean(r.name),sourceName=clean(r.sourceName);
        // The zh_CN translator uses the bracket caption for both "Line"s.
        // Correct only the free TextLine caption, never the actual Bracket.
        if(kind==="TextLine"&&sourceName==="Line"&&name==="线条连谱号")name="普通线条";
        // Native Fingering names are parameterized; search the resolved name.
        const finger=kind==="Fingering"?/(?:^|:)([1-5])$/.exec(r.id)?.[1]:null;
        if(finger)sourceName=sourceName.replace("%1",finger);
        const entry={id:r.id,name,sourceName,kind,enabled:r.enabled===true};
        cells.push(entry);ids.set(entry.id,entry);
      }
      return cells;
    }
    function fillGrid(grid,g,rows,showMore,panelWidth,onCell) {
      // PaletteGridView.qml and utils.js: fixed widths if all cells plus
      // More fit on one row; otherwise divide into native-sized columns.
      const width=Math.max(g.width, panelWidth-2);
      const cols=Math.max(1,Math.floor(width/g.width));
      const gaps=0; // grid strokes are part of the cell rect, not layout gaps
      const cellWidth=showMore&&g.width*rows.length+64<width?g.width:Math.floor((width-gaps)/cols);
      const last=width-(cols-1)*cellWidth;
      const moreSpan=showMore?Math.min(cols,Math.max(1,Math.ceil((64-last)/cellWidth)+1)):0;
      const rowCount=Math.max(1,Math.ceil((rows.length+moreSpan)/cols));
      grid.style.gridTemplateColumns=cols>1?"repeat("+(cols-1)+","+cellWidth+"px) minmax(0,1fr)":"minmax(0,1fr)";
      grid.style.gridAutoRows=g.height+"px";
      grid.dataset.columns=String(cols);grid.dataset.cellWidth=String(g.width);grid.dataset.cellHeight=String(g.height);
      if(!g.drawGrid)grid.classList.add("is-no-grid");
      function viewCell(c,i) {
        const w=i%cols===cols-1?last:cellWidth;
        const b=button("astra-native-catalogue-cell",c.name);
        b.dataset.nativePaletteId=c.id;b.dataset.sourceName=c.sourceName;
        b.dataset.cellEnabled=String(c.enabled);
        b.dataset.paintWidth=String(Math.max(20,w-(g.drawGrid?1:0)));
        b.dataset.paintHeight=String(Math.max(20,g.height-(g.drawGrid?1:0)));
        const dpr=window.devicePixelRatio||1;
        b.dataset.paintScale=String(dpr<=1.02?1:dpr<=1.52?1.5:2);
        b.dataset.paintKey=[c.id,b.dataset.paintWidth,b.dataset.paintHeight,b.dataset.paintScale].join("/");
        if(!c.enabled)b.disabled=true;
        // MuseScore's PaletteGridView.qml suppresses the second click of
        // a double-click. Preserve keyboard activation (detail === 0).
        b.addEventListener("click",e=>{if(e.detail<=1)onCell(b,c,e);});
        b.addEventListener("dragstart",e=>{
          if(!c.enabled||!available()||!e.dataTransfer){e.preventDefault();return;}
          e.dataTransfer.setData(dragType,c.id);e.dataTransfer.effectAllowed="copy";
        });
        attachGlyph(b);
        grid.append(b);
        if(observe)observe.observe(b);else visible.set(b.dataset.paintKey,b);
      }
      rows.forEach(viewCell);
      for(let n=Math.max(0,rowCount*cols-moreSpan-rows.length);n>0;n--){
        const cell=node("div","pl-empty");cell.setAttribute("aria-hidden","true");grid.append(cell);
      }
      if(!showMore)return;
      const extra=button("astra-ms-palette-more",stock.labels.more,stock.labels.more);
      extra.setAttribute("aria-haspopup","dialog");extra.style.gridColumn="span "+moreSpan;
      extra.addEventListener("click",()=>showMoreDialog(g,extra));
      grid.append(extra);
    }
    function apply(c,e) {
      if(!c.enabled)return;
      if(!available()){
        if(state.ready){
          modeHint=true;
          announce(!state.editing?"请先点「编辑乐谱」，再应用符号。":"请先完成文字编辑，再应用符号。");
        }
        return;
      }
      if(modeHint){modeHint=false;announce("");}
      intended={kind:"palette-apply",args:{id:c.id,control:e.ctrlKey},source,revision:state.revision,page:state.page};
      pulse();
      // As in MuseScore, a mouse click hands the keyboard back to the score,
      // so note names typed next keep entering notes (keyboard use keeps focus).
      if(e.detail>=1&&!dialog?.contains(e.target))paper?.focus({preventScroll:true});
    }
    function cardState(card,expanded,animate=true) {
      const {g,section,toggle,expander,clip,results,query,paneWidth}=card;
      const prior=section.classList.contains("is-open");
      // Build nothing for initially closed or unchecked panels. Once opened,
      // leave the painted grid in place so reversal and re-open don't repaint.
      if(expanded&&!clip.firstElementChild) {
        const grid=node("div","astra-native-palette-grid astra-native-catalogue-grid");
        grid.setAttribute("role","group");grid.setAttribute("aria-label",g.local);
        fillGrid(grid,g,results,!query&&g.more,Math.max(120,paneWidth-10),(b,c,e)=>apply(c,e));
        clip.append(grid);
        if(animate&&!reducedMotion())expander.getBoundingClientRect();
      }
      expander.inert=!expanded;
      expander.setAttribute("aria-hidden",String(!expanded));
      toggle.setAttribute("aria-expanded",String(expanded));
      const arrow=toggle.firstElementChild, iconName=expanded?"SMALL_ARROW_DOWN":"SMALL_ARROW_RIGHT";
      arrow.dataset.musescoreIcon=iconName;arrow.textContent=String.fromCodePoint(stock.iconCodes[iconName]);
      section.classList.toggle("is-open",expanded);
      expander.classList.toggle("is-expanded",expanded);
      if(prior!==expanded)for(const b of clip.querySelectorAll("[data-paint-key]")){
        if(expanded) {
          if(observe)observe.observe(b);else visible.set(b.dataset.paintKey,b);
        }else{
          observe?.unobserve(b);
          if(visible.get(b.dataset.paintKey)===b)visible.delete(b.dataset.paintKey);
        }
      }
      if(expanded){syncCells();pulse();}
    }
    function toggleCard(card) {
      const id=card.g.id,expand=!open.has(id);
      if(expand) {
        if(one)for(const other of [...open]){
          open.delete(other);
          if(cards.has(other))cardState(cards.get(other),false);
        }
        open.add(id);
      }else open.delete(id);
      cardState(card,expand);
      card.toggle.focus({preventScroll:true});
    }
    function repaint() {
      if(!aside?.isConnected)return;
      const scroll=pane.scrollTop,paneWidth=pane.clientWidth;
      for(const b of pane.querySelectorAll("[data-paint-key]")){
        observe?.unobserve(b);
        if(visible.get(b.dataset.paintKey)===b)visible.delete(b.dataset.paintKey);
      }
      pane.replaceChildren();cards.clear();
      if(!groups){announce(failed);return;}
      const query=search.value.trim().toLocaleLowerCase();
      const selected=g=>[g.name,g.local].some(n=>n.toLocaleLowerCase().includes(query));
      let matched=false;
      for(const g of groups) {
        const rows=activeCells(g);
        if(!visibleGroup(g)&&!query)continue;
        const results=query?rows.filter(c=>c.enabled&&(selected(g)||[c.name,c.sourceName].some(n=>n.toLocaleLowerCase().includes(query)))):rows;
        if(query&&!results.length)continue;
        matched=true;
        const expanded=!!query||open.has(g.id);
        const section=node("section","astra-native-palette-group");
        if(g.key)section.dataset.paletteKey=g.key;
        section.dataset.paletteGroup=g.id;
        section.dataset.paletteType=g.type;
        const heading=node("h3","astra-ms-palette-heading");
        const toggle=button("astra-native-palette-head","");
        const expander=node("div","astra-ms-palette-expander");
        const clip=node("div","astra-ms-palette-clip");
        expander.id="native-score-catalogue-group-"+g.id;
        expander.append(clip);
        toggle.setAttribute("aria-controls",expander.id);
        toggle.append(icon("SMALL_ARROW_RIGHT"),node("span","astra-native-catalogue-group-name",g.local));
        const card={g,section,toggle,expander,clip,results,query,paneWidth};
        cards.set(g.id,card);
        const arrow=toggle.firstElementChild;
        toggle.addEventListener("click",e=>{
          if(!query&&e.detail<=1&&(singleClick||e.detail===0||arrow.contains(e.target)))toggleCard(card);
        });
        toggle.addEventListener("dblclick",e=>{
          if(!query&&!singleClick&&!arrow.contains(e.target))toggleCard(card);
        });
        heading.append(toggle);
        if(!query){
          const options=button("astra-ms-palette-group-options","面板选项");
          options.append(icon("MENU_THREE_DOTS"));options.setAttribute("aria-haspopup","dialog");
          options.setAttribute("aria-expanded","false");
          options.addEventListener("click",()=>showPanelOptions(g,options));
          // TreePaletteHeader.qml supplies the same menu model to its
          // right-click area and ellipsis, not to notation-cell clicks.
          heading.addEventListener("contextmenu",e=>{
            e.preventDefault();e.stopPropagation();
            showPanelOptions(g,options,{point:[e.clientX,e.clientY],restore:toggle});
          });
          heading.addEventListener("keydown",e=>{
            if(!((e.key==="F10"&&e.shiftKey)||e.key==="ContextMenu"))return;
            e.preventDefault();e.stopPropagation();
            showPanelOptions(g,options,{restore:toggle});
          });
          heading.append(options);
        }
        section.append(heading,expander);
        cardState(card,expanded,false);
        pane.append(section);
      }
      pane.scrollTop=scroll;
      announce(query&&!matched?"未找到结果":"");
      syncCells();
      pulse();
    }
    function syncCells() {
      for(const b of aside?.querySelectorAll("[data-native-palette-id]")||[]) {
        const selecting=dialog?.contains(b);
        b.setAttribute("aria-disabled",String(b.disabled||(!selecting&&!available())));
        b.draggable=!b.disabled&&available();
      }
    }
    function closeDialog(restore=true) {
      if(!dialog)return;
      const old=dialog,anchor=dialogAnchor,back=dialogReturn;
      dialog=null;dialogAnchor=null;dialogReturn=null;wantsMore=null;
      for(const b of old.querySelectorAll("[data-paint-key]")){observe?.unobserve(b);visible.delete(b.dataset.paintKey);}
      const animate=softDialogs()&&old.open;
      if(old.open)old.close();
      if(animate){
        const remove=()=>old.remove();
        old.addEventListener("transitionend",e=>{if(e.target===old&&e.propertyName==="opacity")remove();});
        setTimeout(remove,190);
      }else old.remove();
      if(anchor?.hasAttribute("aria-expanded"))anchor.setAttribute("aria-expanded","false");
      if(restore&&back?.isConnected)back.focus({preventScroll:true});
      pulse();
    }
    function popup(title,from,{point,restore=from}={}) {
      closeDialog(false);
      dialogAnchor=from;dialogReturn=restore;
      const d=dialog=node("dialog","astra-ms-palette-popup");
      const h=node("header",null);
      const close=button("astra-ms-palette-close","关闭");close.append(icon("CLOSE_X_ROUNDED"));
      close.addEventListener("click",()=>closeDialog());
      h.append(node("span",null,title),close);
      d.append(h,node("div","astra-ms-popup-body"));
      d.setAttribute("aria-label",title);
      d.addEventListener("cancel",e=>{e.preventDefault();closeDialog();});
      d.addEventListener("click",e=>{
        if(e.target!==d)return;
        const r=d.getBoundingClientRect();
        if(e.clientX<r.left||e.clientX>r.right||e.clientY<r.top||e.clientY>r.bottom)closeDialog();
      });
      if(from.hasAttribute("aria-expanded"))from.setAttribute("aria-expanded","true");
      aside.append(d);d.showModal();
      const r=from.getBoundingClientRect();
      const [x,y]=point||[r.left,r.bottom+4];
      d.style.left=Math.max(0,Math.min(x,innerWidth-d.offsetWidth))+"px";
      d.style.top=Math.max(8,Math.min(y,innerHeight-d.offsetHeight-8))+"px";
      return d.querySelector(".astra-ms-popup-body");
    }
    function fillMore(g) {
      if(!dialog||wantsMore!==g)return;
      const body=dialog.querySelector(".astra-ms-popup-body");
      for(const b of body.querySelectorAll("[data-paint-key]")){observe?.unobserve(b);visible.delete(b.dataset.paintKey);}
      body.replaceChildren();
      if(!moreByGroup.has(g.id)){body.append(node("p",null,"正在读取…"));return;}
      const added=new Set((additions.get(g.id)||[]).map(c=>c.id));
      const more=moreByGroup.get(g.id).filter(c=>!added.has(c.id));
      if(!more.length){body.append(node("p",null,"没有可添加的符号"));return;}
      const marked=new Set(), grid=node("div","astra-native-palette-grid astra-native-catalogue-grid");
      const add=button("astra-ms-palette-popup-add","添加到 "+g.local,"添加到 "+g.local);
      add.disabled=true;
      fillGrid(grid,g,more,false,Math.max(120,body.clientWidth-10),(b,c)=>{
        if(!c.enabled)return;
        if(marked.has(c.id))marked.delete(c.id);else marked.add(c.id);
        b.setAttribute("aria-pressed",String(marked.has(c.id)));add.disabled=!marked.size;
      });
      add.addEventListener("click",()=>{
        additions.set(g.id,[...(additions.get(g.id)||[]),...more.filter(c=>marked.has(c.id))]);
        closeDialog(false);repaint();pane.querySelector('[aria-controls="native-score-catalogue-group-'+g.id+'"]')?.focus({preventScroll:true});
      });
      body.append(grid,add);
      syncCells();pulse();
    }
    function showMoreDialog(g,from) {
      popup(g.local,from);wantsMore=g;fillMore(g);pulse();
    }
    function showManager(from) {
      const body=popup("面板管理",from);
      const d=body.closest("dialog");
      d.classList.add("astra-ms-panel-manager");
      for(const g of groups||[]) {
        const row=node("label","astra-ms-panel-toggle");
        const box=node("input");
        box.type="checkbox";box.checked=visibleGroup(g);box.dataset.paletteToggle=g.type;
        const mark=node("span","astra-ms-panel-tick");
        mark.setAttribute("aria-hidden","true");
        mark.append(icon("TICK"));
        row.append(box,mark,node("span","astra-ms-panel-label",g.local));
        box.addEventListener("change",()=>{
          selectVisibility(g,box.checked);
          // Reconcile the sidebar only, not the open list or focus/scroll.
          repaint();
        });
        body.append(row);
      }
      const rect=from.getBoundingClientRect();
      d.style.top=Math.max(8,Math.min(rect.bottom+4,innerHeight-d.offsetHeight-8))+"px";
      body.querySelector("input")?.focus({preventScroll:true});
    }
    function menuItem(body,title,handler,{checked,restore}={}) {
      const item=button("astra-ms-palette-menu-item",title,(checked?"✓ ":"")+title);
      if(checked!==undefined)item.setAttribute("aria-pressed",String(checked));
      item.addEventListener("click",()=>{
        closeDialog(false);handler();repaint();
        const back=typeof restore==="function"?restore():restore;
        back?.focus({preventScroll:true});
      });body.append(item);
    }
    function showPanelOptions(g,from,opener={}) {
      if(dialog&&dialogAnchor===from)return;
      const body=popup(g.local,from,opener);
      menuItem(body,"隐藏 "+g.local,()=>selectVisibility(g,false),{
        restore:()=>pane.querySelector(".astra-native-palette-head")||aside.querySelector("[data-palette-manage]")});
      // Account for the buttons inserted after the initial anchor calculation.
      if(opener.point){
        const d=body.closest("dialog");
        const [x,y]=opener.point;
        d.style.left=Math.max(0,Math.min(x,innerWidth-d.offsetWidth))+"px";
        d.style.top=Math.max(8,Math.min(y,innerHeight-d.offsetHeight-8))+"px";
      }
    }
    function showSettings(from) {
      const body=popup("符号面板",from);
      menuItem(body,stock.labels.expand,()=>{for(const g of groups||[])if(visibleGroup(g))open.add(g.id);},{restore:from});
      menuItem(body,stock.labels.collapse,()=>open.clear(),{restore:from});
      menuItem(body,stock.labels.one,()=>{one=!one;},{checked:one,restore:from});
      menuItem(body,"单击打开面板",()=>{
        singleClick=!singleClick;
        try{localStorage.setItem(HEADING_CLICK_KEY,String(singleClick));}catch{}
      },{checked:singleClick,restore:from});
    }
    function keyboardGrid(e) {
      if(e.ctrlKey||e.shiftKey||e.altKey||e.metaKey)return false;
      const group=e.target.closest("[data-palette-group]"), key=e.key;
      if(!group||!["ArrowUp","ArrowDown","ArrowLeft","ArrowRight"].includes(key))return false;
      const head=group.querySelector(".astra-native-palette-head");
      const grid=group.querySelector(".astra-ms-palette-expander.is-expanded .astra-native-catalogue-grid");
      const cells=grid?[...grid.querySelectorAll(".astra-native-catalogue-cell:not(:disabled),.astra-ms-palette-more")]:[];
      const i=cells.indexOf(e.target);
      let target=null;
      if(e.target===head){
        if(key==="ArrowRight")target=cells[0];
        else if(key==="ArrowUp"||key==="ArrowDown"){
          const headings=[...pane.querySelectorAll(".astra-native-palette-head")];
          target=headings[headings.indexOf(head)+(key==="ArrowUp"?-1:1)];
        }else return false;
      }else if(i!==-1){
        if(key==="ArrowLeft")target=head;
        else if(key==="ArrowUp"||key==="ArrowDown")target=cells[i+(key==="ArrowUp"?-1:1)];
        else return false;
      }else return false;
      if(target){
        target.focus({preventScroll:true});
        target.scrollIntoView({block:"nearest",inline:"nearest"});
      }
      return true;
    }
    function bind(root) {
      const target=root?.querySelector(".astra-native-palette");
      if(!target||target===aside)return;
      closeDialog(false);observe?.disconnect();resize?.disconnect();visible.clear();
      aside=target;pane=aside.querySelector(".astra-native-palette-scroll");
      search=aside.querySelector(".astra-native-catalogue-search");
      notice=aside.querySelector(".astra-native-catalogue-notice");
      paper=root.querySelector(".astra-native-paper");image=paper?.querySelector("canvas.astra-native-live")||paper?.querySelector("canvas");
      aside.querySelector("[data-palette-manage]").addEventListener("click",e=>showManager(e.currentTarget));
      aside.querySelector("[data-palette-settings]").addEventListener("click",e=>showSettings(e.currentTarget));
      search.addEventListener("input",repaint);
      search.addEventListener("keydown",e=>{
        e.stopPropagation();
        if(e.key==="ArrowDown"&&search.value.trim()){
          const first=pane.querySelector(".astra-native-catalogue-cell:not(:disabled)");
          if(first){e.preventDefault();first.focus();}
        }
      });
      pane.addEventListener("keydown",e=>{
        const searchRow=aside.querySelector(".astra-native-palette-search");
        if(e.key==="Escape"&&searchRow&&!searchRow.hidden){
          e.preventDefault();e.stopPropagation();
          aside.querySelector(".astra-native-palette-search-toggle")?.click();
          return;
        }
        if(keyboardGrid(e)){e.preventDefault();e.stopPropagation();return;}
        if(!state.editing||state.input||!e.ctrlKey||e.altKey||e.metaKey||e.repeat)return;
        const key=e.key.toLowerCase();
        const kind=key==="z"?(e.shiftKey?"redo":"undo"):(key==="y"&&!e.shiftKey?"redo":"");
        if(!kind)return;
        // Match the score's existing history command without stealing text
        // Undo from the separate search field or numeric entries.
        const cmd=root.querySelector(".astra-native-history [data-stock-action="+kind+"]");
        if(cmd&&!cmd.disabled){
          e.preventDefault();e.stopPropagation();cmd.click();
        }
      });
      if(typeof IntersectionObserver==="function"){
        observe=new IntersectionObserver(entries=>{
          for(const en of entries){
            const k=en.target.dataset.paintKey;
            const expander=en.target.closest(".astra-ms-palette-expander");
            if(en.isIntersecting&&en.intersectionRatio>0&&en.target.isConnected&&(!expander||expander.classList.contains("is-expanded")))
              visible.set(k,en.target);
            else if(visible.get(k)===en.target)visible.delete(k);
          }
          pulse();
        },{root:null});
      }
      if(typeof ResizeObserver==="function"){
        resize=new ResizeObserver(()=>{if(pane.clientWidth>0&&pane.clientWidth!==width){width=pane.clientWidth;repaint();}});
        resize.observe(pane);
      }
      if(!paper||!image)return;
      paper.addEventListener("dragover",e=>{
        if(available()&&e.dataTransfer?.types.includes(dragType)&&!image.hidden){e.preventDefault();e.dataTransfer.dropEffect="copy";}
      });
      paper.addEventListener("drop",e=>{
        if(!available()||image.hidden||!e.dataTransfer?.types.includes(dragType))return;
        const id=e.dataTransfer.getData(dragType);
        if(!ids.get(id)?.enabled)return;
        e.preventDefault();
        const box=image.getBoundingClientRect();
        if(!box.width||!box.height||!Number.isInteger(state.page)||!Number.isInteger(state.height))return;
        const x=(e.clientX-box.left)*960/box.width,y=(e.clientY-box.top)*state.height/box.height;
        if(x<0||x>=960||y<0||y>=state.height)return;
        intended={kind:"palette-drop",args:{id,page:state.page,x,y,control:e.ctrlKey},source,revision:state.revision,page:state.page};
        pulse();
      });
    }
    function update(root,next) {
      const priorAside=aside,wasReady=state.ready,action=available();
      const identity=JSON.stringify([next.project,next.documentGeneration]);
      const changed=source!==identity;
      state=next;
      if(changed){
        source=identity;groups=null;requested=null;failed="";pendingIcons=[];intended=null;wantsMore=null;
        open.clear();icons.clear();ids.clear();moreByGroup.clear();additions.clear();closeDialog(false);
        firstReadAfter=performance.now()+400;
      }else if(!wasReady&&state.ready)firstReadAfter=performance.now()+400;
      bind(root);
      if(changed||aside!==priorAside)repaint();
      else if(action!==available()){
        syncCells();
        if(modeHint){modeHint=false;announce("");}
      }
      const manage=aside?.querySelector("[data-palette-manage]");
      if(manage)manage.disabled=!groups;
      pulse();
    }
    function receive(kind,row) {
      if(kind==="palette-catalog"){
        const type=requested;requested=null;
        if(type!=="default"){
          if(validId(type)&&row.paletteMore?.group===type){
            moreByGroup.set(type,parseCells(row.paletteMore.cells));if(wantsMore?.id===type)fillMore(wantsMore);
          }else{if(wantsMore?.id===type)closeDialog();announce(clean(row.error));}
          pulse();return;
        }
        if(!Array.isArray(row.palette)||!row.palette.length||row.palette.length>100||row.error){
          failed=clean(row.error)||"无法读取符号面板";repaint();return;
        }
        groups=[];
        for(const g of row.palette){
          if(!g||!validId(g.id)||g.id.includes(":")||!paletteType(g.type)||!clean(g.name)||!Number.isInteger(g.width)||!Number.isInteger(g.height)||
             g.width<20||g.width>400||g.height<20||g.height>160)continue;
          const name=clean(g.name),local=clean(g.label)||name;
          groups.push({id:g.id,type:g.type,name,local,key:lookup[name],visible:g.visible===true,more:g.more===true,
            width:g.width,height:g.height,drawGrid:g.drawGrid===true,cells:parseCells(g.cells)});
        }
        repaint();
      }else if(kind==="palette-icons"){
        const batch=pendingIcons;pendingIcons=[];
        for(const {id,key} of batch){
          const value=row.paletteIcons?.[id];icons.set(key,png(value)?value:null);
          for(const b of aside?.querySelectorAll('[data-paint-key="'+key+'"]')||[])attachGlyph(b);
        }
        if(row.error)announce(clean(row.error));
        pulse(14);
      }
    }
    function reveal(key) {
      const g=groups?.find(x=>x.key===key);
      if(!g)return false;
      if(search.value)search.value="";
      selectVisibility(g,true,false);open.add(g.id);
      repaint();
      const head=pane.querySelector('[aria-controls="native-score-catalogue-group-'+g.id+'"]');
      if(head){pane.scrollTop+=head.getBoundingClientRect().top-pane.getBoundingClientRect().top;head.focus({preventScroll:true});}
      return true;
    }
    return {update,receive,reveal,invalidateLayout(){if(pane?.clientWidth>0){width=pane.clientWidth;repaint();}}};
  }
  return {create};
})();
