"use strict";
/* Transport-coupled Manual Beat Recording (Overwrite + Optional MIDI Note-On Snap).
   - Cubase-inspired Auto Punch In/Out (explicit L/R toggle) + Pre-roll + Cycle Keep Last.
   - Authoritative dual-anchor QPC clock interpolation with rate-scaled latency compensation.
   - Visual-first canvas cues (crimson overwrite band vs violet Loop bar, 60fps overlay pins).
   - Bare Tab tap key during Recording (IME-safe) + B-hold per-tap free override. */
window.AstraBeatRecorder = (() => {
  const PREFS_KEY = 'astra-beat-rec-prefs-v1';
  const PREPARATION_MS = 3000;
  const SNAP_TOLERANCE = 0.08;
  const CLUSTER_WINDOW = 0.025;
  const EDITABLES = 'input:not([type=button]):not([type=checkbox]):not([type=radio]):not([type=range]),textarea,select,[contenteditable]:not([contenteditable="false"]),[role="textbox"],[role="searchbox"],[role="combobox"]';
  const POPOVERS = '.editor-command-menu[open],.volume-popover[open],.transport-options[open],.transport-speed-popup[open],#rhythm-context-menu:not([hidden])';

  const prefs = loadPrefs();
  let phase = 'idle'; // 'idle' | 'armed' | 'recording' | 'review'
  let passNumber = 1;
  let currentPass = [];      // [{id, tapQpc, heardQpc, previewSec, finalSec, snap, rawSnapped, status, matchedNote, rippleAt}]
  let lastCompletedPass = []; // Cubase Keep Last archive on R->L cycle wrap
  let ghostPass = [];        // Visual-only dimmed reference of lastCompletedPass during next cycle pass
  let bHeld = false;
  let swallowDeleteKey = null;
  let mutedOldMetronome = false;
  let preRollActive = false;
  let preRollTargetL = 0;
  let startAnchorSec = null;
  let takeEndSec = null;
  let reviewDraft = null, reviewDrag = null, applyQueuedSeq = null;
  let lastReviewHover = null, exitFade = null;
  let lastHudEl = null, lastHudHtml = '';
  const REGION_FLOOR = 1e-4, REGION_STRIP_TOP = 1, REGION_STRIP_HEIGHT = 5.5;
  const CUT_HALF_GAP = 2.5, CUT_ANIM_MS = 380;
  const FADE_MS = 190, GHOST_STRIP_MS = 180, EXIT_FADE_MS = 160, TAP_BLOOM_MS = 220;
  const MAX_DRAFT_UNDO = 200;
  function easeOutCubic(t) { const u = 1 - Math.min(1, Math.max(0, t)); return 1 - u * u * u; }
  function cutSpringState(sec, now) {
    const anim = reviewDraft?.cutAnims?.get(sec);
    if (!anim || reduceMotion?.matches) return { halfGap: CUT_HALF_GAP, active: false, p: 1 };
    const p = Math.min(1, Math.max(0, (now - anim.at) / CUT_ANIM_MS));
    if (p >= 1) {
      reviewDraft.cutAnims.delete(sec);
      return { halfGap: CUT_HALF_GAP, active: false, p: 1 };
    }
    const base = CUT_HALF_GAP * easeOutCubic(Math.min(1, p / 0.24));
    const recoil = 3.2 * Math.sin(Math.PI * Math.min(1, p / 0.54)) * Math.exp(-3.4 * p);
    return { halfGap: base + recoil, active: true, p };
  }
  function inRegion(seconds, r) {
    if (!r || !Number.isFinite(seconds)) return false;
    return (r.includeStart === false ? seconds > r.start : seconds >= r.start) &&
           (r.includeEnd === false ? seconds < r.end : seconds <= r.end);
  }
  function draftRegions() { return reviewDraft?.regions || []; }
  function draftRegionAt(seconds, regions = draftRegions()) {
    // At a shared cut (A.includeEnd === false, B.includeStart === true), right owns exact-cut taps.
    for (let i = regions.length - 1; i >= 0; i--) {
      const r = regions[i];
      if (inRegion(seconds, r)) return r;
    }
    return null;
  }
  function inDraft(i) {
    return !reviewDraft.deleted.has(i) && Boolean(draftRegionAt(reviewDraft.op.beats[i].seconds));
  }
  function regionProjection(regions = draftRegions(), sort = false) {
    const groups = [];
    for (const r of regions) {
      const incS = r.includeStart !== false, incE = r.includeEnd !== false;
      const p = groups.at(-1);
      if (p && p.end === r.start && (p.includeEnd || incS)) {
        p.end = r.end;
        p.includeEnd = incE;
      } else {
        groups.push({ start: r.start, end: r.end, includeStart: incS, includeEnd: incE, ids: [] });
      }
    }
    reviewDraft.op.beats.forEach((b, i) => {
      if (!reviewDraft.deleted.has(i)) draftRegionAt(b.seconds, groups)?.ids.push(i);
    });
    if (sort) for (const g of groups) g.ids.sort((a, b) => reviewDraft.op.beats[a].seconds - reviewDraft.op.beats[b].seconds);
    return groups.filter(g => g.ids.length);
  }
  function hasDraftWrite() { return Boolean(reviewDraft && regionProjection().length); }
  function setLaneCursor(lane, role) {
    if (!lane?.style) return;
    const fallback = { cut: 'col-resize', beat: 'crosshair', erase: 'crosshair', range: 'crosshair' };
    const css = !role ? '' : (window.AstraCursors?.css?.(role) || fallback[role] || role);
    if (lane.style.cursor !== css) lane.style.cursor = css;
  }
  let recMaskVersion = 0, reviewMaskVersion = 0;
  function opSignature(op) { return op ? JSON.stringify(op) : ''; }
  function captureDraftSnapshot() {
    return {
      regions: reviewDraft.regions.map(r => ({
        start: r.start,
        end: r.end,
        includeStart: r.includeStart !== false,
        includeEnd: r.includeEnd !== false,
      })),
      beats: reviewDraft.op.beats.map(b => ({ ...b })),
      edited: [...reviewDraft.edited],
      manualStatus: [...(reviewDraft.manualStatus?.entries() || [])],
      deleted: [...reviewDraft.deleted],
      free: [...reviewDraft.free],
      selectedRegionIndex: reviewDraft.selectedRegion ? reviewDraft.regions.indexOf(reviewDraft.selectedRegion) : -1,
      selectedBeats: [...reviewSelected],
      regionEdited: Boolean(reviewDraft.regionEdited),
      snapToMidi: Boolean(prefs.snapToMidi),
      meterResolution: prefs.meterResolution,
    };
  }
  function pushDraftUndo() {
    if (!reviewDraft) return;
    reviewDraft.undoStack.push(captureDraftSnapshot());
    if (reviewDraft.undoStack.length > MAX_DRAFT_UNDO) reviewDraft.undoStack.shift();
    reviewDraft.redoStack.length = 0;
  }
  function spawnGhostStrip(r) {
    if (!reviewDraft || !r || reduceMotion?.matches) return;
    reviewDraft.ghostStrips.push({ start: r.start, end: r.end, at: performance.now() });
    scheduleCoveredFade();
  }
  function restoreDraftSnapshot(snap) {
    if (!reviewDraft || !snap) return;
    const prevRegions = reviewDraft.regions;
    const nextRegions = snap.regions.map(r => ({
      start: r.start,
      end: r.end,
      includeStart: r.includeStart !== false,
      includeEnd: r.includeEnd !== false,
    }));
    // Ghost any strip that just vanished (e.g. redoing a strip delete) for smooth dissolve
    for (const oldR of prevRegions) {
      const stillCovered = nextRegions.some(nr => nr.start <= oldR.start + 1e-9 && nr.end >= oldR.end - 1e-9);
      if (!stillCovered) spawnGhostStrip(oldR);
    }
    reviewDraft.regions = nextRegions;
    reviewDraft.op = { ...reviewDraft.op, beats: snap.beats.map(b => ({ ...b })) };
    reviewDraft.edited = new Set(snap.edited);
    reviewDraft.manualStatus = new Map(snap.manualStatus || []);
    reviewDraft.deleted = new Set(snap.deleted);
    reviewDraft.free = new Set(snap.free);
    reviewDraft.lastPoints?.clear?.();
    reviewDraft.selectedRegion = snap.selectedRegionIndex >= 0 ? (nextRegions[snap.selectedRegionIndex] || null) : null;
    reviewDraft.regionEdited = Boolean(snap.regionEdited);
    reviewDraft.projected = null;
    reviewDraft.hover = null;
    ++reviewMaskVersion;
    reviewSelected.clear();
    for (const i of snap.selectedBeats) reviewSelected.add(i);
    let prefChanged = false;
    if (Boolean(snap.snapToMidi) !== Boolean(prefs.snapToMidi)) {
      prefs.snapToMidi = Boolean(snap.snapToMidi);
      prefChanged = true;
    }
    if (snap.meterResolution && snap.meterResolution !== prefs.meterResolution) {
      prefs.meterResolution = snap.meterResolution;
      prefChanged = true;
    }
    if (prefChanged) savePrefs();
    applyQueuedSeq = null;
    const nextSig = opSignature(buildOperation());
    const nextGroups = regionProjection(draftRegions(), true);
    if (nextSig && nextSig === reviewDraft.lastPreviewSig && previewData && !previewLoading) {
      reviewGridRegions = nextGroups;
      syncUI();
      requestRollDraw();
      requestOverlayRepaint();
    } else {
      previewData = null;
      reviewGridRegions = nextGroups;
      runPreview();
      requestRollDraw();
      requestOverlayRepaint();
    }
  }
  function draftHistory(direction) {
    if (phase !== 'review' || !reviewDraft || committing) return false;
    if (reviewDrag) {
      releaseReviewDrag();
      requestRollDraw();
      requestOverlayRepaint();
      return true;
    }
    const isUndo = direction === 'undo';
    const fromStack = isUndo ? reviewDraft.undoStack : reviewDraft.redoStack;
    const toStack = isUndo ? reviewDraft.redoStack : reviewDraft.undoStack;
    while (fromStack.at(-1)?.project) {
      const d = doc();
      if (isUndo ? d?.can_undo : d?.can_redo) {
        fromStack.pop();
        toStack.push({ project: true });
        projectHistoryPendingAt = performance.now();
        return 'project';
      }
      fromStack.pop();
    }
    if (!fromStack.length) return false;
    const snap = fromStack.pop();
    toStack.push(captureDraftSnapshot());
    restoreDraftSnapshot(snap);
    return true;
  }
  const reviewSelected=new Set();
  let reviewGridTempo=null,reviewGridRegions=null;
  // Recording-time authoritative grid: the same backend preview as review,
  // throttled, so tapped spans are drawn by the ordinary meter/beat grid.
  let recGridTempo=null,recGridSpan=null,recPreviewSeq=0,recPreviewBusy=false,recPreviewDirty=false,recPreviewTimer=0,recPreviewSentAt=-Infinity;
  const REC_PREVIEW_MS=250;
  // Project edits made outside the take while reviewing are undone through
  // the project history; a marker on the draft stack keeps one linear order.
  let projectHistoryPendingAt=-Infinity;
  let preparation = null, prepRaf = 0, prepTimer = 0, captureStartedMs = -Infinity;
  let sweptPasses = new Map(), sweptNow = null, sweptPixel = null, sweptDrawAt = -Infinity;
  const coveredAt=new Map();
  const reduceMotion=typeof matchMedia==='function'?matchMedia('(prefers-reduced-motion: reduce)'):null;
  let coveredFadeFrame=0;
  let previewData = null;    // Latest authoritative AED_PREVIEW response
  let previewSeq = 0;
  let previewError = '';
  let previewLoading = false;
  let committing = false;
  let sessionScope = null;
  let sessionRange = null;
  let armTarget = null;
  let armedPlayRequested = false;
  let awaitingInitialPlaybackClock = false;
  let earlyTaps = [];
  let retryTarget = null;
  let internalSeekDepth = 0;
  let clockTimer = 0, clockProbeOutstanding = false, clockWatchGeneration = 0;
  let lastUsableClockAt = -Infinity, clockStartAt = 0;
  let glitchDetected = false;
  let lastTapFeedback = null; // {at, status, deltaMs}
  let rippleRaf = 0;

  // Authoritative dual-anchor QPC clock tracker
  let clockOffsetMs = null, clockOffsetAt = 0;
  let lastClock = null;
  let samples = []; // [{qpc, pos, rate, loop, underruns, routeId}]
  let lastPacketQpc = -Infinity;
  let takeTaps = [];
  let takeGroups = new Map();
  let pendingTaps = new Set();
  let tapsDirty = false;
  let tapIdSeq = 0;

  // Cached sorted note-on clusters from editor().notes (n.start ONLY)
  let onsetCache = { revision: null, notes: null, clusters: [] };

  function loadPrefs() {
    const def = {
      snapToMidi: true,
      lrPunch: false,
      preRollBars: 0,
      muteOldMetronome: true,
      offsetMs: 0,
      meterResolution: 'delete',
    };
    try {
      const raw = window.localStorage?.getItem(PREFS_KEY);
      if (!raw) return def;
      const p = JSON.parse(raw);
      return {
        snapToMidi: p.snapToMidi !== false,
        lrPunch: Boolean(p.lrPunch),
        preRollBars: [0, 1, 2].includes(Number(p.preRollBars)) ? Number(p.preRollBars) : 0,
        muteOldMetronome: p.muteOldMetronome !== false,
        offsetMs: Number.isFinite(Number(p.offsetMs)) ? Math.max(-150, Math.min(150, Math.round(Number(p.offsetMs)))) : 0,
        meterResolution: ['delete', 'previous', 'next'].includes(p.meterResolution) ? p.meterResolution : 'delete',
      };
    } catch { return def; }
  }
  function savePrefs() {
    try { window.localStorage?.setItem(PREFS_KEY, JSON.stringify(prefs)); } catch {}
  }

  const runtimeReady = () => { try { return Boolean(state) && typeof tab === 'string'; } catch { return false; } };
  const ed = () => window.AstraProjectEditor;
  const doc = () => ed()?.editor?.() || null;
  const rng = () => ed()?.range?.() || window.AstraTransportPanel?.range?.() || { start: 0, end: 0, loop: false };
  const hasLR = () => { const r = rng(); return Number.isFinite(r.start) && Number.isFinite(r.end) && r.end > r.start + 1e-4; };
  const takeRange = () => sessionRange || rng();
  const activePunch = () => sessionRange ? sessionRange.punch : Boolean(prefs.lrPunch && hasLR());
  function captureTakeRange() { const r = rng(); return {...r, punch:Boolean(prefs.lrPunch && hasLR())}; }
  const isPlayingNow = () => {
    if (!runtimeReady()) return false;
    const src = (typeof transportMidi !== 'undefined' && transportMidi) ? state.midi : state.player;
    if (src && typeof src.playing === 'boolean') return src.playing;
    return Boolean(phase !== 'idle' && lastClock && lastClock.clock_state === 'playing');
  };
  const canRecord = () => {
    if (!runtimeReady() || tab !== 'notes') return false;
    if (!ed()?.active?.()) return false;
    const d = doc();
    if (!d || !d.revision) return false;
    const isEd = typeof ed()?.rhythmEditable === 'function' ? ed().rhythmEditable() : (typeof ed()?.editable === 'function' ? ed().editable() : true);
    return Boolean(isEd || phase !== 'idle');
  };

  function currentDocScope() {
    const d = doc();
    if (!d) return null;
    const stamp = d.edit_stamp || {};
    return {
      project: state.project?.path || '',
      generation: stamp.generation,
      version: stamp.version,
      source_key: d.source_key || '',
      revision: d.revision || '',
      allow_audio_audition: true,
    };
  }

  function updateClockOffset(sentQpcSec, nowMs) {
    if (!Number.isFinite(sentQpcSec)) return;
    const candidate = nowMs - sentQpcSec * 1000;
    if (clockOffsetMs === null) {
      clockOffsetMs = candidate;
      clockOffsetAt = nowMs;
    } else {
      const drifted = clockOffsetMs + Math.max(0, nowMs - clockOffsetAt) * 0.001;
      clockOffsetMs = Math.min(candidate, drifted);
      clockOffsetAt = nowMs;
    }
  }

  function nowToQpcSec(perfMs) {
    if (clockOffsetMs === null) return perfMs / 1000;
    return (perfMs - clockOffsetMs) / 1000;
  }

  function notifyBackendActive(active) {
    try {
      if (typeof bridge?.setBeatRecording === 'function') {
        bridge.setBeatRecording(JSON.stringify({
          active: Boolean(active),
          phase: phase === 'preparing' ? 'recording' : phase,
        }), raw => {
          try {
            const res = JSON.parse(raw);
            if (res?.recording_clock) {
              const clk=res.recording_clock;
              if (phase==='armed' && armTarget && !armTarget.route && clk.route_id && clk.clock_state==='paused') {
                // This setBeatRecording acknowledgement was sampled before the
                // ordered pre-roll and Play commands; seed the locator fence.
                armTarget.route=clk.route_id;
                armTarget.generation=clk.locate_generation ?? null;
                armTarget.alreadyThere=Math.abs(clk.rendered_source_sec-armTarget.position)<1e-4;
              }
              ingestClock(clk, performance.now());
            }
          } catch {}
        });
      }
    } catch {}
  }

  function getOnsetClusters() {
    const d = doc();
    if (!d || !Array.isArray(d.notes)) return [];
    if (onsetCache.revision === d.revision && onsetCache.notes === d.notes) return onsetCache.clusters;
    const starts = [];
    for (const n of d.notes) {
      const s = Number(n?.start);
      if (Number.isFinite(s) && s >= 0) starts.push({ start: s, note: n });
    }
    starts.sort((a, b) => a.start - b.start);
    const clusters = [];
    const maxSpan = CLUSTER_WINDOW * 1.5;
    for (const item of starts) {
      const last = clusters[clusters.length - 1];
      if (!last || item.start - last.end > CLUSTER_WINDOW || item.start - last.onset > maxSpan) {
        clusters.push({ onset: item.start, end: item.start, count: 1, note: item.note });
      } else {
        last.end = item.start;
        last.count += 1;
      }
    }
    onsetCache = { revision: d.revision, notes: d.notes, clusters };
    return clusters;
  }

  function liveSnapPoint(rawSec, prevSec, wantSnap) {
    if (!wantSnap) return { seconds: rawSec, status: 'raw', note: null };
    const clusters = getOnsetClusters();
    if (!clusters.length) return { seconds: rawSec, status: 'no_nearby_onset', note: null };
    const dur = Number(doc()?.duration) || 0;
    const leftClear = Number.isFinite(prevSec) ? Math.max(0, (rawSec - prevSec) * 0.35) : SNAP_TOLERANCE;
    const rad = Math.min(SNAP_TOLERANCE, leftClear);
    const lo = Math.max(0, rawSec - rad), hi = dur > 0 ? Math.min(dur, rawSec + rad) : rawSec + rad;
    let l = 0, r = clusters.length;
    while (l < r) { const m = (l + r) >>> 1; if (clusters[m].onset < lo) l = m + 1; else r = m; }
    let best = null, first = Infinity, second = Infinity, nearby = 0;
    for (let i = l; i < clusters.length && clusters[i].onset <= hi; i++) {
      const c = clusters[i], d = Math.abs(c.onset - rawSec);
      nearby++;
      if (d < first) { second = first; first = d; best = c; }
      else if (d < second) second = d;
    }
    if (!best) return { seconds: rawSec, status: 'no_nearby_onset', note: null };
    const status = (nearby > 1 && second - first < 0.015) ? 'ambiguous_onset' : 'midi_onset';
    return { seconds: best.onset, status, note: best.note };
  }

  function currentFallbackPosition() {
    if (lastClock && Number.isFinite(lastClock.rendered_source_sec)) {
      const nowMs = performance.now();
      const qpc = nowToQpcSec(nowMs);
      const anchor = samples[samples.length - 1];
      if (anchor && lastClock.clock_state === 'playing') {
        const dt = Math.max(0, Math.min(0.4, qpc - anchor.qpc));
        return anchor.pos + dt * (anchor.rate || 1);
      }
      return lastClock.rendered_source_sec;
    }
    if (typeof midiDisplayPosition === 'function' && typeof transportMidi !== 'undefined' && transportMidi) {
      return midiDisplayPosition();
    }
    return Number(state?.player?.position ?? state?.midi?.position ?? 0) || 0;
  }

  function delayFor(clk) {
    return clk?.position_basis !== 'dac' && Number.isFinite(clk?.latency_wall_sec) ? clk.latency_wall_sec : 0;
  }

  // One segment uses a single fixed mapping; UI locators are never retroactively read.
  function clockLoop(clk) {
    const l = clk?.loop;
    return l?.enabled && Number.isFinite(l.start) && Number.isFinite(l.end) && l.end > l.start + 1e-4
      ? { start: l.start, end: l.end } : null;
  }

  function sameSegment(a, b) {
    return a.routeId === b.routeId && a.generation === b.generation && a.basis === b.basis &&
      Math.abs(a.rate - b.rate) < 1e-5 && a.loop?.start === b.loop?.start && a.loop?.end === b.loop?.end;
  }

  function inferLoopAdvance(p0, p1, span, expected, error) {
    const base = p1 - p0;
    const first = Math.ceil((Math.max(0, expected - error) - base) / span);
    const last = Math.floor((expected + error - base) / span);
    return first === last ? base + first * span : null;
  }

  function locateOnSegment(t) {
    if (!samples.length || t < samples[0].qpc - 1e-7) return null;
    let lo = 0, hi = samples.length;
    while (lo < hi) {
      const mid = (lo + hi) >>> 1;
      if (samples[mid].qpc <= t + 1e-7) lo = mid + 1;
      else hi = mid;
    }
    const a = samples[Math.max(0, lo - 1)], b = samples[lo];
    if (!b && t - a.qpc > 0.4) return null;
    const x = b
      ? a.x + (b.x - a.x) * ((t - a.qpc) / (b.qpc - a.qpc))
      : a.x + Math.max(0, t - a.qpc) * a.rate;
    const l = a.loop;
    const span = l ? l.end - l.start : 0;
    const pass = l && x >= l.start ? Math.floor((x - l.start + 1e-9) / span) : 0;
    return { pass, seconds: x - pass * span, settled: !!b || Math.abs(t-a.qpc)<1e-7 };
  }

  function refinePendingTaps() {
    if (!takeTaps.length) return;
    for (const tap of pendingTaps) {
      const refined = locateOnSegment(tap.heardQpc);
      if (!refined) continue;
      if (refined.pass !== tap.result.pass || refined.seconds !== tap.result.seconds) tapsDirty = true;
      tap.result = refined;
      if (refined.settled) pendingTaps.delete(tap);
    }
    if (tapsDirty) {
      const dur = Number(doc()?.duration) || 1e9;
      const groups = new Map();
      for (const tap of takeTaps) {
      if (!tap.result) continue;
      const list = groups.get(tap.result.pass) || [];
      let sec = Math.max(0, Math.min(dur, tap.result.seconds));
      if (activePunch()) {
        const r = takeRange();
        if (sec < r.start - 0.02 || sec > r.end + 0.02) continue;
        sec = Math.max(r.start, Math.min(r.end, sec));
      }
      const prev = list[list.length - 1]?.finalSec ?? null;
      if (prev !== null && sec <= prev + 0.03) continue;
      const snap = liveSnapPoint(sec, prev, tap.snap);
      list.push({...tap, finalSec: sec, previewSec: sec, snappedSec:snap.seconds,
        status:snap.status, matchedNote:snap.note});
        groups.set(tap.result.pass,list);
      }
      takeGroups = groups;
      tapsDirty = false;
    }
    const groups = takeGroups;
    const heard = nowToQpcSec(performance.now()) - delayFor(lastClock);
    const present = locateOnSegment(heard);
    const latest = Math.max(0,...groups.keys());
    const active = Math.max(latest,present?.pass ?? 0);
    passNumber = active + 1;
    currentPass = groups.get(active) || [];
    let prev = -1;
    for (const pass of groups.keys()) if (pass < active && pass > prev) prev = pass;
    lastCompletedPass = groups.get(prev) || [];
    ghostPass = currentPass.length ? [] : lastCompletedPass;
  }

  function sweepBounds() {
    if(activePunch()){
      const r=takeRange();
      return [r.start,r.end];
    }
    return [0,Number(doc()?.duration)||0];
  }

  function extendSweep(pass, seconds, loop, repaint=true) {
    if (phase!=='recording' || !Number.isFinite(seconds)) return;
    const [lower,upper]=sweepBounds();
    if(upper<=lower)return;
    const old=sweptNow;
    const advanced=!!(old && loop && pass>old.pass);
    if(advanced) {
      // A proven audible wrap closes the preceding pass at R. Do not give
      // an empty new pass that old interval or infer any intermediate taps.
      old.end=Math.max(old.end,Math.min(upper,loop.end));
    }
    let span=sweptPasses.get(pass);
    if (!span && seconds<lower && !advanced) return;
    if (span && seconds<lower && !loop) return;
    const projected=Math.max(lower,Math.min(upper,seconds));
    if(!span) {
      if(seconds>upper && !advanced) return;
      const start=advanced ? Math.max(lower,Math.min(upper,loop.start)) : projected;
      span={pass,start,end:projected};
      // Linear pre-roll can last across several user-defined meters.
      // If evidence shows L was crossed between these samples, start at L.
      if(preRollActive && activePunch() && seconds>=lower) span.start=lower;
      sweptPasses.set(pass,span);
    } else {
      span.start=Math.min(span.start,projected);
      span.end=Math.max(span.end,projected);
    }
    if(!repaint)return;
    sweptNow=span;
    // The flag lane contains old-point ink as well as the live band. Redraw
    // only when the advancing edge moves to another pixel, at most 30 Hz.
    const slot=typeof midiX==='function'?Math.round(midiX(span.end)):span.end;
    const now=performance.now();
    if((old?.pass!==pass || sweptPixel!==slot) && now-sweptDrawAt>=32){
      sweptPixel=slot;sweptDrawAt=now;++recMaskVersion;requestBeatLaneDraw();
      if(phase==='recording'&&(currentPass.length>0||ghostPass.length>0))requestRollDraw();
    }
  }

  function resetSweep() {
    if(coveredFadeFrame){cancelAnimationFrame(coveredFadeFrame);coveredFadeFrame=0;}
    coveredAt.clear();
    sweptPasses=new Map();sweptNow=null;sweptPixel=null;sweptDrawAt=-Infinity;
  }

  function sealClock(reason) {
    if (phase === 'recording') {
      glitchDetected = true;
      finishRecording(reason);
      if (phase === 'armed') cleanup('idle');
    }
    samples = [];
  }

  function clockMissing() {
    return (phase==='recording' || (phase==='armed' && armedPlayRequested)) &&
      performance.now()-Math.max(clockStartAt,lastUsableClockAt) > 1200;
  }

  function watchClock() {
    if (clockTimer || typeof setTimeout !== 'function' || !['armed','preparing','recording'].includes(phase)) return;
    clockTimer=setTimeout(()=>{
      clockTimer=0;
      if (!['armed','preparing','recording'].includes(phase)) return;
      if (clockMissing() || performance.now()-lastUsableClockAt > 220) {
        if (!clockProbeOutstanding && typeof bridge?.recordingClock === 'function') {
          clockProbeOutstanding=true;
          const generation=clockWatchGeneration;
          bridge.recordingClock(raw=>{
            if(generation!==clockWatchGeneration)return;
            clockProbeOutstanding=false;
            if(!['armed','preparing','recording'].includes(phase))return;
            try {ingestClock(JSON.parse(raw),performance.now());} catch {}
          });
        }
        syncUI();
      }
      watchClock();
    },250);
  }

  function ingestClock(clk, nowMs) {
    if (!clk || typeof clk !== 'object') return;
    const sent = Number(clk.sent_qpc_sec);
    if (Number.isFinite(sent) && sent < lastPacketQpc) return;
    if (Number.isFinite(sent)) lastPacketQpc = sent;
    updateClockOffset(clk.sent_qpc_sec, nowMs);
    const prevClock = lastClock;
    if (!committing && phase !== 'idle' && sessionScope && clk.doc_scope) {
      if (clk.doc_scope.project !== sessionScope.project ||
          (sessionScope.revision && clk.doc_scope.revision &&
            clk.doc_scope.revision !== sessionScope.revision && !committing)) {
        abort('文档已变更，已取消当前录拍');
        return;
      }
    }
    if(phase==='preparing'){lastClock=clk;return;}
    if (awaitingInitialPlaybackClock && clk.clock_state!=='playing') { lastClock=clk; return; }
    if (clk.clock_state === 'seeking' || clk.rate_pending) {
      // Finish with the sealed prior take; do not install a new anchor into it.
      sealClock('seek');
      if (phase==='armed' && armTarget && clk.route_id) armTarget.seekSeen=true;
      lastClock = clk;
      return;
    }
    if (clk.clock_state === 'paused' || clk.clock_state === 'stopped') {
      if (phase === 'recording' && prevClock?.clock_state === 'playing') finishRecording('transport_stop');
      samples = [];
      lastClock = clk;
      if(phase==='armed' && prevClock?.clock_state!==clk.clock_state) syncUI();
      return;
    }
    if (clk.clock_state !== 'playing') {
      if (phase === 'recording') sealClock('transport_gap');
      lastClock = clk;
      return;
    }
    // Older/late native status sometimes has a position and host QPC but not
    // a dedicated sample stamp. Both use the same steady clock. Use the
    // producer's sent stamp in that case, not a visual browser playhead.
    const qpc = Number.isFinite(clk.sampled_qpc_sec) ? clk.sampled_qpc_sec : clk.sent_qpc_sec;
    const pos = Number(clk.rendered_source_sec);
    if (!Number.isFinite(qpc) || !Number.isFinite(pos)) return;
    lastUsableClockAt=nowMs;
    const firstRunningClock=awaitingInitialPlaybackClock;
    awaitingInitialPlaybackClock=false;
    const a = {qpc,pos,x:pos,rate:clk.rate || 1,loop:clockLoop(clk),
      underruns:clk.underruns || 0,routeId:clk.route_id,
      generation:clk.locate_generation ?? null,basis:clk.position_basis || 'rendered'};
    const last = samples[samples.length - 1];
    if (last && qpc <= last.qpc + 1e-7) return;
    if (last && !sameSegment(last,a)) sealClock('locator_change');
    else if (last) {
      const expected = (qpc - last.qpc) * a.rate;
      // Snapshot cadence and block quantization cause small differences from ideal advance.
      // Ambiguous modulo estimates are intentionally not interpreted as loop wraps.
      const tolerance = (a.basis === 'dac' ? 0.035 : 0.08) * Math.max(1,a.rate);
      let delta = pos - last.pos;
      if (a.loop && last.pos >= a.loop.start - 1e-7 && pos >= a.loop.start - 1e-7)
        delta = inferLoopAdvance(last.pos,pos,a.loop.end-a.loop.start,expected,tolerance);
      else if (delta < -1e-7 || Math.abs(delta-expected) > tolerance) delta = null;
      if (delta === null) {
        // A late or repeated host sample is not evidence of a user seek.
        // Freeze uncertain chronology and wait rather than ending a normal
        // take at the first scheduling hitch. Larger unexplained relocations
        // must still sever the take.
        const drift=pos-last.pos-expected;
        if ((!a.loop && Math.abs(drift) > 0.36) ||
            (a.loop && Math.abs(drift) > Math.max(0.36,(a.loop.end-a.loop.start)*2))) {
          sealClock('position_gap');
        } else {
          glitchDetected=true;
          return;
        }
      } else {
        a.x = last.x + delta;
        if (a.underruns > last.underruns) {
          glitchDetected=true;
          // Preserve existing provisional taps, but do not refine them
          // across an output underrun using a false continuous bracket.
          pendingTaps.clear();
          samples=[];
        }
      }
    }
    lastClock = clk;
    samples.push(a);
    if (firstRunningClock && earlyTaps.length) {
      for (const raw of earlyTaps) {
        const heard=nowToQpcSec(raw.rawMs)-delayFor(clk)-prefs.offsetMs/1000;
        const dt=heard-a.qpc;
        if (dt < -0.24 || dt > 0.4) continue;
        const x=a.pos+dt*a.rate;
        // With no previous anchor a wrap before the first sample is not known.
        if (a.loop && (x<a.loop.start || x>=a.loop.end)) continue;
        const result={pass:0,seconds:x,settled:dt<=0};
        const tap={...raw,heardQpc:heard,result};
        takeTaps.push(tap);
        if(!result.settled) pendingTaps.add(tap);
        tapsDirty=true;
      }
      earlyTaps=[];
    }
    if (phase === 'armed') {
      // Arming does not play or record. An unrelated running transport or late
      // receipt can refresh our clock estimate but cannot begin this take.
      if (!armedPlayRequested || nowToQpcSec(clockStartAt)>a.qpc) { samples=[]; if(prevClock?.clock_state!=='playing')syncUI(); return; }
      // The arming command may still be parked or queued. A stale Play packet
      // at the old locator must not open recording before pre-roll is heard.
      if (armTarget) {
        const sameRoute = a.routeId === armTarget.route;
        const sameGeneration = sameRoute && armTarget.generation !== null &&
          a.generation === armTarget.generation;
        // A route with a real accepted locator generation cannot start on a
        // matching old generation. On compatibility clocks require a nearby
        // fresh position instead of treating the visual seek as evidence.
        const notYetLocated = a.qpc < armTarget.requestQpc ||
          (sameGeneration && !armTarget.alreadyThere) ||
          // Once this locator has rendered, a delayed first packet may already
          // be well beyond the parked position. Never require a brief window
          // that can be missed permanently on a busy UI or audio thread.
          (a.generation === null && !a.loop && a.pos < armTarget.position - 0.04) ||
          (!armTarget.route && !armTarget.seekSeen && Math.abs(a.pos-armTarget.position) > 0.18);
        if (notYetLocated) {
          samples = [];
          return;
        }
      }
      armTarget = null;
      captureStartedMs=clockStartAt;
      enterRecordingState({allowPreRoll:true});
    }
    if (phase === 'recording') {
      const beforePass = passNumber, beforePreRoll = preRollActive, beforeCurrent = currentPass;
      refinePendingTaps();
      if (samples.length > 128) samples.shift();
      const r = takeRange();
      // Coverage is a presentation of this take's audible range, not the
      // visual playback head or the configured punch limit.
      const heard=locateOnSegment(nowToQpcSec(nowMs)-delayFor(clk));
      if(heard)extendSweep(heard.pass,heard.seconds,a.loop);
      if (activePunch()) {
        if (preRollActive && heard && heard.seconds >= preRollTargetL - 1e-6) preRollActive = false;
        if (!a.loop && heard && heard.seconds >= r.end - 1e-6) {
          takeEndSec = r.end;
          finishRecording('punch_out');
          if (typeof withPlayer === 'function') withPlayer('playPause','midiPlayPause');
        }
      }
      if (beforePass !== passNumber || beforePreRoll !== preRollActive || beforeCurrent !== currentPass) syncUI();
      requestOverlayRepaint();
    } else if (samples.length > 128) samples.shift();
  }

  function onPlayback(packet) {
    if (!runtimeReady()) return;
    const nowMs = performance.now();
    if (packet?.recording_clock) {
      ingestClock(packet.recording_clock, nowMs);
      return;
    }
  }

  function setMetronomeSilenced(silence) {
    const d = doc();
    if (!d) return;
    if (silence) {
      if (prefs.muteOldMetronome && d.metronome && !mutedOldMetronome) {
        mutedOldMetronome = true;
        ed()?.action?.('editorMetronome');
      }
    } else if (mutedOldMetronome) {
      mutedOldMetronome = false;
      if (!d.metronome) ed()?.action?.('editorMetronome');
    }
  }

  function preRollStartSeconds() {
    const r = takeRange();
    if (!activePunch() || !prefs.preRollBars) return r.start;
    const d = doc();
    if (!d?.tempo || typeof secondsToBeat !== 'function' || typeof beatToSeconds !== 'function') {
      return Math.max(0, r.start - prefs.preRollBars * 2.0);
    }
    const qL = secondsToBeat(r.start);
    const sigs = d.tempo.time_signatures || [{ beat: 0, numerator: 4, denominator: 4 }];
    let sig = sigs[0];
    for (const s of sigs) { if (s.beat <= qL + 1e-6) sig = s; else break; }
    const barQ = (Number(sig.numerator) || 4) * (4 / (Number(sig.denominator) || 4));
    return Math.max(0, beatToSeconds(Math.max(0, qL - prefs.preRollBars * barQ)));
  }

  function closeRecordMenu() {
    document.querySelectorAll?.('.beat-rec-menu[open]').forEach(m => { m.open = false; });
  }

  function clearPreparation() {
    preparation=null;
    if(prepTimer){clearTimeout(prepTimer);prepTimer=0;}
    if(prepRaf){cancelAnimationFrame(prepRaf);prepRaf=0;}
  }

  function startPreparation(startTransport,routeCurrent) {
    phase='preparing';
    const session=preparation={started:performance.now()};
    notifyBackendActive(true);
    $('roll-scroll')?.focus?.({preventScroll:true});
    syncUI();
    const tick=now=>{
      prepRaf=0;
      if(phase!=='preparing'||preparation!==session)return;
      const frac=Math.max(0,Math.min(1,(now-session.started)/PREPARATION_MS));
      $('transport-beat-rec-btn')?.style?.setProperty('--beat-prep-ring',`${frac*100}%`);
      if(frac<1)prepRaf=requestAnimationFrame(tick);
    };
    const launch=()=>{
      prepTimer=0;
      if(phase!=='preparing'||preparation!==session)return;
      const remaining=PREPARATION_MS-(performance.now()-session.started);
      if(remaining>0){prepTimer=setTimeout(launch,Math.ceil(remaining));return;}
      if(tab!=='notes'||!reviewScopeCurrent()||routeCurrent?.()===false||isPlayingNow()){cancel();return;}
      clearPreparation();
      phase='armed';
      // Re-admit only the pending, synchronous internal pre-roll park. The
      // front end still owns the manual-seek lock throughout this transition.
      notifyBackendActive(true);
      beforeTransportPlay();
      if(phase!=='armed'||!armedPlayRequested)return;
      try{if(startTransport()===false)cancel();}
      catch(error){abort(error?.message||'无法开始录拍');}
    };
    prepRaf=requestAnimationFrame(tick);
    prepTimer=setTimeout(launch,PREPARATION_MS);
  }

  function enterRecordingState({ allowPreRoll = true } = {}) {
    closeRecordMenu();
    phase = 'recording';
    resetSweep();
    takeTaps = [];
    takeGroups = new Map();
    pendingTaps.clear();
    tapsDirty = false;
    window.AstraProjectEditor?.cancelRecordingSeekPreview?.();
    if (typeof cancelRecordingSeekPreview === 'function') cancelRecordingSeekPreview();
    notifyBackendActive(true);
    passNumber = 1;
    currentPass = [];
    lastCompletedPass = [];
    ghostPass = [];
    previewData = null;
    previewError = '';
    glitchDetected = false;
    takeEndSec = null;
    startAnchorSec = currentFallbackPosition();
    if (allowPreRoll && activePunch()) {
      const r = takeRange();
      preRollTargetL = r.start;
      preRollActive = startAnchorSec < r.start - 0.02;
    } else {
      preRollActive = false;
    }
    setMetronomeSilenced(true);
    // Re-enable playhead follow so canvas tracks while tapping
    try { if (typeof followSuspended !== 'undefined') followSuspended = false; } catch {}
    // Move focus to roll canvas so Space always controls transport
    const roll = $('roll-scroll');
    if (roll && typeof roll.focus === 'function') roll.focus({ preventScroll: true });
    syncUI();
    requestOverlayRepaint();
    requestRollDraw();
  }

  function parkTake(position) {
    if (typeof withPlayer !== 'function' || armTarget?.position === position) return;
    armTarget = {
      position,
      route:lastClock?.route_id,
      generation:lastClock?.locate_generation ?? null,
      requestQpc:clockOffsetMs === null ? -Infinity : nowToQpcSec(performance.now()),
      alreadyThere:Number.isFinite(lastClock?.rendered_source_sec) &&
        Math.abs(lastClock.rendered_source_sec-position) < 1e-4,
    };
    // Allow only the recorder's synchronous parking dispatch through the UI lock.
    ++internalSeekDepth;
    try { withPlayer('seek','midiSeek',{position}); }
    finally { --internalSeekDepth; }
  }

  function arm() {
    if (!canRecord()) return;
    const running = isPlayingNow();
    closeRecordMenu();
    exitFade = null;
    lastReviewHover = null;
    $('roll-scroll')?.focus?.({preventScroll:true});
    sessionScope = currentDocScope();
    if (!sessionScope) return;
    sessionRange = captureTakeRange();
    armTarget = null;
    previewData = null;
    previewError = '';
    currentPass = [];
    lastCompletedPass = [];
    ghostPass = [];
    passNumber = 1;
    takeTaps = [];
    takeGroups = new Map();
    pendingTaps.clear();
    tapsDirty = false;
    samples = [];
    phase = 'armed';
    window.AstraProjectEditor?.cancelRecordingSeekPreview?.();
    if (typeof cancelRecordingSeekPreview === 'function') cancelRecordingSeekPreview();
    ++clockWatchGeneration;
    clockProbeOutstanding=false;
    clockStartAt=performance.now();
    lastUsableClockAt=-Infinity;
    armedPlayRequested=false;
    retryTarget=null;
    startAnchorSec=currentFallbackPosition();
    awaitingInitialPlaybackClock=running;
    earlyTaps=[];
    if (running) {
      // The transport is already running. Join it at the heard clock; never
      // send Play/Pause, park a locator or kick off pre-roll on this path.
      captureStartedMs=performance.now();
      enterRecordingState({allowPreRoll:false});
    } else {
      notifyBackendActive(true);
      syncUI();
      requestRollDraw();
    }
    watchClock();
  }

  function toggle() {
    if (phase === 'idle') { arm(); return; }
    if (phase === 'recording') { stopRecordingOnly(); return; }
    if (phase === 'armed' || phase==='preparing') cancel();
    // An existing preview is resolved explicitly through Apply, Discard or Retry.
  }

  function stopRecordingOnly() {
    if(phase==='preparing'||phase==='armed'){cancel();return;}
    if (phase !== 'recording') return;
    beforeTransportPause();
    finishRecording('record_stop');
  }

  function beforeTransportPause() {
    if (phase !== 'recording') return;
    // Lock takeEndSec BEFORE notes_return_on_pause rewinds playhead!
    const pos = currentFallbackPosition();
    if (Number.isFinite(pos)) takeEndSec = pos;
  }

  function beforeTransportPlay() {
    if (phase!=='armed' || armedPlayRequested) return;
    armedPlayRequested=true;
    clockStartAt=performance.now();
    lastUsableClockAt=-Infinity;
    samples=[];
    // Only now is the silent countdown over; the optional audible L/R
    // lead-in keeps exactly its configured length.
    const target=activePunch()?preRollStartSeconds():retryTarget;
    if(Number.isFinite(target)) {
      startAnchorSec=target;
      parkTake(target);
    }
    retryTarget=null;
    syncUI();
  }

  function onTransportAction(actionName,startTransport,routeCurrent) {
    if (phase === 'idle') return;
    if (actionName === 'playPause' || actionName === 'midiPlayPause') {
      if(phase==='preparing'){cancel();return true;}
      if (phase === 'recording' && isPlayingNow()) {
        beforeTransportPause();
        // Seal the take before the ordinary pause receipt can reset its clock.
        finishRecording('space_pause');
      } else if (phase === 'armed') {
        if (armedPlayRequested) {
          // This is a second toggle while the first requested Play did not
          // become audible. The transport gets its ordinary toggle; sever
          // the take so a late completion cannot revive the recorder.
          cancel();
        } else if (!isPlayingNow() && typeof startTransport==='function') {
          startPreparation(startTransport,routeCurrent);
          return true;
        }
      }
    } else if (actionName === 'stop' || actionName === 'midiStop') {
      if(phase==='preparing'){cancel();return true;}
      if (phase === 'armed' && armedPlayRequested) cancel();
      if (phase === 'recording') {
        beforeTransportPause();
        finishRecording('stop');
      }
    }
  }

  function recordTap(event) {
    if (phase !== 'recording') return false;
    const nowMs = performance.now();
    const rawEventMs = (event && Number.isFinite(event.timeStamp) && event.timeStamp > 0 && Math.abs(nowMs - event.timeStamp) < 250)
      ? event.timeStamp : nowMs;
    if(rawEventMs<captureStartedMs)return true;
    if (lastClock && (lastClock.clock_state === 'seeking' || lastClock.rate_pending)) return false;

    if (!samples.length && awaitingInitialPlaybackClock) {
      if(earlyTaps.length<8) earlyTaps.push({id:++tapIdSeq,rawMs:rawEventMs,
        snap:Boolean(prefs.snapToMidi&&!bHeld),rippleAt:nowMs});
      return true;
    }
    if (lastClock?.clock_state !== 'playing' || !samples.length) return false;
    const tapQpc = nowToQpcSec(rawEventMs);
    const heardQpc = tapQpc - delayFor(lastClock) - prefs.offsetMs / 1000;
    // Avoid extrapolating backward across a locator boundary. A tap without a
    // same-segment estimate is consumed without using the visual playhead.
    const result = locateOnSegment(heardQpc);
    if (!result || (preRollActive && result.seconds < preRollTargetL - 1e-6)) return true;
    const entry = {id:++tapIdSeq,tapQpc,heardQpc,result,
      snap:Boolean(prefs.snapToMidi && !bHeld),rippleAt:nowMs};
    takeTaps.push(entry);
    extendSweep(result.pass,result.seconds,samples[samples.length-1].loop,false);
    if (!result.settled) pendingTaps.add(entry);
    tapsDirty = true;
    refinePendingTaps();
    const placed = currentPass.find(t => t.id === entry.id) || lastCompletedPass.find(t => t.id === entry.id);
    if (placed) lastTapFeedback = {at:nowMs,status:placed.status,
      deltaMs:Math.round((placed.snappedSec-placed.finalSec)*1000)};
    scheduleRippleAnim();
    syncUI();
    // Paint the flag on the same next frame and canvas as the old flag lane.
    // A hover gesture can own the transient overlay but cannot hide a tap.
    ++recMaskVersion;
    window.AstraRhythmTimeline?.clearHover?.();
    scheduleRecordingPreview();
    requestRollDraw();
    requestOverlayRepaint();
    return true;
  }

  function scheduleRippleAnim() {
    if (rippleRaf) return;
    const step = () => {
      rippleRaf = 0;
      const now = performance.now();
      const activeList = phase === 'recording' ? currentPass : [];
      const hasRipple = activeList.some(t => now - t.rippleAt < 260);
      requestOverlayRepaint();
      if (hasRipple && phase === 'recording') rippleRaf = requestAnimationFrame(step);
    };
    rippleRaf = requestAnimationFrame(step);
  }

  function resolveEffectivePass() {
    // Cubase Keep Last: if currentPass has taps use it; else fall back to lastCompletedPass!
    const chosen = currentPass.length > 0 ? currentPass : lastCompletedPass;
    if (!chosen.length) return [];
    const dur = Number(doc()?.duration) || 1e9;
    const cleaned = [];
    for (const tap of chosen) {
      const s = Math.max(0, Math.min(dur, tap.finalSec ?? tap.previewSec));
      if (!cleaned.length || s > cleaned[cleaned.length - 1].seconds + 0.01) {
        cleaned.push({ seconds: s, snap: tap.snap, perTapFree: !tap.snap && prefs.snapToMidi });
      }
    }
    return cleaned;
  }

  function guardedCoverage(rows) {
    if(!rows.length || (!activePunch() && rows.length<2))return null;
    const pass=rows[0]?.result?.pass;
    const sweep=sweptPasses.get(pass);
    const sec=r=>r.finalSec ?? r.previewSec;
    const first=sec(rows[0]),last=sec(rows[rows.length-1]);
    if(!sweep || !Number.isFinite(first) || !Number.isFinite(last))return null;
    const [lower,upper]=sweepBounds();
    // The audible head is an upper fence, not a blanket erase operation.
    // Guard the first and last tapped beat using the original local spacing.
    const before=rows.length>1?Math.min(.15,(sec(rows[1])-first)*.35):2e-5;
    const after=rows.length>1?Math.min(.15,(last-sec(rows[rows.length-2]))*.35):2e-5;
    const tempo=doc()?.tempo;
    const hasZeroPulse=Array.isArray(tempo?.pulse_points)
      ? Number(tempo.pulse_points[0]?.seconds)<1e-6
      : Number(tempo?.pulse_projection?.pulse_ordinal_quarter_origin ?? 0)<1e-6;
    const zeroFloor=hasZeroPulse && first>.08 ? .01 : 0;
    const start=Math.max(lower,zeroFloor,Math.min(first,sweep.start),first-before);
    const end=Math.min(upper,Math.max(last,sweep.end),last+after);
    if(end<=start+1e-6)return null;
    return {start,end,isPunch:activePunch()};
  }

  function buildOperation(overrideSnap = null) {
    if (phase==='review' && reviewDraft) {
      const snap=overrideSnap ?? prefs.snapToMidi;
      const {start,end,beats,include_start,include_end,...opts}=reviewDraft.op;
      const groups=regionProjection(draftRegions(),true);
      if(!groups.length)return null;
      const wire=groups.map(({start,end,includeStart,includeEnd,ids})=>({
        start,
        end,
        include_start:includeStart!==false,
        include_end:includeEnd!==false,
        beats:ids.map(i=>({seconds:beats[i].seconds,
          snap:Boolean(snap&&!reviewDraft.free.has(i)&&!reviewDraft.edited.has(i))}))
      }));
      return {...opts,snap_to_midi:Boolean(snap),meter_resolution:prefs.meterResolution,
        ...(wire.length===1?wire[0]:{regions:wire})};
    }
    const rawBeats = resolveEffectivePass();
    if (!rawBeats.length) return null;
    // Freeze the exact same guarded extent that the user saw fade. A loop
    // with no new taps falls back to the selected older populated pass.
    const covered=guardedCoverage(currentPass.length?currentPass:lastCompletedPass);
    if(!covered)return null;
    const {start,end}=covered;
    const useSnap = overrideSnap !== null ? overrideSnap : prefs.snapToMidi;
    const beats = rawBeats
      .filter(b => b.seconds >= start - 1e-6 && b.seconds <= end + 1e-6)
      .map(b => ({
        seconds: Math.max(start, Math.min(end, b.seconds)),
        snap: b.perTapFree ? false : Boolean(useSnap),
      }));
    if (!beats.length) return null;
    return {
      type: 'rhythm_intent',
      action: 'record_beats',
      start,
      end,
      include_start: true,
      include_end: true,
      beats,
      pulse_unit: 1.0,
      snap_to_midi: Boolean(useSnap),
      snap_tolerance_seconds: SNAP_TOLERANCE,
      chord_cluster_seconds: CLUSTER_WINDOW,
      meter_resolution: prefs.meterResolution,
    };
  }

  function finishRecording(reason) {
    if (phase !== 'recording') return;
    const heard=locateOnSegment(nowToQpcSec(performance.now())-delayFor(lastClock));
    const anchor=samples[samples.length-1];
    if(heard)extendSweep(heard.pass,heard.seconds,anchor?.loop,false);
    awaitingInitialPlaybackClock=false;
    earlyTaps=[];
    setMetronomeSilenced(false);
    refinePendingTaps();
    const beats = resolveEffectivePass();
    const minRequired = activePunch() ? 1 : 2;
    if (beats.length < minRequired) {
      currentPass = [];
      lastCompletedPass = [];
      ghostPass = [];
      phase = 'review';
      takeTaps = [];
    takeGroups = new Map();
    pendingTaps.clear();
    tapsDirty = false;
      notifyBackendActive(true);
      previewError = beats.length === 1 && !activePunch() ? '至少录 2 拍才能应用' : '这一遍没有录到拍点';
      syncUI();
      requestOverlayRepaint();
      requestRollDraw();
      return;
    }
    const original=buildOperation();
    const seededTempo=recGridTempo;
    resetRecordingPreview();
    reviewGridTempo=seededTempo;
    reviewSelected.clear();
    lastReviewHover=null;
    exitFade=null;
    reviewDraft=original ? {
      op:original,
      edited:new Set(),
      manualStatus:new Map(),
      deleted:new Set(),
      selectedRegion:null,
      regions:[{start:original.start,end:original.end,includeStart:true,includeEnd:true}],
      oldFade:new Map(),
      newFade:new Map(),
      lastPoints:new Map(),
      undoStack:[],
      redoStack:[],
      ghostStrips:[],
      cutAnims:new Map(),
      lastPreviewSig:'',
      free:new Set(beats.flatMap((beat,i)=>beat.perTapFree?[i]:[]))
    } : null;
    phase = 'review';
    ++reviewMaskVersion;
    window.AstraRhythmTimeline?.clearHover?.();
    notifyBackendActive(true);
    ghostPass = [];
    runPreview();
    syncUI();
    requestOverlayRepaint();
    requestRollDraw();
  }

  function reviewScopeCurrent() {
    const now=currentDocScope();
    return !!(sessionScope && now && now.project===sessionScope.project &&
      now.source_key===sessionScope.source_key && now.revision===sessionScope.revision &&
      (sessionScope.generation==null || now.generation===sessionScope.generation));
  }

  // A project edit outside the take (other beats, meters, notes) moves the
  // revision. The take is stored in audio seconds, so re-validate it against
  // the new revision instead of discarding the recording.
  function rebaseReview() {
    if (phase !== 'review' || committing) return false;
    const now = currentDocScope();
    if (!sessionScope || !now || now.project !== sessionScope.project || now.source_key !== sessionScope.source_key ||
        (sessionScope.generation != null && now.generation !== sessionScope.generation)) return false;
    sessionScope = now;
    if (reviewDraft) {
      if (performance.now() - projectHistoryPendingAt < 5000) projectHistoryPendingAt = -Infinity;
      else {
        reviewDraft.undoStack.push({ project: true });
        if (reviewDraft.undoStack.length > MAX_DRAFT_UNDO) reviewDraft.undoStack.shift();
        reviewDraft.redoStack.length = 0;
      }
      reviewDraft.lastPreviewSig = '';
      reviewDraft.projected = null;
    }
    previewData = null;
    applyQueuedSeq = null;
    ++reviewMaskVersion;
    runPreview();
    syncUI();
    requestRollDraw();
    requestOverlayRepaint();
    return true;
  }
  function ensureReviewScope() {
    if (reviewScopeCurrent() || rebaseReview()) return true;
    abort('项目已修改，录拍预览未应用');
    return false;
  }

  function runPreview() {
    if (phase!=='review' || !reviewScopeCurrent()) return;
    const d = doc();
    const op = buildOperation();
    const groups=reviewDraft?regionProjection(draftRegions(),true):[];
    if (!d || !op || typeof bridge?.previewProject !== 'function') {
      previewData = null;
      ++previewSeq;applyQueuedSeq=null;
      previewLoading = false;
      reviewGridTempo=null;reviewGridRegions=null;
      ++reviewMaskVersion;
      syncUI();requestRollDraw();
      return;
    }
    const seq = ++previewSeq;
    const baseRev = d.revision;
    previewLoading = true;
    previewError = '';
    syncUI();
    const onPreview = raw => {
      if (seq !== previewSeq || phase !== 'review') return;
      if (!reviewScopeCurrent()) { previewLoading = false; if (!rebaseReview()) abort('项目已修改，录拍预览未应用'); return; }
      previewLoading = false;
      let res;
      try { res = JSON.parse(raw); } catch { res = { ok: false, error: '预演响应无效' }; }
      if (!res?.ok) {
        previewData = null;
        reviewGridTempo = null;
        reviewGridRegions = null;
        ++reviewMaskVersion;
        previewError = res?.error || '拍点校验未通过';
        syncUI();
        requestRollDraw();
        return;
      }
      const points=new Map();
      const flat=res.impact?.snap_preview?.points||[];
      let n=0;
      groups.forEach((g,j)=>{
        const byRegion=res.impact?.regions?.[j]?.snap_preview?.points;
        g.ids.forEach((i,k)=>{
          const rawPt=byRegion?.[k]||flat[n];++n;
          if(rawPt){
            const manualSt=reviewDraft?.edited?.has(i)?reviewDraft?.manualStatus?.get(i):undefined;
            const pt=manualSt?{...rawPt,status:manualSt}:rawPt;
            points.set(i,pt);
            const beat=reviewDraft?.op?.beats?.[i];
            if(beat&&reviewDraft?.lastPoints){
              reviewDraft.lastPoints.set(i,{
                raw:beat.seconds,
                snap:Boolean(prefs.snapToMidi&&!reviewDraft.free.has(i)&&!reviewDraft.edited.has(i)),
                pt,
              });
            }
          }
        });
      });
      previewData = { op, result: res, points };
      const sig=opSignature(op);
      if(reviewDraft)reviewDraft.lastPreviewSig=sig;
      // ALWAYS preserve exact covered regions so unaffected bars outside strips never shift!
      reviewGridRegions=groups;
      reviewGridTempo=res.tempo?.anchors?.length>1?res.tempo:null;
      if (reviewGridTempo && typeof bridge?.setBeatRecording === 'function') {
        bridge.setBeatRecording(JSON.stringify({review_click_seq:seq}), () => {});
      }
      ++reviewMaskVersion;
      previewError = '';
      if(applyQueuedSeq===seq){
        applyQueuedSeq=null;
        commit();
        return;
      }
      syncUI();
      requestRollDraw();
    };
    try{bridge.previewProject(JSON.stringify({base_revision:baseRev,operation:op,review_click_seq:seq}),onPreview);}
    catch(error){onPreview(JSON.stringify({ok:false,error:error?.message||'无法校验录拍'}));}
  }

  function resetRecordingPreview() {
    if (recPreviewTimer && typeof clearTimeout === 'function') clearTimeout(recPreviewTimer);
    recPreviewTimer = 0;
    ++recPreviewSeq;
    recGridTempo = null; recGridSpan = null;
    recPreviewBusy = false; recPreviewDirty = false; recPreviewSentAt = -Infinity;
    ++recMaskVersion;
  }
  function scheduleRecordingPreview() {
    if (phase !== 'recording' || typeof bridge?.previewProject !== 'function') return;
    if (recPreviewBusy) { recPreviewDirty = true; return; }
    if (recPreviewTimer) return;
    const wait = Math.max(0, REC_PREVIEW_MS - (performance.now() - recPreviewSentAt));
    recPreviewTimer = setTimeout(() => { recPreviewTimer = 0; sendRecordingPreview(); }, wait);
  }
  function sendRecordingPreview() {
    if (phase !== 'recording') return;
    const d = doc(), scope = currentDocScope();
    if (!d || !sessionScope || !scope || scope.revision !== sessionScope.revision || scope.project !== sessionScope.project) return;
    refinePendingTaps();
    const op = buildOperation();
    if (!op) return;
    const seq = ++recPreviewSeq;
    recPreviewBusy = true; recPreviewDirty = false; recPreviewSentAt = performance.now();
    const done = raw => {
      if (seq !== recPreviewSeq) return;
      recPreviewBusy = false;
      if (phase !== 'recording') return;
      let res; try { res = JSON.parse(raw); } catch { res = null; }
      if (res?.ok && res.tempo?.anchors?.length > 1 && currentDocScope()?.revision === d.revision) {
        recGridTempo = res.tempo;
        recGridSpan = { start: op.start, end: op.end };
        ++recMaskVersion;
        requestRollDraw();
        requestOverlayRepaint();
      }
      if (recPreviewDirty) scheduleRecordingPreview();
    };
    try { bridge.previewProject(JSON.stringify({ base_revision: d.revision, operation: op }), done); }
    catch { done(JSON.stringify({ ok: false })); }
  }

  function commit() {
    if (phase !== 'review' || committing || reviewDrag) return;
    if(!ensureReviewScope())return;
    if(previewLoading){applyQueuedSeq=previewSeq;return;}
    if(!hasDraftWrite()){cleanup('idle');return;}
    if(!previewData)return;
    const op=previewData.op;
    committing = true;
    syncUI();
    const scope=sessionScope, preview=previewData;
    const done=(err,receipt)=>{
      if(!committing||phase!=='review'||sessionScope!==scope)return;
      const current=currentDocScope();
      const applied=receipt?.status==='committed'&&receipt.applied&&!receipt.error&&!receipt.cancelled&&
        receipt.project===scope.project&&current?.project===scope.project&&current.source_key===scope.source_key&&
        current.generation===scope.generation&&receipt.revision===current.revision;
      if(!applied&&!reviewScopeCurrent()){abort('项目已修改，录拍未应用');return;}
      if(!applied){
        committing=false;
        previewError=err||receipt?.error||receipt?.warning||'录拍未应用，请重试';
        syncUI();
        return;
      }
      const imp=preview.result?.impact||{};
      const ins=imp.inserted_point_count??(op.beats?.length??op.regions.reduce((n,r)=>n+r.beats.length,0));
      const rep=imp.replaced_point_count??0;
      cleanup('idle');
      ed()?.rhythmMessage?.(`已录入 +${ins} / 替换 ${rep}，Ctrl+Z 可撤销`);
    };
    try{
      window.AstraRhythmMotion?.capture?.();
      if(ed()?.rhythmCommit?.(op,done)!==true)done('暂时无法应用录拍，请重试');
    }catch(error){done(error?.message||'暂时无法应用录拍，请重试');}
  }

  function retry() {
    if (phase !== 'review' || committing) return;
    releaseReviewDrag();
    reviewSelected.clear();
    reviewGridTempo=null;reviewGridRegions=null;
    ++recMaskVersion;++reviewMaskVersion;
    reviewDraft=null;
    resetRecordingPreview();
    ++previewSeq;
    previewData = null;
    previewLoading=false;
    previewError = '';
    currentPass = [];
    lastCompletedPass = [];
    ghostPass = [];
    passNumber = 1;
    phase = 'armed';
    armTarget = null;
    takeTaps = [];
    takeGroups = new Map();
    pendingTaps.clear();
    tapsDirty = false;
    samples = [];
    notifyBackendActive(true);
    armedPlayRequested=false;
    retryTarget = activePunch() ? null : (startAnchorSec ?? currentFallbackPosition());
    syncUI();
    requestOverlayRepaint();
    requestRollDraw();
  }

  function cancel() {
    if(committing)return;
    cleanup('idle',{fadeOut:phase==='review'&&Boolean(reviewDraft)});
  }

  function abort(msg) {
    cleanup('idle');
    if (msg) ed()?.rhythmMessage?.(msg);
  }

  function captureExitFade() {
    if (reduceMotion?.matches || phase !== 'review' || !reviewDraft) return null;
    const now = performance.now();
    const groups = displayDraftGroups();
    const regions = draftRegions().map(r => ({ start: r.start, end: r.end, includeStart: r.includeStart !== false, includeEnd: r.includeEnd !== false }));
    const oldStart = new Map();
    for (const [sec, entry] of reviewDraft.oldFade.entries()) {
      const t = Math.min(1, Math.max(0, (now - entry.at) / FADE_MS));
      oldStart.set(sec, entry.from + (entry.to - entry.from) * easeOutCubic(t));
    }
    const beats = [];
    for (let i = 0; i < reviewDraft.op.beats.length; i++) {
      const r = draftRegionAt(reviewDraft.op.beats[i].seconds, groups);
      const pt = reviewPoint(i);
      const visible = !reviewDraft.deleted.has(i) && Boolean(r);
      const entry = reviewDraft.newFade.get(i);
      const alpha = entry
        ? entry.from + (entry.to - entry.from) * easeOutCubic(Math.min(1, Math.max(0, (now - entry.at) / FADE_MS)))
        : (visible ? 1 : 0);
      if (alpha > 1e-3) beats.push({ seconds: pt.seconds, status: pt.status, alpha });
    }
    if (!regions.length && !beats.length && !oldStart.size) return null;
    return { at: now, regions, groups, beats, oldStart };
  }

  function cleanup(nextPhase, { fadeOut = false } = {}) {
    exitFade = (nextPhase === 'idle' && fadeOut) ? captureExitFade() : null;
    lastReviewHover = null;
    const laneEl = $('beat-flags-canvas');
    if (laneEl?.style) laneEl.style.cursor = '';
    phase = nextPhase;
    setMetronomeSilenced(false);
    clearPreparation();
    captureStartedMs=-Infinity;
    applyQueuedSeq=null;
    resetSweep();
    releaseReviewDrag();
    reviewSelected.clear();
    reviewGridTempo=null;reviewGridRegions=null;
    ++recMaskVersion;++reviewMaskVersion;
    reviewDraft=null;
    resetRecordingPreview();
    ++previewSeq;
    if(clockTimer && typeof clearTimeout==='function') clearTimeout(clockTimer);
    clockTimer=0;
    ++clockWatchGeneration;
    clockProbeOutstanding=false;
    lastUsableClockAt=-Infinity;
    currentPass = [];
    lastCompletedPass = [];
    ghostPass = [];
    previewData = null;
    previewError = '';
    previewLoading = false;
    committing = false;
    preRollActive = false;
    bHeld = false;
    glitchDetected = false;
    sessionScope = null;
    sessionRange = null;
    armTarget = null;
    armedPlayRequested=false;
    awaitingInitialPlaybackClock=false;
    earlyTaps=[];
    retryTarget=null;
    lastClock = null;
    lastPacketQpc = -Infinity;
    samples = [];
    takeTaps = [];
    takeGroups = new Map();
    pendingTaps.clear();
    tapsDirty = false;
    notifyBackendActive(false);
    syncUI();
    requestOverlayRepaint();
    requestRollDraw();
  }

  function requestOverlayRepaint() {
    if (typeof requestBeatAimDraw === 'function') requestBeatAimDraw();
  }

  // ---- Visual-first Canvas Overlays ----

  function expandSpanForTaps(base, rows) {
    if (!rows?.length) return base;
    let minS = base ? base.start : Infinity, maxS = base ? base.end : -Infinity;
    for (const tap of rows) {
      const s = tap.snappedSec ?? tap.finalSec ?? tap.previewSec;
      const r = tap.finalSec ?? tap.previewSec ?? s;
      if (Number.isFinite(s)) { minS = Math.min(minS, s - 0.02); maxS = Math.max(maxS, s + 0.02); }
      if (Number.isFinite(r)) { minS = Math.min(minS, r - 0.02); maxS = Math.max(maxS, r + 0.02); }
    }
    const [lower, upper] = sweepBounds();
    const start = Math.max(lower, minS), end = Math.min(upper, maxS);
    if (!Number.isFinite(start) || !Number.isFinite(end) || end <= start + 1e-6) return base;
    return { start, end, isPunch: base ? base.isPunch : activePunch() };
  }

  function activeOverwriteSpan() {
    if (phase === 'idle') return null;
    if (phase === 'recording') {
      if (currentPass.length >= 2 || (activePunch() && currentPass.length >= 1)) {
        return expandSpanForTaps(guardedCoverage(currentPass), currentPass);
      }
      if (currentPass.length === 1) {
        const s0 = currentPass[0].snappedSec ?? currentPass[0].finalSec ?? currentPass[0].previewSec;
        const head = sweptNow?.end ?? s0;
        const [lower, upper] = sweepBounds();
        const a = Math.max(lower, Math.min(s0, head) - 0.04);
        const b = Math.min(upper, Math.max(s0 + 0.04, head));
        if (b > a + 1e-6) return { start: a, end: b, isPunch: activePunch() };
      }
      if (ghostPass.length >= 1) {
        return expandSpanForTaps(guardedCoverage(ghostPass), ghostPass);
      }
      return null;
    }
    return null;
  }

  function isDimmedFlag(seconds) {
    if(phase==='review'&&reviewDraft){
      return Boolean(draftRegionAt(seconds,displayDraftGroups()));
    }
    const span = activeOverwriteSpan();
    if (!span) return false;
    return seconds >= span.start - 1e-6 && seconds <= span.end + 1e-6;
  }

  function requestBeatLaneDraw() {
    if (ed()?.drawBeatFlagsOnly?.()) return;
    requestRollDraw();
  }

  function scheduleCoveredFade(){
    if(coveredFadeFrame)return;
    coveredFadeFrame=requestAnimationFrame(()=>{coveredFadeFrame=0;requestBeatLaneDraw();});
  }
  function reviewOpacity(rows,key,visible,fallback){
    if(reduceMotion?.matches)return visible?1:0;
    const to=visible?1:0,now=performance.now();
    let entry=rows.get(key);
    const value=r=>{
      const t=Math.min(1,Math.max(0,(now-r.at)/FADE_MS));
      return r.from+(r.to-r.from)*easeOutCubic(t);
    };
    if(!entry){entry={from:fallback,to,at:now};rows.set(key,entry);}
    else if(entry.to!==to){entry={from:value(entry),to,at:now};rows.set(key,entry);}
    const alpha=value(entry);
    if(Math.abs(alpha-to)>1e-3)scheduleCoveredFade();
    return alpha;
  }
  function flagOpacity(seconds) {
    if(phase==='idle'&&exitFade){
      if(reduceMotion?.matches){exitFade=null;return 1;}
      const t=Math.min(1,Math.max(0,(performance.now()-exitFade.at)/EXIT_FADE_MS));
      if(t>=1)return 1;
      const startAlpha=exitFade.oldStart.has(seconds)
        ? exitFade.oldStart.get(seconds)
        : (draftRegionAt(seconds,exitFade.groups)?0:1);
      if(startAlpha>=1-1e-3)return 1;
      scheduleCoveredFade();
      return startAlpha+(1-startAlpha)*easeOutCubic(t);
    }
    const covered=isDimmedFlag(seconds);
    if(phase==='review'&&reviewDraft){
      const began=coveredAt.get(seconds);
      let fallback=1;
      if(covered){
        if(began!==undefined){
          const e=typeof began==='number'?{from:1,to:0,at:began}:began;
          const age=Math.min(1,Math.max(0,(performance.now()-e.at)/FADE_MS));
          fallback=e.from+(e.to-e.from)*easeOutCubic(age);
        }else if(!reviewDraft.regionEdited){
          fallback=0;
        }
      }
      return reviewOpacity(reviewDraft.oldFade,seconds,!covered,fallback);
    }
    if(reduceMotion?.matches){
      if(!covered)coveredAt.delete(seconds);
      return covered?0:1;
    }
    const now=performance.now();
    let entry=coveredAt.get(seconds);
    if(typeof entry==='number')entry={from:1,to:0,at:entry};
    const to=covered?0:1;
    const val=e=>e.from+(e.to-e.from)*easeOutCubic(Math.min(1,Math.max(0,(now-e.at)/FADE_MS)));
    if(!entry){
      if(!covered)return 1;
      entry={from:1,to:0,at:now};
      coveredAt.set(seconds,entry);
    }else if(entry.to!==to){
      entry={from:val(entry),to,at:now};
      coveredAt.set(seconds,entry);
    }
    const alpha=val(entry);
    if(Math.abs(alpha-to)>1e-3)scheduleCoveredFade();
    else if(to===1)coveredAt.delete(seconds);
    return alpha;
  }

  // Called by project_editor.js inside draw() on #ruler-canvas:
  // Keeps Loop bar pure Time Violet (y=1..14) and adds a 3px Crimson underline at y=13..16 for L/R Punch!
  function drawRuler(ruler, offset, width) {
    if (!ruler || !activePunch()) return;
    if (phase === 'idle' && !document.querySelector('.beat-rec-menu[open]')) return;
    const r = takeRange();
    const x1 = midiX(r.start) - offset, x2 = midiX(r.end) - offset;
    if (x2 < 0 || x1 > width) return;
    ruler.save();
    ruler.fillStyle = '#c66c70';
    const swept=activeOverwriteSpan();
    if(swept) {
      const a=Math.max(0,midiX(swept.start)-offset),b=Math.min(width,midiX(swept.end)-offset);
      if(b>a)ruler.fillRect(a,13,b-a,3);
    }
    // Tiny fixed crimson punch ticks at L and R; the stripe advances beneath purple.
    if (x1 >= -2 && x1 <= width + 2) ruler.fillRect(x1 - 1, 13, 3, 7);
    if (x2 >= -2 && x2 <= width + 2) ruler.fillRect(x2 - 2, 13, 3, 7);
    ruler.restore();
  }

  function flagInk(status, inReview = false) {
    if (inReview) return status === 'midi_onset' ? '#436bc4' : '#d97706';
    return status === 'ambiguous_onset' ? '#d97706' : status === 'midi_onset' ? '#436bc4' : '#c66c70';
  }
  function drawDraftFlag(ctx,x,ink,held=false,dy=0) {
    ctx.save();
    if(dy)ctx.translate(0,dy);
    ctx.fillStyle=ink;
    ctx.fillRect(x,7,1,15);
    ctx.fillRect(x+1,5,held?9:7,7);
    if(held){ctx.strokeStyle='#fff';ctx.lineWidth=1;ctx.strokeRect(x+.5,4.5,10,8);}
    ctx.restore();
  }
  function drawSnapGuide(ctx,px,raw,offset) {
    const rx=Math.round(midiX(raw)-offset);
    if(!Number.isFinite(rx)||Math.abs(rx-px)<1)return;
    ctx.fillStyle='rgba(67,107,196,.35)';
    ctx.fillRect(Math.min(rx,px),14,Math.abs(px-rx),1);
    ctx.fillRect(rx,10,1,9);
  }
  function roundedPillPath(ctx, x, y, w, h, rL, rR) {
    const rw = Math.max(0, w), rh = Math.max(0, h);
    const rl = Math.min(rL, rw * 0.5, rh * 0.5);
    const rr = Math.min(rR, rw * 0.5, rh * 0.5);
    ctx.beginPath();
    ctx.moveTo(x + rl, y);
    ctx.lineTo(x + rw - rr, y);
    if (rr > 0) ctx.quadraticCurveTo(x + rw, y, x + rw, y + rr);
    ctx.lineTo(x + rw, y + rh - rr);
    if (rr > 0) ctx.quadraticCurveTo(x + rw, y + rh, x + rw - rr, y + rh);
    ctx.lineTo(x + rl, y + rh);
    if (rl > 0) ctx.quadraticCurveTo(x, y + rh, x, y + rh - rl);
    ctx.lineTo(x, y + rl);
    if (rl > 0) ctx.quadraticCurveTo(x, y, x + rl, y);
    ctx.closePath();
  }
  function drawGripRibs(ctx, cx, yTop, h, doubleRib, alpha, color = '255,253,253') {
    ctx.fillStyle = `rgba(${color},${alpha})`;
    if (doubleRib) {
      ctx.fillRect(Math.round(cx) - 1, yTop + 1.5, 1, Math.max(1.5, h - 3));
      ctx.fillRect(Math.round(cx) + 1, yTop + 1.5, 1, Math.max(1.5, h - 3));
    } else {
      ctx.fillRect(Math.round(cx), yTop + 1.5, 1, Math.max(1.5, h - 3));
    }
  }
  function drawPullCaret(ctx, cx, cy, dir, ink = '#a82b43') {
    ctx.save();
    ctx.strokeStyle = ink;
    ctx.lineWidth = 1.45;
    ctx.lineCap = 'round';
    ctx.lineJoin = 'round';
    ctx.beginPath();
    if (dir < 0) {
      ctx.moveTo(cx + 1.4, cy - 2.3); ctx.lineTo(cx - 1.4, cy); ctx.lineTo(cx + 1.4, cy + 2.3);
    } else {
      ctx.moveTo(cx - 1.4, cy - 2.3); ctx.lineTo(cx + 1.4, cy); ctx.lineTo(cx - 1.4, cy + 2.3);
    }
    ctx.stroke();
    ctx.restore();
  }
  function drawCutPreviewLine(ctx, cx, height, withNotch = true) {
    const x = Math.round(cx);
    ctx.save();
    ctx.fillStyle = 'rgba(255,253,249,.88)';
    ctx.fillRect(x - 1, 0, 3, REGION_STRIP_TOP + REGION_STRIP_HEIGHT + 2);
    ctx.fillStyle = 'rgba(255,253,249,.45)';
    ctx.fillRect(x - 1, REGION_STRIP_TOP + REGION_STRIP_HEIGHT + 2, 3, height - REGION_STRIP_TOP - REGION_STRIP_HEIGHT - 2);
    ctx.fillStyle = '#b33a4b';
    ctx.fillRect(x, 0, 1, height);
    if (withNotch) {
      ctx.beginPath();
      ctx.moveTo(x - 3.5, 0); ctx.lineTo(x + 4.5, 0); ctx.lineTo(x + 0.5, 4.8); ctx.closePath();
      ctx.fillStyle = '#b33a4b'; ctx.fill();
      ctx.fillStyle = '#fffdf9'; ctx.fillRect(x - 1.5, 0, 4, 1);
    }
    ctx.restore();
  }

  // Called by project_editor.js inside drawTimeline() on #beat-flags-canvas
  function drawBeatLane(ctx, width, offset) {
    if (!ctx) return;
    if (phase === 'idle') {
      if (!exitFade) return;
      const now = performance.now();
      const t = Math.min(1, Math.max(0, (now - exitFade.at) / EXIT_FADE_MS));
      if (t >= 1 || reduceMotion?.matches) { exitFade = null; return; }
      const fade = 1 - easeOutCubic(t);
      const lift = -2 * (1 - fade);
      ctx.save();
      for (const r of exitFade.regions) {
        const x = midiX(r.start) - offset, end = midiX(r.end) - offset;
        if (end < 0 || x > width) continue;
        const a = Math.max(0, x), b = Math.min(width, end), w = b - a;
        if (w <= 0) continue;
        ctx.globalAlpha = fade;
        ctx.fillStyle = 'rgba(198,108,112,.10)';
        ctx.fillRect(a, REGION_STRIP_TOP + REGION_STRIP_HEIGHT, w, 26 - REGION_STRIP_TOP - REGION_STRIP_HEIGHT);
        roundedPillPath(ctx, a, Math.max(0, REGION_STRIP_TOP + lift), w, REGION_STRIP_HEIGHT, 3, 3);
        ctx.fillStyle = '#c66c70';
        ctx.fill();
      }
      for (const bt of exitFade.beats) {
        const px = Math.round(midiX(bt.seconds) - offset);
        if (px < -8 || px > width + 8) continue;
        ctx.globalAlpha = bt.alpha * fade;
        drawDraftFlag(ctx, px, flagInk(bt.status, true), false, lift);
      }
      ctx.restore();
      scheduleCoveredFade();
      return;
    }
    const span = activeOverwriteSpan();
    const now = performance.now();
    ctx.save();
    if (span && span.end > span.start + 1e-6) {
      const x1 = midiX(span.start) - offset, x2 = midiX(span.end) - offset;
      const cx1 = Math.max(0, x1), cx2 = Math.min(width, x2), cw = cx2 - cx1;
      if (cw > 0) {
        const wash = ctx.createLinearGradient(0, REGION_STRIP_TOP + REGION_STRIP_HEIGHT, 0, 26);
        wash.addColorStop(0, phase === 'review' ? 'rgba(67,107,196,.12)' : 'rgba(198,108,112,.14)');
        wash.addColorStop(1, phase === 'review' ? 'rgba(67,107,196,.03)' : 'rgba(198,108,112,.03)');
        ctx.fillStyle = wash;
        ctx.fillRect(cx1, REGION_STRIP_TOP + REGION_STRIP_HEIGHT, cw, 26 - REGION_STRIP_TOP - REGION_STRIP_HEIGHT);
        // Sculpted matte-satin floating capsule ribbon during recording sweep (zero shadowBlur churn)
        const recY = REGION_STRIP_TOP + 0.3, recH = REGION_STRIP_HEIGHT - 0.6, recR = recH * 0.5;
        roundedPillPath(ctx, cx1, recY + 1, cw, recH, recR, recR);
        ctx.fillStyle = 'rgba(156,42,64,.14)';
        ctx.fill();
        roundedPillPath(ctx, cx1, recY, cw, recH, recR, recR);
        if (phase === 'recording' && cw > 8) {
          const grad = ctx.createLinearGradient(Math.max(cx1, cx2 - 30), 0, cx2, 0);
          grad.addColorStop(0, '#c8606f');
          grad.addColorStop(0.76, '#da7483');
          grad.addColorStop(1, '#e88291');
          ctx.fillStyle = grad;
          ctx.fill();
          // Whisper-fine top specular sheen
          ctx.fillStyle = 'rgba(255,250,251,.26)';
          ctx.fillRect(cx1 + 3, recY + 0.75, Math.max(0, cw - 6), 0.7);
          // Crisp leading playhead nib
          ctx.fillStyle = '#fffdf9';
          ctx.fillRect(cx2 - 2.5, recY + 1.3, 1.5, recH - 2.6);
          ctx.fillStyle = 'rgba(198,92,108,.30)';
          ctx.fillRect(cx2 - 1, recY + recH + 1, 1, 26 - recY - recH - 1);
        } else {
          ctx.fillStyle = phase === 'review' ? '#436bc4' : '#c66c70';
          ctx.fill();
        }
      }
      // Crisp IN/OUT brackets when L/R Punch is active
      if (span.isPunch) {
        ctx.fillStyle = '#c66c70';
        if (x1 >= -2 && x1 <= width + 2) { ctx.fillRect(x1, 0, 2, 26); ctx.fillRect(x1, 0, 6, 2); ctx.fillRect(x1, 24, 6, 2); }
        if (x2 >= -2 && x2 <= width + 2) { ctx.fillRect(x2 - 2, 0, 2, 26); ctx.fillRect(x2 - 6, 0, 6, 2); ctx.fillRect(x2 - 6, 24, 6, 2); }
      }
    }
    if(phase==='review'&&reviewDraft){
      // Soft dissolve of recently deleted strip segments
      if(reviewDraft.ghostStrips?.length){
        const keep=[];
        for(const gs of reviewDraft.ghostStrips){
          const t=Math.min(1,Math.max(0,(now-gs.at)/GHOST_STRIP_MS));
          if(t>=1)continue;
          keep.push(gs);
          const fade=1-easeOutCubic(t);
          const gx1=Math.max(0,midiX(gs.start)-offset),gx2=Math.min(width,midiX(gs.end)-offset),gw=gx2-gx1;
          if(gw>0){
            ctx.save();
            ctx.globalAlpha=fade;
            ctx.fillStyle='rgba(198,108,112,.10)';
            ctx.fillRect(gx1,REGION_STRIP_TOP+REGION_STRIP_HEIGHT,gw,26-REGION_STRIP_TOP-REGION_STRIP_HEIGHT);
            roundedPillPath(ctx,gx1,REGION_STRIP_TOP-(1-fade)*1.5,gw,Math.max(2,REGION_STRIP_HEIGHT*fade),3,3);
            ctx.fillStyle='#c86a73';
            ctx.fill();
            ctx.restore();
          }
        }
        reviewDraft.ghostStrips=keep;
        if(keep.length)scheduleCoveredFade();
      }
      const pieces=displayDraftRegions(),paintGroups=displayDraftGroups();
      let animNeedsFrame=false;
      const stripBottom=REGION_STRIP_TOP+REGION_STRIP_HEIGHT;
      for(let idx=0;idx<pieces.length;idx++){
        const r=pieces[idx];
        const x=midiX(r.start)-offset,end=midiX(r.end)-offset;
        if(end<-4||x>width+4)continue;
        const hasLeftJoint=idx>0&&pieces[idx-1].end===r.start;
        const hasRightJoint=idx+1<pieces.length&&pieces[idx+1].start===r.end;
        const leftSpring=hasLeftJoint?cutSpringState(r.start,now):null;
        const rightSpring=hasRightJoint?cutSpringState(r.end,now):null;
        if(leftSpring?.active||rightSpring?.active)animNeedsFrame=true;
        const insetL=hasLeftJoint?leftSpring.halfGap:0;
        const insetR=hasRightJoint?rightSpring.halfGap:0;
        const rawL=x+insetL,rawR=end-insetR;
        const a=Math.max(0,rawL),b=Math.min(width,Math.max(rawL+2,rawR)),w=b-a;
        const owner=r.owner??r;
        const selected=owner===reviewDraft.selectedRegion;
        const hovIsThis=reviewDraft.hover?.r===owner&&reviewDraft.hover?.cut==null;
        const hovEdge=hovIsThis?reviewDraft.hover.edge:null;
        const dragEdge=reviewDrag?.kind==='region-edge'&&reviewDrag.region===owner?reviewDrag.edge:null;
        const hovered=!selected&&hovIsThis;
        const hasBeats=Boolean(draftRegionAt((r.start+r.end)/2,paintGroups));

        // 1. Whisper-soft studio mist veil across the beat lane body
        const wash=ctx.createLinearGradient(0,stripBottom,0,26);
        if(selected){
          wash.addColorStop(0,hasBeats?'rgba(198,64,88,.19)':'rgba(198,64,88,.09)');
          wash.addColorStop(1,hasBeats?'rgba(198,64,88,.035)':'rgba(198,64,88,.015)');
        }else if(hovered){
          wash.addColorStop(0,hasBeats?'rgba(202,94,110,.14)':'rgba(202,94,110,.07)');
          wash.addColorStop(1,hasBeats?'rgba(202,94,110,.025)':'rgba(202,94,110,.01)');
        }else{
          wash.addColorStop(0,hasBeats?'rgba(204,104,118,.11)':'rgba(204,104,118,.04)');
          wash.addColorStop(1,hasBeats?'rgba(204,104,118,.015)':'rgba(204,104,118,.005)');
        }
        ctx.fillStyle=wash;
        ctx.fillRect(a,stripBottom,w,26-stripBottom);

        // 2. Matte-satin sculpted floating porcelain-rose capsule ribbon
        const pillH=selected||hovered?REGION_STRIP_HEIGHT:REGION_STRIP_HEIGHT-0.6;
        const pillY=selected||hovered?REGION_STRIP_TOP:REGION_STRIP_TOP+0.3;
        const pillR=pillH*0.5;
        if(hasBeats){
          // Delicate tinted ambient elevation shadow (layered pill, zero shadowBlur texture allocation)
          if(selected){
            roundedPillPath(ctx,a-0.75,pillY+0.5,w+1.5,pillH+1.6,pillR+0.8,pillR+0.8);
            ctx.fillStyle='rgba(168,36,60,.16)';
            ctx.fill();
          }
          roundedPillPath(ctx,a,pillY+1,w,pillH,pillR,pillR);
          ctx.fillStyle=selected?'rgba(148,28,50,.22)':'rgba(156,46,66,.13)';
          ctx.fill();
          const pillGrad=ctx.createLinearGradient(0,pillY,0,pillY+pillH);
          if(selected){
            pillGrad.addColorStop(0,'#cf4862');
            pillGrad.addColorStop(1,'#ab2b45');
          }else if(hovered){
            pillGrad.addColorStop(0,'#df7b8c');
            pillGrad.addColorStop(1,'#c6586b');
          }else{
            pillGrad.addColorStop(0,'#db7c8b');
            pillGrad.addColorStop(1,'#c25a6a');
          }
          roundedPillPath(ctx,a,pillY,w,pillH,pillR,pillR);
          ctx.fillStyle=pillGrad;
          ctx.fill();
          // Refined subtle rim + whisper-fine 0.5px porcelain specular edge (never chunky!)
          roundedPillPath(ctx,a+0.25,pillY+0.25,Math.max(0.5,w-0.5),Math.max(0.5,pillH-0.5),pillR,pillR);
          ctx.strokeStyle=selected?'rgba(130,22,42,.38)':hovered?'rgba(162,48,68,.28)':'rgba(154,50,68,.20)';
          ctx.lineWidth=0.6;
          ctx.stroke();
          if(w>10){
            ctx.fillStyle=selected?'rgba(255,250,251,.34)':hovered?'rgba(255,250,251,.26)':'rgba(255,250,251,.20)';
            ctx.fillRect(a+3,pillY+0.75,Math.max(0,w-6),0.7);
          }
          // Fresh-cut outward specular sheen gliding along both freshly cut capsules!
          if(leftSpring?.active&&w>10){
            const sp=Math.min(1,Math.max(0,(leftSpring.p-0.05)/0.72));
            const glowW=Math.min(w-3,22);
            const sx=a+1+sp*Math.min(26,Math.max(0,w-glowW-1));
            const alpha=(1-sp)*0.78;
            if(alpha>0.02){
              const sg=ctx.createLinearGradient(sx,0,sx+glowW,0);
              sg.addColorStop(0,`rgba(255,253,254,${alpha.toFixed(3)})`);
              sg.addColorStop(0.65,`rgba(255,244,247,${(alpha*0.45).toFixed(3)})`);
              sg.addColorStop(1,'rgba(255,253,254,0)');
              ctx.save();
              roundedPillPath(ctx,a,pillY,w,pillH,pillR,pillR);
              ctx.clip();
              ctx.fillStyle=sg;
              ctx.fillRect(sx,pillY,glowW,pillH);
              ctx.restore();
            }
          }
          if(rightSpring?.active&&w>10){
            const sp=Math.min(1,Math.max(0,(rightSpring.p-0.05)/0.72));
            const glowW=Math.min(w-3,22);
            const sx=b-1-glowW-sp*Math.min(26,Math.max(0,w-glowW-1));
            const alpha=(1-sp)*0.78;
            if(alpha>0.02){
              const sg=ctx.createLinearGradient(sx,0,sx+glowW,0);
              sg.addColorStop(0,'rgba(255,253,254,0)');
              sg.addColorStop(0.35,`rgba(255,244,247,${(alpha*0.45).toFixed(3)})`);
              sg.addColorStop(1,`rgba(255,253,254,${alpha.toFixed(3)})`);
              ctx.save();
              roundedPillPath(ctx,a,pillY,w,pillH,pillR,pillR);
              ctx.clip();
              ctx.fillStyle=sg;
              ctx.fillRect(sx,pillY,glowW,pillH);
              ctx.restore();
            }
          }
        }else{
          // Empty segment: refined frosted-glass dashed capsule
          ctx.save();
          roundedPillPath(ctx,a+0.5,pillY+0.5,Math.max(1,w-1),Math.max(1,pillH-1),2.5,2.5);
          ctx.fillStyle=selected?'rgba(198,64,88,.18)':'rgba(202,98,112,.10)';
          ctx.fill();
          ctx.strokeStyle=selected?'rgba(170,42,64,.70)':'rgba(182,70,86,.48)';
          ctx.lineWidth=1;
          ctx.setLineDash([3,2]);
          ctx.stroke();
          ctx.restore();
        }

        // 3. Delicate architectural edge hairlines (refined, never heavy wireframe box)
        if(selected){
          ctx.fillStyle='rgba(182,46,72,.26)';
          ctx.fillRect(a,25,w,1);
          ctx.fillStyle='rgba(182,46,72,.44)';
          if(rawL>=0&&rawL<=width)ctx.fillRect(Math.round(rawL),stripBottom+1,1,26-stripBottom-1);
          if(rawR>=0&&rawR<=width)ctx.fillRect(Math.round(rawR)-1,stripBottom+1,1,26-stripBottom-1);
        }else{
          ctx.fillStyle=hovered?'rgba(182,56,78,.32)':'rgba(170,68,84,.20)';
          if(rawL>=0&&rawL<=width)ctx.fillRect(Math.round(rawL),stripBottom+1,1,26-stripBottom-1);
          if(rawR>=0&&rawR<=width)ctx.fillRect(Math.round(rawR)-1,stripBottom+1,1,26-stripBottom-1);
        }

        // 4. Tactile shoulder grips: subtle etched ribs at rest, luminous ivory-porcelain lifted pull-tab on hover/drag!
        if(w>=14){
          const leftActive=hovEdge==='start'||dragEdge==='start';
          const rightActive=hovEdge==='end'||dragEdge==='end';
          if(leftActive&&rawL>=0&&rawL<=width){
            const capW=Math.min(10,w*0.45),capH=8,capY=REGION_STRIP_TOP-1;
            roundedPillPath(ctx,a-0.5,capY+1,capW,capH,4,2.5);
            ctx.fillStyle='rgba(128,24,44,.22)';
            ctx.fill();
            roundedPillPath(ctx,a-0.5,capY,capW,capH,4,2.5);
            ctx.fillStyle='#fffdf9';
            ctx.fill();
            ctx.strokeStyle='#b4344c';
            ctx.lineWidth=1;
            ctx.stroke();
            if(hasLeftJoint)drawPullCaret(ctx,a-0.5+capW*0.5,capY+capH*0.5,1,'#a82b43');
            else drawGripRibs(ctx,a+4,capY+0.5,capH-1,true,0.9,'168,43,67');
            ctx.fillStyle='rgba(180,46,72,.45)';
            ctx.fillRect(Math.round(rawL),stripBottom+1,1,26-stripBottom-1);
          }else if(rawL>=0&&rawL<=width&&hasBeats){
            drawGripRibs(ctx,a+3.5,pillY,pillH,hasLeftJoint||selected||hovered,hasLeftJoint?0.72:selected?0.74:hovered?0.58:0.36);
          }
          if(rightActive&&rawR>=0&&rawR<=width){
            const capW=Math.min(10,w*0.45),capH=8,capY=REGION_STRIP_TOP-1;
            roundedPillPath(ctx,b-capW+0.5,capY+1,capW,capH,2.5,4);
            ctx.fillStyle='rgba(128,24,44,.22)';
            ctx.fill();
            roundedPillPath(ctx,b-capW+0.5,capY,capW,capH,2.5,4);
            ctx.fillStyle='#fffdf9';
            ctx.fill();
            ctx.strokeStyle='#b4344c';
            ctx.lineWidth=1;
            ctx.stroke();
            if(hasRightJoint)drawPullCaret(ctx,b-capW+0.5+capW*0.5,capY+capH*0.5,-1,'#a82b43');
            else drawGripRibs(ctx,b-4,capY+0.5,capH-1,true,0.9,'168,43,67');
            ctx.fillStyle='rgba(180,46,72,.45)';
            ctx.fillRect(Math.round(rawR)-1,stripBottom+1,1,26-stripBottom-1);
          }else if(rawR>=0&&rawR<=width&&hasBeats){
            drawGripRibs(ctx,b-3.5,pillY,pillH,hasRightJoint||selected||hovered,hasRightJoint?0.72:selected?0.74:hovered?0.58:0.36);
          }
        }
      }
      // 5. Crisp internal split seam guides + high-craft laser-porcelain slice confirmation!
      for(let idx=0;idx+1<pieces.length;idx++){
        if(pieces[idx].end!==pieces[idx+1].start)continue;
        const cutSec=pieces[idx].end;
        const cx=Math.round(midiX(cutSec)-offset);
        if(cx<-8||cx>width+8)continue;
        const sp=cutSpringState(cutSec,now);
        // Subtle delicate dotted center guide inside the 5px breathing gap
        ctx.save();
        ctx.strokeStyle='rgba(176,54,74,.36)';
        ctx.lineWidth=1;
        ctx.setLineDash([1.5,2.5]);
        ctx.beginPath();
        ctx.moveTo(cx+0.5,stripBottom+2);
        ctx.lineTo(cx+0.5,25);
        ctx.stroke();
        ctx.restore();
        // High-craft scissor-slice confirmation: luminous incision beam + expanding porcelain ring halo
        if(sp.active&&sp.p<0.62){
          const fp=sp.p/0.62;
          const fade=1-easeOutCubic(fp);
          const beamH=Math.min(26,26*Math.min(1,fp/0.24));
          const auraW=1.2+fp*6.5;
          ctx.save();
          const auraGrad=ctx.createLinearGradient(cx-auraW,0,cx+auraW+1,0);
          auraGrad.addColorStop(0,'rgba(220,76,102,0)');
          auraGrad.addColorStop(0.5,`rgba(220,76,102,${(0.38*fade).toFixed(3)})`);
          auraGrad.addColorStop(1,'rgba(220,76,102,0)');
          ctx.fillStyle=auraGrad;
          ctx.fillRect(cx-auraW,0,auraW*2+1,beamH);
          ctx.fillStyle=`rgba(255,253,254,${(0.96*fade).toFixed(3)})`;
          ctx.fillRect(cx,0,1,beamH);
          // Delicate expanding ring halo centered right on the cut notch
          const ringR=2+easeOutCubic(fp)*8.5;
          ctx.strokeStyle=`rgba(206,62,88,${(0.68*fade).toFixed(3)})`;
          ctx.lineWidth=1.15;
          ctx.beginPath();
          ctx.arc(cx+0.5,REGION_STRIP_TOP+REGION_STRIP_HEIGHT*0.5,ringR,0,Math.PI*2);
          ctx.stroke();
          ctx.restore();
          animNeedsFrame=true;
        }
      }
      if(animNeedsFrame)scheduleCoveredFade();
      // Active region-edge drag hairline (the floating time pill renders cleanly in paintOverlay on the roll!)
      if(reviewDrag?.kind==='region-edge'&&reviewDrag.moved&&Number.isFinite(reviewDrag.to)){
        const ex=midiX(reviewDrag.to)-offset;
        drawCutPreviewLine(ctx,ex,26,false);
      }
      // Alt-cut live aim guide spanning full 26px lane height (never during active drag)
      const cut=!reviewDrag?reviewDraft.hover?.cut:null;
      if(Number.isFinite(cut)){
        drawCutPreviewLine(ctx,cut-offset,26,true);
      }
    }else if(span){
      const a=Math.max(0,midiX(span.start)-offset),b=Math.min(width,midiX(span.end)-offset);
      if(b>a){roundedPillPath(ctx,a,REGION_STRIP_TOP,b-a,REGION_STRIP_HEIGHT,3,3);ctx.fillStyle='#c66c70';ctx.fill();}
    }
    // Pre-roll dashed subtle cue before L
    if (phase === 'recording' && preRollActive && activePunch()) {
      const playX = midiX(currentFallbackPosition()) - offset, lX = midiX(preRollTargetL) - offset;
      if (lX > playX) {
        ctx.strokeStyle = 'rgba(198,108,112,.45)';
        ctx.setLineDash([4, 3]);
        ctx.beginPath(); ctx.moveTo(Math.max(0, playX), 13); ctx.lineTo(Math.min(width, lX), 13); ctx.stroke();
        ctx.setLineDash([]);
      }
    }
    // Cycle Keep Last ghost pins from previous pass
    if (phase === 'recording' && ghostPass.length) {
      ctx.globalAlpha = 0.35;
      ctx.fillStyle = '#7264a7';
      for (const t of ghostPass) {
        const px = Math.round(midiX(t.snappedSec ?? t.finalSec ?? t.previewSec) - offset);
        if (px < -4 || px > width + 4) continue;
        drawDraftFlag(ctx,px,'#7264a7');
      }
      ctx.globalAlpha = 1;
    }
    // Draw live pins in the permanent lane with silky tap bloom
    if(phase==='recording') {
      let animatingTap=false;
      for(const tap of currentPass) {
        const raw=tap.finalSec??tap.previewSec, sec=tap.snappedSec??raw;
        const px=Math.round(midiX(sec)-offset);
        if(px<-8||px>width+8)continue;
        const ink=flagInk(tap.status);
        const age=reduceMotion?.matches?1:Math.min(1,Math.max(0,(now-tap.rippleAt)/TAP_BLOOM_MS));
        const ease=easeOutCubic(age);
        if(age<1)animatingTap=true;
        ctx.globalAlpha=0.25+0.75*ease;
        if(Math.abs(raw-sec)>.002)drawSnapGuide(ctx,px,raw,offset);
        drawDraftFlag(ctx,px,ink,false,reduceMotion?.matches?0:(1-ease)*-2.2);
        if(age<1){
          const ring=3+ease*6;
          ctx.save();
          ctx.globalAlpha=(1-ease)*0.55;
          ctx.strokeStyle=ink;
          ctx.lineWidth=1.2;
          ctx.beginPath();
          ctx.arc(px+0.5,11,ring,0,Math.PI*2);
          ctx.stroke();
          ctx.restore();
        }
      }
      ctx.globalAlpha=1;
      if(animatingTap)scheduleCoveredFade();
    }
    // Authoritative Review preview beats with bidirectional cross-fade
    if (phase==='review' && reviewDraft) {
      if(reviewDrag?.kind==='box' && reviewDrag.moved){
        const a=Math.min(reviewDrag.pressX,reviewDrag.currentX)-offset,b=Math.max(reviewDrag.pressX,reviewDrag.currentX)-offset;
        ctx.fillStyle='rgba(117,96,175,.12)';ctx.fillRect(a,0,b-a,26);
        ctx.strokeStyle='#7560af';ctx.strokeRect(a+.5,.5,b-a,25);
      }
      const paintRegions=displayDraftGroups();
      if(reviewDrag?.kind==='insert'&&Number.isFinite(reviewDrag.to)){
        const x=Math.round(midiX(reviewDrag.to)-offset);
        drawSnapGuide(ctx,x,reviewDrag.raw,offset);
        drawDraftFlag(ctx,x,flagInk(reviewDrag.aim?.onset?'midi_onset':'no_nearby_onset',true));
      }
      for (let i=0;i<reviewDraft.op.beats.length;i++) {
        const r=draftRegionAt(reviewDraft.op.beats[i].seconds,paintRegions);
        const pt=reviewPoint(i),held=reviewSelected.has(i);
        const px=Math.round(midiX(pt.seconds)-offset);
        if (px < -8 || px > width+8) continue;
        const visible=!reviewDraft.deleted.has(i)&&!reviewDrag?.erase?.has(i)&&Boolean(r);
        const alpha=reviewOpacity(reviewDraft.newFade,i,visible,visible?1:0);
        if(alpha<=1e-3)continue;
        ctx.globalAlpha=alpha;
        if(Number.isFinite(pt.raw_seconds) && Math.abs(pt.raw_seconds-pt.seconds)>.002)
          drawSnapGuide(ctx,px,pt.raw_seconds,offset);
        const dy=reduceMotion?.matches||alpha>=1?0:(1-alpha)*-2.2;
        drawDraftFlag(ctx,px,flagInk(pt.status,true),held,dy);
      }
      ctx.globalAlpha=1;
    }
    ctx.restore();
  }

  function activeBeatAim() {
    if (phase !== 'review' || !reviewDraft || !reviewDrag) return null;
    const g = reviewDrag;
    if (g.kind === 'move' && (g.moved || g.lower === g.upper)) {
      const view = g._aimView || (g._aimView = { fromRecorder: true, kind: 'move' });
      view.target = g.lane;
      view.seconds = g.from;
      view.raw = Number.isFinite(g.raw) ? g.raw : g.from;
      view.to = Number.isFinite(g.to) ? g.to : g.from;
      view.aim = g.aim || null;
      view.blocked = g.lower === g.upper;
      view.limited = Boolean(g.limited);
      view.points = g.points || [g.from];
      return view;
    }
    if (g.kind === 'insert' && Number.isFinite(g.to)) {
      const view = g._aimView || (g._aimView = { fromRecorder: true, kind: 'insert' });
      view.target = g.lane;
      view.seconds = g.to;
      view.raw = Number.isFinite(g.raw) ? g.raw : g.to;
      view.to = g.to;
      view.aim = g.aim || null;
      view.blocked = false;
      view.limited = Boolean(g.limited);
      view.points = null;
      return view;
    }
    return null;
  }

  function wantsOverlay() {
    if (activeBeatAim()) return true;
    if (phase === 'recording' && currentPass.length > 0) return true;
    if (phase === 'review' && reviewDraft) {
      if (Number.isFinite(reviewDraft.hover?.cutSec)) return true;
      if (reviewDrag?.kind === 'region-edge' && reviewDrag.moved && Number.isFinite(reviewDrag.to)) return true;
    }
    return false;
  }

  // Called by rhythm_timeline.js inside paintBeatAim() (60fps zero-heavy-redraw overlay!)
  function paintOverlay(guideLayerFn, rollClip, flagClip, noteOrigin, flagOrigin, offset, project) {
    if(!rollClip||!noteOrigin)return false;
    if(phase==='review'&&reviewDraft){
      const isEdge=reviewDrag?.kind==='region-edge'&&reviewDrag.moved&&Number.isFinite(reviewDrag.to);
      const isCut=!reviewDrag&&Number.isFinite(reviewDraft.hover?.cutSec);
      const guideSec=isEdge?reviewDrag.to:(isCut?reviewDraft.hover.cutSec:null);
      if(!Number.isFinite(guideSec))return false;
      const left=Math.floor(rollClip.left), top=Math.floor(rollClip.top);
      const width=Math.ceil(rollClip.right)-left, height=Math.ceil(rollClip.bottom)-top;
      const x=Math.round(noteOrigin.left-left+project(guideSec)-offset);
      if(x<-4||x>width+4)return false;
      const ctx=guideLayerFn(left,top,width,height);
      ctx.save();
      ctx.beginPath();ctx.rect(0,0,width,height);ctx.clip();
      ctx.strokeStyle=isCut?'rgba(176,48,72,.52)':'rgba(176,48,72,.44)';
      ctx.lineWidth=1;
      ctx.setLineDash(isCut?[4,3]:[3,3]);
      ctx.beginPath();ctx.moveTo(x+0.5,0);ctx.lineTo(x+0.5,height);ctx.stroke();
      ctx.setLineDash([]);
      if(isEdge&&height>=24){
        const label=reviewDrag.joint
          ? `↔ ${reviewDrag.to.toFixed(2)} s`
          : reviewDrag.edge==='end'
            ? `‹ ${reviewDrag.to.toFixed(2)} s`
            : `${reviewDrag.to.toFixed(2)} s ›`;
        ctx.font='500 10.5px AstraSans,Segoe UI,sans-serif';
        const tw=ctx.measureText(label).width+12;
        const preferRight=reviewDrag.edge==='end';
        const rawTx=preferRight?x+8:x-tw-8;
        const tx=Math.max(4,Math.min(width-tw-4,rawTx));
        const ty=6, th=18;
        roundedPillPath(ctx,tx,ty+1,tw,th,9,9);
        ctx.fillStyle='rgba(120,28,46,.12)';
        ctx.fill();
        roundedPillPath(ctx,tx,ty,tw,th,9,9);
        ctx.fillStyle='rgba(255,253,254,.96)';
        ctx.fill();
        ctx.strokeStyle='rgba(176,48,72,.38)';
        ctx.lineWidth=1;
        ctx.stroke();
        ctx.fillStyle='#9e2b42';
        ctx.fillText(label,tx+6,ty+12.5);
      }
      ctx.restore();
      return true;
    }
    if(phase!=='recording'||!currentPass.length)return false;
    const left=Math.floor(rollClip.left), top=Math.floor(rollClip.top);
    const width=Math.ceil(rollClip.right)-left,height=Math.ceil(rollClip.bottom)-top;
    const ctx=guideLayerFn(left,top,width,height);
    const now=performance.now();
    ctx.save();ctx.beginPath(); ctx.rect(0,0,width,height);ctx.clip();
    for(const tap of currentPass) {
      const sec=tap.snappedSec??tap.finalSec??tap.previewSec;
      const x=noteOrigin.left-left+project(sec)-offset;
      if(x<-16||x>width+16)continue;
      ctx.fillStyle=flagInk(tap.status);
      const age=Math.min(1,Math.max(0,(now-tap.rippleAt)/240));
      const onGrid=recGridSpan&&sec>=recGridSpan.start-1e-6&&sec<=recGridSpan.end+1e-6;
      if(age<1||!onGrid){
        ctx.globalAlpha=age<1?.45-age*.27:.18;
        ctx.fillRect(x,0,1,height);
      }
      ctx.globalAlpha=1;
      if(tap.matchedNote && typeof rollGeometry!=='undefined' && rollGeometry) {
        const ny=noteOrigin.top-top+(rollGeometry.high-tap.matchedNote.pitch)*rollGeometry.row;
        ctx.fillRect(x-1,ny,3,rollGeometry.row);
        ctx.fillRect(x-3,ny,7,1);
        ctx.fillRect(x-3,ny+rollGeometry.row-1,7,1);
      }
    }
    ctx.restore();
    return true;
  }

  // ---- Top Command Menu ("录制") + Clean Transport Button + Compact Visual HUD ----

  function commandMenuMarkup() {
    const lr = hasLR() ? `${rng().start.toFixed(1)}–${rng().end.toFixed(1)} 秒` : '需先在标尺拖出 L/R';
    const disabled = !canRecord();
    const recLabel = phase === 'recording' ? '停止录制拍点' : (phase === 'armed' ? '取消录拍待命' : '开始录制拍点');
    return `<details class="editor-command-menu beat-rec-menu" data-disclosure="editor-command-record">
      <summary><span class="beat-rec-menu-dot" aria-hidden="true"></span><span>录制</span></summary>
      <div class="editor-command-body beat-rec-menu-body">
        <button type="button" class="quiet beat-rec-menu-start" data-do="editorBeatRecordToggle" aria-pressed="${phase !== 'idle'}" ${disabled ? 'disabled' : ''}>
          <span class="beat-rec-dot" aria-hidden="true"></span><span>${recLabel}</span><kbd>Shift+T</kbd>
        </button>
        <section class="beat-rec-menu-section" aria-label="拍点录制设置">
          <strong>拍点录制设置</strong>
          <label class="beat-rec-opt"><span>吸附音头</span><input type="checkbox" data-beat-rec-pref="snapToMidi" ${prefs.snapToMidi ? 'checked' : ''}></label>
          <label class="beat-rec-opt is-punch" data-on="${prefs.lrPunch}"><span class="beat-rec-opt-meta"><span>只在 L/R 范围内覆盖</span><small id="beat-rec-lr-label">${lr}</small></span><input type="checkbox" data-beat-rec-pref="lrPunch" ${prefs.lrPunch ? 'checked' : ''}></label>
          <div id="beat-rec-preroll-row" class="beat-rec-subrow" ${prefs.lrPunch ? '' : 'hidden'}><span>提前起播</span><div class="beat-rec-seg" role="group" aria-label="提前起播小节">${[0, 1, 2].map(n => `<button type="button" data-beat-rec-preroll="${n}" aria-pressed="${prefs.preRollBars === n}">${n ? n + ' 小节' : '不提前'}</button>`).join('')}</div></div>
          <label class="beat-rec-opt"><span>录拍时关闭节拍器</span><input type="checkbox" data-beat-rec-pref="muteOldMetronome" ${prefs.muteOldMetronome ? 'checked' : ''}></label>
        </section>
        <div class="beat-rec-menu-keys"><kbd>Tab</kbd> 打点 · 按住 <kbd>B</kbd> 临时不吸附</div>
        <details class="beat-rec-advanced">
          <summary>蓝牙 / 手感偏移（<span id="beat-rec-offset-summary">${prefs.offsetMs}</span> ms）</summary>
          <div class="beat-rec-offset-row">
            <small>整体偏早或偏晚再调，平时保持 0</small>
            <div class="beat-rec-offset-box"><input id="beat-rec-offset-ms" type="number" min="-150" max="150" step="5" value="${prefs.offsetMs}"><span>ms</span></div>
          </div>
        </details>
      </div>
    </details>`;
  }

  function displayPhase() {
    return phase==='recording' && preRollActive ? 'preroll' : phase;
  }
  function phaseLabel() {
    return {idle:'录拍',armed:clockMissing()?'等待播放时钟':'已待录',preparing:'准备',preroll:'预滚中',recording:'录拍中',review:'录拍预览'}[displayPhase()];
  }

  function transportMarkup() {
    return `<div id="transport-beat-rec" class="transport-beat-rec" role="group" aria-label="录制拍点" data-lr-punch="${activePunch()}" data-phase="${displayPhase()}">
      <button type="button" id="transport-beat-rec-btn" class="transport-beat-rec-btn" data-do="editorBeatRecordToggle" aria-pressed="${phase !== 'idle'}" style="--beat-prep-ring:${preparation?Math.min(100,(performance.now()-preparation.started)/PREPARATION_MS*100):0}%" title="录制拍点（Tab 打点 · 设置在顶部「录制」菜单）">
        <span class="beat-rec-dot" aria-hidden="true"></span>
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M7 4v16M7 5h10l-2.5 4L17 13H7"/></svg>
        <span class="beat-rec-label" aria-live="polite">${phaseLabel()}</span>
        <span id="transport-beat-rec-count" class="beat-rec-count" hidden>0</span>
      </button>
    </div>`;
  }

  function hudMarkup() {
    return `<div id="beat-rec-hud" class="beat-rec-hud" data-phase="${phase}" ${phase === 'idle' ? 'hidden' : ''}>${phase === 'idle' ? '' : renderHudInner()}</div>`;
  }

  function magnetIcon() {
    return `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 4v8a6 6 0 0 0 12 0V4M6 8h3M15 8h3"/></svg>`;
  }

  function syncUI() {
    if (typeof syncRecordingSeekControls==='function') syncRecordingSeekControls();
    if (typeof syncRecordingProjectSave==='function') syncRecordingProjectSave();
    const axis=$('roll-axis');
    if(axis){
      axis.disabled=phase!=='idle';
      if(axis.disabled)axis.setAttribute('aria-description','先确认或取消录拍');
      else axis.removeAttribute('aria-description');
    }
    const t = $('transport');
    if (t) {
      if (phase === 'idle') delete t.dataset.beatRec;
      else t.dataset.beatRec = phase;
    }
    const wrap = $('transport-beat-rec');
    const btn = $('transport-beat-rec-btn');
    const countEl = $('transport-beat-rec-count');
    const allowed = canRecord();
    if (wrap) {
      wrap.hidden = !ed()?.workingView?.();
      wrap.dataset.lrPunch = String(activePunch());
      wrap.dataset.phase = displayPhase();
    }
    if (btn) {
      const label=btn.querySelector('.beat-rec-label');
      const nextLabel=phaseLabel();
      if (label && label.textContent!==nextLabel) label.textContent=nextLabel;
      btn.disabled = !allowed || phase==='review';
      btn.setAttribute('aria-pressed', String(phase !== 'idle'));
      btn.title = allowed ? '录制拍点（Tab 打点 · 设置在顶部「录制」菜单）' : '请先在音符页切换到「编辑」模式后再录制拍点';
    }
    const menuStart = document.querySelector('.beat-rec-menu-start');
    if (menuStart) {
      menuStart.disabled = !allowed;
      menuStart.setAttribute('aria-pressed', String(phase !== 'idle'));
      const span = menuStart.querySelector('span:nth-child(2)');
      if (span) span.textContent = phase === 'recording' ? '停止录制拍点' : (phase === 'armed' ? '取消录拍待命' : '开始录制拍点');
    }
    if (countEl) {
      const cnt = currentPass.length || lastCompletedPass.length;
      countEl.hidden = phase !== 'recording' || cnt === 0;
      countEl.textContent = String(cnt);
    }
    const lrLabel = $('beat-rec-lr-label');
    if (lrLabel) lrLabel.textContent = hasLR() ? `${rng().start.toFixed(1)}–${rng().end.toFixed(1)} 秒` : '需先在标尺拖出 L/R';
    const preRow = $('beat-rec-preroll-row');
    if (preRow) preRow.hidden = !prefs.lrPunch;
    const punchOpt = document.querySelector('.beat-rec-opt.is-punch');
    if (punchOpt) punchOpt.dataset.on = String(prefs.lrPunch);
    document.querySelectorAll('[data-beat-rec-pref]').forEach(el => {
      const k = el.dataset.beatRecPref;
      if (k in prefs && el.checked !== Boolean(prefs[k])) el.checked = Boolean(prefs[k]);
    });
    document.querySelectorAll('[data-beat-rec-preroll]').forEach(el => {
      el.setAttribute('aria-pressed', String(Number(el.dataset.beatRecPreroll) === prefs.preRollBars));
    });
    const offInput = $('beat-rec-offset-ms');
    if (offInput && document.activeElement !== offInput) offInput.value = String(prefs.offsetMs);
    const offSum = $('beat-rec-offset-summary');
    if (offSum) offSum.textContent = String(prefs.offsetMs);

    const hud = $('beat-rec-hud');
    if (hud) {
      const hidden = phase === 'idle';
      if (hud.hidden !== hidden) hud.hidden = hidden;
      if (hud.dataset.phase !== phase) hud.dataset.phase = phase;
      if (hidden) {
        lastHudEl = hud;
        lastHudHtml = '';
      } else {
        const nextHtml = renderHudInner();
        if (lastHudEl !== hud || lastHudHtml !== nextHtml) {
          hud.innerHTML = nextHtml;
          lastHudEl = hud;
          lastHudHtml = nextHtml;
        }
      }
    }
    ed()?.syncHistoryButtons?.();
    window.AstraTransportFoldStatus?.();
  }

  function renderHudInner() {
    const parts = [];
    const snapBtn = `<button type="button" data-do="editorBeatRecordSnapToggle" aria-pressed="${prefs.snapToMidi}" title="吸附到当前 MIDI 音符开头">${magnetIcon()}<span>吸附音头</span></button>`;
    if (phase === 'armed') {
      parts.push(`<span class="beat-rec-pill is-armed"><span class="beat-rec-hud-dot"></span>准备就绪</span>`);
      if (activePunch()) parts.push(`<span class="beat-rec-pill is-punch">只在 L/R</span>`);
      if (lastClock && lastClock.audible === false) parts.push(`<span class="beat-rec-pill is-warn">无声音源 · 可切原曲音频</span>`);
      if (clockMissing()) parts.push('<span class="beat-rec-pill is-warn">尚未收到可用的播放时钟</span>');
      if (previewError) parts.push(`<span class="beat-rec-pill is-warn">${previewError}</span>`);
      const instruction=armedPlayRequested ? '等待播放开始' :
        (isPlayingNow() ? '先暂停当前播放，再按播放开始录拍' : '按播放或空格开始录拍');
      parts.push(`<span class="beat-rec-hud-keys">${instruction}</span>`);
      parts.push(`<span class="beat-rec-hud-actions">${snapBtn}<button type="button" data-do="editorBeatRecordCancel" title="退出 (Esc)">✕</button></span>`);
    } else if (phase==='preparing') {
      parts.push('<span class="beat-rec-pill is-armed"><span class="beat-rec-hud-dot"></span>准备</span>');
      parts.push('<span class="beat-rec-hud-actions"><button type="button" data-do="editorBeatRecordStop" title="只取消录拍">取消录拍</button></span>');
    } else if (phase === 'recording') {
      if (clockMissing()) parts.push('<span class="beat-rec-pill is-warn">播放时钟中断，暂不接收打点</span>');
      const cnt = currentPass.length;
      const badge = preRollActive ? '提前起播' : (cnt ? `${cnt} 拍` : '录拍中');
      parts.push(`<span class="beat-rec-pill is-recording"><span class="beat-rec-hud-dot"></span>${badge}</span>`);
      if (passNumber > 1) parts.push(`<span class="beat-rec-pill is-pass">第 ${passNumber} 遍${cnt === 0 && lastCompletedPass.length ? ' · 保留上遍' : ''}</span>`);
      if (activePunch()) parts.push(`<span class="beat-rec-pill is-punch">只在 L/R</span>`);
      if (bHeld) parts.push(`<span class="beat-rec-pill is-warn">临时不吸附</span>`);
      parts.push(`<span class="beat-rec-hud-actions">
        <button type="button" class="is-tap" id="beat-rec-tap-pad" title="点击或按 Tab 打点">打点 <kbd>Tab</kbd></button>
        ${snapBtn}
        <button type="button" data-do="editorBeatRecordStop" title="停止 (Space)">■</button>
        <button type="button" data-do="editorBeatRecordCancel" title="放弃 (Esc)">✕</button>
      </span>`);
    } else if (phase === 'review') {
      const imp = previewData?.result?.impact;
      const ins = imp?.inserted_point_count ?? resolveEffectivePass().length;
      const rep = imp?.replaced_point_count ?? 0;
      const amb = imp?.snap_preview?.ambiguous_count || 0;
      const meters = imp?.replaced_meter_count || 0;
      parts.push(`<span class="beat-rec-pill is-review">新录 ${ins} 拍 · 替换 ${rep} 拍</span>`);
      if (imp?.crosses_bar1) parts.push(`<span class="beat-rec-pill is-warn">跨第1小节</span>`);
      if (amb > 0) parts.push(`<span class="beat-rec-pill is-warn">${amb} 处模糊</span>`);
      if (glitchDetected) parts.push(`<span class="beat-rec-pill is-warn">播放卡顿</span>`);
      if (previewError) parts.push(`<span class="beat-rec-pill is-warn">${previewError}</span>`);
      const regs = draftRegions();
      const selIdx = reviewDraft?.selectedRegion ? regs.indexOf(reviewDraft.selectedRegion) : -1;
      const hintText = selIdx >= 0
        ? `已选第 ${selIdx + 1}/${regs.length} 段 · Delete ${regs.length === 1 ? '取消录拍' : '移除本段'} · 拖边自由伸缩 · Ctrl+Z 撤销`
        : regs.length > 1
          ? `共 ${regs.length} 段 · 拖切口左右边可直接分开 · Alt 切段 · Ctrl+Z 撤销`
          : `Alt 切段后直接拖边可分开 · 拖边自由伸缩 · Ctrl+Z 撤销`;
      parts.push(`<span class="beat-rec-hud-keys is-review-hint">${hintText}</span>`);
      let meterUi = '';
      if (meters > 0) {
        meterUi = `<span class="beat-rec-meter-group" title="覆盖范围内有 ${meters} 个拍号">
          <button type="button" data-beat-rec-meter="delete" aria-pressed="${prefs.meterResolution === 'delete'}">删拍号</button>
          <button type="button" data-beat-rec-meter="previous" aria-pressed="${prefs.meterResolution === 'previous'}">移到前拍</button>
          <button type="button" data-beat-rec-meter="next" aria-pressed="${prefs.meterResolution === 'next'}">移到后拍</button>
        </span>`;
      }
      parts.push(`<span class="beat-rec-hud-actions">
        ${meterUi}
        ${snapBtn}
        <button type="button" class="is-primary" data-do="editorBeatRecordApply" ${previewLoading || committing || (!previewData&&hasDraftWrite()) ? 'disabled' : ''}>${hasDraftWrite()?'应用':'完成'} <kbd>Enter</kbd></button>
        <button type="button" data-do="editorBeatRecordRetry" title="重录 (R)">↺</button>
        <button type="button" data-do="editorBeatRecordCancel" title="放弃 (Esc)">✕</button>
      </span>`);
    }
    return parts.join('');
  }

  function action(name) {
    if (!name || !name.startsWith('editorBeatRecord')) return false;
    switch (name) {
      case 'editorBeatRecordToggle': toggle(); return true;
      case 'editorBeatRecordStop':
        stopRecordingOnly();
        return true;
      case 'editorBeatRecordSnapToggle':
        if (phase === 'review' && reviewDraft) pushDraftUndo();
        prefs.snapToMidi = !prefs.snapToMidi;
        savePrefs();
        if (phase === 'review') runPreview();
        else syncUI();
        return true;
      case 'editorBeatRecordApply': commit(); return true;
      case 'editorBeatRecordRetry': retry(); return true;
      case 'editorBeatRecordCancel': cancel(); return true;
      default: return false;
    }
  }

  // Review owns the preview handles (not the original pulse editor).
  // All captured points use the same projection as the paint path.
  function acceptedReviewPoint(i) {
    const b=reviewDraft.op.beats[i];
    const manualSt=reviewDraft.edited?.has(i)?reviewDraft.manualStatus?.get(i):undefined;
    const live=previewData?.points?.get(i);
    if(live)return manualSt&&live.status!==manualSt?{...live,status:manualSt}:live;
    const cached=reviewDraft.lastPoints?.get(i);
    if(cached&&b){
      const wantSnap=Boolean(prefs.snapToMidi&&!reviewDraft.free.has(i)&&!reviewDraft.edited.has(i));
      if(cached.raw===b.seconds&&cached.snap===wantSnap){
        return manualSt&&cached.pt?.status!==manualSt?{...cached.pt,status:manualSt}:cached.pt;
      }
    }
    return manualSt?{seconds:b.seconds,status:manualSt}:{seconds:b.seconds};
  }
  function reviewPoint(i) {
    return reviewDrag?.preview?.get(i) || acceptedReviewPoint(i);
  }

  function reviewPointer(event, lane=$('beat-flags-canvas')) {
    if (phase!=='review' || !reviewDraft || !lane || event.target!==lane) return null;
    const rect=typeof rollPointerRect==='function'?rollPointerRect(lane):lane.getBoundingClientRect();
    const x=event.clientX-rect.left+($('roll-scroll')?.scrollLeft||0),y=event.clientY-rect.top,sec=midiSeconds(x);
    const rows=reviewDraft.op.beats.flatMap((_,i)=>inDraft(i)?[{x:midiX(reviewPoint(i).seconds),key:i}]:[]);
    const index=window.AstraRhythmTimeline?.sharedFlagHit(rows,x,y,row=>reviewSelected.has(row.key))?.key??-1;
    const regions=draftRegions();
    const strip=y>=0&&y<7&&(y<6||index<0);
    let r=draftRegionAt(sec),edge=null;
    if(strip&&!r)r=regions.find(q=>Math.min(Math.abs(x-midiX(q.start)),Math.abs(x-midiX(q.end)))<=6);
    if(strip&&r){
      const wPx=Math.max(0,midiX(r.end)-midiX(r.start));
      const edgeTol=Math.min(6,Math.max(2.5,wPx*0.25));
      const ds=Math.abs(x-midiX(r.start)),de=Math.abs(x-midiX(r.end));
      if(Math.min(ds,de)<=edgeTol)edge=ds<=de?'start':'end';
    }
    const altRegion=event.altKey?draftRegionAt(sec):null;
    const canSplit=Boolean(altRegion&&sec>altRegion.start+REGION_FLOOR&&sec<altRegion.end-REGION_FLOOR);
    // Outside the take the ordinary beat editor keeps working on the project.
    const covered=index>=0||Boolean(r)||Boolean(event.altKey&&altRegion);
    return {lane,x,y,index,sec,r,edge,strip:strip&&Boolean(r),altRegion,canSplit,covered};
  }

  function consumeReviewPointer(event) {
    event.preventDefault();
    event.stopImmediatePropagation();
  }
  function releaseReviewDrag() {
    const g=reviewDrag;reviewDrag=null;
    if(g?.lane?.hasPointerCapture?.(g.pointer))g.lane.releasePointerCapture(g.pointer);
    if(g?.lane?.style)g.lane.style.cursor='';
    requestOverlayRepaint();
    return g;
  }
  function draftMoveBounds(indices,from) {
    const {beats}=reviewDraft.op;
    const memberships=indices.map(i=>draftRegionAt(beats[i].seconds));
    if(memberships.some(r=>!r))return [0,0];
    const start=Math.max(...memberships.map((r,j)=>(r.includeStart===false?r.start+1e-5:r.start)-acceptedReviewPoint(indices[j]).seconds+from));
    const end=Math.min(...memberships.map((r,j)=>(r.includeEnd===false?Math.max(r.start,r.end-1e-5):r.end)-acceptedReviewPoint(indices[j]).seconds+from));
    const rows=beats.flatMap((b,i)=>inDraft(i)?[{seconds:acceptedReviewPoint(i).seconds,rawSeconds:b.seconds}]:[]).sort((a,b)=>a.seconds-b.seconds);
    const result=window.AstraRhythmTimeline?.sharedFlagBounds(rows,indices.map(i=>acceptedReviewPoint(i).seconds).sort((a,b)=>a-b),from,false,
      {start,end,documentEnd:Number(doc()?.duration)||Infinity});
    return result ? [result.min-from,result.max-from] : [0,0];
  }
  function captureReview(g,event) {
    reviewDrag=g;
    if(reviewDraft)reviewDraft.hover=null;
    try{g.lane.setPointerCapture?.(event.pointerId);}catch{}
    g.lane.tabIndex=-1;g.lane.focus?.({preventScroll:true});
    const role=g.kind==='region-edge'?'ew-resize':g.kind==='box'?'range':g.kind==='insert'?'beat':g.kind==='erase'?'erase':g.lower===g.upper?'not-allowed':'grabbing';
    setLaneCursor(g.lane,role);
    requestRollDraw();
    requestOverlayRepaint();
  }
  function reviewDown(event) {
    const hit=reviewPointer(event);
    if(!hit)return;
    if(!hit.covered){
      if(event.button===0&&!event.shiftKey&&(reviewSelected.size||reviewDraft?.selectedRegion)){
        reviewSelected.clear();reviewDraft.selectedRegion=null;syncUI();requestRollDraw();
      }
      return;
    }
    const isPencil=hit.r&&!hit.strip&&hit.index<0&&ed()?.rhythmTool?.()==='beat'&&!event.altKey&&event.button===0;
    if(isPencil)event.stopImmediatePropagation();else consumeReviewPointer(event);
    reviewDraft.pencilClick=null;
    if(event.button!==0||reviewDrag||committing)return;
    if(!ensureReviewScope())return;
    applyQueuedSeq=null;
    const g={lane:hit.lane,pointer:event.pointerId,clientX:event.clientX,
      pressX:hit.x,moved:false,lastEvent:event};
    // Alt-cut works anywhere vertically inside a region on the beat lane
    if(event.altKey){
      if(hit.canSplit){
        const r=hit.altRegion,sec=hit.sec;
        reviewSelected.clear();
        pushDraftUndo();
        const left={start:r.start,end:sec,includeStart:r.includeStart!==false,includeEnd:false};
        const right={start:sec,end:r.end,includeStart:true,includeEnd:r.includeEnd!==false};
        draftRegions().splice(draftRegions().indexOf(r),1,left,right);
        reviewDraft.selectedRegion=right;
        reviewDraft.projected=null;
        reviewDraft.hover=null;
        reviewDraft.cutAnims.set(sec,{at:performance.now()});
        hit.lane.tabIndex=-1;hit.lane.focus?.({preventScroll:true});
        syncUI();
        requestRollDraw();
        requestOverlayRepaint();
      }
      return;
    }
    if(hit.strip){
      reviewSelected.clear();
      reviewDraft.selectedRegion=hit.r;
      hit.lane.tabIndex=-1;hit.lane.focus?.({preventScroll:true});
      if(hit.edge){
        const regions=draftRegions(),j=regions.indexOf(hit.r),isStart=hit.edge==='start';
        const other=isStart?regions[j-1]:regions[j+1];
        const from=hit.r[hit.edge];
        const touching=Boolean(other&&from===other[isStart?'end':'start']);
        // Split blocks peel apart independently on plain drag; hold Shift to slide both sides together.
        const joint=touching&&event.shiftKey?other:null;
        const floor=reviewDraft.op.start,ceil=reviewDraft.op.end;
        const min=isStart?(joint?joint.start+REGION_FLOOR:other?.end??floor):hit.r.start+REGION_FLOOR;
        const max=isStart?hit.r.end-REGION_FLOOR:joint?joint.end-REGION_FLOOR:other?.start??ceil;
        captureReview({...g,kind:'region-edge',region:hit.r,edge:hit.edge,joint,other,from,min,max,to:from},event);
      }else{syncUI();requestRollDraw();}
      return;
    }
    if(!hit.r){reviewDraft.selectedRegion=null;reviewSelected.clear();syncUI();requestRollDraw();return;}
    reviewDraft.selectedRegion=null;
    const tool=ed()?.rhythmTool?.();
    if(tool==='beat'&&hit.index<0){
      captureReview({...g,kind:'insert',region:hit.r,from:hit.sec},event);
      sampleReview(reviewDrag,event);
      return;
    }
    if(tool==='eraseBeat'){
      const erase=new Set();
      if(hit.index>=0)erase.add(hit.index);
      captureReview({...g,kind:'erase',erase},event);
      return;
    }
    if(hit.index<0||ed()?.rhythmTool?.()==='rangeSelect'){
      const original=event.shiftKey?new Set(reviewSelected):new Set();
      if(!event.shiftKey)reviewSelected.clear();
      captureReview({...g,kind:'box',original,currentX:hit.x},event);return;
    }
    const i=hit.index;
    if(event.shiftKey){
      const next=window.AstraRhythmTimeline?.sharedFlagChoose([...reviewSelected],i,true)||[];
      reviewSelected.clear();for(const key of next)reviewSelected.add(key);
      hit.lane.tabIndex=-1;hit.lane.focus?.({preventScroll:true});syncUI();requestRollDraw();return;
    }
    if(!reviewSelected.has(i)){
      reviewSelected.clear();
      for(const key of window.AstraRhythmTimeline?.sharedFlagChoose([],i)||[])reviewSelected.add(key);
    }
    const indices=[...reviewSelected].sort((a,b)=>a-b),from=acceptedReviewPoint(i).seconds;
    const points=indices.map(k=>acceptedReviewPoint(k).seconds).sort((a,b)=>a-b);
    const [lower,upper]=draftMoveBounds(indices,from);
    if(typeof suspendRollFollow==='function'&&lower!==upper)suspendRollFollow();
    captureReview({...g,kind:'move',index:i,indices,points,lower,upper,from,raw:from,to:from,limited:false,preview:new Map()},event);
  }
  function reviewNoteHit(event,offset) {
    const canvas=$('midi-canvas'),roll=$('roll-scroll');
    if(!canvas||!roll)return null;
    const note=typeof rollPointerRect==='function'?rollPointerRect(canvas):canvas.getBoundingClientRect();
    const clip=roll.getBoundingClientRect(),x=event.clientX,y=event.clientY;
    return x>=Math.max(note.left,clip.left)&&x<Math.min(note.right,clip.right)&&
      y>=Math.max(note.top,clip.top)&&y<Math.min(note.bottom,clip.bottom)
        ?{worldX:x-note.left+offset,y:y-note.top}:null;
  }
  function displayDraftRegions(){
    const g=reviewDrag;
    if(g?.kind!=='region-edge'||!g.moved)return draftRegions();
    return draftRegions().map(r=>{
      if(r===g.region){
        const next={...r,[g.edge]:g.to,owner:r};
        if(!g.joint){
          if(g.edge==='end'){
            if(g.other&&g.to===g.other.start)next.includeEnd=false;
            else if(g.to===reviewDraft.op.end)next.includeEnd=true;
          }else if(g.edge==='start'){
            if(g.other&&g.to===g.other.end)next.includeStart=true;
            else if(g.to===reviewDraft.op.start)next.includeStart=true;
          }
        }
        return next;
      }
      if(r===g.joint)return {...r,[g.edge==='start'?'end':'start']:g.to,owner:r};
      if(!g.joint&&r===g.other){
        if(g.edge==='end'&&g.to===r.start)return {...r,includeStart:true,owner:r};
        if(g.edge==='start'&&g.to===r.end)return {...r,includeEnd:false,owner:r};
      }
      return r;
    });
  }
  function displayDraftGroups(){
    const g=reviewDrag?.kind==='region-edge'&&reviewDrag.moved?reviewDrag:null,to=g?.to;
    const hold=reviewDraft.projected;
    if(hold&&hold.g===g&&hold.to===to)return hold.regions;
    const regions=regionProjection(displayDraftRegions());
    reviewDraft.projected={g,to,regions};
    return regions;
  }
  function hasValidOnsetAt(seconds, eps = 1e-4) {
    const clusters = getOnsetClusters();
    if (!clusters.length || !Number.isFinite(seconds)) return false;
    let lo = 0, hi = clusters.length;
    while (lo < hi) { const mid = (lo + hi) >>> 1; if (clusters[mid].onset < seconds - eps) lo = mid + 1; else hi = mid; }
    return lo < clusters.length && Math.abs(clusters[lo].onset - seconds) <= eps;
  }
  function snapReviewOnset(rawSec, event, { axis, min, max, noteHit }) {
    const limited = Math.min(max, Math.max(min, rawSec));
    const snapOff = !prefs.snapToMidi || event.ctrlKey || event.metaKey || bHeld;
    if (snapOff) return { to: limited, aim: null, onset: false, limited: limited !== rawSec };
    const clusters = getOnsetClusters();
    const project = axis?.x ?? midiX;
    const rawX = project(rawSec);
    let best = null, bestPx = 12.5;
    if (noteHit && Number.isFinite(noteHit.start) && noteHit.start >= min - 1e-7 && noteHit.start <= max + 1e-7) {
      const dx = Math.abs(project(noteHit.start) - rawX);
      if (dx <= 14 && Math.abs(noteHit.start - rawSec) <= 0.12) {
        best = { seconds: noteHit.start, note: noteHit };
        bestPx = dx;
      }
    }
    if (!best && clusters.length) {
      let lo = 0, hi = clusters.length;
      const searchLo = Math.max(min, rawSec - 0.15);
      const searchHi = Math.min(max, rawSec + 0.15);
      while (lo < hi) { const mid = (lo + hi) >>> 1; if (clusters[mid].onset < searchLo) lo = mid + 1; else hi = mid; }
      for (let i = lo; i < clusters.length && clusters[i].onset <= searchHi; i++) {
        const c = clusters[i];
        if (c.onset < min - 1e-7 || c.onset > max + 1e-7) continue;
        const dx = Math.abs(project(c.onset) - rawX);
        if (dx < bestPx && Math.abs(c.onset - rawSec) <= 0.10) {
          bestPx = dx;
          best = { seconds: c.onset, note: c.note };
        }
      }
    }
    if (best) {
      return {
        to: Math.min(max, Math.max(min, best.seconds)),
        aim: { kind: 'note', edge: 'start', onset: true, note: best.note, seconds: best.seconds },
        onset: true,
        limited: limited !== rawSec,
      };
    }
    return { to: limited, aim: null, onset: false, limited: limited !== rawSec };
  }
  function changeDraft(){
    reviewDraft.regionEdited=true;
    reviewDraft.projected=null;
    previewData=null;
    reviewGridRegions=regionProjection(draftRegions(),true);
    ++reviewMaskVersion;
    runPreview();requestRollDraw();
  }
  function deleteDraftSelection(){
    if(!reviewDraft||committing||reviewDrag)return;
    const picked=reviewDraft.selectedRegion;
    if(picked){
      const remaining=reviewDraft.regions.filter(r=>r!==picked);
      if(!remaining.length){
        cleanup('idle',{fadeOut:true});
        ed()?.rhythmMessage?.('已取消本次录拍');
        return;
      }
      pushDraftUndo();
      spawnGhostStrip(picked);
      reviewDraft.regions=remaining;
      reviewDraft.selectedRegion=null;
    }else if(reviewSelected.size){
      pushDraftUndo();
      for(const i of reviewSelected)reviewDraft.deleted.add(i);
      reviewSelected.clear();
    }else return;
    changeDraft();
  }
  function sampleReview(g,event) {
    if(!ensureReviewScope())return;
    const rect=typeof rollPointerRect==='function'?rollPointerRect(g.lane):g.lane.getBoundingClientRect();
    const offset=$('roll-scroll')?.scrollLeft||0,world=event.clientX-rect.left+offset;
    g.moved ||= !!window.AstraRhythmTimeline?.sharedFlagDragged(g.clientX,event.clientX);
    g.lastEvent=event;
    if(g.kind==='insert'){
      const axis=typeof rollAxis==='function'?rollAxis():null;
      g.raw=(axis?.seconds??midiSeconds)(world);
      const rStart=g.region.includeStart===false?g.region.start+1e-5:g.region.start;
      const rEnd=g.region.includeEnd===false?Math.max(rStart,g.region.end-1e-5):g.region.end;
      const min=Math.max(rStart,1e-7),max=Math.min(rEnd,(Number(doc()?.duration)||Infinity)-1e-7);
      const result=snapReviewOnset(g.raw,event,{axis,min,max,noteHit:reviewNoteHit(event,offset)});
      g.aim=result.aim;g.to=result.to;g.limited=Boolean(result.limited);
      requestBeatLaneDraw();
      requestOverlayRepaint();
      return;
    }
    if(g.kind==='erase'){
      const lo=Math.min(g.currentX??g.pressX,world),hi=Math.max(g.currentX??g.pressX,world);
      for(let i=0;i<reviewDraft.op.beats.length;i++){
        if(inDraft(i)&&midiX(acceptedReviewPoint(i).seconds)>=lo-3&&midiX(acceptedReviewPoint(i).seconds)<=hi+3)g.erase.add(i);
      }
      g.currentX=world;requestBeatLaneDraw();return;
    }
    if(!g.moved)return;
    if(g.kind==='box') {
      g.currentX=world;
      const lo=midiSeconds(Math.min(g.pressX,world)),hi=midiSeconds(Math.max(g.pressX,world));
      const rows=reviewDraft.op.beats.flatMap((_,i)=>inDraft(i)?[{seconds:acceptedReviewPoint(i).seconds,key:i}]:[]);
      const next=window.AstraRhythmTimeline?.sharedFlagBox(rows,g.original,lo,hi)||[];
      reviewSelected.clear();for(const i of next)reviewSelected.add(i);
      requestBeatLaneDraw();return;
    }
    if(g.kind==='region-edge'){
      // Silky 1:1 linear edge tracking — never sticks to MIDI note onsets/ends underneath.
      const raw=midiSeconds(midiX(g.from)+world-g.pressX);
      let to=Math.max(g.min,Math.min(g.max,raw));
      if(!g.joint&&g.other&&!event.ctrlKey&&!event.metaKey){
        const seam=g.edge==='end'?g.other.start:g.other.end;
        if(Math.abs(midiX(to)-midiX(seam))<=4)to=seam;
      }
      g.aim=null;
      g.to=to;
      ++reviewMaskVersion;
      requestRollDraw();
      requestOverlayRepaint();
      return;
    }
    if(g.lower===g.upper){
      g.raw=g.from;g.to=g.from;g.limited=false;
      requestOverlayRepaint();
      return;
    }
    const axis=typeof rollAxis==='function'?rollAxis():null;
    const project=axis?.x??midiX,invert=axis?.seconds??midiSeconds;
    const raw=invert(project(g.from)+world-g.pressX);
    const min=g.from+g.lower,max=g.from+g.upper,limited=Math.min(max,Math.max(min,raw));
    const snapped=snapReviewOnset(raw,event,{axis,min,max,noteHit:reviewNoteHit(event,offset)});
    g.aim=snapped.aim;
    const to=snapped.to,delta=to-g.from;
    g.raw=raw;g.to=to;g.limited=Boolean(snapped.limited||limited!==raw);
    const snapActive=Boolean(prefs.snapToMidi&&!event.ctrlKey&&!event.metaKey&&!bHeld);
    g.preview.clear();
    for(const i of g.indices){
      const pt=acceptedReviewPoint(i);
      const sec=pt.seconds+delta;
      const isOnset=i===g.index?Boolean(snapped.onset):(snapActive&&hasValidOnsetAt(sec));
      const nextStatus=isOnset?'midi_onset':(snapActive?'no_nearby_onset':'raw');
      g.preview.set(i,{seconds:sec,status:nextStatus,raw_seconds:pt.seconds+limited-g.from});
    }
    ++reviewMaskVersion;
    requestRollDraw();
    requestOverlayRepaint();
  }
  function updateReviewHover(event) {
    const hit=reviewPointer(event);
    if(!hit?.covered){
      if(reviewDraft?.hover){reviewDraft.hover=null;requestBeatLaneDraw();requestOverlayRepaint();}
      return false;
    }
    let nextHover=null,role='';
    if(hit.canSplit){
      nextHover={cut:hit.x,cutSec:hit.sec,r:hit.altRegion,edge:null};
      role='cut';
    }else if(event.altKey){
      nextHover=hit.r?{cut:null,cutSec:null,r:hit.r,edge:null}:null;
      role=hit.r?'not-allowed':'';
    }else if(hit.strip){
      nextHover={cut:null,cutSec:null,r:hit.r,edge:hit.edge};
      role=hit.edge?'ew-resize':'pointer';
    }else{
      const tool=ed()?.rhythmTool?.();
      role=hit.index>=0?(tool==='eraseBeat'?'erase':'grab'):hit.r&&tool==='beat'?'beat':hit.r&&tool==='eraseBeat'?'erase':hit.r&&tool==='rangeSelect'?'range':'';
    }
    const prev=reviewDraft.hover;
    const changed=(prev?.cut??null)!==(nextHover?.cut??null)||(prev?.r??null)!==(nextHover?.r??null)||(prev?.edge??null)!==(nextHover?.edge??null);
    setLaneCursor(hit.lane,role);
    if(changed){
      reviewDraft.hover=nextHover;
      requestBeatLaneDraw();
      requestOverlayRepaint();
    }
    return true;
  }
  function reviewMove(event) {
    const g=reviewDrag;
    if(!g||event.pointerId!==g.pointer){
      // An ordinary beat gesture that started outside the take keeps its pointer.
      if(!g&&window.AstraRhythmTimeline?.isDragging?.())return;
      const hit=reviewPointer(event);
      if(hit?.covered&&!g){
        consumeReviewPointer(event);
        window.AstraRhythmTimeline?.clearHover?.();
        lastReviewHover={target:event.target,clientX:event.clientX,clientY:event.clientY,
          altKey:Boolean(event.altKey),ctrlKey:Boolean(event.ctrlKey),metaKey:Boolean(event.metaKey),shiftKey:Boolean(event.shiftKey)};
        updateReviewHover(event);
      }else{
        if(lastReviewHover&&event.target!==lastReviewHover.target)lastReviewHover=null;
        if(reviewDraft?.hover){reviewDraft.hover=null;requestBeatLaneDraw();requestOverlayRepaint();}
      }
      return;
    }
    consumeReviewPointer(event);sampleReview(g,event);
  }
  function reviewUp(event) {
    const g=reviewDrag;
    if(!g||event.pointerId!==g.pointer)return;
    if(g.kind==='insert')event.stopImmediatePropagation();else consumeReviewPointer(event);
    if(event.type==='pointerup')sampleReview(g,event);
    if(reviewDrag!==g)return;
    releaseReviewDrag();
    if(event.type==='pointerup'&&g.kind==='insert'){
      reviewDraft.pencilClick=g;
      requestRollDraw();
      return;
    }
    if(event.type==='pointerup'&&g.kind==='erase'){
      if(g.erase.size){
        pushDraftUndo();
        for(const i of g.erase){reviewDraft.deleted.add(i);reviewSelected.delete(i);}
        changeDraft();
      }else requestRollDraw();
      return;
    }
    if(event.type==='pointerup'&&g.moved&&g.kind==='region-edge'){
      if(g.to!==g.from){
        pushDraftUndo();
        g.region[g.edge]=g.to;
        if(g.joint){
          g.joint[g.edge==='start'?'end':'start']=g.to;
        }else{
          if(g.edge==='end'){
            if(g.other&&g.to===g.other.start){g.region.includeEnd=false;g.other.includeStart=true;}
            else if(g.to===reviewDraft.op.end)g.region.includeEnd=true;
          }else if(g.edge==='start'){
            if(g.other&&g.to===g.other.end){g.region.includeStart=true;g.other.includeEnd=false;}
            else if(g.to===reviewDraft.op.start)g.region.includeStart=true;
          }
        }
        changeDraft();
      }else requestRollDraw();
      return;
    }
    if(event.type!=='pointerup'||!g.moved||g.kind!=='move'||!g.preview.size){requestRollDraw();return;}
    const delta=(g.preview.get(g.index)?.seconds??g.from)-g.from;
    const statusChanged=g.indices.some(i=>g.preview.get(i)?.status!==acceptedReviewPoint(i)?.status);
    if(Math.abs(delta)<1e-7&&!statusChanged){requestRollDraw();return;}
    pushDraftUndo();
    for(const i of g.indices){
      const pv=g.preview.get(i);
      reviewDraft.op.beats[i]={...reviewDraft.op.beats[i],seconds:pv.seconds,snap:false};
      reviewDraft.edited.add(i);
      reviewDraft.manualStatus??=new Map();
      reviewDraft.manualStatus.set(i,pv.status);
      reviewDraft.lastPoints?.set(i,{raw:pv.seconds,snap:false,pt:{seconds:pv.seconds,raw_seconds:pv.seconds,status:pv.status}});
    }
    changeDraft();
  }
  function resolveDraftPencil(event){
    const g=reviewDraft?.pencilClick;
    if(!g)return;
    reviewDraft.pencilClick=null;
    if(event.target!==g.lane||event.pointerId!==undefined&&event.pointerId!==g.pointer||
        event.button!==0||committing||!ensureReviewScope())return;
      const exists=reviewDraft.op.beats.some((b,i)=>inDraft(i)&&
        (Math.abs(b.seconds-g.to)<1e-5||Math.abs(acceptedReviewPoint(i).seconds-g.to)<1e-5));
      if(Number.isFinite(g.to)&&!exists&&draftRegionAt(g.raw)===g.region&&draftRegionAt(g.to)===g.region){
        pushDraftUndo();
        const i=reviewDraft.op.beats.length;
        const st=g.aim?.onset?'midi_onset':(prefs.snapToMidi?'no_nearby_onset':'raw');
        reviewDraft.op.beats.push({seconds:g.to});
        reviewDraft.edited.add(i);
        reviewDraft.manualStatus??=new Map();
        reviewDraft.manualStatus.set(i,st);
        reviewDraft.lastPoints?.set(i,{raw:g.to,snap:false,pt:{seconds:g.to,raw_seconds:g.to,status:st}});
        if(!reduceMotion?.matches)reviewDraft.newFade.set(i,{from:0,to:1,at:performance.now()});
        reviewSelected.clear();reviewSelected.add(i);
        changeDraft();
      }else requestRollDraw();
  }
  function reviewModifier(event) {
    const g=reviewDrag,p=g?.lastEvent;
    if(g&&['move','region-edge','insert'].includes(g.kind)&&p&&['Control','Meta','Shift'].includes(event.key)){
      sampleReview(g,{clientX:p.clientX,clientY:p.clientY,
        ctrlKey:event.key==='Control'?event.type==='keydown':!!event.ctrlKey,
        metaKey:event.key==='Meta'?event.type==='keydown':!!event.metaKey,
        shiftKey:event.key==='Shift'?event.type==='keydown':!!event.shiftKey,altKey:!!event.altKey});
      return;
    }
    if(!g&&phase==='review'&&reviewDraft&&lastReviewHover&&event.key==='Alt'){
      lastReviewHover={...lastReviewHover,altKey:event.type==='keydown'};
      updateReviewHover(lastReviewHover);
      if(event.type==='keyup')event.preventDefault();
    }
  }

  // ---- Keyboard Guard (Window capture phase: bare Tab during Recording + B hold + Review Enter/R/Esc) ----

  function isBlockedTarget(event) {
    const t = event.target;
    if (t?.closest?.(EDITABLES) || t?.isContentEditable) return true;
    if (document.querySelector('dialog[open]:not(.editor-docked),#ncm-dialog[open],[aria-modal="true"]:not([hidden]):not([aria-hidden="true"])')) return true;
    if (t?.closest?.(POPOVERS)) return true;
    return false;
  }

  function ownsSpaceKey(event) {
    const t=event.target;
    return phase!=='idle' && runtimeReady() && tab==='notes' &&
      !t?.isContentEditable && !t?.closest?.(EDITABLES);
  }

  function ownsTabKey() {
    return (phase==='recording'||phase==='preparing') && runtimeReady() && tab==='notes';
  }

  function installListeners() {
    if (typeof window === 'undefined' || typeof document === 'undefined') return;

    // Window capture precedes the normal pulse editor's document capture.
    window.addEventListener('pointerdown',reviewDown,true);
    window.addEventListener('pointermove',reviewMove,true);
    document.addEventListener('pointerleave',event=>{
      if(event.target?.id==='beat-flags-canvas'){
        lastReviewHover=null;
        if(reviewDraft?.hover&&!reviewDrag){reviewDraft.hover=null;requestRollDraw();requestOverlayRepaint();}
      }
    },true);
    window.addEventListener('keydown',reviewModifier,true);
    window.addEventListener('keyup',reviewModifier,true);
    for(const name of ['pointerup','pointercancel','lostpointercapture'])
      window.addEventListener(name,reviewUp,true);
    for(const name of ['click','dblclick','contextmenu']) window.addEventListener(name,event=>{
      if(reviewPointer(event)?.covered){
        consumeReviewPointer(event);
        if(name==='click')resolveDraftPencil(event);
      }
    },true);

    window.addEventListener('keydown', event => {
      if (swallowDeleteKey) {
        const sameDeleteKey = event.key === swallowDeleteKey.key || (swallowDeleteKey.code && event.code === swallowDeleteKey.code);
        if (sameDeleteKey && event.repeat) {
          event.preventDefault();
          event.stopImmediatePropagation();
          return;
        }
        if (!event.repeat) swallowDeleteKey = null;
      }
      if (phase === 'idle' || !runtimeReady() || tab !== 'notes') return;
      if (event.isComposing || event.keyCode === 229 || event.getModifierState?.('AltGraph')) return;

      // Intercept Undo/Redo while beat recording/review is active:
      // In review, steps through local draft history; in other active phases, prevents mutating the project underneath.
      const shortcut = window.AstraEditorShortcuts?.resolve?.(event);
      const lowKey = (event.key || '').toLowerCase();
      const isUndo = shortcut === 'editorUndo' ||
        (!shortcut && (event.ctrlKey || event.metaKey) && !event.altKey && !event.shiftKey && (lowKey === 'z' || event.code === 'KeyZ'));
      const isRedo = shortcut === 'editorRedo' ||
        (!shortcut && (event.ctrlKey || event.metaKey) && !event.altKey && (
          (event.shiftKey && (lowKey === 'z' || event.code === 'KeyZ')) ||
          (!event.shiftKey && (lowKey === 'y' || event.code === 'KeyY'))
        ));
      if ((isUndo || isRedo) && !isBlockedTarget(event)) {
        // Top of the take history is a project edit: let the editor's own
        // undo path run (it asks history() and then sends undo/redo project).
        if (phase === 'review' && !reviewDrag && (isUndo ? reviewDraft?.undoStack : reviewDraft?.redoStack)?.at(-1)?.project) return;
        event.preventDefault();
        event.stopImmediatePropagation();
        if (!event.repeat && phase === 'review') draftHistory(isUndo ? 'undo' : 'redo');
        return;
      }

      // Track B hold for single-beat free override (recording AND review drag snap bypass!)
      if ((event.code === 'KeyB' || event.key === 'b' || event.key === 'B') && !event.ctrlKey && !event.altKey && !event.metaKey) {
        if ((phase === 'recording' || phase === 'review') && !isBlockedTarget(event)) {
          event.stopImmediatePropagation();
          if (!bHeld) {
            bHeld = true;
            if (phase === 'recording') syncUI();
            if (phase === 'review' && reviewDrag?.lastEvent && ['move', 'insert'].includes(reviewDrag.kind)) {
              sampleReview(reviewDrag, reviewDrag.lastEvent);
            }
          }
        }
        return;
      }

      // Bare Tab = Tap Beat during active Recording (immune to Chinese IME!)
      if (event.key === 'Tab' && (phase==='recording'||phase==='preparing')) {
        if (event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return;
        if (isBlockedTarget(event)) return;
        event.preventDefault();
        event.stopImmediatePropagation();
        if (!event.repeat && phase==='recording') recordTap(event);
        return;
      }

      if (event.key === 'Escape' && !event.ctrlKey && !event.altKey && !event.metaKey) {
        if (isBlockedTarget(event)) return;
        event.preventDefault(); event.stopImmediatePropagation();
        cancel();
        return;
      }

      const isDelete = shortcut === 'editorDelete' ||
        (!event.ctrlKey && !event.altKey && !event.metaKey && (event.key === 'Delete' || event.key === 'Backspace'));
      if(phase==='review' && (reviewSelected.size||reviewDraft?.selectedRegion) && isDelete && !isBlockedTarget(event)){
        event.preventDefault();event.stopImmediatePropagation();
        swallowDeleteKey = { key: event.key, code: event.code };
        if(!event.repeat)deleteDraftSelection();
        return;
      }

      if (phase === 'review' && !event.ctrlKey && !event.altKey && !event.metaKey && !isBlockedTarget(event)) {
        if (event.key === 'Enter') {
          event.preventDefault(); event.stopImmediatePropagation();
          if(!event.repeat)commit();
          return;
        }
        if (!event.repeat && (event.code === 'KeyR' || event.key === 'r' || event.key === 'R')) {
          event.preventDefault(); event.stopImmediatePropagation();
          retry();
          return;
        }
      }
    }, true);

    window.addEventListener('keyup', event => {
      if (swallowDeleteKey && (event.key === swallowDeleteKey.key || (swallowDeleteKey.code && event.code === swallowDeleteKey.code))) {
        swallowDeleteKey = null;
      }
      if ((event.code === 'KeyB' || event.key === 'b' || event.key === 'B') && bHeld) {
        bHeld = false;
        if (phase === 'recording') syncUI();
        if (phase === 'review' && reviewDrag?.lastEvent && ['move', 'insert'].includes(reviewDrag.kind)) {
          sampleReview(reviewDrag, reviewDrag.lastEvent);
        }
      }
    }, true);

    window.addEventListener('blur', () => {
      swallowDeleteKey = null;
      if (bHeld) { bHeld = false; if (phase === 'recording') syncUI(); }
      lastReviewHover = null;
      if (reviewDraft?.hover) {
        reviewDraft.hover = null;
        const laneEl = $('beat-flags-canvas');
        if (laneEl?.style && !reviewDrag) laneEl.style.cursor = '';
        requestRollDraw();
        requestOverlayRepaint();
      }
    });

    // Pointerdown for Tap Pad (zero release latency!)
    document.addEventListener('pointerdown', event => {
      if (event.target?.closest?.('#beat-rec-tap-pad')) {
        event.preventDefault();
        recordTap(event);
      }
    }, true);

    // Repaint ruler crimson preview immediately when toggling the top 录制 menu
    document.addEventListener('toggle', event => {
      if (event.target?.matches?.('.beat-rec-menu')) {
        syncUI();
        requestRollDraw();
      }
    }, true);

    document.addEventListener('change', event => {
      const cb = event.target?.closest?.('[data-beat-rec-pref]');
      if (cb) {
        const k = cb.dataset.beatRecPref;
        if (k in prefs) {
          if (phase === 'review' && reviewDraft && k === 'snapToMidi') pushDraftUndo();
          prefs[k] = Boolean(cb.checked);
          savePrefs();
          if (phase === 'review' && (k === 'snapToMidi' || k === 'lrPunch')) runPreview();
          else syncUI();
          requestRollDraw();
        }
        return;
      }
      if (event.target?.id === 'beat-rec-offset-ms') {
        const v = Number(event.target.value);
        if (Number.isFinite(v)) {
          prefs.offsetMs = Math.max(-150, Math.min(150, Math.round(v)));
          savePrefs();
          event.target.value = String(prefs.offsetMs);
          const offSum = $('beat-rec-offset-summary');
          if (offSum) offSum.textContent = String(prefs.offsetMs);
        }
      }
    });

    document.addEventListener('click', event => {
      const pr = event.target?.closest?.('[data-beat-rec-preroll]');
      if (pr) {
        prefs.preRollBars = Number(pr.dataset.beatRecPreroll) || 0;
        savePrefs();
        syncUI();
        return;
      }
      const mr = event.target?.closest?.('[data-beat-rec-meter]');
      if (mr) {
        if (phase === 'review' && reviewDraft && mr.dataset.beatRecMeter !== prefs.meterResolution) pushDraftUndo();
        prefs.meterResolution = mr.dataset.beatRecMeter;
        savePrefs();
        if (phase === 'review') runPreview();
        else syncUI();
      }
    });
  }

  function sync() {
    if(committing){syncUI();return;}
    if (phase==='review' && !committing && !reviewScopeCurrent() && !rebaseReview()) {
      abort('项目已修改，录拍预览未应用');return;
    }
    if (phase !== 'idle' && !canRecord()) {
      abort('');
      return;
    }
    syncUI();
  }

  // The grid is drawn by the ordinary meter/beat logic from ONE tempo map:
  // the backend preview of the take (whole timeline, as Enter would write it).
  // `regions` are only the stretches the preview has not reached yet; they are
  // left blank instead of mixing old lines with tap anchors.
  const spanKey = rows => rows.map(r => `${r.start.toFixed(5)}-${r.end.toFixed(5)}`).join(',');
  function previewGridMask() {
    if (phase === 'recording') {
      const span = activeOverwriteSpan();
      if (!span && !recGridTempo) return null;
      const blank = [];
      if (span) {
        if (!recGridTempo || !recGridSpan) blank.push({ start: span.start, end: span.end });
        else {
          if (span.start < recGridSpan.start - 1e-6) blank.push({ start: span.start, end: recGridSpan.start });
          if (span.end > recGridSpan.end + 1e-6) blank.push({ start: recGridSpan.end, end: span.end });
        }
      }
      const regions = blank.map(r => ({ ...r, includeStart: true, includeEnd: true }));
      return { mode: 'recording', regions, tempo: recGridTempo, version: `r${recMaskVersion}:${spanKey(regions)}` };
    }
    if (phase === 'review' && reviewDraft) {
      const groups = displayDraftGroups();
      if (!groups.length && !reviewGridTempo) return null;
      const regions = reviewGridTempo ? [] : groups;
      return { mode: 'review', regions, tempo: reviewGridTempo, version: `v${reviewMaskVersion}:${spanKey(regions)}` };
    }
    return null;
  }

  installListeners();

  return {
    phase: () => phase,
    active: () => phase !== 'idle',
    // While reviewing a take the rest of the project stays editable.
    locksRhythm: () => phase !== 'idle' && (phase !== 'review' || committing),
    ownsPointer: event => Boolean(reviewPointer(event)?.covered),
    blocksProjectSave: () => phase !== 'idle' || committing,
    recording: () => phase === 'recording',
    activeBeatAim,
    wantsOverlay,
    hasHistorySession: () => phase !== 'idle',
    canHistory: dir => phase === 'review' && Boolean(reviewDraft) && !committing && (dir === 'undo' ? reviewDraft.undoStack.length > 0 : reviewDraft.redoStack.length > 0),
    history: dir => draftHistory(dir),
    gridTempo: () => phase === 'recording' ? recGridTempo : phase === 'review' ? reviewGridTempo : null,
    blocksManualSeek: () => ['armed','preparing','recording'].includes(phase) && !internalSeekDepth,
    ownsTabKey,
    ownsSpaceKey,
    prefs: () => ({ ...prefs }),
    commandMenuMarkup,
    transportMarkup,
    hudMarkup,
    action,
    sync,
    syncUI,
    toggle,
    arm,
    cancel,
    retry,
    commit,
    recordTap,
    onPlayback,
    onTransportAction,
    beforeTransportPause,
    isDimmedFlag,
    flagOpacity,
    previewGridMask,
    previewGridRegions:()=>phase==='review'&&!reviewGridTempo?reviewGridRegions:null,
    previewGridTempo: () => phase==='review'?reviewGridTempo:null,
    drawRuler,
    drawBeatLane,
    paintOverlay,
    // Expose pure helpers for unit testing
    _test: { buildOperation, resolveEffectivePass, getOnsetClusters, liveSnapPoint, hasValidOnsetAt, snapReviewOnset, flagInk, acceptedReviewPoint, reviewPoint, getReviewDraft: () => reviewDraft, ingestClock, setPrefs: p => Object.assign(prefs, p),
      // Seed a finished take (as finishRecording would) for headless review tests.
      seedReview: (op) => {
        sessionScope = currentDocScope(); phase = 'review';
        reviewDraft = { op, edited:new Set(), manualStatus:new Map(), deleted:new Set(), selectedRegion:null,
          regions:[{start:op.start,end:op.end,includeStart:true,includeEnd:true}], oldFade:new Map(), newFade:new Map(),
          lastPoints:new Map(), undoStack:[], redoStack:[], ghostStrips:[], cutAnims:new Map(), lastPreviewSig:'', free:new Set() };
        runPreview();
      } },
  };
})();
