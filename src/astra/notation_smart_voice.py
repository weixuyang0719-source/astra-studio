"""Keep DuET's independent sustained and moving layers on two piano staves.

This is engraving only. Exact sounding intervals are checked before publication.
No voice allocation from millisecond thresholds or pedal release is performed.
"""
from collections import Counter
from copy import deepcopy
from fractions import Fraction
from io import BytesIO
from pathlib import Path
import xml.etree.ElementTree as ET

from .notation_readability import sounding_signature
from .score_origin import is_source_leading_silence
from .notation_single_voice import (
    _streams, _identity, _replacement, _restore_missing_beams, _set_voice,
    _text, _pitch_key, _rearticulate_same_pitch, _Event,
)


def coalesce_ties(stream):
    """Remove internal tied segmentation in one lane, never real re-attacks.

    Tuplets/grace and continuation-specific articulations are left untouched.
    Outer incoming/outgoing ties survive, including cross-measure chains.
    """
    active = {}; result = []; merged = 0
    for event in sorted(stream.events, key=lambda e: (e.start, e.end)):
        node = event.node
        if node.find('pitch') is None or node.find('grace') is not None:
            result.append(event); continue
        key = _pitch_key(event)
        previous = active.get(key)
        safe = (node.find('time-modification') is None
                and all(n.tag == 'tied' for n in node.findall('notations/*')))
        if (safe and previous is not None and previous.end == event.start
                and previous.node.find('time-modification') is None
                and previous.node.find("tie[@type='start']") is not None
                and node.find("tie[@type='stop']") is not None):
            previous.end = event.end
            for tie in list(previous.node.findall("tie[@type='start']")):
                previous.node.remove(tie)
            for notation in previous.node.findall('notations'):
                for tie in list(notation.findall("tied[@type='start']")):
                    notation.remove(tie)
            if node.find("tie[@type='start']") is not None:
                ET.SubElement(previous.node, 'tie', {'type': 'start'})
                notation = previous.node.find('notations')
                if notation is None: notation = ET.SubElement(previous.node, 'notations')
                ET.SubElement(notation, 'tied', {'type': 'start'})
            merged += 1
        else:
            event = _Event(event.start, event.end, deepcopy(node))
            result.append(event); active[key] = event
    stream.events = sorted(result, key=lambda e: e.start)
    return merged


def _independent(group):
    for index, stream in enumerate(group):
        for other in group[index + 1:]:
            for a in stream.events:
                if a.node.find('pitch') is None or a.end <= a.start: continue
                for b in other.events:
                    if b.node.find('pitch') is None or b.end <= b.start: continue
                    if max(a.start, b.start) < min(a.end, b.end) and (a.start, a.end) != (b.start, b.end):
                        return True
    return False


def set_lane(nodes, voice, stem=None):
    for node in nodes:
        _set_voice(node, voice)
        if node.tag != 'note' or node.find('pitch') is None or stem is None: continue
        existing = node.find('stem')
        if existing is None:
            existing = ET.Element('stem')
            staff = node.find('staff')
            node.insert(list(node).index(staff) if staff is not None else len(node), existing)
        existing.text = stem


def _hide_covered_rests(lanes, divisions):
    streams = []
    for nodes in lanes:
        measure = ET.Element('measure'); measure.extend(nodes)
        parsed, _ = _streams(measure, divisions); streams.extend(parsed)
    hidden = 0
    for lane_index, stream in enumerate(streams):
        cover = sorted((e.start, e.end) for other in streams if other is not stream
                       for e in other.events if e.node.find('pitch') is not None)
        for e in stream.events:
            if e.node.find('rest') is None: continue
            if e.node.get('print-object')=='yes' or e.node.get('id') or e.node.find('notations') is not None:
                continue
            # The generated secondary lane is a supplementary sustained layer.
            # Its empty lead-in/tail need no second competing silence symbol;
            # the primary lane already represents the staff's rhythmic frame.
            # An explicitly visible/authored rest remains authoritative.
            if lane_index > 0:
                e.node.set('print-object', 'no'); hidden += 1; continue
            end = e.start
            for a, b in cover:
                if a <= end < b: end = b
            if end >= e.end:
                e.node.set('print-object', 'no'); hidden += 1
    return hidden


def _preserve_explicit_rests(nodes, lanes, divisions):
    """Carry explicit rest visibility through a necessary interval rewrite."""
    measure=ET.Element('measure');measure.extend(nodes)
    streams,_=_streams(measure,divisions)
    output=[e for s in streams for e in s.events if e.node.find('rest') is not None]
    for source in (e for s in lanes for e in s.events if e.node.find('rest') is not None):
        visibility=source.node.get('print-object');mark=source.node.find('notations')
        # Tuplet endpoints belong to the reconstructed rhythmic group, not
        # necessarily to this rest. _replacement already transfers them to
        # the surviving note/rest at the same exact boundary. Do not require
        # an obsolete covered rest or duplicate the transferred brackets.
        if mark is not None:
            mark=deepcopy(mark)
            for child in list(mark):
                if child.tag=='tuplet':mark.remove(child)
            if not len(mark):mark=None
        if visibility is None and mark is None:continue
        matches=[e for e in output if source.start<=e.start<e.end<=source.end]
        if mark is not None and not any(e.start==source.start and e.end==source.end for e in matches):
            raise ValueError('带显式记号的休止符不能安全拆分，请保留原声部')
        for e in matches:
            if visibility is not None:e.node.set('print-object',visibility)
            if mark is not None and e.start==source.start and e.end==source.end:
                e.node.append(deepcopy(mark))


def _simplify_smart_measure(record, dirty=()):
    measure = record['measure']; divisions = record['divisions']; signature = record['signature']
    if is_source_leading_silence(measure):
        return None
    groups = {}; prelude = []
    for stream in record['streams']:
        keys = _identity(stream)
        if not keys and not groups: prelude.extend(stream.children); continue
        if len(keys) != 1: raise ValueError('交错声部不能安全整理')
        (staff, voice), = keys
        groups.setdefault(staff, []).append(stream)
    if set(groups) != {'1', '2'} or any(len(g) > 2 for g in groups.values()):
        raise ValueError('智能排版仅支持左右手两谱表、每谱表最多两个已有声部')
    children = prelude; previous_length = None
    local_poly = []; collapsed = []; hidden = 0; merged_total = 0; beams = 0
    for staff, group in groups.items():
        merged = {id(s): coalesce_ties(s) for s in group}
        merged_total += sum(merged.values())
        poly = len(group) > 1 and _independent(group)
        lanes = [[s] for s in group] if poly else [group]
        if poly: local_poly.append(dict(measure=measure.get('number'), staff=int(staff)))
        elif len(group) > 1: collapsed.append(dict(measure=measure.get('number'), staff=int(staff)))
        rendered = []
        for index, lane in enumerate(lanes):
            rebuild = len(lane) > 1 or any(merged[id(s)] for s in lane) or (id(measure), staff) in dirty
            if rebuild:
                nodes, _ = _replacement(lane, staff, divisions, signature[1] == 8 and signature[0] in (6,9,12,15,18))
                _preserve_explicit_rests(nodes,lane,divisions)
            else: nodes = lane[0].children
            set_lane(nodes, str((1 if staff == '1' else 5) + index), ('up' if index == 0 else 'down') if poly else None)
            beams += _restore_missing_beams(nodes, divisions, signature)
            rendered.append(nodes)
        if poly: hidden += _hide_covered_rests(rendered, divisions)
        for nodes, lane in zip(rendered, lanes):
            if previous_length is not None:
                backup = ET.Element('backup'); _text(backup, 'duration', previous_length); children.append(backup)
            children.extend(nodes); previous_length = max(s.cursor for s in lane)
    measure[:] = children
    return (local_poly, collapsed, hidden, merged_total, beams)


def simplify_smart_voices(path):
    path = Path(path); tree = ET.parse(path); parts = tree.findall('part')
    if len(parts) != 1: raise ValueError('智能双手排版需要一个钢琴谱部')
    original = sounding_signature(path)
    records = []; origin = Fraction(0); divisions = 1; signature = (4, 4)
    for measure in parts[0].findall('measure'):
        time = measure.find('attributes/time')
        if time is not None:
            # Free meter has its written cursor length, but no grouping grid.
            signature = ((0, 0) if time.find('senza-misura') is not None else
                         (int(time.findtext('beats')), int(time.findtext('beat-type'))))
        streams, divisions = _streams(measure, divisions)
        if not streams or any(int(b.backup.findtext('duration')) != a.cursor for a, b in zip(streams, streams[1:])):
            raise ValueError('原始声部的小节起点不能安全对齐')
        records.append(dict(measure=measure, streams=streams, divisions=divisions,
                            origin=origin, signature=signature))
        origin += Fraction(max(s.cursor for s in streams), divisions)
    expected, corrections, dirty = _rearticulate_same_pitch(records, parts[0].get('id'), original)
    audit = dict(mode='smart', strict_two_staff_voices=False, maximum_voices_per_staff=2,
                 local_polyphonic_staff_bars=[], collapsed_staff_bars=[], covered_rests_hidden=0,
                 internal_tie_fragments_merged=0, restored_beam_marks=0,
                 same_pitch_rearticulation_corrections=corrections,
                 sounding_events_preserved=expected == original, attacks_and_pitches_preserved=True)
    for record in records:
        res = _simplify_smart_measure(record, dirty)
        if res is None:
            continue
        local_poly, collapsed, hidden, merged_total, beams = res
        audit['local_polyphonic_staff_bars'].extend(local_poly)
        audit['collapsed_staff_bars'].extend(collapsed)
        audit['covered_rests_hidden'] += hidden
        audit['internal_tie_fragments_merged'] += merged_total
        audit['restored_beam_marks'] += beams
    contents = ET.tostring(tree.getroot(), encoding='utf-8', xml_declaration=True)
    result = sounding_signature(BytesIO(contents))
    def attacks(events):
        counter = Counter()
        for (pitch, start, end, grace), count in events.items(): counter[pitch, start, grace] += count
        return counter
    if expected != result or attacks(original) != attacks(result):
        raise ValueError('智能双手排版未通过音高、起止与重击校验')
    path.write_bytes(contents)
    return audit
