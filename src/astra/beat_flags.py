"""Move a musical beat flag in source time without moving any musical address."""
from copy import deepcopy
import math

from .timing import TempoMap

EPS=1e-7


def validate_flag(tempo, seconds, beat):
    if any(isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) for v in (seconds,beat)):
        raise ValueError('拍旗帜的位置无效。')
    clock=TempoMap(deepcopy(tempo))
    if not clock.beats[0]-EPS<=beat<=clock.beats[-1]+EPS or abs(clock.to_seconds(beat)-seconds)>EPS:
        raise ValueError('拍旗帜已变化，请重新选择。')
    rows=tempo['time_signatures'];index=max(i for i,row in enumerate(rows) if row['beat']<=beat+EPS)
    row=rows[index];unit=4/row['denominator']
    origin=float(row['beat']) if index else float(tempo.get('first_full_bar_beat',
        tempo.get('grid_origin',tempo.get('pickup_beats',row['beat']))))
    if abs((beat-origin)/unit-round((beat-origin)/unit))>EPS:
        raise ValueError('请选择当前拍号下的完整拍位旗帜。')


def move_flag(tempo, operation, fixed_anchors=()):
    clock=TempoMap(deepcopy(tempo))
    values=[]
    for key in ('seconds','to_seconds','beat'):
        value=operation.get(key)
        if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value):
            raise ValueError('拍点位置必须是有限数值。')
        values.append(float(value))
    source,target,beat=values
    if not clock.beats[0]+EPS<beat<clock.beats[-1]-EPS:
        raise ValueError('不能拖动音频首尾边界。')
    if abs(clock.to_seconds(beat)-source)>EPS:
        raise ValueError('拍点位置已变化，请重新选择旗帜。')
    signatures=tempo['time_signatures']
    index=max(i for i,row in enumerate(signatures) if row['beat']<=beat+EPS)
    row=signatures[index];unit=4/row['denominator']
    origin=float(row['beat']) if index else float(tempo.get('first_full_bar_beat',
        tempo.get('grid_origin',tempo.get('pickup_beats',row['beat']))))
    if abs((beat-origin)/unit-round((beat-origin)/unit))>EPS:
        raise ValueError('请选择当前拍号下的完整拍位旗帜；不会把任意小数拍当成新拍。')
    neighbors={clock.beats[0],clock.beats[-1]}
    for i,meter in enumerate(signatures):
        start=float(meter['beat']);stop=float(signatures[i+1]['beat']) if i+1<len(signatures) else clock.beats[-1]
        phase=start if i else float(tempo.get('first_full_bar_beat',tempo.get('grid_origin',tempo.get('pickup_beats',start))))
        step=4/meter['denominator'];k=math.floor((beat-phase)/step)
        neighbors.update(q for j in (k-1,k,k+1,k+2) if start-EPS<=(q:=phase+j*step)<=stop+EPS)
        neighbors.add(start)
    left=max(q for q in neighbors if q<beat-EPS)
    right=min(q for q in neighbors if q>beat+EPS)
    before,after=clock.to_seconds(left),clock.to_seconds(right)
    scope=operation.get('scope')
    if scope is not None:
        if not isinstance(scope,dict):
            raise ValueError('选区已失效，请重新框选。')
        start,end=scope.get('start'),scope.get('end')
        if (any(isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) for v in (start,end))
                or start<clock.seconds[0]-EPS or end>clock.seconds[-1]+EPS or end-start<=EPS):
            raise ValueError('选区边界必须在曲目内且严格递增。')
        if not start+EPS<source<end-EPS:
            raise ValueError('只能移动框内旗帜；选区两端保持固定。')
        if not start+EPS<target<end-EPS:
            raise ValueError('旗帜不能越过选区边界；框外节拍不会移动。')
        # A selection can end between beats. Its exact endpoints are temporary
        # supports, not persisted user locks or a silently enlarged selection.
        before,after=max(before,start),min(after,end)
        left,right=clock.to_beat(before),clock.to_beat(after)
    if not before+EPS<target<after-EPS:
        raise ValueError('拍点不能越过相邻旗帜；补拍或删拍请使用拍数修正。')
    pins=[]  # Only the explicit score-end boundary remains a support.
    if tempo.get('score_end'):
        pins.append({'seconds':float(tempo['score_end']['seconds'])})
    if any(abs(float(pin['seconds'])-source)<EPS for pin in pins):
        raise ValueError('这面旗帜已固定，请先解除锁定。')
    rows=[deepcopy(a) for a in clock.anchors if abs(a['beat']-beat)>EPS]
    # Virtual ruler beats get real neighboring boundaries in this one edit;
    # dragging a beat in a two-anchor constant map must not stretch the song.
    for seconds,q in [(before,left),(after,right),
                      *[(float(p['seconds']),clock.to_beat(float(p['seconds']))) for p in pins]]:
        if not any(abs(a['seconds']-seconds)<EPS for a in rows):
            rows.append({'seconds':seconds,'beat':q})
    rows.append({'seconds':target,'beat':beat});rows.sort(key=lambda a:a['seconds'])
    if any(b['seconds']-a['seconds']<=EPS or b['beat']-a['beat']<=EPS for a,b in zip(rows,rows[1:])):
        raise ValueError('拖动与相邻映射点或固定旗帜冲突；请缩小移动范围。')
    result=deepcopy(tempo);result['anchors']=rows
    new=TempoMap(result)
    if 'intro_downbeat_seconds' in result and 'first_full_bar_beat' in result:
        result['intro_downbeat_seconds']=new.to_seconds(result['first_full_bar_beat'])
    return result,{'start_seconds':before,'end_seconds':after,
        'source_seconds':source,'target_seconds':target,'beat':beat}
