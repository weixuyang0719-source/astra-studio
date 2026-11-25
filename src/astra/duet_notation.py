"""Re-engrave cached DuET predictions against an explicit reference clock.

Model durations are musical values. Convert their beat units when DuET reads a
passage at half/double speed; never replace them with raw performance note-offs.
Upstream engraving is reused, but its hand-playability arrangement pass is
disabled: transcription must not thin notes or turn sustained bass into grace.
This module loads no model.
"""
from __future__ import annotations

import argparse
from bisect import bisect_right
from contextlib import contextmanager
import copy
import hashlib
import json
import math
from pathlib import Path
from statistics import median
import sys
import tempfile
import xml.etree.ElementTree as ET

from .timing import TempoMap

VERSION = 40


@contextmanager
def fast_readable_context(upstream):
    """In-memory O(N) replacements for O(N^2) vendor layout/cross_over/bar_items.

    Leaves pinned vendor/duet untouched on disk (preserving _source_hash) while
    guaranteeing 100% bit-identical clef, cross-staff, and MusicXML decisions.
    """
    from bisect import bisect_left, bisect_right
    from collections import defaultdict
    from fractions import Fraction as F
    import heapq
    from duet.readable import layout as layout_mod, hands as hands_mod, musicxml as musicxml_mod
    from duet.readable.spelling import ledgers

    orig_lh_clefs = upstream.lh_clefs
    orig_cross_over = upstream.cross_over
    orig_bar_items = musicxml_mod.bar_items
    clef_cost = layout_mod.clef_cost

    def fast_lh_clefs(plan, ctx, staff=1):
        cfg, NB, bars = ctx.cfg, ctx.nbars, ctx.bars
        L1 = plan.layers[staff]
        home, other = ('G', 'F') if staff == 0 else ('F', 'G')
        switch = cfg.clef_switch_penalty_rh if staff == 0 else cfg.clef_switch_penalty

        bar_starts = [bars.start(m) for m in range(NB)]
        bar_ends = [bars.end(m) for m in range(NB)]
        bar_by = [defaultdict(list) for _ in range(NB)]
        bar_held = [[] for _ in range(NB)]
        bar_graces = [[] for _ in range(NB)]

        for n in L1:
            on_v = n['on']
            end_v = on_v + n['dur']
            pitch = n['pitch']
            m0 = bisect_right(bar_ends, on_v)
            m1 = bisect_left(bar_starts, end_v)
            for m in range(m0, m1):
                bar_by[m][on_v].append(pitch)
                if on_v < bar_starts[m] < end_v:
                    bar_held[m].append(pitch)
        for n in plan.notes:
            if n['grace'] and n['staff'] == staff:
                on_v = n['on']
                m = bisect_right(bar_starts, on_v) - 1
                if 0 <= m < NB and bar_starts[m] <= on_v < bar_ends[m]:
                    bar_graces[m].append(n['pitch'])

        def bar_cost(m, c):
            o = 'G' if c == 'F' else 'F'
            by = bar_by[m]
            costs = sorted(((clef_cost(ps, c), clef_cost(ps, o)) for ps in by.values()), key=lambda t: t[1] + 6 - t[0])
            tot = sum(a for a, _ in costs)
            if costs and costs[0][1] + 6 < costs[0][0]:
                tot += costs[0][1] + 6 - costs[0][0]
            gr = bar_graces[m]
            bias = cfg.clef_rh_bass_bias if (staff == 0 and c == 'F') else 0
            return tot + cfg.grace_clef_weight * clef_cost(gr, c) + bias

        def tie_cost(m, c):
            return cfg.clef_tie_penalty * sum(ledgers(p, c) for p in bar_held[m])

        cost = {home: bar_cost(0, home), other: bar_cost(0, other) + 3}
        back = []
        for m in range(1, NB):
            new, bk = {}, {}
            for c in 'FG':
                tc = tie_cost(m, c)
                opts = {pc: cost[pc] + (0 if pc == c else switch + tc) for pc in 'FG'}
                pc = min(opts, key=opts.get)
                new[c] = opts[pc] + bar_cost(m, c); bk[c] = pc
            cost = new; back.append(bk)
        c = min(cost, key=cost.get)
        clef2 = [c]
        for bk in reversed(back):
            c = bk[c]; clef2.append(c)
        return clef2[::-1]

    def fast_cross_over(notes, ctx):
        cfg = ctx.cfg
        rh = [n for n in notes if n['staff'] == 0 and not n['grace']]
        lh_at = defaultdict(list)
        for n in notes:
            if n['staff'] == 1 and not n['grace']:
                lh_at[n['on']].append(n)
        bar_of_t = {t: ctx.bar_of(t) for t in lh_at}
        lh_bar = defaultdict(list)
        for t, grp in lh_at.items():
            lh_bar[bar_of_t[t]] += [n['pitch'] for n in grp]
        led = {t: max(ledgers(n['pitch'], 'F') for n in grp) for t, grp in lh_at.items()}
        lows = {t: min(n['pitch'] for n in grp) for t, grp in lh_at.items()}
        high = set()
        if cfg.cross_high:
            high_by_bar = defaultdict(list)
            for t in lh_at:
                if lows[t] >= cfg.cross_over_min and led[t] >= cfg.local_clef_min_ledgers and min(lh_bar[bar_of_t[t]]) < 50:
                    high.add(t)
                    high_by_bar[bar_of_t[t]].append(t)
            cands_by_bar = defaultdict(list)
            for t in lh_at:
                if t not in high and lows[t] >= cfg.cross_over_min and led[t] >= cfg.local_clef_min_ledgers - 1:
                    cands_by_bar[bar_of_t[t]].append(t)
            half_beat = F(1, 2)
            for b, h_list in list(high_by_bar.items()):
                cands = cands_by_bar.get(b)
                if not cands:
                    continue
                grow = True
                while grow:
                    grow = False
                    remaining = []
                    for t in cands:
                        if any(abs(t - h) <= half_beat for h in h_list):
                            high.add(t)
                            h_list.append(t)
                            grow = True
                        else:
                            remaining.append(t)
                    cands = remaining

        # Pre-index RH by onset and by bar, and sort RH/LH intervals for O(N log N) active sweep!
        rh_pitches_at = defaultdict(set)
        rh_list_at = defaultdict(list)
        rh_by_bar = defaultdict(list)
        rh_sorted = []
        for idx_r, r in enumerate(rh):
            on_r = r['on']
            p_r = r['pitch']
            rh_pitches_at[on_r].add(p_r)
            rh_list_at[on_r].append(p_r)
            rh_by_bar[ctx.bar_of(on_r)].append(p_r)
            rh_sorted.append((on_r, on_r + r['dur'], p_r, idx_r))
        rh_sorted.sort(key=lambda x: x[0])

        lh_sorted_items = sorted(lh_at.items())
        rh_ptr = 0
        rh_heap = []  # (end, pitch)
        lh_heap = []  # (end, pitch)
        lo_min, need = cfg.ottava_rh
        moved = 0
        for t, grp in lh_sorted_items:
            # Advance RH active set (on <= t < end)
            while rh_ptr < len(rh_sorted) and rh_sorted[rh_ptr][0] <= t:
                _, end_r, p_r, _ = rh_sorted[rh_ptr]
                if end_r > t:
                    heapq.heappush(rh_heap, (end_r, p_r))
                rh_ptr += 1
            while rh_heap and rh_heap[0][0] <= t:
                heapq.heappop(rh_heap)
            # Expire LH notes with end <= t (strictly on < t < end: current grp added AFTER check!)
            while lh_heap and lh_heap[0][0] <= t:
                heapq.heappop(lh_heap)

            lo = lows[t]
            if lo >= cfg.cross_over_min:
                if t in high:
                    rh_at_t = rh_pitches_at.get(t)
                    skip = bool(rh_at_t and any(n['pitch'] in rh_at_t for n in grp))
                    if not skip:
                        bar_rh = rh_by_bar.get(bar_of_t[t])
                        if (bar_rh and min(bar_rh) >= lo_min and lo < lo_min
                                and (sum(p >= need for p in bar_rh) >= 2 or max(bar_rh) >= need + 3)):
                            skip = True
                else:
                    has_held = any(p < lo - 12 for _, p in lh_heap)
                    if not has_held or not rh_heap or lo <= max(p for _, p in rh_heap):
                        skip = True
                    else:
                        chord = rh_list_at.get(t, []) + [n['pitch'] for n in grp]
                        skip = max(chord) - min(chord) > cfg.hand_span or len(chord) > cfg.hand_max_notes
                if not skip:
                    for n in grp:
                        n.setdefault('original_staff', n['staff'])
                        n['staff'] = 0; n['crossed'] = 'high' if t in high else True
                    moved += len(grp)
                    ctx.log(f"LH {'too high for the bass clef' if t in high else 'above RH'} -> RH staff "
                            f"{sorted(n['pitch'] for n in grp)} ({ctx.where(t)})")
            # Note: original checks `for g in lh_at.values()` regardless of whether an earlier group moved,
            # so push all original LH notes from grp for subsequent t' > t!
            for n in grp:
                end_n = n['on'] + n['dur']
                if end_n > t:
                    heapq.heappush(lh_heap, (end_n, n['pitch']))
        return moved

    # Fast cached O(log N) bar_items when musicxml.bar_items is not overridden by readable_writer
    _bar_item_cache = {}
    def fast_bar_items(evs, lo, hi):
        key = id(evs)
        entry = _bar_item_cache.get(key)
        if entry is None or entry[0] is not evs or entry[1] != len(evs):
            ons = [ev['on'] for ev in evs]
            # evs from events_for is sorted by onset; maintain prefix max end for fast pruning
            pmax = []
            cur = None
            for ev in evs:
                e = ev['end']
                cur = e if cur is None or e > cur else cur
                pmax.append(cur)
            entry = (evs, len(evs), ons, pmax)
            if len(_bar_item_cache) > 16:
                _bar_item_cache.clear()
            _bar_item_cache[key] = entry
        _, n_ev, ons, pmax = entry
        if not n_ev:
            return []
        right = bisect_left(ons, hi)
        if right == 0:
            return []
        # Find earliest index where pmax[i] > lo (so no event before it can reach > lo)
        left = bisect_right(pmax, lo, 0, right)
        items = []
        for idx in range(left, right):
            ev = evs[idx]
            if ev['end'] <= lo:
                continue
            s = ev['on'] if ev['on'] > lo else lo
            e = ev['end'] if ev['end'] < hi else hi
            items.append((s - lo, e - lo, dict(ev=ev, tie_in=ev['on'] < lo, tie_out=ev['end'] > hi)))
        return items

    upstream.lh_clefs = fast_lh_clefs
    upstream.cross_over = fast_cross_over
    musicxml_mod.bar_items = fast_bar_items
    try:
        yield
    finally:
        upstream.lh_clefs = orig_lh_clefs
        upstream.cross_over = orig_cross_over
        musicxml_mod.bar_items = orig_bar_items
        _bar_item_cache.clear()


@contextmanager
def _without_playability_arrangement(upstream):
    """Keep model content; hand-span constraints must not arrange this score.

    The pinned vendor has no config switch for this stage. Its fallback can
    delete pitches, force rolled chords, or change a normal bass into a grace
    note. Bypass only that stage in this isolated worker; timing-derived rolls,
    genuine model grace notes and subsequent non-destructive layout remain.
    """
    original=upstream.playability
    def preserve(notes, ctx):
        ctx.log('playability arrangement disabled: preserving notes and model hands')
        return notes, {'reassigned':0,'grace_bass':0,'thinned':0,'rolled':0,
                       'arrangement_disabled':True}
    upstream.playability=preserve
    try:yield
    finally:upstream.playability=original


def _beat_at(tempo, seconds):
    # The upstream grid can extrapolate a beat before audio zero. Extend only
    # the first reference segment for interval comparison, not the MIDI file.
    if seconds < 0:
        return seconds * tempo.beats[1] / tempo.seconds[1]
    return tempo.to_beat(seconds)


def infer_beat_ratios(model_grid, tempo, *, tolerance=.18, min_support=2):
    """Return reference quarters per model quarter, restricted to .5/1/2.

    Unsupported ratios are deliberately left at 1 and returned for review.
    Requiring consecutive evidence avoids interpreting a single jittered beat
    as a half/double-time passage. Real tempo changes already in the reference
    map cancel out when the model follows those changes correctly.
    """
    times = [float(v) for v in model_grid]
    if len(times) < 2 or not all(math.isfinite(v) for v in times) or any(b <= a for a, b in zip(times, times[1:])):
        raise ValueError("Model beat times must be finite and strictly increasing")
    if not 0 < tolerance < .5 or min_support < 1:
        raise ValueError("Invalid ratio confidence settings")
    reference = [_beat_at(tempo, t) for t in times]
    raw = [b - a for a, b in zip(reference, reference[1:])]
    candidates = []
    for value in raw:
        closest = min((.5, 1., 2.), key=lambda k: abs(math.log2(value / k)))
        candidates.append(closest if abs(value / closest - 1) <= tolerance else None)
    accepted = list(candidates)
    i = 0
    while i < len(candidates):
        j = i + 1
        while j < len(candidates) and candidates[j] == candidates[i]:
            j += 1
        if candidates[i] not in (None, 1.) and j - i < min_support:
            accepted[i:j] = [None] * (j - i)
        i = j
    review = []
    for index, value in enumerate(accepted):
        if value is None:
            review.append({"kind": "uncertain-beat-ratio", "model_beat": index,
                           "start_seconds": times[index], "end_seconds": times[index + 1],
                           "reference_beats_per_model_beat": raw[index],
                           "action": "retain_model_duration_unit"})
    ratios = [value if value is not None else 1. for value in accepted]
    return ratios, review


def classify_ratio_review(review, positions, durations, onsets, reference_onsets, *, audio_duration=None):
    """Audit uncertain grid intervals against independent musical attacks.

    This can confirm the existing neutral ratio (1) without changing durations.
    It never infers new half/double-time changes from majority smoothing. A chord
    supplies one vote; reversed model positions and long/silent spans supply none.
    Preserve all original diagnostics, including unused extrapolated grid spans.
    """
    count = len(positions)
    if any(len(values) != count for values in (durations, onsets, reference_onsets)):
        raise ValueError("Ratio context arrays must have equal lengths")
    clusters = []
    for index in sorted(range(count), key=lambda i: onsets[i]):
        if not clusters or onsets[index] - onsets[clusters[-1][0]] > .06:
            clusters.append([])
        clusters[-1].append(index)
    anchors = []
    for indices in clusters:
        votes = {}
        for index in indices:
            value = float(positions[index])
            votes[value] = votes.get(value, 0) + 1
        position = max(votes, key=votes.get)
        if votes[position] / len(indices) >= .6:
            anchors.append((position, median(float(reference_onsets[i]) for i in indices), indices))
    slopes = []
    reversed_spans = []
    for left, right in zip(anchors, anchors[1:]):
        delta_model, delta_reference = right[0] - left[0], right[1] - left[1]
        if delta_model < 0:
            reversed_spans.append((right[0], left[0]))
        if not .1 <= delta_model <= 2.01 or delta_reference <= 0:
            continue
        value = delta_reference / delta_model
        closest = min((.5, 1., 2.), key=lambda r: abs(math.log2(value / r)))
        candidate = closest if abs(value / closest - 1) <= .18 else None
        slopes.append((left[0], right[0], candidate, value, left[2], right[2]))
    uncertain, diagnostic, resolved = [], [], []
    for original in review:
        item = dict(original)
        beat = item["model_beat"]
        affected = [i for i, (start, duration) in enumerate(zip(positions, durations))
                    if duration > 0 and start < beat + 1 - 1e-8 and start + duration > beat + 1e-8]
        item.update(affected_note_indices=affected, affected_note_count=len(affected))
        if audio_duration is not None:
            item["outside_audio"] = item["start_seconds"] >= audio_duration
        local = [s for s in slopes if s[0] >= beat - 4 and s[1] <= beat + 5]
        supported = [s for s in local if s[2] is not None]
        neutral = [s for s in supported if s[2] == 1.]
        conflicting_inside = any(s[2] != 1. and s[0] < beat + 1 and s[1] > beat for s in supported)
        conflicting_inside |= any(start < beat + 1 and stop > beat for start, stop in reversed_spans)
        confirmed = (len(neutral) >= 3 and len(neutral) / max(1, len(supported)) >= .8
                     and len(supported) / max(1, len(local)) >= .6 and not conflicting_inside)
        if confirmed:
            item["action"] = "confirm_model_duration_unit_from_attack_context"
            item["confirmed_ratio"] = 1.
            item["evidence"] = {"neutral_attack_spacings": len(neutral),
                                "supported_attack_spacings": len(supported),
                                "local_attack_spacings": len(local),
                                "spacings": [{"model_start": s[0], "model_end": s[1],
                                              "observed_ratio": s[3], "candidate": s[2],
                                              "left_note_indices": s[4], "right_note_indices": s[5]}
                                             for s in local]}
            resolved.append(item)
        elif not affected:
            item["action"] = "diagnostic_only_no_note_duration_crosses_interval"
            diagnostic.append(item)
        else:
            uncertain.append(item)
    affected = sorted({i for item in uncertain for i in item["affected_note_indices"]})
    return {"uncertain_intervals": uncertain, "diagnostic_intervals": diagnostic,
            "context_resolved_intervals": resolved, "original_uncertain_intervals": review,
            "affected_note_indices": affected, "affected_note_count": len(affected),
            "message": "Only unresolved intervals crossed by note durations need review; unused grid spans and context-confirmed neutral units remain in diagnostics."}


_RATIOS_PREFIX_CACHE = {}


def _ratios_prefix(ratios):
    """Validate ratios once and cache cumulative prefix sums for O(1) integration."""
    key = (id(ratios), len(ratios))
    cached = _RATIOS_PREFIX_CACHE.get(key)
    if cached is not None and cached[0] is ratios:
        return cached[1]
    if any(not math.isfinite(v) or v <= 0 for v in ratios):
        raise ValueError("Beat ratios must be positive")
    prefix = [0.0] * (len(ratios) + 1)
    acc = 0.0
    for idx, val in enumerate(ratios):
        acc += val
        prefix[idx + 1] = acc
    if len(_RATIOS_PREFIX_CACHE) > 8:
        _RATIOS_PREFIX_CACHE.clear()
    _RATIOS_PREFIX_CACHE[key] = (ratios, prefix)
    return prefix


def scale_model_duration(start, duration, ratios):
    """Integrate beat-unit ratios across the complete *model* note duration."""
    if not ratios or not math.isfinite(start) or not math.isfinite(duration) or start < 0 or duration < 0:
        raise ValueError("Invalid model duration")
    prefix = _ratios_prefix(ratios)
    if duration <= 1e-10:
        return 0.0
    end = start + duration
    n = len(ratios)

    def _cum(pos):
        idx = int(math.floor(pos + 1e-10))
        if idx < n:
            return prefix[idx] + (pos - idx) * ratios[idx]
        return prefix[n] + (pos - n) * ratios[n - 1]

    return _cum(end) - _cum(start)


def reference_phase(tempo, beat):
    """Origin and identity of the explicit meter/phase segment containing beat.

    A range edit may translate subsequent musical coordinates by a non-grid
    amount while preserving seconds. Signature events reset phase, including
    repeated signatures, consistently with reference_bars.
    """
    origin = float(tempo.data.get('grid_origin', tempo.data.get('pickup_beats', 0)))
    region = 0
    for index, signature in enumerate(tempo.data.get('time_signatures', ())):
        position = float(signature['beat'])
        if position > float(beat) + 1e-9:
            break
        if position > 1e-9:
            origin, region = position, index + 1
    # Explicit short measures reset bar/subdivision phase at both boundaries,
    # but never reinterpret quarter coordinates or performance seconds.
    boundaries=sorted({tempo.to_beat(float(a[key])) for a in tempo.data.get('short_bar_annotations',[])
                       for key in ('start_seconds','end_seconds')})
    for index, position in enumerate(boundaries):
        if position <= float(beat)+1e-9 and position >= origin-1e-9:
            origin,region=position,len(tempo.data.get('time_signatures',()))+index+1
    return origin, region


def snap_reference_onset(seconds, tempo, grid="auto"):
    """The existing Astra onset grids, without looking at performance offsets."""
    steps = {"straight": .25, "triplet": 1 / 3, "fine": .125}
    value = tempo.to_beat(float(seconds))
    origin, _ = reference_phase(tempo, value)
    def snap(step):
        return origin + round((value - origin) / step) * step
    if grid == "auto":
        step, _ = min(((.25, 0.), (1 / 3, .0004), (1 / 6, .0006), (.125, .001)),
                      key=lambda item: (value - snap(item[0])) ** 2 + item[1])
    elif grid in steps:
        step = steps[grid]
    else:
        raise ValueError("Unknown onset grid")
    return max(0., snap(step))


def align_model_onset_groups(model_positions, onsets, pitches, durations, tempo,
                             grid='auto', *, protected_input_ids=(),protected_anchor_seconds=(), staffs=None,
                             manual_collision_input_ids=()):
    """Use model rhythm within the selected reference grid, not performance jitter.

    Same-model-onset notes that land in the same reference cell are a written
    chord. Distinct model rhythms, repeated pitches and regularly articulated
    runs still retain separate attacks. This is a notation projection only;
    the source performance remains in seconds with every original attack.
    """
    count = len(onsets)
    if any(len(values) != count for values in (model_positions, pitches, durations)):
        raise ValueError('Onset group arrays must have equal lengths')
    if staffs is not None and len(staffs) != count:
        raise ValueError('Staff array must match onset groups')
    raw = [tempo.to_beat(float(t)) for t in onsets]
    result = [snap_reference_onset(t, tempo, grid) for t in onsets]
    initial_result = list(result)
    protected = set(protected_input_ids)
    manual_collision = set(manual_collision_input_ids) & protected
    locked = protected - manual_collision
    anchors=sorted(set(map(float,protected_anchor_seconds)))
    regions=[bisect_right(anchors,float(t)) for t in onsets]
    at_anchor={i for i,t in enumerate(onsets) if any(abs(float(t)-a)<1e-8 for a in anchors)}
    def crosses_pin(ids,target):
        return any((float(onsets[i])<a-1e-8 and target>tempo.to_beat(a)+1e-8)
                   or (float(onsets[i])>a+1e-8 and target<tempo.to_beat(a)-1e-8)
                   for i in ids for a in anchors)
    model_groups = {}
    phases = [reference_phase(tempo, q) for q in raw]
    for i in range(count):
        if float(durations[i]) > 0:
            model_groups.setdefault(round(float(model_positions[i]), 7), []).append(i)
    running = set()
    for ids in model_groups.values():
        ids = sorted(ids, key=lambda i: (onsets[i], i))
        gaps = [float(onsets[b])-float(onsets[a]) for a,b in zip(ids,ids[1:])]
        steps = [int(pitches[b])-int(pitches[a]) for a,b in zip(ids,ids[1:])]
        # A model can erroneously flatten a continuous arpeggio into one q.
        # Retain a regular pitched sequence spanning multiple written cells.
        # This is structural evidence, not an absolute millisecond cutoff.
        if (len(ids) >= 3 and len({result[i] for i in ids}) > 1 and min(gaps) > 0
                and min(gaps) >= .75*max(gaps)
                and (all(s > 0 for s in steps) or all(s < 0 for s in steps))):
            running.update(ids)
    # An adaptive grid can bisect one compact model chord: e.g. 3 notes choose
    # the fine lattice and 3 choose the straight lattice. Neither cell then has
    # majority support, and serializing all six invents an impossible run.
    # Establish this narrow, model-supported cluster BEFORE cell voting. The
    # bound applies to the entire cluster, never a transitive near-neighbor
    # chain. Reviewed notes, repeated pitches and regular runs stay separate.
    compact_membership = {}
    compact_groups = []
    for ids in model_groups.values():
        cluster = []
        def finish_compact():
            if len(cluster) < 2:
                return
            target = snap_reference_onset(median(float(onsets[i]) for i in cluster), tempo, grid)
            token = len(compact_groups)
            compact_groups.append({'input_ids':list(cluster),'model_position':float(model_positions[cluster[0]]),
                'source_span_quarters':max(raw[i] for i in cluster)-min(raw[i] for i in cluster),
                'before':[initial_result[i] for i in cluster],'reference_onset':target,
                'reason':'compact_same_model_attack_before_adaptive_cell_vote'})
            for i in cluster:
                compact_membership[i] = token
                result[i] = target
        for i in sorted(ids, key=lambda i:(onsets[i],i)):
            if (i in running or i in protected or i in at_anchor or (cluster and
                    (raw[i]-raw[cluster[0]] > 1/16 + 1e-9
                     or phases[i][1] != phases[cluster[0]][1]
                     or regions[i] != regions[cluster[0]]
                     or any(int(pitches[j]) == int(pitches[i]) for j in cluster)))):
                finish_compact(); cluster = []
            if i not in running and i not in protected and i not in at_anchor:
                cluster.append(i)
        finish_compact()
    groups = []
    buckets = {}
    cell_support = {}
    for model_key, ids in model_groups.items():
        for cell in {result[i] for i in ids}:
            cell_support[(model_key, cell)] = sum(result[i] == cell for i in ids) > len(ids)/2
    for i in sorted(range(count), key=lambda i: (onsets[i], i)):
        if float(durations[i]) <= 0:
            continue
        key = (round(float(model_positions[i]), 7), result[i])
        candidates = buckets.setdefault(key, [])
        group = next((g for g in candidates
                      if i not in running and not any(j in running for j in g)
                      and i not in at_anchor and not any(j in at_anchor for j in g)
                      and all(phases[j][1] == phases[i][1] for j in g)
                      and all(regions[j] == regions[i] for j in g)
                      and (cell_support[key]
                           or (i in compact_membership and all(compact_membership.get(j)==compact_membership[i] for j in g))
                           or all(abs(float(onsets[i])-float(onsets[j])) < 1e-7 for j in g))
                      and not any(int(pitches[j]) == int(pitches[i]) for j in g)), None)
        if group is None:
            group = []; groups.append(group); candidates.append(group)
        group.append(i)
    groups.sort(key=lambda g: (median(raw[i] for i in g), min(g)))
    # Different hands may disagree in model rhythm while attacking together.
    # Do not invent a global ordering for an isolated, simultaneous cross-staff
    # chord. This requires both model disagreement and independent local spacing
    # evidence, not a blanket millisecond threshold. Same-staff attacks/restrikes,
    # runs, locks, phase boundaries and pins remain outside this rule.
    cross_staff_attacks = []
    if staffs is not None:
        original_groups = groups
        targets_before = [snap_reference_onset(median(float(onsets[i]) for i in g), tempo, grid)
                          for g in groups]
        groups = []
        start = 0
        while start < len(original_groups):
            end = start + 1
            while end < len(original_groups) and abs(targets_before[end]-targets_before[start]) < 1e-9:
                end += 1
            batch = original_groups[start:end]
            ids = [i for g in batch for i in g]
            span = max(raw[i] for i in ids)-min(raw[i] for i in ids)
            staff_sets = [set(staffs[i] for i in g) for g in batch]
            separate_staffs = all(len(s)==1 for s in staff_sets) and len(set.union(*staff_sets))==len(batch)
            model_span = max(float(model_positions[i]) for i in ids)-min(float(model_positions[i]) for i in ids)
            before = (min(raw[i] for i in ids)-max(raw[i] for i in original_groups[start-1])) if start else 0.
            after = (min(raw[i] for i in original_groups[end])-max(raw[i] for i in ids)) if end < len(original_groups) else 0.
            target = targets_before[start]
            supported = (len(batch)>1 and separate_staffs and span <= 1/32
                         and min(before,after) > max(8*span,1/8)
                         and model_span > max(4*span,1/16)
                         and len(set(int(pitches[i]) for i in ids))==len(ids)
                         and not (set(ids)&(running|protected|at_anchor))
                         and len(set(phases[i][1] for i in ids))==1
                         and len(set(regions[i] for i in ids))==1
                         and not crosses_pin(ids,target))
            if supported:
                groups.append(ids)
                cross_staff_attacks.append({'input_ids':ids,'reference_onset':target,
                    'source_span_quarters':span,'model_span_quarters':model_span,
                    'neighbor_gaps_quarters':[before,after],
                    'reason':'isolated_cross_staff_attack_with_model_rhythm_disagreement'})
            else:
                groups.extend(batch)
            start = end
    centers = [median(raw[i] for i in group) for group in groups]
    targets = [snap_reference_onset(median(float(onsets[i]) for i in group), tempo, grid) for group in groups]
    # A manual MIDI attack is protected from regrouping and model reinterpretation.
    # It does not lock an inferred printed cell. Explicit reviewed attacks still
    # constrain the collision solver, and their review checks remain unchanged.
    def allows_manual_fit(group, target):
        return all(i not in manual_collision or (abs(target-raw[i]) <= 1/16 + 1e-9
                    and reference_phase(tempo, target)[1] == phases[i][1])
                   for i in group)

    audit = {'method': 'reference_model_cell_v4', 'groups': [], 'skipped_groups': [],
             'changed_note_count': 0, 'raw_timestamps_modified': False,
             'refined_collisions': [], 'preserved_run_input_ids': sorted(running),
             'compact_model_attacks':compact_groups, 'cross_staff_attacks':cross_staff_attacks,
             'refined_manual_collision_input_ids': []}
    index = 0
    while index < len(groups):
        stop = index + 1
        while stop < len(groups) and targets[stop] <= targets[stop-1] + 1e-9:
            stop += 1
        if stop - index > 1:
            lo = targets[index-1] if index else -1
            hi = targets[stop] if stop < len(groups) else math.inf
            selected = None
            # Include both dyadic and triplet subdivisions, no repeated snapping
            # of already-quantized events. Use raw reference positions each time.
            subdivisions = (1/6, 1/12, 1/24, 1/48) if grid == 'triplet' else (.125, .0625, .03125, 1/6, 1/12, 1/24, 1/48)
            for step in subdivisions:
                candidate = []
                for q in centers[index:stop]:
                    origin, _ = reference_phase(tempo, q)
                    candidate.append(max(0., origin + round((q-origin)/step)*step))
                if (lo < candidate[0] and candidate[-1] < hi
                        and not any(crosses_pin(g,v) for g,v in zip(groups[index:stop],candidate))
                        and all(allows_manual_fit(g,v) for g,v in zip(groups[index:stop],candidate))
                        and all(b-a > 1e-9 for a,b in zip(candidate,candidate[1:]))):
                    selected = candidate
                    break
            if selected is None:
                # Closely staggered chord attacks can straddle the same cell at
                # every rounding lattice. Separate them on a legal fine grid
                # with bounded displacement; never invent 1/480-note fragments.
                step = 1/48 if grid == 'triplet' else 1/32
                # Each attack keeps its own phase lattice. A collision across
                # a signature boundary is normal (e.g. a late chord followed
                # by the next downbeat), not grounds to abort the whole score.
                # Dynamic programming selects the least-displaced ordered path;
                # candidates cannot cross phase boundaries or protected pins.
                paths = [(0., [])]
                for group, center in zip(groups[index:stop], centers[index:stop]):
                    origin, region = reference_phase(tempo, center)
                    nearest = round((center-origin)/step)
                    candidates = [origin + k*step for k in range(nearest-3, nearest+4)]
                    candidates = [v for v in candidates if v >= 0 and lo < v < hi
                                  and abs(v-center) <= 1/16 + 1e-9
                                  and reference_phase(tempo, v)[1] == region
                                  and not crosses_pin(group, v)
                                  and allows_manual_fit(group, v)]
                    following = []
                    for value in candidates:
                        choices = [(cost+(value-center)**2, path+[value])
                                   for cost, path in paths
                                   if not path or value-path[-1] > 1e-9]
                        if choices:
                            following.append(min(choices, key=lambda item:item[0]))
                    paths = following
                    if not paths:
                        break
                if not paths:
                    raise ValueError('相邻起音过密，无法在记谱网格内保持顺序；请核对这些音符')
                selected = min(paths,key=lambda item:item[0])[1]
            audit['refined_collisions'].append({'input_ids': [i for group in groups[index:stop] for i in group],
                 'grid_quarters': step, 'before': targets[index:stop], 'after': selected})
            audit['refined_manual_collision_input_ids'].extend(
                i for group, target in zip(groups[index:stop], selected)
                for i in group if i in manual_collision and abs(initial_result[i]-target)>1e-9)
            targets[index:stop] = selected
        index = stop
    for group, target in zip(groups, targets):
        if crosses_pin(group,target):
            raise ValueError('记谱起音会跨过固定锚点，已保留旧谱；请核对该处起音或解除对应锚点后重试。')
        if len(group) > 1:
            audit['groups'].append({'input_ids':group, 'model_position':float(model_positions[group[0]]),
                'reference_onset':target, 'reason':('isolated_cross_staff_attack_with_model_rhythm_disagreement'
                          if any(g['input_ids']==group for g in cross_staff_attacks)
                          else 'same_model_rhythm_and_reference_cell')})
        for i in group:
            if (i in manual_collision and abs(initial_result[i]-target) > 1e-9
                    and i not in audit['refined_manual_collision_input_ids']):
                raise ValueError('手动音符的起音分组不兼容；请核对这些音符')
            if i in locked and abs(initial_result[i]-target) > 1e-9:
                raise ValueError('新起音细分与已有记谱审核修订冲突；请保留旧谱或重新确认该处修订')
            audit['changed_note_count'] += abs(initial_result[i]-target) > 1e-9
            result[i] = target
    audit['group_count'] = len(groups)
    return result, audit


def original_model_rolls(upstream, score, data, frames, q, qd, meter, model_bars,
                         model_grid, shift_at, cfg):
    """Read roll identities from the original cached prediction and fused grid.

    This runs only the existing deterministic notation quantizer. Neither the
    reference clock nor any reprojected onset participates in classification.
    """
    import numpy as np
    from fractions import Fraction
    from duet.readable.context import Ctx
    from duet.readable.inputs import Inputs
    ctx=Ctx(cfg=cfg,log=lambda _:None)
    ctx.B,ctx.num,ctx.beat_type=meter.bar_q,meter.num,meter.den
    ctx.pulse,ctx.compound=meter.pulse_q,meter.compound
    original=dict(score);original['notes']=score['notes'].copy()
    original['notes'][:,1]=q;original['notes'][:,2]=qd
    original['is_grace']=np.asarray(qd)==0
    on=data['ref_onset'];positions=q+np.asarray([shift_at(t) for t in on])
    lead=Fraction(int(min(positions)/float(ctx.pulse)))*ctx.pulse
    ctx.pickup=lead if 0<lead<ctx.B else Fraction(0)
    if model_bars is not None:
        ctx.bars=upstream.map_model_bars(model_bars,q,on,shift_at,
            Fraction(float(max(positions))).limit_denominator(48),ctx,quiet=True)
    inp=Inputs(perf=list(zip(on,data['ref_offset'],data['ref_pitch'],data['ref_vel'])),
               pedal=[],score=original,frames=frames)
    notes=upstream.quantize(inp,model_grid,shift_at,ctx)
    buckets={}
    for note in notes:
        if note.get('roll'):
            buckets.setdefault((note['staff'],float(note['on'])),[]).append(note)
    result=[]
    for (staff,_),group in buckets.items():
        group.sort(key=lambda n:(n['t'],n['i']))
        # Timing-only free-zone rolls are not a model-onset rhythm prior.
        if len(group)<2 or len({round(float(positions[n['i']]),7) for n in group})!=1:continue
        spread=max(n['x'] for n in group)-min(n['x'] for n in group)
        sustain=all(n['xo']>=max(g['x'] for g in group)+.5 for n in group)
        result.append(dict(source='duet_original_quantize',input_ids=[n['i'] for n in group],staff=staff,
            model_onset_quarters=float(positions[group[0]['i']]),
            model_spread_quarters=float(spread),
            model_spread_limit_quarters=float(cfg.roll_max_spread if sustain else min(cfg.roll_max_spread,cfg.roll_spread_free)),
            model_classifier_limits={k:float(getattr(cfg,k)) for k in
                ('roll_gap','roll_min_spread','roll_max_spread','roll_spread_free')},
            source_spread_seconds=float(group[-1]['t']-group[0]['t']),sustained=bool(sustain),
            direction='up' if group[-1]['pitch']>group[0]['pitch'] else 'down'))
    return result


def project_model_notation(prior, reference, *, grid='auto', protected_input_ids=(),
                           protected_anchor_seconds=(), manual_collision_input_ids=()):
    """The single deterministic projection used by formal export and local view.

    Callers must verify the original prediction/performance identity first.
    No written MusicXML coordinate is accepted as a model position here.
    """
    from .notation_model_rhythm import preserve_model_sequences
    from fractions import Fraction
    positions=prior['positions'];qd=prior['durations'];on=prior['onsets']
    pitches=prior['pitches'];staffs=prior['staffs'];protected=set(protected_input_ids)
    ratios,uncertain=infer_beat_ratios(prior['model_grid'],reference)
    aligned,groups=align_model_onset_groups(positions,on,pitches,qd,reference,grid,
                                           protected_input_ids=protected,protected_anchor_seconds=protected_anchor_seconds,staffs=staffs,
                                           manual_collision_input_ids=manual_collision_input_ids)
    timing=classify_ratio_review(uncertain,positions,qd,on,aligned,audio_duration=reference.seconds[-1])
    aligned,sequence=preserve_model_sequences(positions,on,pitches,qd,reference,ratios,aligned,grid=grid,
        uncertain_intervals=timing['uncertain_intervals']+timing['diagnostic_intervals'],
        protected_input_ids=protected,protected_anchor_seconds=protected_anchor_seconds,
        excluded_input_ids=groups['preserved_run_input_ids'],staffs=staffs,
        original_model_rolls=prior['original_model_rolls'])
    durations=[float(Fraction(scale_model_duration(float(p),float(d),ratios)).limit_denominator(48))
               for p,d in zip(positions,qd)]
    _,rolls=compact_model_rolls(positions,on,prior['offsets'],pitches,staffs,durations,aligned,
                              reference,protected_input_ids=protected,protected_anchor_seconds=protected_anchor_seconds)
    for roll in sequence['original_model_rolls']:
        group=dict(roll,duration_quarters=durations[roll['input_ids'][0]],
                   before_quarters=[float(aligned[i]) for i in roll['input_ids']])
        rolls['groups']=[r for r in rolls['groups'] if not set(r['input_ids'])&set(group['input_ids'])]
        rolls['groups'].append(group)
    return dict(onsets=aligned,durations=durations,ratios=ratios,uncertain=uncertain,
                timing_review=timing,onset_group_alignment=groups,
                model_sequence_alignment=sequence,compact_model_rolls=rolls)


def compact_model_rolls(model_positions, onsets, offsets, pitches, staffs,
                        durations, aligned, tempo, *, protected_input_ids=(),protected_anchor_seconds=()):
    """Use DuET's model-agreed/off-grid roll idea with narrow reference guards.

    This is notation interpretation, not editing performance events. Unlike the
    upstream broad free-zone/playability heuristics it needs same model onset,
    one hand, overlapping held notes and a compact isolated attack. Ordinary
    grid runs and reviewed notes retain the already locked reference attacks.
    """
    groups = {}
    for i, (position, staff) in enumerate(zip(model_positions, staffs)):
        if float(durations[i]) > 0:
            groups.setdefault((round(float(position), 6), int(staff)), []).append(i)
    result = list(aligned); accepted = []; protected = set(protected_input_ids)
    raw = [tempo.to_beat(float(t)) for t in onsets]
    anchors=sorted(set(map(float,protected_anchor_seconds)))
    origin = float(tempo.data.get('grid_origin', tempo.data.get('pickup_beats', 0)))
    for (_, staff), ids in groups.items():
        ids.sort(key=lambda i: (float(onsets[i]), i))
        if not 2 <= len(ids) <= 5 or protected.intersection(ids):
            continue
        if len({int(pitches[i]) for i in ids}) != len(ids):
            continue
        values = [float(durations[i]) for i in ids]
        if max(values)-min(values) > 1e-9:
            continue  # Differently layered sustains need an explicit decision.
        steps = [int(pitches[b])-int(pitches[a]) for a,b in zip(ids,ids[1:])]
        if not (all(step > 0 for step in steps) or all(step < 0 for step in steps)):
            continue
        first, last = ids[0], ids[-1]
        spread = raw[last] - raw[first]
        seconds = float(onsets[last]) - float(onsets[first])
        if not (.03 < seconds <= .12 and .12 <= spread <= .32):
            continue
        gaps = [float(onsets[b])-float(onsets[a]) for a,b in zip(ids,ids[1:])]
        if len(gaps) > 1 and min(gaps) >= max(gaps)*.75:
            continue  # Evenly articulated fast figures remain written notes.
        # DuET's same_q + offgrid test, with explicit reference coordinates.
        # A clean 16th or triplet figure must not become a chord just because
        # the model assigned its notes the same onset.
        offgrid = max(min(abs((raw[i]-origin)-round((raw[i]-origin)/step)*step)
                          for step in (.25, 1/3)) for i in ids)
        if offgrid <= .05:
            continue
        if any(float(offsets[i]) < float(onsets[last]) + .04 for i in ids):
            continue
        # A rolled chord may contain seconds (e.g. G/Ab/Eb). Its model-agreed
        # common attack, sustain and non-regular timing are the evidence; a
        # minimum pitch interval would wrongly reject valid cluster voicings.
        outsiders = [i for i in range(len(onsets)) if int(staffs[i]) == staff
                     and i not in ids and float(durations[i]) > 0]
        if any(float(onsets[first]) <= float(onsets[i]) <= float(onsets[last]) for i in outsiders):
            continue
        before = [float(onsets[i]) for i in outsiders if float(onsets[i]) < float(onsets[first])]
        after = [float(onsets[i]) for i in outsiders if float(onsets[i]) > float(onsets[last])]
        if ((before and float(onsets[first])-max(before) < seconds*2)
                or (after and min(after)-float(onsets[last]) < seconds*2)):
            continue
        target = min(float(aligned[i]) for i in ids)
        # Roll consolidation is a separate projection stage. It must obey the
        # same fixed boundaries as the sequence stage that precedes it.
        if any(min(float(onsets[first]),tempo.to_seconds(target))-1e-8<=a<=
               max(float(onsets[last]),tempo.to_seconds(target))+1e-8 for a in anchors):
            continue
        from .notation_model_rhythm import _reference_bar
        if len({_reference_bar(tempo,q) for q in [target]+[raw[i] for i in ids]})!=1:
            continue
        if after and target + min(float(durations[i]) for i in ids) > tempo.to_beat(min(after)) + .1:
            continue
        for i in ids:
            result[i] = target
        accepted.append({'input_ids': ids, 'staff': staff, 'pitches': [int(pitches[i]) for i in ids],
                         'source_seconds': [float(onsets[i]) for i in ids],
                         'before_quarters': [float(aligned[i]) for i in ids],
                         'onset_quarters': target, 'spread_seconds': seconds,
                         'duration_quarters':values[0],
                         'direction':'up' if steps[0]>0 else 'down'})
    return result, {'method': 'duet_model_roll_reference_guard_v1', 'groups': accepted,
                    'raw_timestamps_modified': False}


@contextmanager
def _reference_attacks(upstream, roll_ids=(), omitted_ids=()):
    """DuET supplies note values/hands; it cannot reinterpret locked onsets."""
    from fractions import Fraction
    original_quantize, original_sanitize = upstream.quantize, upstream.sanitize
    def quantize(inp, grid, shift, ctx):
        notes = [n for n in original_quantize(inp, grid, shift, ctx) if n.get('i') not in omitted_ids]
        for note in notes:
            if not note['grace']:
                note['on'] = Fraction(str(float(inp.score['notes'][note['i'], 1]))).limit_denominator(7680)
                note['roll'] = note['i'] in roll_ids
        return notes
    def sanitize(notes, ctx):
        saved = {id(n): n['on'] for n in notes}
        original_sanitize(notes, ctx)
        for note in notes:
            note['on'] = saved[id(note)]
    upstream.quantize, upstream.sanitize = quantize, sanitize
    try:
        yield
    finally:
        upstream.quantize, upstream.sanitize = original_quantize, original_sanitize


def reference_bars(tempo, last_beat):
    """Explicit quarter-note bar geometry, including pickup and meter changes."""
    from .score_boundary import confirmed_end_beat
    end = confirmed_end_beat(tempo)
    if end is not None and last_beat > end + 1e-7:
        raise ValueError('记谱音符跨过已确认谱面终点；请后移或取消终点，或明确编辑该音符，不会自动裁尾')
    from .score_origin import score_opening_meter_rows
    signatures = score_opening_meter_rows(tempo.data)
    starts, lengths, meters = [], [], []
    pickup = float(tempo.data.get("pickup_beats", 0))
    first_full = float(tempo.data.get('first_full_bar_beat', 0))
    keep = tempo.data.get('opening_policy', {}).get('keep_before_bar1')
    if type(keep) is bool:
        if end is not None and end <= first_full + 1e-8:
            raise ValueError('Confirmed score end must follow bar 1')
        pickup = 0.
        last_beat = max(last_beat, first_full)
    # Verified leading silence is outside notation, not an empty pickup/intro.
    # The map remains in original source seconds and quarter coordinates.
    position = (tempo.to_beat(float(tempo.data['score_start_seconds']))
                if tempo.data.get('opening_mode') == 'normal'
                and tempo.data.get('score_start_seconds') is not None else 0.)
    if type(keep) is bool:
        position = 0. if keep else first_full
    short_ranges = [(tempo.to_beat(float(a['start_seconds'])), tempo.to_beat(float(a['end_seconds'])))
                    for a in tempo.data.get('short_bar_annotations', [])]
    short_boundaries = sorted({q for pair in short_ranges for q in pair})
    index = 0
    # A note ending exactly on a barline does not open one more (empty) bar.
    while (position < end - 1e-8 if end is not None else (position < last_beat - 1e-8 or not starts)):
        while index + 1 < len(signatures) and signatures[index + 1]["beat"] <= position + 1e-8:
            index += 1
        signature = signatures[index]
        if signature.get('free') and position >= tempo.beats[-1]-1e-8:break
        length = float(signature["numerator"] * 4 / signature["denominator"])
        if not starts and pickup:
            length = pickup
        next_change = float(signatures[index + 1]["beat"]) if index + 1 < len(signatures) else math.inf
        if position < first_full - 1e-8:
            next_change = min(next_change, first_full)
        for a, b in short_ranges:
            if abs(position-a) < 1e-7:
                length = b-a
                break
        following = next((q for q in short_boundaries if q > position+1e-7), math.inf)
        next_change = min(next_change, following)
        if next_change < position + length - 1e-7:
            # Explicit range edits can end with a partial bar. Preserve the
            # actual quarter span instead of stretching notes into a full bar.
            length = next_change - position
        if signature.get('free'):
            free_end=min(float(signatures[index+1]['beat']) if index+1<len(signatures) else tempo.beats[-1],end if end is not None else tempo.beats[-1])
            length=free_end-position
            if length<=1e-8:break
        meter = f"{signature['numerator']}/{signature['denominator']}"
        if end is not None and position + length > end + 1e-8:
            from .boundary_meter import boundary_time_signature
            length = end - position
            short = boundary_time_signature(length, (signature['denominator'],))
            meter = f"{short['numerator']}/{short['denominator']}"
        starts.append(position); lengths.append(length)
        meters.append(meter)
        position += length
    return starts, lengths, meters


@contextmanager
def _reference_grid(upstream, grid):
    """One isolated process/run only; no upstream files or model logic changed."""
    from fractions import Fraction
    original = upstream.build_grid
    original_ctx = upstream.Ctx
    original_analyse = upstream.analyse_meter
    original_bars = upstream.map_model_bars
    class ReferenceContext(original_ctx):
        # Native pickup means *discard this leading empty span*. Our explicit
        # reference bars already express any short pickup measure; a first MIDI
        # note after silence must not shift the entire engraved timeline left.
        @property
        def pickup(self):
            return Fraction(0)
        @pickup.setter
        def pickup(self, value):
            pass
    def zero_shift(_):
        return 0.
    zero_shift.free = lambda _: False
    def locked(*args, **kwargs):
        return grid.copy(), zero_shift
    def explicit_meter(score, onsets, log):
        from collections import Counter
        from duet.readable.meter import parse
        import numpy as np
        meters = [str(value) for value in score['meters']]
        main = parse(Counter(meters).most_common(1)[0][0])
        bars = [(float(start), *(int(v) for v in meter.split('/')), float(length))
                for start, length, meter in zip(score['bar_starts'], score['bar_lengths'], meters)]
        log('reference meter: explicit bar geometry retained; no meter reinterpretation')
        return main, np.array(score['notes'][:, 1], copy=True), np.array(score['notes'][:, 2], copy=True), lambda p:p, bars
    def explicit_bars(model_bars, q_model, onsets, shift_at, last_on, ctx, quiet=False):
        from duet.readable.context import Bars
        # A short explicit range boundary is a partial bar, never evidence for
        # rewriting the requested 4/4 as 3/4 or 2/4.
        starts = [Fraction(str(bar[0])).limit_denominator(7680) for bar in model_bars]
        lengths = [Fraction(str(bar[3])).limit_denominator(7680) for bar in model_bars]
        return Bars(starts, [(bar[1], bar[2]) for bar in model_bars], lengths)
    upstream.build_grid = locked
    upstream.Ctx = ReferenceContext
    upstream.analyse_meter = explicit_meter
    upstream.map_model_bars = explicit_bars
    try:
        yield
    finally:
        upstream.build_grid = original
        upstream.Ctx = original_ctx
        upstream.analyse_meter = original_analyse
        upstream.map_model_bars = original_bars


def _hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as file:
        for block in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _json_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def validate_notation_revisions(revisions, *, input_sha256, prediction_sha256, reference, data):
    """Bind explicit reviewed shortening/omission edits to one performance.

    IDs are meaningless without the input/prediction identity. Expected pitch,
    raw timestamps and printed onset/duration also guard against stale edits.
    This deliberately does not infer edits or change performance MIDI.
    """
    if revisions is None:
        return None
    from fractions import Fraction
    if not isinstance(revisions, dict) or revisions.get('version') != 1:
        raise ValueError('Unsupported notation revision format')
    for field, expected in (('input_sha256', input_sha256),
                            ('prediction_sha256', prediction_sha256),
                            ('reference_sha256', _json_hash(reference))):
        if revisions.get(field) != expected:
            raise ValueError(f'Notation revision identity mismatch: {field}; recheck edits for this input')
    changes = revisions.get('notes')
    if not isinstance(changes, list) or not changes:
        raise ValueError('Notation revisions require an explicit nonempty notes list')
    result = copy.deepcopy(revisions)
    seen = set()
    for change in result['notes']:
        if not isinstance(change, dict):
            raise ValueError('Invalid notation revision entry')
        i = change.get('input_id')
        if type(i) is not int or not 0 <= i < len(data['ref_pitch']) or i in seen:
            raise ValueError('Invalid or duplicate notation revision input ID')
        seen.add(i)
        if type(change.get('pitch')) is not int or change['pitch'] != int(data['ref_pitch'][i]):
            raise ValueError(f'Notation revision pitch mismatch for input {i}')
        for field, source in (('raw_start_seconds', 'ref_onset'), ('raw_end_seconds', 'ref_offset')):
            value = change.get(field)
            if type(value) not in (int, float) or not math.isfinite(value) or abs(value - float(data[source][i])) > 1e-6:
                raise ValueError(f'Notation revision timestamp mismatch for input {i}')
        action = change.get('action', 'shorten')
        if action not in ('shorten', 'omit'):
            raise ValueError('Unsupported notation revision action')
        fields = ('printed_start_beat', 'expected_duration_quarters')
        if action == 'shorten':
            fields += ('duration_quarters',)
        elif 'duration_quarters' in change:
            raise ValueError('Omission must not also change duration')
        for field in fields:
            value = change.get(field)
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                raise ValueError(f'Invalid notation revision {field}')
        if action == 'shorten':
            duration = change['duration_quarters']
            if not 0 < duration < change['expected_duration_quarters']:
                raise ValueError('Reviewed duration edits currently support explicit shortening only')
            if abs(float(Fraction(duration).limit_denominator(48)) - duration) > 1e-8:
                raise ValueError('Notation revision duration is not on a supported notation grid')
        if not isinstance(change.get('reason'), str) or not change['reason'].strip():
            raise ValueError('Notation revision needs a review reason')
    result['notes'].sort(key=lambda item: item['input_id'])
    return result


@contextmanager
def _reviewed_durations(upstream, revisions):
    """Apply explicit reviewed edits after native cleanup, before layout.

    Omission here cannot lengthen or shift a neighbouring note by changing the
    upstream voice assignment. This is a user edit, not a model accuracy claim.
    """
    from fractions import Fraction
    expected = {n['input_id']: n for n in (revisions or {}).get('notes', [])}
    applied = {}
    original = upstream.assign
    def assign(notes, staff, ctx):
        pool = original(notes, staff, ctx)
        for note in pool:
            change = expected.get(note.get('i'))
            if change is None:
                continue
            if (note.get('grace') or note['pitch'] != change['pitch']
                    or abs(float(note['on']) - change['printed_start_beat']) > 1e-8
                    or abs(float(note['dur']) - change['expected_duration_quarters']) > 1e-8):
                raise ValueError(f'Stale notation revision at input {note.get("i")}: printed event changed')
            if change.get('action', 'shorten') == 'shorten':
                note['dur'] = Fraction(change['duration_quarters']).limit_denominator(48)
            applied[note['i']] = dict(change, applied_after='native_voice_and_hold_cleanup')
        return [note for note in pool if expected.get(note.get('i'), {}).get('action') != 'omit']
    upstream.assign = assign
    try:
        yield applied
        if set(applied) != set(expected):
            raise ValueError(f'Notation revision inputs were not engraved: {sorted(set(expected) - set(applied))}')
    finally:
        upstream.assign = original


def _review_after_revisions(review, applied):
    if not applied:
        return review
    result = copy.deepcopy(review)
    result['pre_revision_affected_note_indices'] = result['affected_note_indices'][:]
    result['notation_revisions'] = list(applied.values())
    result['user_resolved_intervals'] = []
    unresolved = []
    for item in result['uncertain_intervals']:
        before = item['affected_note_indices'][:]
        item['reviewed_note_indices'] = [i for i in before if i in applied]
        item['pre_revision_affected_note_indices'] = before
        item['affected_note_indices'] = [i for i in before if i not in applied]
        item['affected_note_count'] = len(item['affected_note_indices'])
        (unresolved if item['affected_note_count'] else result['user_resolved_intervals']).append(item)
    result['uncertain_intervals'] = unresolved
    result['affected_note_indices'] = sorted({i for item in unresolved for i in item['affected_note_indices']})
    result['affected_note_count'] = len(result['affected_note_indices'])
    return result


def _model_grid(upstream, score, on, frames, cfg, logs, analyse, build_grid):
    """DuET's own grid, used only to classify rolls in direct mode."""
    import numpy as np
    meter, q, qd, _, model_bars = analyse(score, on, logs.append)
    if model_bars is not None and cfg.snap_bar_lines:
        model_bars = upstream.snap_model_bars(model_bars, q, logs.append)
    grid_bars = None if model_bars is None else (np.array([b[0] for b in model_bars]), np.array([b[3] for b in model_bars]))
    model_grid, shift_at = build_grid(frames, on, q, meter.bar_q, cfg, logs.append,
                                     pulse=meter.pulse_q, bars=grid_bars)
    return meter, q, qd, model_grid, shift_at, model_bars


def _direct_positions(cache, reference_data, prediction_sha256):
    """Written positions when DuET wrote on exactly this beat map, else None.

    Two cases: a constrained re-decode on this map (constrained.json), or the
    DuET first-pass map built from this very prediction (notation_binding).
    A constrained cache for another map is an error, never silently reused.
    """
    from .duet_beat_map import anchors_digest, binding_matches
    marker = Path(cache) / 'constrained.json'
    if marker.is_file():
        data = json.loads(marker.read_text(encoding='utf-8'))
        if data.get('anchors_sha256') != anchors_digest(reference_data):
            raise ValueError('该 DuET 谱是按另一套拍点写的；请按当前拍点重新写谱')
        return {'method': 'duet_constrained_decode', 'positions': data['positions_quarters'],
                'durations': data['durations_quarters'], 'bar_starts': data['bar_starts']}
    if binding_matches(reference_data, prediction_sha256):
        return {'method': 'duet_first_pass_map', 'offset': float(reference_data['notation_binding']['quarter_offset'])}
    return None


def _direct_projection(direct, prior, reference, *, protected_input_ids=(), protected_anchor_seconds=()):
    """Use DuET's written positions as notation coordinates; no regrid/snap."""
    from fractions import Fraction
    count = len(prior['onsets'])
    if direct['method'] == 'duet_first_pass_map':
        onsets = [float(p) + direct['offset'] for p in prior['positions']]
        durations = [float(d) for d in prior['durations']]
    else:
        onsets = [float(v) for v in direct['positions']]
        durations = [float(v) for v in direct['durations']]
    if len(onsets) != count or len(durations) != count:
        raise ValueError('DuET 写谱位置与输入音符数量不一致')
    durations = [float(Fraction(d).limit_denominator(48)) for d in durations]
    _, rolls = compact_model_rolls(onsets, prior['onsets'], prior['offsets'], prior['pitches'], prior['staffs'],
                                   durations, onsets, reference, protected_input_ids=protected_input_ids,
                                   protected_anchor_seconds=protected_anchor_seconds)
    for roll in prior['original_model_rolls']:
        ids = roll['input_ids']
        if (len({round(onsets[i], 7) for i in ids}) == 1 and len({int(prior['staffs'][i]) for i in ids}) == 1
                and not set(ids) & set(protected_input_ids)):
            rolls['groups'] = [r for r in rolls['groups'] if not set(r['input_ids']) & set(ids)]
            rolls['groups'].append(dict(roll, staff=int(prior['staffs'][ids[0]]),
                                        pitches=[int(prior['pitches'][i]) for i in ids],
                                        source_seconds=[float(prior['onsets'][i]) for i in ids],
                                        duration_quarters=durations[ids[0]],
                                        before_quarters=[onsets[i] for i in ids], onset_quarters=onsets[ids[0]]))
    timing = {'uncertain_intervals': [], 'diagnostic_intervals': [], 'context_resolved_intervals': [],
              'original_uncertain_intervals': [], 'affected_note_indices': [], 'affected_note_count': 0,
              'message': 'DuET 已按当前拍点写谱，无需拍值倍率核对。'}
    return dict(onsets=onsets, durations=durations, ratios=[1.], uncertain=[], timing_review=timing,
                onset_group_alignment={'method': direct['method'], 'changed_note_count': 0, 'groups': [],
                                       'preserved_run_input_ids': [], 'raw_timestamps_modified': False},
                model_sequence_alignment={'method': direct['method'], 'accepted': [], 'rejected': [],
                                          'original_model_rolls': [], 'changed_note_count': 0,
                                          'raw_timestamps_modified': False},
                compact_model_rolls=rolls)


def refine_duet_notation(raw_midi, duet_cache_dir, tempo_map, output_dir, *,
                         title=None, show_pedal=False, grid="auto", engraving_config=None,
                         notation_revisions=None, protected_note_onsets=None,
                         protected_anchor_seconds=None):
    """Create a clock-constrained MusicXML in its own hash-keyed cache directory.

    Run in the existing score subprocess, not concurrently in a UI process:
    the scoped upstream function binding fixes only the engraving beat clock.
    Input MIDI, raw model predictions and all aligned MIDI exports stay intact.
    """
    import numpy as np
    import pandas as pd
    from .duet_score import SOURCE_COMMIT, duet_source_dir
    source = duet_source_dir()
    sys.path.insert(0, str(source / "src"))
    from duet.midi import read_performance_midi
    from duet.readable.inputs import Inputs
    from duet.readable.config import Config
    from duet.readable import pipeline as upstream
    from duet.readable.meter import analyse
    from duet.readable.grid import build_grid, fix_rewinds

    midi = Path(raw_midi).resolve(strict=True)
    cache = Path(duet_cache_dir).resolve(strict=True)
    model_manifest = cache / "astra-cache.json"
    if model_manifest.is_file():
        recorded = json.loads(model_manifest.read_text(encoding="utf-8"))
        if recorded.get("identity", {}).get("input_sha256") != _hash(midi):
            raise ValueError("DuET cache belongs to a different performance MIDI")
    reference_data = copy.deepcopy(tempo_map.data if isinstance(tempo_map, TempoMap) else tempo_map)
    reference = TempoMap(reference_data)
    from .notation_model_rhythm import validate_protection,preserve_model_sequences,match_protected_notes,VERSION as SEQUENCE_VERSION
    protected_note_onsets,protected_anchor_seconds=validate_protection(protected_note_onsets,protected_anchor_seconds)
    options = dict(engraving_config or {})
    # Keep native duration/voice cleanup. Only beat-clock reinterpretation is
    # disabled after the explicit reference conversion has been applied.
    options.update(grid_count_fix=False, half_bar_shift=False, snap_bar_lines=False,
                   retime_outliers=False, auto_gliss=False, auto_runs=False,
                   auto_chords=False, triplet_context=False, compound_snap=False,
                   swing_min_notes=1000000000, roll_gap=0.0)
    cfg = Config.resolve(options)
    from .score_numbering import VERSION as NUMBERING_VERSION
    identity = {"version": VERSION, "numbering_version": NUMBERING_VERSION, "source_commit": SOURCE_COMMIT,
                "input_sha256": _hash(midi), "prediction_sha256": _hash(cache / "score.npz"),
                "frames_sha256": _hash(cache / "frames.csv"), "reference": reference_data,
                "title": title or midi.stem, "show_pedal": bool(show_pedal),
                "onset_grid": grid, "engraving_config": options,
                "protected_note_onsets":protected_note_onsets,
                "protected_anchor_seconds":protected_anchor_seconds,
                "model_sequence_version":SEQUENCE_VERSION}
    data = read_performance_midi(midi)
    revisions = validate_notation_revisions(notation_revisions,
        input_sha256=identity['input_sha256'], prediction_sha256=identity['prediction_sha256'],
        reference=reference_data, data=data)
    identity['notation_revisions'] = revisions
    cache_key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    base = Path(output_dir).resolve();base.mkdir(parents=True, exist_ok=True)
    destination = base / cache_key
    def result(cached):
        return {"musicxml": str(destination / "score.musicxml"),
                "report": str(destination / "notation-report.json"),
                "review": str(destination / "score.review.json"),
                "timing_review": str(destination / "timing-review.json"),
                "cache_key": cache_key, "cached": cached}
    manifest_path = destination / "notation-report.json"
    if manifest_path.is_file():
        prior = json.loads(manifest_path.read_text(encoding="utf-8"))
        if (prior.get("identity") == identity and prior.get("status") == "complete"
                and all((destination / name).is_file() and _hash(destination / name) == digest
                        for name, digest in prior.get("artifacts", {}).items())
                and prior.get("artifacts")):
            return result(True)
    cleanup = data["midi_input_cleanup"]
    if cleanup["input_note_on_events"] != cleanup["retained_notes"]:
        raise ValueError("Invalid source MIDI would discard notes")
    with np.load(cache / "score.npz", allow_pickle=False) as bundle:
        score = {name: np.array(bundle[name], copy=True) for name in
                 ("notes", "bar_starts", "bar_lengths", "meters", "voices", "keys")}
    if len(score["notes"]) != len(data["ref_pitch"]) or not np.array_equal(score["notes"][:, 0], data["ref_pitch"]):
        raise ValueError("Cached DuET predictions do not match the input MIDI notes")
    frames = pd.read_csv(cache / "frames.csv")
    on = data["ref_onset"]
    upstream_report = json.loads((cache / "report.json").read_text(encoding="utf-8"))
    original_cfg = Config.resolve(upstream_report.get("engraving", {}).get("config", {}))
    logs = []
    direct = _direct_positions(cache, reference_data, identity['prediction_sha256'])
    meter, q, qd, _, model_bars = analyse(score, on, logs.append)
    model_grid = shift_at = None
    if direct is not None:
        try:
            meter, q, qd, model_grid, shift_at, model_bars = _model_grid(
                upstream, score, on, frames, original_cfg, logs, analyse, build_grid)
        except (ValueError, IndexError, ZeroDivisionError) as exc:
            logs.append(f'model grid unavailable in direct mode: {exc}')
            model_grid = shift_at = None
        ratios, uncertain = [1.], []
    elif model_bars is not None and original_cfg.snap_bar_lines:
        model_bars = upstream.snap_model_bars(model_bars, q, logs.append)
    if direct is None and model_bars is None:
        fbeats = None
        if meter.pulse_q == 1:
            from scipy.signal import find_peaks
            peaks, _ = find_peaks(frames.p_any_beat.values, height=original_cfg.rewind_frame_conf,
                                 distance=original_cfg.beat_peak_distance)
            fbeats = (frames.performance_time_sec.values[peaks], frames.p_downbeat.values[peaks] > .5)
        q = fix_rewinds(on, q, float(meter.bar_q), original_cfg, logs.append, fbeats)
    if direct is None:
        grid_bars = None if model_bars is None else (np.array([b[0] for b in model_bars]), np.array([b[3] for b in model_bars]))
        model_grid, shift_at = build_grid(frames, on, q, meter.bar_q, original_cfg, logs.append,
                                         pulse=meter.pulse_q, bars=grid_bars)
        ratios, uncertain = infer_beat_ratios(model_grid, reference)
    if model_grid is not None:
        positions = q + np.asarray([shift_at(t) for t in on])
        if direct is None and np.any(positions < 0):
            raise ValueError("Model positions before zero require manual review")
        native_rolls=original_model_rolls(upstream,score,data,frames,q,qd,meter,model_bars,
                                        model_grid,shift_at,original_cfg)
    else:
        positions = np.asarray(q, float); native_rolls = []
    reviewed_ids={item['input_id'] for item in (revisions or {}).get('notes',[])}
    # Share exact source-MIDI protection with every notation projection stage.
    manual_ids=match_protected_notes(protected_note_onsets,on,data['ref_pitch'])
    protected_ids=reviewed_ids | manual_ids
    projection_prior=dict(version=1,kind='original_duet_prediction',
        positions=list(map(float,positions)),durations=list(map(float,qd)),
        onsets=list(map(float,on)),offsets=list(map(float,data['ref_offset'])),
        pitches=list(map(int,data['ref_pitch'])),staffs=list(map(int,score['notes'][:,3])),
        model_grid=list(map(float,model_grid)) if model_grid is not None else [],original_model_rolls=native_rolls)
    if direct is not None:
        projected=_direct_projection(direct, projection_prior, reference,
            protected_input_ids=protected_ids, protected_anchor_seconds=protected_anchor_seconds)
    else:
        projected=project_model_notation(projection_prior,reference,grid=grid,
            protected_input_ids=protected_ids,protected_anchor_seconds=protected_anchor_seconds,
            manual_collision_input_ids=manual_ids - reviewed_ids)
    ratios,uncertain=projected['ratios'],projected['uncertain']
    timing_review=projected['timing_review'];onset_group_review=projected['onset_group_alignment']
    sequence_review=projected['model_sequence_alignment'];roll_review=projected['compact_model_rolls']
    converted=score['notes'].copy()
    converted[:,1]=projected['onsets'];converted[:,2]=projected['durations']
    omitted_ids = set()
    opening = reference_data.get('score_start_seconds')
    if opening is not None and reference_data.get('opening_policy', {}).get('score_only'):
        from .score_origin import manual_opening_span
        for i, (start, end) in enumerate(zip(on, data['ref_offset'])):
            span = manual_opening_span(reference, start, end, converted[i, 1], converted[i, 1]+converted[i, 2])
            if span is None:
                omitted_ids.add(i)
            else:
                converted[i, 1], stop = span
                converted[i, 2] = stop-converted[i, 1]
    remaining = [i for i in range(len(converted)) if i not in omitted_ids]
    if not remaining:
        raise ValueError('第 1 小节起点之后没有可制谱的音符。')
    last_written = float(np.max(converted[remaining, 1] + converted[remaining, 2]))
    starts, lengths, meters = reference_bars(reference, last_written)
    # Key priors use the same clock as the written positions. A direct decode
    # does not need an acoustic model grid, which may not exist for this score.
    if direct is None:
        original_starts = np.asarray(score["bar_starts"], float)
        key_positions = [_beat_at(reference, float(np.interp(b, np.arange(len(model_grid)), model_grid))) for b in original_starts]
    elif direct['method'] == 'duet_first_pass_map':
        key_positions = [float(b) + direct['offset'] for b in score['bar_starts']]
    else:
        # The assembled score can advance empty bars by inferred lengths. The
        # constrained marker retains each decode bar's exact reference start.
        key_positions = direct['bar_starts']
    mapped_keys = [int(score["keys"][max(0, min(len(score["keys"]) - 1, bisect_right(key_positions, b) - 1))]) for b in starts]
    # Engrave relative to bar 1 (a rational origin); canonical XML receives
    # its precise source-time prefix after layout. Analysis stays absolute.
    layout_origin = reference.to_beat(opening) if opening is not None and reference_data.get('opening_policy', {}).get('score_only') else 0.
    if layout_origin:
        converted[:, 1] = np.maximum(0., converted[:, 1] - layout_origin)
        starts = [v-layout_origin for v in starts]
    score.update(notes=converted, bar_starts=np.array(starts), bar_lengths=np.array(lengths),
                 meters=np.array(meters), keys=np.array(mapped_keys), is_grace=converted[:, 2] == 0)
    perf = list(zip(data["ref_onset"], data["ref_offset"], data["ref_pitch"], data["ref_vel"]))
    # Visibility is presentation only; keep performance data available upstream.
    pedal = list(zip(data["pedal_time"], data["pedal_value"] * 127))
    inputs = Inputs(perf=perf, pedal=pedal, score=score, frames=frames)
    limit = math.ceil(max(starts[-1] + lengths[-1], last_written-layout_origin)) + 32
    locked_grid = np.array([reference.to_seconds(float(b)+layout_origin) for b in range(limit + 1)])
    with tempfile.TemporaryDirectory(prefix=".notation-pending-", dir=base) as temp:
        pending = Path(temp)
        from .notation_readability import readable_writer
        from .notation_precision import reference_rhythm
        origin = 0. if layout_origin else reference_data.get("grid_origin", reference_data.get("pickup_beats", 0))
        with fast_readable_context(upstream), _reference_grid(upstream, locked_grid), reference_rhythm(upstream, origin) as rhythm, \
                _reference_attacks(upstream, omitted_ids=omitted_ids), readable_writer(upstream) as readability, _without_playability_arrangement(upstream):
            engraved_revisions = dict(revisions, notes=[n for n in revisions['notes'] if n['input_id'] not in omitted_ids]) if revisions and omitted_ids else revisions
            with _reviewed_durations(upstream, engraved_revisions) as applied_revisions:
                xml, summary = upstream.run(inputs, pending, title=identity["title"], cfg=cfg)
        from .score_origin import restore_source_origin
        source_origin_report = restore_source_origin(xml, reference_data)
        from .score_numbering import apply_measure_numbering
        numbering_report = apply_measure_numbering(xml, reference_data, pending / "score.review.json")
        reference_tempo_report = None
        if reference_data.get('source') in ('imported_midi', 'manual'):
            from .score_tempo import apply_reference_tempo
            reference_tempo_report = apply_reference_tempo(xml, reference_data)
        timing_review = _review_after_revisions(timing_review, applied_revisions)
        root = ET.parse(xml).getroot()
        if not show_pedal:
            for measure in root.findall('.//measure'):
                for direction in measure.findall('direction'):
                    for kind in direction.findall('direction-type'):
                        for mark in kind.findall('pedal'):
                            kind.remove(mark)
                        if len(kind) == 0:
                            direction.remove(kind)
                    if direction.find('direction-type') is None:
                        measure.remove(direction)
            ET.ElementTree(root).write(xml, encoding='utf-8', xml_declaration=True)
        if not root.findall(".//note/pitch"):
            raise ValueError("Constrained engraving produced no notes")
        if not show_pedal and root.findall(".//pedal"):
            raise ValueError("Pedal visibility was not respected")
        (pending / "timing-review.json").write_text(json.dumps(timing_review, ensure_ascii=False, indent=2), encoding="utf-8")
        report = {"status": "complete", "identity": identity, "input_notes": len(on),
                  "model_duration_notes_rescaled": int(np.count_nonzero(abs(converted[:, 2] - qd) > 1e-7)),
                  "beat_ratios": {str(r): ratios.count(r) for r in (.5, 1., 2.)},
                  "uncertain_ratio_intervals": len(timing_review["uncertain_intervals"]),
                  "raw_uncertain_ratio_intervals": len(uncertain),
                  "context_resolved_ratio_intervals": len(timing_review["context_resolved_intervals"]),
                  "inactive_ratio_intervals": len(timing_review["diagnostic_intervals"]),
                  "affected_note_indices": timing_review["affected_note_indices"],
                  "affected_note_count": timing_review["affected_note_count"], "engraving": summary,
                  "readability": readability, "reference_rhythm": rhythm,
                  "onset_group_alignment": onset_group_review,
                  "model_sequence_alignment":sequence_review,
                  "original_model_roll_priors":native_rolls,
                  "projection_prior":projection_prior,
                  "compact_model_rolls": roll_review,
                  "notation_revisions": list(applied_revisions.values()),
                  "reference_midi_tempo": reference_tempo_report,
                  "source_origin": source_origin_report,
                  "measure_numbering": numbering_report,
                  "pedal_visible": bool(show_pedal), "raw_midi_unchanged": _hash(midi) == identity["input_sha256"],
                  "artifacts": {name: _hash(pending / name) for name in ("score.musicxml", "score.review.json", "timing-review.json")}}
        if not report["raw_midi_unchanged"]:
            raise ValueError("Input MIDI changed during engraving")
        (pending / "notation-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        if destination.exists():
            raise ValueError("Existing notation cache is invalid; choose another output directory")
        destination.mkdir()
        for artifact in pending.iterdir():
            artifact.replace(destination / artifact.name)
    return result(False)


def main():
    if len(sys.argv) == 2:
        request = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
        print("[notation] 复用 DuET 预测，按参考速度图重新制谱", flush=True)
        value = refine_duet_notation(**request)
        output = Path(request["output_dir"])
        (output / "result.json").write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(value, ensure_ascii=False), flush=True)
        return
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("midi", type=Path)
    parser.add_argument("duet_cache", type=Path)
    parser.add_argument("tempo", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--title")
    parser.add_argument("--show-pedal", action="store_true")
    parser.add_argument("--grid", choices=("auto", "straight", "triplet", "fine"), default="auto")
    parser.add_argument("--revisions", type=Path, help="Identity-bound reviewed notation corrections JSON")
    args = parser.parse_args()
    value = refine_duet_notation(args.midi, args.duet_cache,
                                json.loads(args.tempo.read_text(encoding="utf-8")), args.output,
                                title=args.title, show_pedal=args.show_pedal, grid=args.grid,
                                notation_revisions=json.loads(args.revisions.read_text(encoding='utf-8')) if args.revisions else None)
    print(json.dumps(value, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
