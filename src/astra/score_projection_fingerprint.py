"""Deterministic engraving-input DAG, independent of selection UI and renderer.

This is a sufficient (not necessary) equality proof. It consumes the actual
projected events immediately before engraving, never guesses locality of the
model's sequence/roll alignment. Unequal/unknown inputs retain the slow proof.
"""
import hashlib
import json
import xml.etree.ElementTree as ET
from fractions import Fraction
from collections import Counter

def _hash(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()

def _xml(node):
    return None if node is None else ET.canonicalize(ET.tostring(node,encoding='unicode'))

def engraving_inputs(starts,lengths,meters,events,initial_key,keys,harmonies,clefs,reference,offset,*,directions=(),warnings=()):
    from .score_rhythm_annotations import apply_annotations
    # Annotation projection needs measure locations, not engraved note streams.
    skeleton=ET.Element('score-partwise');part=ET.SubElement(skeleton,'part',{'id':'P1'})
    for i,(start,length,meter) in enumerate(zip(starts,lengths,meters)):
        measure=ET.SubElement(part,'measure',{'number':str(i+1)})
        attrs=ET.SubElement(measure,'attributes');ET.SubElement(attrs,'divisions').text='9600'
        time=ET.SubElement(attrs,'time');ET.SubElement(time,'beats').text=str(meter[0]);ET.SubElement(time,'beat-type').text=str(meter[1])
        note=ET.SubElement(measure,'note');ET.SubElement(note,'rest')
        # Match the renderer's absolute-boundary rounding, not independent lengths.
        duration=round(Fraction(str(float(start+length)))*9600)-round(Fraction(str(float(start)))*9600)
        ET.SubElement(note,'duration').text=str(duration)
    apply_annotations(skeleton,reference,beat_offset=float(offset))
    annotations=[]
    for measure in part:
        annotations.append({'attrs':dict(measure.attrib),
            'time':[_xml(n) for n in measure.findall('attributes/time')],
            'directions':[_xml(n) for n in measure.findall('direction')]})
    from .notation_readability import _sounding_pitch
    offset_tick=Fraction(round(Fraction(str(float(offset)))*9600),9600)
    sounding=Counter((e['staff'],*_sounding_pitch(e['node'].find('pitch')),
        str(e['start']-offset_tick),str(e['end']-offset_tick),e['grace']) for e in events)
    preserved_marks=Counter((e['staff'],*_sounding_pitch(e['node'].find('pitch')),
        str(e['start']-offset_tick),str(e['end']-offset_tick),e['grace'],
        tuple(_xml(n) for n in e['node'].findall('notations/*') if n.tag not in ('tied','tuplet'))) for e in events)
    nodes={
      'preserved_note_marks':_hash(sorted((list(key),count) for key,count in preserved_marks.items())),
      'sounding_events':_hash(sorted((list(key),count) for key,count in sounding.items())),
      'bar_structure':_hash([starts,lengths,meters]),
      'projected_notes':_hash([{'start':str(e['start']),'end':str(e['end']),
          'staff':e['staff'],'grace':e['grace'],'node':_xml(e['node'])} for e in events]),
      'context_markers':_hash({'initial_keys':[_xml(k) for k in initial_key],'keys':[(q,_xml(n)) for q,n in keys],
          'harmonies':[(q,_xml(n)) for q,n in harmonies],'clefs':{k:_xml(v) for k,v in clefs.items()}}),
      'local_annotations':_hash(annotations),
      'source_directions':_hash([(q,_xml(n)) for q,n in directions]),
      'notation_warnings':_hash(list(warnings)),
      'engraver_contract':'range_replacement-v1;divisions=9600;two-staves;compact-preview-v1;preserved-roll-groups-v2;per-staff-keys-v1'}
    return {'signature':_hash(nodes),'nodes':nodes,'method':'projected_engraving_inputs_v1'}
