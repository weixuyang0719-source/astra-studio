"""Pure opening interpretation, independent of performance seconds and tempo warp.

The existing fields remain the serialization boundary so historical projects and
notation consumers need no migration. No source note, CC, or audio is an input.
"""
from copy import deepcopy
import math

from .timing import TempoMap

EPS = 1e-8


def _without_obsolete_phase_markers(rows):
    """Remove only redundant old synthetic phase labels, never authored changes."""
    result = []
    for row in rows:
        redundant = (result and row.get('origin') == 'editor_first_bar'
                     and all(row[key] == result[-1][key]
                             for key in ('numerator', 'denominator')))
        if not redundant:
            result.append(deepcopy(row))
    return result


def apply_opening(tempo, operation):
    """Return ``{tempo, summary, warnings}`` for an explicit set_opening intent.

    ``seconds`` denotes the first complete bar, not a trim boundary. A pickup
    inherits the signature effective at that point, can be shorter than one
    beat, and cannot contain an unrelated meter change. Intro material remains
    present and can span any number of bars. Normal may exclude verified leading
    silence from notation, but never trims the source or its tempo mapping.
    """
    if operation.get('action', 'set_opening') != 'set_opening':
        raise ValueError('未知开头操作；恢复初稿应由版本化工程负责')
    mode = operation.get('mode')
    if mode not in ('normal', 'pickup', 'intro'):
        raise ValueError('请选择正常、弱起或前奏开头')
    seconds = operation.get('seconds', 0 if mode == 'normal' else None)
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not math.isfinite(seconds):
        raise ValueError('第一完整小节位置必须为有限秒数')
    old = TempoMap(deepcopy(tempo))
    if not 0 <= seconds < old.seconds[-1] - EPS:
        raise ValueError('第一完整小节必须位于音频结束之前')
    if mode in ('pickup', 'intro') and seconds <= EPS:
        raise ValueError('没有前段时请选择正常开头；弱起或前奏需要正的起始长度')
    seconds = float(seconds)
    beat = old.to_beat(seconds)
    result = deepcopy(tempo)
    rows = _without_obsolete_phase_markers(result['time_signatures'])
    if mode == 'pickup':
        if any(EPS < row['beat'] < beat - EPS for row in rows):
            raise ValueError('弱起内部存在拍号变化；请保留为前奏，或先明确处理这些拍号事件')
        inherited = next(row for row in reversed(rows) if row['beat'] <= beat + EPS)
        length = inherited['numerator'] * 4 / inherited['denominator']
        if not EPS < beat < length - EPS:
            raise ValueError('弱起必须短于继承拍号的一小节；较长前段请选前奏，不会自动截断')
        # Inheriting the first full bar's meter is part of the explicit pickup
        # operation. Keep the first row's identity and every later meter event.
        rows[0].update(numerator=inherited['numerator'], denominator=inherited['denominator'])
    result.update(time_signatures=rows, pickup_beats=beat if mode == 'pickup' else 0.0,
                  first_full_bar_beat=beat, grid_origin=beat, opening_mode=mode,
                  intro_downbeat_seconds=seconds,
                  opening_policy={'source': 'user', 'protected': True, **({'score_only': True} if mode == 'normal' else {})})
    if mode == 'normal' and seconds > EPS:
        result['score_start_seconds'] = seconds
    else:
        result.pop('score_start_seconds', None)
    TempoMap(result)
    names = {'normal': '正常开头', 'pickup': '弱起', 'intro': '前奏'}
    summary = f'开头设为{names[mode]}；第一完整小节在 {seconds:g} 秒，原始演奏时间不变'
    warnings = []
    if mode == 'intro' or (mode == 'normal' and seconds>0):
        warnings.append('若开头长度无法精确写成标准 MIDI 短拍号，导出会保留播放时间并写入边界标记和拍号事件；不保证 Cubase 等外部 DAW 在该位置重起小节，小节网格可能需手动对齐。')
    if mode == 'intro':
        warnings.append('第一完整小节之前的音符和拍号全部保留为前奏，不会自动并入弱起')
    return {'tempo': result, 'summary': summary, 'warnings': warnings}
