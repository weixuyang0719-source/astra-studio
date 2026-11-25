"""Prepare immutable click events off the realtime thread; native scheduler times them.
Matches MidiPreview woodblock accents, meter changes and grid origin. No timer/device.
"""
import copy,math
from types import SimpleNamespace

def metronome_clock(data):
    from astra.timing import TempoMap
    tempo=copy.deepcopy(data)
    origin=float(tempo.get('grid_origin',tempo.get('pickup_beats',0)))
    if not math.isfinite(origin):raise ValueError('invalid metronome grid origin')
    # Origin is beat phase, not a negative timeline time. TempoMap only converts
    # nonnegative beats; normalize its validation copy, never the authored data.
    tempo['grid_origin']=max(0.,origin)
    return TempoMap(tempo),tempo,origin

def metronome_beats(tempo, total):
    """Yield (score seconds, accent) without modifying the tempo or performance."""
    # A session-scoped table prepared by the native recording preview. Keep
    # ordinary click conversion identical, and never infer another BPM map here.
    if isinstance(tempo,dict) and '_review_click_table' in tempo:
        for seconds,accent in tempo['_review_click_table']:
            if 0 <= seconds < total:
                yield seconds,accent
        return
    clock,tempo,origin=metronome_clock(tempo);meters=tempo['time_signatures']
    count=0
    for i,meter in enumerate(meters):
        start=float(meter['beat']);stop=meters[i+1]['beat'] if i+1<len(meters) else clock.to_beat(total)
        if i==0:start=origin
        unit=4/meter['denominator']
        index=math.ceil((0-start)/unit-1e-10) if i==0 else 0
        while start+index*unit<stop-1e-8:
            beat=max(0.,start+index*unit)
            at=clock.to_seconds(beat)
            if at>=total:break
            count+=1
            if count>100000:raise ValueError('metronome event budget exceeded')
            yield at,index%meter['numerator']==0
            index+=1

def review_click_table(base, draft, operation, total):
    """One exact half-open ownership rule for visible and audible beats."""
    regions = operation.get('regions') or [operation]
    def covered(at):
        return any((at >= q['start'] if q.get('include_start', True) else at > q['start'])
                   and (at <= q['end'] if q.get('include_end', True) else at < q['end'])
                   for q in regions)
    def explicit(tempo):
        points = tempo.get('pulse_points')
        if points is None:
            return ()  # Never invent click beats from the meter during review.
        from bisect import bisect_right
        meters = tempo['time_signatures']
        starts = [float(m['beat']) for m in meters]
        first_full = float(tempo.get('first_full_bar_beat', tempo.get('grid_origin', 0)) or 0)
        prelude = isinstance(tempo.get('opening_policy'), dict)
        def accent_at(point):
            beat = float(point['beat'])
            i = max(0, bisect_right(starts, beat+1e-8)-1)
            m = meters[i]
            if m.get('free'):
                return False
            start = starts[i]
            if i == 0 and beat >= first_full-1e-8 and (prelude or first_full > 0):
                start = first_full
            width = 4.0*m['numerator']/m['denominator']
            diff = beat-start
            return diff >= -1e-8 and abs(diff-round(diff/width)*width)<=1e-8
        return ((p['seconds'],accent_at(p)) for p in points if 0 <= p['seconds'] < total)
    merged = {}
    for t, a in explicit(base):
        if not covered(t):
            merged[t] = a
    for t, a in explicit(draft):
        if covered(t):
            merged[t] = a
    if len(merged)>100000:
        raise ValueError('review click event budget exceeded')
    return tuple(sorted(merged.items()))


def with_metronome(prepared, metronome=(None,False), duration=0):
    """Compatibility for callers that still build a combined event stream."""
    import mido
    events,notes,base,metadata=prepared
    total=max(float(base),float(duration))
    tempo,enabled=metronome
    if not enabled:return events,notes,total,metadata
    events=list(events);notes=list(notes)
    for at,accent in metronome_beats(tempo,total):
        pitch,velocity=(76,95) if accent else (77,60)
        end=min(total,at+.045)
        events.extend(((at,mido.Message('note_on',channel=9,note=pitch,velocity=velocity)),(end,mido.Message('note_off',channel=9,note=pitch,velocity=0))))
        notes.append(SimpleNamespace(start=at,end=end,sounding_end=end,channel=9,pitch=pitch,velocity=velocity))
    events.sort(key=lambda row:row[0])
    return events,notes,total,metadata
