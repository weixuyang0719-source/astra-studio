"""Pure, seconds-preserving edits of the project's single musical clock.

All ranges are half-open audio seconds. Beats are quarter-note equivalents,
never numerator-dependent counts. UI history owns persistence and undo.
"""
from copy import deepcopy
from math import isfinite
from bisect import bisect_right

from .timing import TempoMap

EPS = 1e-8


def _number(value, name):
    if isinstance(value, bool):
        raise ValueError(f'{name} 必须是有限数值')
    try:
        value = float(value)
    except (TypeError, ValueError):
        raise ValueError(f'{name} 必须是有限数值') from None
    if not isfinite(value):
        raise ValueError(f'{name} 必须是有限数值')
    return value


def _time(value, clock):
    value = _number(value, '时间')
    if value < 0 or value > clock.seconds[-1] + EPS:
        raise ValueError('时间超出当前音频范围')
    return min(value, clock.seconds[-1])


def _range(op, clock):
    start, end = _time(op.get('start', 0), clock), _time(op.get('end', clock.seconds[-1]), clock)
    if end - start <= EPS:
        raise ValueError('请指定有正时长的左右定位点')
    return start, end


def _meter_at(data, beat):
    return next(row for row in reversed(data['time_signatures']) if row['beat'] <= beat + EPS)


def _rows(rows):
    result = []
    for row in sorted(rows, key=lambda r: r['beat']):
        if result and abs(result[-1]['beat'] - row['beat']) < EPS:
            result[-1] = row
        else:
            result.append(row)
    return result


def _remap_structure(data, old, new):
    """Meter changes belong to source time; only their music addresses move."""
    data['time_signatures'] = [dict(row, beat=new.to_beat(old.to_seconds(row['beat'])))
                               for row in data['time_signatures']]
    for key in ('grid_origin', 'first_full_bar_beat'):
        if key in data:
            data[key] = new.to_beat(old.to_seconds(data[key]))
    if data.get('pickup_beats'):
        full = new.to_beat(old.to_seconds(data['pickup_beats']))
        initial = data['time_signatures'][0]
        size = initial['numerator'] * 4 / initial['denominator']
        data['pickup_beats'] = full % size
        data.setdefault('first_full_bar_beat', full)


def _meter_edit_constraints(before, after, operation):
    """A bounded tag edit preserves outside physical structure, not numbering."""
    scope=operation.get('scope')
    if scope is None:return
    from .rebar_edit import list_boundaries
    old,new=TempoMap(deepcopy(before)),TempoMap(deepcopy(after))
    prior,current=list_boundaries(before,'before'),list_boundaries(after,'after')
    if scope is not None:
        if not isinstance(scope,dict) or not {'start','end'}<=scope.keys():
            raise ValueError('编辑选区已失效，请重新框选。')
        left,right=_range(scope,old)
        if operation.get('type')!='meter':
            raise ValueError('框内拍号修改须限定范围并在末端恢复，不能直接修改全局标签。')
        start,end=_range(operation,old)
        if start<left-EPS or end>right+EPS:
            raise ValueError('拍号操作越过了编辑选区；框外内容保持不变。')
        def exterior(rows):
            return [(r['seconds'],r['meter']) for r in rows
                    if r['seconds']<left-EPS or r['seconds']>=right-EPS]
        a,b=exterior(prior),exterior(current)
        if len(a)!=len(b) or any(x[1]!=y[1] or abs(x[0]-y[0])>EPS for x,y in zip(a,b)):
            raise ValueError('拍号修改会移动框外小节线或改变框外拍号；请调整选区内的划分。小节重新编号不受限制。')


def apply_tempo_operation(data, operation, fixed_anchors=None):
    """Return a detached {tempo, fixed_anchors, summary, warnings} draft.

    Legacy ``fixed_anchors`` is accepted but ignored. Range endpoints and
    valid monotonic timing remain mandatory; hidden editor locks are retired.
    """
    result = deepcopy(data)
    pins = []
    old = TempoMap(deepcopy(data))
    op = dict(operation)
    kind = op.get('type', op.get('action'))
    kind = {'regroup': 'meter', 'move': 'move_point', 'insert': 'add_point',
            'delete': 'remove_point'}.get(kind, kind)
    warnings = []

    if kind in ('meter_event','add_meter_event','remove_meter_event'):
        at = _time(op.get('seconds'), old)
        beat = old.to_beat(at)
        index = next((i for i,row in enumerate(result['time_signatures']) if abs(row['beat']-beat) < EPS), None)
        if kind=='add_meter_event':
            if index is not None:
                raise ValueError('此小节已有拍号标签，请修改现有标签。')
            rows=result['time_signatures']
            rows.append({'beat':beat,'numerator':op.get('numerator'),'denominator':op.get('denominator',4)})
            result['time_signatures']=_rows(rows)
            index=next(i for i,row in enumerate(result['time_signatures']) if abs(row['beat']-beat)<EPS)
        elif index is None:
            raise ValueError('此处没有拍号标记，请明确选中已有标记；范围变拍请使用选区操作')
        if kind=='remove_meter_event':
            if index==0:raise ValueError('不能删除起始拍号；可以修改它。')
            del result['time_signatures'][index]
            # A restoration label identical to the preceding signature is now
            # redundant, not an instruction to start another off-grid bar.
            merged=[]
            for row in result['time_signatures']:
                if (merged and row.get('origin')!='editor_first_bar'
                        and all(row[k]==merged[-1][k] for k in ('numerator','denominator'))):
                    continue
                merged.append(row)
            result['time_signatures']=merged
            summary='已移除拍号标签并合并重复恢复标签；拍点、音符和踏板秒时间不变。'
        else:
            n, d = op.get('numerator'), op.get('denominator', 4)
            if type(n) is not int or not 1 <= n <= 32 or type(d) is not int or d not in (1,2,4,8,16,32,64,128):
                raise ValueError('请输入有效拍号，例如 3/4 或 6/8')
            if index == 0 and result.get('pickup_beats',0) >= n*4/d:
                raise ValueError('现有弱起长于新小节，请先调整弱起或使用完整引子模式')
            result['time_signatures'][index].update(numerator=n, denominator=d)
            end = (old.to_seconds(result['time_signatures'][index+1]['beat'])
                   if index+1 < len(result['time_signatures']) else old.seconds[-1])
            summary = f'将 {at:g} 秒小节首拍的标签设为 {n}/{d}，持续至 {end:g} 秒的下一标签或曲尾；速度与演奏秒数不变'

    elif kind == 'meter':
        start, end = _range(op, old)
        n, d = op.get('numerator'), op.get('denominator', 4)
        if type(n) is not int or not 1 <= n <= 32 or type(d) is not int or d not in (1, 2, 4, 8, 16, 32, 64, 128):
            raise ValueError('请输入有效拍号，例如 4/4 或 6/8')
        left, right = old.to_beat(start), old.to_beat(end)
        inside = [r for r in result['time_signatures'] if left + EPS < r['beat'] < right - EPS]
        if inside and not op.get('replace_changes', False):
            raise ValueError('范围内已有变拍；请缩小范围，或明确选择替换范围内拍号')
        restore = dict(_meter_at(result, right), beat=right)
        rows = [r for r in result['time_signatures'] if r['beat'] < left - EPS or r['beat'] >= right - EPS]
        rows.append({'beat': left, 'numerator': n, 'denominator': d})
        if end < old.seconds[-1] - EPS:
            rows.append(restore)
        result['time_signatures'] = _rows(rows)
        if start == 0 and result.get('pickup_beats', 0) >= n * 4 / d:
            raise ValueError('现有弱起长于新小节，请先将弱起改为完整引子或调整范围')
        result['allow_partial_measures'] = True
        summary = f'仅重分组为 {n}/{d}；速度及演奏秒数不变，范围结束恢复原拍号'

    elif kind in ('recount', 'interpret'):
        start, end = _range(op, old)
        left, right = old.to_beat(start), old.to_beat(end)
        span = right - left
        if kind == 'interpret':
            factor = _number(op.get('factor'), '节拍解释倍率')
            if factor not in (.5, 1., 2.):
                raise ValueError('节拍解释仅支持 ½、1、×2')
            target = span * factor
        else:
            target = _number(op.get('quarters', op.get('beats')), '四分音符等值拍数')
        if target <= 0:
            raise ValueError('拍数跨度必须大于零')
        factor, delta = target / span, target - span
        distribution = op.get('distribution', 'preserve')
        if distribution not in ('preserve', 'uniform'):
            raise ValueError('段内分布应为 preserve 或 uniform')
        samples = set(old.seconds + [start, end])
        if distribution == 'uniform':
            samples = {t for t in samples if t <= start or t >= end}
        def mapped(t):
            beat = old.to_beat(t)
            if t <= start: return beat
            if t >= end: return beat + delta
            return left + (beat-left) * factor
        result['anchors'] = [{'seconds': t, 'beat': mapped(t)} for t in sorted(samples)]
        new = TempoMap(dict(result, pickup_beats=0))
        _remap_structure(result, old, new)
        result['allow_partial_measures'] = True
        summary = f'固定 {start:g}–{end:g} 秒，解释为 {target:g} 个四分音符单位；后续音乐编号平移 {delta:+g}，音频位置不变'

    elif kind == 'pulse_times':
        # Explicit acoustic candidate: replace only the selected clock segment.
        # Events remain seconds-based; signatures keep their source seconds.
        start, end = _range(op, old)
        raw = op.get('beat_seconds')
        if not isinstance(raw, list) or not 2 <= len(raw) <= 65:
            raise ValueError('拍点候选需包含 1–64 个间隔及两端')
        seconds = [_number(t, '拍点秒数') for t in raw]
        if (abs(seconds[0]-start) > EPS or abs(seconds[-1]-end) > EPS or
                any(b <= a for a,b in zip(seconds,seconds[1:]))):
            raise ValueError('候选拍点必须严格递增且保持选区精确两端')
        left, right = old.to_beat(start), old.to_beat(end)
        delta = len(seconds)-1-(right-left)
        result['anchors'] = (
            [deepcopy(a) for a in old.anchors if a['seconds'] < start] +
            [{'seconds':t,'beat':left+i} for i,t in enumerate(seconds)] +
            [dict(a,beat=a['beat']+delta) for a in old.anchors if a['seconds'] > end])
        new = TempoMap(dict(result, pickup_beats=0))
        _remap_structure(result, old, new)
        result['anchors'].sort(key=lambda a:a['seconds'])
        result['allow_partial_measures'] = True
        result['local_pulse_decode'] = deepcopy(op.get('analysis', {}))
        summary = (f'依据缓存重选 {start:g}–{end:g} 秒内的 {len(seconds)-1} 个四分音符间隔；'
                   f'后续音乐编号平移 {delta:+g}，音符、踏板和范围外拍点秒位置不变')
        warnings.append('拍号与第一拍不由本操作重新识别；原分组标记保留音频位置，必要时再复核分组。')

    elif kind == 'move_beat':
        from .beat_flags import move_flag
        result,impact=move_flag(result,op,pins)
        summary=(f'拍点位置从 {impact["source_seconds"]:g} 秒移至 {impact["target_seconds"]:g} 秒；'
                 f'影响 {impact["start_seconds"]:g}–{impact["end_seconds"]:g} 秒，拍数与原演奏不变')
    elif kind in ('move_point', 'add_point', 'remove_point'):
        at = _time(op.get('seconds'), old)
        index = next((i for i,a in enumerate(result['anchors']) if abs(a['seconds']-at) < EPS), None)
        if kind != 'add_point' and index is None:
            raise ValueError('此处没有速度映射点，请先选择已有拍点')
        if kind != 'add_point' and (index in (0, len(result['anchors'])-1)):
            raise ValueError('不能移动或删除音频端点／固定锚点，请先解除固定')
        if kind == 'add_point':
            if index is not None:
                raise ValueError('此处已有拍点，请使用移动操作')
            beat = _number(op.get('beat', old.to_beat(at)), '拍位')
            result['anchors'].append({'seconds': at, 'beat': beat})
        elif kind == 'move_point':
            to = _time(op.get('to_seconds', at), old)
            result['anchors'][index] = dict(result['anchors'][index], seconds=to,
                beat=_number(op.get('beat', result['anchors'][index]['beat']), '拍位'))
        else:
            result['anchors'].pop(index)
        result['anchors'].sort(key=lambda a: a['seconds'])
        result['anchors'].sort(key=lambda a:a['seconds'])
        new = TempoMap(dict(result, pickup_beats=0))
        _remap_structure(result, old, new)
        result['allow_partial_measures'] = True
        summary = '局部拍点已调整；音符、踏板及音频秒时间不变'

    elif kind in ('score_end', 'clear_score_end'):
        if kind == 'clear_score_end':
            result.pop('score_end', None)
            summary = '已取消谱面终点；恢复按记谱内容结束，演奏与音频不变'
        else:
            at = _time(op.get('seconds'), old)
            result['score_end'] = {'seconds': at, 'origin': 'editor_score_end'}
            from .score_boundary import ending_measure
            ending_measure(TempoMap(result))  # Reject unrepresentable spans, never round/stretches.
            summary = f'谱面终点设为 {at:g} 秒；只表示末小节，不裁剪原始演奏或音频余音'
            warnings.append('若记谱音仍跨过终点，生成时会明确提示；不会自动截断长音来凑齐。')
        result['allow_partial_measures'] = True

    elif kind == 'first_bar':
        at = _time(op.get('seconds'), old)
        beat = old.to_beat(at)
        result['time_signatures'] = [r for r in result['time_signatures']
                                     if r.get('origin') != 'editor_first_bar']
        meter = _meter_at(result, beat)
        size = meter['numerator'] * 4 / meter['denominator']
        mode = op.get('mode', 'intro')
        if mode not in ('intro', 'pickup'):
            raise ValueError('请选择完整引子或弱起')
        if mode == 'pickup' and (beat >= size - EPS or any(r['beat'] > 0 and r['beat'] < beat for r in result['time_signatures'])):
            raise ValueError('弱起必须短于一个小节且不跨变拍；完整引子请使用引子模式')
        result['pickup_beats'] = beat if mode == 'pickup' else 0.
        result['first_full_bar_beat'] = beat
        result['grid_origin'] = beat
        result['opening_mode'] = mode
        result['intro_downbeat_seconds'] = at
        if mode == 'intro' and beat > EPS:
            # A subsequent opening correction replaces only our own phase
            # marker, never a user/authored meter change at that position.
            if not any(abs(r['beat']-beat)<EPS for r in result['time_signatures']):
                result['time_signatures'] = _rows(result['time_signatures'] +
                    [dict(meter, beat=beat, origin='editor_first_bar')])
        result['allow_partial_measures'] = True
        summary = '首个完整小节已指定；前段保留为' + ('弱起' if mode == 'pickup' else '引子，允许不完整边界小节')
    else:
        raise ValueError('未知速度编辑操作')

    if kind in ('meter','meter_event','add_meter_event','remove_meter_event'):
        from .rebar_edit import _geometry
        try:
            _geometry(result)
        except ValueError as exc:
            raise ValueError('只改拍号无法在当前拍点上组成完整小节；请选择小节线，或使用“从这里重新定拍”明确小节数。'+str(exc)) from None
        _meter_edit_constraints(data,result,{**op,'type':kind})
    clock = TempoMap(result)
    result['source'] = 'manual_editor'
    result['meter_policy'] = {'mode': 'manual_editor', 'protected': True}
    result['last_tempo_operation'] = deepcopy(op)
    return {'tempo': result, 'fixed_anchors': pins, 'summary': summary, 'warnings': warnings}
