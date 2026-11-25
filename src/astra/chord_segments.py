"""Metric-window adapter for Chorder's existing duration-weighted detector.

This module does not harmonize or rewrite notes. Chorder performs all root,
quality and bass scoring; this adapter supplies score-aligned windows and
rejects labels whose defining tones are absent from the observed window.
"""
from types import SimpleNamespace


def analyze_chord_segments(score, regions=None):
    """Return neutral chord segments and every window's evidence.

    4/4 is split at its half-bar; compound meters use their real metric beats.
    Short/simple triple or odd meters use a complete bar. Pickup, final partial
    bars, and explicit key-region boundaries clip windows. Detection is based
    on notes overlapping each window, including sustained bass and arpeggios.
    Faster harmonic rhythm than these windows is a documented limitation.
    """
    try:
        from chorder import Dechorder
    except ModuleNotFoundError as exc:
        if exc.name not in {'chorder', 'miditoolkit'}:
            raise
        raise ImportError(
            'Experimental Chorder analysis is optional. Install it from the project folder: '
            '.venv\\Scripts\\python.exe -m pip install -e ".[experimental-chords]". '
            'The default chord analysis does not require this extra.'
        ) from exc
    from music21 import harmony, meter, stream

    regions = regions or []
    parts = list(score.parts)
    if not parts:
        raise ValueError('Chord segmentation needs a measured score')
    measures = list(parts[0].getElementsByClass(stream.Measure))
    if not measures:
        raise ValueError('Chord segmentation needs real measure boundaries')
    notes=[]
    for item in score.recurse().notes:
        if isinstance(item, harmony.Harmony) or item.duration.isGrace or item.quarterLength <= 0:
            continue
        start=float(item.getOffsetInHierarchy(score));end=start+float(item.quarterLength)
        notes.extend(SimpleNamespace(pitch=int(p.midi), start=start, end=end) for p in item.pitches)
    diagnostics=[];segments=[]
    for index, measure in enumerate(measures):
        origin=float(measure.getOffsetInHierarchy(score))
        end=(float(measures[index+1].getOffsetInHierarchy(score)) if index+1<len(measures)
             else origin+float(measure.highestTime))
        if end<=origin:continue
        ts=measure.timeSignature or measure.getContextByClass(meter.TimeSignature)
        if ts is None:
            diagnostics.append({'measure_index':index,'measure':measure.number,
                                'reason':'missing_time_signature','accepted':False})
            continue
        offsets=list(map(float,ts.getBeatOffsets()))
        full=float(ts.barDuration.quarterLength)
        metric_ends=offsets+[full]
        if len(offsets)>=4 and len(offsets)%2==0:
            boundaries=[0., offsets[len(offsets)//2], full]
        elif len(offsets)>=2 and all(b-a>1. for a,b in zip(metric_ends,metric_ends[1:])):
            boundaries=metric_ends
        else:
            boundaries=[0.,full]
        padding=float(measure.paddingLeft)
        edges={origin,end}
        edges.update(origin+x-padding for x in boundaries if origin<origin+x-padding<end)
        for region in regions:
            for field in ('start_quarters','end_quarters'):
                if field in region and origin<float(region[field])<end:edges.add(float(region[field]))
        edges=sorted(edges)
        for left,right in zip(edges,edges[1:]):
            overlap=[n for n in notes if n.start<right and n.end>left]
            # Upstream get_chord_map expects already-filtered overlapping notes;
            # otherwise it can sum negative overlap lengths.
            clipped=[SimpleNamespace(pitch=n.pitch,start=max(left,n.start),end=min(right,n.end)) for n in overlap]
            pcs=sorted({n.pitch%12 for n in clipped})
            found,score_value=Dechorder.get_chord_quality(clipped,left,right)
            required=(sorted({(found.root_pc+pc)%12 for pc in Dechorder.chord_maps[found.quality]})
                      if found.is_complete() else [])
            missing=sorted(set(required)-set(pcs))
            accepted=found.is_complete() and len(pcs)>=3 and not missing
            reason=('supported_aggregated_chord' if accepted else 'insufficient_distinct_pitch_classes'
                    if len(pcs)<3 else 'missing_defining_chord_tones' if missing else 'upstream_no_chord')
            window={'measure_index':index,'measure':measure.number,'absolute_quarters':left,
                'offset_quarters':left-origin,'duration_quarters':right-left,
                'beat':float(ts.getBeatProportion(left-origin+padding)),
                'uncertain':not accepted,'roman':None,'chromatic':None,
                'meter':ts.ratioString,'pitch_classes':pcs,'root_pc':found.root_pc,
                'quality':found.quality,'bass_pc':found.bass_pc,'upstream_label':str(found),
                'upstream_score':float(score_value),'score_is_probability':False,
                'required_pitch_classes':required,'missing_pitch_classes':missing,
                'accepted':accepted,'reason':reason,'note_event_count':len(clipped),
                'attack_count':len({n.start for n in overlap if left <= n.start < right}),
                'evidence':'chorder_duration_weighted_metric_window'}
            diagnostics.append(window)
            if not accepted:continue
            item=dict(window,window_indices=[len(diagnostics)-1])
            previous=segments[-1] if segments else None
            key=lambda x:(x['root_pc'],x['quality'],x['bass_pc'])
            # Keep key/measure boundaries explicit even when their labels match.
            region_boundary=any(abs(left-float(r.get('start_quarters',-1)))<1e-8 for r in regions)
            if (previous and previous['measure_index']==index and key(previous)==key(item)
                    and abs(previous['absolute_quarters']+previous['duration_quarters']-left)<1e-8
                    and not region_boundary):
                previous['duration_quarters']+=right-left
                previous['window_indices'].append(len(diagnostics)-1)
                previous['attack_count'] += item['attack_count']
                previous['pitch_classes']=sorted(set(previous['pitch_classes'])|set(pcs))
            else:segments.append(item)
    return {'schema_version':1,'method':'chorder_0.1.4_metric_windows',
        'segments':segments,'windows':diagnostics,
        'accepted_windows':sum(w['accepted'] for w in diagnostics),
        'rejected_windows':sum(not w['accepted'] for w in diagnostics),
        'limitations':['Labels are inferred candidates, not calibrated confidence.',
            'Harmonic changes faster than the metric windows can be missed.',
            'Incomplete chords without all defining tones remain unlabelled.']}
