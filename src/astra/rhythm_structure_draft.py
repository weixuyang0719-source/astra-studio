"""Detached structural editing transaction. No project IO or implicit reflow.

Coordinates are source seconds; incoming pulse units are quarter-note units.
An invalid draft is useful data, never an effective TempoMap. Caller owns CAS.
"""
from bisect import bisect_right
from copy import deepcopy
import hashlib
import json
import math

from .timing import TempoMap

VERSION = 1
EPS = 1e-7


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _num(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError('位置和计数必须是有限数值')
    return float(value)


def _meter(rows, seconds):
    return rows[max(0, bisect_right([r['seconds'] for r in rows], seconds+EPS)-1)]


def _at(rows, seconds):
    return next((r for r in rows if abs(r['seconds']-seconds)<EPS), None)


def _derived_flags(draft, clock):
    """Denominator changes derive selectable subdivisions, never recount q."""
    flags=[]
    for a,b in zip(draft['bars'],draft['bars'][1:]):
        start,end=clock.to_beat(a['seconds']),clock.to_beat(b['seconds'])
        step=4/_meter(draft['labels'],a['seconds'])['denominator'];k=0
        while start+k*step<end-EPS:
            q=start+k*step;t=clock.to_seconds(q)
            flags.append(dict(seconds=t,beat=q,denominator=int(4/step),derived=_at(draft['pulses'],t) is None))
            k+=1
    return flags


def create_draft(tempo, revision, *, scope=None, fixed_anchors=(), preserve_unedited_positions=True):
    """Start from a valid committed map; scope boundaries remain in source time."""
    from .rebar_edit import _geometry
    if not isinstance(revision, str) or not revision:
        raise ValueError('缺少来源编辑版本')
    clock, starts, lengths, _, endq = _geometry(deepcopy(tempo))
    end = clock.to_seconds(endq)
    if scope is not None:
        scope = dict(start=_num(scope['start']), end=_num(scope['end']))
        if not 0 <= scope['start'] < scope['end'] <= end+EPS:
            raise ValueError('结构编辑选区必须位于谱面范围内')
    labels = [dict(id=f'm{i}', seconds=clock.to_seconds(r['beat']),
                   numerator=r['numerator'], denominator=r['denominator'])
              for i, r in enumerate(tempo['time_signatures'])]
    first = float(tempo.get('first_full_bar_beat', tempo.get('pickup_beats', 0)))
    qs = {0., clock.beats[-1], endq}
    bars = []
    for q, length in zip(starts, lengths):
        if q >= endq-EPS:
            continue
        finish = min(q+length, endq)
        meter = _meter(labels, clock.to_seconds(q))
        unit = 4/meter['denominator']
        qs.add(q); qs.add(finish)
        k = 1
        while q+k*unit < finish-EPS:
            qs.add(q+k*unit); k += 1
        bars.append(dict(id=f'b{len(bars)}', seconds=clock.to_seconds(q),
                         exception=('opening' if q < first-EPS and abs(finish-first)<EPS else
                                    'tail' if abs(finish-endq)<EPS and abs(finish-q-meter['numerator']*unit)>EPS else None)))
    bars.append(dict(id=f'b{len(bars)}', seconds=end, exception='end'))
    qs = sorted(qs)
    pulses = [dict(id=f'p{i}', seconds=clock.to_seconds(q), units=q-qs[i-1] if i else 0.)
              for i, q in enumerate(qs)]
    draft = dict(version=VERSION, source_revision=revision, source_sha256=_hash(tempo),
                 source=deepcopy(tempo), scope=scope, preserve_unedited_positions=bool(preserve_unedited_positions),
                 pulses=pulses, bars=bars, labels=labels, fixed_anchors=[],
                 operations=[], changed_ids=[])
    draft['baseline'] = deepcopy({k:draft[k] for k in ('pulses','bars','labels')})
    return inspect_draft(draft)


def _candidate(draft):
    source = TempoMap(deepcopy(draft['source']))
    pulses = draft['pulses']
    seconds = [p['seconds'] for p in pulses]
    beats = [0.]
    for pulse in pulses[1:]:
        beats.append(beats[-1]+pulse['units'])
    if seconds[0] != 0 or any(b-a<=EPS for a,b in zip(seconds,seconds[1:])) or any(b-a<=EPS for a,b in zip(beats,beats[1:])):
        raise ValueError('拍旗时间必须严格递增，每拍计数必须为正')
    old_q = [source.to_beat(t) for t in seconds]
    def mapped(t):
        return TempoMap._interpolate(source.to_beat(t), old_q, beats)
    # Keep original tempo curvature between surviving physical flags, and all
    # explicitly fixed seconds, rather than replacing real rallentando by lines.
    support = sorted(set(seconds+source.seconds))
    result = deepcopy(draft['source'])
    result['anchors'] = [dict(seconds=t, beat=mapped(t)) for t in support]
    result['time_signatures'] = [dict(beat=mapped(m['seconds']), numerator=m['numerator'], denominator=m['denominator'],
                                    origin='editor_structure_draft') for m in draft['labels']]
    for key in ('pickup_beats','grid_origin','first_full_bar_beat'):
        if key in result:
            seconds=draft.get('metadata_seconds',{}).get(key,source.to_seconds(float(result[key])))
            result[key] = mapped(seconds)
    if 'intro_downbeat_seconds' in result and 'first_full_bar_beat' in draft.get('metadata_seconds',{}):
        result['intro_downbeat_seconds']=draft['metadata_seconds']['first_full_bar_beat']
    result['source'] = 'manual_editor'
    result['meter_policy'] = {'mode':'manual_editor','protected':True}
    return result


def inspect_draft(draft):
    """Return a copy with concrete conflicts. Never attach an invalid map."""
    out = deepcopy(draft)
    out['fixed_anchors']=[]
    for key in ('tempo','display','guidance','impact','summary'):
        out.pop(key, None)
    conflicts = []
    def issue(code, message, seconds=None, **details):
        conflicts.append(dict(code=code, message=message, seconds=seconds, **details))
    if _hash(out['source']) != out['source_sha256']:
        issue('source_changed','草稿来源已变化；请重新载入或合并，不可覆盖当前项目')
    try:
        candidate = _candidate(out)
        clock = TempoMap(candidate)
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        issue('invalid_map', str(exc), suggestion='修正拍旗顺序、正拍数或起始拍号后重试')
        out.update(valid=False, conflicts=conflicts, guidance=deepcopy(conflicts),
                   summary='结构草稿保留了修改，但当前拍点映射不合法',
                   display={**{k:deepcopy(out[k]) for k in ('bars','pulses','labels')},'beat_flags':[]},
                   impact={'scope':deepcopy(out['scope']),'explicit_operation_count':len(out['operations'])})
        return out
    bars, labels = out['bars'], out['labels']
    if any(b['seconds']-a['seconds']<=EPS for a,b in zip(bars,bars[1:])):
        issue('boundary_order','小节线物理时间必须严格递增；这不是小节编号顺序限制')
    for left, right in zip(bars,bars[1:]):
        a,b = left['seconds'],right['seconds']
        meter = _meter(labels,a)
        units = clock.to_beat(b)-clock.to_beat(a)
        count = units*meter['denominator']/4
        original = next((r for r in out['baseline']['bars'] if r['id']==left['id']),None)
        original_right = next((r for r in out['baseline']['bars'] if r['id']==right['id']),None)
        exception = left.get('exception') in ('opening','tail') and original is not None and original_right is not None
        if exception:
            old_clock = TempoMap(deepcopy(out['source']))
            prior = old_clock.to_beat(original_right['seconds'])-old_clock.to_beat(original['seconds'])
            if abs(prior-units)>EPS:
                issue('protected_short_bar','既有弱起或曲尾短节计数需保留；请使用明确的弱起／曲尾编辑入口',a,
                      end_seconds=b, suggestions=['恢复该短节原拍数；普通段的补删拍可继续保留'])
        if abs(count-meter['numerator'])>EPS and not exception:
            suggestions = ['补回或删除拍旗，或明确修改小节线与拍号']
            proposed = None
            if abs(count-round(count))<EPS and 1<=round(count)<=32:
                proposed = dict(type='set_meter', seconds=a, numerator=round(count), denominator=meter['denominator'])
                suggestions.insert(0,f'将本小节设为 {round(count)}/{meter["denominator"]}，在下一小节恢复原拍号')
            if out['preserve_unedited_positions']:
                suggestions.append('如要重新分组现有拍旗，可关闭保留未编辑位置，再框选完整小节执行按当前拍号重分组')
            issue('bar_count',f'{a:g}–{b:g} 秒有 {count:g} 个 {meter["denominator"]} 分音符单位，但标签为 {meter["numerator"]}/{meter["denominator"]}',
                  a, end_seconds=b, actual_count=count, denominator=meter['denominator'], suggestions=suggestions, proposed_operation=proposed)
    for label in labels:
        if not _at(bars,label['seconds']):
            issue('label_boundary','拍号标签必须在所选拍位建立小节线；不会移回旧小节开头',label['seconds'])
    scope = out['scope']
    baseline = out['baseline']
    for category in ('pulses','bars','labels'):
        now = {r['id']:r for r in out[category]}
        for old in baseline[category]:
            seconds = old['seconds']
            external = scope is not None and (seconds < scope['start']-EPS or seconds > scope['end']+EPS)
            protected = external or (out['preserve_unedited_positions'] and old['id'] not in out['changed_ids'])
            if protected and (old['id'] not in now or abs(now[old['id']]['seconds']-seconds)>EPS):
                issue('protected_position','未编辑的拍旗或小节线位置需要保留；请明确编辑该位置或调整范围',seconds)
    original_clock = TempoMap(deepcopy(out['source']))
    if scope:
        # Check exterior meter at every label/bar transition, not just labels
        # whose physical positions survived. No automatic restore is invented.
        probes = sorted({r['seconds'] for rows in (baseline['bars'],bars,baseline['labels'],labels) for r in rows} | {scope['end']})
        for seconds in probes:
            if seconds < scope['start']-EPS or seconds >= scope['end']-EPS:
                old,new = _meter(baseline['labels'],seconds),_meter(labels,seconds)
                if (old['numerator'],old['denominator']) != (new['numerator'],new['denominator']):
                    issue('scope_meter','拍号变更延伸到选区外；请在选区末端恢复原拍号',seconds)
                    break
        supports = sorted(set(original_clock.seconds+clock.seconds+[scope['start'],scope['end']]))
        before = [t for t in supports if t<=scope['start']+EPS]
        after = [t for t in supports if t>=scope['end']-EPS]
        for region in (before,after):
            deltas=[clock.to_beat(t)-original_clock.to_beat(t) for t in region]
            if deltas and max(deltas)-min(deltas)>EPS:
                issue('scope_mapping','拍数变更会拉伸选区外的拍间映射；请将选区边缘落在保留拍旗上或扩大显式选区',
                      region[0],suggestions=['框选包含该拍间隔的完整范围，或恢复边缘附近的拍旗'])
                break
    def identity(rows, meter_rows, clock, seconds):
        bar = rows[max(0,bisect_right([r['seconds'] for r in rows],seconds+EPS)-1)]
        return bar['seconds'], (clock.to_beat(seconds)-clock.to_beat(bar['seconds']))*_meter(meter_rows,seconds)['denominator']/4
    if not conflicts:
        # The effective map must reproduce the draft's actual boundaries. An
        # internally consistent display alone is not a commit certificate.
        from .rebar_edit import _geometry
        try:
            verified, starts, _, _, endq = _geometry(candidate)
            physical = [verified.to_seconds(q) for q in starts if q<endq-EPS]+[verified.to_seconds(endq)]
            expected = [row['seconds'] for row in bars]
            if len(physical)!=len(expected) or any(abs(a-b)>EPS for a,b in zip(physical,expected)):
                issue('boundary_roundtrip','生效图无法重现草稿的小节边界；请检查弱起、曲尾或拍号标签',
                      suggestions=['保留完整结构草稿并修复边界；不会只保存一部分修改'])
        except ValueError as exc:
            issue('boundary_roundtrip',str(exc))
    out.update(valid=not conflicts, conflicts=conflicts)
    out['guidance'] = [dict(code=c['code'],seconds=c.get('seconds'),message=c['message'],
                            suggestions=c.get('suggestions',[]),proposed_operation=c.get('proposed_operation')) for c in conflicts]
    first=clock.to_seconds(float(candidate.get('first_full_bar_beat',candidate.get('pickup_beats',0))))
    first_index=next((i for i,row in enumerate(bars) if abs(row['seconds']-first)<EPS),0)
    out['display'] = dict(bars=[dict(row,bar_number=i-first_index+1) for i,row in enumerate(bars)],
                          pulses=deepcopy(out['pulses']),labels=deepcopy(labels),
                          beat_flags=_derived_flags(out,clock),
                          unit_explanation='拍旗计数使用拍号分母单位；内部四分音符单位保持不变，6/8 每八分音符为 0.5。')
    out['impact'] = dict(scope=deepcopy(scope),following_bar_number_shift=len(bars)-len(out['baseline']['bars']),
                         source_seconds_unchanged=True,explicit_operation_count=len(out['operations']))
    out['summary'] = '结构草稿已闭合，可一次保存' if not conflicts else f'结构草稿保留了修改，尚有 {len(conflicts)} 处冲突待解决'
    if not conflicts:
        out['tempo']=candidate
    return out


def _regroup_bars(out, op):
    if out['preserve_unedited_positions']:
        raise ValueError('按当前拍号重分组会改变未编辑的小节线；请先关闭“保留未编辑位置”')
    start,end=_num(op['start']),_num(op['end'])
    if end-start<=EPS or not _at(out['bars'],start) or not _at(out['bars'],end):
        raise ValueError('重分组需明确选择两个现有小节边界')
    scope=out['scope']
    if scope and (start<scope['start']-EPS or end>scope['end']+EPS):
        raise ValueError('重分组不能越过结构编辑选区')
    clock=TempoMap(_candidate(out))
    # Meter labels, fixed-flag bar identities and opening/ending exception edges
    # are partitions, never discarded to force an apparently complete result.
    protected={start,end}
    protected.update(r['seconds'] for r in out['labels'] if start<r['seconds']<end)
    for index,row in enumerate(out['bars'][:-1]):
        if row.get('exception') in ('opening','tail'):
            protected.update([row['seconds'],out['bars'][index+1]['seconds']])
    partitions=sorted(t for t in protected if start-EPS<=t<=end+EPS)
    desired=set(partitions)
    for left,right in zip(partitions,partitions[1:]):
        meter=_meter(out['labels'],left)
        step=meter['numerator']*4/meter['denominator']
        q,stop=clock.to_beat(left),clock.to_beat(right)
        k=1
        while q+k*step<stop-EPS:
            desired.add(clock.to_seconds(q+k*step));k+=1
    existing=out['bars']
    replacement=[row for row in existing if row['seconds']<start-EPS or row['seconds']>end+EPS]
    for seconds in sorted(desired):
        old=_at(existing,seconds)
        replacement.append(deepcopy(old) if old else dict(id='regroup'+_hash([out['source_sha256'],out['operations'],op,seconds])[:16],seconds=seconds,exception=None))
    for row in existing:
        if start<row['seconds']<end and not any(abs(t-row['seconds'])<EPS for t in desired):
            if row['id'] not in out['changed_ids']:out['changed_ids'].append(row['id'])
    out['bars']=replacement
    return out


def apply_edit(draft, operation):
    """Structural conflicts persist; malformed/out-of-scope commands raise.

    set_meter changes one physical bar (restores next). add_meter_label changes
    until next explicit label. Removing a label never silently removes a bar.
    """
    out=deepcopy(draft); out['fixed_anchors']=[]; op=deepcopy(operation); kind=op.get('type')
    if kind=='preserve_positions':
        if type(op.get('enabled')) is not bool: raise ValueError('保留位置开关必须为布尔值')
        out['preserve_unedited_positions']=op['enabled']
    elif kind=='regroup_bars':
        out=_regroup_bars(out,op)
    else:
        t=_num(op['seconds']); scope=out['scope']
        if t<0 or t>out['bars'][-1]['seconds']+EPS or scope and not scope['start']-EPS<=t<=scope['end']+EPS:
            raise ValueError('此位置不在结构编辑选区内')
        def touch(row):
            if row['id'] not in out['changed_ids']:out['changed_ids'].append(row['id'])
        def new_id(prefix):return prefix+_hash([out['source_sha256'],out['operations'],op])[:16]
        pulse=_at(out['pulses'],t); bar=_at(out['bars'],t)
        if not pulse and kind in ('set_meter','add_meter_label','delete_pulse','calibrate_pulse'):
            # After changing 4/4 to 8/8 the intervening eighth flags are real
            # musical positions. Materialize by splitting existing units, not
            # adding a count or shifting any later beat.
            clock=TempoMap(_candidate(out))
            if _at(_derived_flags(out,clock),t):
                index=bisect_right([r['seconds'] for r in out['pulses']],t)
                before,after=out['pulses'][index-1],out['pulses'][index]
                units=clock.to_beat(t)-clock.to_beat(before['seconds'])
                after['units']-=units
                pulse=dict(id=new_id('derived'),seconds=t,units=units)
                out['pulses'].insert(index,pulse)
        if kind=='add_pulse':
            if pulse:raise ValueError('此处已有拍旗；校准位置请用 calibrate_pulse')
            denominator=op.get('denominator',_meter(out['labels'],t)['denominator'])
            if type(denominator) is not int or denominator not in (1,2,4,8,16,32,64,128):raise ValueError('请选择明确计数单位，例如 6/8 的八分音符')
            out['pulses'].append(dict(id=new_id('p'),seconds=t,units=4/denominator))
        elif kind in ('delete_pulse','calibrate_pulse'):
            if not pulse:raise ValueError('没有选中现有拍旗')
            if t==0 or t==out['bars'][-1]['seconds']:raise ValueError('不能删除或移动首尾边界')
            if bar and kind=='delete_pulse':
                raise ValueError('该拍旗也是小节线；如需合并，请先删除同位拍号标签（若有），再删除此小节线，最后删拍；合并后的拍数仍需与拍号一致')
            if kind=='delete_pulse' and 'units' in op and abs(_num(op['units'])-pulse['units'])>EPS:
                raise ValueError(f'该拍旗当前实际计数为 {pulse["units"]:g} 个四分音符单位；请刷新选中位置，不会按新拍号偷偷重解释')
            touch(pulse)
            if kind=='delete_pulse':out['pulses'].remove(pulse)
            else:
                target=_num(op['to_seconds']);i=out['pulses'].index(pulse)
                if not out['pulses'][i-1]['seconds']+EPS<target<out['pulses'][i+1]['seconds']-EPS:raise ValueError('校准不能越过相邻拍旗')
                if scope and (not scope['start']+EPS<t<scope['end']-EPS or not scope['start']+EPS<target<scope['end']-EPS):raise ValueError('不能移动选区边界或移出编辑选区')
                if bar:
                    touch(bar);bar['seconds']=target
                    label=_at(out['labels'],t)
                    if label:touch(label);label['seconds']=target
                    source_clock=TempoMap(deepcopy(out['source']))
                    for key in ('pickup_beats','grid_origin','first_full_bar_beat'):
                        if key not in out['source']:continue
                        source_t=out.get('metadata_seconds',{}).get(key,source_clock.to_seconds(float(out['source'][key])))
                        if abs(source_t-t)<EPS:
                            out.setdefault('metadata_seconds',{})[key]=target
                pulse['seconds']=target
        elif kind in ('set_meter','add_meter_label'):
            n,d=op.get('numerator'),op.get('denominator',4)
            if type(n) is not int or not 1<=n<=32 or type(d) is not int or d not in (1,2,4,8,16,32,64,128):raise ValueError('拍号无效')
            if not pulse:raise ValueError('请将拍号放在明确拍旗上，或先补拍；不会自动吸附到旧小节线')
            if not bar:out['bars'].append(dict(id=new_id('b'),seconds=t,exception=None));out['bars'].sort(key=lambda r:r['seconds'])
            following=next((r for r in out['bars'] if r['seconds']>t+EPS),None)
            if kind=='set_meter' and following and not _at(out['labels'],following['seconds']):
                old=_meter(out['labels'],following['seconds'])
                out['labels'].append(dict(id=new_id('restore'),seconds=following['seconds'],numerator=old['numerator'],denominator=old['denominator']))
            label=_at(out['labels'],t)
            if label:touch(label);label.update(numerator=n,denominator=d)
            else:out['labels'].append(dict(id=new_id('m'),seconds=t,numerator=n,denominator=d))
        elif kind=='delete_meter_label':
            label=_at(out['labels'],t)
            if not label or t==0:raise ValueError('请选择非起始拍号标签')
            touch(label);out['labels'].remove(label)
        elif kind in ('add_bar','delete_bar'):
            if kind=='add_bar':
                if not pulse or bar:raise ValueError('请在尚无小节线的现有拍旗处添加小节线')
                out['bars'].append(dict(id=new_id('b'),seconds=t,exception=None))
            else:
                if not bar or bar.get('exception') or t==0:raise ValueError('首尾及弱起边界不能隐式删除')
                touch(bar);out['bars'].remove(bar)
        else:raise ValueError('未知结构草稿操作')
    for key in ('pulses','bars','labels'):out[key].sort(key=lambda r:r['seconds'])
    out['operations'].append(op)
    return inspect_draft(out)


def commit_draft(draft, *, expected_revision, expected_source_sha256):
    """Validate against caller's current identity; caller must CAS persistence."""
    if draft['source_revision']!=expected_revision or draft['source_sha256']!=expected_source_sha256:
        raise ValueError('结构草稿版本已过期；请合并当前编辑后再保存')
    checked=inspect_draft(draft)
    if not checked['valid']:
        raise ValueError('结构草稿仍有冲突：'+checked['conflicts'][0]['message'])
    clock=TempoMap(checked['tempo']);pins=deepcopy(checked['fixed_anchors'])
    return dict(tempo=checked['tempo'],fixed_anchors=pins,source_revision=checked['source_revision'],
                source_sha256=checked['source_sha256'],operations=checked['operations'],
                summary=checked['summary'],impact=checked['impact'])
