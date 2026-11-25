"""Uniform intervals with bounded MIDI-onset suggestions, no model inference."""
import math
from bisect import bisect_left, bisect_right


def propose(start, end, count, onsets, *, snap=True):
    if (type(count) is not int or not 1 <= count <= 4096 or
            not all(isinstance(v,(int,float)) and not isinstance(v,bool) and math.isfinite(v) for v in (start,end)) or end<=start):
        raise ValueError('Invalid division range or count')
    step=(end-start)/count
    if step <= 4e-8: raise ValueError('Intervals too small to edit safely')
    # Disjoint search windows guarantee uniqueness and strict order without
    # silently dropping a point or borrowing a distant onset from a neighbour.
    radius=min(.12,step*.30)
    candidates=sorted(set(float(t) for t in onsets if isinstance(t,(int,float)) and not isinstance(t,bool) and math.isfinite(t) and start<t<end))
    rows=[]
    for i in range(1,count):
        uniform=start+step*i
        near=candidates[bisect_left(candidates,uniform-radius):bisect_right(candidates,uniform+radius)] if snap else []
        target=min(near,key=lambda t:(abs(t-uniform),t)) if near else uniform
        ambiguous=len(near)>1 and sorted(abs(t-uniform) for t in near)[1]-abs(target-uniform)<.015
        rows.append(dict(index=i,uniform_seconds=uniform,seconds=target,delta_seconds=target-uniform,
                         status='ambiguous_onset' if ambiguous else 'midi_onset' if near else 'no_nearby_onset' if snap else 'uniform',
                         nearby_onset_count=len(near)))
    return dict(beat_seconds=[start]+[r['seconds'] for r in rows]+[end],points=rows,
                radius_seconds=radius,source='current_midi_note_onsets' if snap else 'uniform_seconds',
                model_used=False,interval_count=count,endpoints_fixed=True)
