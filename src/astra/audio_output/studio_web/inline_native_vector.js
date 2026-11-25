// Vector page view: replays a page's display list (worker "vector-page")
// onto a canvas at any density in one frame, as MuseScore's paint view
// repaints its scene at the current matrix. Paths are in page canvas units
// (960 wide); glyph outlines are shared and placed with a transform.
(function () {
  "use strict";
  const CAPS = ["butt", "round", "square"], JOINS = ["miter", "round", "bevel"];
  const BG = "#f0f1f7";

  // Parse once: Path2D objects are reused for every redraw of the page.
  function prepare(v) {
    if (v.prepared) return v;
    const glyphs = v.glyphs.map(d => new Path2D(d));
    const ops = v.ops.map(op => {
      switch (op[0]) {
        case "f": case "F": return {k: 0, path: new Path2D(op[1]), color: op[2], rule: op[0] === "f" ? "nonzero" : "evenodd"};
        case "s": return {k: 1, path: new Path2D(op[1]), color: op[2], width: op[3], cap: CAPS[op[4]] || "butt",
                          join: JOINS[op[5]] || "miter", dash: Array.isArray(op[6]) ? op[6] : null};
        case "g": return {k: 2, glyph: glyphs[op[1]], color: op[2],
                          m: op.length === 6 ? [op[3], 0, 0, op[3], op[4], op[5]] : op.slice(3, 9)};
        default: return null;
      }
    }).filter(Boolean);
    v.prepared = {ops};
    return v;
  }

  // Draw page `v` into `canvas` (backing store already sized); k = device
  // pixels per page unit.
  function draw(canvas, v, k) {
    prepare(v);
    const ctx = canvas.getContext("2d", {alpha: false});
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.fillStyle = BG;
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    ctx.save();
    ctx.setTransform(k, 0, 0, k, 0, 0);
    const c = v.clip;
    ctx.beginPath(); ctx.rect(c[0], c[1], c[2], c[3]); ctx.clip();
    ctx.miterLimit = 2;   // QPen default
    for (const op of v.prepared.ops) {
      if (op.k === 0) {
        ctx.fillStyle = op.color; ctx.fill(op.path, op.rule);
      } else if (op.k === 1) {
        ctx.strokeStyle = op.color;
        ctx.lineWidth = op.width > 0 ? op.width : -op.width / k;   // cosmetic: device pixels
        ctx.lineCap = op.cap; ctx.lineJoin = op.join;
        ctx.setLineDash(op.dash || []);
        ctx.stroke(op.path);
      } else {
        const m = op.m;
        ctx.setTransform(k * m[0], k * m[1], k * m[2], k * m[3], k * m[4], k * m[5]);
        ctx.fillStyle = op.color; ctx.fill(op.glyph, "nonzero");
        ctx.setTransform(k, 0, 0, k, 0, 0);
      }
    }
    ctx.restore();
    ctx.setLineDash([]);
  }

  // Draw a vertically cropped excerpt [cropTop..cropBottom] of page `v`,
  // trimmed horizontally to the white sheet box v.clip so no grey surround leaks.
  function drawCropped(canvas, v, k, cropTop, cropBottom) {
    prepare(v);
    const c = Array.isArray(v.clip) && v.clip.length === 4 ? v.clip : [0, 0, 960, v.h || 1358];
    const x0 = c[0], dx = -x0 * k, dy = -cropTop * k;
    const ctx = canvas.getContext("2d", {alpha: false});
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.fillStyle = "#ffffff";
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    ctx.save();
    ctx.setTransform(k, 0, 0, k, dx, dy);
    ctx.beginPath();
    ctx.rect(c[0], Math.max(c[1], cropTop), c[2], Math.max(1, Math.min(c[1] + c[3], cropBottom) - Math.max(c[1], cropTop)));
    ctx.clip();
    ctx.miterLimit = 2;
    for (const op of v.prepared.ops) {
      if (op.k === 0) {
        ctx.fillStyle = op.color; ctx.fill(op.path, op.rule);
      } else if (op.k === 1) {
        ctx.strokeStyle = op.color;
        ctx.lineWidth = op.width > 0 ? op.width : -op.width / k;
        ctx.lineCap = op.cap; ctx.lineJoin = op.join;
        ctx.setLineDash(op.dash || []);
        ctx.stroke(op.path);
      } else {
        const m = op.m;
        ctx.setTransform(k * m[0], k * m[1], k * m[2], k * m[3], k * m[4] + dx, k * m[5] + dy);
        ctx.fillStyle = op.color; ctx.fill(op.glyph, "nonzero");
        ctx.setTransform(k, 0, 0, k, dx, dy);
      }
    }
    ctx.restore();
    ctx.setLineDash([]);
    return {clipX: c[0], clipW: c[2]};
  }

  window.AstraVectorPage = {prepare, draw, drawCropped};
})();
