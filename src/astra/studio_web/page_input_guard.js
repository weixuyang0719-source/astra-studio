"use strict";
// Ctrl-wheel (also emitted by trackpad pinch) belongs to the editor, not to
// Chromium page zoom. Cancel only the browser default: component handlers
// still receive the original event and ordinary scrolling remains native.
document.addEventListener("wheel", (event) => {
  if (event.ctrlKey || event.metaKey) event.preventDefault();
}, {capture: true, passive: false});
