"""Strict two-staff engraving for DuET's piano MusicXML."""

from collections import Counter, defaultdict
from copy import deepcopy
from dataclasses import dataclass
from fractions import Fraction
from io import BytesIO
from pathlib import Path
import xml.etree.ElementTree as ET

from .notation_readability import dotted_allowed, sounding_signature, _sounding_pitch


_TYPES = {
    Fraction(4, 2 ** i): name for i, name in enumerate(
        ('whole', 'half', 'quarter', 'eighth', '16th', '32nd', '64th', '128th', '256th')
    )
}
_VOICES = {'1': '1', '2': '5'}


@dataclass
class _Event:
    start: int
    end: int
    node: ET.Element


@dataclass
class _Stream:
    backup: ET.Element | None
    children: list
    events: list
    markers: list
    cursor: int = 0


def _streams(measure, divisions):
    streams = [_Stream(None, [], [], [])]
    previous = 0
    for child in measure:
        if child.tag == 'backup':
            streams.append(_Stream(child, [], [], []))
            previous = 0
            continue
        stream = streams[-1]
        stream.children.append(child)
        if child.tag == 'attributes':
            value = child.findtext('divisions')
            if value is not None:
                # A clef change on the other staff may repeat the current
                # divisions after a backup. This does not change any time unit.
                # A genuine mid-measure unit change still needs explicit
                # conversion and remains rejected by this parser.
                if int(value) != divisions and (stream.cursor or len(streams) != 1):
                    return [], int(value)
                divisions = int(value)
        if child.tag == 'forward':
            stream.cursor += int(child.findtext('duration'))
            continue
        if child.tag != 'note':
            stream.markers.append((stream.cursor, child))
            continue
        duration = int(child.findtext('duration', '0'))
        chord = child.find('chord') is not None
        start = previous if chord else stream.cursor
        if not chord:
            previous = start
            stream.cursor += duration
        stream.events.append(_Event(start, start + duration, child))
    return streams, divisions


def _identity(stream):
    return {(e.node.findtext('staff', '1'), e.node.findtext('voice', '1'))
            for e in stream.events}


def _pitch_key(event):
    pitch = event.node.find('pitch')
    step, octave = pitch.findtext('step'), int(pitch.findtext('octave'))
    alter = Fraction(pitch.findtext('alter', '0'))
    semitone = (0, 2, 4, 5, 7, 9, 11)['CDEFGAB'.index(step)]
    return octave * 12 + semitone + alter


def _rhythm(ticks, divisions, start, length, compound, original):
    beats = Fraction(ticks, divisions)
    if beats in _TYPES:
        return _TYPES[beats], 0
    base = beats * Fraction(2, 3)
    if base in _TYPES and (original or dotted_allowed(
            Fraction(start, divisions), beats, Fraction(length, divisions), compound)):
        return _TYPES[base], 1
    return None


def _pieces(start, end, divisions, length, compound, original, time_modification):
    """Spell an exact interval, splitting into legal tied values if necessary."""
    factor = Fraction(1)
    if time_modification is not None:
        factor = Fraction(int(time_modification.findtext('normal-notes')),
                          int(time_modification.findtext('actual-notes')))
    unit = divisions * factor
    while start < end:
        remaining = end - start
        rhythm = _rhythm(remaining, unit, start, length, compound,
                         original or time_modification is not None)
        if rhythm is not None:
            yield start, end, rhythm
            return
        for value, name in _TYPES.items():
            ticks = value * unit
            if ticks.denominator == 1 and 0 < ticks < remaining:
                stop = start + int(ticks)
                yield start, stop, (name, 0)
                start = stop
                break
        else:
            raise ValueError('时值无法用安全的音符或连音组表达')


def _text(parent, tag, value):
    ET.SubElement(parent, tag).text = str(value)


def _note(event, start, end, rhythm, staff, voice, chord, time_modification, tuplets):
    node = ET.Element('note')
    if chord:
        ET.SubElement(node, 'chord')
    ties = []
    if event is None:
        ET.SubElement(node, 'rest')
    else:
        node.append(event.node.find('pitch'))
        if start != event.start or event.node.find("tie[@type='stop']") is not None:
            ties.append('stop')
        if end != event.end or event.node.find("tie[@type='start']") is not None:
            ties.append('start')
    _text(node, 'duration', end - start)
    for tie in ties:
        ET.SubElement(node, 'tie', {'type': tie})
    _text(node, 'voice', voice)
    name, dots = rhythm
    _text(node, 'type', name)
    for _ in range(dots):
        ET.SubElement(node, 'dot')
    if event is not None and start == event.start:
        accidental = event.node.find('accidental')
        if accidental is not None:
            node.append(accidental)
    if time_modification is not None:
        node.append(time_modification)
    _text(node, 'staff', staff)
    markings = []
    if event is not None:
        for marking in event.node.findall('notations/*'):
            if marking.tag in ('tied', 'tuplet'):
                continue
            release = marking.tag == 'fermata' or marking.get('type') == 'stop'
            keep = end == event.end if release else start == event.start
            if keep:
                markings.append(marking)
    if ties or markings or tuplets:
        notation = ET.SubElement(node, 'notations')
        for tie in ties:
            ET.SubElement(notation, 'tied', {'type': tie})
        notation.extend(tuplets)
        notation.extend(markings)
    return node


def _unsupported(note):
    supported = {'pitch', 'rest', 'chord', 'duration', 'voice', 'type', 'dot',
                 'accidental', 'stem', 'staff', 'beam', 'tie', 'notations',
                 'grace', 'time-modification'}
    return (any(child.tag not in supported for child in note)
            or any(child.tag not in ('tied', 'arpeggiate', 'fermata', 'glissando', 'slide', 'tuplet')
                   for child in note.findall('notations/*'))
            or any(key != 'print-object' for key in note.attrib))


def _set_voice(node, voice):
    element = node.find('voice')
    if element is not None:
        changed = element.text != voice
        element.text = voice
        return changed
    if node.tag == 'note':
        # The original DuET writer always includes a voice.
        raise ValueError('缺少谱表声部编号')
    return False


def _grace_and_tuplets(group, voice):
    grace = defaultdict(list)
    grace_owners = {}
    starts, stops = defaultdict(list), defaultdict(list)
    timed = []
    for index, stream in enumerate(group):
        for event in stream.events:
            note = event.node
            if note.find('grace') is not None:
                if event.start in grace_owners and grace_owners[event.start] != index:
                    raise ValueError('同一时刻存在两组独立倚音')
                grace_owners[event.start] = index
                _set_voice(note, voice)
                grace[event.start].append(note)
                continue
            tm = note.find('time-modification')
            if tm is not None:
                actual, normal = (int(tm.findtext(tag, '0')) for tag in ('actual-notes', 'normal-notes'))
                # MusicXML's optional normal-type is a standard tuplet unit
                # hint (also emitted by our roll writer), not another ratio.
                if (actual <= 0 or normal <= 0
                        or any(child.tag not in ('actual-notes','normal-notes','normal-type') for child in tm)
                        or tm.findtext('normal-type') not in (None,*_TYPES.values())):
                    raise ValueError('不支持的连音组比例')
                timed.append((event.start, event.end, tm))
            for marker in note.findall('notations/tuplet'):
                kind = marker.get('type')
                if kind not in ('start', 'stop') or tm is None:
                    raise ValueError('不支持的连音组标记')
                entries = starts[event.start] if kind == 'start' else stops[event.end]
                if all(ET.tostring(marker) != ET.tostring(existing) for existing in entries):
                    entries.append(marker)
    open_bracket = None
    for position in sorted(starts.keys() | stops.keys()):
        ending, beginning = stops.get(position, ()), starts.get(position, ())
        if ending:
            if len(ending) != 1 or ending[0].get('number', '1') != open_bracket:
                raise ValueError('连音组的结束位置无法安全合并')
            open_bracket = None
        if beginning:
            if len(beginning) != 1 or open_bracket is not None:
                raise ValueError('存在独立重叠的连音组括号')
            open_bracket = beginning[0].get('number', '1')
    if open_bracket is not None:
        raise ValueError('无法确认跨小节连音组的结束位置')
    return grace, timed, starts, stops


def _replacement(group, staff, divisions, compound):
    length = max(stream.cursor for stream in group)
    if length <= 0:
        raise ValueError('谱表没有有效时长')
    events = [event for stream in group for event in stream.events]
    if any(_unsupported(e.node) or e.end < e.start or
           (e.start == e.end and e.node.find('grace') is None) for e in events):
        raise ValueError('存在无法安全改写的记谱元素')
    pitched = [e for e in events if e.end > e.start and e.node.find('pitch') is not None]
    if any(event.end > length for event in pitched):
        raise ValueError('音符超出谱表时长')
    voice = _VOICES[staff]
    grace, timed, starts, stops = _grace_and_tuplets(group, voice)
    markers = defaultdict(list)
    for stream in group:
        for position, child in stream.markers:
            if child.tag not in ('attributes', 'direction', 'harmony', 'barline', 'print'):
                raise ValueError('存在未知的带位置标记')
            _set_voice(child, voice)
            markers[position].append(child)
    bounds = sorted({0, length, *markers, *grace, *starts, *stops,
                     *(e.start for e in pitched), *(e.end for e in pitched),
                     *(point for a, b, _ in timed for point in (a, b))})

    result, count = [], 0
    for start, end in zip(bounds, bounds[1:]):
        active = [e for e in pitched if e.start <= start < e.end]
        pitches = [_pitch_key(event) for event in active]
        if len(pitches) != len(set(pitches)):
            raise ValueError('同一谱表存在重叠的同音高，无法保留独立的起止')
        ratios = {(tm.findtext('actual-notes'), tm.findtext('normal-notes')): tm
                  for a, b, tm in timed if a <= start < b}
        if len(ratios) > 1:
            raise ValueError('重叠的连音组使用不同比例')
        time_modification = next(iter(ratios.values()), None)
        # A reconstructed gap/hold can end at a triplet attack even when
        # no original event owned that tuplet (e.g. a same-key reattack).
        # Preserve that exact interval with
        # the ordinary 3:2 spelling, not dyadic fragments plus an impossible
        # residual. This is not a general arbitrary-duration fallback.
        if time_modification is None:
            denominator = Fraction(end-start, divisions).denominator
            while denominator % 2 == 0:
                denominator //= 2
            if denominator == 3:
                time_modification = ET.Element('time-modification')
                _text(time_modification, 'actual-notes', 3)
                _text(time_modification, 'normal-notes', 2)
        exact = any(e.start == start and e.end == end for e in events)
        result.extend(markers.get(start, ()))
        result.extend(grace.get(start, ()))
        for left, right, rhythm in _pieces(start, end, divisions, length, compound, exact, time_modification):
            tuplets = starts.get(left, []) + stops.get(right, [])
            for index, event in enumerate(active or [None]):
                result.append(_note(event, left, right, rhythm, staff, voice, bool(index),
                                    time_modification, tuplets if index == 0 else ()))
            count += len(active)
    result.extend(markers.get(length, ()))
    if length in grace:
        raise ValueError('小节末端的倚音缺少附属音符')
    return result, count - len(pitched)


def _covered_rests(group):
    hidden = 0
    for stream in group:
        cover = sorted((event.start, event.end) for other in group if other is not stream
                       for event in other.events if event.node.find('pitch') is not None)
        for event in stream.events:
            if event.node.find('rest') is None or event.node.get('print-object') == 'no':
                continue
            covered_to = event.start
            for start, end in cover:
                if start <= covered_to < end:
                    covered_to = end
            hidden += covered_to >= event.end
    return hidden


def _restore_missing_beams(children, divisions, signature):
    """Restore grouping lost by the single-voice sweep, without respelling notes.

    Existing authored beams are authoritative. Use music21's meter grouping
    only for a generated staff with no beams; this also repairs older cached
    single-voice XML. It changes no durations, ties, pitches, or voice numbers.
    """
    notes = [node for node in children if node.tag == 'note']
    if any(node.find('beam') is not None for node in notes):
        return 0
    # Irregular/additive meters need an explicit grouping policy, which cannot
    # be inferred from the numerator alone. Keep their existing appearance.
    numerator, denominator = signature
    if not ((denominator in (2, 4) and numerator in (2, 3, 4))
            or (denominator == 8 and numerator in (3, 6, 9, 12, 15, 18))):
        return 0
    from music21 import duration, meter, note, stream

    measure = stream.Measure()
    targets = []
    position = Fraction(0)
    for node in children:
        if node.tag == 'forward':
            gap = Fraction(int(node.findtext('duration')), divisions)
            measure.insert(position, note.Rest(quarterLength=gap))
            targets.append(None)
            position += gap
        if node.tag != 'note' or node.find('chord') is not None or node.find('grace') is not None:
            continue
        length = Fraction(int(node.findtext('duration', '0')), divisions)
        kind = node.findtext('type')
        if kind is None or length <= 0:
            return 0  # Incomplete legacy XML: do not guess its written value.
        value = duration.Duration(type=kind, dots=len(node.findall('dot')))
        ratio = node.find('time-modification')
        if ratio is not None:
            value.appendTuplet(duration.Tuplet(int(ratio.findtext('actual-notes')),
                                              int(ratio.findtext('normal-notes'))))
        if Fraction(value.quarterLength) != length:
            return 0  # Preserve an upstream special spelling, even if unusual.
        item = note.Rest() if node.find('rest') is not None else note.Note()
        item.duration = value
        measure.insert(position, item)
        targets.append(node)
        position += length
    clock = meter.TimeSignature(f'{numerator}/{denominator}')
    if position > clock.barDuration.quarterLength:
        return 0
    measure.paddingRight = clock.barDuration.quarterLength - position
    beams = clock.getBeams(measure)
    count = 0
    for target, group in zip(targets, beams):
        if target is None or target.find('rest') is not None or group is None:
            continue
        for mark in group:
            kind = {'start': 'begin', 'stop': 'end', 'continue': 'continue',
                    'partial': 'forward hook' if mark.direction == 'right' else 'backward hook'}[mark.type]
            element = ET.Element('beam', {'number': str(mark.number)})
            element.text = kind
            # MusicXML puts beams after staff and before notations.
            notation = target.find('notations')
            target.insert(list(target).index(notation) if notation is not None else len(target), element)
            count += 1
    return count


def _rearticulate_same_pitch(records, part_id, signature):
    """End an old written hold at the next explicit same-key attack.

    A single hand/voice cannot independently hold and restrike one piano key.
    Preserve both attacks; only the earlier notated release is corrected. This
    is audited notation semantics, never a change to performance MIDI. Exact
    simultaneous duplicates remain ambiguous and are rejected by the merger.
    """
    chains=[];active={}
    for record in records:
        for stream in record['streams']:
            for event in stream.events:
                pitch=event.node.find('pitch')
                if pitch is None or event.node.find('grace') is not None:continue
                key=(part_id,event.node.findtext('staff','1'),*_sounding_pitch(pitch))
                start=record['origin']+Fraction(event.start,record['divisions'])
                end=record['origin']+Fraction(event.end,record['divisions'])
                voice=event.node.findtext('voice','1')
                ties={t.get('type') for t in event.node.findall('tie')}
                if 'stop' in ties:
                    matches=[c for c in active.get(key,[]) if c['end']==start]
                    same=[c for c in matches if c['voice']==voice]
                    candidates=same if same else matches
                    if len(candidates)!=1:raise ValueError('无法唯一定位同音重击之前的连线')
                    chain=candidates[0];active[key].remove(chain)
                    chain['end']=end;chain['voice']=voice
                else:
                    chain={'key':key,'start':start,'end':end,'voice':voice,'pieces':[]}
                    chains.append(chain)
                chain['pieces'].append((record,event,start,end))
                if 'start' in ties:active.setdefault(key,[]).append(chain)
    by_pitch=defaultdict(list)
    for chain in chains:by_pitch[chain['key']].append(chain)
    corrected=signature.copy();changes=[];dirty=set()
    for key,items in by_pitch.items():
        items.sort(key=lambda c:(c['start'],c['end']))
        for index,chain in enumerate(items):
            later=next((c for c in items[index+1:] if c['start']>chain['start']),None)
            if later is None or chain['end']<=later['start']:continue
            stop=later['start']
            original=(key,chain['start'],chain['end'],False)
            if corrected[original]<=0:raise ValueError('同音重击修订与原记谱事件不匹配')
            corrected[original]-=1
            if not corrected[original]:del corrected[original]
            corrected[(key,chain['start'],stop,False)]+=1
            first=chain['pieces'][0][0]
            changes.append({'measure':first['measure'].get('number'),'staff':int(key[1]),
                'pitch':list(key[2:]),'start_beat':float(chain['start']),
                'old_end_beat':float(chain['end']),'new_end_beat':float(stop),
                'next_attack_beat':float(later['start']),
                'next_attack_end_beat':float(later['end']),
                'reason':'end_prior_written_hold_at_explicit_same_pitch_reattack',
                'scope':'notation_only'})
            for record,event,start,end in chain['pieces']:
                if end<stop:continue
                dirty.add((id(record['measure']),key[1]))
                if start>=stop:
                    # Retain the original stream's cursor and identity. The
                    # single-voice sweep fills this old tail with the new note.
                    node=ET.Element('note');ET.SubElement(node,'rest')
                    _text(node,'duration',event.end-event.start)
                    _text(node,'voice',event.node.findtext('voice','1'))
                    _text(node,'staff',key[1]);event.node=node
                else:
                    event.node=deepcopy(event.node)
                    endpoint=(stop-record['origin'])*record['divisions']
                    if endpoint.denominator!=1:raise ValueError('同音重击位置不能精确写入当前小节')
                    event.end=int(endpoint)
                    for tie in list(event.node.findall("tie[@type='start']")):event.node.remove(tie)
                    for notation in event.node.findall('notations'):
                        for tie in list(notation.findall("tied[@type='start']")):notation.remove(tie)
    return corrected,changes,dirty


def simplify_staff_voices(path, mode='simple'):
    """Write exactly one voice per staff or raise without modifying the source."""
    if mode == 'smart':
        from .notation_smart_voice import simplify_smart_voices
        return simplify_smart_voices(path)
    if mode not in ('simple', 'detailed'):
        raise ValueError('Unknown piano voice layout')
    audit = {'mode': mode, 'strict_two_staff_voices': mode == 'simple',
             'collapsed_staff_bars': [], 'fallback_staff_bars': [],
             'covered_rests_hidden': 0, 'added_note_fragments': 0,
             'restored_beam_marks': 0, 'rebeamed_staff_bars': [],
             'sounding_events_preserved': True,'same_pitch_rearticulation_corrections':[],
             'attacks_and_pitches_preserved':True}
    if mode == 'detailed':
        return audit
    path = Path(path)
    tree = ET.parse(path)
    parts = tree.findall('part')
    if len(parts) != 1 or parts[0].find('measure') is None:
        raise ValueError('双手单声部排版需要一个钢琴乐器谱部')
    from .score_origin import is_source_leading_silence
    original_signature=sounding_signature(path)
    records=[];origin=Fraction(0);divisions=1;compound=False;signature=(4,4)
    for measure in parts[0].findall('measure'):
        meter=measure.find('attributes/time')
        if meter is not None:
            compound=(meter.findtext('beat-type')=='8' and meter.findtext('beats') in ('6','9','12','15','18'))
            try:
                signature=(int(meter.findtext('beats')),int(meter.findtext('beat-type')))
            except (ValueError,TypeError):
                signature=(0,0)  # Additive signature: preserve existing beams.
        streams,divisions=_streams(measure,divisions)
        if not streams or any(int(following.backup.findtext('duration'))!=previous.cursor
                              for previous,following in zip(streams,streams[1:])):
            raise ValueError(f'第 {measure.get("number","?")} 小节无法对齐原始声部')
        records.append({'measure':measure,'streams':streams,'divisions':divisions,
                        'compound':compound,'origin':origin,'signature':signature})
        origin+=Fraction(max([s.cursor for s in streams]+[e.end for s in streams for e in s.events]),divisions)
    expected,corrections,dirty=_rearticulate_same_pitch(records,parts[0].get('id'),original_signature)
    audit['same_pitch_rearticulation_corrections']=corrections
    audit['sounding_events_preserved']=expected==original_signature
    changed = False
    for part in parts:
        for record in records:
            measure,streams,divisions,compound=(record[k] for k in ('measure','streams','divisions','compound'))
            if is_source_leading_silence(measure):
                continue
            measure_number = measure.get('number', '?')
            groups = {}
            prelude = []
            previous_length = None
            for stream in streams:
                keys = _identity(stream)
                if not keys and not groups:
                    if stream.backup is not None:
                        prelude.append(stream.backup)
                    prelude.extend(stream.children)
                    previous_length = stream.cursor
                    continue
                if len(keys) != 1:
                    raise ValueError(f'第 {measure_number} 小节含交错声部，无法安全合并')
                (staff, _), = keys
                if staff not in _VOICES:
                    raise ValueError(f'第 {measure_number} 小节包含未知钢琴谱表')
                groups.setdefault(staff, []).append(stream)
            if groups.keys() != _VOICES.keys():
                raise ValueError(f'第 {measure_number} 小节需要完整的两个钢琴谱表')
            children = prelude
            for staff, group in groups.items():
                if previous_length is not None:
                    backup = ET.Element('backup')
                    _text(backup, 'duration', previous_length)
                    children.append(backup)
                previous_length = max(stream.cursor for stream in group)
                if len(group) == 1 and (id(measure),staff) not in dirty:
                    for node in group[0].children:
                        changed = _set_voice(node, _VOICES[staff]) or changed
                    beam_count = _restore_missing_beams(group[0].children, divisions, record['signature'])
                    if beam_count:
                        audit['restored_beam_marks'] += beam_count
                        audit['rebeamed_staff_bars'].append({'measure':measure_number,'staff':int(staff)})
                        changed = True
                    children.extend(group[0].children)
                    continue
                try:
                    replacement, fragments = _replacement(group, staff, divisions, compound)
                except (ArithmeticError, TypeError, ValueError) as exc:
                    hand = '右手' if staff == '1' else '左手'
                    raise ValueError(f'第 {measure_number} 小节{hand}无法安全合成单声部：{exc}') from exc
                beam_count = _restore_missing_beams(replacement, divisions, record['signature'])
                if beam_count:
                    audit['restored_beam_marks'] += beam_count
                    audit['rebeamed_staff_bars'].append({'measure':measure_number,'staff':int(staff)})
                children.extend(replacement)
                audit['covered_rests_hidden'] += _covered_rests(group)
                audit['added_note_fragments'] += fragments
                audit['collapsed_staff_bars'].append({'measure': measure_number, 'staff': int(staff)})
                changed = True
            measure[:] = children

    if not changed:
        return audit
    contents = ET.tostring(tree.getroot(), encoding='utf-8', xml_declaration=True)
    try:
        output_signature=sounding_signature(BytesIO(contents))
        preserved = expected == output_signature
        def attacks(signature):
            result=Counter()
            for (pitch,start,end,grace),count in signature.items():
                result[(pitch,start,grace)]+=count
            return result
        preserved=preserved and attacks(original_signature)==attacks(output_signature)
    except ValueError:
        preserved = False
    if not preserved:
        raise ValueError('双手单声部排版未通过音高、起止与重击校验')
    path.write_bytes(contents)
    return audit
