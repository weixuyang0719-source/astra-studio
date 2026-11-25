"""Explicit score ending, separate from performance duration and audio tails."""
import math
from copy import deepcopy

from .boundary_meter import boundary_time_signature


def confirmed_end_beat(clock):
    end = clock.data.get('score_end')
    if end is None:
        return None
    if not isinstance(end, dict) or end.get('origin') != 'editor_score_end':
        raise ValueError('谱面终点缺少明确确认来源，请重新设置或取消终点')
    seconds = end.get('seconds')
    if (isinstance(seconds, bool) or not isinstance(seconds, (int, float))
            or not math.isfinite(seconds) or not 0 < seconds <= clock.seconds[-1]):
        raise ValueError('谱面终点必须在当前音频范围内且晚于起点')
    beat = clock.to_beat(seconds)
    if beat <= float(clock.data.get('first_full_bar_beat', 0)) + 1e-8:
        raise ValueError('谱面终点必须晚于首个完整小节起点')
    return beat


def ending_measure(clock):
    """Return final bar geometry without trimming any MIDI note/control event."""
    end = confirmed_end_beat(clock)
    if end is None:
        return None
    from .timing import TempoMap
    from .duet_notation import reference_bars
    plain = deepcopy(clock.data)
    plain.pop('score_end', None)
    starts, lengths, _ = reference_bars(TempoMap(plain), end)
    index = max(i for i, start in enumerate(starts) if start < end - 1e-8)
    start = starts[index]
    surrounding = next(s for s in reversed(clock.data['time_signatures']) if s['beat'] <= start + 1e-8)
    length = end - start
    partial = length < lengths[index] - 1e-8
    meter = boundary_time_signature(length, (surrounding['denominator'],)) if partial else {
        'numerator': surrounding['numerator'], 'denominator': surrounding['denominator']}
    return {'start': start, 'end': end, 'length': length, 'partial': partial,
            'meter': meter, 'surrounding': surrounding}
