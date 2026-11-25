"""Read-only, conservative tempo-trend proposals from explicit pulse timestamps.

Confidence is an evidence score, NOT a calibrated probability. No candidate is
an instruction to change timing. Thresholds are exposed in evidence; local MAD
sets the noise threshold so tiny monotone timing jitter is not called a trend.
"""
import math
from statistics import median


def _evidence(values):
    n = len(values)
    slope = median((values[j]-values[i])/(j-i)
                   for i in range(n) for j in range(i+1, n))
    intercept = median(v-slope*i for i, v in enumerate(values))
    residual = [v-(intercept+slope*i) for i,v in enumerate(values)]
    noise = 1.4826*median(abs(v-median(residual)) for v in residual)
    base = median(values)
    change = slope*(n-1)
    # Pairwise agreement tolerates local reversals but not a single step jump.
    pairs = [(values[j]-values[i])*slope for i in range(n) for j in range(i+1,n)]
    agreement = sum(v > 0 for v in pairs)/len(pairs)
    threshold = max(6*noise, .06*base)
    accepted = abs(change) >= threshold and agreement >= .8
    return dict(slope_seconds_per_quarter_per_interval=slope,
                fitted_change_seconds_per_quarter=change,
                relative_change=change/base, residual_mad_seconds_per_quarter=noise,
                pairwise_direction_agreement=agreement,
                required_change_seconds_per_quarter=threshold,
                interval_count=n, accepted=accepted)


def detect_tempo_trends(tempo, *, min_intervals=6, window_intervals=16):
    """Return preview candidates, suspicious intervals and warnings; never mutate.

    Candidates use inclusive point indices (end-start = interval_count), source
    seconds, direction accelerando/ritardando, and evidence. Quarter spacing is
    read from consecutive beat coordinates, else step_quarters, else unit pulses.
    Missing explicit points yields no inferred candidates. Near-octave jumps
    cannot be distinguished from missing beats or deliberate jumps here and are
    flagged, never silently corrected. Work is linear in recording length for a
    fixed window size.
    """
    if type(min_intervals) is not int or min_intervals < 6:
        raise ValueError('min_intervals must be an integer >= 6')
    if type(window_intervals) is not int or window_intervals < min_intervals or window_intervals > 64:
        raise ValueError('window_intervals must be between min_intervals and 64')
    result = dict(candidates=[], suspicious_intervals=[], warnings=[],
                  source_seconds_unchanged=True)
    points = tempo.get('pulse_points')
    if not points or len(points) < min_intervals+1:
        result['warnings'].append('Insufficient explicit pulse points; no trend inferred.')
        return result
    values = []
    fallback = False
    for i,(a,b) in enumerate(zip(points,points[1:])):
        dt = float(b['seconds'])-float(a['seconds'])
        if 'beat' in a and 'beat' in b:
            unit = float(b['beat'])-float(a['beat'])
        else:
            fallback = True
            unit = float(a.get('step_quarters',1))
        if not math.isfinite(dt) or not math.isfinite(unit) or dt <= 0 or unit <= 0:
            raise ValueError('Pulse seconds and musical positions must be finite and strictly increasing.')
        values.append(dt/unit)
    if fallback:
        result['warnings'].append('Some beat coordinates absent; step_quarters or unit pulses used. Verify beat interpretation.')
    blocked = set()
    for i in range(1,len(values)):
        ratio = values[i]/values[i-1]
        if ratio >= 1.6 or ratio <= 1/1.6:
            blocked.update((i-1,i))
            result['suspicious_intervals'].append(dict(
                start_point_index=i-1,end_point_index=i+1,
                start_seconds=points[i-1]['seconds'],end_seconds=points[i+1]['seconds'],
                interval_ratio=ratio,
                reason='Abrupt tempo ratio: possible missing/extra pulse, half/double interpretation, or intentional jump.'))
    hits=[]
    # Check bounded windows, including short phrases at either end.
    for start in range(len(values)-min_intervals+1):
        for size in sorted(set((min_intervals,min(window_intervals,len(values)-start)))):
            end=start+size
            if any(i in blocked for i in range(start,end)):
                continue
            evidence=_evidence(values[start:end])
            if evidence['accepted']:
                direction='accelerando' if evidence['relative_change'] < 0 else 'ritardando'
                hits.append((start,end,direction))
    merged=[]
    for start,end,direction in sorted(hits):
        if merged and direction==merged[-1][2] and start < merged[-1][1]:
            # Only merge if the entire span still has a coherent trend. Bound
            # evidence sampling for long recordings, not the actual duration.
            a,b,d=merged[-1]
            sampled=values[a:max(b,end)]
            if len(sampled)>64:
                sampled=[sampled[round(i*(len(sampled)-1)/63)] for i in range(64)]
            if _evidence(sampled)['accepted']:
                merged[-1]=(a,max(b,end),d)
                continue
        merged.append((start,end,direction))
    for start,end,direction in merged:
        sample=values[start:end]
        stride=max(1,math.ceil(len(sample)/64))
        evidence=_evidence(sample[::stride])
        evidence['sample_stride_intervals']=stride
        evidence['interval_count']=end-start
        confidence=min(.99, .5*evidence['pairwise_direction_agreement']+
                       .5*min(1,abs(evidence['fitted_change_seconds_per_quarter'])/
                              (2*evidence['required_change_seconds_per_quarter'])))
        result['candidates'].append(dict(start_point_index=start,end_point_index=end,
            start_seconds=points[start]['seconds'],end_seconds=points[end]['seconds'],
            start_pulse_id=points[start].get('id'),end_pulse_id=points[end].get('id'),
            direction=direction,confidence=confidence,confidence_kind='heuristic_evidence_score',
            evidence=evidence))
    return result
