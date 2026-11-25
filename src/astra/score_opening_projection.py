"""Project only the synthetic source clock prelude into score-local time.

The canonical MusicXML, generated MIDI and original tempo map stay in source
coordinates. Their exact origin is returned alongside this display/seed copy.
"""
from copy import deepcopy
from collections import Counter
from dataclasses import dataclass
from fractions import Fraction
from hashlib import sha256
from io import BytesIO
from math import isfinite
from pathlib import Path
import xml.etree.ElementTree as ET

from .notation_readability import sounding_signature
from .score_origin import PREFIX_ID, is_source_leading_silence
from .score_tempo import _measure_layout, _remove_generated_clock
from .timing import TempoMap


@dataclass(frozen=True)
class ScoreOpening:
    source_sha256: str
    beat: Fraction = Fraction(0)
    seconds: float = 0.

    def into_source_seconds(self, score_seconds):
        return self.seconds + score_seconds

    def into_score_seconds(self, source_seconds):
        return source_seconds - self.seconds


def _own_clock(node):
    return node.tag == 'direction' and node.get('id','').startswith('astra-reference-tempo-')


def _prefix_span(measure, divisions):
    """Recognize our own hidden rest, optionally decorated with our tempo clock."""
    clean = deepcopy(measure)
    _remove_generated_clock(clean)
    if not is_source_leading_silence(clean):
        return False
    if int(clean.findtext('attributes/divisions')) != divisions:
        return False
    for direction in measure.findall('direction'):
        if not _own_clock(direction):
            return False
        for child in direction:
            if child.tag not in ('direction-type', 'staff', 'offset', 'sound'):
                return False
            if child.tag == 'sound' and (set(child.attrib) != {'tempo'} or len(child)):
                return False
            if child.tag == 'direction-type':
                for word in child:
                    if word.tag != 'words' or ((word.text or '').strip()
                            and not word.get('id','').startswith('astra-integer-tempo-')):
                        return False
    return True


def _first_attributes(prefix, first):
    attributes = {}
    for old in (prefix, first):
        for block in old.findall('attributes'):
            for child in block:
                attributes[child.tag, child.get('number')] = deepcopy(child)
    for block in list(first.findall('attributes')):
        first.remove(block)
    merged = ET.Element('attributes')
    order = ('divisions','key','time','staves','part-symbol','instruments','clef',
             'staff-details','transpose','directive','measure-style')
    merged.extend(node for (tag, _), node in sorted(attributes.items(),
                  key=lambda x: (order.index(x[0][0]) if x[0][0] in order else len(order),
                                 str(x[0][1]))))
    first.insert(0, merged)


def _seed_clock(prefix, first, tempo, origin, explicit_marks):
    """Inherit exact boundary tempo; do not disturb tempo positions in retained bars."""
    if not prefix.findall('direction'):
        return
    if not any(_own_clock(d) and d.find('sound') is not None
               and int(d.findtext('offset', '0')) == 0 for d in first.findall('direction')):
        span = max(0, min(len(tempo.beats)-2,
                          next((i-1 for i, b in enumerate(tempo.beats) if b > float(origin)),
                               len(tempo.beats)-2)))
        b0,b1 = tempo.beats[span:span+2]
        t0,t1 = tempo.seconds[span:span+2]
        exact = 60*(b1-b0)/(t1-t0)
        if not isfinite(exact) or exact <= 0:
            raise ValueError('Invalid score opening tempo')
        init = ET.Element('direction', placement='above', id='astra-reference-tempo-projected-opening')
        ET.SubElement(ET.SubElement(init,'direction-type'),'words')
        ET.SubElement(init,'staff').text='1'
        ET.SubElement(init,'sound',tempo=format(exact,'.12g'))
        first.insert(1,init)
    # Move only the automatic heading; explicit marks in the retained score
    # stay exact. A manual pre-score mark is omitted with the synthetic prelude.
    if not explicit_marks:
        mark=next((w for w in prefix.findall('direction/direction-type/words')
                   if w.get('id','').startswith('astra-integer-tempo-')), None)
        if mark is not None and not any(w.get('id','').startswith('astra-integer-tempo-')
                for w in first.findall('direction/direction-type/words')):
            heading=ET.Element('direction',placement='above')
            ET.SubElement(heading,'direction-type').append(deepcopy(mark))
            ET.SubElement(heading,'staff').text='1'
            first.insert(1,heading)


def project_score_opening(raw, reference):
    """Return score-local bytes and origin for a revision-bound generated score.

    An ordinary empty measure, real upbeat or intentional intro is not guessed
    to be synthetic. Never apply this to an independently edited native file.
    """
    root=ET.fromstring(raw)
    identity=sha256(raw).hexdigest()
    parts=root.findall('part')
    measures=[p.findall('measure') for p in parts]
    if (reference.get('opening_mode') not in ('normal','pickup')
            or not any(rows and rows[0].get('id')==PREFIX_ID for rows in measures)):
        return raw, ScoreOpening(identity)
    layout=_measure_layout(root)
    spans=[]
    for part, rows in zip(parts,measures):
        if len(rows) < 2:
            raise ValueError('Cannot project a score without a musical measure')
        prefix, first = rows[:2]
        _,_,_,length,divisions=next(row for row in layout if row[0] is part)
        if not _prefix_span(prefix, divisions):
            raise ValueError('The opening is not an Astra-owned silent clock prelude')
        spans.append(length)
    if len(set(spans)) != 1:
        raise ValueError('Piano staves disagree on the synthetic opening origin')
    origin=spans[0]
    tempo=TempoMap(reference)
    sec=tempo.to_seconds(float(origin))
    if reference.get('opening_mode')=='normal':
        chosen=reference.get('score_start_seconds')
        if type(chosen) not in (int,float) or not isfinite(chosen) or abs(sec-chosen)>1e-6:
            raise ValueError('The opening prelude does not match the confirmed first bar')
    for index, (part, rows) in enumerate(zip(parts, measures)):
        prefix,first=rows[:2]
        _first_attributes(prefix,first)
        if index==0:
            _seed_clock(prefix,first,tempo,origin,'notation_tempo_marks' in reference)
        part.remove(prefix)
    result=ET.tostring(root,encoding='utf-8',xml_declaration=True)
    expected=Counter({(key,a-origin,b-origin,grace):v
                      for (key,a,b,grace),v in sounding_signature(BytesIO(raw)).items()})
    if sounding_signature(BytesIO(result))!=expected:
        raise ValueError('Score opening projection changed a written note')
    return result, ScoreOpening(identity, origin, sec)

