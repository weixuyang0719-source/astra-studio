"use strict";
/* Optional compact right-click contribution for the native score workspace. */
(() => {
  const command = (label, action, value) => ({
    label,
    kind: "content",
    fields: value === undefined ? {action} : {action,value},
  });
  function measureActions(state = {}) {
    const {contextMenu, measureContext: m, selectionKind} = state;
    if (!m || !["measure","range","selection"].includes(contextMenu)) return [];
    const bar = Number.isInteger(m.number);
    const canInsert = bar || m.whole === true;
    if (contextMenu === "selection" && !m.canSplit) return [];
    const commands = [];
    if (contextMenu !== "selection" && canInsert) {
      commands.push(command("在前面插入小节","measure-add-before"),
                    command("在后面插入小节","measure-add-after"));
    }
    if (contextMenu === "selection" && m.canSplit && ["Note","Rest"].includes(selectionKind)) {
      commands.push(command("在此处拆分小节","measure-split"));
    }
    if (contextMenu !== "selection" && m.canJoin) {
      commands.push(command("合并所选小节","measure-join"));
    }
    if ((contextMenu === "measure" || (contextMenu === "range" && m.whole === true)) && bar) {
      for (const pair of Array.isArray(m.pickupOptions) ? m.pickupOptions : []) {
        if (!Array.isArray(pair) || pair.length !== 2 || !pair.every(Number.isInteger)) continue;
        commands.push({
          label: (m.first ? "设为弱起：" : "实际时值：")+pair.join("/"),
          kind: "content",
          fields: {action:"measure-length",value:pair,...(m.first ? {excludeNumber:true} : {})},
        });
      }
      if (m.canResetLength) {
        commands.push(command("恢复拍号时值","measure-length-reset"));
      }
      const mode = m.numberMode;
      if ([0,1,2].includes(mode)) {
        const labels = ["小节编号：自动","小节编号：显示","小节编号：隐藏"];
        for (let i = 0; i < labels.length; i++) {
          if (i !== mode) commands.push(command(labels[i],"measure-number-mode",i));
        }
      }
      if (typeof m.excludeNumber === "boolean") {
        commands.push(command(m.excludeNumber ? "计入小节编号" : "不计入小节编号",
                              "measure-exclude-number",!m.excludeNumber));
      }
    }
    if (contextMenu !== "selection" && m.canDelete) {
      commands.push(command("删除所选小节","measure-delete"));
    }
    return commands;
  }
  if (typeof module !== "undefined" && module.exports) module.exports = measureActions;
  if (typeof window !== "undefined") window.AstraNativeScore?.setContextActions?.(measureActions);
})();
