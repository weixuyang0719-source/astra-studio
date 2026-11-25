"""Atomic integer-bar edits of the single effective tempo map.

The caller owns revision CAS, persistence and undo. This module only returns a
detached draft. Notes and CC remain seconds-based and are never inputs here.
"""
from bisect import bisect_right
from copy import deepcopy
import hashlib
import json
import math

from .timing import TempoMap

EPS = 1e-7
VERSION = 2


def _sha(data):
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _number(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f'{name}必须是有限数值')
    return float(value)


def _geometry(tempo):
    from .duet_notation import reference_bars
    clock = TempoMap(deepcopy(tempo))
    last = clock.beats[-1]
    pickup = float(tempo.get('pickup_beats', 0))
    first = float(tempo.get('first_full_bar_beat', pickup))
    # The final audio/score boundary is the only trailing exception. Interior
    # partial bars must never be legitimized by allow_partial_measures=True.
    ending = tempo.get('score_end', {}).get('seconds')
    last = min(last, clock.to_beat(float(ending))) if ending is not None else last
    starts, lengths, meters = reference_bars(clock, last)
    for index, (start, length, meter) in enumerate(zip(starts, lengths, meters)):
        n, d = map(int, meter.split('/'))
        size = n*4/d
        opening = (index == 0 and pickup > 0 and abs(length-pickup) < EPS) or (
            first > 0 and abs(start+length-first) < EPS and start < first)
        trailing = start+length >= last-EPS
        explicit = any(abs(clock.to_seconds(start)-r.get('start_seconds',-1))<EPS and abs(clock.to_seconds(start+length)-r.get('end_seconds',-1))<EPS for r in tempo.get('explicit_partial_measures',[]))
        if abs(length-size) > EPS and not opening and not trailing and not explicit:
            seconds = clock.to_seconds(start+length)
            raise ValueError(f'现有第 {index+1} 小节在 {seconds:.3f} 秒形成 {length:g} 个四分音符单位的不完整普通小节；'
                             '请返回合法版本后重划，不会自动覆盖旧稿')
    return clock, starts, lengths, meters, last


def list_boundaries(tempo, revision):
    """Revision-bound, displayable existing bar lines, never arbitrary seconds."""
    if not isinstance(revision, str) or not revision:
        raise ValueError('缺少当前编辑版本')
    clock, starts, lengths, meters, last = _geometry(tempo)
    identity = _sha(tempo)
    first = float(tempo.get('first_full_bar_beat', tempo.get('pickup_beats', 0)))
    first_index = next((i for i,q in enumerate(starts) if abs(q-first)<EPS), 0)
    rows = []
    for index, q in enumerate(starts):
        if q > last+EPS:
            break
        rows.append(dict(id=_sha([revision, identity, index, q]), revision=revision,
            tempo_sha256=identity, index=index, bar_number=index-first_index+1,
            label=(f'第 {index-first_index+1} 小节开始' if index >= first_index else '引子／弱起开始'),
            beat=float(q), seconds=clock.to_seconds(q), meter=meters[index],
            opening_boundary=index<first_index))
    # reference_bars normally includes a start exactly at the audio endpoint;
    # a confirmed score_end can stop its iteration before that final start.
    if starts and abs(starts[-1]+lengths[-1]-last)<EPS and not any(abs(r['beat']-last)<EPS for r in rows):
        index = len(starts)
        rows.append(dict(id=_sha([revision,identity,index,last]), revision=revision,tempo_sha256=identity,
            index=index,bar_number=index-first_index+1,label='已确认曲尾小节线',
            beat=last,seconds=clock.to_seconds(last),meter=meters[-1],opening_boundary=False))
    return rows


def apply_rebar(tempo, operation, fixed_anchors=None, *, revision):
    """Recount only between two existing, immovable selected bar lines.

    Outside physical beat/bar times and interval slopes do not change.
    Bar numbers may naturally change after an explicit new bar count.
    Right-hand absolute quarter
    coordinates may translate; they are not physical event times.
    """
    op = deepcopy(operation)
    if op.get('boundary_revision') != revision or op.get('tempo_sha256') != _sha(tempo):
        raise ValueError('所选小节线已过期，请在当前版本重新选择')
    boundaries = list_boundaries(tempo, revision)
    by_id = {r['id']: r for r in boundaries}
    try:
        left, right = by_id[op['left_boundary']], by_id[op['right_boundary']]
    except (KeyError, TypeError):
        raise ValueError('请选择当前版本中两条已有小节线') from None
    if left['index'] >= right['index']:
        raise ValueError('右侧小节线必须位于左侧之后')
    n, d, bars = op.get('numerator'), op.get('denominator'), op.get('bar_count')
    if type(n) is not int or not 1<=n<=32 or type(d) is not int or d not in (1,2,4,8,16,32,64,128):
        raise ValueError('请输入有效拍号，例如 4/4 或 6/8')
    if type(bars) is not int or not 1<=bars<=4096:
        raise ValueError('小节数必须是 1–4096 的整数')
    selected_bars=right['index']-left['index']
    distribution=op.get('distribution','preserve')
    if distribution not in ('preserve','uniform'):
        raise ValueError('重划分布必须是保留原形状或明确的选区定速')
    start, end = _number(op.get('start'),'左定位秒数'), _number(op.get('end'),'右定位秒数')
    clock = TempoMap(deepcopy(tempo))
    a, b, qa, qb = left['seconds'], right['seconds'], left['beat'], right['beat']
    if start<0 or end>clock.seconds[-1]+EPS or end-start<=EPS:
        raise ValueError('左右定位必须在音频内且严格递增')
    score_end=tempo.get('score_end',{}).get('seconds')
    if score_end is not None and end>float(score_end)+EPS:
        raise ValueError('重划不能越过已确认谱面终点；请先调整或取消终点')
    if abs(start-a)>EPS or abs(end-b)>EPS:
        raise ValueError('框选边界必须是所选已有小节线，不能移动框外节拍；请明确重新选择边界')
    # Use their exact existing seconds, not rounded display text.
    start,end=a,b
    inside = [r for r in tempo['time_signatures'] if qa+EPS<r['beat']<qb-EPS]
    if inside and not op.get('replace_changes',False):
        raise ValueError('范围内已有变拍；请明确选择替换范围内拍号')
    # Do not implicitly relabel a pickup/intro as an ordinary complete bar.
    first = float(tempo.get('first_full_bar_beat',tempo.get('pickup_beats',0)))
    if qa < first-EPS:
        raise ValueError('请从首个完整小节开始重划；弱起／引子使用专用操作')
    target = bars*n*4/d
    delta = target-(qb-qa)
    factor = target/(qb-qa)
    def qmap(q):
        if q<=qa:return q
        if q>=qb:return q+delta
        return qa+(q-qa)*factor
    pins = []
    samples=set(clock.seconds)|{start,end}
    # Source meter times and metadata must be represented exactly in the same
    # transported curve rather than separately approximated.
    samples.update(clock.to_seconds(r['beat']) for r in tempo['time_signatures']
                   if r['beat']<=clock.beats[-1])
    # Leave every old sample second exact; avoid inserting a numerically
    # duplicate derived boundary near an already present clock sample.
    exact=clock.seconds
    samples={next((x for x in exact if abs(t-x)<1e-10),t) for t in samples}
    result=deepcopy(tempo)
    result['anchors']=[]
    for t in sorted(samples):
        beat=qmap(clock.to_beat(t))
        if distribution=='uniform' and start<=t<=end:
            beat=qa+(t-start)*target/(end-start)
        result['anchors'].append(dict(seconds=t,beat=beat))
    restore=next(r for r in reversed(tempo['time_signatures']) if r['beat']<=qb+EPS)
    rows=[dict(r,beat=qmap(r['beat'])) for r in tempo['time_signatures'] if r['beat']<qa-EPS or r['beat']>qb+EPS]
    rows.extend([dict(beat=qa,numerator=n,denominator=d,origin='editor_rebar'),
                 dict(restore,beat=qa+target,origin='editor_rebar_restore')])
    result['time_signatures']=sorted(rows,key=lambda r:r['beat'])
    for key in ('grid_origin','first_full_bar_beat','pickup_beats'):
        if key in result:result[key]=qmap(result[key])
    new=TempoMap(result)
    _geometry(result)
    new_boundaries=list_boundaries(result,revision)
    def musical_identity(rows, seconds, music_clock):
        idx=max(0,bisect_right([r['seconds'] for r in rows],seconds+EPS)-1)
        row=rows[idx];denominator=int(row['meter'].split('/')[1])
        return row['seconds'],(music_clock.to_beat(seconds)-row['beat'])*denominator/4
    result['source']='manual_editor'
    result['meter_policy']={'mode':'manual_editor','protected':True}
    result['last_tempo_operation']=dict(op,type='rebar')
    impact=dict(scope='selected_range',start_seconds=start,end_seconds=end,selected_start_seconds=start,
        selected_end_seconds=end,old_boundary_seconds=[a,b],new_boundary_seconds=[start,end],
        old_bar_numbers=[left['bar_number'],right['bar_number']],new_bar_count=bars,affected_bar_count=bars,
        quarters=target,following_quarter_shift=delta,
        distribution='uniform_selected_range' if distribution=='uniform' else 'preserve_piecewise_shape',
        source_note_seconds_modified=False,source_control_seconds_modified=False,
        outer_clock_seconds_unchanged=True,outside_bar_numbers_unchanged=bars==selected_bars,
        following_bar_number_shift=bars-selected_bars,
        outside_physical_beats_and_bars_unchanged=True)
    result['rebar_edit']=dict(version=VERSION,impact=deepcopy(impact))
    return dict(tempo=result,fixed_anchors=pins,impact=impact,warnings=[],
        summary=f'第 {left["bar_number"]}–{right["bar_number"]} 小节线重划为 {bars} 小节 {n}/{d}；'
                + ('选区按确认拍数定速，' if distribution=='uniform' else '') +
                f'仅修改框内 {start:g}–{end:g} 秒；框外拍点、小节线时刻与演奏秒数不变'
                + (f'，后方小节编号顺延 {bars-selected_bars:+d}' if bars!=selected_bars else ''))
