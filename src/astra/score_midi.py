"""Audition MIDI from final notation intervals, with an explicit reference clock.

This is neither the original performance nor a re-transcription. Fermatas and
renderer playback conventions cannot change its tempo. Supplied controllers
remain seconds-domain performance data, separately from written key durations.
"""
from collections import defaultdict
from copy import deepcopy
from fractions import Fraction
import hashlib
import math
from pathlib import Path
import xml.etree.ElementTree as ET

import mido

from .midi import PPQ, _track, export_conductor, control_events
from .notation_readability import sounding_signature
from .score_tempo import _measure_layout
from .timing import TempoMap


CHANNELS = {0: (0, 2, 4, 6, 8, 11, 13, 15), 1: (1, 3, 5, 7, 10, 12, 14)}


def _pitch(key):
    _, _, step, alter, octave = key
    semitone = Fraction({'C': 0, 'D': 2, 'E': 4, 'F': 5, 'G': 7, 'A': 9, 'B': 11}[step])
    value = (int(octave) + 1) * 12 + semitone + Fraction(alter)
    if value.denominator != 1 or not 0 <= value <= 127:
        raise ValueError('谱面包含普通 MIDI 音高不能精确表示的微分音或越界音符')
    return int(value)


def _performance(notes, clock):
    result = []
    for index, note in enumerate(notes or []):
        try:
            pitch, start, end = int(note['pitch']), float(note['start']), float(note['end'])
            velocity = int(note.get('velocity', 72))
            if not all(math.isfinite(value) for value in (start, end)) or start < 0 or end <= start:
                continue
            if not 0 <= pitch <= 127 or not 1 <= velocity <= 127:
                continue
            result.append({'index': index, 'pitch': pitch, 'start': start, 'end': end,
                           'beat': clock.to_beat(start), 'velocity': velocity})
        except (ValueError, TypeError, KeyError):
            continue
    return result


def _match(note, performance, used, *, grace=False):
    candidates = []
    for original in performance:
        if original['index'] in used or original['pitch'] != note['pitch']:
            continue
        if grace and original['end'] - original['start'] > .25:
            continue
        distance = abs(original['beat'] - float(note['start']))
        if distance <= (.5 if grace else .25):
            candidates.append((distance, original['index'], original))
    candidates.sort(key=lambda item: (item[0], item[1]))
    # An ambiguous velocity should remain explicitly fixed, not be attributed
    # to whichever nearby performance event happened to be iterated first.
    if not candidates or len(candidates) > 1 and candidates[1][0] - candidates[0][0] < .04:
        return None
    original = candidates[0][2]
    used.add(original['index'])
    return original


def write_score_midi(xml_path, tempo_data, output_path, performance_notes=None, *,
                     controls=None, pedals=None, purpose='mapped', include_time_signatures=True):
    """Write conductor + right/left hand tracks and return JSON-safe audit data.

    Ordinary pitch/onset/release come only from sounding_signature(final XML),
    including merged cross-bar ties. Reference TempoMap governs playback; XML
    tempo/fermata playback hints cannot add unexpected changes. If a zero-length
    grace cannot be matched to a short original event, a disclosed <=50ms
    approximation is used, without moving regular notes.
    """
    xml_path, output_path = Path(xml_path), Path(output_path)
    if xml_path.resolve() == output_path.resolve():
        raise ValueError('谱面 MIDI 不得覆盖 MusicXML')
    clock = TempoMap(deepcopy(tempo_data.data if isinstance(tempo_data, TempoMap) else tempo_data))
    tree = ET.parse(xml_path)
    root = tree.getroot()
    if root.tag != 'score-partwise':
        raise ValueError('需要最终 score-partwise MusicXML')
    parts = [part.get('id') for part in root.findall('part')]
    if not 1 <= len(parts) <= 2 or len(set(parts)) != len(parts):
        raise ValueError('钢琴谱面需要一个双谱表声部或两个单谱表声部')
    layout = _measure_layout(root)
    score_end = max((start + extent for _, _, start, extent, _ in layout), default=Fraction(0))
    notes = []
    for (key, start, end, grace), count in sounding_signature(xml_path).items():
        part, staff = key[:2]
        if len(parts) == 1 and staff in ('1', '2'):
            hand = int(staff) - 1
        elif len(parts) == 2 and staff == '1':
            hand = parts.index(part)
        else:
            raise ValueError('无法可靠分配最终谱面的左右手谱表')
        if start < 0 or end < start or not grace and end == start:
            raise ValueError('普通谱面音符必须具有正时值和非负起点')
        for _ in range(count):
            notes.append({'pitch': _pitch(key), 'start': start, 'end': end, 'grace': grace,
                          'part':part,'staff':staff,
                          'hand': hand, 'velocity': 72, 'velocity_source': 'fixed_72'})
    if not notes:
        raise ValueError('最终谱面没有可试听的有音高音符')

    performance = _performance(performance_notes, clock)
    from .notation_free_run import playback_overrides
    overrides=playback_overrides(root,performance,clock)
    reserved={n['index'] for n in overrides.values()}
    ordinary_performance=[n for n in performance if n['index'] not in reserved]
    free_audit=[]
    used = set()
    for note in sorted((n for n in notes if not n['grace']), key=lambda n: (n['start'], n['hand'], n['pitch'])):
        key=(note['part'],note['staff'],note['pitch'],note['start'],note['end'])
        original=overrides.pop(key,None)
        if original is not None:
            if original['index'] in used:raise ValueError('花奏原演奏已被另一音符使用，拒绝重复归属')
            used.add(original['index'])
            free_audit.append({'input_id':original['input_id'],'performance_index':original['index'],
                'pitch':note['pitch'],'source_start_seconds':original['start'],'source_end_seconds':original['end'],
                'written_start_beat':float(note['start']),'written_end_beat':float(note['end'])})
            note.update(start=Fraction(str(clock.to_beat(original['start']))),end=Fraction(str(clock.to_beat(original['end']))))
        else:
            original = _match(note, ordinary_performance, used)
        if original:
            note.update(velocity=original['velocity'], velocity_source='matched_performance',
                        performance_index=original['index'])
    if overrides:raise ValueError('部分花奏映射未进入最终 MIDI，拒绝漏音')

    warnings, grace_audit = [], []
    regular_attacks = defaultdict(list)
    for note in notes:
        if not note['grace']:
            regular_attacks[note['pitch']].append(float(note['start']))
    grace_groups = defaultdict(list)
    for note in notes:
        if note['grace']:
            grace_groups[(note['hand'], note['start'])].append(note)
    for (_, anchor), group in grace_groups.items():
        anchor_seconds = clock.to_seconds(float(anchor))
        for order, note in enumerate(group):
            original = _match(note, performance, used, grace=True)
            if original:
                begin, finish = original['start'], original['end']
                method = 'matched_short_performance'
                note.update(velocity=original['velocity'], velocity_source='matched_performance',
                            performance_index=original['index'])
            else:
                # Share at most 50ms before the written anchor, preserving XML
                # traversal order when several unmatched ornaments share it.
                base = max(0., anchor_seconds - .05)
                width = (anchor_seconds - base) / len(group)
                begin, finish = base + width*order, base + width*(order+1)
                if finish <= begin:  # Opening grace: no negative pre-roll.
                    begin, finish = anchor_seconds, anchor_seconds + .05/len(group)
                method = 'approximation_50ms_group'
                warnings.append('装饰音没有唯一原演奏匹配，使用明确标记的短时值近似；主音位置不变。')
            start_beat, end_beat = clock.to_beat(begin), clock.to_beat(finish)
            next_attacks = [beat for beat in regular_attacks[note['pitch']] if beat >= start_beat - 1e-9]
            if next_attacks:
                end_beat = min(end_beat, min(next_attacks))
            if end_beat <= start_beat:
                end_beat = start_beat + 1/PPQ
                method += '_single_tick_collision'
                warnings.append('装饰音与同音高主音没有可用间隙，保留独立通道的单 tick 音符并标记核对。')
            note.update(start=Fraction(str(start_beat)), end=Fraction(str(end_beat)))
            grace_audit.append({'pitch': note['pitch'], 'hand': note['hand'], 'anchor_beat': float(anchor),
                                'start_beat': start_beat, 'end_beat': end_beat, 'method': method,
                                'performance_index': note.get('performance_index')})

    events = {0: [], 1: []}
    active, used_channels = defaultdict(dict), {0: set(), 1: set()}
    maximum_error = ordinary_error = 0.
    for note in sorted(notes, key=lambda n: (n['start'], n['hand'], n['pitch'], n['grace'])):
        if purpose == 'seconds_120':
            start, end = (round(clock.to_seconds(float(note[key])) * 2 * PPQ) for key in ('start', 'end'))
        else:
            start, end = round(note['start']*PPQ), round(note['end']*PPQ)
        if end <= start:
            if not note['grace']:
                raise ValueError('谱面普通音符时值小于 MIDI 分辨率，拒绝静默拉长')
            end = start + 1
        encoded_beats = [clock.to_beat(t / PPQ / 2) for t in (start, end)] if purpose == 'seconds_120' else [start / PPQ, end / PPQ]
        rounding_error = max(abs(encoded_beats[0]-float(note['start'])), abs(encoded_beats[1]-float(note['end'])))
        maximum_error = max(maximum_error, rounding_error)
        if not note['grace']:
            ordinary_error = max(ordinary_error, rounding_error)
        hand, pitch = note['hand'], note['pitch']
        channels = active[(hand, pitch)]
        channel = next((c for c in CHANNELS[hand] if channels.get(c, -1) <= start), None)
        if channel is None:
            raise ValueError('同一手的同音高重叠超出可用 MIDI 通道，拒绝截断或丢音')
        channels[channel] = end
        used_channels[hand].add(channel)
        events[hand].extend([(start, 3, mido.Message('note_on', channel=channel, note=pitch, velocity=note['velocity'])),
                             (end, 2, mido.Message('note_off', channel=channel, note=pitch, velocity=0))])
    source_controls = control_events(clock, controls=controls, pedals=pedals,
                                     channels=sorted(used_channels[0] | used_channels[1]), purpose=purpose)
    # Hand assignment is a notation presentation. Pedal applies to both hands;
    # retain source samples and mirror CC64 only onto newly allocated channels.
    mirrored = []
    if controls is not None:
        source_pedal_channels = {msg.channel for _, _, msg in source_controls if msg.control == 64}
        if len(source_pedal_channels) == 1:
            for tick, priority, msg in source_controls:
                if msg.control == 64:
                    mirrored.extend((tick, priority, msg.copy(channel=c))
                                    for c in sorted((used_channels[0] | used_channels[1]) - source_pedal_channels))
    score_end_tick = round((clock.to_seconds(float(score_end)) * 2 if purpose == 'seconds_120' else float(score_end)) * PPQ)
    last_tick = max(score_end_tick, max(t for values in events.values() for t, _, _ in values),
                    max((t for t, _, _ in source_controls), default=0))
    conductor = [event for event in export_conductor(clock, purpose, include_time_signatures) if event[0] < last_tick]
    conductor[0] = (0, -10, mido.MetaMessage('track_name', name='Astra Score / explicit tempo'))
    conductor.extend([(0, -9, mido.MetaMessage('text', text='Final notation audition; performance MIDI unchanged; supplied controllers preserved')),
                      (last_tick, 10, mido.MetaMessage('marker', text='End of written score'))])
    midi = mido.MidiFile(type=1, ticks_per_beat=PPQ)
    midi.tracks.append(_track(conductor + source_controls + mirrored))
    for hand, name in ((0, 'Piano Right Hand'), (1, 'Piano Left Hand')):
        track_events = [(0, -2, mido.MetaMessage('track_name', name=name))]
        track_events.extend((0, -1, mido.Message('program_change', channel=c, program=0))
                            for c in sorted(used_channels[hand]))
        midi.tracks.append(_track(track_events + events[hand]))
    if maximum_error > 1e-10:
        warnings.append('部分拍位或装饰音按 MIDI tick 四舍五入；审计提供最大误差。')
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_name(output_path.name + '.tmp')
    midi.save(temporary)
    temporary.replace(output_path)
    matched = sum(n['velocity_source'] == 'matched_performance' for n in notes)
    return {'output_path': str(output_path.resolve()), 'source_xml_sha256': hashlib.sha256(xml_path.read_bytes()).hexdigest(),
            'note_count': len(notes), 'right_hand_notes': sum(n['hand'] == 0 for n in notes),
            'left_hand_notes': sum(n['hand'] == 1 for n in notes), 'grace_count': len(grace_audit),
            'ordinary_note_count': len(notes)-len(grace_audit),
            'grace_notes': grace_audit, 'matched_velocity_notes': matched, 'fixed_velocity_notes': len(notes)-matched,
            'free_run_source_playback':free_audit,
            'velocity_policy': 'Unique nearby same-pitch performance velocity; otherwise fixed 72, not original dynamics.',
            'timing_policy': ('Final MusicXML sounding intervals and explicit TempoMap; explicit accepted free-run groups use verified original seconds; no fermata playback stretching.'),
            'pedal_policy': 'Supplied seconds-domain controllers preserved; single-channel CC64 mirrored to both hand channels.',
            'controller_events': len(source_controls), 'mirrored_pedal_events': len(mirrored), 'export_purpose': purpose,
            'score_end_beat': float(score_end), 'score_duration_seconds': last_tick/PPQ/2 if purpose == 'seconds_120' else clock.to_seconds(last_tick/PPQ),
            'ticks_per_beat': PPQ, 'maximum_rounding_error_beats': maximum_error,
            'ordinary_maximum_rounding_error_beats': ordinary_error,
            'warnings': list(dict.fromkeys(warnings))}
