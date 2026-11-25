"""Multi-tier symbolic alignment & natural-system excerpt planner for read-only right compare drawer.

Layer 0: Exact provenance fast-path (same clock, no structural right edits).
Layer 1: Full-song Gotoh affine-gap semi-global alignment (with local (1,2)/(2,1)
         tempo-stretch inside the match state) + monotonic bracketed refinement.
         Separates genuinely matched passages (M state) from deleted sections
         (U vertical gap state), so deleting one of two identical choruses never
         mis-maps the deleted chorus onto the surviving chorus.
Layer 2: Guaranteed non-empty fallback ('approximate' / 大致位置) showing the
         nearest valid excerpt with zero fake highlight.
"""
from bisect import bisect_left, bisect_right
from collections import OrderedDict
from hashlib import sha256
from io import BytesIO
import math
import threading

import mido
import numpy as np


_CACHE_LOCK = threading.Lock()
_ALIGN_CACHE = OrderedDict()
_ALIGN_CACHE_MAX = 8
_MAX_ALIGNMENT_CELLS = 4_000_000
_MAX_FRAMES = 3600

class _AlignmentBudget(ValueError):
    pass

class _AlignmentCancelled(ValueError):
    pass


def parse_right_midi(raw_bytes):
    """Parse in-memory SMF bytes from AstraNativeScore playbackSnapshot.

    Returns (notes, tick_to_sec, sec_to_tick, duration_sec) where each note is
    (pitch, start_sec, end_sec, start_tick, end_tick).
    """
    mid = mido.MidiFile(file=BytesIO(raw_bytes))
    ppq = max(1, int(mid.ticks_per_beat or 480))
    events = []
    for tr in mid.tracks:
        tick = 0
        for msg in tr:
            tick += int(msg.time)
            if msg.type in ('set_tempo', 'note_on', 'note_off'):
                events.append((tick, 0 if msg.type == 'set_tempo' else 1, msg))
    events.sort(key=lambda x: (x[0], x[1]))

    tempo_points = [(0, 0.0, 500000)]  # (tick, sec, us_per_quarter)
    cur_tick = 0
    cur_sec = 0.0
    cur_tempo = 500000
    active = {}
    notes = []

    for tick, _, msg in events:
        if tick > cur_tick:
            cur_sec += (tick - cur_tick) * (cur_tempo / 1_000_000.0) / ppq
            cur_tick = tick
        if msg.type == 'set_tempo':
            cur_tempo = max(1, int(msg.tempo))
            if tempo_points and tempo_points[-1][0] == tick:
                tempo_points[-1] = (tick, cur_sec, cur_tempo)
            else:
                tempo_points.append((tick, cur_sec, cur_tempo))
        elif msg.type == 'note_on' and msg.velocity > 0:
            key = (getattr(msg, 'channel', 0), int(msg.note))
            active.setdefault(key, []).append((cur_sec, tick))
        elif msg.type == 'note_off' or (msg.type == 'note_on' and msg.velocity == 0):
            key = (getattr(msg, 'channel', 0), int(msg.note))
            stack = active.get(key)
            if stack:
                s_sec, s_tick = stack.pop(0)
                if cur_sec > s_sec + 1e-4:
                    notes.append((int(msg.note), s_sec, cur_sec, s_tick, tick))
    for (_, pitch), stack in active.items():
        for s_sec, s_tick in stack:
            end_s = max(cur_sec, s_sec + 0.1)
            notes.append((pitch, s_sec, end_s, s_tick, max(cur_tick, s_tick + ppq // 4)))
    notes.sort(key=lambda x: (x[1], x[0]))

    t_ticks = [p[0] for p in tempo_points]
    t_secs = [p[1] for p in tempo_points]

    def tick_to_sec(tick_val):
        idx = max(0, bisect_right(t_ticks, tick_val) - 1)
        t0, s0, us = tempo_points[idx]
        return s0 + (tick_val - t0) * (us / 1_000_000.0) / ppq

    def sec_to_tick(sec_val):
        sec_val = max(0.0, float(sec_val))
        idx = max(0, bisect_right(t_secs, sec_val) - 1)
        t0, s0, us = tempo_points[idx]
        return max(0, int(round(t0 + (sec_val - s0) * ppq / (us / 1_000_000.0))))

    dur = max((n[2] for n in notes), default=max(cur_sec, 0.1))
    return notes, tick_to_sec, sec_to_tick, dur, ppq


def _build_frames(notes, hop=0.30):
    """36-dim L2-normalized polyphonic chroma + onset + octave-register frames."""
    if not notes:
        return np.zeros((1, 36), dtype=np.float32), np.array([0.0], dtype=np.float64), np.array([False])
    t_max = max(float(n[2]) for n in notes)
    n_frames = max(1, int(math.ceil(t_max / hop)))
    feat = np.zeros((n_frames, 36), dtype=np.float32)
    times = np.arange(n_frames, dtype=np.float64) * hop
    for row in notes:
        pitch, s, e = int(row[0]), float(row[1]), float(row[2])
        if e <= s + 1e-4:
            continue
        i0 = max(0, int(s / hop))
        i1 = min(n_frames, max(i0 + 1, int(math.ceil(e / hop))))
        pc = pitch % 12
        oct_bin = min(11, max(0, pitch // 12))
        feat[i0:i1, pc] += 1.0
        feat[i0, 12 + pc] += 2.0
        feat[i0:i1, 24 + oct_bin] += 0.6
    norms = np.linalg.norm(feat, axis=1, keepdims=True)
    active = (norms[:, 0] > 1e-6)
    feat = np.divide(feat, np.maximum(norms, 1e-6))
    return feat, times, active


def gotoh_semiglobal(A, B, *, threshold=0.38, gap_open=1.25, gap_ext=0.04, warp_pen=0.07, cancelled=None):
    """Gotoh affine-gap semi-global alignment with local (1,2)/(2,1) tempo stretch.

    States:
      0 (M): matched frames (allows (1,1), (2,1), (1,2) tempo stretch up to 2x!)
      1 (U): vertical gap (Left frames deleted from Right)
      2 (L): horizontal gap (Right frames inserted vs Left)

    Free start and end gaps absorb cut/added preludes and outros cleanly.
    """
    N, M = len(A), len(B)
    if N == 0 or M == 0:
        return np.full(N, -1, dtype=np.int32), np.zeros(N, dtype=np.float32), np.zeros(N, dtype=np.int32), 0.0
    if N*M>_MAX_ALIGNMENT_CELLS or max(N,M)>_MAX_FRAMES:
        raise _AlignmentBudget('对齐计算超出上限。')
    if cancelled and cancelled():
        raise _AlignmentCancelled()
    raw_sim = (A @ B.T).astype(np.float32)
    sim = raw_sim - np.float32(threshold)
    NEG = np.float32(-1e9)

    M_mat = np.full((N + 1, M + 1), NEG, dtype=np.float32)
    U_mat = np.full((N + 1, M + 1), NEG, dtype=np.float32)
    L_mat = np.full((N + 1, M + 1), NEG, dtype=np.float32)

    # Pointers for M: 0=M(1,1), 1=U(1,1), 2=L(1,1), 4=M(2,1), 5=M(1,2), 3=start
    PM = np.full((N + 1, M + 1), 3, dtype=np.int8)
    PU = np.full((N + 1, M + 1), 3, dtype=np.int8)
    PL = np.full((N + 1, M + 1), 3, dtype=np.int8)

    M_mat[0, :] = 0.0
    M_mat[:, 0] = 0.0

    idx_ext = np.arange(M + 1, dtype=np.float32) * np.float32(gap_ext)
    open_plus_ext = np.float32(gap_open + gap_ext)
    gap_open_f = np.float32(gap_open)
    gap_ext_f = np.float32(gap_ext)
    warp_f = np.float32(warp_pen)

    for i in range(1, N + 1):
        if not i%16 and cancelled and cancelled():
            raise _AlignmentCancelled()
        s_row = sim[i - 1]
        # 1) U[i, j]: Left deleted in Right
        u_open = M_mat[i - 1] - open_plus_ext
        u_cont = U_mat[i - 1] - gap_ext_f
        u_from_m = u_open >= u_cont
        U_mat[i] = np.where(u_from_m, u_open, u_cont)
        PU[i] = np.where(u_from_m, 0, 1).astype(np.int8)

        # 2) M[i, 1:]: match from (i-1, j-1) in {M, U, L}
        best_prev = M_mat[i - 1, :-1].copy()
        p_m = np.zeros(M, dtype=np.int8)
        m_u = U_mat[i - 1, :-1] > best_prev
        best_prev = np.where(m_u, U_mat[i - 1, :-1], best_prev)
        p_m = np.where(m_u, 1, p_m)
        m_l = L_mat[i - 1, :-1] > best_prev
        best_prev = np.where(m_l, L_mat[i - 1, :-1], best_prev)
        p_m = np.where(m_l, 2, p_m)
        best_m = best_prev + s_row

        # Local tempo compress (2,1): 2 Left frames -> 1 Right frame (from M[i-2, j-1])
        if i >= 2:
            v21 = M_mat[i - 2, :-1] + (s_row + sim[i - 2]) * np.float32(0.5) - warp_f
            m21 = v21 > best_m
            best_m = np.where(m21, v21, best_m)
            p_m = np.where(m21, 4, p_m)

        # Local tempo stretch (1,2): 1 Left frame -> 2 Right frames (from M[i-1, j-2])
        if M >= 2:
            v12 = M_mat[i - 1, :-2] + (s_row[1:] + s_row[:-1]) * np.float32(0.5) - warp_f
            m12 = v12 > best_m[1:]
            best_m[1:] = np.where(m12, v12, best_m[1:])
            p_m[1:] = np.where(m12, 5, p_m[1:])

        M_mat[i, 1:] = best_m
        PM[i, 1:] = p_m

        # 3) L[i, j]: Right inserted vs Left, O(M) prefix-max trick
        cand = M_mat[i] - gap_open_f + idx_ext
        pref = np.maximum.accumulate(cand[:-1])
        L_mat[i, 1:] = pref - idx_ext[1:]
        from_m_l = (M_mat[i, :-1] - open_plus_ext) >= (L_mat[i, 1:] - np.float32(1e-5))
        PL[i, 1:] = np.where(from_m_l, 0, 2).astype(np.int8)

    # Pick optimal free-end cell on last row or last column
    best_score = -1e18
    end_i, end_j, end_state = N, M, 0
    for st, mat in ((0, M_mat), (1, U_mat), (2, L_mat)):
        j_b = int(np.argmax(mat[N, :]))
        if float(mat[N, j_b]) > best_score:
            best_score = float(mat[N, j_b])
            end_i, end_j, end_state = N, j_b, st
        i_b = int(np.argmax(mat[:, M]))
        if float(mat[i_b, M]) > best_score:
            best_score = float(mat[i_b, M])
            end_i, end_j, end_state = i_b, M, st

    matched_j = np.full(N, -1, dtype=np.int32)
    matched_sim = np.zeros(N, dtype=np.float32)
    near_j = np.full(N, -1, dtype=np.int32)

    i, j, st = end_i, end_j, end_state
    while i > 0 and j > 0:
        if st == 0:
            code = int(PM[i, j])
            if code == 3:
                break
            matched_j[i - 1] = j - 1
            matched_sim[i - 1] = float(raw_sim[i - 1, j - 1])
            near_j[i - 1] = j - 1
            if code == 4:
                if i >= 2:
                    matched_j[i - 2] = j - 1
                    matched_sim[i - 2] = float(raw_sim[i - 2, j - 1])
                    near_j[i - 2] = j - 1
                i -= 2
                j -= 1
                st = 0
            elif code == 5:
                i -= 1
                j -= 2
                st = 0
            else:
                i -= 1
                j -= 1
                st = code
        elif st == 1:
            code = int(PU[i, j])
            near_j[i - 1] = max(0, min(M - 1, j - 1))
            i -= 1
            st = code if code in (0, 1, 2) else 3
            if st == 3:
                break
        elif st == 2:
            code = int(PL[i, j])
            j -= 1
            st = code if code in (0, 1, 2) else 3
            if st == 3:
                break
        else:
            break

    # Fill leading/trailing unaligned boundaries with nearest valid edge
    for k in range(0, i):
        near_j[k] = max(0, min(M - 1, j))
    for k in range(end_i, N):
        near_j[k] = max(0, min(M - 1, max(0, end_j - 1)))

    return matched_j, matched_sim, near_j, best_score


def _cached_global_alignment(cache_key, left_notes, right_midi_bytes, hop=0.30, *, cancelled=None):
    with _CACHE_LOCK:
        hit = _ALIGN_CACHE.get(cache_key)
        if hit is not None:
            _ALIGN_CACHE.move_to_end(cache_key)
            return hit
    if cancelled and cancelled():
        raise _AlignmentCancelled()
    right_notes, tick_to_sec, sec_to_tick, right_dur, ppq = parse_right_midi(right_midi_bytes)
    a=max((float(n[2]) for n in left_notes),default=0.0)
    b=max((float(n[2]) for n in right_notes),default=0.0)
    if not math.isfinite(a) or not math.isfinite(b) or not a or not b:
        raise _AlignmentBudget('没有足够可对照的音符。')
    ratio=max(1.0,a/(hop*(_MAX_FRAMES-1)),b/(hop*(_MAX_FRAMES-1)))
    # At most 4M DP cells (~90 MB before scratch) and one background job.
    # Do not trade away too much timing resolution on very long works.
    ratio=max(ratio,(a*b/(_MAX_ALIGNMENT_CELLS-2*_MAX_FRAMES))**0.5/hop)
    hop*=ratio*1.0005 if ratio>1 else 1.0
    if hop>0.72:
        raise _AlignmentBudget('工程较长，无法可靠匹配。')
    F_L, T_L, active_L = _build_frames(left_notes, hop=hop)
    F_R, T_R, active_R = _build_frames(right_notes, hop=hop)
    matched_j, matched_sim, near_j, total_score = gotoh_semiglobal(F_L, F_R,cancelled=cancelled)
    matched_indices = np.flatnonzero(matched_j >= 0)
    first_anchor_L = float(T_L[int(matched_indices[0])]) if len(matched_indices) else None
    last_anchor_L = float(T_L[int(matched_indices[-1])]) if len(matched_indices) else None
    entry = {
        'hop': hop,
        'T_L': T_L,
        'T_R': T_R,
        'active_L': active_L,
        'matched_j': matched_j,
        'matched_sim': matched_sim,
        'near_j': near_j,
        'total_score': total_score,
        'first_anchor_L': first_anchor_L,
        'last_anchor_L': last_anchor_L,
        'right_notes': right_notes,
        'tick_to_sec': tick_to_sec,
        'sec_to_tick': sec_to_tick,
        'right_dur': right_dur,
        'ppq': ppq,
    }
    if cancelled and cancelled():
        raise _AlignmentCancelled()
    with _CACHE_LOCK:
        _ALIGN_CACHE[cache_key] = entry
        _ALIGN_CACHE.move_to_end(cache_key)
        while len(_ALIGN_CACHE) > _ALIGN_CACHE_MAX:
            _ALIGN_CACHE.popitem(last=False)
    return entry


def _has_written_playback_identity(bar_map):
    # Native bars carry played/unrolled MIDI ticks, whereas cursor rectangles
    # use written ticks. Only a forward identity map may label blue matches.
    prior=-1
    for row in bar_map.get('bars') or ():
        if (not isinstance(row,(tuple,list)) or not row or
                type(row[0]) is not int or row[0]<=prior):
            return False
        prior=row[0]
    previous_end=None
    for row in bar_map.get('spans') or ():
        if (not isinstance(row,(tuple,list)) or len(row)!=3 or
                not all(type(x) is int for x in row)):
            return False
        written,end,played=row
        if (written!=played or written<0 or end<=written or
                (previous_end is not None and previous_end!=written)):
            return False
        previous_end=end
    for row in bar_map.get('pauses') or ():
        if (not isinstance(row,(tuple,list)) or len(row)!=2 or
                not all(type(x) is int for x in row) or row[1]!=0):
            return False
    return True

def _bar_rows_from_map(bar_map):
    """Normalize playbackBarMap['bars'] into unique written bar entries [(idx, start_tick, end_tick, page, y)]."""
    raw_bars = (bar_map or {}).get('bars') or []
    end_tick = int((bar_map or {}).get('endMidiTick') or 0)
    if not raw_bars:
        return []
    # Deduplicate repeats by taking first occurrence of each source_ordinal in bar order
    seen = set()
    rows = []
    for k, row in enumerate(raw_bars):
        ordinal = int(row[0])
        if ordinal in seen:
            continue
        seen.add(ordinal)
        s_tick = int(row[1])
        e_tick = int(raw_bars[k + 1][1]) if k + 1 < len(raw_bars) else max(s_tick + 480, end_tick)
        page = int(row[2]) if len(row) >= 4 else 1
        y = int(row[3]) if len(row) >= 4 else 200
        rows.append((ordinal, s_tick, max(s_tick + 1, e_tick), max(1, page), y))
    rows.sort(key=lambda r: r[1])
    return rows


def _ticks_to_bar_range(bar_rows, start_tick, end_tick):
    """Snap [start_tick, end_tick] to inclusive 0-based indices into bar_rows."""
    if not bar_rows:
        return 0, 0
    matched = [i for i, (_, s, e, _, _) in enumerate(bar_rows) if e > start_tick + 30 and s < end_tick - 30]
    if matched:
        return matched[0], matched[-1]
    starts = [r[1] for r in bar_rows]
    idx = max(0, min(len(bar_rows) - 1, bisect_right(starts, start_tick) - 1))
    return idx, idx


def _group_page_systems(bar_rows):
    """Return {page: [(sys_y, [bar_idx, ...]), ...]} sorted top-to-bottom per page."""
    by_page = {}
    for idx, (_, _, _, page, y) in enumerate(bar_rows):
        by_page.setdefault(page, []).append((y, idx))
    result = {}
    for page, items in by_page.items():
        items.sort(key=lambda t: (t[0], t[1]))
        systems = []
        for y, idx in items:
            if systems and abs(y - systems[-1][0]) <= 18:
                systems[-1][1].append(idx)
            else:
                systems.append([y, [idx]])
        result[page] = [(sys_y, bars) for sys_y, bars in systems]
    return result


def build_excerpt_plan(bar_map, *, mode, label, detail, core_bar_indices=None, view_bar_indices=None,
                       context_bars=2, page_heights=None):
    """Build natural-system cropped strip plan covering core_bars ± context_bars.

    Rounds cleanly to complete natural systems so ties/slurs/beams are never
    sliced mid-system, without bringing in unrelated systems of the full page.
    """
    bar_rows = _bar_rows_from_map(bar_map)
    if not bar_rows:
        return {
            'mode': mode, 'label': label, 'detail': detail,
            'coreBars': None, 'contextBars': None,
            'coreStartTick': None, 'coreEndTick': None, 'strips': [],
        }
    total = len(bar_rows)
    if core_bar_indices is not None:
        m0 = max(0, min(total - 1, int(core_bar_indices[0])))
        m1 = max(m0, min(total - 1, int(core_bar_indices[1])))
        c0 = max(0, m0 - context_bars)
        c1 = min(total - 1, m1 + context_bars)
        core_bars_1based = [bar_rows[m0][0] + 1, bar_rows[m1][0] + 1]
        core_start_tick = int(bar_rows[m0][1])
        core_end_tick = int(bar_rows[m1][2])
    else:
        v0, v1 = view_bar_indices if view_bar_indices is not None else (0, min(3, total - 1))
        c0 = max(0, min(total - 1, int(v0)))
        c1 = max(c0, min(total - 1, int(v1)))
        core_bars_1based = None
        core_start_tick = None
        core_end_tick = None

    page_systems = _group_page_systems(bar_rows)
    strips = []
    for page in sorted(page_systems):
        systems = page_systems[page]
        picked = [si for si, (_, idxs) in enumerate(systems) if any(c0 <= idx <= c1 for idx in idxs)]
        if not picked:
            continue
        sa, sb = picked[0], picked[-1]
        ys = [s[0] for s in systems]
        lh = int((page_heights or {}).get(page) or 1358)
        # Midpoint-between-systems vertical crop bounds: never clip high notes,
        # measure numbers, dynamics, or pedal marks, and never leak neighbor systems.
        if sa == 0:
            crop_top = max(24, ys[0] - 76)
            safe_top = 0
        else:
            gap = ys[sa] - ys[sa - 1]
            sep_above = ys[sa - 1] + int(round(gap * 0.78))
            crop_top = max(ys[sa - 1] + 158, min(ys[sa] - 44, sep_above))
            safe_top = crop_top
        if sb == len(systems) - 1:
            crop_bottom = min(lh - 14, ys[sb] + 188)
            safe_bottom = lh
        else:
            gap = ys[sb + 1] - ys[sb]
            sep_below = ys[sb] + int(round(gap * 0.76))
            crop_bottom = min(ys[sb + 1] - 42, max(ys[sb] + 154, sep_below))
            safe_bottom = crop_bottom
        if crop_bottom <= crop_top + 100:
            crop_bottom = min(lh - 14, crop_top + 220)
        strips.append({
            'page': int(page),
            'cropTop': int(max(0, crop_top)),
            'cropBottom': int(min(lh, crop_bottom)),
            'safeTop': int(max(0, safe_top)),
            'safeBottom': int(min(lh, safe_bottom)),
            'logicalHeight': lh,
            'systemYs': [int(ys[k]) for k in range(sa, sb + 1)],
        })

    return {
        'mode': mode,
        'label': label,
        'detail': detail,
        'coreBars': core_bars_1based,
        'contextBars': [bar_rows[c0][0] + 1, bar_rows[c1][0] + 1],
        'coreStartTick': core_start_tick,
        'coreEndTick': core_end_tick,
        'strips': strips,
    }


def provisional_excerpt_plan(bar_map, *, start_seconds, end_seconds, left_duration,
                             origin_info=None, page_heights=None):
    """Instant 0ms reference excerpt shown while symbolic alignment runs."""
    bar_rows = _bar_rows_from_map(bar_map)
    total = len(bar_rows)
    if not total:
        return build_excerpt_plan(bar_map, mode='provisional', label='正在精确定位…', detail='')
    # Prefer rough proportional position in song so user immediately sees nearby music
    dur = max(float(left_duration or 0.0), float(end_seconds or 0.0), 1.0)
    opening_sec = float((origin_info or {}).get('opening_seconds') or 0.0)
    eff_s = max(0.0, float(start_seconds) - opening_sec)
    eff_e = max(eff_s + 0.5, float(end_seconds) - opening_sec)
    eff_dur = max(1.0, dur - opening_sec)
    frac_s = max(0.0, min(1.0, eff_s / eff_dur))
    frac_e = max(frac_s, min(1.0, eff_e / eff_dur))
    v0 = max(0, min(total - 1, int(round(frac_s * (total - 1))) - 1))
    v1 = max(v0, min(total - 1, int(round(frac_e * (total - 1))) + 1))
    if v1 - v0 < 3:
        v1 = min(total - 1, v0 + 3)
    return build_excerpt_plan(
        bar_map,
        mode='provisional',
        label='正在定位…',
        detail='先显示附近参考谱段，精确定位完成后自动更新。',
        core_bar_indices=None,
        view_bar_indices=(v0, v1),
        page_heights=page_heights,
    )


def _duplicate_onsets(notes,start,end):
    """Reject indistinguishable repeated left phrases when right provenance is absent."""
    if end-start<1.2:
        return False
    ordered=sorted((float(n[1]),int(n[0])) for n in notes)
    times=[x[0] for x in ordered]
    lo,hi=bisect_left(times,start-.035),bisect_left(times,end-.035)
    sample=ordered[lo:hi]
    if len(sample)<5:
        return False
    anchor,pitch=sample[0]
    pattern=[(t-anchor,p) for t,p in sample]
    checked=0
    for offset,(other,op) in enumerate(ordered):
        if op!=pitch or abs(other-anchor)<(end-start)*.75:
            continue
        if offset+len(sample)>len(ordered):
            break
        measured=ordered[offset:offset+len(sample)]
        if (abs((measured[-1][0]-other)-pattern[-1][0])>.075
                or measured[-1][1]!=pattern[-1][1]):
            continue
        checked+=1
        if checked>96 or all(p==rp and abs((t-other)-dt)<=.065
                             for (t,p),(dt,rp) in zip(measured,pattern)):
            return True
    return False

def _same_timeline(left_notes, right_notes, start, end, *, search=3.0, tolerance=.06):
    """Both sides follow the same audio: find the constant time offset.

    Returns (offset_seconds, coverage) for the selected left notes, or None.
    The original score's playback uses the project's reference tempo, so its
    note times differ from the audio only by the silent lead-in it omits.
    """
    window = [(int(n[0]), float(n[1])) for n in left_notes if start - 1e-6 <= float(n[1]) < end]
    if len(window) < 4:
        return None
    by_pitch = {}
    for n in right_notes:
        by_pitch.setdefault(int(n[0]), []).append(float(n[1]))
    for values in by_pitch.values():
        values.sort()
    diffs = []
    for pitch, t in window:
        values = by_pitch.get(pitch, [])
        k = bisect_left(values, t - search)
        while k < len(values) and values[k] <= t + search:
            diffs.append(values[k] - t); k += 1
    if not diffs:
        return None
    diffs.sort()
    best, support = 0., -1
    lo = 0
    for hi, value in enumerate(diffs):          # densest 60 ms bucket
        while diffs[lo] < value - 2 * tolerance:
            lo += 1
        if hi - lo > support:
            support, best = hi - lo, diffs[(lo + hi) // 2]
    offset = float(np.median([d for d in diffs if abs(d - best) <= tolerance]))
    matched = 0
    for pitch, t in window:
        values = by_pitch.get(pitch, [])
        k = bisect_left(values, t + offset - tolerance)
        matched += k < len(values) and values[k] <= t + offset + tolerance
    return offset, matched / len(window)


def _format_bar_span(b0_1based, b1_1based):
    return f'第 {b0_1based} 小节' if b0_1based == b1_1based else f'第 {b0_1based}–{b1_1based} 小节'


def align_compare_excerpt(*, project, left_revision, right_revision, left_notes, left_duration,
                          right_midi_bytes, bar_map, origin_info, structural_edited,
                          start_seconds, end_seconds, start_beat, end_beat, clock_digest,
                          page_heights=None,cancelled=None):
    """Run the 3-layer alignment pipeline and return a guaranteed non-empty excerpt plan."""
    bar_rows = _bar_rows_from_map(bar_map)
    total = len(bar_rows)
    if not total:
        return build_excerpt_plan(bar_map, mode='approximate', label='大致位置', detail='当前原谱无可用小节。')

    if not _has_written_playback_identity(bar_map):
        plan=provisional_excerpt_plan(bar_map,start_seconds=start_seconds,end_seconds=end_seconds,
            left_duration=left_duration,origin_info=origin_info,page_heights=page_heights)
        return dict(plan,mode='approximate',label='大致位置',
                    detail='原谱有反复、跳转或延长停顿，当前只显示参考谱段，不标为已对齐。')

    # ── Layer 0: Exact provenance fast-path (0 ms) ────────────────────────────
    # Left and right share the audio timeline. When the original score carries
    # its own beat map, convert the selection's seconds with THAT map, so later
    # beat/meter/note edits on the left never degrade the location.
    own_clock = False
    if (origin_info and origin_info.get('reference_clock') and origin_info.get('clock_digest')
            and not structural_edited):
        try:
            from copy import deepcopy
            from .timing import TempoMap
            right_clock = TempoMap(deepcopy(origin_info['reference_clock']))
            start_beat = right_clock.to_beat(float(start_seconds))
            end_beat = right_clock.to_beat(float(end_seconds))
            clock_digest = origin_info['clock_digest']
            own_clock = True
        except (TypeError, ValueError, KeyError):
            pass
    if (origin_info and origin_info.get('kind') in ('committed_xml', 'midi_seed')
            and not structural_edited
            and origin_info.get('clock_digest') and clock_digest
            and origin_info['clock_digest'] == clock_digest):
        opening_beat = float(origin_info.get('opening_beat') or 0.0)
        local_start = float(start_beat) - opening_beat
        local_end = float(end_beat) - opening_beat
        score_end = origin_info.get('score_end_beat')
        if local_end <= 1e-4:
            # Inside discarded prelude: show opening bars cleanly, no fake highlight
            return build_excerpt_plan(
                bar_map,
                mode='approximate',
                label='大致位置 · 原谱开头',
                detail='所选段落位于被裁掉的前奏内，当前显示原谱开头。',
                core_bar_indices=None,
                view_bar_indices=(0, min(3, total - 1)),
                page_heights=page_heights,
            )
        if score_end is not None and local_start >= float(score_end) - 1e-4:
            return build_excerpt_plan(
                bar_map,
                mode='approximate',
                label='大致位置 · 原谱结尾',
                detail='所选段落超出原谱末尾，当前显示原谱结尾。',
                core_bar_indices=None,
                view_bar_indices=(max(0, total - 4), total - 1),
                page_heights=page_heights,
            )
        clamped_s = max(0.0, local_start)
        clamped_e = min(float(score_end), local_end) if score_end is not None else local_end
        if clamped_e > clamped_s + 1e-4:
            s_tick = max(0, int(round(clamped_s * 480)))
            e_tick = max(s_tick + 1, int(round(clamped_e * 480)))
            m0, m1 = _ticks_to_bar_range(bar_rows, s_tick, e_tick)
            b_text = _format_bar_span(bar_rows[m0][0] + 1, bar_rows[m1][0] + 1)
            return build_excerpt_plan(
                bar_map,
                mode='exact',
                label=f'已定位 · {b_text}',
                detail=('已按原谱自己的拍点与开头偏移精准定位，前后各预留 2 小节。' if own_clock
                        else '已按当前工程节拍与开头偏移精准定位，前后各预留 2 小节。'),
                core_bar_indices=(m0, m1),
                context_bars=2,
                page_heights=page_heights,
            )

    # ── Layer 0b: same audio timeline (any right score, incl. edited MSCZ) ───
    # Unless the right side's bar structure was edited, its playback times are
    # the audio's times plus the omitted lead-in. One offset locates the bars.
    if left_notes and not structural_edited:
        try:
            right_notes, _, sec_to_tick0, _, _ = parse_right_midi(right_midi_bytes)
            found = _same_timeline(left_notes, right_notes, float(start_seconds), float(end_seconds))
        except (ValueError, OSError, EOFError):
            found = None
        if found and found[1] >= .7:
            offset = found[0]
            s_tick = sec_to_tick0(max(0.0, float(start_seconds) + offset))
            e_tick = sec_to_tick0(max(0.0, float(end_seconds) + offset))
            m0, m1 = _ticks_to_bar_range(bar_rows, s_tick, max(s_tick + 120, e_tick))
            b_text = _format_bar_span(bar_rows[m0][0] + 1, bar_rows[m1][0] + 1)
            return build_excerpt_plan(
                bar_map, mode='exact', label=f'已定位 · {b_text}',
                detail=f'两边按同一段音频的时间对上（{round(found[1] * 100)}% 的音吻合），前后各预留 2 小节。',
                core_bar_indices=(m0, m1), context_bars=2, page_heights=page_heights)

    # ── Layer 1: Full-song Gotoh affine-gap alignment + monotonic bracket ─────
    midi_digest = sha256(right_midi_bytes).hexdigest()[:16]
    cache_key = (str(project), str(left_revision), int(right_revision or 0), midi_digest)
    if cancelled and cancelled():
        raise _AlignmentCancelled()
    def approximate(reason):
        plan=provisional_excerpt_plan(bar_map,start_seconds=start_seconds,end_seconds=end_seconds,
                left_duration=left_duration,origin_info=origin_info,page_heights=page_heights)
        return dict(plan,mode='approximate',label='大致位置',detail=reason)
    # Exact immutable source above can disambiguate a repeated passage. Without
    # it, two identical phrases after one was removed are genuinely ambiguous.
    if _duplicate_onsets(left_notes,float(start_seconds),float(end_seconds)):
        return approximate('所选乐句在音符中重复，无法可靠区分，当前只显示附近谱段。')
    try:
        aligned = _cached_global_alignment(cache_key,left_notes,right_midi_bytes,hop=.30,cancelled=cancelled)
    except _AlignmentBudget as exc:
        return approximate(str(exc))

    hop = aligned['hop']
    T_L = aligned['T_L']
    T_R = aligned['T_R']
    active_L = aligned['active_L']
    mj = aligned['matched_j']
    msim = aligned['matched_sim']
    nj = aligned['near_j']
    sec_to_tick = aligned['sec_to_tick']

    i0 = max(0, min(len(T_L) - 1, int(round(float(start_seconds) / hop))))
    i1 = max(i0 + 1, min(len(T_L), int(round(float(end_seconds) / hop))))
    win_mj = mj[i0:i1]
    win_sim = msim[i0:i1]
    win_nj = nj[i0:i1]
    win_active = active_L[i0:i1]

    # Boundary checks against overall global overlap
    first_L = aligned['first_anchor_L']
    last_L = aligned['last_anchor_L']
    if first_L is not None and float(end_seconds) <= first_L + 0.45:
        return build_excerpt_plan(
            bar_map,
            mode='approximate',
            label='大致位置 · 原谱开头',
            detail='所选段落早于原谱有效起始位置（前奏可能已裁切），当前显示原谱开头。',
            core_bar_indices=None,
            view_bar_indices=(0, min(3, total - 1)),
            page_heights=page_heights,
        )
    if last_L is not None and float(start_seconds) >= last_L - 0.45:
        return build_excerpt_plan(
            bar_map,
            mode='approximate',
            label='大致位置 · 原谱结尾',
            detail='所选段落晚于原谱有效结尾，当前显示原谱结尾。',
            core_bar_indices=None,
            view_bar_indices=(max(0, total - 4), total - 1),
            page_heights=page_heights,
        )

    matched_mask = win_mj >= 0
    if np.any(win_active):
        active_matched = matched_mask & win_active
        coverage = float(np.sum(active_matched)) / float(np.sum(win_active))
        mean_sim = float(np.mean(win_sim[active_matched])) if np.any(active_matched) else 0.0
    else:
        coverage = float(np.mean(matched_mask))
        mean_sim = float(np.mean(win_sim[matched_mask])) if np.any(matched_mask) else 0.0

    q_dur = max(0.1, float(end_seconds) - float(start_seconds))
    r_span_sec = 0.0
    if np.any(matched_mask):
        r_s = float(T_R[int(win_mj[matched_mask][0])])
        r_e = float(T_R[int(win_mj[matched_mask][-1])]) + hop
        r_span_sec = max(0.0, r_e - r_s)

    not_collapsed = (q_dur < 1.8) or (r_span_sec >= 0.35 * q_dur)
    if np.any(matched_mask) and coverage >= 0.50 and mean_sim >= 0.52 and not_collapsed:
        s_tick = sec_to_tick(r_s)
        e_tick = sec_to_tick(r_e)
        m0, m1 = _ticks_to_bar_range(bar_rows, s_tick, max(s_tick + 120, e_tick))
        b_text = _format_bar_span(bar_rows[m0][0] + 1, bar_rows[m1][0] + 1)
        return build_excerpt_plan(
            bar_map,
            mode='aligned',
            label=f'已对齐 · {b_text}',
            detail='已结合全曲未改乐句锚定乐段位置并对齐，前后各预留 2 小节。',
            core_bar_indices=(m0, m1),
            context_bars=2,
            page_heights=page_heights,
        )

    # ── Layer 2: Guaranteed non-empty nearest valid excerpt ('大致位置') ──────
    # Deleted section (e.g. one of two identical choruses removed on Main Score),
    # empty silence, or heavily incompatible rewrite: show nearest seam, ZERO fake highlight!
    if len(win_nj) and aligned['total_score'] > 2.0:
        near_sec = float(np.median(T_R[win_nj]))
        near_tick = sec_to_tick(near_sec)
        m_near, _ = _ticks_to_bar_range(bar_rows, near_tick, near_tick + 120)
        v0 = max(0, m_near - 2)
        v1 = min(total - 1, m_near + 2)
    else:
        dur = max(float(left_duration or 0.0), float(end_seconds), 1.0)
        frac = max(0.0, min(1.0, float(start_seconds) / dur))
        m_near = max(0, min(total - 1, int(round(frac * (total - 1)))))
        v0 = max(0, m_near - 2)
        v1 = min(total - 1, m_near + 2)

    return build_excerpt_plan(
        bar_map,
        mode='approximate',
        label='大致位置',
        detail='该段在原谱中可能已删改或未找到可靠对应，当前显示附近有效谱段（不高亮假匹配）。',
        core_bar_indices=None,
        view_bar_indices=(v0, v1),
        page_heights=page_heights,
    )
