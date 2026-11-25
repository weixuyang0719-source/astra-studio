"""Use DuET's pinned rhythm layout for a local single-staff event sweep."""
from .duet_score import duet_source_dir
from collections import defaultdict
from fractions import Fraction
from functools import lru_cache
import importlib.util
from pathlib import Path
import xml.etree.ElementTree as ET

from .notation_single_voice import _note, _grace_and_tuplets, _pitch_key


@lru_cache(maxsize=1)
def _native():
    path=duet_source_dir()/'src/duet/readable/notation.py'
    spec=importlib.util.spec_from_file_location('astra_duet_notation_rhythm',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


def range_replacement(group,staff,divisions,compound=False):
    """Keep exact intervals; native regions/layout/beam decide written rhythm.

    No voice reassignment, model inference or nearest-value fallback occurs.
    music21 only resolves an exact tiny value beyond the native type table.
    """
    native=_native();length=max(s.cursor for s in group)
    events=[e for s in group for e in s.events]
    pitched=[e for e in events if e.node.find('pitch') is not None and e.end>e.start]
    voice='1' if staff=='1' else '5'
    grace,_,_,_=_grace_and_tuplets(group,voice)
    markers=defaultdict(list)
    for stream in group:
        for position,node in stream.markers:markers[position].append(node)
    bounds=sorted({0,length,*markers,*grace,*(e.start for e in pitched),*(e.end for e in pitched)})
    items=[]
    for a,b in zip(bounds,bounds[1:]):
        active=[e for e in pitched if e.start<=a<e.end]
        if len({_pitch_key(e) for e in active})!=len(active):raise ValueError('局部谱表同音重叠，无法安全合并。')
        items.append((Fraction(a,divisions),Fraction(b,divisions),active or None))
    pieces=native.layout_voice(items,Fraction(length,divisions),compound=compound,
        extra_cuts=[Fraction(k,divisions) for k in markers])
    from .notation_readability import compact_dotted
    pieces,_=compact_dotted(pieces,Fraction(length,divisions),compound=compound,
                           cuts=[Fraction(k,divisions) for k in markers])
    native.beam(pieces,Fraction(length,divisions),compound)
    regions=defaultdict(list)
    for index,pc in enumerate(pieces):
        if pc['reg'] is not None:regions[pc['reg']].append(index)
    result=[];emitted=set()
    for index,pc in enumerate(pieces):
        a=pc['s']*divisions;b=(pc['s']+pc['d'])*divisions
        if a.denominator!=1 or b.denominator!=1:raise ValueError('局部节奏网格无法精确表达。')
        a,b=int(a),int(b)
        if a not in emitted:
            result.extend(markers.get(a,()));result.extend(grace.get(a,()));emitted.add(a)
        reg=pc['reg'];normal_value=pc['d']*(Fraction(3,2) if reg else 1)
        name,dots=native.type_of(pc['d'],reg is not None)
        value=next((v for v,n in native.TYPES.items() if n==name),None)
        if value*(Fraction(3,2) if dots else 1)!=normal_value:
            from music21.duration import Duration
            duration=Duration(normal_value)
            if duration.type=='inexpressible' or duration.tuplets or len(duration.components)!=1:
                raise ValueError('选区包含无法常规记谱的不齐部分小节；请将选区缩至相邻完整小节。原谱和编辑保留，不近似时值。')
            name,dots=duration.type,duration.dots
        tm=None;tuplets=[]
        if reg:
            tm=ET.Element('time-modification');ET.SubElement(tm,'actual-notes').text='3';ET.SubElement(tm,'normal-notes').text='2'
            ET.SubElement(tm,'normal-type').text=native.type_of(reg[2],True)[0]
            indices=regions[reg]
            if index==indices[0]:tuplets.append(ET.Element('tuplet',{'type':'start','number':'1','bracket':'yes'}))
            if index==indices[-1]:tuplets.append(ET.Element('tuplet',{'type':'stop','number':'1'}))
        for j,event in enumerate(pc['pl'] or [None]):
            node=_note(event,a,b,(name,dots),staff,voice,bool(j),tm,tuplets if j==0 else ())
            if event is not None and j==0:
                for level,text in pc.get('beam',{}).items():
                    beam=ET.Element('beam',{'number':str(level)});beam.text=text
                    at=next((k for k,c in enumerate(node) if c.tag=='notations'),len(node));node.insert(at,beam)
            result.append(node)
    result.extend(markers.get(length,()))
    if length in grace:raise ValueError('片段末倚音缺少附属音符，请增加上下文。')
    return result
