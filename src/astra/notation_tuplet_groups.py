"""Make already complete generated 3:2 runs explicit for the native importer.

An implicit run can close too soon when its smallest written note changes;
MuseScore then appends a rest to the incomplete tail and invalidates the bar.
Only regroup an exact, complete run; never change notes, durations or meter.
"""
from fractions import Fraction
from io import BytesIO
from pathlib import Path
from xml.etree import ElementTree as ET

from .notation_readability import sounding_signature
from .notation_single_voice import _streams, _identity, _TYPES


_VALUES={name:value for value,name in _TYPES.items()}


def _nominal(note, duration, divisions):
    modification=note.find('time-modification')
    value=_VALUES.get(note.findtext('type'))
    dots=len(note.findall('dot'))
    if (note.find('grace') is not None or duration<=0 or modification is None
            or value is None or dots>2
            or [(v.tag,v.text) for v in modification]!=[('actual-notes','3'),('normal-notes','2')]):
        return None
    unit=value/Fraction(2**dots)
    written=unit*(2**(dots+1)-1)
    if written*Fraction(2,3)!=Fraction(duration,divisions):
        return None
    return written,unit


def _needs_group(run):
    if len(run)<2 or len({unit for _,_,unit in run})<2:
        return False
    count=sum(written for _,written,_ in run)/min(unit for _,_,unit in run)
    if count.denominator!=1 or count<6 or count%3:
        return False # a genuinely incomplete group is not repaired here
    covered=Fraction(0)
    smallest=None
    for _,written,unit in run:
        covered+=written
        smallest=unit if smallest is None else min(smallest,unit)
        # Match native implicit completion, including when a new smaller
        # value makes the group overshoot its original cutoff.
        if covered>=3*smallest:
            covered=Fraction(0)
            smallest=None
    return covered>0


def _lane_groups(events, divisions):
    chords=[]
    for event in events:
        if event.node.find('chord') is None:
            chords.append((event.start,event.end,[event.node]))
        elif chords and chords[-1][:2]==(event.start,event.end):
            chords[-1][2].append(event.node)
        else:
            return []
    opened=set()
    run=[]; result=[]; end=None
    def finish():
        if _needs_group(run):
            result.append((run[0][0],run[-1][0]))
        run.clear()
    for start,stop,nodes in chords:
        markers=[marker for n in nodes for marker in n.findall('notations/tuplet')]
        if markers or opened:
            finish()
            for marker in markers:
                kind=marker.get('type'); number=marker.get('number','1')
                if kind=='start' and number not in opened:opened.add(number)
                elif kind=='stop' and number in opened:opened.remove(number)
                else:return [] # leave existing unusual/ambiguous markings untouched
            continue
        value=_nominal(nodes[0],stop-start,divisions)
        if value is None or any(_nominal(note,stop-start,divisions)!=value for note in nodes[1:]):
            finish()
            continue
        if start!=end:finish()
        run.append((nodes[0],*value));end=stop
    finish()
    return [] if opened else result


def disambiguate_generated_triplets(path):
    """Return number of added explicit groups. Only pass app-generated XML."""
    path=Path(path)
    original=path.read_bytes()
    tree=ET.fromstring(original)
    edits=[]
    for part in tree.findall('part'):
        divisions=1; meter=Fraction(4)
        for measure in part.findall('measure'):
            time=measure.find('attributes/time')
            if time is not None:
                try:meter=4*Fraction(int(time.findtext('beats')),int(time.findtext('beat-type')))
                except (TypeError,ValueError,ZeroDivisionError):meter=None
            try:streams, divisions=_streams(measure,divisions)
            except (TypeError,ValueError):continue
            if meter is None or measure.get('implicit')=='yes':
                continue
            # An actual pickup/short/free measure is a different problem. A
            # normalized lane must already end on the nominal meter boundary.
            # Tempo-only synthetic forward/backup streams have no note events.
            lanes=[s for s in streams if s.events]
            expected=meter*divisions
            if (not lanes or expected.denominator!=1
                    or any(s.cursor!=expected or len(_identity(s))!=1 for s in lanes)):
                continue
            edits.extend(group for lane in lanes for group in _lane_groups(lane.events,divisions))
    if not edits:return 0
    for first,last in edits:
        for kind,note in [('start',first),('stop',last)]:
            notation=note.find('notations')
            if notation is None:notation=ET.SubElement(note,'notations')
            attrs={'type':kind,'number':'1'}
            if kind=='start':attrs.update(bracket='no',**{'show-number':'none'})
            ET.SubElement(notation,'tuplet',attrs)
    encoded=ET.tostring(tree,encoding='utf-8',xml_declaration=True)
    if sounding_signature(BytesIO(original))!=sounding_signature(BytesIO(encoded)):
        raise RuntimeError('连音组兼容处理改变了音符时间，已停止导出。')
    path.write_bytes(encoded)
    return len(edits)

