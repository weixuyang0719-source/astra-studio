"""Retain only provably mapped, wholly visible source slur pairs."""
from copy import deepcopy
from fractions import Fraction
import xml.etree.ElementTree as ET
from .score_tempo import _measure_layout
from .notation_single_voice import _streams
from .notation_readability import _sounding_pitch


def restore_slurs(xml, events):
    active={};pairs=[];unresolved=0
    for _,measure,start,_,division in _measure_layout(ET.parse(xml).getroot()):
        streams,_=_streams(measure,division)
        endpoints=[]
        for stream in streams:
            for event in stream.events:
                node=event.node
                if node.find('pitch') is None:continue
                for mark in node.findall('notations/slur'):
                    endpoints.append((start+Fraction(event.start,division),node,deepcopy(mark)))
        # Keep stable source order for coincident stop/start on the same number.
        for onset,node,mark in sorted(endpoints,key=lambda x:x[0]):
            number=mark.get('number','1');kind=mark.get('type')
            endpoint=(float(onset),node.findtext('staff','1'),_sounding_pitch(node.find('pitch')))
            if kind=='start':
                if number in active:unresolved+=1
                active[number]=(endpoint,mark)
            elif kind=='stop':
                prior=active.pop(number,None)
                if prior:pairs.append((prior[0],endpoint,prior[1],mark))
                else:unresolved+=1
            elif kind!='continue':unresolved+=1
    unresolved+=len(active)
    def mapped(endpoint):
        onset,staff,pitch=endpoint;found=[]
        for event in events:
            prior=event.get('prior')
            if not prior or str(prior['staff'])!=staff or tuple(prior['key'][2:])!=tuple(pitch):continue
            if prior['start']-1e-8<=onset<prior['start']+max(prior['duration'],1e-7)-1e-9:found.append(event)
        return found[0] if len(found)==1 else None
    count=0;omitted=0
    for left,right,start_mark,stop_mark in pairs:
        a,b=mapped(left),mapped(right)
        if a is None or b is None or a.get('clipped') or b.get('clipped'):
            omitted+=1;continue
        if a['start']>b['start'] or a['pitch']!=a['prior']['pitch'] or b['pitch']!=b['prior']['pitch']:
            unresolved+=1;continue
        for event,mark in ((a,start_mark),(b,stop_mark)):
            # Old absolute layout coordinates do not survive re-engraving.
            for attr in ('default-x','default-y','relative-x','relative-y'):mark.attrib.pop(attr,None)
            notation=event['node'].find('notations')
            if notation is None:notation=ET.SubElement(event['node'],'notations')
            notation.append(mark)
        count+=1
    return {'retained_pairs':count,'omitted_boundary_or_unmapped_pairs':omitted,
            'warnings':['部分原谱连线端点无法可靠配对，已省略该连线。'] if unresolved else []}


def clip_unpaired_slurs(root):
    """Remove fragment-edge slurs only; never touch notes or sounding ties."""
    for part in root.findall('part'):
        active={};keep=set();marks=[]
        for notation in part.findall('.//notations'):
            for mark in notation.findall('slur'):
                marks.append((notation,mark));number=mark.get('number','1');kind=mark.get('type')
                if kind=='start':active[number]=[mark]
                elif kind=='continue' and number in active:active[number].append(mark)
                elif kind=='stop' and number in active:
                    keep.update(active.pop(number));keep.add(mark)
        for parent,mark in marks:
            if mark not in keep:parent.remove(mark)
