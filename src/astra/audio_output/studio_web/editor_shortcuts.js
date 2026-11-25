/* Configurable editor commands. No event listeners and no project mutations. */
(() => {
  'use strict';
  const STORAGE_KEY = 'astra.editor.shortcuts.v1';
  const BASE = 'https://www.steinberg.help/r/cubase-pro/16.0/en/cubase_nuendo/topics/key_commands/';
  const SOURCES = {
    zoom: BASE+'key_commands_zoom_category_c.html?contentId=xCb4y9qR5mTHolp2LGgEBg',
    extension: 'Astra 扩展：独立调整 CC64 显示高度，无 Cubase 默认按键',
    tool: BASE+'key_commands_tool_category_c.html?contentId=HZv36BcFFtqe~9CIE7Br~A',
    edit: BASE+'key_commands_edit_category_c.html?contentId=Ly0_lW_4pAXePZEhN_vy5A',
    midi: BASE+'key_commands_midi_category_c.html?contentId=3R16mgKqicCBclKW06hlEg',
    transport: BASE+'key_commands_transport_category_c.html?contentId=PP5HjmhyDslGYNpM7hTsUQ',
    quantize: BASE+'key_commands_quantize_category_c.html?contentId=PCwnnqW7AlIRR3Zjz5GNeA',
  };
  const commands = Object.freeze([
    ['editorPitchZoomIn','纵向放大音高','Shift+H','zoom'],
    ['editorPitchZoomOut','纵向缩小音高','Shift+G','zoom'],
    ['editorCCZoomIn','增大 CC64 区域（Astra）','','extension'],
    ['editorCCZoomOut','缩小 CC64 区域（Astra）','','extension'],
    ['editorSelectTool','选择工具','1','tool'],
    ['editorDrawTool','绘制工具','8','tool'],
    ['editorQuantize','量化所选音符','Q','quantize'],
    ['editorLocateSelection','定位点包围所选音符','P','transport'],
    ['editorSelectAll','全选当前编辑区域对象','Ctrl+A','edit'],
    ['editorUndo','撤销','Ctrl+Z','edit'],
    ['editorRedo','重做','Ctrl+Shift+Z','edit'],
    ['editorCopy','复制所选事件','Ctrl+C','edit'],
    ['editorPaste','粘贴事件','Ctrl+V','edit'],
    ['editorPasteAtOrigin','原位粘贴','Alt+V','edit'],
    ['editorDuplicate','再复制一次','Ctrl+D','edit'],
    ['editorRepeat','重复…','Ctrl+K','edit'],
    ['editorCut','剪切','Ctrl+X','edit'],
    ['editorSelectNone','取消选择','Ctrl+Shift+A','edit'],
    ['editorInvertSelection','反向选择','Ctrl+Alt+I','edit'],
    ['editorCCLaneToggle','显示/隐藏 CC64 泳道','Alt+L','midi'],
    ['editorDelete','删除所选事件','Delete','edit'],
    ['editorSnapToggle','开启或关闭吸附','J','edit'],
    ['editorLoop','开启或关闭循环','NumpadDivide','transport'],
    ['editorTransportPlay','播放','Enter','transport'],
    ['editorTransportStop','停止','Numpad0','transport'],
    ['editorJumpLeft','到左定位点','Numpad1','transport'],
    ['editorJumpRight','到右定位点','Numpad2','transport'],
    ['editorFocusLeft','输入左定位点','Shift+L','transport'],
    ['editorFocusRight','输入右定位点','Shift+R','transport'],
    ['editorMetronome','节拍器','C','transport'],
    ['editorBeatRecordToggle','录制拍点','Shift+T','transport'],
  ].map(([action,label,defaultBinding,category]) => Object.freeze({action,label,defaultBinding,source:SOURCES[category]})));
  const defaults = Object.fromEntries(commands.map(command => [command.action,command.defaultBinding]));
  const modifiers = ['Ctrl','Alt','Shift','Meta'];
  const aliases = {control:'Ctrl',ctrl:'Ctrl',alt:'Alt',option:'Alt',shift:'Shift',meta:'Meta',cmd:'Meta',command:'Meta',
    esc:'Escape',escape:'Escape',space:'Space',spacebar:'Space',del:'Delete',delete:'Delete',backspace:'Backspace',
    enter:'Enter',return:'Enter',tab:'Tab',home:'Home',end:'End',pageup:'PageUp',pagedown:'PageDown',insert:'Insert',
    arrowup:'ArrowUp',arrowdown:'ArrowDown',arrowleft:'ArrowLeft',arrowright:'ArrowRight',
    '+':'Plus',plus:'Plus','-':'Minus',minus:'Minus','=':'Equal',equal:'Equal','/':'Slash',slash:'Slash',
    '.':'Period',period:'Period',',':'Comma',comma:'Comma',';':'Semicolon',semicolon:'Semicolon',
    "'":'Quote',quote:'Quote','[':'BracketLeft',bracketleft:'BracketLeft',']':'BracketRight',bracketright:'BracketRight',
    '\\':'Backslash',backslash:'Backslash','`':'Backquote',backquote:'Backquote',
    numpaddivide:'NumpadDivide',numpadmultiply:'NumpadMultiply',numpadadd:'NumpadAdd',numpadsubtract:'NumpadSubtract',
    numpaddecimal:'NumpadDecimal',numpadenter:'NumpadEnter'};
  function keyName(key) {
    if(typeof key !== 'string') return '';
    if(key === ' ') return 'Space';
    const value=key.trim(),lower=value.toLowerCase();
    if(Object.hasOwn(aliases,lower)) return aliases[lower];
    if(/^[a-z0-9]$/i.test(value) || /^F(?:[1-9]|1\d|2[0-4])$/i.test(value)) return value.toUpperCase();
    if(/^numpad[0-9]$/i.test(value)) return 'Numpad'+value.at(-1);
    return '';
  }
  function normalize(chord) {
    if(typeof chord !== 'string') throw new Error('快捷键必须是按键组合。');
    if(!chord.trim()) return '';
    // A plus sign as the final key can also be entered as Ctrl++.
    const value=chord.trim().replace(/\+\+$/, '+Plus');
    const tokens=value.split('+').map(token=>keyName(token));
    const key=tokens.pop(),mods=new Set(tokens);
    if(!key || modifiers.includes(key) || tokens.some(token=>!modifiers.includes(token)) || mods.size!==tokens.length)
      throw new Error('请输入一个按键，可搭配 Ctrl、Alt、Shift；不要输入多段快捷键。');
    return [...modifiers.filter(mod=>mods.has(mod)),key].join('+');
  }
  function canonical(event) {
    if(!event || event.isComposing || event.keyCode===229 || event.getModifierState?.('AltGraph')) return '';
    let key=keyName(event.key);
    if(/^Numpad(?:[0-9]|Divide|Multiply|Add|Subtract|Decimal|Enter)$/.test(event.code||'')) key=event.code;
    // Shift+digit emits a punctuation character in many layouts; retain the
    // numbered key so the recorded combination resolves on the next press.
    else if(/^Digit[0-9]$/.test(event.code||'')) key=event.code.slice(-1);
    if(!key || modifiers.includes(key)) return '';
    return [event.ctrlKey&&'Ctrl',event.altKey&&'Alt',event.shiftKey&&'Shift',event.metaKey&&'Meta',key].filter(Boolean).join('+');
  }
  const projectSaveChord=chord=>chord==='Ctrl+S'||chord==='Meta+S';
  function reserved(chord) {
    if(projectSaveChord(chord))return true;
    const parts=chord.split('+'),key=parts.at(-1),ctrl=parts.includes('Ctrl')||parts.includes('Meta');
    // These are implemented by the editor/global transport and remain outside
    // the configurable action registry in this release.
    if(['B','Escape','Space','ArrowUp','ArrowDown','ArrowLeft','ArrowRight'].includes(key)) return true;
    if(ctrl && ['Plus','Equal','Minus','0','O','Enter','R','W',
      'NumpadAdd','NumpadSubtract','Numpad0','NumpadEnter'].includes(key)) return true;
    if(key==='F5' || (parts.includes('Alt') && ['F4','Tab'].includes(key))) return true;
    return false;
  }
  function validate(map) {
    const used=new Map();
    for(const command of commands) {
      const chord=normalize(map[command.action]);
      if(chord && reserved(chord)) throw new Error('此组合由播放、临时吸附、导航或窗口操作保留，请换一个。');
      if(chord && used.has(chord)) throw new Error(`快捷键 ${chord} 已用于“${used.get(chord)}”。请先清除原绑定。`);
      if(chord) used.set(chord,command.label);
      map[command.action]=chord;
    }
    return map;
  }
  function validatedBindings(bindings) {
    if(!bindings || Array.isArray(bindings) || typeof bindings!=='object'
        || Object.keys(bindings).some(action=>!Object.hasOwn(defaults,action)))
      throw new Error('快捷键配置格式无效。');
    const merged={...defaults,...bindings};
    // New defaults must not steal a chord explicitly configured in an older version.
    for(const action of Object.keys(defaults))if(!Object.hasOwn(bindings,action)&&merged[action]&&Object.values(bindings).includes(merged[action]))merged[action]='';
    // Migrate only obsolete bindings that collide with the project Save command.
    for(const action of Object.keys(merged))if(projectSaveChord(normalize(merged[action])))merged[action]='';
    return validate(merged);
  }
  let current={...defaults};
  const listeners=new Set();
  try {
    const stored=window.localStorage?.getItem(STORAGE_KEY);
    if(stored) {
      const data=JSON.parse(stored);
      if(data.version!==1) throw new Error('Invalid saved version');
      current=validatedBindings(data.bindings);
    }
  } catch (_) { current={...defaults}; }
  function persist(next) {
    try {
      if(!window.localStorage) throw new Error('Storage unavailable');
      window.localStorage.setItem(STORAGE_KEY,JSON.stringify({version:1,bindings:next}));
    } catch (_) { throw new Error('无法保存快捷键；当前设置未改变。请检查本机存储是否可用。'); }
    current=next;
    for(const listener of listeners) {
      try { listener({...current}); }
      catch(error) { console.warn('Shortcut change listener failed',error); }
    }
    return {...current};
  }
  function hydrate(bindings) {
    const next=validatedBindings(bindings);
    current=next;
    return {...current};
  }
  function onChanged(callback) {
    if(typeof callback!=='function') throw new Error('快捷键监听器必须是函数。');
    listeners.add(callback);
    return ()=>listeners.delete(callback);
  }
  function set(action,chord) {
    if(!Object.hasOwn(defaults,action)) throw new Error('未知的编辑动作。');
    const normalized=normalize(chord);
    const owner=normalized && commands.find(command=>command.action!==action && current[command.action]===normalized);
    if(owner) throw new Error(`快捷键 ${normalized} 已用于“${owner.label}”。请先清除原绑定。`);
    return persist(validate({...current,[action]:normalized}));
  }
  function blocked(event) {
    if(!event || event.defaultPrevented || event.repeat || event.isComposing || event.keyCode===229 || event.getModifierState?.('AltGraph')) return true;
    const target=event.target;
    if(target?.isContentEditable || target?.closest?.('input,textarea,select,[contenteditable]:not([contenteditable="false"]),[role="textbox"]')) return true;
    // Native docked dialogs are non-modal and intentionally leave the piano
    // roll usable. Inputs within them are still caught by the target guard.
    if(typeof document !== 'undefined' && document.querySelector?.('dialog[open]:not(.editor-docked),[role="dialog"][aria-modal="true"]:not([hidden]):not([aria-hidden="true"]):not(.editor-docked)')) return true;
    return false;
  }
  function resolve(event) {
    if(blocked(event)) return null;
    const chord=canonical(event);
    if(!chord) return null;
    return commands.find(command=>current[command.action]===chord)?.action || null;
  }
  window.AstraEditorShortcuts=Object.freeze({commands,canonical,resolve,bindings:()=>({...current}),set,reset:()=>persist({...defaults}),hydrate,onChanged});
})();
