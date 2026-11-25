/* The transport owns a Space press and its release, not the focused widget. */
(() => {
  'use strict';
  const controls = 'button,summary,a[href],input[type=checkbox],input[type=radio],input[type=range],[role=button],[role=tab],[role=switch],[role=checkbox],[role=radio],[role=slider]';
  const localScopes = 'dialog,[role=dialog],[role=alertdialog],[role=menu],[role=menuitem],[role=listbox],[role=option],[popover]:popover-open,.editor-command-menu[open],.editor-score-preview-entry,.volume-popover[open],.transport-options[open],.transport-speed-popup[open]';
  const editables = 'textarea,select,input:not([type=checkbox]):not([type=radio]):not([type=range]),[role=textbox],[role=searchbox],[role=combobox]';
  const arrowKeys = new Set(['ArrowUp','ArrowDown','ArrowLeft','ArrowRight','Home','End']);
  const arrowControls = 'input[type=range],input[type=radio],select,[role=tab],[role=slider],[role=radio],[role=menu],[role=listbox]';
  const isSpace = event => event.code==='Space' || event.key===' ' || event.key==='Spacebar';
  const inLocalScope = event => event.composedPath().some(node =>
    node?.isContentEditable || node?.matches?.(localScopes+','+editables));
  const keyboardNavigation = event => {
    if(event.ctrlKey || event.altKey || event.metaKey || event.isComposing) return false;
    return event.key==='Tab' ||
      (event.key==='Enter' && event.target?.closest?.(controls)) ||
      (arrowKeys.has(event.key) && event.target?.closest?.(arrowControls));
  };

  window.AstraTransportKeys = {install({available, toggle}) {
    let pointerFocus = false, owner = null;
    const consume = event => {event.preventDefault();event.stopImmediatePropagation();};
    const clearPress = () => {owner=null;};

    window.addEventListener('pointerdown', () => {pointerFocus=true;}, true);
    window.addEventListener('keydown', event => {
      if(!isSpace(event)) {
        // DAW edit/zoom shortcuts are not traversal into the last clicked button.
        if(!event.defaultPrevented && !window.AstraBeatRecorder?.ownsTabKey?.() && keyboardNavigation(event)) pointerFocus=false;
        return;
      }
      if(owner==='transport') {consume(event);return;}
      if(owner==='local') return;

      const modal = 'dialog:modal,[aria-modal=true]:not([hidden]):not([aria-hidden=true])';
      // A take is a dedicated keyboard interaction, not focus traversal.
      // If the transport is busy still swallow the press/release: no unrelated widget click.
      if (!event.defaultPrevented && !event.isComposing && event.keyCode!==229 &&
          !event.ctrlKey && !event.altKey && !event.metaKey && !event.shiftKey &&
          !document.querySelector(modal) && window.AstraBeatRecorder?.ownsSpaceKey?.(event)) {
        owner='transport';consume(event);
        if(!event.repeat && available())toggle();
        return;
      }
      if(event.defaultPrevented || event.isComposing || event.keyCode===229 ||
        event.ctrlKey || event.altKey || event.metaKey || event.shiftKey ||
        inLocalScope(event) || document.querySelector(modal) ||
        (!pointerFocus && event.target?.closest?.(controls)) || !available()) {
        owner='local';return;
      }

      owner='transport';consume(event);
      if(!event.repeat) toggle();
    }, true);
    window.addEventListener('keyup', event => {
      if(!isSpace(event)) return;
      if(owner==='transport') consume(event);
      if(!event.repeat) clearPress();
    }, true);
    // A key released outside the window may never reach WebEngine. Reset the
    // press, not focus provenance; refocusing is not keyboard traversal.
    window.addEventListener('blur', clearPress);
    window.addEventListener('pagehide', clearPress);
    document.addEventListener('visibilitychange', () => {if(document.hidden) clearPress();});
  }};
})();
