"""Musical-intent edits, independent of whether the detected bar grid is correct.

Only the derived clock/grouping changes. Source events are not inputs here.
"""
from copy import deepcopy
from .timing import TempoMap
from math import isfinite
from uuid import uuid4

EPS = 1e-8


def _number(value, name):
    if isinstance(value, bool):
        raise ValueError(f'{name}必须为有限数值')
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f'{name}必须为有限数值') from None
    if not isfinite(number):
        raise ValueError(f'{name}必须为有限数值')
    return number


def _time(value, clock):
    value = _number(value, '时间')
    if not 0 <= value <= clock.seconds[-1] + EPS:
        raise ValueError('时间超出音频范围')
    return min(value, clock.seconds[-1])


def _range(op, clock):
    start = _time(op.get('start', 0), clock)
    end = _time(op.get('end', clock.seconds[-1]), clock)
    if end - start <= EPS:
        raise ValueError('范围右端必须晚于左端')
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
    # Compatibility projection: meter events retain their physical location when
    # explicitly reinterpreting beat counts. No second musical clock is stored.
    data['time_signatures'] = [dict(row, beat=new.to_beat(old.to_seconds(row['beat'])))
                               for row in data['time_signatures']]
    for key in ('grid_origin', 'first_full_bar_beat'):
        if key in data:
            data[key] = new.to_beat(old.to_seconds(data[key]))
    if data.get('pickup_beats'):
        full = new.to_beat(old.to_seconds(data['pickup_beats']))
        initial = data['time_signatures'][0]
        data['pickup_beats'] = full % (initial['numerator'] * 4 / initial['denominator'])
        data.setdefault('first_full_bar_beat', full)


def _display_bars(data):
    # Engraving end is not the editable performance extent. Derive the ruler
    # without changing or discarding that legacy, independently stored boundary.
    from .duet_notation import reference_bars
    plain = deepcopy(data)
    plain.pop('score_end', None)
    clock = TempoMap(plain)
    return clock, reference_bars(clock, clock.beats[-1])


def _meter(op):
    n,d=op.get('numerator'),op.get('denominator',4)
    if type(n) is not int or not 1<=n<=32 or type(d) is not int or d not in (1,2,4,8,16,32,64,128):
        raise ValueError('请输入有效拍号，例如 3/4 或 6/8')
    return n,d


def _partial_boundaries(data):
    """Record exact user-created short measures, not a global validation bypass."""
    clock,(starts,lengths,meters)=_display_bars(data)
    marked=[]
    for q,length,meter in zip(starts,lengths,meters):
        n,d=map(int,meter.split('/'))
        if abs(length-n*4/d)>EPS:
            marked.append({'start_seconds':clock.to_seconds(q),'end_seconds':clock.to_seconds(q+length)})
    for annotation in data.get('short_bar_annotations',[]):
        if annotation not in marked:marked.append(deepcopy(annotation))
    data['explicit_partial_measures']=marked
    return marked


def apply_intent(data, operation, fixed_anchors=()):
    op=deepcopy(operation);kind=op.get('action')
    old=TempoMap(deepcopy(data));result=deepcopy(data);pins=[]
    warnings=[];impact={'source_seconds_unchanged':True}
    if kind in ('detect_trends','apply_trend'):
        from .rhythm_trends import detect_tempo_trends
        from .rhythm_pulses import pulse_state
        report=detect_tempo_trends(pulse_state(data))
        if kind=='detect_trends':
            return dict(tempo=deepcopy(data),fixed_anchors=pins,summary='Tempo-expression candidates only; source timing unchanged',warnings=report['warnings'],impact=dict(impact,tempo_changed=False,**{k:report[k] for k in ('candidates','suspicious_intervals')}))
        candidate=next((c for c in report['candidates'] if abs(c['start_seconds']-op.get('start',-1))<EPS and abs(c['end_seconds']-op.get('end',-1))<EPS and c['direction']==op.get('direction')),None)
        if candidate is None:raise ValueError('Trend candidate changed; preview again')
        annotations=result.setdefault('tempo_expression_annotations',[])
        annotations[:]=[a for a in annotations if not(abs(a['start_seconds']-candidate['start_seconds'])<EPS and abs(a['end_seconds']-candidate['end_seconds'])<EPS)]
        annotations.append(dict(candidate,source='user_confirmed_detection'))
        # Reuse the normal single revision/undo path without touching the clock.
        kind='trend_annotation_saved'
    if kind=='delete_beat':op.update(action='delete_beats',seconds=[op.get('seconds')]);kind='delete_beats'
    if kind in ('add_beat','delete_beats','divide_beats','clear_beats','set_meter','move_meter','set_beat_duration','meter_bars'):
        from .rhythm_pulses import apply_pulses
        result,summary,impact=apply_pulses(data,op,pins)
    elif kind=='trend_annotation_saved':
        summary='Tempo expression annotation saved; performance and tempo map unchanged'
        impact.update(tempo_changed=False)
    elif kind=='set_free_meter':
        from .rhythm_pulses import pulse_state
        result=pulse_state(data);start=_time(op.get('seconds',op.get('start')),old)
        point=next((p for p in result['pulse_points'] if abs(p['seconds']-start)<EPS),None)
        if start>=old.seconds[-1]-EPS:raise ValueError('Free measure must start before the project end')
        q=old.to_beat(start);rows=result['time_signatures']
        existing=next((r for r in rows if abs(r['beat']-q)<EPS),None)
        if point is None and start>EPS and not (existing or {}).get('smart_measure'):
            raise ValueError('Select an existing pulse for the free measure start')
        row=dict(existing or _meter_at(result,q),beat=q,free=True,origin='editor_intent')
        row.pop('pulse_id',None);row.pop('smart_measure',None)
        if point:row['pulse_id']=point['id']
        result['time_signatures']=_rows([r for r in rows if abs(r['beat']-q)>EPS]+[row])
        end=next((old.to_seconds(r['beat']) for r in result['time_signatures'] if r['beat']>q+EPS),old.seconds[-1])
        # The new event replaces old fixed-span exceptions only in its own area.
        result['short_bar_annotations']=[a for a in result.get('short_bar_annotations',[]) if a['end_seconds']<=start+EPS or a['start_seconds']>=end-EPS]
        summary='Free measure until the next meter event; performance and tempo unchanged'
        impact.update(tempo_changed=False,start_seconds=start,end_seconds=end)
    elif kind=='mark_short_bar':
        start=_time(op.get('start'),old);end=_time(op.get('end'),old)
        if end<=start:raise ValueError('Short measure requires two ordered boundaries')
        marks=result.setdefault('short_bar_annotations',[])
        if not op.get('remove',False):
            q0,q1=old.to_beat(start),old.to_beat(end)
            meter=next(m for m in reversed(data['time_signatures']) if m['beat']<=q0+EPS)
            if q1-q0>=meter['numerator']*4/meter['denominator']-EPS:raise ValueError('Short measure must be shorter than one local full measure')
            if any(q0+EPS<m['beat']<q1-EPS for m in data['time_signatures']):raise ValueError('Short measure cannot cross an internal meter change')
            if any(max(start,m['start_seconds'])<min(end,m['end_seconds'])-EPS and not(abs(m['start_seconds']-start)<EPS and abs(m['end_seconds']-end)<EPS) for m in marks):raise ValueError('Short measure annotations cannot overlap')
        marks[:]=[m for m in marks if not(abs(m['start_seconds']-start)<EPS and abs(m['end_seconds']-end)<EPS)]
        if not op.get('remove',False):marks.append(dict(start_seconds=start,end_seconds=end,source='user'))
        summary='Short measure annotation saved; notes and durations unchanged'
        impact.update(tempo_changed=False,short_bar_count=len(marks))
    elif kind=='set_opening':
        from .rhythm_opening import apply_opening
        changed=apply_opening(data,op);result=changed['tempo'];summary=changed['summary'];warnings.extend(changed['warnings'])
        impact.update(tempo_changed=False)
    elif kind=='restore_initial':
        initial=data.get('rhythm_initial') or {}
        if not initial.get('available') or not isinstance(initial.get('tempo'),dict):raise ValueError('此工程没有可恢复的初稿；当前用户编辑未改变')
        result=deepcopy(initial['tempo']);result['rhythm_initial']=deepcopy(initial)
        summary='已恢复保存的初稿结构；原始音符、CC 和音频秒位置不变'
        impact.update(tempo_changed=True)
    elif kind == 'move_beats':
        result,impact,summary=_move_pulses(data,op)
    elif kind == 'move_beat':
        result,impact,summary=_edit_pulse(data,op)
    elif kind in ('fix_position','unfix_position'):
        raise ValueError('内部固定点功能已移除；拍点可直接移动。')
    elif kind == 'delete_meter':
        at=_time(op.get('seconds'),old);q=old.to_beat(at)
        index=next((i for i,r in enumerate(result['time_signatures']) if abs(r['beat']-q)<EPS),None)
        if index is None:raise ValueError('此处没有拍号，请重新选择')
        if index==0:raise ValueError('开头拍号不能删除，可双击修改')
        result['time_signatures'].pop(index)
        from .rhythm_pulses import _reconcile_smart_measures
        _reconcile_smart_measures(result)
        summary='已删除拍号标记，沿用前一个拍号；拍点及演奏时间不变'
        impact.update(tempo_changed=False,start_seconds=at)
    elif kind == 'move_meter':
        at = _time(op.get('seconds'), old)
        target = _time(op.get('to_seconds'), old)
        q = old.to_beat(at)
        index = next((i for i,r in enumerate(result['time_signatures'])
                      if abs(r['beat']-q) < EPS), None)
        if index is None:
            raise ValueError('原位置没有拍号事件，请重新选择现有拍号')
        if index == 0:
            raise ValueError('初始拍号固定在开头，可以双击修改，不能移动')
        rows = result['time_signatures']
        target_q = old.to_beat(target)
        following = rows[index+1]['beat'] if index+1 < len(rows) else old.beats[-1]
        if not rows[index-1]['beat'] + EPS < target_q < following - EPS:
            raise ValueError('拍号不能越过相邻拍号或音频边界；没有覆盖任何事件')
        rows[index]['beat'] = target_q
        impact.update(start_seconds=min(at,target),end_seconds=max(at,target),tempo_changed=False)
        summary = '已移动拍号事件；节拍映射和原始演奏时间不变' 
    elif kind=='meter':
        n,d=_meter(op);start=_time(op.get('seconds',op.get('start',0)),old);left=old.to_beat(start)
        if 'end' in op and op['end'] is not None:
            end=_time(op['end'],old)
            if end-start<=EPS:raise ValueError('选区右端必须晚于左端')
        else:
            end=next((old.to_seconds(r['beat']) for r in data['time_signatures'] if r['beat']>left+EPS),old.seconds[-1])
        if end-start<=EPS:
            raise ValueError('拍号必须位于音频结束之前')
        right=old.to_beat(end)
        existing=next((r for r in result['time_signatures'] if abs(r['beat']-left)<EPS),{})
        rows=[r for r in result['time_signatures'] if r['beat']<left-EPS or r['beat']>=right-EPS]
        rows.append(dict(existing,beat=left,numerator=n,denominator=d,origin='editor_intent',free=False))
        if end<old.seconds[-1]-EPS:
            rows.append(dict(_meter_at(data,right),beat=right))
            # If the selected endpoint cuts an old bar, recover its old phase
            # at the next original boundary, rather than rephase the whole tail.
            _,(starts,_,_)=_display_bars(data)
            recovery=next((q for q in starts if q>right+EPS),None)
            if recovery is not None and not any(abs(q-right)<EPS for q in starts) and not any(right+EPS<r['beat']<=recovery+EPS for r in rows):
                rows.append(dict(_meter_at(data,recovery),beat=recovery,origin='editor_intent_restore'))
        result['time_signatures']=_rows(rows)
        # A newly smaller initial meter does not delete or shorten an intro.
        if start==0 and result.get('pickup_beats',0)>=n*4/d:
            result.setdefault('first_full_bar_beat',result['pickup_beats'])
            result['pickup_beats']=0.
        impact.update(start_seconds=start,end_seconds=end,tempo_changed=False)
        summary=f'{start:g}–{end:g} 秒设为 {n}/{d}；只改分组，速度和演奏不变'
    elif kind in ('recount','interpret'):
        start,end=_range(op,old);left,right=old.to_beat(start),old.to_beat(end)
        if kind=='recount':
            n,d=_meter(op);bars=op.get('bar_count')
            if type(bars) is not int or not 1<=bars<=4096:raise ValueError('小节数应为 1–4096 的整数')
            target=bars*n*4/d
        else:
            factor=_number(op.get('factor'),'拍值比例')
            if factor not in (.5,2.):raise ValueError('拍值比例应为 0.5 或 2')
            target=(right-left)*factor
        delta=target-(right-left);factor=target/(right-left)
        distribution=op.get('distribution','preserve')
        if distribution not in ('preserve','uniform'):raise ValueError('请选择保留变速或均匀分布')
        samples=sorted(set(old.seconds+[start,end]))
        def mapped(t):
            q=old.to_beat(t)
            if t<=start:return q
            if t>=end:return q+delta
            return left+(t-start)*target/(end-start) if distribution=='uniform' else left+(q-left)*factor
        result['anchors']=[dict(seconds=t,beat=mapped(t)) for t in samples]
        new=TempoMap(dict(result,pickup_beats=0));_remap_structure(result,old,new)
        if kind=='recount':
            restore=dict(_meter_at(data,right),beat=left+target,origin='editor_intent_restore')
            rows=[r for r in result['time_signatures'] if r['beat']<left-EPS or r['beat']>left+target+EPS]
            rows.append(dict(beat=left,numerator=n,denominator=d,origin='editor_intent'))
            if end<old.seconds[-1]-EPS:rows.append(restore)
            result['time_signatures']=_rows(rows)
            if start==0 and result.get('pickup_beats',0)>=n*4/d:
                result.setdefault('first_full_bar_beat',result['pickup_beats']);result['pickup_beats']=0.
            summary=f'固定 {start:g}–{end:g} 秒为 {bars} 小节 {n}/{d}；原演奏不变'
        else:summary=f'{start:g}–{end:g} 秒拍值乘 {factor:g}；拍号与演奏耗时不变'
        impact.update(start_seconds=start,end_seconds=end,tempo_changed=True,quarters=target,following_quarter_shift=delta)
    else:
        raise ValueError('未知音乐结构操作')
    if 'rhythm_initial' not in result:
        baseline=deepcopy(data);baseline.pop('rhythm_initial',None)
        result['rhythm_initial']={'available':True,'source':'before_first_rhythm_edit','tempo':baseline}
    if 'pulse_points' in result and kind in ('interpret','recount'):
        mapped=TempoMap(result)
        for p in result['pulse_points']:p['beat']=mapped.to_beat(p['seconds'])
        for a,b in zip(result['pulse_points'],result['pulse_points'][1:]):a['step_quarters']=b['beat']-a['beat']
    from .free_meter import free_ranges
    free_spans=free_ranges(result)
    partials=[p for p in _partial_boundaries(result) if not any(a['start_seconds']-EPS<=p['start_seconds'] and p['end_seconds']<=a['end_seconds']+EPS for a in free_spans)]
    if any(p['end_seconds']<TempoMap(result).seconds[-1]-EPS and not any(abs(a['start_seconds']-p['start_seconds'])<EPS and abs(a['end_seconds']-p['end_seconds'])<EPS for a in result.get('short_bar_annotations',[])) for p in partials):warnings.append('边界处保留不完整小节，不会拉伸音符来凑拍；如需完整小节，请使用“选区设为 N 小节”。')
    clock=TempoMap(result)
    result['rhythm_operation_version']=1
    result['source']='manual_editor';result['meter_policy']={'mode':'manual_editor','protected':True}
    result['last_tempo_operation']=op
    return dict(tempo=result,fixed_anchors=pins,summary=summary,warnings=warnings,impact=impact)

def _pulse_knots(data):
    """Visible musical beat positions; distinct from detector tempo samples."""
    if 'pulse_points' in data:
        clock=TempoMap(deepcopy(data));points={(p['seconds'],p['beat']) for p in data['pulse_points']}
        points.update(((0.,0.),(clock.seconds[-1],clock.beats[-1])))
        return clock,sorted(points)
    clock,(starts,lengths,meters)=_display_bars(data)
    points={0.,clock.beats[-1]}
    for q,length,meter in zip(starts,lengths,meters):
        unit=4/int(meter.split('/')[1]);k=0
        while q+k*unit<min(q+length,clock.beats[-1])-EPS:
            points.add(q+k*unit);k+=1
        if q+length<=clock.beats[-1]+EPS:points.add(min(q+length,clock.beats[-1]))
    return clock,[(clock.to_seconds(q),q) for q in sorted(points)]


def _edit_pulse(data,op):
    clock,points=_pulse_knots(data);kind=op['action'];at=_time(op.get('seconds'),clock)
    if not EPS<at<clock.seconds[-1]-EPS:raise ValueError('不能增删或移动曲目首尾边界')
    chosen=next((p for p in points if abs(p[0]-at)<EPS),None)
    if kind!='add_beat' and chosen is None:raise ValueError('请选择当前网格的一面拍旗，不是速度曲线的采样点')
    if kind=='add_beat' and chosen is not None:raise ValueError('此处已有拍点，可拖动它校准位置')
    left=max((p for p in points if p[0]<at-EPS),key=lambda p:p[0])
    right=min((p for p in points if p[0]>at+EPS),key=lambda p:p[0])
    a,qa=left;b,qb=right;source_q=clock.to_beat(at)
    if kind=='move_beat':
        target=_time(op.get('to_seconds'),clock)
        if op.get('scope') is not None:
            scope = op['scope']
            if not isinstance(scope, dict):
                raise ValueError('编辑范围无效，请重新框选')
            start, end = _range(scope, clock)
            if not start+EPS < at < end-EPS:
                raise ValueError('只能校准选区内拍点；选区两端作为临时固定位置')
            a,b = max(a,start),min(b,end)
            qa,qb = clock.to_beat(a),clock.to_beat(b)
        if not a+EPS<target<b-EPS:raise ValueError('目标越过相邻拍点或固定点；补拍/删拍请用对应工具')
        def warp(t):
            if t<=a or t>=b:return t
            if t<=at:return a+(t-a)*(target-a)/(at-a)
            return target+(t-at)*(b-target)/(b-at)
        samples=sorted(set(clock.seconds+[a,at,b]))
        out=deepcopy(data);out['anchors']=[dict(seconds=warp(t),beat=clock.to_beat(t)) for t in samples]
        for p in out.get('pulse_points',[]):p['seconds']=warp(p['seconds'])
        if 'intro_downbeat_seconds' in out and 'first_full_bar_beat' in out:
            out['intro_downbeat_seconds']=TempoMap(out).to_seconds(out['first_full_bar_beat'])
            if out.get('opening_mode')=='normal' and 'score_start_seconds' in out:out['score_start_seconds']=out['intro_downbeat_seconds']
        return out,dict(start_seconds=a,end_seconds=b,tempo_changed=True,source_seconds_unchanged=True),'拍点已校准；相邻拍点与固定点保持，音符和音频不动'


def _move_pulses(data, op):
    """One atomic translation of selected pulse identities in audio seconds.

    Every unselected pulse and pin partitions the warp. Dense tempo samples
    interpolate through that simultaneous mapping, never through N sequential edits.
    """
    from bisect import bisect_right
    from .rhythm_pulses import pulse_state
    out = pulse_state(data)
    clock = TempoMap(out)
    values = op.get('seconds')
    if not isinstance(values, list) or not values:
        raise ValueError('请选择要移动的拍点')
    selected = sorted(set(_time(t, clock) for t in values))
    delta = _number(op.get('delta_seconds'), '移动距离')
    points = out['pulse_points']
    chosen = []
    for t in selected:
        point = next((p for p in points if abs(p['seconds']-t)<EPS), None)
        if point is None:raise ValueError('所选拍点已改变，请重新选择')
        if not EPS<t<clock.seconds[-1]-EPS:raise ValueError('曲目首尾边界不能整组移动；请取消选择边界')
        chosen.append(point)
    chosen_ids = {p['id'] for p in chosen}
    knots = {0.:0., clock.seconds[-1]:clock.seconds[-1]}
    source = sorted(knots); destination = [knots[t] for t in source]
    if any(b<=a+EPS for a,b in zip(destination,destination[1:])):
        raise ValueError('整组移动不能越过相邻未选拍点、固定点或曲目边界')
    def warp(t):
        if t<=source[0]:return destination[0]
        if t>=source[-1]:return destination[-1]
        i=bisect_right(source,t)-1
        return destination[i]+(t-source[i])*(destination[i+1]-destination[i])/(source[i+1]-source[i])
    samples=sorted(set(clock.seconds+source))
    out['anchors']=[dict(seconds=warp(t),beat=clock.to_beat(t)) for t in samples]
    for p in points:
        if p['id'] in chosen_ids:p['seconds']+=delta
    if 'intro_downbeat_seconds' in out and 'first_full_bar_beat' in out:
        out['intro_downbeat_seconds']=TempoMap(out).to_seconds(out['first_full_bar_beat'])
        if out.get('opening_mode')=='normal' and 'score_start_seconds' in out:out['score_start_seconds']=out['intro_downbeat_seconds']
    return out, dict(tempo_changed=True,source_seconds_unchanged=True,moved_count=len(chosen),delta_seconds=delta), f'已整体移动 {len(chosen)} 个拍点；拍号随所属拍点移动，音符和音频不动'
