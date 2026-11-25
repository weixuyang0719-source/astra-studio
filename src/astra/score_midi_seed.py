"""Project the explicitly authored Notes opening/free bars into a new native seed."""
from copy import deepcopy
from fractions import Fraction
from pathlib import Path
import xml.etree.ElementTree as ET

from .timing import TempoMap


def needs_seed_projection(reference):
    return bool(isinstance(reference, dict) and (
        type(reference.get('opening_policy', {}).get('keep_before_bar1')) is bool or
        any(row.get('free') for row in reference.get('time_signatures', ()))))


def _local_reference(clock, origin):
    origin_seconds=clock.to_seconds(origin)
    active=next(row for row in reversed(clock.data['time_signatures']) if row['beat']<=origin+1e-8)
    data=dict(anchors=[dict(beat=0.,seconds=0.)]+[
        dict(beat=q-origin,seconds=seconds-origin_seconds)
        for q,seconds in zip(clock.beats,clock.seconds) if q>origin+1e-8],
        time_signatures=[dict(active,beat=0.)]+[
            dict(row,beat=row['beat']-origin)
            for row in clock.data['time_signatures'] if row['beat']>origin+1e-8])
    if 'notation_tempo_marks' in clock.data:
        data['notation_tempo_marks']=[dict(mark,beat=max(0.,mark['beat']-origin))
            for mark in clock.data['notation_tempo_marks'] if mark['beat']>=origin-1e-8]
    return data, origin_seconds


def _hide_unmetered_barline(measure):
    if measure.find("barline[@location='right']") is None:
        edge=ET.SubElement(measure,'barline',location='right')
        ET.SubElement(edge,'bar-style').text='none'


def project_free_barlines(raw):
    """Hide default visual edges in generated free measures, not authored marks."""
    if b'<senza-misura' not in raw:
        return raw
    root=ET.fromstring(raw)
    for measure in root.findall('part/measure'):
        if measure.find('attributes/time/senza-misura') is not None:
            _hide_unmetered_barline(measure)
    return ET.tostring(root,encoding='utf-8',xml_declaration=True)


def project_midi_seed(source, reference, output):
    """Generate a fresh score-local seed; neither source MIDI nor Notes clock is written."""
    from .midi_preview import _read_midi
    from .duet_notation import reference_bars
    from .score_range import _draft
    from .score_tempo import apply_reference_tempo, _measure_layout
    from .score_origin import score_opening_meter_rows
    clock=TempoMap(deepcopy(reference))
    keep=reference.get('opening_policy',{}).get('keep_before_bar1')
    origin=float(reference.get('first_full_bar_beat',0)) if keep is False else 0.
    origin_seconds=clock.to_seconds(origin)
    _,raw,_=_read_midi(source)
    notes=[dict(id=str(i),pitch=n.pitch,velocity=n.velocity,start=max(n.start,origin_seconds),end=n.end)
           for i,n in enumerate(raw) if n.end>origin_seconds+1e-8 and n.end>n.start+1e-8]
    if not notes:
        raise ValueError('No MIDI notes remain after the selected score opening')
    last=max(clock.to_beat(n['end']) for n in notes)
    starts,lengths,text=reference_bars(clock,last-1e-7)
    if not starts or abs(starts[0]-origin)>1e-7:
        raise ValueError('The first score measure does not match the selected opening')
    if len(starts)>4096 or len(notes)*len(starts)>4_000_000:
        raise ValueError('Score exceeds the editable draft limit; use the committed MusicXML score')
    meters=[tuple(map(int,m.split('/'))) for m in text]
    _draft(None,None,dict(tempo=clock.data,notes=notes),starts,lengths,meters,output,allow_current_midi_preview=True)

    # Native importer understands bar-style none, but its internal measure
    # must still retain the absolute length and next meter's time anchor.
    tree=ET.parse(output)
    root=tree.getroot()
    root.remove(root.find('work'))   # this is a native seed, not a range preview
    expected=list(zip(starts,lengths))
    rows=_measure_layout(root)
    signatures=score_opening_meter_rows(clock.data)
    if len(rows)!=len(expected):
        raise ValueError('Generated score lost a measure')
    was_free=False
    for idx,(_,m,start,length,_) in enumerate(rows):
        absolute,extent=expected[idx]
        if abs(float(start)+origin-absolute)>1/9600+1e-8 or abs(float(length)-extent)>2/9600+1e-8:
            raise ValueError('Generated score moved an authored bar boundary')
        sig=next(row for row in reversed(signatures) if row['beat']<=absolute+1e-7)
        is_free=bool(sig.get('free'))
        if is_free or was_free:
            # The range writer's annotation pass compares encoded starts to
            # the unrounded clock. Reassert only this transition using the
            # verified source geometry, rather than a sub-tick guess.
            attrs=m.find('attributes')
            for old in attrs.findall('time'):attrs.remove(old)
            time=ET.Element('time')
            at=next((i for i,e in enumerate(attrs) if e.tag not in ('footnote','level','divisions','key')),len(attrs))
            attrs.insert(at,time)
            if is_free:ET.SubElement(time,'senza-misura')
            else:
                ET.SubElement(time,'beats').text=str(sig['numerator'])
                ET.SubElement(time,'beat-type').text=str(sig['denominator'])
        if is_free:
            # Do not add ordinary bar lines merely because a duration requires
            # an internal XML measure. The next timed meter is still present.
            _hide_unmetered_barline(m)
        was_free=is_free
    tree.write(output,encoding='utf-8',xml_declaration=True)
    local,origin_seconds=_local_reference(clock,origin)
    apply_reference_tempo(output,local)
    return dict(source_origin_seconds=origin_seconds,source_origin_beat=origin,
                source_anchor_count=len(clock.anchors),retained_notes=len(notes))




