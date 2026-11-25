"""Explicit user rhythm expressions; visual directions never synthesize tempo."""
from fractions import Fraction
import xml.etree.ElementTree as ET
from .timing import TempoMap

PREFIX='astra-user-rhythm-'

def apply_annotations(root, reference, *, beat_offset=0):
    from .score_tempo import _measure_layout
    for measure in root.findall('.//measure'):
        for child in list(measure.findall('direction')):
            if child.get('id','').startswith(PREFIX): measure.remove(child)
    layout=_measure_layout(root)
    if not layout:return {'expressions':[], 'short_bars':[]}
    bars=[r for r in layout if r[0] is layout[0][0]]
    clock=TempoMap(reference);audit={'expressions':[], 'short_bars':[], 'warnings':[]}
    def direction(seconds, serial, text=None, dash=None, number=1):
        beat=clock.to_beat(float(seconds));q=Fraction(str(beat))-Fraction(str(beat_offset))
        row=next((r for r in reversed(bars) if r[2]<=q<r[2]+r[3]),None)
        if row is None and abs(float(q-bars[-1][2]-bars[-1][3]))<1e-7:row=bars[-1]
        if row is None:return None
        _,measure,start,length,divisions=row
        d=ET.Element('direction',{'placement':'above','id':PREFIX+serial})
        typ=ET.SubElement(d,'direction-type')
        if text:ET.SubElement(typ,'words',{'font-style':'italic'}).text=text
        if dash:
            if text:typ=ET.SubElement(d,'direction-type')
            ET.SubElement(typ,'dashes',{'type':dash,'number':str(number)})
        units=round((q-start)*divisions)
        if units:ET.SubElement(d,'offset',{'sound':'no'}).text=str(units)
        ET.SubElement(d,'staff').text='1'
        measure.insert(1 if measure.find('attributes') is not None else 0,d)
        return {'seconds':seconds,'beat':beat,'measure':measure.get('number'),'rounding_quarters':abs(float(q-start)-units/divisions)}
    for i,a in enumerate(reference.get('tempo_expression_annotations',[])):
        if a.get('direction') not in ('accelerando','ritardando'):raise ValueError('Unknown explicit tempo expression')
        start,end=float(a['start_seconds']),float(a['end_seconds'])
        if not 0<=start<end<=clock.seconds[-1]:raise ValueError('Tempo expression range is outside the score clock')
        x=direction(start,f'expression-{i}-start','accel.' if a['direction']=='accelerando' else 'rit.','start',i+1)
        y=direction(end,f'expression-{i}-stop',dash='stop',number=i+1)
        if x is None or y is None:audit['warnings'].append('速度表达范围超出当前有谱面的区段，未能完整印出起止标记；原速度图未改变。')
        audit['expressions'].append({'start':x,'end':y,'direction':a['direction'],'playback_changed':False})
    for i,a in enumerate(reference.get('short_bar_annotations',[])):
        start,end=clock.to_beat(a['start_seconds']),clock.to_beat(a['end_seconds'])
        for _,measure,q,length,_ in layout:
            if abs(float(q)+beat_offset-start)<1e-7 and abs(float(q+length)+beat_offset-end)<1e-7:
                measure.set('implicit','yes')
        x=direction(a['start_seconds'],f'short-{i}','short bar')
        if x is None:audit['warnings'].append('短小节位于当前谱面范围之外，标记未印出；请检查导出范围。')
        audit['short_bars'].append({'start':x,'end_seconds':a['end_seconds'],'playback_changed':False})
    from .free_meter import free_ranges
    from .score_origin import score_opening_meter_rows
    keep=reference.get('opening_policy', {}).get('keep_before_bar1')
    signatures=score_opening_meter_rows(reference)
    free=free_ranges(dict(reference,time_signatures=signatures))
    if keep is False:
        first=float(reference.get('first_full_bar_beat',0))
        free=[a for a in free if a['start_beat']>=first-1e-7]
    audit['free_measures']=[]
    for a in free:
        start,end=a['start_beat'],a['end_beat']
        for _,measure,q,length,_ in layout:
            inside=start-1e-7<=float(q)+beat_offset<end-1e-7
            restore=abs(float(q)+beat_offset-end)<1e-7
            if not inside and not restore:continue
            attrs=measure.find('attributes')
            if attrs is None:attrs=ET.Element('attributes');measure.insert(0,attrs)
            for time in list(attrs.findall('time')):attrs.remove(time)
            # MusicXML attributes require time before staves and clefs.
            position=next((i for i,child in enumerate(attrs)
                           if child.tag not in ('footnote','level','divisions','key')),len(attrs))
            time=ET.Element('time')
            attrs.insert(position,time)
            if inside:
                ET.SubElement(time,'senza-misura')
                measure.set('implicit','yes')
            else:
                meter=next(r for r in reversed(signatures) if r['beat']<=end+1e-7)
                ET.SubElement(time,'beats').text=str(meter['numerator'])
                ET.SubElement(time,'beat-type').text=str(meter['denominator'])
        audit['free_measures'].append(dict(a,playback_changed=False))
    return audit
