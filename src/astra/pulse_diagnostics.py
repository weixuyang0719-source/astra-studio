"""Small, non-destructive review hints; no musical truth or auto correction."""
from __future__ import annotations

import math
from statistics import median

from .acoustic_snap import suggest_acoustic_snap


def diagnose_pulse_path(beat_seconds, beat_probability=None, *, activations=None,
                        fps=50., max_hints=5):
    """Inspect an existing path, without re-decoding or touching its seconds.

    Thresholds are Astra review policies, not calibrated correctness scores.
    Selected-pulse-only input cannot establish a missed acoustic onset. A long
    interval or half/double ratio can be real music, and is labelled accordingly.
    """
    beats=[float(t) for t in beat_seconds]
    if any(not math.isfinite(t) or t<0 for t in beats) or any(b<=a for a,b in zip(beats,beats[1:])):
        raise ValueError('Diagnostic pulse seconds must be finite and increasing')
    if type(max_hints) is not int or not 1<=max_hints<=10:
        raise ValueError('Diagnostic hint limit must be 1–10')
    fps=float(fps)
    if not math.isfinite(fps) or fps<=0:
        raise ValueError('Diagnostic frame rate must be positive')
    probabilities=None if beat_probability is None else [float(p) for p in beat_probability]
    if probabilities is not None and (len(probabilities)!=len(beats) or
            any(not math.isfinite(p) or not 0<=p<=1 for p in probabilities)):
        raise ValueError('Diagnostic pulse probabilities must match and lie in [0,1]')
    gaps=[b-a for a,b in zip(beats,beats[1:])]
    found=[]

    def add(kind,start,end,evidence,priority):
        found.append(dict(start_seconds=max(0.,start),end_seconds=end,types=[kind],
                          evidence=[dict(type=kind,**evidence)],priority=priority,
                          requires_manual_review=True))

    if activations is not None and len(beats)>1:
        # Validate only the bounded windows used by the existing snap kernel.
        for i,t in enumerate(beats):
            frame=round(t*fps)
            if not 0<=frame<len(activations):
                continue
            p=float(activations[frame])
            if not math.isfinite(p) or not 0<=p<=1:
                raise ValueError('Invalid diagnostic acoustic evidence')
            if p>=.2:
                continue
            local=gaps[max(0,i-2):min(len(gaps),i+2)]
            bpm=60/median(local)
            suggestion=suggest_acoustic_snap(activations,t,bpm,fps=fps,
                min_probability=.5,min_contrast=.4)
            if (suggestion['accepted'] and suggestion['source_probability']-p>=.4 and
                    abs(suggestion['delta_seconds'])>=max(.04,1.5/fps)-1e-9):
                target=suggestion['candidate_seconds']
                add('weak_pulse_near_strong_evidence',min(t,target)-1/fps,
                    max(t,target)+1/fps,dict(pulse_index=i,seconds=t,probability=p,
                    nearby_seconds=target,nearby_probability=suggestion['source_probability'],
                    displacement_seconds=target-t,weighted_score=suggestion['weighted_score'],
                    interpretation='review_alignment_not_automatic_move'),3)

    def stable(values):
        centre=median(values)
        return all(abs(v/centre-1)<=.12 for v in values)

    for i,gap in enumerate(gaps):
        if i>=2 and i+2<len(gaps):
            context=gaps[i-2:i]+gaps[i+1:i+3]
            neighbour=median(context)
            if stable(context) and 1.65<=gap/neighbour<=3.2:
                add('isolated_long_interval',beats[i-1],beats[i+2],dict(
                    interval_index=i,interval_seconds=gap,neighbour_interval_seconds=neighbour,
                    ratio=gap/neighbour,
                    interpretation='could_be_missing_pulse_or_real_hold_no_inserted_beat'),2)
        if i>=4 and i+4<=len(gaps):
            before,after=gaps[i-4:i],gaps[i:i+4]
            ratio=median(after)/median(before)
            if stable(before) and stable(after) and (1.7<=ratio<=2.3 or 1/2.3<=ratio<=1/1.7):
                add('tempo_level_change',beats[i-1],beats[i+1],dict(
                    boundary_index=i,seconds=beats[i],before_interval_seconds=median(before),
                    after_interval_seconds=median(after),ratio=ratio,
                    interpretation='could_be_half_double_interpretation_or_real_tempo_change'),1)

    total=len(found)
    merged=[]
    for row in sorted(found,key=lambda x:x['start_seconds']):
        if merged and row['start_seconds']<=merged[-1]['end_seconds']+.5:
            previous=merged[-1]
            previous['end_seconds']=max(previous['end_seconds'],row['end_seconds'])
            previous['types']=sorted(set(previous['types']+row['types']))
            previous['evidence'].extend(row['evidence'])
            previous['priority']=max(previous['priority'],row['priority'])
        else:
            merged.append(row)
    selected=sorted(merged,key=lambda x:(-x['priority'],x['start_seconds']))[:max_hints]
    selected.sort(key=lambda x:x['start_seconds'])
    for row in selected:
        row.pop('priority')
    return dict(version=1,source='full_activation' if activations is not None else 'selected_pulses_only',
                ranges=selected,total_candidate_count=total,
                omitted_range_count=max(0,len(merged)-max_hints),
                automatic_correction=False,source_events_modified=False,
                policy='Astra review thresholds; no new DBN run, no probability-as-truth claim')
