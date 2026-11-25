"""Map audio-model Harte chords into a verified notation clock, without inference.

XML ownership: only harmony IDs beginning ``astra-harmony-`` or
``astra-audio-harmony-`` are replaceable. Unmarked author symbols are retained.
Legacy generated/unmarked symbols must be removed by the caller using its known
provenance, or the caller must provide a clean, unannotated score.
"""
from bisect import bisect_right
from copy import deepcopy
from fractions import Fraction
import math
from pathlib import Path
import tempfile
import xml.etree.ElementTree as ET

from .timing import TempoMap

OWNED_PREFIXES=('astra-harmony-','astra-audio-harmony-')


def _harte_symbol(label):
    """Use upstream Harte grammar and music21 naming, checking pitch preservation."""
    from harte.harte import Harte
    from music21 import harmony
    from music21.musicxml.m21ToXml import MeasureExporter
    parsed=Harte(label)
    if not parsed.pitches:raise ValueError('Empty Harte chord')
    symbol=harmony.chordSymbolFromChord(parsed)
    if ('Cannot' in symbol.figure or not symbol.chordKind
            or set(symbol.pitchClasses)!=set(parsed.pitchClasses)
            or symbol.root().pitchClass!=parsed.root().pitchClass
            or symbol.bass().pitchClass!=parsed.bass().pitchClass):
        raise ValueError('music21 cannot preserve this Harte chord as a chord symbol')
    node=MeasureExporter().chordSymbolToXml(symbol,append=False)
    if isinstance(node,list):node=node[0]
    # music21 has an internal 7sus kind that is not a MusicXML kind-value.
    # Encode the same tones using the standard dominant seventh with a suspension,
    # otherwise renderers print the long internal name and can clip the page.
    kind=node.find('kind')
    if kind is not None and kind.text=='suspended-fourth-seventh':
        kind.text='dominant'
        kind.set('text','7sus4')
        for value,operation in [('3','subtract'),('4','add')]:
            degree=ET.SubElement(node,'degree')
            ET.SubElement(degree,'degree-value').text=value
            ET.SubElement(degree,'degree-alter').text='0'
            ET.SubElement(degree,'degree-type').text=operation
    return symbol.figure,node,sorted(set(parsed.pitchClasses))


def _segments(segments):
    previous=-1.
    for index,segment in enumerate(segments):
        if isinstance(segment,dict):
            start=segment.get('start_seconds',segment.get('start'))
            end=segment.get('end_seconds',segment.get('end'))
            label=segment.get('harte',segment.get('label'))
        else:start,end,label=segment
        start,end=float(start),float(end)
        if not all(math.isfinite(v) for v in (start,end)) or start<0 or end<=start or start<previous-1e-7:
            raise ValueError('Audio chord segments must be finite, ordered, non-overlapping intervals')
        if not isinstance(label,str) or not label.strip():raise ValueError('Missing Harte chord label')
        previous=end
        yield index,start,end,label.strip()


def _alignment_mapper(alignment):
    """Return a bounded mapping. Explicit unverified alignment is an error."""
    if not alignment or alignment.get('verified') is not True:
        raise ValueError('Audio-to-score alignment is not verified; re-export notation first')
    anchors=alignment['anchors']
    seconds=[float(a['seconds']) for a in anchors]
    beats=[float(a['score_quarters']) for a in anchors]
    if len(anchors)<2 or any(not math.isfinite(x) or x<0 for x in seconds+beats):
        raise ValueError('Invalid audio-to-score anchors')
    if any(b<=a for a,b in zip(seconds,seconds[1:])) or any(b<=a for a,b in zip(beats,beats[1:])):
        raise ValueError('Audio-to-score anchors must be strictly increasing')
    def position(value):
        if value<seconds[0]-1e-8 or value>seconds[-1]+1e-8:return None
        return TempoMap._interpolate(max(seconds[0],min(seconds[-1],value)),seconds,beats)
    return position


def _xml_layout(path):
    from .score_tempo import _measure_layout
    tree=ET.parse(path);root=tree.getroot();layout=_measure_layout(root)
    if not layout:raise ValueError('MusicXML has no timed measures')
    primary=[item for item in layout if item[0] is layout[0][0]]
    return tree,layout,primary


def tag_legacy_generated_harmonies(musicxml,old_report):
    """Tag exact cached labels in caller-owned audio exports, never MIDI imports.

    The caller must establish source_kind=audio and export/report identity first.
    A matching symbol alone cannot prove authorship in arbitrary user files.
    Root/kind/inversion/bass/degrees and the exact measure offset must match.
    """
    method=str(old_report.get('method',''))
    if not (method.startswith('music21_') or method.startswith('audio_model_')):
        raise ValueError('Legacy harmony ownership is not established by this report')
    from music21 import harmony
    from music21.musicxml.m21ToXml import MeasureExporter
    from .notation_readability import sounding_signature
    path=Path(musicxml);before=sounding_signature(path)
    tree,layout,primary=_xml_layout(path)
    def semantic(node):
        result=[]
        for tag in ('root','kind','inversion','bass','degree','numeral'):
            for child in node.findall(tag):
                # Layout/text presentation is not the harmonic content.
                result.append((tag,(child.text or '').strip(),tuple((c.tag,(c.text or '').strip()) for c in child)))
        return tuple(result)
    expected={}
    for event in old_report.get('chords',[]):
        if event.get('uncertain',True):continue
        if 'measure_index' not in event or 'offset_quarters' not in event:continue
        try:
            node=MeasureExporter().chordSymbolToXml(harmony.ChordSymbol(event['label']),append=False)
            if isinstance(node,list):node=node[0]
        except Exception:continue
        expected.setdefault(int(event['measure_index']),[]).append((float(event['offset_quarters']),semantic(node)))
    count=0
    for index,(_,measure,_,_,divisions) in enumerate(primary):
        cursor=0.
        for node in measure:
            duration=float(node.findtext('duration','0'))/divisions
            if node.tag=='backup':cursor-=duration
            elif node.tag=='forward':cursor+=duration
            elif node.tag=='note' and node.find('chord') is None:cursor+=duration
            elif node.tag=='harmony' and not node.get('id'):
                position=cursor+float(node.findtext('offset','0'))/divisions
                matches=[item for item in expected.get(index,[]) if abs(item[0]-position)<1e-7 and item[1]==semantic(node)]
                if matches:
                    node.set('id',f'astra-harmony-legacy-{count}');count+=1
                    expected[index].remove(matches[0])
    if count:
        with tempfile.NamedTemporaryFile(dir=path.parent,suffix='.musicxml',delete=False) as file:temp=Path(file.name)
        try:
            tree.write(temp,encoding='utf-8',xml_declaration=True)
            if sounding_signature(temp)!=before:raise RuntimeError('Legacy ownership tagging changed sounding notes')
            temp.replace(path)
        finally:temp.unlink(missing_ok=True)
    return {'tagged_legacy_generated_symbols':count,'sounding_intervals_unchanged':True}


def _write_xml(path,tree,layout,primary,events,symbols,show):
    from .notation_readability import sounding_signature
    before=sounding_signature(path)
    root=tree.getroot();removed=0;inserted=0;preserved=0
    for measure in root.findall('.//measure'):
        for node in list(measure.findall('harmony')):
            if node.get('id','').startswith(OWNED_PREFIXES):measure.remove(node);removed+=1
            else:preserved+=1
    # Integer XML units are enlarged only as needed, retaining exact note values.
    # This supports quarter-beat labels even in scores originally using divisions=1.
    offsets=[Fraction(round(e['offset_quarters']*9600),9600) for e in events if e['measure_index'] is not None]
    existing_resolution=math.lcm(*(item[4] for item in layout))
    target=math.lcm(existing_resolution,*(v.denominator for v in offsets))
    # Exact reference-tempo anchors can already require large units. Retaining
    # those units is not a chord-induced expansion and must not block annotation.
    if target>max(10_000_000,existing_resolution):
        raise ValueError('Chord offset requires unreasonable XML resolution')
    if events:
        for _,measure,_,_,divisions in layout:
            if divisions==target:continue
            scale=Fraction(target,divisions)
            for child in measure.iter():
                if child.tag in ('duration','offset') and child.text:
                    value=Fraction(child.text)*scale
                    if value.denominator!=1:raise ValueError('Cannot retain exact MusicXML time units')
                    child.text=str(value.numerator)
            attrs=measure.find('attributes')
            if attrs is None:attrs=ET.Element('attributes');measure.insert(0,attrs)
            division=attrs.find('divisions')
            if division is None:division=ET.Element('divisions');attrs.insert(0,division)
            division.text=str(target)
    for event,node in zip(events,symbols):
        if event['uncertain']:
            event['notation_status']='timing_review';continue
        index=event['measure_index']
        if index is None:continue
        measure=primary[index][1]
        if any(not h.get('id','').startswith(OWNED_PREFIXES) for h in measure.findall('harmony')):
            event['notation_status']='author_harmony_preserved';continue
        element=deepcopy(node);element.set('id',f"astra-audio-harmony-{event['segment_index']}")
        if not show:element.set('print-object','no')
        offset=round(event['offset_quarters']*target)
        for old in list(element.findall('offset')):element.remove(old)
        if offset:ET.SubElement(element,'offset').text=str(offset)
        insertion=next((i for i,c in enumerate(measure) if c.tag in ('note','backup','forward')),len(measure))
        measure.insert(insertion,element);inserted+=1
        event['notation_status']='shown' if show else 'hidden'
    with tempfile.NamedTemporaryFile(dir=path.parent,suffix='.musicxml',delete=False) as file:temp=Path(file.name)
    try:
        tree.write(temp,encoding='utf-8',xml_declaration=True)
        if sounding_signature(temp)!=before:raise RuntimeError('Chord annotation changed sounding notes')
        temp.replace(path)
    finally:temp.unlink(missing_ok=True)
    return {'removed_owned_symbols':removed,'inserted_symbols':inserted,'preserved_author_symbols':preserved,
            'sounding_intervals_unchanged':True}


def apply_audio_harmony(report,segments,tempo_map,*,musicxml=None,show_chords=False,score_alignment=None):
    """Apply cached audio labels into the caller's bound notation clock.

    ``score_alignment``: {verified: True, source: str, anchors: [{seconds,
    score_quarters}, ...]}. No extrapolation outside its coverage. When omitted,
    the caller declares that the supplied TempoMap is the notation clock (legacy
    music21 exports). DuET callers should supply identity-verified alignment.
    Intersecting intervals are clipped to verified coverage for mapping only;
    source intervals are always retained in ``audio_harmony.segments``.
    """
    if type(show_chords) is not bool:raise ValueError('show_chords must be boolean')
    reference=TempoMap(deepcopy(tempo_map))
    if score_alignment is None:
        score_alignment={'verified':True,'source':'caller_reference_tempo_map',
            'anchors':[{'seconds':a['seconds'],'score_quarters':a['beat']} for a in reference.anchors]}
    mapper=_alignment_mapper(score_alignment)
    path=Path(musicxml) if musicxml is not None else None
    tree,layout,primary=_xml_layout(path) if path else (None,None,None)
    diagnostics=[];events=[];symbols=[];warnings=[]
    parsed_segments=list(_segments(segments))
    boundary_grid={}
    coverage_start=float(score_alignment['anchors'][0]['seconds'])
    coverage_end=float(score_alignment['anchors'][-1]['seconds'])
    beat_start=float(score_alignment['anchors'][0]['score_quarters'])
    beat_end=float(score_alignment['anchors'][-1]['score_quarters'])
    mapped_intervals={}
    # A frame-level boundary just ahead of a notated attack can otherwise round
    # to the previous bar's final sixteenth. Prefer that attack only within a
    # small time AND beat window, and only if no closer real anticipation exists.
    attacks=[]
    if path:
        from .notation_readability import sounding_signature
        attacks=sorted({float(item[1]) for item in sounding_signature(path)})
    anchor_seconds=[float(a['seconds']) for a in score_alignment['anchors']]
    anchor_beats=[float(a['score_quarters']) for a in score_alignment['anchors']]
    def display_boundary(seconds,beat):
        ordinary=max(beat_start,min(beat_end,math.floor(beat*4+.5)/4))
        target=float(round(beat))
        if not attacks or not beat_start<=target<=beat_end or abs(target-beat)>.20:
            return ordinary
        i=bisect_right(attacks,beat)
        nearest=min(attacks[max(0,i-1):i+1],key=lambda x:abs(x-beat))
        if abs(nearest-target)>1e-7:return ordinary
        target_seconds=TempoMap._interpolate(target,anchor_beats,anchor_seconds)
        if abs(target_seconds-seconds)>.16:return ordinary
        return target
    for index,source_start,source_end,_ in parsed_segments:
        start,end=max(source_start,coverage_start),min(source_end,coverage_end)
        if end<=start:continue
        mapped_intervals[index]=(start,end)
        a,b=mapper(start),mapper(end)
        if a is None or b is None:continue
        qa=display_boundary(start,a)
        qb=display_boundary(end,b)
        boundary_grid.setdefault(start,(qa,False));boundary_grid.setdefault(end,(qb,False))
        if qb<=qa:
            # Shared endpoints use the same finer resolution in adjacent chords.
            boundary_grid[start]=(max(beat_start,min(beat_end,round(a*9600)/9600)),True)
            boundary_grid[end]=(max(beat_start,min(beat_end,round(b*9600)/9600)),True)
    for index,start,end,harte in parsed_segments:
        entry={'segment_index':index,'source_start_seconds':start,'source_end_seconds':end,'harte_label':harte,
               'reference_start_quarters':reference.to_beat(start),'reference_end_quarters':reference.to_beat(end)}
        diagnostics.append(entry)
        if index in mapped_intervals:
            mapping_start,mapping_end=mapped_intervals[index]
            clipped=mapping_start!=start or mapping_end!=end
            entry.update(mapping_start_seconds=mapping_start,mapping_end_seconds=mapping_end,
                alignment_clipped=clipped,timing_review=clipped,
                clipping_reason='verified_alignment_coverage' if clipped else None)
        if harte in ('N','X'):
            entry['status']='no_chord' if harte=='N' else 'unknown';continue
        try:label,node,pcs=_harte_symbol(harte)
        except ImportError:
            raise
        except Exception as exc:
            entry.update(status='unsupported_label',detail=str(exc));continue
        if index not in mapped_intervals:
            entry['status']='unverified_score_alignment';continue
        a,b=mapper(mapping_start),mapper(mapping_end)
        if a is None or b is None:
            entry['status']='unverified_score_alignment';continue
        # Every endpoint is mapped from absolute source seconds independently.
        # A collapsed short interval remains exact and is reviewed, never silently lost.
        snapped_a,fine_a=boundary_grid[mapping_start];snapped_b,fine_b=boundary_grid[mapping_end]
        fine=fine_a or fine_b
        short=snapped_b<=snapped_a
        if short:
            entry['status']='below_midi_tick_resolution';continue
        entry.update(mapped_start_quarters=a,mapped_end_quarters=b,
                     snapped_start_quarters=snapped_a,snapped_end_quarters=snapped_b,status='mapped')
        mi=measure=offset=beat=None
        if primary:
            starts=[float(item[2]) for item in primary]
            candidate=bisect_right(starts,snapped_a)-1
            if candidate>=0 and snapped_a<float(primary[candidate][2]+primary[candidate][3])-1e-8:
                mi=candidate;measure=primary[mi][1].get('number',str(mi+1))
                try:measure=int(measure)
                except ValueError:pass
                offset=snapped_a-starts[mi]
        else:
            from .duet_notation import reference_bars
            starts,lengths,_=reference_bars(reference,snapped_a)
            mi=bisect_right(starts,snapped_a)-1;measure=mi+1;offset=snapped_a-starts[mi]
        if mi is None:
            entry['status']='outside_notation';continue
        # Existing report uses metric beats for simple meters, not bar*4 math.
        signature=next(s for s in reversed(reference.data['time_signatures']) if s['beat']<=snapped_a)
        numerator,denominator=signature['numerator'],signature['denominator']
        if primary:
            for _,xml_measure,_,_,_ in primary[:mi+1]:
                xml_time=xml_measure.find('attributes/time')
                if xml_time is not None:
                    try:numerator=int(xml_time.findtext('beats'));denominator=int(xml_time.findtext('beat-type'))
                    except (TypeError,ValueError):pass
            first_length=float(primary[0][3])
        else:first_length=lengths[0]
        padding=max(0,numerator*4/denominator-first_length) if mi==0 else 0
        beat=1+(padding+offset)/(4/denominator)
        event={**entry,'measure':measure,'measure_index':mi,'beat':beat,'offset_quarters':offset,
               'absolute_quarters':snapped_a,'duration_quarters':snapped_b-snapped_a,'label':label,
               'uncertain':False,'roman':None,'chromatic':None,'pitch_classes':pcs,
               'evidence':'audio_model_harte','timing_adjustment_quarters':snapped_a-a,
               'fine_timing_preserved':fine}
        events.append(event);symbols.append(node)
    unresolved=[d for d in diagnostics if d['status'] not in ('mapped','no_chord')]
    if unresolved:warnings.append({'kind':'audio_harmony_review','message':f'{len(unresolved)} 个音频和弦区段未可靠定位或转换，未强行写入谱面。'})
    clipped=[d for d in diagnostics if d.get('alignment_clipped') and d['status']=='mapped']
    if clipped:warnings.append({'kind':'audio_harmony_alignment_clipped',
        'message':f'{len(clipped)} 个和弦区段仅在已验证对齐范围内定位；保留原始秒数，边界裁剪已记录。'})
    audit=_write_xml(path,tree,layout,primary,events,symbols,show_chords) if path else {}
    updated=deepcopy(report)
    updated.setdefault('symbolic_chords',deepcopy(report.get('chords',[])))
    updated.update(chords=events,method='audio_model_harte_verified_score_alignment',
        audio_harmony={'segments':diagnostics,'alignment':deepcopy(score_alignment),
            'mapping':'absolute_seconds_to_verified_score_quarters','display_grid_quarters':.25,
            'attack_snap_max_quarters':.20,'attack_snap_max_seconds':.16,
            'xml':audit,'show_chords':show_chords,'score_alignment_verified':mapper is not None})
    updated['warnings']=[w for w in updated.get('warnings',[]) if not isinstance(w,dict) or w.get('kind')!='analysis_scope']+warnings
    updated['warnings'].append({'kind':'analysis_scope','message':'和弦来自音频模型，分析声部由调用方指定；未据此改动钢琴音符、调号或用户修订。模型标签不是人工确认。'})
    if path:updated['musicxml_path']=str(path.resolve())
    report.clear();report.update(updated)
    return report
