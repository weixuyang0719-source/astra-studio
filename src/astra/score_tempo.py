"""Write an explicit reference clock into generated MusicXML without inference."""
from bisect import bisect_right
from fractions import Fraction
from math import isfinite, lcm
from pathlib import Path
import xml.etree.ElementTree as ET

from .timing import TempoMap


def _remove_generated_clock(measure):
    """Remove current offset directions or legacy balanced cursor sequences."""
    owned=lambda node: node.tag=='direction' and node.get('id','').startswith('astra-reference-tempo-')
    children=list(measure)
    start=next((i for i,node in enumerate(children) if owned(node)),None)
    if start is None:return
    # When the first change is mid-measure, our sequence starts with a forward
    # immediately after attributes, before the first owned direction. Original
    # leading forwards are after our whole sequence and must not be consumed.
    if (start>0 and children[start-1].tag=='forward'
            and all(node.tag=='attributes' for node in children[:start-1])):
        start-=1
    cursor=0;remove=[]
    for index,node in enumerate(children[start:],start):
        if owned(node):remove.append(node)
        elif node.tag in ('forward','backup'):
            if node.tag=='forward' and (index+1==len(children) or not owned(children[index+1])):
                break  # An original leading rest/forward is not ours.
            if node.tag=='backup' and not cursor:
                break
            cursor+=int(node.findtext('duration'))*(1 if node.tag=='forward' else -1)
            remove.append(node)
            if cursor<0:raise ValueError('Generated tempo cursor sequence is unbalanced')
            if node.tag=='backup' and cursor==0:break
        else:break
    if cursor:raise ValueError('Generated tempo cursor sequence is unbalanced')
    for node in remove:measure.remove(node)


def remove_generated_reference_clock(xml_path: str | Path) -> bool:
    """Remove only Astra's generated tempo directions for structural score edits.

    Original notes, rests, directions, and native forward/backup events remain.
    Call apply_reference_tempo with the same reference after editing to restore
    playback and printed marks. No matching sequence means no file write.
    """
    path=Path(xml_path);tree=ET.parse(path);changed=False
    for measure in tree.getroot().findall('.//measure'):
        if any(d.get('id','').startswith('astra-reference-tempo-') for d in measure.findall('direction')):
            _remove_generated_clock(measure);changed=True
    if changed:tree.write(path,encoding='utf-8',xml_declaration=True)
    return changed


def _measure_layout(root):
    layout = []
    for part in root.findall('part'):
        position, divisions = Fraction(0), 1
        for measure in part.findall('measure'):
            value = measure.findtext('attributes/divisions')
            if value is not None:
                divisions = int(value)
            if divisions <= 0:
                raise ValueError('MusicXML divisions must be positive')
            cursor = last_onset = extent = Fraction(0)
            for child in measure:
                duration = Fraction(child.findtext('duration', '0')) / divisions
                if child.tag == 'backup':
                    cursor -= duration
                elif child.tag == 'forward':
                    cursor += duration
                    extent = max(extent, cursor)
                elif child.tag == 'note':
                    if child.find('chord') is None:
                        last_onset = cursor
                        cursor += duration
                    extent = max(extent, last_onset + duration, cursor)
            if extent <= 0:
                raise ValueError('Cannot place a reference tempo in an empty-duration measure')
            layout.append((part, measure, position, extent, divisions))
            position += extent
    return layout


def apply_reference_tempo(xml_path: str | Path, reference_data: dict) -> dict:
    """Replace generated tempo guesses with the supplied clock, retaining notes.

    This is for explicit MIDI/manual clocks. It does not estimate or smooth BPM.
    Existing dynamics, pedal and other directions survive; only generated
    metronomes, tempo playback values and rit./a tempo words are removed.
    """
    clock = TempoMap(reference_data)
    path = Path(xml_path)
    tree = ET.parse(path)
    root = tree.getroot()
    for measure in root.findall('.//measure'):
        _remove_generated_clock(measure)
    layout = _measure_layout(root)
    if not layout:
        raise ValueError('MusicXML has no measures')
    source_ppq = reference_data.get('source_ppq')
    midi_events = reference_data.get('midi_tempo_events')
    if source_ppq and midi_events:
        events = [(Fraction(event['tick'], source_ppq), Fraction(60000000, event['microseconds_per_quarter']))
                  for event in midi_events]
        # A user may edit anchors while retaining import provenance. In that
        # case the active reference clock wins over stale original MIDI metadata.
        metadata_matches = all(
            abs((clock.to_seconds(float(stop)) - clock.to_seconds(float(position))) /
                float(stop - position) - 60 / float(bpm)) < 1e-8
            for index, (position, bpm) in enumerate(events)
            for stop in [events[index + 1][0] if index + 1 < len(events) else Fraction(str(clock.beats[-1]))]
            if stop > position)
    else:
        metadata_matches = False
    if not metadata_matches:
        events = [(Fraction(str(a['beat'])), Fraction(60) *
                   (Fraction(str(b['beat'])) - Fraction(str(a['beat']))) /
                   (Fraction(str(b['seconds'])) - Fraction(str(a['seconds']))))
                  for a, b in zip(reference_data['anchors'], reference_data['anchors'][1:])]
    events = [(position, bpm) for index, (position, bpm) in enumerate(events)
              if index == 0 or bpm != events[index - 1][1]]
    primary_part = layout[0][0]
    primary = [item for item in layout if item[0] is primary_part]
    starts = [item[2] for item in primary]
    end = primary[-1][2] + primary[-1][3]
    events = [(position, bpm) for position, bpm in events if position < end]
    # Display is deliberately separate from the playback clock. Detailed
    # manual rubato or imported MIDI can have a sound event at every pulse.
    # Show only the opening tempo by default; later marks are explicitly
    # authored through the display list (an empty list hides all marks).
    explicit_marks = 'notation_tempo_marks' in reference_data
    if explicit_marks:
        raw_marks = reference_data['notation_tempo_marks']
        if not isinstance(raw_marks, list):
            raise ValueError('notation_tempo_marks must be a list')
    else:
        raw_marks=[{'beat':position,'bpm':bpm} for position,bpm in events[:1]]
    marks=[]
    for item in raw_marks:
        if not isinstance(item, dict) or 'beat' not in item:
            raise ValueError('Each notation tempo mark needs a beat')
        position=Fraction(str(item['beat']))
        if position<0 or position>=end:
            raise ValueError('Notation tempo mark lies outside the score')
        bpm=item.get('bpm');words=item.get('text')
        if bpm is not None and (not isfinite(float(bpm)) or float(bpm)<=0):
            raise ValueError('Displayed tempo must be finite and positive')
        if words is not None and (not isinstance(words,str) or not words.strip() or len(words)>200):
            raise ValueError('Tempo mark text must be nonempty and at most 200 characters')
        if bpm is None and words is None:
            raise ValueError('Notation tempo mark needs bpm or text')
        marks.append((position,bpm,words))
    # Decimal seconds/beat calculations are not rational notation grids. A
    # float such as 125.07407407407408 must not inflate XML divisions to 10^14.
    # Quantize only generated clock directions on a common, fine grid; notes
    # retain their exact units and imported MIDI tempo ticks remain exact.
    direction_rounding = 0.0
    if not metadata_matches:
        resolution = lcm(9600, *(item[4] for item in layout))
        def clock_position(position):
            nonlocal direction_rounding
            snapped = Fraction(round(position * resolution), resolution)
            direction_rounding = max(direction_rounding, float(abs(snapped-position)))
            return snapped
        events = [(clock_position(position), bpm) for position, bpm in events]
        marks = [(clock_position(position), bpm, words) for position, bpm, words in marks]
    # Preserve arbitrary MIDI tempo ticks, even when the score's original
    # divisions only represented simple note values (DuET often chooses 24).
    target_divisions = lcm(*(item[4] for item in layout), *(pos.denominator for pos, _ in events),
                           *(pos.denominator for pos, _, _ in marks))
    removed = 0
    for _, measure, _, _, divisions in layout:
        if divisions != target_divisions:
            scale = Fraction(target_divisions, divisions)
            for child in measure.iter():
                if child.tag in ('duration', 'offset') and child.text:
                    value = Fraction(child.text) * scale
                    if value.denominator != 1:
                        raise ValueError('Cannot preserve MusicXML timing units exactly')
                    child.text = str(value.numerator)
            attributes = measure.find('attributes')
            if attributes is None:
                attributes = ET.Element('attributes')
                measure.insert(0, attributes)
            element = attributes.find('divisions')
            if element is None:
                element = ET.Element('divisions')
                attributes.insert(0, element)
            element.text = str(target_divisions)
        for direction in list(measure.findall('direction')):
            changed = False
            for dtype in list(direction.findall('direction-type')):
                for item in list(dtype):
                    if item.tag == 'metronome' or item.tag == 'words' and (item.text or '').strip() in ('rit.', 'a tempo'):
                        dtype.remove(item)
                        changed = True
                if not len(dtype):
                    direction.remove(dtype)
            for sound in direction.findall('sound'):
                if 'tempo' in sound.attrib:
                    del sound.attrib['tempo']
                    changed = True
                if not sound.attrib and all(item.tag=='offset' for item in sound):
                    direction.remove(sound)
            if changed:
                removed += 1
                for dtype in list(direction.findall('direction-type')):
                    if all(c.tag == 'words' and not (c.text or '').strip() for c in dtype):
                        direction.remove(dtype)
                if direction.find('direction-type') is None and direction.find('sound') is None:
                    measure.remove(direction)
        for sound in measure.findall('sound'):
            sound.attrib.pop('tempo', None)
            if not sound.attrib and all(item.tag=='offset' for item in sound):
                measure.remove(sound)
    sound_by_beat=dict(events)
    marks_by_beat={}
    for position,bpm,words in marks:
        marks_by_beat.setdefault(position,[]).append((bpm,words))
    grouped={}
    for serial,position in enumerate(sorted(set(sound_by_beat)|set(marks_by_beat))):
        index=max(0,bisect_right(starts,position)-1)
        offset=(position-primary[index][2])*target_divisions
        if offset.denominator!=1:
            raise ValueError('Reference tempo offset cannot be represented exactly')
        direction=ET.Element('direction',{'placement':'above','id':f'astra-reference-tempo-{serial}'})
        for bpm,words in marks_by_beat.get(position,[]):
            if bpm is not None:
                # Words are visual only; sound below owns exact playback tempo.
                label=ET.SubElement(ET.SubElement(direction,'direction-type'),'words',{'id':f'astra-integer-tempo-{serial}-{len(direction)}'})
                label.text=f'♩ = {max(1,int(float(bpm)+.5))}'
            if words is not None:
                ET.SubElement(ET.SubElement(direction,'direction-type'),'words').text=words
        if not len(direction):
            # Music21's established sounding-only tempo representation: empty
            # words satisfy direction-type without creating a printed number.
            ET.SubElement(ET.SubElement(direction,'direction-type'),'words')
        ET.SubElement(direction,'staff').text='1'
        if position in sound_by_beat:
            ET.SubElement(direction,'sound',{'tempo':format(float(sound_by_beat[position]),'.12g')})
        grouped.setdefault(index,[]).append((offset.numerator,direction))
    for index,changes in grouped.items():
        measure=primary[index][1];sequence=[]
        for offset,direction in changes:
            if offset:
                position=ET.Element('offset',{'sound':'yes'});position.text=str(offset)
                # MusicXML direction order: direction-type, offset, staff, sound.
                at=next((i for i,node in enumerate(direction) if node.tag in ('staff','sound')),len(direction))
                direction.insert(at,position)
            sequence.append(direction)
        # Fine clock points are directions, not musical cursor movements.
        # A balanced forward/backup sequence can make MuseScore reject a valid
        # score after recounting (e.g. 500/9600-quarter forward spans). Standard
        # offsets leave every note and native cursor event intact. Some external
        # players still snap off-onset tempi; Astra's own MIDI is authoritative.
        insertion=next((i for i,node in enumerate(measure) if node.tag!='attributes'),len(measure))
        for node in sequence:
            measure.insert(insertion,node);insertion+=1
    from .score_rhythm_annotations import apply_annotations
    rhythm_annotations = apply_annotations(root, reference_data)
    tree.write(path, encoding='utf-8', xml_declaration=True)
    return {'rhythm_annotations': rhythm_annotations, 'source': reference_data.get('source'), 'events_written': len(events),
            'tempo_encoding':'direction_offset',
            'source_midi_tempo_metadata_used': metadata_matches,
            'generated_tempo_directions_removed': removed, 'divisions': target_divisions,
            'max_direction_rounding_quarters': direction_rounding,
            'explicit_notation_marks':explicit_marks,
            'notation_marks':[{'beat':float(position),'bpm':float(bpm) if bpm is not None else None,
                               'text':words} for position,bpm,words in marks],
            'events': [{'beat': float(position), 'bpm': float(bpm)} for position, bpm in events]}
