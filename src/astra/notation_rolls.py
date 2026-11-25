"""Explicit rolled-chord interval edits after upstream voice/duration cleanup.

Only accepted notes may change; all other sounding intervals are a write gate.
This layer reuses the existing single-voice sweep rather than re-running the
upstream voice allocator, which can otherwise shorten neighbouring held notes.
"""
from collections import Counter
from copy import deepcopy
from fractions import Fraction
from io import BytesIO
from pathlib import Path
import xml.etree.ElementTree as ET

from .notation_readability import sounding_signature
from .notation_single_voice import _streams, _replacement, _restore_missing_beams, _Event
from .score_midi import _pitch
from .score_origin import is_source_leading_silence
from .score_tempo import _measure_layout


_FINE_TYPES = frozenset(('32nd', '64th', '128th', '256th', '512th', '1024th'))


def _element_reading_cost(elem):
    notes = elem.findall('.//note')
    fine = 0
    ties = 0
    for n in notes:
        if n.findtext('type') in _FINE_TYPES:
            fine += 1
        for t in n.findall('tie'):
            if t.get('type') == 'start':
                ties += 1
    return (fine, ties, len(notes))


def _reading_cost(path):
    """Compare actual written fragments, not just unchanged sounding events."""
    return _element_reading_cost(ET.parse(path).getroot())


def apply_roll_groups(path, groups, *, layout_mode=None):
    # An inferred rolled chord is a readability option. A candidate that adds
    # fine fragments/tie chains is not worth applying just because its MIDI
    # intervals remain valid. Evaluate each candidate independently so one bad
    # long roll cannot erase a useful short roll elsewhere.
    from collections import defaultdict
    from .notation_single_voice import _identity
    from .notation_smart_voice import coalesce_ties, set_lane, _simplify_smart_measure

    path = Path(path); accepted = []; rejected = []; costs = []; layout_passes = []
    if layout_mode not in (None, 'smart'):
        raise ValueError('Unknown roll layout mode')
    if layout_mode == 'smart':
        from .notation_single_voice import simplify_staff_voices
        layout_passes.append(simplify_staff_voices(path, 'smart'))
    if not groups:
        return {'method': 'post_voice_roll_intervals_v2', 'accepted': accepted, 'rejected': rejected,
                'reading_cost_changes': costs, 'outside_group_intervals_preserved': True, 'raw_events_modified': False,
                'layout_passes': layout_passes}

    tree = ET.parse(path)
    root = tree.getroot()
    expected = sounding_signature(path)
    records = []
    meter = (4, 4)
    for part, measure, start, length, division in _measure_layout(root):
        time_el = measure.find('attributes/time')
        if time_el is not None:
            meter = ((0, 0) if time_el.find('senza-misura') is not None else
                     (int(time_el.findtext('beats')), int(time_el.findtext('beat-type'))))
        streams, _ = _streams(measure, division)
        source_prefix = is_source_leading_silence(measure)
        identities = [_identity(s) for s in streams]
        if not source_prefix and (any(len(keys) != 1 for keys in identities)
                or any(sum(next(iter(keys))[0] == staff for keys in identities) > 2 for staff in ('1', '2'))):
            raise ValueError('滚奏合并需要已整理的两谱表，每谱表最多两个声部')
        records.append(dict(part=part.get('id'), measure=measure, start=start, length=length,
                            end=start + length, division=division, streams=streams,
                            source_prefix=source_prefix, meter=meter))

    by_staff_pitch = defaultdict(list)
    by_exact_pitch = defaultdict(list)
    for k in expected:
        if not k[3]:
            by_staff_pitch[(k[0][1], _pitch(k[0]))].append(k)
        by_exact_pitch[k[0]].append(k)

    m_costs = [_element_reading_cost(r['measure']) for r in records]
    total_cost = (
        sum(c[0] for c in m_costs),
        sum(c[1] for c in m_costs),
        sum(c[2] for c in m_costs),
    )
    score_end = records[-1]['end']

    def _rebuild_record_streams(idx):
        rec = records[idx]
        streams, _ = _streams(rec['measure'], rec['division'])
        identities = [_identity(s) for s in streams]
        if not rec['source_prefix'] and (any(len(keys) != 1 for keys in identities)
                or any(sum(next(iter(keys))[0] == staff for keys in identities) > 2 for staff in ('1', '2'))):
            raise ValueError('滚奏合并需要已整理的两谱表，每谱表最多两个声部')
        rec['streams'] = streams

    for group in groups:
        staff = str(int(group['staff']) + 1)
        target = Fraction(str(group['onset_quarters'])).limit_denominator(9600)
        duration = Fraction(str(group['duration_quarters'])).limit_denominator(9600)
        finish = target + duration
        selected = []
        for pitch, onset in zip(group['pitches'], group['before_quarters']):
            onset_f = float(onset)
            matches = [k for k in by_staff_pitch.get((staff, pitch), ())
                       if expected.get(k, 0) == 1 and abs(float(k[1]) - onset_f) < 1e-8]
            if len(matches) != 1:
                break
            selected.append(matches[0])
        reason = None
        if len(selected) != len(group['pitches']):
            reason = 'ambiguous_or_missing_written_attack'
        elif finish > score_end:
            reason = 'past_score_boundary'
        else:
            sel_set = set(selected)
            for k in selected:
                for other in by_exact_pitch.get(k[0], ()):
                    if expected.get(other, 0) > 0 and other not in sel_set and target < other[2] and finish > other[1]:
                        reason = 'same_pitch_collision'
                        break
                if reason is not None:
                    break
        pieces = []
        piece_rec_idxs = set()
        if reason is None:
            for key, start, end, grace in selected:
                found = []
                key_midi = _pitch(key)
                for idx, record in enumerate(records):
                    if record['end'] <= start or record['start'] >= end:
                        continue
                    for stream in record['streams']:
                        for event in stream.events:
                            node = event.node
                            if node.find('pitch') is None or node.find('grace') is not None:
                                continue
                            begin = record['start'] + Fraction(event.start, record['division'])
                            stop = record['start'] + Fraction(event.end, record['division'])
                            if (node.findtext('staff', '1') == staff
                                    and _pitch((None, None, node.findtext('pitch/step'), node.findtext('pitch/alter', '0'), node.findtext('pitch/octave'))) == key_midi
                                    and start <= begin < stop <= end):
                                found.append((idx, record, stream, event))
                                piece_rec_idxs.add(idx)
                if not found or any(tm is not None and (tm.findtext('actual-notes'), tm.findtext('normal-notes')) != ('3', '2')
                                    for _, _, _, e in found for tm in [e.node.find('time-modification')]):
                    reason = 'unsupported_tuplet_or_missing_tie_chain'
                    break
                pieces.append((key, found))
        if reason is not None:
            rejected.append({'input_ids': group['input_ids'], 'reason': reason})
            continue

        earliest = min((item for _, found in pieces for item in found),
                       key=lambda item: item[1]['start'] + Fraction(item[3].start, item[1]['division']))
        voice = earliest[3].node.findtext('voice', '1')

        spans = []
        span_rec_idxs = set()
        for idx, record in enumerate(records):
            begin = max(target, record['start']); stop = min(finish, record['end'])
            if stop <= begin:
                continue
            a = (begin - record['start']) * record['division']
            b = (stop - record['start']) * record['division']
            if a.denominator != 1 or b.denominator != 1:
                reason = 'unrepresentable_boundary'
                break
            spans.append((idx, record, int(a), int(b), begin > target, stop < finish))
            span_rec_idxs.add(idx)
        if reason is not None:
            rejected.append({'input_ids': group['input_ids'], 'reason': reason})
            continue

        destinations = {id(record['measure']): next((s for s in record['streams']
            if not record['source_prefix'] and (staff, voice) in _identity(s)), None) for _, record, _, _, _, _ in spans}
        if any(s is None for s in destinations.values()):
            rejected.append({'input_ids': group['input_ids'], 'reason': 'missing_destination_voice'})
            continue

        touched = sorted(piece_rec_idxs | span_rec_idxs)
        saved_measures = {idx: deepcopy(records[idx]['measure']) for idx in touched}
        saved_m_costs = {idx: m_costs[idx] for idx in touched}
        before = total_cost

        dirty = set()
        for (key, found), old in zip(pieces, selected):
            template = deepcopy(found[0][3].node)
            for idx, record, stream, event in found:
                stream.events.remove(event)
                dirty.add((id(record['measure']), id(stream)))
            for idx, record, a, b, continuing, continues in spans:
                node = deepcopy(template)
                for tag in ('tie', 'chord', 'beam'):
                    for item in list(node.findall(tag)):
                        node.remove(item)
                for notation in node.findall('notations'):
                    for item in list(notation):
                        if item.tag in ('tied', 'arpeggiate'):
                            notation.remove(item)
                notation = node.find('notations')
                if notation is None:
                    notation = ET.SubElement(node, 'notations')
                for kind, enabled in (('stop', continuing), ('start', continues)):
                    if enabled:
                        ET.SubElement(node, 'tie', {'type': kind})
                        ET.SubElement(notation, 'tied', {'type': kind})
                if not continuing:
                    attrs = {'direction': 'down'} if group.get('direction') == 'down' else {}
                    ET.SubElement(notation, 'arpeggiate', attrs)
                stream = destinations[id(record['measure'])]
                stream.events.append(_Event(a, b, node))
                dirty.add((id(record['measure']), id(stream)))

        for idx in touched:
            record = records[idx]
            measure = record['measure']
            if record['source_prefix']:
                continue
            m_meter = record['meter']
            children = []
            for stream in record['streams']:
                if stream.backup is not None:
                    children.append(stream.backup)
                s_staff = next(n.findtext('staff', '1') for n in stream.children if n.tag == 'note')
                if (id(measure), id(stream)) not in dirty:
                    children.extend(stream.children)
                    continue
                s_voice = next(n.findtext('voice', '1') for n in stream.children if n.tag == 'note')
                stem = next((n.findtext('stem') for n in stream.children if n.tag == 'note' and n.find('stem') is not None), None)
                coalesce_ties(stream)
                compound = m_meter[1] == 8 and m_meter[0] in (6, 9, 12, 15, 18)
                if any(e.node.find('time-modification') is not None for e in stream.events):
                    from .notation_range_rhythm import range_replacement
                    replacement = range_replacement([stream], s_staff, record['division'], compound)
                else:
                    replacement, _ = _replacement([stream], s_staff, record['division'], compound)
                    _restore_missing_beams(replacement, record['division'], m_meter)
                set_lane(replacement, s_voice, stem)
                children.extend(replacement)
            measure[:] = children
            if layout_mode == 'smart':
                new_streams, _ = _streams(measure, record['division'])
                _simplify_smart_measure(dict(measure=measure, streams=new_streams,
                                             divisions=record['division'], signature=m_meter))

        new_m_costs = {idx: _element_reading_cost(records[idx]['measure']) for idx in touched}
        after = (
            total_cost[0] - sum(saved_m_costs[i][0] for i in touched) + sum(new_m_costs[i][0] for i in touched),
            total_cost[1] - sum(saved_m_costs[i][1] for i in touched) + sum(new_m_costs[i][1] for i in touched),
            total_cost[2] - sum(saved_m_costs[i][2] for i in touched) + sum(new_m_costs[i][2] for i in touched),
        )
        if after > before:
            for idx in touched:
                records[idx]['measure'][:] = list(saved_measures[idx])
                _rebuild_record_streams(idx)
            rejected.append({'input_ids': group['input_ids'], 'reason': 'written_fragment_cost_increased',
                             'before': before, 'after': after})
            continue

        # Commit group
        for (key, _), old in zip(pieces, selected):
            expected[old] -= 1
            if not expected[old]:
                del expected[old]
            new_k = (key, target, finish, False)
            expected[new_k] += 1
            if new_k not in by_staff_pitch[(staff, _pitch(key))]:
                by_staff_pitch[(staff, _pitch(key))].append(new_k)
            if new_k not in by_exact_pitch[key]:
                by_exact_pitch[key].append(new_k)
        for idx in touched:
            m_costs[idx] = new_m_costs[idx]
            _rebuild_record_streams(idx)
        total_cost = after
        costs.append({'input_ids': group['input_ids'], 'before': before, 'after': after})
        accepted.append(group)

    if accepted:
        contents = ET.tostring(root, encoding='utf-8', xml_declaration=True)
        if sounding_signature(BytesIO(contents)) != expected:
            raise ValueError('滚奏记谱改变了允许组以外的音高或时值，保留原谱')
        path.write_bytes(contents)
        if layout_mode == 'smart':
            from .notation_single_voice import simplify_staff_voices
            layout_passes.append(simplify_staff_voices(path, 'smart'))

    return {'method': 'post_voice_roll_intervals_v2', 'accepted': accepted, 'rejected': rejected,
            'reading_cost_changes': costs, 'outside_group_intervals_preserved': True, 'raw_events_modified': False,
            'layout_passes': layout_passes}


def _apply_roll_groups(path, groups):
    if not groups:
        return {'method':'post_voice_roll_intervals_v1','accepted':[],'rejected':[],
                'outside_group_intervals_preserved':True,'raw_events_modified':False}
    path=Path(path);tree=ET.parse(path);original=sounding_signature(path)
    expected=original.copy();records=[]
    for part,measure,start,length,division in _measure_layout(tree.getroot()):
        streams,_=_streams(measure,division)
        from .notation_single_voice import _identity
        source_prefix=is_source_leading_silence(measure)
        identities=[_identity(s) for s in streams]
        if not source_prefix and (any(len(keys)!=1 for keys in identities)
                or any(sum(next(iter(keys))[0]==staff for keys in identities)>2 for staff in ('1','2'))):
            raise ValueError('滚奏合并需要已整理的两谱表，每谱表最多两个声部')
        records.append(dict(part=part.get('id'),measure=measure,start=start,length=length,
                            division=division,streams=streams,source_prefix=source_prefix))
    accepted=[];rejected=[];dirty=set()
    for group in groups:
        staff=str(int(group['staff'])+1);target=Fraction(str(group['onset_quarters'])).limit_denominator(9600)
        duration=Fraction(str(group['duration_quarters'])).limit_denominator(9600);finish=target+duration
        selected=[]
        for pitch,onset in zip(group['pitches'],group['before_quarters']):
            matches=[k for k,count in expected.items() if count==1 and not k[3]
                     and k[0][1]==staff and _pitch(k[0])==pitch
                     and abs(float(k[1])-float(onset))<1e-8]
            if len(matches)!=1:break
            selected.append(matches[0])
        reason=None
        if len(selected)!=len(group['pitches']):reason='ambiguous_or_missing_written_attack'
        elif finish>records[-1]['start']+records[-1]['length']:reason='past_score_boundary'
        elif any(k[0]==other[0] and k!=other and other not in selected
                 and target<other[2] and finish>other[1] for k in selected for other in expected):
            reason='same_pitch_collision'
        pieces=[]
        if reason is None:
            for key,start,end,grace in selected:
                found=[]
                for record in records:
                    for stream in record['streams']:
                        for event in stream.events:
                            node=event.node
                            if node.find('pitch') is None or node.find('grace') is not None:continue
                            begin=record['start']+Fraction(event.start,record['division'])
                            stop=record['start']+Fraction(event.end,record['division'])
                            if (node.findtext('staff','1')==staff and _pitch((None,None,node.findtext('pitch/step'),node.findtext('pitch/alter','0'),node.findtext('pitch/octave')))==_pitch(key)
                                    and start<=begin<stop<=end):
                                found.append((record,stream,event))
                if not found or any(tm is not None and (tm.findtext('actual-notes'),tm.findtext('normal-notes'))!=('3','2')
                                    for _,_,e in found for tm in [e.node.find('time-modification')]):
                    reason='unsupported_tuplet_or_missing_tie_chain';break
                pieces.append((key,found))
        if reason is not None:
            rejected.append({'input_ids':group['input_ids'],'reason':reason});continue
        # A model-confirmed roll may have been split across lanes solely by
        # staggered attacks. Move only its explicitly selected notes into the
        # earliest note's lane; the sounding-signature gate protects all others.
        earliest=min((item for _,found in pieces for item in found),
                     key=lambda item:item[0]['start']+Fraction(item[2].start,item[0]['division']))
        voice=earliest[2].node.findtext('voice','1')
        # Validate every new boundary before changing the in-memory tree.
        spans=[]
        for record in records:
            begin=max(target,record['start']);stop=min(finish,record['start']+record['length'])
            if stop<=begin:continue
            a=(begin-record['start'])*record['division'];b=(stop-record['start'])*record['division']
            if a.denominator!=1 or b.denominator!=1:reason='unrepresentable_boundary';break
            spans.append((record,int(a),int(b),begin>target,stop<finish))
        if reason is not None:
            rejected.append({'input_ids':group['input_ids'],'reason':reason});continue
        destinations={id(record['measure']):next((s for s in record['streams']
            if not record['source_prefix'] and (staff,voice) in _identity(s)),None) for record,_,_,_,_ in spans}
        if any(s is None for s in destinations.values()):
            rejected.append({'input_ids':group['input_ids'],'reason':'missing_destination_voice'});continue
        for (key,found),old in zip(pieces,selected):
            template=deepcopy(found[0][2].node)
            for record,stream,event in found:
                stream.events.remove(event);dirty.add((id(record['measure']),id(stream)))
            for record,a,b,continuing,continues in spans:
                node=deepcopy(template)
                for tag in ('tie','chord','beam'):
                    for item in list(node.findall(tag)):node.remove(item)
                for notation in node.findall('notations'):
                    for item in list(notation):
                        if item.tag in ('tied','arpeggiate'):notation.remove(item)
                notation=node.find('notations')
                if notation is None:notation=ET.SubElement(node,'notations')
                for kind,enabled in (('stop',continuing),('start',continues)):
                    if enabled:
                        ET.SubElement(node,'tie',{'type':kind});ET.SubElement(notation,'tied',{'type':kind})
                if not continuing:
                    attrs={'direction':'down'} if group.get('direction')=='down' else {}
                    ET.SubElement(notation,'arpeggiate',attrs)
                stream=destinations[id(record['measure'])]
                stream.events.append(_Event(a,b,node));dirty.add((id(record['measure']),id(stream)))
            expected[old]-=1
            if not expected[old]:del expected[old]
            expected[(key,target,finish,False)]+=1
        accepted.append(group)
    meter=(4,4)
    for record in records:
        measure=record['measure'];time=measure.find('attributes/time')
        if time is not None:
            meter = ((0, 0) if time.find('senza-misura') is not None else
                     (int(time.findtext('beats')), int(time.findtext('beat-type'))))
        if record['source_prefix']:continue
        children=[]
        for stream in record['streams']:
            if stream.backup is not None:children.append(stream.backup)
            staff=next(n.findtext('staff','1') for n in stream.children if n.tag=='note')
            if (id(measure),id(stream)) not in dirty:children.extend(stream.children);continue
            voice=next(n.findtext('voice','1') for n in stream.children if n.tag=='note')
            stem=next((n.findtext('stem') for n in stream.children if n.tag=='note' and n.find('stem') is not None),None)
            from .notation_smart_voice import coalesce_ties, set_lane
            coalesce_ties(stream)
            compound=meter[1]==8 and meter[0] in (6,9,12,15,18)
            if any(e.node.find('time-modification') is not None for e in stream.events):
                from .notation_range_rhythm import range_replacement
                replacement=range_replacement([stream],staff,record['division'],compound)
            else:
                replacement,_=_replacement([stream],staff,record['division'],compound)
                _restore_missing_beams(replacement,record['division'],meter)
            set_lane(replacement,voice,stem)
            children.extend(replacement)
        measure[:]=children
    contents=ET.tostring(tree.getroot(),encoding='utf-8',xml_declaration=True)
    if sounding_signature(BytesIO(contents))!=expected:
        raise ValueError('滚奏记谱改变了允许组以外的音高或时值，保留原谱')
    if accepted:path.write_bytes(contents)
    return {'method':'post_voice_roll_intervals_v1','accepted':accepted,'rejected':rejected,
            'outside_group_intervals_preserved':True,'raw_events_modified':False}
