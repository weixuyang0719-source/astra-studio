"""Turn beat-synchronous decoder evidence into a clock, without audio inference.

The HMM proposes meters; actual activation support confirms them. HMM tail
continuation is not independent downbeat evidence. No constant-tempo fit or
pulse insertion is performed at this boundary.
"""
import copy
import math
from statistics import median

from .timing import BEAT_MAPPING_VERSION, TempoMap, scale_tempo

METER_DECODER_VERSION = 4


def _bar_path_score(probabilities, downbeats, changes, observation_weight, change_probability):
    """Score a *given* local path, not a new decoder.

    Same observation densities as madmom RNNBeatTrackingObservationModel:
    log(p) on downbeats, log((1-p)/(lambda-1)) otherwise. Bar transitions use
    the existing DBNBarTrackingProcessor's stay/change prior. These are model
    scores, not calibrated probabilities of musical correctness.
    """
    score = 0.
    for i, p in enumerate(probabilities):
        p = min(1-1e-7, max(1e-7, p))
        score += math.log(p) if i in downbeats else math.log((1-p)/(observation_weight-1))
        if i and i in downbeats:
            score += math.log(change_probability if i in changes else 1-change_probability)
    return score


def _compare_short_meter(beats, numbers, meters, probabilities, left, right,
                         following_end, previous_meter, first, boundary,
                         support_threshold, minimum_supported_bars, parameters):
    """Conservative verification of a DBN-proposed, bounded short excursion.

    Never inserts pulses, moves downbeats or invents a compensating meter.
    The returning stable meter and both endpoints must already be observed.
    """
    new_meter = meters[left]
    row = {'start_index': left, 'end_index_exclusive': right,
           'start_seconds': beats[left],
           'end_seconds': beats[right] if right < len(beats) else beats[-1],
           'from_numerator': previous_meter, 'candidate_numerator': new_meter,
           'accepted': False, 'scores': {}, 'reason': None,
           'quarter_count': right-left, 'minimum_log_score_margin': math.log(10),
           'pulse_seconds_changed': False}

    def reject(reason):
        row['reason'] = reason
        return row

    count, remainder = divmod(right-left, new_meter)
    if remainder or not 0 < count < minimum_supported_bars:
        return reject('not_a_short_complete_meter_excursion')
    row['candidate_short_bars'] = count
    if right >= len(beats) or meters[right] != previous_meter or numbers[right] != 1:
        return reject('no_observed_return_to_previous_meter')
    position = (first+left-boundary)/previous_meter
    if abs(position-round(position)) > 1e-8:
        return reject('left_endpoint_conflicts_with_retained_bar_phase')
    window_start = left-2*previous_meter
    window_end = right+minimum_supported_bars*previous_meter
    if window_start < 0:
        return reject('insufficient_preceding_bar_context')
    if window_end >= len(beats) or window_end > following_end:
        return reject('insufficient_complete_returning_bars')
    row.update(window_start_index=window_start, window_end_index=window_end,
               window_start_seconds=beats[window_start], window_end_seconds=beats[window_end],
               window_quarter_count=window_end-window_start,
               candidate_bar_beat_counts=[previous_meter]*2+[new_meter]*count+
                                         [previous_meter]*minimum_supported_bars,
               unchanged_endpoint_phase=(window_end-window_start) % previous_meter)
    prior = list(range(window_start, left, previous_meter))
    after = list(range(right, window_end+1, previous_meter))
    required = prior+list(range(left, right, new_meter))+after
    row['endpoint_and_context_evidence'] = [
        {'index': i, 'seconds': beats[i], 'probability': probabilities[i]} for i in required]
    if (any(meters[i] != previous_meter for i in range(window_start, left)) or
            any(meters[i] != previous_meter for i in range(right, window_end)) or
            any(numbers[i] != 1 for i in prior+after)):
        return reject('context_not_a_stable_returning_meter')
    weight = float(parameters.get('observation_weight', 100))
    change = float(parameters.get('meter_change_prob', .001))
    if not math.isfinite(weight) or not weight > 1 or not math.isfinite(change) or not 0 < change < .5:
        return reject('invalid_decoder_scoring_parameters')
    row['scoring_parameters'] = {'observation_weight': weight, 'meter_change_prob': change,
                                 'method': 'madmom_bar_observation_and_transition_path_score'}
    observed = probabilities[window_start:window_end+1]
    candidate_down = {i-window_start for i in required}
    unchanged_down = set(range(0, len(observed), previous_meter))
    candidate_score = _bar_path_score(observed, candidate_down,
        {left-window_start, right-window_start}, weight, change)
    unchanged_score = _bar_path_score(observed, unchanged_down, set(), weight, change)
    margin = candidate_score-unchanged_score
    row['scores'] = {'short_excursion': candidate_score, 'unchanged_meter': unchanged_score,
                     'difference': margin}
    different = sorted(unchanged_down-candidate_down)
    weak_competing = sum(observed[i] < support_threshold for i in different)
    row['phase_discrimination'] = {
        'candidate_only_indices': [i+window_start for i in sorted(candidate_down-unchanged_down)],
        'unchanged_only_indices': [i+window_start for i in different],
        'weak_unchanged_only_positions': weak_competing}
    if any(probabilities[i] < support_threshold for i in (left, right)):
        return reject('weak_short_segment_endpoint')
    if any(probabilities[i] < support_threshold for i in prior+after):
        return reject('weak_preceding_or_returning_downbeats')
    if any(probabilities[i] < support_threshold for i in range(left, right, new_meter)):
        return reject('weak_short_segment_internal_downbeat')
    # Uniformly high activations must not win merely because the shorter bar
    # offers one extra downbeat density. The two phases need acoustic contrast.
    if not different or weak_competing < max(2, math.ceil(len(different)/2)):
        return reject('ambiguous_competing_downbeat_phases')
    if margin < row['minimum_log_score_margin']:
        return reject('competing_unchanged_meter_not_decisively_worse')
    row.update(accepted=True, reason='bounded_short_meter_beats_unchanged_candidate')
    return row


def tempo_map_from_bar_result(result, duration, beat_scale=1.0, *,
                              support_threshold=.5, minimum_supported_bars=3,
                              allow_unconfirmed_opening=False):
    """Consume ``beat_seconds, beat_numbers, meters, downbeat_probability``.

    All arrays have one item per pulse. Numbers are 1-based. ``meters`` comes
    from the decoder's hidden-state pattern, not a truncated bar's max number.
    Probability .5 means positive binary activation evidence, not a calibrated
    confidence guarantee. Normally three complete bars and a majority of their
    start activations must support a new meter. A bounded short excursion has
    a separate, stricter two-ended path comparison. Audio timestamps are unchanged.
    The opt-in opening policy keeps a valid decoder clock usable when its
    initial meter/phase has insufficient evidence; it does not accept that
    interpretation as confirmed or relax the gate for later meter changes.
    """
    beats = [float(x) for x in result.get('beat_seconds', result.get('beats', []))]
    numbers = list(result.get('beat_numbers', []))
    meters = list(result.get('meters', []))
    probabilities = [float(x) for x in result.get('downbeat_probability', [])]
    if (len(beats) < 3 or not all(len(x) == len(beats) for x in (numbers, meters, probabilities))
            or not all(math.isfinite(t) and t >= 0 for t in beats)
            or any(b <= a for a, b in zip(beats, beats[1:]))):
        raise ValueError('Bar decoder needs matching arrays and increasing finite beat seconds')
    if (not math.isfinite(duration) or duration < beats[-1]
            or not 0 < support_threshold < 1
            or type(minimum_supported_bars) is not int or minimum_supported_bars < 3):
        raise ValueError('Invalid duration or downbeat evidence policy')
    if not all(math.isfinite(p) and 0 <= p <= 1 for p in probabilities):
        raise ValueError('Downbeat probabilities must be finite and between zero and one')
    if not all(type(n) is int and type(m) is int and m in (3, 4) and 1 <= n <= m
               for n, m in zip(numbers, meters)):
        raise ValueError('Expected integer 1-based positions in supported 3/4 or 4/4 meters')
    for i in range(1, len(beats)):
        expected = numbers[i-1] % meters[i-1] + 1
        if numbers[i] != expected or (meters[i] != meters[i-1] and expected != 1):
            raise ValueError('Decoder path changes meter or phase inside an incomplete bar')

    interval = median(b-a for a, b in zip(beats, beats[1:]))
    first = max(.125, round(beats[0]/interval*8)/8) if beats[0] > 1e-8 else 0.
    while first and first/beats[0]*60 > 300 and first > 1/32:
        first /= 2
    anchors = [{'seconds': 0., 'beat': 0.}]
    anchors.extend({'seconds': t, 'beat': first+i} for i, t in enumerate(beats) if t > 1e-8)
    if duration > beats[-1]:
        anchors.append({'seconds': duration,
                        'beat': first+len(beats)-1+(duration-beats[-1])/(beats[-1]-beats[-2])})
    initial = meters[0]
    pickup = (first + (initial-numbers[0]+1) % initial) % initial
    signatures = [{'beat': 0., 'numerator': initial, 'denominator': 4}]
    boundary = pickup
    starts = [0] + [i for i in range(1, len(beats)) if meters[i] != meters[i-1]]
    segments = []
    short_comparisons = []
    unresolved = []
    paired_recoveries = set()
    unconfirmed_opening = False
    for si, left in enumerate(starts):
        right = starts[si+1] if si+1 < len(starts) else len(beats)
        meter = meters[left]
        bar_starts = [i for i in range(left, right) if numbers[i] == 1]
        observations = []
        for i in bar_starts:
            # A next boundary must be a real decoded pulse, not extrapolated
            # from duration or an unfinished last bar.
            complete = i+meter < len(beats) and i+meter <= right and numbers[i+meter] == 1
            observations.append({'beat_index': i, 'seconds': beats[i], 'beat': first+i,
                'probability': probabilities[i], 'complete_bar': complete,
                'supported': complete and probabilities[i] >= support_threshold})
        complete = sum(x['complete_bar'] for x in observations)
        supported = sum(x['supported'] for x in observations)
        acoustic_support = supported >= minimum_supported_bars and supported >= complete/2
        segment = {'start_index': left, 'end_index_exclusive': right,
            'start_seconds': beats[left], 'start_beat': first+left, 'numerator': meter,
            'complete_bars': complete, 'supported_bars': supported,
            'acoustic_support': acoustic_support, 'bar_start_evidence': observations,
            'applied': False}
        if si == 0:
            if not acoustic_support:
                if not allow_unconfirmed_opening:
                    raise ValueError('Opening meter lacks three observed complete-bar downbeats')
                unconfirmed_opening = True
                segment.update(reason='unconfirmed_opening_meter_and_phase',
                               provisional_display=True)
            else:
                segment.update(applied=True, reason='supported_opening_meter')
        elif meter == signatures[-1]['numerator']:
            segment['reason'] = 'same_as_retained_meter'
            bars = (first+left-boundary)/meter
            segment['phase_alignment_confirmed'] = abs(bars-round(bars)) <= 1e-8
        elif not acoustic_support:
            following_end = starts[si+2] if si+2 < len(starts) else len(beats)
            comparison = _compare_short_meter(beats, numbers, meters, probabilities,
                left, right, following_end, signatures[-1]['numerator'], first, boundary,
                support_threshold, minimum_supported_bars, result.get('parameters') or {})
            short_comparisons.append(comparison)
            segment['short_comparison_index'] = len(short_comparisons)-1
            if comparison['accepted']:
                signatures.append({'beat': first+left, 'numerator': meter, 'denominator': 4})
                boundary = first+left
                paired_recoveries.add(right)
                segment.update(applied=True, reason='supported_bounded_short_meter')
            else:
                segment['reason'] = 'insufficient_observed_complete_bar_downbeats'
                unresolved.append({'start_seconds': beats[left],
                    'end_seconds': comparison.get('window_end_seconds',
                        beats[right] if right < len(beats) else duration),
                    'candidate_start_index': left, 'reason': comparison['reason'],
                    'short_comparison_index': len(short_comparisons)-1,
                    'requires_manual_review': True,
                    'retained_clock_is_not_confirmation': True})
        else:
            bars = (first+left-boundary)/signatures[-1]['numerator']
            if abs(bars-round(bars)) > 1e-8:
                segment['reason'] = 'incompatible_after_rejected_meter_segment'
            else:
                signatures.append({'beat': first+left, 'numerator': meter, 'denominator': 4})
                boundary = first+left
                segment.update(applied=True, reason='supported_meter_change_on_complete_bar')
                if left in paired_recoveries:
                    segment['paired_short_meter_recovery'] = True
        segments.append(segment)
    rejected = [x for x in segments if x['reason'] not in
                ('supported_opening_meter', 'supported_meter_change_on_complete_bar',
                 'supported_bounded_short_meter', 'same_as_retained_meter',
                 'unconfirmed_opening_meter_and_phase')]
    warnings = []
    if unconfirmed_opening:
        warnings.append('开头拍号与小节首拍证据不足，当前仅按解码候选暂显；请在卷帘确认首个完整小节。已保留原解码拍点秒数，不因该提示补拍、删拍或改用整曲修拍规则。')
    if rejected:
        warnings.append(f'{len(rejected)} 段候选变拍缺少足够实测强拍支持或完整小节衔接；保留现有拍号，原始概率与原因已记录。')
    if unresolved:
        locations = '、'.join(f"{s['start_seconds']:.2f}–{s['end_seconds']:.2f} 秒"
                             for s in unresolved[:5])
        more = f'（另有 {len(unresolved)-5} 处，详见速度图诊断）' if len(unresolved) > 5 else ''
        warnings.append(f'{len(unresolved)} 处局部变拍候选未能确认：{locations}{more}。保留的网格不代表已确认，请在卷帘核对这些原曲位置的强拍。')
    phase_conflicts = [s for s in segments if s.get('phase_alignment_confirmed') is False]
    if phase_conflicts:
        positions = '、'.join(f"{s['start_seconds']:.2f} 秒" for s in phase_conflicts[:5])
        warnings.append(f'候选短变拍被拒绝后，后续强拍与保留网格存在相位冲突，起点：{positions}；未伪造短小节补齐，请在卷帘核对。')
    if first:
        warnings.append('音频开头至首节拍为对齐前导段；该段速度仅用于保留音频起点。')
    from .pulse_diagnostics import diagnose_pulse_path
    pulse_review=copy.deepcopy(result.get('pulse_diagnostics'))
    usable_review=(isinstance(pulse_review,dict) and pulse_review.get('version')==1 and
                   isinstance(pulse_review.get('ranges'),list))
    if usable_review:
        for row in pulse_review['ranges']:
            try:
                left,right=float(row['start_seconds']),float(row['end_seconds'])
                usable_review=(math.isfinite(left) and math.isfinite(right) and 0<=left<=right<=duration
                               and isinstance(row.get('types'),list) and isinstance(row.get('evidence'),list))
            except (KeyError,TypeError,ValueError):
                usable_review=False
            if not usable_review:break
    if not usable_review:
        # Old cached results lack the full probability curve. Period changes
        # can still be surfaced, without re-running inference or inventing peaks.
        pulse_review=diagnose_pulse_path(beats)
    if pulse_review.get('ranges'):
        positions='、'.join(f"{row['start_seconds']:.2f}–{row['end_seconds']:.2f} 秒"
                            for row in pulse_review['ranges'][:5])
        warnings.append(f'拍点复核：{positions}。存在弱拍附近强证据、长拍距或半倍速疑点；也可能是真实演奏，请先试听。这些剩余疑点未自动移拍或补拍。')
    data = {'version': 1, 'source': 'beat_this', 'anchors': anchors,
        'pulse_diagnostics':pulse_review,
        'time_signatures': signatures, 'pickup_beats': pickup, 'grid_origin': first,
        'lead_in_alignment': bool(first), 'warnings': warnings,
        'beat_mapping_version': BEAT_MAPPING_VERSION, 'meter_decoder_version': METER_DECODER_VERSION,
        'meter_policy': {'mode': 'beat_synchronous_dbn_with_observed_bar_support',
                         'opening_status': 'unconfirmed' if unconfirmed_opening else 'confirmed'},
        'meter_diagnostics': {'method': 'dbn_bar_observation_gate_v1',
            'support_threshold': support_threshold, 'minimum_supported_bars': minimum_supported_bars,
            'minimum_supported_fraction': .5, 'candidate_segments': segments,
            'short_meter_comparisons': short_comparisons, 'uncertain_intervals': unresolved,
            'retained_phase_conflicts': [{'seconds': s['start_seconds'], 'beat': s['start_beat']}
                                        for s in phase_conflicts],
            'opening_meter_confirmed': not unconfirmed_opening,
            'opening_phase_confirmed': not unconfirmed_opening,
            'decoded_result': copy.deepcopy(result), 'beat_seconds_unchanged': True,
            'pulse_review':pulse_review,
            'pulse_repairs': [], 'timebase_policy': 'decoder_pulses_preserved_no_constant_fit',
            'detected_beats_seconds': beats,
            'detected_downbeats_seconds': [t for t, n in zip(beats, numbers) if n == 1]}}
    TempoMap(data)
    return scale_tempo(data, beat_scale)
