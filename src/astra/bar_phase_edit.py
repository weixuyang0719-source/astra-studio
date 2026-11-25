"""Explicit, revision-bound local bar decoding using cached audio evidence.

This changes grouping only. It cannot recover missing pulses or reinterpret
half/double tempo. The working quarter clock and every source event stay fixed.
"""
from bisect import bisect_right
from copy import deepcopy
import hashlib
import math
from pathlib import Path
import re

from . import editing
from .timing import TempoMap

EPS = 1e-6


def _phase(tempo, beat):
    rows=tempo['time_signatures']
    row=rows[max(0,bisect_right([r['beat'] for r in rows],beat+EPS)-1)]
    if row['denominator'] != 4 or row['numerator'] not in (3,4):
        raise ValueError('缓存分组复核目前只支持 3/4 与 4/4；其它拍号请使用手工分组。')
    origin=float(row['beat'])
    first=float(tempo.get('first_full_bar_beat',tempo.get('pickup_beats',0)))
    if beat < first-EPS:
        raise ValueError('请先指定首个完整小节，再选择其后的完整拍点范围。')
    if origin < first <= beat+EPS:origin=first
    offset=beat-origin
    if abs(offset-round(offset)) > EPS:
        raise ValueError('范围和确认强拍必须位于当前四分音符网格；请先修正拍位或拍数。')
    return {'beat_number':int(round(offset)) % row['numerator']+1,'meter':row['numerator']}


def prepare(project, base_revision, request):
    """Validate a small immutable request on the GUI thread; no model/file hash."""
    doc=editing.current(project)
    editing._check_base(doc,base_revision)
    clock=TempoMap(deepcopy(doc['tempo']))
    values=[]
    for name in ('start','end','downbeat_seconds'):
        value=request.get(name)
        if isinstance(value,bool) or not isinstance(value,(float,int)) or not math.isfinite(value):
            raise ValueError('请输入有效的范围与确认强拍时间。')
        values.append(float(value))
    start,end,at=values
    if not 0<=start<at<end<=doc['duration']+EPS:
        raise ValueError('确认强拍需位于选区内部；两端用于保留原分组衔接。')
    left,right=clock.to_beat(start),clock.to_beat(end)
    span=right-left
    if abs(span-round(span)) > EPS or not 2<=span<=4096:
        raise ValueError('请选择 2–4096 个完整四分音符间隔；此操作不改变拍数。')
    boundary={'start':_phase(doc['tempo'],left),'end':_phase(doc['tempo'],right)}
    positions=[left+i for i in range(round(span)+1)]
    # Reject an internal fractional phase reset instead of silently skipping it.
    for row in doc['tempo']['time_signatures']:
        if left<row['beat']<right:
            _phase(doc['tempo'],row['beat'])
            if abs(row['beat']-left-round(row['beat']-left))>EPS:
                raise ValueError('选区跨越非整拍分组边界，请拆分范围或先修正拍位。')
    offset=clock.to_beat(at)-left
    if abs(offset-round(offset))>EPS:
        raise ValueError('确认强拍不在当前四分拍上；请先移动映射点，或启用网格吸附。')
    _phase(doc['tempo'],clock.to_beat(at))
    meter=request.get('meter')
    if meter is not None and (type(meter) is not int or meter not in (3,4)):
        raise ValueError('确认拍号只能选择 3/4、4/4 或由缓存判断。')
    beats=[clock.to_seconds(q) for q in positions]
    downbeat={'seconds':beats[round(offset)]}
    if meter is not None:downbeat['meter']=meter
    constraints={'version':1,'kind':'bar_phase','start_seconds':beats[0],'end_seconds':beats[-1],
                 'beat_seconds':beats,'boundary_states':boundary,'downbeats':[downbeat]}
    return {'project':str(Path(project).resolve()),'base_revision':base_revision,
            'tempo':deepcopy(doc['tempo']),'fixed_anchors':[],
            'constraints':constraints,'quarter_positions':positions}


def _activation_file(project):
    """Bind the shared NPZ to this project's current detection record exactly."""
    from .pipeline import read_json
    root=Path(project).resolve();state=read_json(root/'project.json')
    key=state.get('analysis_keys',{}).get('beat_this')
    if not isinstance(key,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',key):
        raise ValueError('此工程没有可核对的节拍激活缓存；请使用手工分组。')
    record=root/'cache'/f'beats-{key}.json';path=root/'cache'/'beat'/'activations.npz'
    if not record.resolve().is_relative_to(root) or not path.resolve().is_relative_to(root):
        raise ValueError('节拍缓存不在当前工程目录内。')
    if not record.is_file() or not path.is_file():
        raise ValueError('缺少当前节拍激活缓存；此操作不会自动重跑模型。')
    evidence=read_json(record).get('bar_result') or {}
    expected=evidence.get('activation_sha256')
    if not isinstance(expected,str) or not re.fullmatch(r'[a-f0-9]{64}',expected):
        raise ValueError('历史节拍结果缺少激活身份，不能混用其它模型缓存。')
    if path.stat().st_size>128*1024*1024:
        raise ValueError('节拍激活缓存超过局部复核读取上限。')
    if hashlib.sha256(path.read_bytes()).hexdigest()!=expected:
        raise ValueError('当前激活与原节拍结果不匹配；保留编辑结果，请先核对分析版本。')
    return path,expected,evidence.get('activation_identity')


def build_candidate(prepared, decoded):
    """Merge only signatures; constrain both edges and reject invalid paths."""
    original=prepared['tempo'];constraints=prepared['constraints']
    seconds=constraints['beat_seconds'];positions=prepared['quarter_positions']
    if decoded.get('decoder')!='constrained_bar_dbn' or decoded.get('beat_seconds')!=seconds:
        raise ValueError('局部解码改变了固定拍点，候选未应用。')
    numbers,meters=decoded.get('beat_numbers',[]),decoded.get('meters',[])
    if len(numbers)!=len(seconds) or len(meters)!=len(seconds):
        raise ValueError('局部解码未覆盖完整范围。')
    for i,(number,meter) in enumerate(zip(numbers,meters)):
        if type(number) is not int or type(meter) is not int or meter not in (3,4) or not 1<=number<=meter:
            raise ValueError('解码返回无效拍号／拍位。')
        if i and (number!=numbers[i-1]%meters[i-1]+1 or (meter!=meters[i-1] and number!=1)):
            raise ValueError('解码路径在小节内部跳转，候选未应用。')
    for index,name in ((0,'start'),(-1,'end')):
        if {'beat_number':numbers[index],'meter':meters[index]}!=constraints['boundary_states'][name]:
            raise ValueError('解码与选区边界冲突，范围外分组不会被移动。')
    for point in constraints['downbeats']:
        index=seconds.index(point['seconds'])
        if numbers[index]!=1 or ('meter' in point and meters[index]!=point['meter']):
            raise ValueError('解码未满足确认强拍，候选未应用。')
    left,right=positions[0],positions[-1]
    rows=[deepcopy(r) for r in original['time_signatures'] if r['beat']<=left+EPS]
    for q,n,m in zip(positions,numbers,meters):
        if q>=right-EPS:break
        if n==1:
            phase=_phase(dict(original,time_signatures=rows),q)
            if phase!={'beat_number':1,'meter':m}:
                rows=[r for r in rows if abs(r['beat']-q)>EPS]
                rows.append({'beat':q,'numerator':m,'denominator':4,'origin':'confirmed_bar_decode'})
    # At the right edge we must already have the original phase, without a
    # fabricated signature reset in the middle of its measure.
    if _phase(dict(original,time_signatures=rows),right)!=constraints['boundary_states']['end']:
        raise ValueError('候选无法保持右边界相位，未修改工程。')
    rows.extend(deepcopy(r) for r in original['time_signatures'] if r['beat']>=right-EPS)
    rows=sorted({r['beat']:r for r in rows}.values(),key=lambda r:r['beat'])
    metadata={k:deepcopy(decoded.get(k)) for k in ('decoder','constraint_decoder_version',
              'activation_sha256','activation_identity','log_likelihood','unconstrained_log_likelihood',
              'constraint_diagnostics','probability_source','manual_constraint_decoder')}
    operation={'type':'bar_phase_candidate','time_signatures':rows,'analysis':metadata}
    tempo=deepcopy(original);tempo['time_signatures']=rows
    TempoMap(tempo)
    key=lambda row:(row['beat'],row['numerator'],row['denominator'])
    old_rows={key(r) for r in original['time_signatures']};new_rows={key(r) for r in rows}
    clock=TempoMap(original)
    changes=[f"{clock.to_seconds(q):.3f} 秒起 {n}/{d}" for q,n,d in sorted(new_rows-old_rows)]
    changes.extend(f"移除 {clock.to_seconds(q):.3f} 秒的原 {n}/{d} 分组标记" for q,n,d in sorted(old_rows-new_rows))
    detail='；'.join(changes) if changes else '分组与当前结果一致'
    return {'operation':operation,'tempo':tempo,'summary':f'候选：{detail}。范围 {seconds[0]:.3f}–{seconds[-1]:.3f} 秒；固定两端相位，速度、音符与 CC64 不变。',
            'warnings':list(decoded.get('warnings',[]))+['此操作不补漏拍、不改变拍数或半倍速解释；若这些仍有误，请先修正拍位。']}


def run(prepared):
    """Thread worker: no edits, no model inference, no user paths in decoder."""
    from .active_pulse_evidence import is_native, resolve
    if is_native(prepared['tempo']):
        from .manual_pulse_decode import decode_evidence
        evidence=resolve(prepared['project'],tempo=prepared['tempo'],load_probabilities=True)
        return build_candidate(prepared,decode_evidence(evidence,prepared['constraints']))
    from .rhythm_decode import decode_cached_activations
    path,digest,identity=_activation_file(prepared['project'])
    decoded=decode_cached_activations(path,constraints=prepared['constraints'])
    if decoded.get('activation_sha256')!=digest or decoded.get('activation_identity')!=identity:
        raise ValueError('解码期间激活缓存已变化，候选未应用；请重新预览。')
    return build_candidate(prepared,decoded)
