"use strict";

(() => {
  const paper = document.getElementById("paper");
  const staging = document.getElementById("staging");
  const Engine = window.opensheetmusicdisplay?.OpenSheetMusicDisplay;
  let waiting = null;
  let active = null;

  function release(job) {
    if (!job) return;
    job.element.remove();
    try {
      job.osmd?.dispose();
    } catch (error) {
      console.warn("Could not dispose the previous score renderer", error);
    }
  }

  function discard() {
    const previous = waiting;
    waiting = null;
    release(previous);
  }

  async function layout(token, xml, audit, host, selection = false) {
    discard();
    const element = document.createElement("div");
    element.className = "score";
    staging.append(element);
    const job = {token, element, osmd: null};
    waiting = job;

    try {
      // music21 includes a conventional external DTD declaration. Remove that
      // declaration before parsing, without resolving an external identifier.
      const text = xml.replace(/<!DOCTYPE\s+score-partwise\b[^<>\[\]]*>/i, "");
      const documentXml = new DOMParser().parseFromString(text, "application/xml");
      if (documentXml.documentElement.localName !== "score-partwise" ||
          documentXml.querySelector("parsererror") || documentXml.doctype) {
        throw new Error("无法读取谱面 MusicXML。");
      }
      const freeBars = [...new Set(Array.from(documentXml.querySelectorAll("senza-misura"),
        node => node.closest("measure")?.getAttribute("number") || "?"))];
      for (const free of documentXml.querySelectorAll("time > senza-misura")) {
        // Some free meters inherit a nominal time glyph from the renderer.
        // Suppress that glyph and label the free bar in this private DOM only.
        const time = free.parentElement;
        const attributes = time.parentElement;
        time.remove();
        const direction = documentXml.createElement("direction");
        direction.setAttribute("placement", "above");
        const type = documentXml.createElement("direction-type");
        const label = documentXml.createElement("words");
        label.textContent = free.textContent.trim() || "senza misura";
        type.append(label);
        direction.append(type);
        attributes.after(direction);
      }
      const parts = Array.from(documentXml.documentElement.children)
        .filter(part => part.localName === "part");
      const lengths = parts.map(part => part.children.length);
      const count = lengths[0];
      if (!count || !lengths.every(length => length === count)) {
        throw new Error("声部的小节范围不完整。");
      }
      const hasTitle = Array.from(documentXml.querySelectorAll("work > work-title, movement-title"))
        .some(node => node.textContent.trim());
      job.osmd = new Engine(element, {
        backend: "svg", autoResize: false, disableCursor: true, drawTitle: hasTitle,
        drawMeasureNumbersOnlyAtSystemStart: true,
        keepWarm: false, pageFormat: "A4_P",
      });
      job.osmd.EngravingRules.RenderPedals = true;
      await job.osmd.load(documentXml);
      if (waiting !== job) return;
      if (job.osmd.Sheet.SourceMeasures.length !== count ||
          Object.values(job.osmd.Sheet.SheetErrors.measureErrors).some(errors => errors.length)) {
        throw new Error("本地排版没有读到完整小节。MusicXML 已保留，本次未出图。");
      }
      if (freeBars.length && !matchesFreeTiming(job.osmd.Sheet.SourceMeasures, audit)) {
        throw new Error("自由小节 " + freeBars.join("、") + " 及相邻小节的时值核对失败。MusicXML 已保留，本次未出图。");
      }
      // Engrave at a 7.4 mm staff height on A4 before scaling the preview.
      job.osmd.Zoom = 0.70;
      job.osmd.render();
      if (!element.querySelector("svg :is(path, line, text, rect)")) {
        throw new Error("OSMD 未生成可显示的谱面。");
      }
      const engraving = selection ? tuneSelection(job.osmd, element, documentXml, parts, count, freeBars) : null;
      const proof = JSON.stringify({...makePages(job.osmd, element, count), free_bars: freeBars,
        ...(engraving ? {selection_engraving: engraving} : {})});
      host.composed(token, true, proof);
    } catch (error) {
      if (waiting !== job) return;
      discard();
      host.composed(token, false, String(error.message || error).slice(0, 250));
    }
  }

  const MAX_SELECTION_BARS = 25; // A 24-bar span may touch partial bars at both ends.
  // OSMD's coordinate unit is one staff space. Keep the stock geometric
  // skyline; only its visible gap allowance is adjusted within the excerpt.
  const SELECTION_STAFF_FLOOR = 4.2;
  const SELECTION_SKY_GAP = 0.90;

  function systemsIn(osmd) {
    return osmd.GraphicSheet.MusicPages.flatMap(page => page.MusicSystems);
  }

  function systemMeasures(system) {
    return system.StaffLines[0].Measures.filter(measure => !measure.IsExtraGraphicalMeasure);
  }

  function startsOf(systems) {
    return systems.map(system => systemMeasures(system)[0]?.parentSourceMeasure.measureListIndex);
  }

  function systemLabelWidth(system, rules) {
    return system.MaxLabelLength > 0 ? system.MaxLabelLength + rules.SystemLabelsRightMargin : 0;
  }

  function graphicLines(osmd) {
    const rounded = n => Number.isFinite(n) ? Number(n.toFixed(3)) : null;
    return systemsIn(osmd).map(system => {
      const staves = system.StaffLines;
      const [upper, lower] = staves;
      const distance = lower && lower.PositionAndShape.RelativePosition.y - upper.PositionAndShape.RelativePosition.y;
      let skylineClearance;
      if (staves.length === 2 && upper.BottomLine.length && lower.SkyLine.length) {
        // Match the pinned renderer's ±6 skyline samples. A flat value is
        // not an assurance of clearance between actually engraved symbols.
        skylineClearance = Infinity;
        for (const [i, bottom] of upper.BottomLine.entries()) {
          for (let j = Math.max(0, i - 6); j < Math.min(lower.SkyLine.length, i + 7); j++) {
            skylineClearance = Math.min(skylineClearance, distance - bottom + lower.SkyLine[j]);
          }
        }
      }
      const measures = systemMeasures(system);
      return {
        count: measures.length, start: measures[0]?.parentSourceMeasure.measureListIndex ?? null,
        spring: rounded(measures[0]?.staffEntriesScaleFactor),
        width: rounded(upper.PositionAndShape.Size.width),
        courtesy: upper.Measures.filter(bar => bar.IsExtraGraphicalMeasure).length,
        staff_gap: rounded(distance === undefined ? NaN : distance - upper.StaffHeight),
        skyline_clearance: rounded(skylineClearance)
      };
    });
  }

  function makeSelectionPlan(osmd, systems, count) {
    const rules = osmd.EngravingRules;
    const available = osmd.Sheet.pageWidth - rules.PageLeftMargin - rules.PageRightMargin -
      rules.SystemLeftMargin - rules.SystemRightMargin;
    const starts = new Set(startsOf(systems));
    const rows = osmd.GraphicSheet.MeasureList.map(measures => measures.filter(Boolean));
    if (rows.length !== count || !rows.every(row => row.length === 2)) return null;
    if (osmd.Sheet.SourceMeasures.slice(1).some(bar => bar.hasBeginInstructions())) return null;
    // Courtesy key, meter and clef instructions can add line-end width.
    // The short planner is limited to stable headers; those sources use the
    // stock line-breaker, which computes its exact courtesy widths itself.
    const items = rows.map(row => ({
      begin: Math.max(...row.map(bar => bar.beginInstructionsWidth)),
      end: Math.max(...row.map(bar => bar.endInstructionsWidth)),
      glyph: Math.max(...row.map(bar => bar.minimumStaffEntriesWidth))
    }));
    if (items.some(row => !(row.glyph > 0) || !Number.isFinite(row.begin + row.end))) return null;
    const body = items.filter((row, i) => !starts.has(i)).map(row => row.begin);
    const further = items.filter((row, i) => i !== 0 && starts.has(i)).map(row => row.begin);
    if (!body.length) return null;
    const middle = Math.max(...body);
    const later = further.length ? Math.max(...further) : Math.max(middle, items[0].begin);
    if (!body.every(w => middle - w < 0.45) || !further.every(w => later - w < 0.45)) return null;
    const initialLabel = systemLabelWidth(systems[0], rules);
    const laterLabel = Math.max(0, ...systems.slice(1).map(s => systemLabelWidth(s, rules)));
    const lineCount = Math.max(2, systems.length);
    const columns = [available];
    if (count <= 8 && lineCount === 2) {
      // A narrower *music column* can keep a short excerpt from being
      // stretched to both margins. Paper dimensions/notation size stay fixed.
      for (let col = Math.floor(available); col >= available * .64; col--) columns.push(col);
    }
    let best = null;
    for (const column of columns) {
      let pending = new Map([[0, {starts: [], loss: 0}]]);
      for (let line = 0; line < lineCount; line++) {
        const next = new Map();
        for (const [start, prefix] of pending) {
          let fixed = (start === 0 ? items[0].begin : later) - middle;
          let glyph = 0;
          const label = start === 0 ? initialLabel : laterLabel;
          for (let end = start + 1; end <= count; end++) {
            const bar = items[end - 1];
            fixed += middle + bar.end;
            glyph += bar.glyph;
            const spring = (column - label - fixed - 1.0) / glyph;
            if (spring < 1.04) break;
            const final = line === lineCount - 1;
            if (final !== (end === count) || count - end < lineCount - 1 - line) continue;
            const printedSpring = final ? Math.min(spring, rules.LastSystemMaxScalingFactor) : spring;
            if (!final && printedSpring > 1.62) continue;
            const remaining = Math.max(0, column - label - fixed - printedSpring * glyph);
            const loss = prefix.loss + (printedSpring - 1.19)**2 + .40 * (remaining / column)**2 +
              (end - start === 1 ? 3 : 0);
            if (loss < (next.get(end)?.loss ?? Infinity)) {
              next.set(end, {starts: [...prefix.starts, start], loss});
            }
          }
        }
        pending = next;
      }
      const candidate = pending.get(count);
      if (candidate && candidate.loss < (best?.loss ?? Infinity)) {
        best = {...candidate, extraRightMargin: available - column};
      }
    }
    if (!best || best.starts.length < 2) return null;
    const ends = [...best.starts.slice(1), count];
    return best.starts.some((start, i) => ends[i] - start < 2) ? null : best;
  }

  function tuneSelection(osmd, element, documentXml, parts, count, freeBars) {
    const baseline = systemsIn(osmd);
    const baseLines = graphicLines(osmd);
    const proof = algorithm => ({algorithm, lines: graphicLines(osmd)});
    // These limits and fallbacks change drawing only, not the validated
    // range budget, clips, time, note data, or manual MusicXML layout.
    if (parts.length !== 1 || count > MAX_SELECTION_BARS || freeBars.length ||
        parts[0].querySelector("attributes > staves")?.textContent.trim() !== "2" ||
        documentXml.querySelector("print[new-system], print[new-page]") ||
        baseline.some(system => system.StaffLines.length !== 2 ||
          system.checkStaffEntriesForStaffEntryLink())) return proof("native");

    const basePages = makePages(osmd, element, count, false).systems;
    const rules = osmd.EngravingRules;
    const originals = {
      SystemRightMargin: rules.SystemRightMargin,
      NewSystemAtXMLNewSystemAttribute: rules.NewSystemAtXMLNewSystemAttribute,
      BetweenStaffDistance: rules.BetweenStaffDistance,
      MinimumStaffLineDistance: rules.MinimumStaffLineDistance,
      MinSkyBottomDistBetweenStaves: rules.MinSkyBottomDistBetweenStaves
    };
    const sourceFlags = osmd.Sheet.SourceMeasures.map(bar => bar.printNewSystemXml);
    const thin = count >= 4 && (baseLines.some(line => line.count === 1 && baseLines.length > 1) ||
      (baseLines.length === 1 && baseLines[0].spring !== null && baseLines[0].spring < 1.04));
    const plan = thin ? makeSelectionPlan(osmd, baseline, count) : null;
    const applyLayout = active => {
      Object.assign(rules, {
        ...originals, BetweenStaffDistance: SELECTION_STAFF_FLOOR,
        MinimumStaffLineDistance: SELECTION_STAFF_FLOOR, MinSkyBottomDistBetweenStaves: SELECTION_SKY_GAP
      });
      if (active) {
        rules.SystemRightMargin += active.extraRightMargin;
        rules.NewSystemAtXMLNewSystemAttribute = true;
      }
      const forced = new Set(active?.starts.slice(1));
      // Private renderer flag only. Never write hints to MusicXML or change
      // an existing hint, and never disable the renderer's automatic fit guard.
      osmd.Sheet.SourceMeasures.forEach((bar, i) => {
        bar.printNewSystemXml = sourceFlags[i] || forced.has(i);
      });
    };
    const acceptable = (active, starts) => {
      const pages = makePages(osmd, element, count, false).systems;
      const lines = graphicLines(osmd);
      const actual = lines.map(line => line.start);
      const expected = active?.starts ?? starts;
      return expected.length === actual.length && expected.every((value, i) => actual[i] === value) &&
        pages.length <= basePages.length &&
        (pages.length === 1 || pages.at(-1) >= Math.min(2, basePages.at(-1))) &&
        lines.every(line => line.spring !== null && (!active || line.spring >= 1.01) &&
          line.skyline_clearance !== null && line.skyline_clearance >= .82);
    };
    const originalStarts = baseLines.map(line => line.start);
    try {
      applyLayout(plan);
      osmd.render();
      if (!acceptable(plan, originalStarts)) throw new Error("Unsafe selection layout");
      // If a small excerpt has two strikingly different gaps, add no more
      // than one staff space to the tighter pair. Never equalize all rows to a
      // tall outlier or override the clearance around notes/dynamics.
      const lines = graphicLines(osmd);
      if (count <= 8 && lines.length === 2) {
        const gaps = lines.map(line => line.staff_gap);
        const smaller = Math.min(...gaps);
        const larger = Math.max(...gaps);
        const floor = Math.min(7.2, Math.floor((larger - 1.15) * 10) / 10, smaller + 1);
        if (floor - smaller >= .15) {
          rules.MinimumStaffLineDistance = floor;
          rules.BetweenStaffDistance = floor;
          osmd.render();
          if (!acceptable(plan, originalStarts)) {
            applyLayout(plan);
            osmd.render();
            if (!acceptable(plan, originalStarts)) throw new Error("Unsafe selection layout");
          }
        }
      }
      return proof(plan ? "balanced" : "engraved");
    } catch (error) {
      Object.assign(rules, originals);
      osmd.Sheet.SourceMeasures.forEach((bar, i) => { bar.printNewSystemXml = sourceFlags[i]; });
      osmd.render();
      return proof("native");
    }
  }

  function matchesFreeTiming(measures, audit) {
    if (!audit) return false;
    const {rows} = JSON.parse(audit);
    const same = (value, n, d) => {
      const numerator = value.GetExpandedNumerator();
      const denominator = value.Denominator;
      // OSMD stores whole notes while Astra/MusicXML uses quarter notes.
      return Number.isSafeInteger(numerator) && Number.isSafeInteger(denominator) &&
        4n * BigInt(numerator) * BigInt(d) === BigInt(n) * BigInt(denominator);
    };
    return rows.length === measures.length && measures.every((measure, i) => {
      const [start, startDenom, length, lengthDenom] = rows[i];
      return same(measure.AbsoluteTimestamp, start, startDenom) && same(measure.Duration, length, lengthDenom);
    });
  }

  function makePages(osmd, element, count, wrap = true) {
    const svgs = Array.from(element.querySelectorAll("svg[id^='osmdSvgPage']"));
    if (!svgs.length || svgs.length !== osmd.GraphicSheet.MusicPages.length) {
      throw new Error("A4 排版的页面不完整。");
    }
    const coverage = svgs.map(() => []);
    let staffMeasuresDrawn = 0;
    osmd.GraphicSheet.MeasureList.forEach((staffMeasures, index) => {
      const expected = staffMeasures.filter(Boolean);
      const drawn = expected.map(measure => measure.getSVGGElement()).filter(Boolean);
      staffMeasuresDrawn += drawn.length;
      const pageIndexes = new Set(drawn.map(group => svgs.indexOf(group.ownerSVGElement)));
      if (!drawn.length || drawn.length !== expected.length || pageIndexes.size !== 1 || pageIndexes.has(-1)) {
        throw new Error("A4 排版缺少第 " + (index + 1) + " 个小节。");
      }
      coverage[[...pageIndexes][0]].push(index + 1);
    });
    const seen = coverage.flat();
    if (seen.length !== count || !seen.every((n, i) => n === i + 1) || coverage.some(page => !page.length)) {
      throw new Error("A4 分页的小节顺序不完整。");
    }
    const bounds = [];
    for (const [i, svg] of svgs.entries()) {
      const box = svg.getBBox();
      const view = svg.viewBox.baseVal;
      if (!view.width || !view.height || box.width <= 0 || box.height <= 0 ||
          box.x < -1 || box.y < -1 || box.x + box.width > view.width + 1 || box.y + box.height > view.height + 1) {
        throw new Error("第 " + (i + 1) + " 页谱面超出版心。");
      }
      bounds.push([box.x, box.y, box.width, box.height, view.width, view.height].map(n => Math.round(n*100)/100));
      if (wrap) {
        const page = document.createElement("section");
        page.className = "engraving-page";
        const footer = document.createElement("footer");
        footer.textContent = (i + 1) + " / " + svgs.length;
        page.append(svg, footer);
        element.append(page);
      }
    }
    const systems = osmd.GraphicSheet.MusicPages.map(page => page.MusicSystems.length);
    if (systems.some(total => total < 1)) throw new Error("A4 排版包含空白页。");
    return {pages: svgs.length, measures: count, staff_measures: staffMeasuresDrawn, coverage, bounds, systems};
  }

  function publish(token, host) {
    if (waiting?.token !== token) return;
    const previous = active;
    active = waiting;
    waiting = null;
    paper.replaceChildren(active.element);
    paper.dataset.token = token;
    release(previous);
    host.published(token);
  }

  // Future notation annotations can attach to the committed SVG by token.
  // The display never writes changes back to the source XML or editor.
  new QWebChannel(qt.webChannelTransport, channel => {
    const host = channel.objects.scoreSurface;
    host.invalidate.connect(discard);
    host.clear.connect(() => {
      discard();
      release(active);
      active = null;
      delete paper.dataset.token;
    });
    host.prepare.connect((token, xml, audit) => layout(token, xml, audit, host));
    host.prepareSelection?.connect((token, xml, audit) => layout(token, xml, audit, host, true));
    host.previewZoom.connect(scale => {
      if (Number.isFinite(scale) && scale >= 0.5 && scale <= 3) {
        paper.style.setProperty("--preview-zoom", scale);
      }
    });
    host.commit.connect(token => publish(token, host));
    host.initialized(Boolean(Engine));
  });
})();

