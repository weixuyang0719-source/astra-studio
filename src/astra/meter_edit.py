"""Align an explicitly selected audio time to a complete bar boundary."""

from bisect import bisect_right
from copy import deepcopy
from math import floor, isfinite

from .timing import TempoMap


_QUICK_METERS = (2, 3, 4)
_EPS = 1e-7
_MAX_BEAT_OFFSET = .28


def _meter(row):
    return row['numerator'], row['denominator']


def _bar_length(meter):
    return 4 * meter[0] / meter[1]


def _same(a, b):
    return abs(a - b) <= _EPS


def _validate_barlines(signatures, pickup):
    current = _meter(signatures[0])
    origin = pickup
    for row in signatures[1:]:
        meter = _meter(row)
        if meter == current:
            continue
        bars = (row['beat'] - origin) / _bar_length(current)
        if bars < -_EPS or not _same(bars, round(bars)):
            raise ValueError(f'后续 {row["beat"]:g} 拍的拍号将不在小节线上。请先核对该变拍点。')
        origin, current = row['beat'], meter


def _candidate(beat, signatures, pickup, previous):
    candidates = []
    for index, row in enumerate(signatures):
        meter = _meter(row)
        start = row['beat'] if index else pickup
        limit = signatures[index + 1]['beat'] if index + 1 < len(signatures) else float('inf')
        length = _bar_length(meter)
        requested = float(previous) if previous is not None else length
        guess = floor((beat - start - requested) / length)
        for number in (guess, guess + 1):
            if number < 0:
                continue
            left = start + number * length
            target = left + requested
            if left < limit - _EPS and target <= limit + _EPS:
                candidates.append((abs(beat - target), left, target, meter))
    if previous is None and pickup > 0:
        candidates.append((abs(beat - pickup), 0., pickup, _meter(signatures[0])))
    if candidates:
        distance, left, target, meter = min(candidates)
        if distance <= min(_MAX_BEAT_OFFSET, 2 / meter[1]):
            return left, target, meter
    raise ValueError('这里离现有小节线较远。若前一小节实际为 2、3 或 4 拍，请在「前一小节」明确选择；也可调整时间再预览。')


def _normalize_signatures(rows):
    result = []
    for row in sorted(rows, key=lambda item: item['beat']):
        if result and _same(row['beat'], result[-1]['beat']):
            result[-1] = row
        elif not result or _meter(result[-1]) != _meter(row):
            result.append(row)
    return result


def preview_meter_edit(data, seconds, numerator=None, previous=None):
    """Return a detached draft. Preserve every later interval in audio seconds."""
    if type(seconds) not in (float, int) or not isfinite(seconds) or seconds < 0:
        raise ValueError('请输入有效的时间（秒）。')
    if numerator is not None and (type(numerator) is not int or numerator not in _QUICK_METERS):
        raise ValueError('快捷修正支持 2/4、3/4 和 4/4。')
    if previous is not None and previous != 'pickup' and (type(previous) is not int or previous not in _QUICK_METERS):
        raise ValueError('请选择有效的前一小节拍号。')
    if not isinstance(data, dict):
        raise ValueError('速度图必须是对象。')
    result = deepcopy(data)
    tempo = TempoMap(result)
    if seconds >= tempo.seconds[-1]:
        raise ValueError('请输入速度图范围内且早于结尾的时间。')
    signatures = _normalize_signatures(result['time_signatures'])
    pickup = float(result.get('pickup_beats', 0))
    _validate_barlines(signatures, pickup)
    beat = tempo.to_beat(seconds)
    target = beat
    previous_start = None
    original_meters = [row['beat'] for row in signatures]
    if _same(seconds, 0):
        if previous is not None:
            raise ValueError('曲首没有前一小节。')
        target, meter = 0., _meter(signatures[0])
    elif previous == 'pickup':
        first_meter = _meter(signatures[0])
        first_length = _bar_length(first_meter)
        if beat <= _EPS or beat >= first_length or any(row['beat'] < beat - _EPS for row in signatures[1:]):
            raise ValueError('弱起须在曲首，且短于一个完整小节。')
        # Only this explicit user choice permits a bounded rhythmic inference.
        # Round musical quarters, not seconds (which vary with the tempo).
        target = round(beat * 4) / 4
        if target <= 0 or target >= first_length:
            raise ValueError('这个位置太接近曲首或完整小节，无法确定一个短弱起。若没有弱起，请保留曲首；否则请定位真正的首个完整小节第一拍。')
        if abs(target - beat) > .125 + _EPS:
            raise ValueError('所选位置与常见弱起长度相差较大，请重新定位首个完整小节第一拍。')
        previous_start, pickup = 0., target
        # A new first complete bar defines the musical subdivision phase. It
        # is not an audio timestamp to move again during later phase edits.
        result['grid_origin'] = pickup
        meter = first_meter
    else:
        previous_start, target, before_meter = _candidate(beat, signatures, pickup, previous)
        meter = _meter(signatures[bisect_right(original_meters, target + _EPS) - 1])
    if not _same(target, beat):
        unit = 1. if previous is not None else 4 / before_meter[1]
        left = max(previous_start, target - unit)
        if beat <= left + _EPS:
            raise ValueError('修正会压缩掉前一拍。请选择更准确的时间。')
        left_seconds = tempo.to_seconds(left)
        offset = target - beat

        def shift(position):
            if position >= beat:
                return position + offset
            if position <= left:
                return position
            return left + (position - left) * (target - left) / (beat - left)

        anchors = {a['seconds']: {**a, 'beat': shift(a['beat'])} for a in result['anchors']
                   if not (_same(a['seconds'], left_seconds) or _same(a['seconds'], seconds))}
        anchors[left_seconds] = {'seconds': left_seconds, 'beat': left}
        anchors[seconds] = {'seconds': seconds, 'beat': target}
        result['anchors'] = [anchors[t] for t in sorted(anchors)]
        # Imported MIDI tempo events describe the old clock. Opposite local
        # edits can cancel their total beat offset without restoring local
        # timing, so an aggregate tempo check cannot safely reuse this cache.
        result.pop('midi_tempo_events', None)
        # Keep the musical grid fixed: target is the confirmed downbeat.
        # Shifting grid_origin as though it were an audio event would snap
        # that very downbeat away from target (notably for weak pickups).
        signatures = [{**row, 'beat': shift(row['beat'])} for row in signatures if not _same(row['beat'], target)]

    desired = (numerator, 4) if numerator is not None else meter
    if previous in _QUICK_METERS:
        signatures.append({'beat': previous_start, 'numerator': previous, 'denominator': 4})
    if not _same(seconds, 0) and not any(_same(row['seconds'], seconds) for row in result['anchors']):
        result['anchors'].append({'seconds': seconds, 'beat': target})
        result['anchors'].sort(key=lambda row: row['seconds'])
    signatures.append({'beat': target, 'numerator': desired[0], 'denominator': desired[1]})
    result['time_signatures'] = _normalize_signatures(signatures)
    result['pickup_beats'] = pickup
    _validate_barlines(result['time_signatures'], pickup)
    revised_tempo = TempoMap(result)
    # Printed rit./a tempo marks denote audible events. Unlike the quantizer's
    # musical phase, their beat coordinates must follow a changed audio clock.
    if result.get('notation_tempo_marks'):
        result['notation_tempo_marks'] = [
            {**mark, 'beat': revised_tempo.to_beat(tempo.to_seconds(float(mark['beat'])))}
            for mark in result['notation_tempo_marks']]

    edit = {'seconds': seconds, 'beat': target, 'previous_beat': beat,
            'numerator': desired[0], 'denominator': desired[1], 'previous': previous,
            'beat_adjustment': target - beat}
    if previous == 'pickup':
        edit.update(pickup_beats=pickup,pickup_snap_step=.25)
    result['manual_meter_edits'] = [*result.get('manual_meter_edits', []), edit]
    result['source'] = 'manual'
    result.pop('constant_bpm', None)
    result.pop('fit', None)
    return {'data': result, 'edit': edit}


def preview_roll_edit(data, seconds, *, opening=False, numerator=None):
    """A user-picked first downbeat defines bar phase, not the first sounding note.

    Full intro bars and leading silence remain; only the fractional prefix is
    a pickup. Existing future meter changes must still land on valid barlines.
    """
    if not opening:
        return preview_meter_edit(data,seconds,numerator)
    if type(seconds) not in (float,int) or not isfinite(seconds) or seconds<0:
        raise ValueError('请选择有效的音符或标尺位置。')
    if numerator is not None and (type(numerator) is not int or numerator not in _QUICK_METERS):
        raise ValueError('请选择 2/4、3/4 或 4/4。')
    draft=deepcopy(data);clock=TempoMap(draft)
    if seconds>=clock.seconds[-1]:raise ValueError('请选择曲目结束前的位置。')
    beat=clock.to_beat(seconds);target=round(beat*4)/4
    if seconds>0 and target<=0:raise ValueError('这里太接近起点，请选择首个完整小节的第一拍。')
    first=draft['time_signatures'][0]
    if numerator is not None:first.update(numerator=numerator,denominator=4)
    length=_bar_length(_meter(first))
    # The selected downbeat may follow a whole bar of intro: do not compress
    # that intro into a shorter pickup or change its notes to make it fit.
    pickup=target%length
    draft.update(pickup_beats=pickup,grid_origin=pickup)
    result=preview_meter_edit(draft,seconds)
    result['data']['intro_downbeat_seconds']=seconds
    result['edit'].update(opening=True,pickup_beats=pickup,intro_full_bars=int(target//length))
    return result
