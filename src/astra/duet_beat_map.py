"""First-pass beat map read from DuET's own note positions (replaces Cubase).

The map uses the existing tempo-map fields, so opening, meter, recording and
preview tools need no change. Its quarter coordinate is DuET's written
position plus one fixed prefix: a note written at model quarter q sits at
beat q + prefix. ``notation_binding`` records that identity so the first
score can use DuET's positions directly; any later edit breaks the binding
and the score is re-decoded on the edited map (see duet_constrained).
"""
from bisect import bisect_left
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path

VERSION = 1
SOURCE = 'duet_first_pass'
BINDING_FIELDS = ('anchors', 'time_signatures', 'pickup_beats', 'first_full_bar_beat', 'grid_origin',
                  'opening_mode', 'opening_policy', 'score_start_seconds', 'short_bar_annotations',
                  'explicit_partial_measures')


def map_digest(tempo):
    """Identity of everything that decides bars and beat positions."""
    view = {k: tempo.get(k) for k in BINDING_FIELDS}
    return hashlib.sha256(json.dumps(view, sort_keys=True, default=float).encode()).hexdigest()


def anchors_digest(tempo):
    """Identity of the beat points alone (seconds <-> beats), not bar grouping.

    When only bars/meter/opening change, every note keeps its absolute beat
    position, so DuET's written positions stay valid and are just re-barred.
    """
    rows = [(round(float(a['seconds']), 9), round(float(a['beat']), 9)) for a in (tempo or {}).get('anchors', [])]
    return hashlib.sha256(json.dumps(rows).encode()).hexdigest()


def _chain(points):
    """Longest run of points increasing in both written position and time."""
    tails, tail_idx, prev = [], [], [-1] * len(points)
    for i, (_, t) in enumerate(points):
        k = bisect_left(tails, t)
        if k == len(tails):
            tails.append(t); tail_idx.append(i)
        else:
            tails[k] = t; tail_idx[k] = i
        prev[i] = tail_idx[k - 1] if k else -1
    out, i = [], tail_idx[-1] if tail_idx else -1
    while i >= 0:
        out.append(points[i]); i = prev[i]
    return out[::-1]


def beat_seconds(onsets, positions, durations, last_quarter):
    """Seconds of every integer model quarter 0..last_quarter (extrapolated at ends)."""
    groups = {}
    for t, p, d in zip(onsets, positions, durations):
        if d > 0 and math.isfinite(t) and math.isfinite(p):
            groups.setdefault(round(float(p), 6), []).append(float(t))
    points = sorted((p, sorted(ts)[len(ts) // 2]) for p, ts in groups.items())
    chain = _chain(points)
    if len(chain) < 2:
        raise ValueError('DuET 拍点不足，无法生成初稿拍点图')
    P = [c[0] for c in chain]; T = [c[1] for c in chain]
    def at(k):
        if k <= P[0]:
            return T[0] + (k - P[0]) * (T[1] - T[0]) / (P[1] - P[0])
        if k >= P[-1]:
            return T[-1] + (k - P[-1]) * (T[-1] - T[-2]) / (P[-1] - P[-2])
        j = bisect_left(P, k)
        return T[j - 1] + (k - P[j - 1]) * (T[j] - T[j - 1]) / (P[j] - P[j - 1])
    return [at(k) for k in range(int(last_quarter) + 1)], {'points': len(points), 'kept': len(chain)}


def _meter_rows(bar_starts, bar_lengths, meters, prefix):
    """Signature events at DuET's bar starts; irregular bars get their own meter."""
    rows = []
    def add(beat, n, d, origin):
        if rows and rows[-1]['numerator'] == n and rows[-1]['denominator'] == d:
            return
        rows.append({'beat': beat, 'numerator': n, 'denominator': d, 'origin': origin})
    for start, length, meter in zip(bar_starts, bar_lengths, meters):
        n, d = (int(v) for v in str(meter).split('/'))
        if abs(length - n * 4 / d) > 1e-6:
            for den in (d, 8, 16, 32):
                num = length * den / 4
                if abs(num - round(num)) < 1e-6 and round(num) >= 1:
                    n, d = int(round(num)), den
                    break
        add(prefix + float(start), n, d, SOURCE)
    return rows


def build_first_pass_map(prior, score, duration, prediction_sha256):
    """Return a tempo-map dict in the existing schema, plus an audit."""
    from .rhythm_opening import apply_opening
    from .timing import TempoMap
    onsets = prior['model_note_onsets_seconds']
    positions = prior['model_note_positions_quarters']
    durations = prior['model_note_durations_quarters']
    starts = [float(v) for v in score['bar_starts']]
    lengths = [float(v) for v in score['bar_lengths']]
    meters = [str(v) for v in score['meters']]
    last = math.ceil(max(max(p + max(d, 0) for p, d in zip(positions, durations)), starts[-1] + lengths[-1])) + 1
    secs, audit = beat_seconds(onsets, positions, durations, last)
    k0 = next((k for k, s in enumerate(secs) if s > 1e-3), None)
    if k0 is None or k0 + 1 >= len(secs):
        raise ValueError('DuET 拍点不在音频范围内')
    step = secs[k0 + 1] - secs[k0]
    prefix = round(secs[k0] / step * 32) / 32 - k0
    anchors = [{'seconds': 0.0, 'beat': 0.0}]
    for k, s in enumerate(secs):
        beat = prefix + k
        if s > 1e-3 and beat > 1e-6 and s > anchors[-1]['seconds'] + 1e-4 and beat > anchors[-1]['beat'] + 1e-6:
            anchors.append({'seconds': float(s), 'beat': float(beat)})
    # End exactly at the end of the audio, like the acoustic beat maps did:
    # beats past the audio would make tools treat the gap as a score end.
    a, b = anchors[-2], anchors[-1]
    rate = (b['beat'] - a['beat']) / (b['seconds'] - a['seconds'])
    while len(anchors) > 2 and anchors[-1]['seconds'] >= duration - 1e-4:
        anchors.pop()
    last = anchors[-1]
    anchors.append({'seconds': float(duration), 'beat': last['beat'] + (float(duration) - last['seconds']) * rate})
    rows = _meter_rows(starts, lengths, meters, prefix)
    def full(length, meter):
        n, d = (int(v) for v in meter.split('/'))
        return abs(length - n * 4 / d) < 1e-6
    first_full = next((i for i, (l, m) in enumerate(zip(lengths, meters)) if full(l, m)), 0)
    rows = [{'beat': 0.0, 'numerator': rows[min(first_full, len(rows) - 1)]['numerator'],
             'denominator': rows[min(first_full, len(rows) - 1)]['denominator']}] + [r for r in rows if r['beat'] > 1e-9]
    base = {'version': 1, 'source': SOURCE, 'pulse_mapping_version': 1, 'candidate_only': False,
            'anchors': anchors, 'time_signatures': rows, 'grid_origin': prefix % 1.0,
            'pickup_beats': 0.0, 'first_full_bar_beat': 0.0, 'lead_in_alignment': True, 'warnings': []}
    clock = TempoMap(deepcopy(base))
    downbeat = prefix + starts[first_full]
    seconds = clock.to_seconds(downbeat)
    pickup_notes = first_full > 0
    mode = 'pickup' if pickup_notes else 'normal'
    try:
        opened = apply_opening(base, {'mode': mode, 'seconds': seconds if seconds > 1e-3 else 0})
    except ValueError:
        mode = 'intro'
        opened = apply_opening(base, {'mode': mode, 'seconds': seconds})
    tempo = opened['tempo']
    tempo['opening_policy'] = {**tempo.get('opening_policy', {}), 'source': SOURCE, 'protected': False}
    tempo['opening_phase_confirmed'] = False
    tempo['warnings'] = ['拍点、拍号与第一小节来自 DuET 初稿；可在节奏编辑中修改，更新谱面时 DuET 会按修改后的拍点重新写谱。']
    tempo['notation_binding'] = {'version': VERSION, 'method': 'duet_first_pass',
                                 'prediction_sha256': prediction_sha256, 'quarter_offset': prefix,
                                 'map_sha256': None, 'anchors_sha256': None}
    tempo['notation_binding']['anchors_sha256'] = anchors_digest(tempo)
    tempo['notation_binding']['map_sha256'] = map_digest(tempo)
    TempoMap(deepcopy(tempo))
    audit.update(prefix_quarters=prefix, opening_mode=mode, downbeat_seconds=seconds,
                 bars=len(starts), anchors=len(anchors))
    return tempo, audit


def binding_matches(tempo, prediction_sha256):
    """DuET's first-pass positions are valid on this map (same beat points)."""
    b = (tempo or {}).get('notation_binding') or {}
    return (b.get('method') == 'duet_first_pass' and b.get('prediction_sha256') == prediction_sha256
            and b.get('anchors_sha256') == anchors_digest(tempo))


def resolve_first_pass(project, state, tempo, mode, *, normalized_audio, log=print, cache_only=False):
    """Run (or reuse) DuET once and publish its beats as the first beat map.

    Called where the Cubase metrical resolution used to run; returns the same
    ``{tempo, prepared_prediction, summary}`` shape.
    """
    import numpy as np
    from .pipeline import _notes_bundle, _pedals_in_duration
    from .score_duet_export import prepare_duet_prediction
    from .duet_cached_prior import extract_pending_notation_prior
    project = Path(project)
    bundle = _notes_bundle(project, state, 'piano', state['analysis_keys']['piano'])
    if not bundle or not bundle['notes']:
        raise ValueError('没有钢琴音符，无法由 DuET 生成拍点')
    options = dict(state['config'].get('score', {}))
    if cache_only:
        options['_harmony_reuse_only'] = True
    title = Path(state['source']).stem.split(' - ', 1)[-1]
    pedals = list(_pedals_in_duration(bundle.get('pedals', []), state['duration']))
    log('[duet] 由 DuET 推出拍点、拍号与第一小节（不再运行 Cubase 拍点分析）')
    prepared = prepare_duet_prediction(bundle['notes'], pedals, tempo, project, options=options,
                                       title=title, controls=bundle.get('controls'), log=log)
    cache = Path(prepared['prediction']['report']).parent
    def digest(path):
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    audio_sha = digest(normalized_audio)
    original = Path(state.get('source_audio') or state['source'])
    binding = {'original_path': str(original), 'original_sha256': digest(original),
               'normalized_path': str(normalized_audio), 'normalized_sha256': audio_sha,
               'method': 'ffmpeg_decode_audio'}
    extracted = extract_pending_notation_prior(project / 'project.json', state, bundle['notes'], cache,
                                               prepared['midi'], expected_audio_sha256=audio_sha,
                                               audio_binding=binding)
    with np.load(cache / 'score.npz', allow_pickle=False) as b:
        score = {k: np.array(b[k]) for k in ('bar_starts', 'bar_lengths', 'meters')}
    result, audit = build_first_pass_map(extracted['prior'], score, state['duration'], digest(cache / 'score.npz'))
    summary = {'version': VERSION, 'status': 'applied', 'source': SOURCE, **audit,
               'message': f"DuET 初稿拍点：{audit['bars']} 小节，开头为{ {'normal': '正常开头', 'pickup': '弱起', 'intro': '前奏'}[audit['opening_mode']] }"}
    result['duet_first_pass'] = summary
    log('[duet] ' + summary['message'])
    return {'tempo': result, 'prepared_prediction': prepared, 'summary': summary}
