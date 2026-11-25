"""Print-only score projection. Never feed its trimmed XML to MIDI playback."""
from bisect import bisect_left,bisect_right
from copy import deepcopy
from fractions import Fraction
import hashlib
from pathlib import Path
import xml.etree.ElementTree as ET

from .notation_readability import sounding_signature
from .score_tempo import _measure_layout
from .timing import TempoMap


def principal_print_tempo(reference):
    """Time-weighted dominant tempo neighbourhood, not pulse/count averaging.

    The synthetic prefix or confirmed opening is excluded. A +/-2% (at least
    1 BPM) neighbourhood collects numerical jitter; its weighted median is
    printed as an integer. No tempo, pulse, or metrical interpretation changes.
    """
    clock=TempoMap(reference);cutoff=clock.to_seconds(float(reference.get('first_full_bar_beat',reference.get('pickup_beats',0))))
    pulse=reference.get('pulse_projection',{})
    prefix=pulse.get('prefix_interval_seconds',0) if pulse.get('prefix_policy')=='notational_prefix' else 0
    if isinstance(prefix,(int,float)): cutoff=max(cutoff,float(prefix))
    values=[]
    for a,b in zip(reference['anchors'],reference['anchors'][1:]):
        duration=b['seconds']-max(a['seconds'],cutoff)
        if duration>0:
            values.append((60*(b['beat']-a['beat'])/(b['seconds']-a['seconds']),duration))
    if not values:
        a,b=reference['anchors'][:2];values=[(60*(b['beat']-a['beat'])/(b['seconds']-a['seconds']),b['seconds']-a['seconds'])]
    values.sort();bpms=[p for p,w in values];weights=[0.]
    for _,w in values: weights.append(weights[-1]+w)
    candidates=[]
    for bpm,_ in values:
        radius=max(1.,bpm*.02);a=bisect_left(bpms,bpm-radius);b=bisect_right(bpms,bpm+radius)
        candidates.append((weights[b]-weights[a],-bpm,a,b))
    support,_,a,b=max(candidates);target=weights[a]+support/2
    median=values[min(b-1,max(a,bisect_left(weights,target)-1))][0]
    return {'bpm':max(1,int(median+.5)),'representative_bpm':median,
            'support_seconds':support,'total_seconds':weights[-1],'excluded_prefix_seconds':cutoff,
            'method':'time_weighted_dominant_2_percent_neighbourhood_median'}


def make_print_score(canonical_xml,output_xml,reference):
    """Omit only completely silent leading measures in a separate PDF input.

    Canonical XML, its exact sound directions, source meter and score MIDI
    retain their original time origin. A genuine pickup note/grace is retained.
    Original measure numbers remain available for editor correspondence.
    """
    source,output=Path(canonical_xml),Path(output_xml)
    if source.resolve()==output.resolve():raise ValueError('Print XML cannot replace canonical synchronized XML')
    raw=source.read_bytes();root=ET.fromstring(raw);layout=_measure_layout(root)
    # Canonical XML uses integer text plus exact sound tempo, avoiding reader
    # ambiguity. PDF-only projection uses standard integer metronome engraving.
    from math import isfinite
    for dtype in root.findall('.//direction-type'):
        for word in list(dtype.findall('words')):
            if word.get('id','').startswith('astra-integer-tempo-'):
                # Our canonical integer text has no playback authority.
                value=(word.text or '').split('=',1)[-1].strip()
                if value.isdigit():
                    metro=ET.SubElement(dtype,'metronome')
                    ET.SubElement(metro,'beat-unit').text='quarter'
                    ET.SubElement(metro,'per-minute').text=value
                    dtype.remove(word)
        for metro in dtype.findall('metronome'):
            value_node=metro.find('per-minute')
            try:value=float(value_node.text)
            except (AttributeError,TypeError,ValueError):continue
            if isfinite(value) and value>0:
                value_node.text=str(max(1,int(value+.5)))
                metro.attrib.pop('print-object',None)

    parts=root.findall('part');measures=[p.findall('measure') for p in parts]
    common=min(map(len,measures),default=0);trim=0
    for index in range(common):
        if any(m[index].find('.//pitch') is not None or m[index].find('.//unpitched') is not None
               or m[index].find('.//grace') is not None for m in measures):break
        trim+=1
    all_empty=trim==common
    if all_empty:trim=0  # Keep valid source and add an explicit empty-score note.
    explicit='notation_tempo_marks' in reference
    authored_opening=[]
    if explicit and trim:
        for old in measures[0][:trim]:
            for direction in old.findall('direction'):
                if direction.find('direction-type/metronome') is not None:
                    copied=ET.Element('direction',dict(direction.attrib))
                    for child in direction:
                        if child.tag in ('direction-type','staff','voice'):copied.append(deepcopy(child))
                    authored_opening.append(copied)
    for direction in root.findall('.//direction'):
        for dtype in list(direction.findall('direction-type')):
            if not len(dtype):direction.remove(dtype)
    origins=[];removed=[]
    for part,rows in zip(parts,measures):
        part_layout=[row for row in layout if row[0] is part]
        origin=part_layout[trim][2] if trim<len(part_layout) else Fraction(0);origins.append(origin)
        if not trim:continue
        first=rows[trim];attributes={}
        # Attributes inherited by the first retained bar must be explicit now.
        for old in rows[:trim+1]:
            for block in old.findall('attributes'):
                for child in block:attributes[(child.tag,child.get('number'))]=deepcopy(child)
        for block in list(first.findall('attributes')):first.remove(block)
        merged=ET.Element('attributes')
        order=('divisions','key','time','staves','part-symbol','instruments','clef','staff-details','transpose','directive','measure-style')
        merged.extend(v for k,v in sorted(attributes.items(),key=lambda item:(order.index(item[0][0]) if item[0][0] in order else len(order),str(item[0][1]))))
        first.insert(0,merged)
        # First visible bar starts a new print context, not an inherited page.
        for printing in list(first.findall('print')):first.remove(printing)
        for old in rows[:trim]:
            removed.append({'part':part.get('id'),'measure':old.get('number')});part.remove(old)
    if len(set(origins))>1:raise ValueError('Cannot trim parts with different leading empty durations')
    origin=origins[0] if origins else Fraction(0)
    tempo_audit=None
    if authored_opening:
        first=parts[0].find('measure')
        for direction in reversed(authored_opening):first.insert(1,direction)
    if not explicit and parts:
        tempo_audit=principal_print_tempo(reference)
        # Move only the automatic opening PRINT mark. Keep subsequent tempo
        # marks and every sound node in the canonical source untouched.
        first=parts[0].find('measure')
        opening=None;cursor=0
        for child in first:
            if child.tag=='direction' and cursor+int(child.findtext('offset','0'))==0:
                opening=child.find('direction-type/metronome')
                if opening is not None:break
            elif child.tag in ('backup','forward'):
                cursor+=int(child.findtext('duration','0'))*(-1 if child.tag=='backup' else 1)
            elif child.tag=='note' and child.find('chord') is None:
                cursor+=int(child.findtext('duration','0'))
        if opening is not None:
            # If no bar was removed, update the existing first printed mark.
            opening.find('per-minute').text=str(tempo_audit['bpm'])
        else:
            direction=ET.Element('direction',{'placement':'above'})
            metro=ET.SubElement(ET.SubElement(direction,'direction-type'),'metronome')
            ET.SubElement(metro,'beat-unit').text='quarter';ET.SubElement(metro,'per-minute').text=str(tempo_audit['bpm'])
            ET.SubElement(direction,'staff').text='1'
            first.insert(1 if first.find('attributes') is not None else 0,direction)
    expression_audit=None
    if not explicit and parts and not all_empty:
        from .print_tempo_expression import apply_print_expressions
        expression_audit=apply_print_expressions(root,reference,origin=origin,principal=tempo_audit)
    if all_empty and parts:
        direction=ET.Element('direction');ET.SubElement(ET.SubElement(direction,'direction-type'),'words').text='No transcribed notes / 无转录音符'
        parts[0].find('measure').append(direction)
    encoded=ET.tostring(root,encoding='utf-8',xml_declaration=True)
    # Only a global display origin is removed, never a pitched interval.
    from io import BytesIO
    from collections import Counter
    expected=Counter({(key,start-origin,end-origin,grace):count for (key,start,end,grace),count in sounding_signature(BytesIO(raw)).items()})
    if sounding_signature(BytesIO(encoded))!=expected:raise ValueError('Print trimming changed a note beyond the explicit display origin')
    output.parent.mkdir(parents=True,exist_ok=True);output.write_bytes(encoded)
    if source.read_bytes()!=raw:raise ValueError('Canonical XML changed during print projection')
    return {'print_xml':str(output.resolve()),'canonical_xml':str(source.resolve()),
            'canonical_sha256':hashlib.sha256(raw).hexdigest(),'print_sha256':hashlib.sha256(encoded).hexdigest(),
            'removed_leading_measures':removed,'display_origin_beat':float(origin),
            'display_origin_seconds':TempoMap(reference).to_seconds(float(origin)),
            'all_empty':all_empty,'principal_print_tempo':tempo_audit,
            'tempo_expressions':expression_audit,
            'playback_source':'canonical_xml_only','canonical_bytes_unchanged':True}
