"""Stable, editable pulse identities over the one existing seconds/quarter clock.

Pulse ordinal is not a meter denominator. Changing a signature never changes the
clock. Count edits explicitly change the clock but never performance seconds.
"""
from copy import deepcopy
from hashlib import sha256
from uuid import uuid4
from bisect import bisect_left, bisect_right
import math
from .timing import TempoMap

EPS=1e-8


def pulse_state(data):
    out=deepcopy(data);clock=TempoMap(out)
    if 'pulse_points' not in out:
        projection=out.get('pulse_projection') or {}
        count=projection.get('pulse_count')
        scale=projection.get('post_projection_beat_scale',1.)
        unit=projection.get('effective_quarter_span_per_pulse',projection.get('quarter_span_per_pulse'))
        origin=projection.get('pulse_ordinal_quarter_origin')
        if isinstance(origin,(int,float)) and isinstance(scale,(int,float)):origin*=scale
        points={}
        if (type(count) is int and 0<count<=1000000
                and isinstance(unit,(int,float)) and math.isfinite(unit) and unit>0
                and isinstance(origin,(int,float)) and math.isfinite(origin) and origin>=0):
            # Detector ordinals, not meter-denominator subdivisions. Resolve
            # through the EFFECTIVE clock so manual warps remain authoritative.
            coordinates=[(origin+i*unit,unit) for i in range(count)]
            out['pulse_migration_source']='model_projection'
        else:
            from .duet_notation import reference_bars
            plain=deepcopy(out);plain.pop('score_end',None)
            starts,lengths,meters=reference_bars(TempoMap(plain),clock.beats[-1])
            coordinates=[]
            for start,length,meter in zip(starts,lengths,meters):
                unit=4/int(meter.split('/')[1]);k=0
                while start+k*unit<=min(start+length,clock.beats[-1])+EPS:
                    coordinates.append((start+k*unit,unit));k+=1
            out['pulse_migration_source']='legacy_meter_grid'
        for q,unit in coordinates:
            if q>clock.beats[-1]+EPS:continue
            t=clock.to_seconds(q)
            points[round(t,10)]={'id':'pulse-'+sha256(f'{t:.10f}/{q:.10f}'.encode()).hexdigest()[:20],
                'seconds':t,'beat':q,'step_quarters':unit}
        out['pulse_points']=sorted(points.values(),key=lambda p:p['seconds'])
        for a,b in zip(out['pulse_points'],out['pulse_points'][1:]):a['step_quarters']=b['beat']-a['beat']
    points=out['pulse_points']
    if len({p['id'] for p in points})!=len(points):raise ValueError('拍点身份重复，请先修复工程')
    for i,p in enumerate(points):
        if not math.isfinite(p['seconds']) or not 0<=p['seconds']<=clock.seconds[-1]+EPS:
            raise ValueError('拍点超出音频范围')
        if i and p['seconds']<=points[i-1]['seconds']+EPS:raise ValueError('拍点必须按实际秒位置严格递增')
        if abs(clock.to_beat(p['seconds'])-p['beat'])>1e-6:raise ValueError('拍点与唯一速度映射不一致')
        p.setdefault('step_quarters',points[i+1]['beat']-p['beat'] if i+1<len(points) else 1.)
        if not math.isfinite(p['step_quarters']) or p['step_quarters']<=0:raise ValueError('拍点音乐单位无效')
    for row in out['time_signatures']:
        if 'pulse_id' not in row:
            p=next((p for p in points if abs(p['beat']-row['beat'])<EPS),None)
            if p:row['pulse_id']=p['id']
        if row.get('pulse_id'):
            bound=next((p for p in points if p['id']==row['pulse_id']),None)
            if bound is None:raise ValueError('拍号所附拍点不存在，请明确重新附着')
            if abs(bound['beat']-row['beat'])>EPS:raise ValueError('拍号与附属拍点位置不一致，请明确重新附着')
    return out


def _at(points,t):return next((p for p in points if abs(p['seconds']-t)<EPS),None)


def _seconds(value,clock):
    if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or not 0<=value<=clock.seconds[-1]:
        raise ValueError('位置必须在音频范围内')
    return float(value)


def _delete_targets(out, clock, operation):
    targets = operation.get('seconds')
    if not isinstance(targets, list) or not targets:
        raise ValueError('请先选择要删除的拍点')
    ids = operation.get('pulse_ids')
    if 'pulse_ids' in operation and (not isinstance(ids, list) or len(ids) != len(targets)
                                    or any(not isinstance(ident, str) or not ident for ident in ids)):
        raise ValueError('拍点身份列表无效，请重新选择')
    points = out['pulse_points']
    by_id = {p['id']: p for p in points} if ids is not None else None
    times = [p['seconds'] for p in points] if ids is None else None
    removed = set()
    for index, value in enumerate(targets):
        at = _seconds(value, clock)
        if ids is not None:
            p = by_id.get(ids[index])
            if p is None or p['seconds'] != at:
                raise ValueError('所选拍点身份或位置已变化，请重新选择')
        else:
            i = bisect_left(times, at)
            if i < len(times) and times[i] == at:
                p = points[i]
            else:
                # Legacy displays have no IDs and can differ by float roundoff.
                # Reject ambiguity instead of deleting the first nearby point.
                matches = [points[j] for j in (i-1, i) if 0 <= j < len(times) and abs(times[j]-at) < EPS]
                if len(matches) != 1:
                    raise ValueError('无法唯一匹配当前拍点，请重新选择')
                p = matches[0]
        removed.add(p['id'])
    return removed


def _resolve_meters(out,removed,remaining,resolution):
    affected=[m for m in out['time_signatures'] if m.get('pulse_id') in removed]
    # The initial conductor signature at beat zero is mandatory MIDI metadata.
    affected=[m for m in affected if m['beat']>EPS]
    if affected and resolution not in ('previous','next','delete'):
        raise ValueError('所选拍点附有拍号：请选择移到前一拍、后一拍或明确删除拍号')
    rows=[]
    oldbyid={p['id']:p for p in out['pulse_points']}
    for m in out['time_signatures']:
        row=deepcopy(m)
        if row.get('pulse_id') in removed:
            if row['beat']<=EPS:row.pop('pulse_id',None)
            elif resolution=='delete':continue
            else:
                at=oldbyid[row['pulse_id']]['seconds']
                candidates=[p for p in remaining if p['seconds']<at-EPS] if resolution=='previous' else [p for p in remaining if p['seconds']>at+EPS]
                if not candidates:raise ValueError('该方向没有保留拍点，请选择另一方向或明确删除拍号')
                target=candidates[-1] if resolution=='previous' else candidates[0]
                row['pulse_id']=target['id']
        rows.append(row)
    attached=[r.get('pulse_id') for r in rows if r.get('pulse_id')]
    if len(attached)!=len(set(attached)):raise ValueError('多个拍号将落在同一拍点；请先分别处理，未覆盖任何拍号')
    out['time_signatures']=rows


def _rebuild(out,old,oldpoints,uniform=None):
    points=out['pulse_points']
    if not points:
        # A deliberate empty draft retains its mathematical clock until clicks
        # establish pulse constraints. This is not a hidden list of visible beats.
        out['pulse_mode']='manual_empty';return
    first=points[0]
    first['beat']=0. if first['seconds']<EPS else old.to_beat(first['seconds'])
    for previous,p in zip(points,points[1:]):p['beat']=previous['beat']+previous['step_quarters']
    times=[p['seconds'] for p in points];qs=[p['beat'] for p in points]
    if times[0]>EPS:times.insert(0,0.);qs.insert(0,0.)
    if times[-1]<old.seconds[-1]-EPS:
        priorlast=oldpoints[-1] if oldpoints else None
        remainder=old.beats[-1]-old.to_beat(max(priorlast['seconds'],points[-1]['seconds'])) if priorlast else old.beats[-1]-old.to_beat(points[-1]['seconds'])
        # Keep a positive mathematical tail, not an undeletable musical pulse.
        if remainder<=EPS:remainder=points[-1]['step_quarters']
        times.append(old.seconds[-1]);qs.append(qs[-1]+remainder)
    oldqs=[old.to_beat(t) for t in times]
    samples=sorted(set(old.seconds+times))
    def mapped(t):
        if uniform and uniform[0]-EPS<=t<=uniform[1]+EPS:
            return TempoMap._interpolate(t,times,qs)
        return TempoMap._interpolate(old.to_beat(t),oldqs,qs)
    out['anchors']=[dict(seconds=t,beat=mapped(t)) for t in samples]
    new=TempoMap(dict(out,pickup_beats=0))
    byid={p['id']:p for p in points}
    rows=[]
    for row in out['time_signatures']:
        row=deepcopy(row)
        row['beat']=byid[row['pulse_id']]['beat'] if row.get('pulse_id') else (0. if row['beat']==0 else new.to_beat(old.to_seconds(row['beat'])))
        rows.append(row)
    rows.sort(key=lambda r:r['beat'])
    if any(abs(a['beat']-b['beat'])<EPS for a,b in zip(rows,rows[1:])):
        raise ValueError('拍号重排后位置冲突，请先处理附属拍号')
    out['time_signatures']=rows
    for key in ('first_full_bar_beat','grid_origin'):
        if key in out:out[key]=new.to_beat(old.to_seconds(out[key]))
    if out.get('pickup_beats'):
        pickup=new.to_beat(old.to_seconds(out['pickup_beats']))
        size=rows[0]['numerator']*4/rows[0]['denominator']
        if pickup>=size-EPS:raise ValueError('本次拍数修改会让弱起达到一整小节；请先改为前奏或调整首完整小节')
        out['pickup_beats']=pickup
    out['pulse_mode']='manual'
    TempoMap(out)


def _meter_bars(out, old, operation, impact):
    from .duet_notation import reference_bars
    from .score_boundary import confirmed_end_beat

    start=_seconds(operation.get('start'),old);end=_seconds(operation.get('end'),old)
    n,d=operation.get('numerator'),operation.get('denominator',4)
    if (type(n) is not int or not 1<=n<=32 or type(d) is not int
            or d not in (1,2,4,8,16,32,64,128)):
        raise ValueError('拍号无效，请检查分子和分母')
    score_end=confirmed_end_beat(old)
    starts,lengths,_=reference_bars(old,min(old.beats[-1],score_end) if score_end is not None else old.beats[-1])
    signatures=out['time_signatures']
    meter_beats=[m['beat'] for m in signatures]
    bars=[]
    for q,length in zip(starts,lengths):
        a,b=old.to_seconds(q),old.to_seconds(q+length)
        if a>=end-EPS or b<=start+EPS:
            continue
        meter=signatures[bisect_right(meter_beats,q+EPS)-1]
        if meter.get('free') or abs(length-meter['numerator']*4/meter['denominator'])>EPS:
            raise ValueError('范围包含弱起、自由拍或不完整小节，请改选完整小节')
        if a<start-EPS or b>end+EPS:
            raise ValueError('请将左右定位点放在小节线上，选中完整小节')
        bars.append((a,b,q,length))
    if (not bars or abs(bars[0][0]-start)>EPS or abs(bars[-1][1]-end)>EPS
            or any(abs(left[1]-right[0])>EPS for left,right in zip(bars,bars[1:]))):
        raise ValueError('请将左右定位点放在小节线上，选中连续的完整小节')
    start,end=bars[0][0],bars[-1][1]
    points=out['pulse_points'];times=[p['seconds'] for p in points]
    unit=4/d;span=n*unit
    generated=[];segments=[];repartitioned=[];kept=set()
    def at(t):
        k=bisect_left(times,t-EPS)
        return points[k] if k<len(times) and abs(times[k]-t)<=EPS else None

    for index,(a,b,q,length) in enumerate(bars):
        first=bisect_left(times,a-EPS);last=bisect_left(times,b-EPS)
        slots={}
        for point in points[first:last]:
            slot=(point['beat']-q)*n/length
            i=round(slot)
            if not 0<=i<n or abs(slot-i)>1e-6 or i in slots:
                slots=None
                break
            slots[i]=point
        nq=bars[0][2]+index*span
        segments.append((a,b,q,length,nq,slots is not None))
        if slots is None:
            repartitioned.append(dict(start=a,end=b))
        for i in range(n):
            if slots is None:
                t=a+(b-a)*i/n
                point=at(t)
            else:
                t=a if i==0 else old.to_seconds(q+length*i/n)
                point=slots.get(i)
            if point is not None:
                t=point['seconds'];kept.add(point['id'])
            generated.append(dict(point or {'id':uuid4().hex},
                                  seconds=t,beat=nq+i*unit,step_quarters=unit))

    boundary=at(end)
    if boundary is not None:kept.add(boundary['id'])
    end_q=bars[-1][2]+bars[-1][3];new_end_q=bars[0][2]+len(bars)*span
    shift=new_end_q-end_q
    boundaries=[bar[0] for bar in bars]+[end]
    pos_by_second={p['seconds']:p['beat'] for p in generated}
    if boundary is not None:pos_by_second[boundary['seconds']]=new_end_q
    def mapped(t):
        if t in pos_by_second:return pos_by_second[t]
        if t<start:return old.to_beat(t)
        if t>=end:return old.to_beat(t)+shift
        a,b,q,length,nq,retain=segments[bisect_right(boundaries,t)-1]
        fraction=(old.to_beat(t)-q)/length if retain else (t-a)/(b-a)
        return nq+span*fraction

    before=[dict(p) for p in points[:bisect_left(times,start-EPS)]]
    after=[dict(p,beat=mapped(p['seconds'])) for p in points[bisect_right(times,end+EPS):]]
    boundary=dict(boundary,beat=new_end_q) if boundary else dict(
        id=uuid4().hex,seconds=end,beat=new_end_q,step_quarters=old.to_beat(end+1)-end_q)
    out['pulse_points']=before+generated+[boundary]+after
    if before:before[-1]['step_quarters']=generated[0]['beat']-before[-1]['beat']
    if after:boundary['step_quarters']=after[0]['beat']-new_end_q

    # Existing knots carry the old local rhythm when compatible; exact new
    # point knots carry generated/retained identities, avoiding noisy tiny
    # duplicate segments from a seconds round trip.
    for t,p in zip(boundaries,generated[::n]+[boundary]):
        if abs(t-p['seconds'])>EPS:pos_by_second.setdefault(t,mapped(t))
    pos_by_second.setdefault(boundary['seconds'],new_end_q)
    # The zero knot is mandatory even when a legacy start point is slightly
    # above zero; require its true seconds, not a rounded approximation.
    pos_by_second.setdefault(0.,0.)
    knots=sorted(pos_by_second)
    sample=set(knots)
    for t in old.seconds:
        i=bisect_left(knots,t)
        if not any(abs(knots[j]-t)<=EPS for j in (i-1,i) if 0<=j<len(knots)):
            sample.add(t)
    out['anchors']=[dict(seconds=t,beat=mapped(t)) for t in sorted(sample)]

    begin=generated[0]
    original=out['time_signatures']
    following=next(m for m in reversed(original) if m['beat']<=end_q+EPS)
    replaced=[m for m in original if start-EPS<=old.to_seconds(m['beat'])<end-EPS]
    rows=[dict(m,beat=mapped(old.to_seconds(m['beat']))) for m in original if m not in replaced]
    rows.append(dict(beat=begin['beat'],numerator=n,denominator=d,pulse_id=begin['id'],origin='editor_intent'))
    if not any(abs(m['beat']-new_end_q)<EPS for m in rows):
        rows.append(dict(following,beat=new_end_q,pulse_id=boundary['id'],origin='editor_intent'))
    out['time_signatures']=sorted(rows,key=lambda m:m['beat'])
    for key in ('first_full_bar_beat','grid_origin','pickup_beats'):
        if key in out:out[key]=mapped(old.to_seconds(out[key]))
    out['pulse_mode']='manual'
    pulse_state(out)
    replaced_count=sum(p['id'] not in kept for p in points[bisect_left(times,start-EPS):bisect_right(times,end+EPS)])
    return out,'已更改拍号；小节线的实际时间不变',dict(
        impact,bar_count=len(bars),preserved_point_count=len(kept),
        inserted_point_count=sum(p['id'] not in kept for p in generated+[boundary]),
        replaced_point_count=replaced_count,replaced_meter_count=len(replaced),
        repartitioned_bars=repartitioned,bar_boundaries_seconds=boundaries)


def _norm_smart_owner(raw):
    if isinstance(raw, str) and len(raw) >= 2 and raw[0] == '"' and raw[-1] == '"':
        return raw[1:-1]
    return raw


def _smart_owner_matches(row, owner_raw):
    norm = _norm_smart_owner(owner_raw)
    if not norm:
        return False
    if norm == 'zero':
        return abs(row['beat']) <= EPS
    pid = row.get('pulse_id')
    return pid is not None and _norm_smart_owner(str(pid)) == norm


def _reconcile_smart_measures(out):
    sigs = out.get('time_signatures') or []
    if not any(isinstance(r.get('smart_measure'), dict) for r in sigs):
        return
    is_gen = lambda r: isinstance(r.get('smart_measure'), dict) and bool(r['smart_measure'].get('generated'))
    real_check = [r for r in sigs if not is_gen(r)]
    surviving = []
    for old_r in sigs:
        r = dict(old_r)
        sm = r.get('smart_measure')
        if isinstance(sm, dict):
            ow = sm.get('owner')
            alive = isinstance(ow, str) and any(_smart_owner_matches(c, ow) for c in real_check)
            if not alive:
                if sm.get('generated') and r['beat'] > EPS:
                    continue
                r.pop('smart_measure', None)
                r.pop('free', None)
        surviving.append(r)
    surviving.sort(key=lambda r: r['beat'])
    if not any(isinstance(r.get('smart_measure'), dict) for r in surviving):
        out['time_signatures'] = surviving
        return

    clock = TempoMap(out)
    first_full = float(out.get('first_full_bar_beat', out.get('pickup_beats', 0)) or 0.0)
    policy = out.get('opening_policy')
    rev_open = isinstance(policy, dict) and isinstance(policy.get('keep_before_bar1'), bool)
    marked = out.get('short_bar_annotations') or []
    points = out.get('pulse_points') or []

    def protect(a, b, in_prelude):
        if a < first_full - EPS and (not in_prelude or b > first_full + EPS):
            return True
        sa, sb = clock.to_seconds(a), clock.to_seconds(b)
        return any(m['start_seconds'] < sb - EPS and m['end_seconds'] > sa + EPS for m in marked)

    real_rows = [r for r in surviving if not is_gen(r)]
    gen_rows = [r for r in surviving if is_gen(r)]
    rebuilt = []
    for idx, base_src in enumerate(real_rows):
        base = dict(base_src)
        a = float(base['beat'])
        in_prelude = rev_open and a < first_full - EPS
        b = float(real_rows[idx + 1]['beat']) if idx + 1 < len(real_rows) else math.inf
        if in_prelude:
            b = min(b, first_full)
        base_smart = isinstance(base.get('smart_measure'), dict)
        manual_free = bool(base.get('free')) and not base_smart
        sec_gen = [g for g in gen_rows if a + EPS < g['beat'] < b - EPS]
        if manual_free or (not base_smart and not sec_gen):
            rebuilt.append(base)
            rebuilt.extend(dict(g) for g in sec_gen)
            continue
        owner_str = ''
        if base_smart and isinstance(base['smart_measure'].get('owner'), str):
            owner_str = base['smart_measure']['owner']
        elif sec_gen and isinstance(sec_gen[-1]['smart_measure'].get('owner'), str):
            owner_str = sec_gen[-1]['smart_measure']['owner']
        base.pop('smart_measure', None)
        base.pop('free', None)
        if not math.isfinite(b) or b <= a + EPS:
            rebuilt.append(base)
            continue
        unit_len = base['numerator'] * 4.0 / base['denominator']
        span = b - a
        nearest_bars = round(span / unit_len)
        if (span >= unit_len - EPS and abs(span - nearest_bars * unit_len) <= EPS) or span <= EPS:
            rebuilt.append(base)
            continue
        full = a + math.floor((span + EPS) / unit_len) * unit_len
        if full <= a + EPS:
            if not protect(a, b, in_prelude):
                base.update(free=True, smart_measure=dict(owner=owner_str, generated=False))
            rebuilt.append(base)
        else:
            rebuilt.append(base)
            if full < b - EPS and not protect(full, b, in_prelude):
                gen = dict(base, beat=full, free=True, smart_measure=dict(owner=owner_str, generated=True))
                gen.pop('pulse_id', None)
                pt = _at(points, clock.to_seconds(full))
                if pt:
                    gen['pulse_id'] = pt['id']
                rebuilt.append(gen)
    rebuilt.sort(key=lambda r: r['beat'])
    out['time_signatures'] = rebuilt


def _smart_meter_rows(out, q, desired, owner):
    from .duet_notation import reference_bars
    from .score_boundary import confirmed_end_beat

    owner_norm = _norm_smart_owner(owner)
    clean=[]
    for old in out['time_signatures']:
        row=dict(old)
        auto=row.get('smart_measure',{})
        if isinstance(auto, dict) and _norm_smart_owner(auto.get('owner'))==owner_norm:
            if auto.get('generated'):continue
            row.pop('smart_measure',None);row.pop('free',None)
        clean.append(row)
    out['time_signatures']=clean
    clock=TempoMap(out);score_end=confirmed_end_beat(clock)
    starts,lengths,_=reference_bars(clock,min(clock.beats[-1],score_end) if score_end is not None else clock.beats[-1])
    first_full=out.get('first_full_bar_beat',out.get('pickup_beats',0))
    rows=[r for r in clean if abs(r['beat']-q)>EPS]+[desired]
    rows.sort(key=lambda r:r['beat'])
    report=[]
    def span(a,b,side):
        if a<first_full-EPS or b<=a+EPS:return
        sa,sb=clock.to_seconds(a),clock.to_seconds(b)
        if any(m['start_seconds']<sb-EPS and m['end_seconds']>sa+EPS
               for m in out.get('short_bar_annotations',[])):return
        row=next((r for r in reversed(rows) if r['beat']<=a+EPS),None)
        if row is None or row.get('free'):return
        generated=abs(row['beat']-a)>EPS
        if generated:
            row=dict(row,beat=a)
            row.pop('pulse_id',None)
            point=_at(out['pulse_points'],sa)
            if point:row['pulse_id']=point['id']
            rows.append(row);rows.sort(key=lambda r:r['beat'])
        row.update(free=True,smart_measure=dict(owner=owner,generated=generated))
        report.append(dict(start=sa,end=sb,side=side))

    i=bisect_left(starts,q-EPS)-1
    if i>=0:
        a=starts[i]
        active=next(r for r in reversed(clean) if r['beat']<=a+EPS)
        if not active.get('free') and q<=a+lengths[i]+EPS and q-a<active['numerator']*4/active['denominator']-EPS:
            span(a,q,'before')
    following=next((r for r in rows if r['beat']>q+EPS and not (isinstance(r.get('smart_measure'), dict) and r['smart_measure'].get('generated'))),None)
    fixed=following['beat'] if following else math.inf
    if math.isfinite(fixed):
        size=desired['numerator']*4/desired['denominator']
        full=q+math.floor((fixed-q+EPS)/size)*size
        if full<fixed-EPS:span(full,fixed,'after')
    return rows,report


def apply_pulses(data,operation,pins=()):
    pins=()  # Legacy editor pins are not musical pulse points.
    out=pulse_state(data);old=TempoMap(deepcopy(out));points=out['pulse_points'];oldpoints=deepcopy(points)
    kind=operation['action'];impact={'source_seconds_unchanged':True,'tempo_changed':True}
    if kind=='set_meter':
        at=_seconds(operation.get('seconds'),old);p=_at(points,at)
        q=p['beat'] if p else old.to_beat(at)
        existing=next((r for r in out['time_signatures'] if abs(r['beat']-q)<EPS),{})
        if p is None and at!=0 and not existing.get('smart_measure'):
            raise ValueError('拍号需要附在原有拍点；请先选择拍点')
        n,d=operation.get('numerator'),operation.get('denominator',4)
        if type(n) is not int or not 1<=n<=32 or type(d) is not int or d not in (1,2,4,8,16,32,64,128):
            raise ValueError('拍号无效')
        row=dict(existing,beat=q,numerator=n,denominator=d,origin='editor_intent')
        row.pop('free',None);row.pop('smart_measure',None)
        if p:row['pulse_id']=p['id']
        if existing.get('free') and not existing.get('smart_measure'):
            rows=[r for r in out['time_signatures'] if abs(r['beat']-q)>EPS]+[row]
            report=[]
        else:
            owner=p['id'] if p else ('zero' if at==0 else format(at,'.17g'))
            rows,report=_smart_meter_rows(out,q,row,owner)
        out['time_signatures']=sorted(rows,key=lambda r:r['beat'])
        if out.get('pickup_beats',0)>=n*4/d and q==0:
            raise ValueError('新拍号容不下当前弱起；请先调整开头')
        return out,'拍号已更改；拍点和速度映射不变',dict(impact,tempo_changed=False,smart_measures=report)
    if kind=='move_meter':
        at=_seconds(operation.get('seconds'),old);to=_seconds(operation.get('to_seconds'),old);target=_at(points,to)
        source=next((r for r in out['time_signatures'] if abs(old.to_seconds(r['beat'])-at)<EPS),None)
        if source is None or target is None:raise ValueError('请选择现有拍号及其目标拍点')
        if source['beat']==0:raise ValueError('初始拍号不能移动，只能修改')
        if any(r is not source and abs(r['beat']-target['beat'])<EPS for r in out['time_signatures']):raise ValueError('目标拍点已有拍号，未覆盖')
        source.update(beat=target['beat'],pulse_id=target['id']);out['time_signatures'].sort(key=lambda r:r['beat'])
        _reconcile_smart_measures(out)
        return out,'拍号已移至目标拍点；持续到下一个拍号',dict(impact,tempo_changed=False)
    if kind=='clear_beats':
        if len(out['time_signatures'])>1 and operation.get('meter_resolution')!='delete':raise ValueError('从零布拍将清除附属变拍拍号，请明确选择删除这些拍号')
        out['time_signatures']=[dict(out['time_signatures'][0])];out['time_signatures'][0].pop('pulse_id',None)
        out['pulse_points']=[];out['pulse_mode']='manual_empty'
        return out,'拍点已清空，可连续点击布拍；原始音符和速度初稿保留',dict(impact,tempo_changed=False)
    supplement_owner=None
    if kind=='add_beat':
        at=_seconds(operation.get('seconds'),old)
        if _at(points,at):raise ValueError('该位置已有拍点')
        q=old.to_beat(at)
        current=next((r for r in reversed(out['time_signatures']) if r['beat']<=q+EPS),None)
        if current and q>current['beat']+EPS:supplement_owner=current.get('smart_measure',{}).get('owner')
        index=bisect_right([p['seconds'] for p in points],at)
        unit=operation.get('pulse_unit',1.)
        if isinstance(unit,bool) or not isinstance(unit,(int,float)) or not math.isfinite(unit) or unit<=0:raise ValueError('新增拍点单位必须为正数四分音符单位')
        points.insert(index,dict(id=uuid4().hex,seconds=at,beat=old.to_beat(at),step_quarters=float(unit)))
        summary='已新增一拍；已有拍点秒位置不变，后续重新编号'
        if not oldpoints and at>EPS:impact['requires_opening_choice']=True
    elif kind=='meter_bars':
        return _meter_bars(out,old,operation,impact)
    elif kind=='set_beat_duration':
        targets=operation.get('seconds');unit=operation.get('duration_quarters')
        if not isinstance(targets,list) or not targets:raise ValueError('请先选择拍点')
        if isinstance(unit,bool) or not isinstance(unit,(int,float)) or not math.isfinite(unit) or unit<=0:raise ValueError('时值必须为正数四分音符单位')
        matched=[_at(points,_seconds(t,old)) for t in targets]
        if any(p is None for p in matched):raise ValueError('所选拍点已变化，请重新选择')
        for p in matched:p['step_quarters']=float(unit)
        if all(p is points[-1] for p in matched):
            return out,'Tail interval planned; source clock unchanged',dict(impact,tempo_changed=False)
        summary='已修改到下一拍点的谱面时值；原始秒位置不变'
    elif kind=='delete_beats':
        removed=_delete_targets(out,old,operation)
        remaining=[p for p in points if p['id'] not in removed]
        _resolve_meters(out,removed,remaining,operation.get('meter_resolution','delete'))
        out['pulse_points']=remaining
        summary=f'已删除 {len(removed)} 个拍点；其余拍点及原始事件秒位置不变'
    elif kind=='divide_beats':
        start=_seconds(operation.get('start'),old);end=_seconds(operation.get('end'),old);n=operation.get('interval_count')
        if end<=start or type(n) is not int or not 1<=n<=4096:raise ValueError('请选择递增两端和 1–4096 个间隔')
        left,right=_at(points,start),_at(points,end)
        if left is None or right is None:raise ValueError('均分的两端必须是已有拍点，请先添加端点')
        q_i,q_j=left['beat'],right['beat']
        left_meter=next((r for r in reversed(out['time_signatures']) if r['beat']<=q_i+EPS),out['time_signatures'][0])
        right_meter=next((r for r in reversed(out['time_signatures']) if r['beat']<=q_j+EPS),out['time_signatures'][0])
        beat_unit=4.0/left_meter['denominator']
        legal_units=(0.125,0.25,0.5,0.75,1.0,1.5,2.0,3.0,4.0)
        li,rj=points.index(left),points.index(right)
        u0=float(left['step_quarters'])
        uniform_legal=(any(abs(u0-v)<=1e-6 for v in legal_units) and
                       all(abs(float(points[k]['step_quarters'])-u0)<=1e-6 for k in range(li,rj)))
        unit=u0 if (uniform_legal and abs((q_j-q_i)-n*u0)<=1e-5) else beat_unit
        right_has_meter=any((r.get('pulse_id')==right['id']) or abs(r['beat']-q_j)<=EPS for r in out['time_signatures'])
        interior=[p for p in points if start+EPS<p['seconds']<end-EPS];removed={p['id'] for p in interior}
        remaining=[p for p in points if p['id'] not in removed]
        kept_sigs=[];replaced_meters=0
        for r in out['time_signatures']:
            bound_rem=r.get('pulse_id') in removed
            inside_t=start+EPS<old.to_seconds(r['beat'])<end-EPS
            if (bound_rem or inside_t) and r['beat']>EPS:
                replaced_meters+=1
                continue
            kept_sigs.append(r)
        out['time_signatures']=kept_sigs
        restored_right=False
        if replaced_meters>0 and not right_has_meter:
            differs=(right_meter['numerator']!=left_meter['numerator'] or
                     right_meter['denominator']!=left_meter['denominator'] or
                     (bool(right_meter.get('free')) and not right_meter.get('smart_measure')) !=
                     (bool(left_meter.get('free')) and not left_meter.get('smart_measure')))
            if differs:
                anchor_m=dict(right_meter,beat=q_j,pulse_id=right['id'])
                if isinstance(anchor_m.get('smart_measure'),dict):
                    anchor_m.pop('smart_measure',None);anchor_m.pop('free',None)
                out['time_signatures'].append(anchor_m)
                out['time_signatures'].sort(key=lambda r:r['beat'])
                restored_right=True
        left['step_quarters']=unit
        from .midi_pulse_divide import propose
        division=propose(start,end,n,operation.get('_note_onsets',[]),snap=operation.get('snap_to_midi',False))
        generated=[dict(id=uuid4().hex,seconds=t,beat=0.,step_quarters=unit) for t in division['beat_seconds'][1:-1]]
        out['pulse_points']=sorted(remaining+generated,key=lambda p:p['seconds'])
        _rebuild(out,old,oldpoints,uniform=(start,end))
        if restored_right:
            for r in out['time_signatures']:
                if r.get('pulse_id')==right['id']:
                    desired=dict(r);desired.pop('smart_measure',None);desired.pop('free',None)
                    rows,rep=_smart_meter_rows(out,desired['beat'],desired,right['id'])
                    out['time_signatures']=rows;impact['smart_measures']=rep
                    break
        _reconcile_smart_measures(out)
        return out,f'两端间已按秒均分 {n} 个间隔，共 {n+1} 个拍点',dict(impact,division_preview=division,replaced_points=[dict(id=p['id'],seconds=p['seconds']) for p in interior],interval_count=n,point_count=n+1,replaced_point_count=len(interior),replaced_meter_count=replaced_meters,unit_quarters=unit)
    else:raise ValueError('未知拍点操作')
    _rebuild(out,old,oldpoints)
    _reconcile_smart_measures(out)
    return out,summary,impact
