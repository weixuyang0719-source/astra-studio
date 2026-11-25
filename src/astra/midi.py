"""SMF type 1 with a shared tempo track for every export."""
from pathlib import Path
import math
import mido
from .timing import TempoMap
from .boundary_meter import boundary_time_signature

PPQ = 9600


def _encoding_anchors(tempo):
    """Coalesce only sub-resolution floating-point boundary duplicates in SMF.

    The effective map is untouched. Events still use that original map; a
    same-tick cluster can represent only one instant. Keep the last absolute
    target (the origin stays zero) and reject meaningful time spans. Absolute
    elapsed correction in tempo_events prevents repeated clusters accumulating
    discarded time throughout the song.
    """
    result=[]
    first_seconds=None
    for anchor in tempo.anchors:
        tick=round(anchor['beat']*PPQ)
        if result and tick==round(result[-1]['beat']*PPQ):
            if anchor['seconds']-first_seconds>0.0001+1e-12:
                raise ValueError('Tempo 锚点过于密集，超出 MIDI 分辨率；同一刻度内有超过0.1毫秒的时间变化')
            if tick!=0:result[-1]=anchor
        else:
            result.append(anchor)
            first_seconds=anchor['seconds']
    if len(result)<2:raise ValueError('Tempo 范围过短，无法按 MIDI 分辨率表示')
    return result


def _track(events):
    track=mido.MidiTrack()
    previous=0
    for tick,priority,msg in sorted(events,key=lambda e:(e[0],e[1])):
        track.append(msg.copy(time=tick-previous))
        previous=tick
    track.append(mido.MetaMessage('end_of_track',time=0))
    return track


def tempo_events(tempo):
    events=[(0,-10,mido.MetaMessage('track_name',name='Astra Tempo / original audio starts at 0'))]
    elapsed=0.
    anchors=_encoding_anchors(tempo)
    for i,(a,b) in enumerate(zip(anchors,anchors[1:])):
        tick=round(a['beat']*PPQ)
        next_tick=round(b['beat']*PPQ)
        if next_tick <= tick:
            raise ValueError('Tempo 锚点过于密集，超出 MIDI 分辨率')
        # Carry rounding error forward against absolute audio time, avoiding
        # accumulation even after thousands of changes.
        micro=round((b['seconds']-elapsed)*PPQ/(next_tick-tick)*1e6)
        if not 1<=micro<=16777215:
            raise ValueError('Tempo 超出 MIDI 范围')
        events.append((tick,0,mido.MetaMessage('set_tempo',tempo=micro)))
        elapsed += (next_tick-tick)/PPQ*micro/1e6
    pickup=tempo.data.get('pickup_beats',0)
    if pickup:
        # SMF has no pickup flag. Encode a short opening meter, then restore the
        # written meter at the first full bar, so DAW bar lines match the score.
        short=boundary_time_signature(pickup,(tempo.data['time_signatures'][0]['denominator'],))
        events.append((0,1,mido.MetaMessage('time_signature',**short)))
        events.append((0,2,mido.MetaMessage('marker',text='Pickup / audio lead-in')))
        events.append((round(pickup*PPQ),2,mido.MetaMessage('marker',text='First full bar')))
    for index,s in enumerate(tempo.data['time_signatures']):
        position=pickup if index==0 and pickup else s['beat']
        events.append((round(position*PPQ),1,mido.MetaMessage('time_signature',
            numerator=s['numerator'],denominator=s['denominator'])))
    _silent_leadin_boundary_events(tempo, events)
    _intro_boundary_events(tempo, events)
    _short_measure_events(tempo, events)
    from .free_meter import free_ranges
    for a in free_ranges(tempo.data):
        events.append((round(a['start_beat']*PPQ),2,mido.MetaMessage('marker',text='Free measure / source timing retained; SMF has no unmetered signature')))
        events.append((round(a['end_beat']*PPQ),2,mido.MetaMessage('marker',text='End of free measure')))
    _ending_boundary_events(tempo, events)
    return events


def _silent_leadin_boundary_events(tempo, events):
    """Keep source time zero while aligning the first written normal measure.

    SMF cannot omit silent time. Only its conductor grouping is shortened;
    this does not label silence as a musical pickup or change tempo events.
    """
    data=tempo.data
    if data.get('opening_mode')!='normal' or not data.get('score_start_seconds'):
        return
    first=tempo.to_beat(data['score_start_seconds'])
    if first<=1e-8:return
    rows=data['time_signatures']
    following=next(row for row in reversed(rows) if row['beat']<=first+1e-8)
    # Derive the silent prefix with normal source-origin grouping, rather than
    # score geometry which intentionally excludes this prefix.
    from copy import deepcopy
    from .duet_notation import reference_bars
    plain=deepcopy(data)
    for key in ('score_start_seconds','first_full_bar_beat','grid_origin','opening_mode'):
        plain.pop(key,None)
    starts,lengths,_=reference_bars(TempoMap(plain),first)
    start=max(q for q in starts if q<first-1e-8)
    prior=next(row for row in reversed(rows) if row['beat']<=start+1e-8)
    length=first-start
    if length<prior['numerator']*4/prior['denominator']-1e-8:
        tick=round(start*PPQ)
        try:
            short=boundary_time_signature(length,(prior['denominator'],following['denominator']))
        except ValueError:
            events.append((tick,2,mido.MetaMessage('marker',text='Silent lead-in / timing retained; external DAW bar reset not guaranteed')))
        else:
            events[:]=[event for event in events if not(event[0]==tick and event[2].type=='time_signature')]
            events.append((tick,1,mido.MetaMessage('time_signature',**short)))
    restore=round(first*PPQ)
    if not any(t==restore and msg.type=='time_signature' for t,_,msg in events):
        events.append((restore,1,mido.MetaMessage('time_signature',numerator=following['numerator'],denominator=following['denominator'])))
    events.append((restore,2,mido.MetaMessage('marker',text='First full bar / preceding silence excluded from score')))


def _short_measure_events(tempo,events):
    rows=tempo.data['time_signatures']
    for mark in tempo.data.get('short_bar_annotations',[]):
        start=tempo.to_beat(mark['start_seconds']);end=tempo.to_beat(mark['end_seconds'])
        prior=next(row for row in reversed(rows) if row['beat']<=start+1e-8)
        following=next(row for row in reversed(rows) if row['beat']<=end+1e-8)
        tick,restore=round(start*PPQ),round(end*PPQ)
        try:short=boundary_time_signature(end-start,(prior['denominator'],following['denominator']))
        except ValueError:
            events.append((tick,2,mido.MetaMessage('marker',text='Confirmed short measure / timing retained; external DAW bar reset not guaranteed')))
        else:
            events[:]=[e for e in events if not(e[0]==tick and e[2].type=='time_signature')]
            events.append((tick,1,mido.MetaMessage('time_signature',**short)))
        if not any(t==restore and msg.type=='time_signature' for t,_,msg in events):
            events.append((restore,1,mido.MetaMessage('time_signature',numerator=following['numerator'],denominator=following['denominator'])))
        events.append((restore,2,mido.MetaMessage('marker',text='End of confirmed short measure')))


def _ending_boundary_events(tempo, events):
    from .score_boundary import ending_measure
    ending = ending_measure(tempo)
    if ending is None:
        return
    start, end = round(ending['start'] * PPQ), round(ending['end'] * PPQ)
    if ending['partial']:
        events[:] = [event for event in events if not (event[0] == start and event[2].type == 'time_signature')]
        events.append((start, 1, mido.MetaMessage('time_signature', **ending['meter'])))
        # The declaration does not cut tails. Resume the surrounding grouping
        # after it for any still-playing source events, unless an authored
        # signature already exists exactly there.
        if not any(t == end and msg.type == 'time_signature' for t, _, msg in events):
            meter = next(s for s in reversed(tempo.data['time_signatures']) if s['beat'] <= ending['end'] + 1e-8)
            events.append((end, 1, mido.MetaMessage('time_signature', numerator=meter['numerator'], denominator=meter['denominator'])))
    events.append((end, 2, mido.MetaMessage('marker', text='Confirmed score end / performance tails retained')))


def _intro_boundary_events(tempo, events):
    """Express only the final partial bar of an explicitly confirmed intro.

    Earlier complete intro bars and all mid-song meter events stay intact.
    Mixed legacy pickup/first_full metadata keeps its existing geometry.
    """
    data=tempo.data
    if data.get('pickup_beats'):
        return
    first=data.get('first_full_bar_beat',0)
    if (not isinstance(first,(int,float)) or not math.isfinite(first) or first<=1e-8
            or first>tempo.beats[-1]+1e-8):
        return
    rows=data['time_signatures']
    explicit=data.get('opening_mode')=='intro' or any(
        row.get('origin')=='editor_first_bar' and abs(row['beat']-first)<1e-8 for row in rows)
    if not explicit:
        return
    # Reuse the same pure, model-free geometry as constrained score export.
    from .duet_notation import reference_bars
    starts,lengths,_=reference_bars(tempo,first)
    previous=max(index for index,start in enumerate(starts) if start<first-1e-8)
    start,length=starts[previous],lengths[previous]
    prior=next(row for row in reversed(rows) if row['beat']<=start+1e-8)
    following=next(row for row in reversed(rows) if row['beat']<=first+1e-8)
    if abs(start+length-first)>1e-8 or length>=prior['numerator']*4/prior['denominator']-1e-8:
        return
    tick=round(start*PPQ);restore=round(first*PPQ)
    try:
        short=boundary_time_signature(length,(prior['denominator'],following['denominator']))
    except ValueError:
        # A free intro is not a forced short metrical measure. SMF cannot encode
        # arbitrary fractions as a time signature; retain its existing meter,
        # identify the boundary and reset the written meter at the exact tick.
        # Never quantize the source clock to manufacture a representable bar.
        events.append((tick,2,mido.MetaMessage('marker',text='Free intro / timing retained; external DAW bar reset not guaranteed')))
    else:
        events[:]=[event for event in events if not (event[0]==tick and event[2].type=='time_signature')]
        events.append((tick,1,mido.MetaMessage('time_signature',**short)))
    if not any(t==restore and msg.type=='time_signature' for t,_,msg in events):
        events.append((restore,1,mido.MetaMessage('time_signature',
            numerator=following['numerator'],denominator=following['denominator'])))
    events.append((restore,2,mido.MetaMessage('marker',text='First full bar / confirmed intro')))


EXPORT_PURPOSES = ('mapped', 'target_grid', 'seconds_120')


def export_conductor(tempo, purpose='mapped', include_time_signatures=True):
    """Encode the selected use without ever modifying the source clock.

    target_grid deliberately requires the receiver to supply this same clock.
    seconds_120 uses SMF's default 120 BPM; it does not retain musical bar geometry.
    Meter events, if requested, are placed at their corresponding real seconds.
    """
    if purpose not in EXPORT_PURPOSES:
        raise ValueError('未知 MIDI 导出用途')
    result = []
    for tick, priority, message in tempo_events(tempo):
        if message.type == 'set_tempo' and purpose != 'mapped':
            continue
        if message.type == 'time_signature' and not include_time_signatures:
            continue
        if purpose == 'seconds_120':
            tick = round(tempo.to_seconds(tick / PPQ) * 2 * PPQ)
        result.append((tick, priority, message))
    result.append((0, -8, mido.MetaMessage('text', text={
        'mapped': 'Effective project tempo; source seconds mapped once.',
        'target_grid': 'No tempo; requires identical target tempo map and origin.',
        'seconds_120': 'No tempo; seconds encoded at default 120 BPM; musical grid not retained.'
    }[purpose])))
    return result


def control_events(tempo, *, controls=None, pedals=None, channels=(0,), purpose='mapped'):
    """Raw CC samples keep values/channels; legacy model pedal intervals broadcast.

    An explicit controls list (including []) is authoritative over pedal intervals.
    This avoids adding a second binary pedal over edited/half-pedal samples.
    """
    if purpose not in EXPORT_PURPOSES:
        raise ValueError('未知 MIDI 导出用途')
    samples = controls
    if samples is None:
        samples = []
        for pedal in pedals or []:
            if pedal['number'] not in (64, 66, 67):
                raise ValueError('不支持的踏板控制号')
            begin, end = float(pedal['start']), float(pedal['end'])
            if not math.isfinite(end) or end <= begin:
                raise ValueError('踏板区间必须具有正时长')
            for channel in channels:
                samples.extend(({'seconds': begin, 'number': pedal['number'], 'value': pedal.get('value', 127), 'channel': channel},
                                {'seconds': end, 'number': pedal['number'], 'value': pedal.get('release_value', 0), 'channel': channel}))
    events = []
    for sample in samples:
        seconds = float(sample.get('seconds', sample.get('time', -1)))
        number, value, channel = sample.get('number', sample.get('control')), sample.get('value'), sample.get('channel', 0)
        if (not math.isfinite(seconds) or seconds < 0 or type(number) is not int or not 0 <= number <= 127
                or type(value) is not int or not 0 <= value <= 127 or type(channel) is not int or not 0 <= channel <= 15):
            raise ValueError('无效 CC 秒事件、控制号、数值或通道')
        beat = seconds * 2 if purpose == 'seconds_120' else tempo.to_beat(seconds)
        # Stable input order at the same tick; all controllers precede attacks.
        events.append((round(beat * PPQ), 1, mido.Message('control_change', channel=channel, control=number, value=value)))
    return events


def preserved_events(tempo, events, purpose='mapped'):
    """Retain non-note source events in seconds; clock metadata has one owner."""
    result = []
    for event in events or []:
        seconds = float(event['seconds'])
        if not math.isfinite(seconds) or seconds < 0:
            raise ValueError('无效原始事件秒时间')
        data = dict(event['message'])
        kind = data.get('type')
        if kind in ('set_tempo', 'time_signature', 'end_of_track'):
            continue  # Deliberately replaced by the effective project map.
        if kind in ('note_on', 'note_off', 'control_change'):
            raise ValueError('音符和 CC 需通过各自事件集合导出，不能重复编码')
        data['time'] = 0
        try:
            message = mido.Message.from_dict(data)
        except LookupError:
            message = mido.MetaMessage.from_dict(data)
        beat = seconds * 2 if purpose == 'seconds_120' else tempo.to_beat(seconds)
        result.append((round(beat * PPQ), 1, message))
    return result


def write_midi(path, notes, tempo_data, instrument='piano', quantized=False, pedals=None,
               *, controls=None, purpose='mapped', include_time_signatures=True, events=None):
    tempo=TempoMap(tempo_data)
    mid=mido.MidiFile(type=1,ticks_per_beat=PPQ)
    mid.tracks.append(_track(export_conductor(tempo, purpose, include_time_signatures)))
    if notes or pedals or controls or events:
        original_events = preserved_events(tempo, events, purpose)
        events=[(0,-2,mido.MetaMessage('track_name',name=instrument))]
        channels=[c for c in range(16) if c!=9]
        programmed = {message.channel for _, _, message in original_events if message.type == 'program_change'}
        events.extend((0,-1,mido.Message('program_change',channel=c,program=0 if instrument=='piano' else 24)) for c in channels if c not in programmed)
        events.extend(original_events)
        active={}
        events.extend(control_events(tempo, controls=controls, pedals=pedals, channels=channels, purpose=purpose))
        for n in sorted(notes,key=lambda n:n['start_beat'] if quantized and purpose != 'seconds_120' else n['start']):
            if purpose == 'seconds_120':
                start, end = float(n['start']) * 2, float(n['end']) * 2
            else:
                start=n['start_beat'] if quantized else tempo.to_beat(n['start'])
                end=n['end_beat'] if quantized else tempo.to_beat(n['end'])
            a=round(start*PPQ); b=max(a+1,round(end*PPQ))
            pitch=int(n['pitch'])
            used=active.setdefault(pitch,{})
            explicit=n.get('channel')
            if explicit is not None and (type(explicit) is not int or not 0 <= explicit <= 15):
                raise ValueError('无效音符 MIDI 通道')
            # Explicit source channels are performance data, including legal
            # repeated note-on overlap. Do not reroute them away from their CC.
            channel=explicit if explicit is not None else next((c for c in channels if used.get(c,-1)<=a),None)
            if channel is None:raise ValueError('同音高重叠超过 MIDI 15 个旋律通道，请检查模型输出')
            used[channel]=b
            events.extend([(a,3,mido.Message('note_on',channel=channel,note=pitch,velocity=max(1,min(127,int(n['velocity']))))),
                (b,2,mido.Message('note_off',channel=channel,note=pitch,velocity=0))])
        mid.tracks.append(_track(events))
    path=Path(path)
    temporary=path.with_suffix('.mid.tmp')
    mid.save(str(temporary))
    temporary.replace(path)


def import_tempo_midi(path, duration=1.):
    """Read an existing DAW tempo track, including meter changes, at time zero."""
    mid=mido.MidiFile(path)
    if mid.type==2 or mid.ticks_per_beat<=0:
        raise ValueError('请导入 PPQ 格式的同步 MIDI Tempo Track')
    ticks=0; seconds=0.; micro=500000
    anchors=[{'seconds':0.,'beat':0.}]
    meters=[{'beat':0.,'numerator':4,'denominator':4}]
    for msg in mido.merge_tracks(mid.tracks):
        ticks+=msg.time
        seconds+=msg.time/mid.ticks_per_beat*micro/1e6
        beat=ticks/mid.ticks_per_beat
        if msg.type=='set_tempo':
            if seconds>anchors[-1]['seconds']:
                anchors.append({'seconds':seconds,'beat':beat})
            micro=msg.tempo
        if msg.type=='time_signature':
            entry={'beat':beat,'numerator':msg.numerator,'denominator':msg.denominator}
            if meters[-1]['beat']==beat:
                meters[-1]=entry
            else:
                meters.append(entry)
    end=max(duration,seconds,anchors[-1]['seconds']+1)
    anchors.append({'seconds':end,'beat':ticks/mid.ticks_per_beat+(end-seconds)*1e6/micro})
    data={'version':1,'source':'imported_midi','anchors':anchors,'time_signatures':meters,'warnings':[]}
    TempoMap(data)
    return data
