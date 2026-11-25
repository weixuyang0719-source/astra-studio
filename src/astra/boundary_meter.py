"""Exact notation for an already-confirmed opening/ending measure boundary.

This does not detect pickups, trim tails, change beats, or construct tempo.
Callers must supply a confirmed boundary span in quarter-note units.
"""
import math


def boundary_time_signature(span_quarters, surrounding_denominators=(4,)):
    """Represent a span without reducing below the surrounding beat unit.

    Thus two quarters beside 4/4 become 2/4, not 1/2; beside 6/8 they
    become 4/8. Finer binary units are allowed only when necessary. Unlike
    host boundary compensation this never rounds to a 64th or folds a long
    span into fewer beats. Values beyond SMF's exact meter range are rejected.
    """
    try:
        span = float(span_quarters)
        denominators = tuple(surrounding_denominators)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError('边界小节长度或周围拍号无效') from exc
    allowed = (1, 2, 4, 8, 16, 32, 64, 128)
    if (isinstance(span_quarters, bool) or not math.isfinite(span) or span <= 0
            or not denominators or any(type(d) is not int or d not in allowed for d in denominators)):
        raise ValueError('边界小节长度或周围拍号无效')
    preferred = min(denominators)
    for denominator in allowed:
        if denominator < preferred:
            continue
        numerator = round(span * denominator / 4)
        # Ignore floating-point interpolation noise, not a musical grid step.
        if 1 <= numerator <= 255 and abs(numerator * 4 / denominator - span) <= 1e-9:
            return {'numerator': numerator, 'denominator': denominator}
    raise ValueError('边界小节长度无法精确写成 MIDI 拍号；请明确二分音符细分边界，不会自动舍入或改变速度')
