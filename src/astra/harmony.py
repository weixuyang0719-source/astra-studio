"""Conservative, local harmony annotations for an already-notated MusicXML score.

The existing key signatures are boundaries, not suggestions. Profile correlations
are evidence scores, never probabilities. Analysis does not change pitches,
durations, voices, accidentals, or key signatures; optional XML edits only add
mode metadata and chord symbols. All heavy imports are local to the public API.
"""
from __future__ import annotations

from collections import Counter, defaultdict, deque
from copy import deepcopy
from pathlib import Path
import math
import xml.etree.ElementTree as ET


PROFILES = ("AardenEssen", "KrumhanslSchmuckler", "TemperleyKostkaPayne")


def _number(value: str):
    try:
        return int(value)
    except ValueError:
        return value


def _label(tonic: str, mode: str) -> str:
    return tonic.replace("-", "b") + (" 小调" if mode == "minor" else " 大调")


def _key_evidence(segment, fifths=None):
    from music21 import key

    pitch_classes = {p.pitchClass for n in segment.recurse().notes for p in n.pitches}
    count = len(list(segment.recurse().notes))
    pools = {}
    winners = Counter()
    for method in PROFILES:
        try:
            result = segment.analyze(method)
        except Exception:
            continue
        winner_id = (result.tonic.pitchClass, result.mode)
        winners[winner_id] += 1
        for candidate in [result, *result.alternateInterpretations]:
            corr = float(candidate.correlationCoefficient)
            if not math.isfinite(corr):
                continue
            ident = (candidate.tonic.pitchClass, candidate.mode)
            item = pools.setdefault(ident, {"tonic": candidate.tonic.name,
                                           "mode": candidate.mode,
                                           "fifths": candidate.sharps,
                                           "profile_scores": {}})
            item["profile_scores"][method] = round(corr, 5)
    candidates = []
    for ident, item in pools.items():
        item["correlation"] = round(sum(item["profile_scores"].values()) /
                                    len(item["profile_scores"]), 5)
        item["profile_votes"] = winners[ident]
        item["label"] = _label(item["tonic"], item["mode"])
        candidates.append(item)
    candidates.sort(key=lambda item: item["correlation"], reverse=True)
    if fifths is not None:
        # Preserve the signature's written tonic spelling, including flat keys.
        allowed = { (key.KeySignature(fifths).asKey(mode).tonic.pitchClass, mode):
                    key.KeySignature(fifths).asKey(mode).tonic.name
                    for mode in ("major", "minor") }
        selected = [dict(item, tonic=allowed[(key.Key(item["tonic"], item["mode"]).tonic.pitchClass,
                                               item["mode"])])
                    for item in candidates
                    if (key.Key(item["tonic"], item["mode"]).tonic.pitchClass, item["mode"]) in allowed]
        for item in selected:
            item["label"] = _label(item["tonic"], item["mode"])
            item["fifths"] = fifths
    else:
        selected = candidates
    if not selected:
        return {"tonic": None, "mode": "unknown", "label": "调性待确认", "fifths": fifths,
                "uncertain": True, "candidates": [], "note_events": count}
    best = selected[0]
    margin = best["correlation"] - (selected[1]["correlation"] if len(selected) > 1 else 0)
    certain = (count >= 8 and len(pitch_classes) >= 4 and best["correlation"] >= .55
               and margin >= .08 and best["profile_votes"] >= 2)
    return {"tonic": best["tonic"] if certain else None,
            "mode": best["mode"] if certain else "unknown",
            "fifths": best["fifths"] if fifths is None else fifths,
            "label": best["label"] if certain else "调性待确认",
            "uncertain": not certain, "correlation": best["correlation"],
            "correlation_margin": round(margin, 5), "candidates": selected[:4],
            "unrestricted_candidates": candidates[:3], "note_events": count}


def _region_evidence(segment, fifths, declared_mode=None):
    """Explicit source mode changes are boundaries, even with identical fifths."""
    evidence = _key_evidence(segment, fifths)
    if declared_mode in ('major', 'minor') and fifths is not None:
        from music21 import key
        declared = key.KeySignature(fifths).asKey(declared_mode)
        consistent = (evidence['mode'] == declared_mode) if not evidence['uncertain'] else None
        evidence['mode_analysis'] = {
            name: evidence.get(name) for name in
            ('tonic', 'mode', 'uncertain', 'correlation', 'correlation_margin')}
        evidence.update(tonic=declared.tonic.name, mode=declared_mode,
                        label=_label(declared.tonic.name, declared_mode), uncertain=False,
                        declared_mode=declared_mode, mode_source='explicit_source',
                        analysis_matches_declaration=consistent)
    else:
        evidence.update(declared_mode=None, mode_source='profile_analysis')
    return evidence


def apply_key_confirmation(report: dict, confirmed_key: dict | None) -> dict:
    """Attach user evidence without replacing or disguising the analysis."""
    if (isinstance(confirmed_key, dict) and confirmed_key.get('tonic')
            and confirmed_key.get('mode') in ('major', 'minor')):
        detected = report.setdefault('global_key', {})
        detected['user_confirmation'] = {
            'tonic': confirmed_key['tonic'], 'mode': confirmed_key['mode'], 'source': 'user',
            'matches_analysis': (confirmed_key['tonic'], confirmed_key['mode']) ==
                                (detected.get('tonic'), detected.get('mode'))}
    return report


def _analysis_stream(score, lo=0, hi=None):
    from music21 import stream, harmony

    result = stream.Stream()
    for part in score.parts:
        measures = list(part.getElementsByClass(stream.Measure))
        for measure in measures[lo:hi]:
            for n in measure.recurse().notes:
                if not isinstance(n, harmony.Harmony):
                    # Durations are weighted by music21's profiles. Tied segments
                    # need not be merged: their combined weight is unchanged.
                    result.append(deepcopy(n))
    return result


def _key_evidence_from_distribution(pc_weights, count, fifths=None):
    """Exact music21 KeyWeightKeyAnalysis using pre-accumulated 12-PC duration weights."""
    from music21 import key, pitch
    from music21.analysis import discrete

    analyzers = (
        ("AardenEssen", discrete.AardenEssen()),
        ("KrumhanslSchmuckler", discrete.KrumhanslSchmuckler()),
        ("TemperleyKostkaPayne", discrete.TemperleyKostkaPayne()),
    )
    pitch_classes = {pc for pc, w in enumerate(pc_weights) if w > 0}
    pools = {}
    winners = Counter()
    if count and any(w > 0 for w in pc_weights):
        for method, an in analyzers:
            try:
                maj_corrs = an._getDifference(True, pc_weights, "major")
                min_corrs = an._getDifference(True, pc_weights, "minor")
            except Exception:
                continue
            sort_list = [(maj_corrs[pc], pitch.Pitch(pc), "major") for pc in range(12)]
            sort_list += [(min_corrs[pc], pitch.Pitch(pc), "minor") for pc in range(12)]
            sort_list.sort()
            sort_list.reverse()
            cands = []
            for corr, p_obj, mode in sort_list:
                valid_set = an.keysValidMajor if mode == "major" else an.keysValidMinor
                if p_obj.name not in valid_set:
                    p_obj = p_obj.getEnharmonic()
                k_obj = key.Key(tonic=p_obj, mode=mode)
                cands.append((k_obj, float(corr)))
            win_k = cands[0][0]
            winners[(win_k.tonic.pitchClass, win_k.mode)] += 1
            for k_obj, corr in cands:
                if not math.isfinite(corr):
                    continue
                ident = (k_obj.tonic.pitchClass, k_obj.mode)
                item = pools.setdefault(ident, {"tonic": k_obj.tonic.name,
                                               "mode": k_obj.mode,
                                               "fifths": k_obj.sharps,
                                               "profile_scores": {}})
                item["profile_scores"][method] = round(corr, 5)
    candidates = []
    for ident, item in pools.items():
        item["correlation"] = round(sum(item["profile_scores"].values()) /
                                    len(item["profile_scores"]), 5)
        item["profile_votes"] = winners[ident]
        item["label"] = _label(item["tonic"], item["mode"])
        candidates.append(item)
    candidates.sort(key=lambda item: item["correlation"], reverse=True)
    if fifths is not None:
        allowed = {(key.KeySignature(fifths).asKey(mode).tonic.pitchClass, mode):
                   key.KeySignature(fifths).asKey(mode).tonic.name
                   for mode in ("major", "minor")}
        selected = [dict(item, tonic=allowed[(key.Key(item["tonic"], item["mode"]).tonic.pitchClass,
                                               item["mode"])])
                    for item in candidates
                    if (key.Key(item["tonic"], item["mode"]).tonic.pitchClass, item["mode"]) in allowed]
        for item in selected:
            item["label"] = _label(item["tonic"], item["mode"])
            item["fifths"] = fifths
    else:
        selected = candidates
    if not selected:
        return {"tonic": None, "mode": "unknown", "label": "调性待确认", "fifths": fifths,
                "uncertain": True, "candidates": [], "note_events": count}
    best = selected[0]
    margin = best["correlation"] - (selected[1]["correlation"] if len(selected) > 1 else 0)
    certain = (count >= 8 and len(pitch_classes) >= 4 and best["correlation"] >= .55
               and margin >= .08 and best["profile_votes"] >= 2)
    return {"tonic": best["tonic"] if certain else None,
            "mode": best["mode"] if certain else "unknown",
            "fifths": best["fifths"] if fifths is None else fifths,
            "label": best["label"] if certain else "调性待确认",
            "uncertain": not certain, "correlation": best["correlation"],
            "correlation_margin": round(margin, 5), "candidates": selected[:4],
            "unrestricted_candidates": candidates[:3], "note_events": count}


def _score_measure_pc_stats(score):
    """Single-pass per-measure pitch-class duration weights & note-event counts (no deepcopy)."""
    from music21 import harmony, stream

    part_measures = [list(part.getElementsByClass(stream.Measure)) for part in score.parts]
    n_measures = max((len(ms) for ms in part_measures), default=0)
    pc_weights = [[0.0] * 12 for _ in range(n_measures)]
    note_counts = [0] * n_measures
    for ms in part_measures:
        for idx, measure in enumerate(ms):
            w = pc_weights[idx]
            cnt = 0
            for n in measure.recurse().notes:
                if not isinstance(n, harmony.Harmony):
                    cnt += 1
                    ql = float(n.quarterLength)
                    if ql > 0.0:
                        for p in n.pitches:
                            w[p.pitchClass] += ql
            note_counts[idx] += cnt
    return pc_weights, note_counts


def _region_evidence_from_distribution(pc_weights, count, fifths, declared_mode=None):
    evidence = _key_evidence_from_distribution(pc_weights, count, fifths)
    if declared_mode in ('major', 'minor') and fifths is not None:
        from music21 import key
        declared = key.KeySignature(fifths).asKey(declared_mode)
        consistent = (evidence['mode'] == declared_mode) if not evidence['uncertain'] else None
        evidence['mode_analysis'] = {
            name: evidence.get(name) for name in
            ('tonic', 'mode', 'uncertain', 'correlation', 'correlation_margin')}
        evidence.update(tonic=declared.tonic.name, mode=declared_mode,
                        label=_label(declared.tonic.name, declared_mode), uncertain=False,
                        declared_mode=declared_mode, mode_source='explicit_source',
                        analysis_matches_declaration=consistent)
    else:
        evidence.update(declared_mode=None, mode_source='profile_analysis')
    return evidence


_CHORD_FIG_CACHE: dict = {}
_CHORD_SYM_PROTO_CACHE: dict = {}
_ROMAN_CACHE: dict = {}


def _vertical_chord_events(score, regions, measure_numbers, *, build_symbols=True):
    from music21 import chord, harmony, key, roman, stream, exceptions21, meter
    from music21.analysis.reduceChords import ChordReducer

    # Harmony objects, if already present, must never become analysis pitches.
    # Avoid expensive deepcopy(score) when no Harmony objects exist in the input.
    has_existing_harmony = any(True for _ in score.recurse().getElementsByClass(harmony.Harmony))
    if has_existing_harmony:
        analysis_copy = deepcopy(score)
        for existing in list(analysis_copy.recurse().getElementsByClass(harmony.Harmony)):
            existing.activeSite.remove(existing)
        vertical = analysis_copy.chordify()
    else:
        vertical = score.chordify()
    chords = []
    symbols = []
    skipped = 0
    reducer = ChordReducer()
    has_quarter_regions = "start_quarters" in regions[0]
    key_cache = {}
    key_pcs_cache = {}
    if len(_CHORD_FIG_CACHE) > 4096:
        _CHORD_FIG_CACHE.clear()
    if len(_CHORD_SYM_PROTO_CACHE) > 2048:
        _CHORD_SYM_PROTO_CACHE.clear()
    if len(_ROMAN_CACHE) > 8192:
        _ROMAN_CACHE.clear()

    def _get_tonal(region):
        if region["uncertain"]:
            return None, None
        tk = (region["tonic"], region["mode"])
        k_obj = key_cache.get(tk)
        if k_obj is None:
            k_obj = key.Key(tk[0], tk[1])
            key_cache[tk] = k_obj
            key_pcs_cache[tk] = {p.pitchClass for p in k_obj.pitches}
        return k_obj, key_pcs_cache[tk]

    for index, measure in enumerate(vertical.getElementsByClass(stream.Measure)):
        if index >= len(measure_numbers):
            break
        region = next(r for r in regions if r["start_index"] <= index <= r["end_index"])
        tonal_key, tonal_pcs = _get_tonal(region)
        previous = None
        measure_events = []
        slices = list(measure.recurse().getElementsByClass(chord.Chord))
        # Use the upstream reducer only for evidence aggregation. Its full
        # reduction rewrites rhythm/notes, which is inappropriate for markers.
        weights = reducer.computeMeasureChordWeights(slices, reducer.quarterLengthOnly)
        weights = {tuple(sorted(pcs)): float(value) for pcs, value in weights.items()}
        sounding_duration = sum(weights.values())
        strengths = {}
        for item in slices:
            pcs_key = tuple(sorted(set(item.pitchClasses)))
            bs = float(item.beatStrength)
            if bs > strengths.get(pcs_key, 0):
                strengths[pcs_key] = bs
        m_offset = measure.getOffsetInHierarchy(vertical)
        for c in slices:
            offset = float(c.offset) if c.activeSite is measure else float(c.getOffsetInHierarchy(measure))
            absolute = float(m_offset + c.offset) if c.activeSite is measure else float(c.getOffsetInHierarchy(vertical))
            if has_quarter_regions:
                region = next((r for r in regions if r["start_quarters"] <= absolute < r["end_quarters"]), region)
                tonal_key, tonal_pcs = _get_tonal(region)
            duration = float(c.quarterLength)
            pcs = set(c.pitchClasses)
            if duration <= 0 or not 3 <= len(pcs) <= 5:
                skipped += 1
                previous = None
                continue
            try:
                pitches = c.pitches
                sym_key = (c.bass().name, tuple(sorted({p.name for p in pitches})))
                cached_fig = _CHORD_FIG_CACHE.get(sym_key)
                if cached_fig is None:
                    figure, chord_type = harmony.chordSymbolFigureFromChord(c, includeChordType=True)
                    if (not chord_type or chord_type == "none"
                            or "Cannot" in figure or "add" in figure or "subtract" in figure):
                        cached_fig = (False, None)
                    else:
                        cached_fig = (True, figure)
                    _CHORD_FIG_CACHE[sym_key] = cached_fig
                ok, label = cached_fig
                if not ok:
                    skipped += 1
                    previous = None
                    continue
                symbol = None
                if build_symbols:
                    proto = _CHORD_SYM_PROTO_CACHE.get(label)
                    if proto is None:
                        proto = harmony.ChordSymbol(label)
                        _CHORD_SYM_PROTO_CACHE[label] = proto
                    if not proto.chordKind or "Cannot" in proto.figure or "add" in proto.figure or "subtract" in proto.figure:
                        skipped += 1
                        previous = None
                        continue
                    symbol = proto
                    label = proto.figure
                event = {"measure": measure_numbers[index],
                         "measure_index": index, "beat": round(float(c.beat), 6),
                         "absolute_quarters": absolute,
                         "offset_quarters": round(offset, 6), "duration_quarters": round(duration, 6),
                         "label": label, "uncertain": duration < 1 or float(c.beatStrength) < .5,
                         "evidence": "sounding_pitch_set", "pitch_classes": sorted(pcs),
                         "slice_count": 1, "stability_reasons": [],
                         "roman": None, "chromatic": None}
                if tonal_key is not None:
                    rk = (label, tuple(p.nameWithOctave for p in pitches), tonal_key.tonic.name, tonal_key.mode)
                    rn_fig = _ROMAN_CACHE.get(rk)
                    if rn_fig is None:
                        proto = _CHORD_SYM_PROTO_CACHE.get(label)
                        if proto is None:
                            proto = harmony.ChordSymbol(label)
                            _CHORD_SYM_PROTO_CACHE[label] = proto
                        rn_sym = deepcopy(proto)
                        rn_sym.pitches = pitches
                        rn_fig = roman.romanNumeralFromChord(rn_sym, tonal_key, preferSecondaryDominants=True).figure
                        _ROMAN_CACHE[rk] = rn_fig
                    event["roman"] = rn_fig
                    event["chromatic"] = not pcs.issubset(tonal_pcs)
                if (previous is not None and previous["label"] == label
                        and previous["pitch_classes"] == event["pitch_classes"]
                        and previous["roman"] == event["roman"]
                        and abs(previous["offset_quarters"] + previous["duration_quarters"] - offset) < 1e-5):
                    previous["duration_quarters"] = round(previous["duration_quarters"] + duration, 6)
                    previous["slice_count"] += 1
                    continue
                chords.append(event)
                measure_events.append(event)
                symbols.append((index, offset, symbol, event))
                previous = event
            except (ValueError, TypeError, ZeroDivisionError, exceptions21.Music21Exception):
                skipped += 1
                previous = None
        # Reassess the *merged* sounding interval, not just its first fragment.
        # A whole metric beat is meaningful even on beats 2/4 or if a chord
        # anticipates a beat. No onset is snapped or moved by this decision.
        ts = measure.timeSignature or measure.getContextByClass(meter.TimeSignature) or meter.TimeSignature('4/4')
        beat_edges = list(map(float, ts.getBeatOffsets())) + [float(ts.barDuration.quarterLength)]
        padding = float(measure.paddingLeft)
        beat_spans = [(a - padding, b - padding) for a, b in zip(beat_edges, beat_edges[1:])]
        beat_unit = min(b - a for a, b in beat_spans)
        for event in measure_events:
            start = event["offset_quarters"]
            end = start + event["duration_quarters"]
            full_beats = sum(start <= a + 1e-6 and end >= b - 1e-6 for a, b in beat_spans)
            pcs_key = tuple(event["pitch_classes"])
            support = weights.get(pcs_key, 0.)
            share = support / sounding_duration if sounding_duration else 0.
            repeated_support = (event["duration_quarters"] >= beat_unit / 2 - 1e-6
                                and support >= 2 * beat_unit - 1e-6 and share >= .6
                                and strengths.get(pcs_key, 0.) >= .5)
            reasons = []
            if full_beats:
                reasons.append("sustained_over_complete_metric_beat")
            if repeated_support:
                reasons.append("recurring_pitch_set_with_strong_beat_support")
            event["uncertain"] = not reasons
            event["stability_reasons"] = reasons
            event["stability_evidence"] = {
                "complete_metric_beats": full_beats,
                "metric_beat_quarters": beat_unit,
                "pitch_set_measure_quarters": round(support, 6),
                "pitch_set_sounding_share": round(share, 6),
                "pitch_set_max_attack_strength": strengths.get(pcs_key, 0.),
            }
    # Tiny isolated slices remain excluded, but tiny identical adjacent slices
    # have now had a chance to establish a sustained, readable chord.
    removed = {id(event) for event in chords if event["duration_quarters"] < .5 and event["uncertain"]}
    skipped += len(removed)
    chords = [event for event in chords if id(event) not in removed]
    symbols = [entry for entry in symbols if id(entry[3]) not in removed]
    return chords, symbols, skipped


def _chord_events(score, regions, measure_numbers, *, experimental_chorder=False, build_symbols=True):
    """Keep explicit sounding harmonies; fill arpeggio gaps with Chorder.

    Root/quality/bass recognition is upstream, not a local template classifier.
    A coarse window must not replace two different observed chord attacks or
    an already supported vertical chord with an averaged label.
    """
    from music21 import harmony, key, pitch, roman

    chords, symbols, skipped = _vertical_chord_events(
        score, regions, measure_numbers, build_symbols=build_symbols)
    if not experimental_chorder:
        # The two-beat aggregation experiment can anticipate a later chord's
        # resolution tone. Keep it opt-in until boundary-aware models have
        # been compared; increased coverage alone is not an accuracy result.
        return chords, symbols, skipped, {'method': 'music21_sounding_pitch_sets',
                                         'experimental_chorder': False}
    from .chord_segments import analyze_chord_segments
    analysis = analyze_chord_segments(score, regions)
    suffixes = {'M':'', 'm':'m', 'o':'dim', '+':'+', '7':'7', 'M7':'maj7',
                'm7':'m7', 'o7':'dim7', '/o7':'m7b5', 'sus2':'sus2', 'sus4':'sus4'}
    additions = []
    for segment in analysis['segments']:
        if segment['attack_count'] < 2:
            # One brief block chord is already handled by vertical evidence;
            # a longer window must not extend it over the following silence.
            segment['integration'] = 'retained_single_attack_vertical_evidence'
            continue
        start = segment['absolute_quarters']
        end = start + segment['duration_quarters']
        existing = [c for c in chords if c['absolute_quarters'] < end-1e-7
                    and c['absolute_quarters']+c['duration_quarters'] > start+1e-7]
        if any(not c['uncertain'] for c in existing):
            segment['integration'] = 'retained_supported_sounding_chord'
            continue
        if len({c['label'] for c in existing}) > 1:
            segment['integration'] = 'retained_multiple_sounding_chord_changes'
            continue
        index = segment['measure_index']
        region = next(r for r in regions if r['start_index'] <= index <= r['end_index'])
        if 'start_quarters' in regions[0]:
            region = next((r for r in regions if r['start_quarters'] <= start < r['end_quarters']), region)
        tonal = key.Key(region['tonic'], region['mode']) if not region['uncertain'] else None
        def name(pc):
            if tonal:
                spelling = next((p.name for p in tonal.pitches if p.pitchClass == pc), None)
                if spelling:
                    return spelling
            return pitch.Pitch(pc+60).name
        label = name(segment['root_pc']) + suffixes[segment['quality']]
        if segment['bass_pc'] != segment['root_pc']:
            label += '/' + name(segment['bass_pc'])
        symbol = harmony.ChordSymbol(label)
        event = dict(segment, label=symbol.figure, measure=measure_numbers[index],
                     slice_count=len(segment['window_indices']),
                     stability_reasons=['upstream_duration_weighted_window_with_defining_tones'])
        if tonal:
            event['roman'] = roman.romanNumeralFromChord(symbol, tonal, preferSecondaryDominants=True).figure
            event['chromatic'] = not set(segment['pitch_classes']).issubset({p.pitchClass for p in tonal.pitches})
        segment['integration'] = 'added_arpeggio_harmony'
        additions.append(event)
        symbols.append((index, event['offset_quarters'], symbol, event))
    # Replaced weak fragments remain in the detailed upstream/vertical evidence,
    # rather than competing with one accepted interval in the displayed list.
    replaced = {id(c) for c in chords if c['uncertain'] and any(
        a['absolute_quarters'] <= c['absolute_quarters']+1e-7 and
        a['absolute_quarters']+a['duration_quarters'] >= c['absolute_quarters']+c['duration_quarters']-1e-7
        for a in additions)}
    analysis['vertical_candidates'] = deepcopy(chords)
    analysis['added_segments'] = len(additions)
    chords = [c for c in chords if id(c) not in replaced] + additions
    symbols = [s for s in symbols if id(s[3]) not in replaced]
    chords.sort(key=lambda c: c['absolute_quarters'])
    symbols.sort(key=lambda s: (s[0], s[1]))
    return chords, symbols, skipped, analysis


def analyze_harmony(musicxml: str | Path, *, output_path: str | Path | None = None,
                    write_modes: bool = True, write_chords: bool = False,
                    experimental_chorder: bool = False) -> dict:
    """Analyze plain MusicXML; optionally write annotations to a *new* path.

    Key regions follow existing fifths changes. Mode inference is deliberately
    conservative and never inserts a new key signature from a transient chord.
    Chorder metric windows are an explicit evaluation option, not the default.
    Incomplete chords may be omitted. ``beat`` is music21's one-based beat,
    while ``offset_quarters`` is unambiguous in compound meters.
    """
    from music21 import chord, converter, harmony, key, roman, stream, exceptions21

    source = Path(musicxml).resolve()
    target = Path(output_path).resolve() if output_path is not None else None
    if target == source:
        raise ValueError("Use a separate output_path; harmony analysis preserves its input.")
    tree = ET.parse(source)
    root = tree.getroot()
    if root.tag != "score-partwise":
        raise ValueError("Harmony analysis requires uncompressed score-partwise MusicXML.")
    xml_parts = root.findall("part")
    if not xml_parts:
        raise ValueError("MusicXML has no score parts.")
    xml_measures = xml_parts[0].findall("measure")
    score = converter.parse(str(source))
    if not score.parts or not xml_measures:
        raise ValueError("MusicXML has no measures.")
    regions = []
    signature = 0
    declared_mode = None
    for index, measure in enumerate(xml_measures):
        explicit = measure.find("attributes/key/fifths")
        if explicit is not None:
            signature = int(explicit.text)
            declared_mode = measure.findtext("attributes/key/mode")
            if declared_mode not in ('major', 'minor'):
                declared_mode = None
        if (not regions or regions[-1]["fifths"] != signature
                or regions[-1]['declared_mode'] != declared_mode):
            regions.append({"start_index": index, "fifths": signature,
                            "declared_mode": declared_mode,
                            "start_measure": _number(measure.get("number", str(index + 1)))})
    measure_pc_weights, measure_note_counts = _score_measure_pc_stats(score)
    warnings = []
    for index, region in enumerate(regions):
        hi = regions[index + 1]["start_index"] if index + 1 < len(regions) else len(xml_measures)
        region["end_index"] = hi - 1
        region["end_measure"] = _number(xml_measures[hi - 1].get("number", str(hi)))
        lo = region["start_index"]
        w_reg = [sum(measure_pc_weights[m][pc] for m in range(lo, min(hi, len(measure_pc_weights)))) for pc in range(12)]
        c_reg = sum(measure_note_counts[lo:hi])
        region.update(_region_evidence_from_distribution(w_reg, c_reg,
                                                         region["fifths"], region['declared_mode']))
        region["boundary_source"] = "existing_key_signature"
        if region.get('analysis_matches_declaration') is False:
            warnings.append({'kind': 'declared_mode_conflict', 'start_measure': region['start_measure'],
                             'end_measure': region['end_measure'],
                             'message': '音符统计与源文件声明的大小调不同，已保留源声明及其变化位置。'})
        if region["uncertain"]:
            warnings.append({"kind": "uncertain_mode", "start_measure": region["start_measure"],
                             "end_measure": region["end_measure"],
                             "message": "大小调证据不足，保留原调号；候选相关分数不是概率。"})
    w_all = [sum(m[pc] for m in measure_pc_weights) for pc in range(12)]
    global_key = _key_evidence_from_distribution(w_all, sum(measure_note_counts))
    chords, symbols, skipped, segmentation = _chord_events(
        score, regions, [_number(m.get("number", str(i + 1))) for i, m in enumerate(xml_measures)],
        experimental_chorder=experimental_chorder,
        build_symbols=bool(target and write_chords))
    if any(c["chromatic"] for c in chords):
        warnings.append({"kind": "chromatic_harmony",
                         "message": "调外和弦可能是离调、借用和弦或旋律经过音；和弦标签不会触发换调号。小调导音也不等于转调。"})
    warnings.append({"kind": "analysis_scope",
                     "message": "仅分析所转录钢琴声部，并非原曲全编制和声。当前和弦名称是分析候选，分解和弦、转位与变化位置仍可能遗漏或误判；分数不是准确率或概率。"})
    if target is not None:
        if write_modes:
            for part in xml_parts:
                for index, measure in enumerate(part.findall("measure")):
                    region = next((r for r in regions if r["start_index"] <= index <= r["end_index"]), None)
                    if region is None or region["uncertain"]:
                        continue
                    for key_element in measure.findall("attributes/key"):
                        fifths_element = key_element.find("fifths")
                        if fifths_element is None or int(fifths_element.text) != region["fifths"]:
                            continue
                        mode = key_element.find("mode")
                        if mode is None:
                            mode = ET.SubElement(key_element, "mode")
                        mode.text = region["mode"]
        if write_chords:
            from music21.musicxml.m21ToXml import MeasureExporter
            divisions = 1
            for index, measure in enumerate(xml_measures):
                dv = measure.find("attributes/divisions")
                if dv is not None:
                    divisions = int(dv.text)
                # Existing authored chord symbols take precedence over generated
                # labels, and repeated calls cannot pile up duplicate symbols.
                if measure.find("harmony") is not None:
                    continue
                insert_at = next((i for i, child in enumerate(measure)
                                  if child.tag in {"note", "backup", "forward"}), len(measure))
                for symbol_index, offset, symbol, event in symbols:
                    if symbol_index != index or event["uncertain"]:
                        continue
                    elem = MeasureExporter().chordSymbolToXml(symbol, append=False)
                    if isinstance(elem, list):
                        elem = elem[0]
                    if offset:
                        off = ET.SubElement(elem, "offset")
                        off.text = str(round(offset * divisions))
                    measure.insert(insert_at, elem)
                    insert_at += 1
        target.parent.mkdir(parents=True, exist_ok=True)
        tree.write(target, encoding="utf-8", xml_declaration=True)
    return {"schema_version": 2, "method": ("music21_profiles_and_chorder_metric_windows"
             if experimental_chorder else "music21_profiles_and_sounding_pitch_sets"),
            "segmentation": segmentation,
            "global_key": global_key, "key_regions": regions, "chords": chords,
            "warnings": warnings, "review": warnings,
            "skipped_slices": skipped, "musicxml_path": str(target or source),
            "write_modes": bool(target and write_modes), "write_chords": bool(target and write_chords)}


def analyze_midi_harmony(midi_path: str | Path, *, experimental_chorder: bool = False) -> dict:
    """Analyze MIDI without quantizing or modifying it.

    mido supplies exact tick/PPQ note positions to music21. This intentionally
    bypasses music21's MIDI importer: even ``quantizePost=False`` in 9.9.1
    groups attacks less than 1/16 quarter apart. Tempo changes never enter this
    coordinate conversion. ``absolute_quarters * ticks_per_beat`` is the marker
    tick in the input file, including any leading silence.
    """
    from fractions import Fraction
    from bisect import bisect_right
    import mido
    from music21 import key, meter, note, stream

    source = Path(midi_path).resolve()
    midi = mido.MidiFile(source)
    if midi.type == 2 or midi.ticks_per_beat <= 0:
        raise ValueError("Harmony analysis requires synchronous MIDI with positive PPQ timing.")
    ppq = midi.ticks_per_beat
    part = stream.Part()
    active = defaultdict(deque)
    signatures = {0: "4/4"}
    key_events = {}
    tick = 0
    count = 0
    unmatched = 0
    tempo_events = []
    # Merged tracks preserve their absolute tick times; notes are matched per
    # MIDI channel and pitch. Channel 10 percussion is not tonal evidence.
    for message in mido.merge_tracks(midi.tracks):
        tick += message.time
        if message.type == "time_signature":
            signatures[tick] = f"{message.numerator}/{message.denominator}"
        elif message.type == "key_signature":
            key_events[tick] = key.Key(message.key[:-1], "minor") if message.key.endswith("m") else key.Key(message.key, "major")
        elif message.type == "set_tempo":
            tempo_events.append({"absolute_quarters": tick / ppq, "microseconds_per_quarter": message.tempo})
        elif message.type == "note_on" and message.velocity and message.channel != 9:
            active[(message.channel, message.note)].append((tick, message.velocity))
        elif message.type in {"note_on", "note_off"} and message.channel != 9:
            pending = active[(message.channel, message.note)]
            if not pending:
                unmatched += 1
                continue
            start, velocity = pending.popleft()
            if tick > start:
                n = note.Note(message.note, quarterLength=Fraction(tick - start, ppq))
                n.volume.velocity = velocity
                part.insert(Fraction(start, ppq), n)
                count += 1
    unmatched += sum(len(items) for items in active.values())
    if not count:
        raise ValueError("MIDI has no completed pitched notes to analyze.")
    for start, signature in signatures.items():
        part.insert(Fraction(start, ppq), meter.TimeSignature(signature))
    for start, value in key_events.items():
        part.insert(Fraction(start, ppq), value)
    # Keep an exact, unmeasured copy for profile slices. Measure construction is
    # only for labels/beat strengths, never for determining absolute note starts.
    flat = deepcopy(part)
    part.makeMeasures(inPlace=True)
    part.makeTies(inPlace=True)
    score = stream.Score()
    score.insert(0, part)
    measures = list(part.getElementsByClass(stream.Measure))
    starts = [float(m.offset) for m in measures]
    end = float(flat.highestTime)
    boundaries = [(0., None, None)]
    for start, value in sorted(key_events.items()):
        position = start / ppq
        if position >= end:
            continue
        if position == 0:
            boundaries[0] = (position, value.sharps, value.mode)
        elif boundaries[-1][1:] != (value.sharps, value.mode):
            boundaries.append((position, value.sharps, value.mode))
    regions = []
    warnings = []
    for i, (start, fifths, declared_mode) in enumerate(boundaries):
        stop = boundaries[i + 1][0] if i + 1 < len(boundaries) else end
        segment = stream.Stream()
        for n in flat.notes:
            a, b = float(n.offset), float(n.offset + n.quarterLength)
            duration = min(b, stop) - max(a, start)
            if duration > 0:
                copied = deepcopy(n)
                copied.quarterLength = duration
                segment.append(copied)
        lo = max(0, bisect_right(starts, start) - 1)
        hi = max(lo, bisect_right(starts, stop - 1e-9) - 1)
        evidence = _region_evidence(segment, fifths, declared_mode)
        region = {"start_index": lo, "end_index": hi,
                  "start_measure": measures[lo].number, "end_measure": measures[hi].number,
                  "start_quarters": start, "end_quarters": stop,
                  "boundary_source": "midi_key_signature" if fifths is not None else "global_profile",
                  **evidence}
        regions.append(region)
        if region.get('analysis_matches_declaration') is False:
            warnings.append({'kind': 'declared_mode_conflict', 'start_measure': region['start_measure'],
                             'end_measure': region['end_measure'],
                             'message': '音符统计与MIDI声明的大小调不同，已保留源声明及其变化位置。'})
        if region["uncertain"]:
            warnings.append({"kind": "uncertain_mode", "start_measure": region["start_measure"],
                             "end_measure": region["end_measure"], "message": "调性证据不足；保留候选，不改变MIDI调号。"})
    chords, _, skipped, segmentation = _chord_events(score, regions, [m.number for m in measures],
                                                    experimental_chorder=experimental_chorder,
                                                    build_symbols=False)
    warnings.append({"kind": "analysis_scope", "message": "仅分析所转录MIDI声部，不代表原曲全编制和声。和弦来自同时发声音高；分解和弦可能漏识别，相关分数不是概率。"})
    if unmatched:
        warnings.append({"kind": "unmatched_note_events", "count": unmatched,
                         "message": "存在未配对音符事件，已忽略，未改写源文件。"})
    return {"schema_version": 2, "method": ("mido_exact_ticks_music21_profiles_and_chorder_metric_windows"
             if experimental_chorder else "mido_exact_ticks_music21_profiles_and_sounding_pitch_sets"),
            "segmentation": segmentation,
            "global_key": _key_evidence(flat), "key_regions": regions,
            "chords": chords, "warnings": warnings, "review": warnings,
            "skipped_slices": skipped, "midi_path": str(source), "musicxml_path": None,
            "ticks_per_beat": ppq, "tempo_events": tempo_events,
            "write_modes": False, "write_chords": False}
