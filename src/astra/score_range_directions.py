"""Conservative source-direction and current-sustain projection for local drafts.

Directions are located in source seconds, never attached to newly guessed notes.
Returned absolute beats include a possible right-edge pedal stop at ``hi``;
the caller must insert that at the end of its final measure, not discard it.
"""
from copy import deepcopy
from fractions import Fraction
import math
import xml.etree.ElementTree as ET
from .score_tempo import _measure_layout


def _clean(direction):
    node=deepcopy(direction)
    for off in list(node.findall('offset')):node.remove(off)
    # Source layout coordinates are not meaningful after local reflow.
    for child in node.iter():
        for attr in ('default-x','default-y','relative-x','relative-y'):
            child.attrib.pop(attr,None)
    return node


def _only(direction, tags):
    node=_clean(direction)
    for group in list(node.findall('direction-type')):
        for child in list(group):
            if child.tag not in tags:group.remove(child)
        if not len(group):node.remove(group)
    # Old playback tempo/controller commands must not override the new map.
    for sound in list(node.findall('sound')):node.remove(sound)
    return node


def _pedal(kind):
    node=ET.Element('direction',{'placement':'below'})
    dtype=ET.SubElement(node,'direction-type')
    ET.SubElement(dtype,'pedal',{'type':kind,'line':'yes','sign':'no'})
    ET.SubElement(node,'staff').text='2'
    return node


def collect_directions(xml,base_clock,current_clock,lo,hi,doc):
    """Return directions, warnings, and boundary audit; never modify inputs.

The initial supported point vocabulary is words/dynamics/rehearsal. Timed
wedge spans fully inside the range are retained; unknown/cropped spans are
reported rather than guessed. CC64 values
use MIDI's standard >=64 sustain threshold; half-pedal detail is disclosed.
"""
    root=ET.parse(xml).getroot()
    if len(root.findall('part'))!=1:
        raise ValueError('局部编辑草稿暂不支持多乐器总谱，请查看原正式谱。')
    result=[];warnings=[];previous_dynamics={};source_pedals=[]
    point_tags={'words','dynamics','rehearsal'}
    wedges={}
    for _,measure,start,_,division in _measure_layout(root):
        cursor=Fraction(0)
        for child in measure:
            if child.tag=='backup':cursor-=Fraction(child.findtext('duration','0'))/division
            elif child.tag=='forward':cursor+=Fraction(child.findtext('duration','0'))/division
            elif child.tag=='note' and child.find('chord') is None:
                cursor+=Fraction(child.findtext('duration','0'))/division
            elif child.tag=='direction':
                if child.get('id','').startswith(('astra-reference-tempo-','astra-user-rhythm-')):continue
                position=start+cursor+Fraction(child.findtext('offset','0'))/division
                q=current_clock.to_beat(base_clock.to_seconds(float(position)))
                tags={n.tag for n in child.findall('direction-type/*')}
                if tags & point_tags:
                    node=_only(child,point_tags)
                    if lo<=q<hi:result.append((q,node))
                    elif q<lo and 'dynamics' in tags:
                        key=(child.findtext('staff','1'),child.findtext('voice',''))
                        if key not in previous_dynamics or q>=previous_dynamics[key][0]:
                            previous_dynamics[key]=(q,_only(child,{'dynamics'}))
                for pedal in child.findall('direction-type/pedal'):
                    source_pedals.append((q,pedal.get('type'),child))
                for wedge in child.findall('direction-type/wedge'):
                    key=(child.findtext('staff','1'),wedge.get('number','1'))
                    node=_only(child,{'wedge'})
                    for group in list(node.findall('direction-type')):node.remove(group)
                    group=ET.Element('direction-type');group.append(deepcopy(wedge));node.insert(0,group)
                    wedges.setdefault(key,[]).append((q,wedge.get('type'),node))
                unsupported=tags-point_tags-{'pedal','metronome','wedge'}
                if unsupported and q<hi:
                    # A span starting before the range may still be active.
                    warnings.append('原谱含跨度或特殊演奏标记（'+', '.join(sorted(unsupported))+'），局部草稿尚未完整重建，请以正式谱核对。')
                if 'metronome' in tags and lo<=q<hi:
                    warnings.append('原谱速度文字未直接复制；局部速度标记由当前拍位设置生成。')
    for key,(_,node) in previous_dynamics.items():
        # A new dynamic exactly at the boundary supersedes inherited state.
        if not any(abs(q-lo)<1e-9 and n.find('direction-type/dynamics') is not None
                   and (n.findtext('staff','1'),n.findtext('voice',''))==key for q,n in result):
            result.append((lo,node))
    for key,entries in wedges.items():
        pending=None;pairs=[];invalid=False
        for q,kind,node in sorted(entries,key=lambda item:item[0]):
            if kind in ('crescendo','diminuendo') and pending is None:
                pending=(q,node)
            elif kind=='stop' and pending is not None and q>pending[0]:
                pairs.append((pending,(q,node)));pending=None
            else:invalid=True
        if pending is not None:invalid=True
        if invalid:
            if any(q<hi for q,_,_ in entries):warnings.append('原谱渐强渐弱标记未能可靠配对，局部草稿暂未重建，请以正式谱核对。')
            continue
        for begin,stop in pairs:
            if lo<=begin[0]<stop[0]<=hi:result.extend((begin,stop))
            elif begin[0]<hi and stop[0]>lo:
                warnings.append('渐强渐弱跨出预览边界，局部草稿暂未裁切该标记，请扩大范围查看。')
    inherited=False;clipped=False
    if 'controls' in doc:
        controls=[c for c in doc.get('controls',[]) if c.get('number')==64]
        relevant=[c for c in controls if current_clock.to_beat(c['seconds'])<=hi]
        channels={c.get('channel',0) for c in relevant}
        if len(channels)>1:
            warnings.append('多个 MIDI 通道的踏板无法可靠对应到谱表，局部草稿暂不合并踏板线。')
        else:
            state=False;events=[]
            for _,control in sorted(enumerate(controls),key=lambda row:(row[1]['seconds'],row[0])):
                seconds=float(control['seconds']);value=float(control['value'])
                if not math.isfinite(seconds) or not math.isfinite(value):raise ValueError('踏板事件包含无效数值。')
                q=current_clock.to_beat(seconds)
                if q>hi:break
                if value not in (0,127):warnings.append('踏板线按 CC64 开关状态显示，未表示半踏板深度。')
                down=value>=64
                if q<lo:state=down
                else:events.append((q,down))
            boundary_events=[down for q,down in events if q==lo]
            if boundary_events:
                state=boundary_events[-1]
                if state:result.append((lo,_pedal('start')))
            elif state:result.append((lo,_pedal('start')));inherited=True
            for q,down in events:
                if q==lo:continue
                if down==state:continue
                # At the right boundary only a release belongs to this crop.
                if q==hi and down:continue
                result.append((q,_pedal('start' if down else 'stop')));state=down
            if state:
                result.append((hi,_pedal('stop')));clipped=True
    elif source_pedals:
        # No authoritative controller track: preserve only a simple, single
        # sustain-line stream. Complex overlapping pedal numbering is unknown.
        if any(kind not in ('start','stop','change') or
               node.find('direction-type/pedal').get('number','1')!='1'
               for _,kind,node in source_pedals):
            warnings.append('原谱踏板使用复杂标记，局部草稿暂未重建，请以正式谱核对。')
        else:
            state=False
            for q,kind,node in sorted(source_pedals,key=lambda row:row[0]):
                if q<lo:state=kind!='stop'
            if state:result.append((lo,_pedal('start')));inherited=True
            for q,kind,node in sorted(source_pedals,key=lambda row:row[0]):
                if not lo<=q<=hi:continue
                if q==hi and kind!='stop':continue
                result.append((q,_only(node,{'pedal'})));state=kind!='stop'
            if state:result.append((hi,_pedal('stop')));clipped=True
    if clipped:warnings.append('踏板延续到预览范围之外，末端踏板线仅为片段显示收束，不代表原曲松踏板。')
    return {'directions':sorted(result,key=lambda item:item[0]),
            'warnings':list(dict.fromkeys(warnings)),
            'pedal_boundary':{'inherited':inherited,'clipped_at_right':clipped,
                              'source':'current_cc64' if 'controls' in doc else 'formal_notation'}}
