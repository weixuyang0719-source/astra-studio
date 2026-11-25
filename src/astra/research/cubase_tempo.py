"""Read Cubase 16 step-tempo SMT exports without executing Cubase components.

Research-only adapter, not wired to the product's automatic or import workflow.
User requests pulse algorithms, not adoption of host tempo/signature repairs.
The installed host's five exported fixtures establish 480 ticks/quarter. Meter
geometry is checked whenever available. Unsupported curve fields are rejected,
never silently flattened. File/project zero must match the source audio zero.
"""
import hashlib
import math
from pathlib import Path
import xml.etree.ElementTree as ET

from ..timing import TempoMap

PPQ = 480.


def import_cubase_tempo(path, duration=1.):
    path=Path(path)
    if path.stat().st_size>16*1024*1024:raise ValueError('SMT 速度图超过读取上限。')
    raw=path.read_bytes()
    if b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper():
        raise ValueError('SMT 不能包含外部定义。')
    try:root=ET.fromstring(raw)
    except ET.ParseError as exc:raise ValueError('无法读取 Cubase SMT XML。') from exc
    if root.tag!='MasterTrack':raise ValueError('不是受支持的 Cubase SMT 速度图。')
    tempos=[];meters=[]
    tempo_tracks=[o for o in root.findall('obj') if o.get('class')=='MTempoTrackEvent']
    signature_tracks=[o for o in root.findall('obj') if o.get('class')=='MSignatureTrackEvent']
    if len(tempo_tracks)!=1 or len(signature_tracks)!=1:
        raise ValueError('SMT 需包含唯一速度轨与拍号轨。')

    def fields(obj):
        values={}
        for e in obj:
            name=e.get('name')
            if name in values:raise ValueError('SMT 事件包含重复字段。')
            values[name]=e.get('value')
        return values

    def number(value):
        try:n=float(value)
        except (TypeError,ValueError):raise ValueError('SMT 数值无效。') from None
        if not math.isfinite(n):raise ValueError('SMT 数值必须有限。')
        return n

    for obj in tempo_tracks[0].findall("list[@name='TempoEvent']/obj"):
        if obj.get('class')!='MTempoEvent':raise ValueError('不支持的 SMT 速度事件类型。')
        f=fields(obj)
        if set(f)!={'BPM','PPQ'}:
            raise ValueError('此 SMT 含未核实的速度曲线字段；请从 Cubase 导出带速度的 MIDI，不能直接按阶梯速度读取。')
        q,bpm=number(f['PPQ'])/PPQ,number(f['BPM'])
        if q<0 or bpm<=0:raise ValueError('SMT 速度与位置无效。')
        tempos.append((q,bpm))
    for obj in signature_tracks[0].findall("list[@name='SignatureEvent']/obj"):
        if obj.get('class')!='MTimeSignatureEvent':raise ValueError('不支持的 SMT 拍号事件类型。')
        f=fields(obj)
        try:
            row={'beat':number(f['Position'])/PPQ,'numerator':int(f['Numerator']),
                 'denominator':int(f['Denominator']),'bar':int(f['Bar'])}
        except (KeyError,ValueError,TypeError):raise ValueError('SMT 拍号字段不完整或无效。') from None
        meters.append(row)
    tempos.sort();meters.sort(key=lambda r:r['beat'])
    if not tempos or tempos[0][0]!=0 or any(b[0]<=a[0] for a,b in zip(tempos,tempos[1:])):
        raise ValueError('SMT 速度需从零开始且不能包含同位置冲突。')
    if not meters or meters[0]['beat']!=0:raise ValueError('SMT 拍号需从零开始。')
    implied=[]
    for a,b in zip(meters,meters[1:]):
        if a['denominator']<=0:raise ValueError('SMT 拍号分母无效。')
        quarters=(b['bar']-a['bar'])*a['numerator']*4/a['denominator']
        if quarters<=0:raise ValueError('SMT 小节编号必须递增。')
        estimate=(b['beat']-a['beat'])*PPQ/quarters
        if abs(estimate-PPQ)>.001:
            raise ValueError('SMT 小节几何不符合已核实的 480 PPQ；请改用 MIDI 速度图，避免错读拍位。')
        implied.append(estimate)
    anchors=[{'seconds':0.,'beat':0.}]
    for (a,bpm),(b,_) in zip(tempos,tempos[1:]):
        anchors.append({'seconds':anchors[-1]['seconds']+(b-a)*60/bpm,'beat':b})
    end=max(number(duration),anchors[-1]['seconds']+1.)
    anchors.append({'seconds':end,'beat':anchors[-1]['beat']+(end-anchors[-1]['seconds'])*tempos[-1][1]/60})
    data={'version':1,'source':'cubase_smt','anchors':anchors,
          'time_signatures':[{k:r[k] for k in ('beat','numerator','denominator')} for r in meters],
          'allow_partial_measures':True,'warnings':[
              '已采用 Cubase 导出速度图；要求 Cubase 工程零点与本曲音频零点一致。此导入不会重新检测或自动纠正宿主拍号。'],
          'cubase_import':{'file':str(path.resolve()),'sha256':hashlib.sha256(raw).hexdigest(),
              'ticks_per_quarter':PPQ,'verified_signature_geometry':implied,'curve':'step',
              'assumed_audio_start_seconds':0.}}
    TempoMap(data)
    return data
