"""Revision-bound, limited-range score feedback without neural inference.

An unchanged working revision copies the final score's measures. An edited
revision builds an explicitly labelled local draft from current onsets and
cached written durations (manual length edits override those durations).
Neither path changes the working document or publishes a formal export.
"""
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from copy import deepcopy
from fractions import Fraction
from io import BytesIO
import hashlib
import json
import math
from pathlib import Path
import tempfile
from time import perf_counter
import xml.etree.ElementTree as ET

from .timing import TempoMap
from .duet_notation import reference_bars, snap_reference_onset, reference_phase
from .notation_readability import clip_boundary_ties, sounding_signature, _sounding_pitch
from .notation_single_voice import _Stream, _Event, _replacement, _restore_missing_beams
from .score_tempo import _measure_layout, remove_generated_reference_clock
from .score_midi import _pitch

VERSION=24


def _clock_content(tempo):
    # Undo/reset provenance is not a playback or notation mapping change.
    return {k:v for k,v in tempo.items() if k!='rhythm_initial'}


def _json(path):return json.loads(Path(path).read_text(encoding='utf-8'))
def _write(path,value):Path(path).write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')
def _q(value):return Fraction(str(float(value))).limit_denominator(9600)


def _compact_preview_layout(path):
    tree=ET.parse(path);root=tree.getroot()
    for node in list(root):
        if node.tag in ('work','movement-title','credit','defaults'):root.remove(node)
    defaults=ET.Element('defaults');scale=ET.SubElement(defaults,'scaling')
    ET.SubElement(scale,'millimeters').text='7';ET.SubElement(scale,'tenths').text='40'
    page=ET.SubElement(defaults,'page-layout')
    ET.SubElement(page,'page-height').text='850';ET.SubElement(page,'page-width').text='1100'
    margins=ET.SubElement(page,'page-margins',{'type':'both'})
    for name,value in (('left-margin',35),('right-margin',35),('top-margin',25),('bottom-margin',25)):
        ET.SubElement(margins,name).text=str(value)
    system=ET.SubElement(defaults,'system-layout');ET.SubElement(system,'system-distance').text='65'
    ET.SubElement(system,'top-system-distance').text='20'
    root.insert(next((i for i,n in enumerate(root) if n.tag=='part-list'),0),defaults)
    for measure in root.findall('.//measure'):
        for printing in list(measure.findall('print')):measure.remove(printing)
    tree.write(path,encoding='utf-8',xml_declaration=True)


def _context(clock,start,end,context_bars):
    if not all(math.isfinite(v) for v in (start,end)) or not 0<=start<end<=clock.seconds[-1]+1e-6:
        raise ValueError('预览选区必须位于曲目内，右端晚于左端。')
    score_end=clock.data.get('score_end')
    if score_end and end>float(score_end['seconds'])+1e-6:
        raise ValueError('预览选区超过已确认记谱终点。')
    limit=clock.to_beat(float(score_end['seconds'])) if score_end else clock.beats[-1]
    starts,lengths,meters=reference_bars(clock,limit-1e-8)
    # reference_bars uses a tolerance and may include a boundary at limit.
    # It is an endpoint, not an extra empty measure or extra audio duration.
    rows=[(s,min(length,limit-s),meter) for s,length,meter in zip(starts,lengths,meters) if s<limit-1e-8]
    if not rows:raise ValueError('所选范围没有可用的小节。')
    starts,lengths,meters=map(list,zip(*rows))
    # Count a half-open interval in each rendered bar's own beat length.
    # Tempo and meter can vary; elapsed seconds are not a measure count.
    epsilon=1e-8
    def at_boundary(beat):
        i=bisect_right(starts,beat)
        for j in (i-1,i):
            if 0<=j<len(starts) and abs(starts[j]-beat)<=epsilon:return starts[j]
        return beat
    lo=at_boundary(clock.to_beat(start))
    hi=at_boundary(clock.to_beat(end))
    # Do not collapse a valid sub-epsilon interval while snapping boundaries.
    if hi<=lo:lo,hi=clock.to_beat(start),clock.to_beat(end)
    first=max(0,bisect_right(starts,lo)-1)
    last=max(first,bisect_left(starts,hi)-1)
    def overlap(i):
        return max(0.,min(hi,starts[i]+lengths[i])-max(lo,starts[i]))/lengths[i]
    bars=overlap(first) if first==last else (last-first-1)+overlap(first)+overlap(last)
    # At most two partial edge bars. Keep a hard count bound for engraving
    # even if a floating-point seam produces an extra touched bar.
    if bars>24+epsilon or last-first+1>25:
        raise ValueError(f'选区约 {bars:.2f} 小节（涉及 {last-first+1} 小节）；局部预览上限为 24 小节，请缩短选区或正式重导出。')
    a=max(0,first-context_bars);b=min(len(starts),last+1+context_bars)
    return starts[a:b],lengths[a:b],[tuple(map(int,m.split('/'))) for m in meters[a:b]],a,b



def _formal_context(xml,clock,start,end,context_bars):
    """Trim optional context only; never silently omit requested score bars."""
    rows=list(_measure_layout(ET.parse(xml).getroot()))
    available={round(float(s),7):float(length) for _,_,s,length,_ in rows}
    requested=_context(clock,start,end,0)
    def present(s,length):
        return round(float(s),7) in available and abs(available[round(float(s),7)]-length)<1e-6
    if any(not present(s,length) for s,length in zip(requested[0],requested[1])):
        last=max((float(s+length) for _,_,s,length,_ in rows),default=0)
        raise ValueError(f'所选范围超出当前正式谱或小节映射不一致（正式谱结束于 {clock.to_seconds(last):.2f} 秒）。请缩小范围或更新正式谱。')
    starts,lengths,meters,a,b=_context(clock,start,end,context_bars)
    keep=[i for i,(s,length) in enumerate(zip(starts,lengths)) if present(s,length)]
    if not keep or keep!=list(range(keep[0],keep[-1]+1)):
        raise ValueError('正式谱上下文存在小节缺口，请更新正式谱后重试。')
    left,right=keep[0],keep[-1]+1
    return starts[left:right],lengths[left:right],meters[left:right],a+left,a+right

def _duration_priors(xml,base_doc):
    clock=TempoMap(deepcopy(base_doc['tempo']));written=[]
    lane_by_attack={};arpeggiation_by_attack={};note_details={}
    from .notation_single_voice import _streams
    for _,measure,start,_,division in _measure_layout(ET.parse(xml).getroot()):
        streams,_=_streams(measure,division)
        for stream in streams:
            for event in stream.events:
                node=event.node
                if node.find('pitch') is None or node.find("tie[@type='stop']") is not None:continue
                key=(node.findtext('staff','1'),*_sounding_pitch(node.find('pitch')),
                     start+Fraction(event.start,division),node.find('grace') is not None)
                lane_by_attack[key]=node.findtext('voice','1')
                safe={'articulations','fermata'}
                technical_safe={'fingering','pluck','string','fret','thumb-position','heel','toe','fingernails','open-string','down-bow','up-bow','snap-pizzicato','stopped'}
                technical=node.find('notations/technical')
                if technical is not None and all(child.tag in technical_safe for child in technical):safe.add('technical')
                note_details[key]={'written_pitch':ET.tostring(node.find('pitch'),encoding='unicode'),
                    'point_notations':[ET.tostring(mark,encoding='unicode') for mark in node.findall('notations/*') if mark.tag in safe],
                    'unhandled_notations':sorted({mark.tag for mark in node.findall('notations/*') if mark.tag not in safe|{'arpeggiate','non-arpeggiate','tied','tuplet','slur'}}),
                    'has_lyrics':bool(node.findall('lyric'))}
                arpeggiation_by_attack[key]=[ET.tostring(mark,encoding='unicode')
                    for mark in node.findall('notations/arpeggiate')+node.findall('notations/non-arpeggiate')]
    for (key,start,end,grace),count in sounding_signature(xml).items():
        for _ in range(count):written.append({'pitch':_pitch(key),'start':float(start),
            'duration':float(end-start),'staff':int(key[1]),'grace':grace,'key':key,
            'voice':lane_by_attack.get((*key[1:],start,grace),'1' if key[1]=='1' else '5'),
            'arpeggiation':arpeggiation_by_attack.get((*key[1:],start,grace),[]),
            **note_details.get((*key[1:],start,grace),{})})
    used=set();result={}
    for n in base_doc['notes']:
        choices=sorted((abs(w['start']-clock.to_beat(n['start'])),i)
                       for i,w in enumerate(written) if i not in used and w['pitch']==n['pitch'])
        if not choices or choices[0][0]>.3 or len(choices)>1 and abs(choices[1][0]-choices[0][0])<1e-8:continue
        _,index=choices[0];used.add(index);result[n['id']]=written[index]
    return result


def _unchanged_clock_regions(old, new):
    """Outside regions with unchanged clock slope (and optional translation).

    An interior constant-offset plateau may itself be part of a timing edit;
    do not mistake it for untouched context or bypass the notation lattice.
    """
    points=sorted({*old.seconds,*new.seconds})
    shifts=[new.to_beat(t)-old.to_beat(t) for t in points];regions=[]
    for a,b,left,right in zip(points,points[1:],shifts,shifts[1:]):
        if abs(left-right)>1e-8:continue
        if regions and abs(regions[-1][1]-a)<1e-8 and abs(regions[-1][2]-left)<1e-8:
            regions[-1]=(regions[-1][0],b,left)
        else:regions.append((a,b,left))
    return [r for r in regions if abs(r[0]-points[0])<1e-8 or abs(r[1]-points[-1])<1e-8]


def _restore_arpeggiation(events, roll_groups=()):
    """Retain source markings only on a still-coincident written chord."""
    groups=defaultdict(list)
    for i,event in enumerate(events):
        prior=event['prior']
        if prior and prior.get('arpeggiation'):
            groups[(event['staff'],prior['start'])].append(i)
    preserved=[]
    def mark(event, nodes):
        notation=event['node'].find('notations')
        if notation is None:notation=ET.SubElement(event['node'],'notations')
        for raw in nodes:
            element=ET.fromstring(raw)
            if not any(ET.tostring(existing)==ET.tostring(element) for existing in notation):notation.append(element)
    for ids in groups.values():
        if len({events[i]['start'] for i in ids})!=1:continue
        for i in ids:mark(events[i],events[i]['prior']['arpeggiation'])
        preserved.extend(events[i]['id'] for i in ids)
    for group in roll_groups:
        # Final written notation wins in a wholly unchanged group. Native
        # roll guesses must not add marks merely because a remote clock changed.
        if group['input_ids'] and all(events[i].get('preserve_written',False) for i in group['input_ids']):
            continue
        for i in group['input_ids']:
            mark(events[i],[ET.tostring(ET.Element('arpeggiate',{'direction':group['direction']}),encoding='unicode')])
    return preserved


def _context_markers(xml,base_clock,current_clock,lo,hi):
    """Source seconds preserve key/harmony placement after a timing edit."""
    keys=[];harmonies=[];clefs={};initial_keys={}
    source_chords={};sidecar=Path(xml).with_name('piano_harmony.json')
    if sidecar.is_file():
        for chord in _json(sidecar).get('chords',[]):
            seconds=chord.get('mapping_start_seconds')
            if isinstance(seconds,(int,float)) and math.isfinite(seconds):
                source_chords['astra-audio-harmony-'+str(chord.get('segment_index'))]=float(seconds)
    source_root=ET.parse(xml).getroot()
    if len(source_root.findall('part'))!=1:
        raise ValueError('编辑后的局部草稿目前仅支持单个钢琴声部；多乐器正式谱仍可查看，未修改原谱。')
    if source_root.find('.//transpose') is not None:
        raise ValueError('这份谱包含移调乐器，暂不支持生成编辑后的局部草稿。原正式谱仍可查看，工程和正式谱未改变。')
    for _,measure,start,_,division in _measure_layout(source_root):
        cursor=Fraction(0)
        for node in measure:
            if node.tag=='attributes':
                for key in node.findall('key'):
                    q=current_clock.to_beat(base_clock.to_seconds(float(start+cursor)))
                    keys.append((q,deepcopy(key)))
                for clef in node.findall('clef'):
                    q=current_clock.to_beat(base_clock.to_seconds(float(start+cursor)))
                    if q<=lo:clefs[clef.get('number','1')]=deepcopy(clef)
                    elif q<hi:keys.append((q,deepcopy(clef)))  # timed attribute change, not a new key
            elif node.tag=='backup':cursor-=Fraction(int(node.findtext('duration')),division)
            elif node.tag=='forward':cursor+=Fraction(int(node.findtext('duration')),division)
            elif node.tag=='note' and node.find('chord') is None:cursor+=Fraction(int(node.findtext('duration','0')),division)
            elif node.tag=='harmony':
                position=start+cursor+Fraction(int(node.findtext('offset','0')),division)
                seconds=source_chords.get(node.get('id'),base_clock.to_seconds(float(position)))
                q=current_clock.to_beat(seconds)
                if lo<=q<hi:
                    copy=deepcopy(node)
                    for off in copy.findall('offset'):copy.remove(off)
                    harmonies.append((q,copy))
    for position,key in keys:
        if key.tag!='key' or position>lo:continue
        number=key.get('number')
        if number is not None and number not in ('1','2'):
            raise ValueError('局部草稿暂不支持两个以上的谱表；原正式谱未改变。')
        for staff in (('1','2') if number is None else (number,)):
            value=deepcopy(key);value.set('number',staff);initial_keys[staff]=value
    return [initial_keys[k] for k in sorted(initial_keys)],[(q,k) for q,k in keys if lo<q<hi],harmonies,clefs


def _draft(xml,base_doc,doc,starts,lengths,meters,output,*,projection_only=False,allow_current_midi_preview=False,written_priors=None):
    from music21.pitch import Pitch
    divisions=9600;coordinate_errors=[]
    def draft_q(value):
        # A bounded denominator is not necessarily a divisor of divisions.
        # Encode every absolute boundary on ONE explicit integer tick lattice.
        exact=Fraction(str(float(value)));encoded=Fraction(round(exact*divisions),divisions)
        coordinate_errors.append(float(abs(encoded-exact)))
        return encoded
    prior_free=xml is None and base_doc is None
    if prior_free and (not allow_current_midi_preview or projection_only):
        raise ValueError('A prior-free score requires the current MIDI preview mode')
    clock=TempoMap(deepcopy(doc['tempo']))
    oldclock=clock if prior_free else TempoMap(deepcopy(base_doc['tempo']))
    lo,hi=starts[0],starts[-1]+lengths[-1]
    draft_lo,draft_hi=draft_q(lo),draft_q(hi)
    priors={} if prior_free else written_priors if written_priors is not None else _duration_priors(xml,base_doc)
    old={} if prior_free else {n['id']:n for n in base_doc['notes']}
    events=[];sources=Counter();clipped=0
    same_clock=prior_free or _clock_content(doc['tempo'])==_clock_content(base_doc['tempo'])
    native=None;current_midi_draft=prior_free
    if not same_clock:
        from .score_projection_prior import StaleProjectionEvents,load_projected_prior
        try:
            native=load_projected_prior(xml,base_doc,doc,clock)
        except StaleProjectionEvents:
            if not allow_current_midi_preview or projection_only:raise
            current_midi_draft=True
    unchanged_regions=_unchanged_clock_regions(oldclock,clock)
    global_origin=float(clock.data.get('grid_origin',clock.data.get('pickup_beats',0)))
    local_phase=(prior_free or not same_clock) and any(abs((s-global_origin)*24-round((s-global_origin)*24))>1e-6 for s in starts)
    if local_phase:
        endpoint=clock.data.get('score_end',{}).get('seconds',clock.seconds[-1])
        phase_starts,_,_=reference_bars(clock,clock.to_beat(endpoint)-1e-8)
        notation_clock=TempoMap(deepcopy(doc['tempo']))
    def notation_snap(seconds):
        if not local_phase:return snap_reference_onset(seconds,clock,'auto')
        index=max(0,bisect_right(phase_starts,clock.to_beat(seconds))-1)
        notation_clock.data['grid_origin']=phase_starts[index]
        return snap_reference_onset(seconds,notation_clock,'auto')
    for n in doc['notes']:
        prior=priors.get(n['id']);previous=old.get(n['id'])
        if current_midi_draft:prior=None
        on=notation_snap(n['start'])
        preserve_written=False
        unchanged_event=(previous is not None and all(n.get(k)==previous.get(k)
                         for k in ('start','end','pitch','velocity')))
        if prior is not None and unchanged_event:
            written_seconds=oldclock.to_seconds(prior['start'])
            protected_times=[n['start'],written_seconds]
            if native is not None:
                # The original-model projection precedes native duration/voice
                # cleanup. Do not regress untouched context to that raw prior.
                # Reuse final notation only if its WHOLE written interval and
                # performance interval stay in one unchanged outer clock region.
                protected_times.extend((n['end'],oldclock.to_seconds(prior['start']+prior['duration'])))
            for a,b,shift in unchanged_regions:
                if all(a-1e-8<=t<=b+1e-8 for t in protected_times):
                    phase_delta=(reference_phase(clock,prior['start']+shift)[0]-
                                 reference_phase(oldclock,prior['start'])[0]-shift)
                    # A changed opening/bar phase is a notation edit even if
                    # the seconds-to-quarter slope is unchanged. Ignore only
                    # sub-tick numerical drift or complete-quarter translation.
                    if abs(phase_delta-round(phase_delta))>1/19200:continue
                    on=prior['start']+shift;preserve_written=True;break
        edited_length=(n.get('origin')=='manual' and (previous is None or abs((n['end']-n['start'])-(previous['end']-previous['start']))>1e-6))
        if current_midi_draft:
            value=max(1/24,notation_snap(n['end'])-on);source='current_midi_duration'
        elif edited_length:
            value=max(1/24,notation_snap(n['end'])-on);source='manual_length'
        elif prior is not None:
            value=prior['duration'];source='cached_written_duration'
        else:value=.5;source='unmatched_eighth_draft'
        grace=bool(prior and prior['grace'] and not edited_length)
        if native is not None:
            if preserve_written:
                value=prior['duration'];grace=prior['grace'];source='cached_written_unchanged_clock'
            else:
                row=native['rows'][n['id']]
                on=row['start'];value=row['duration'];grace=row['grace'];source='verified_model_duration'
        end=on+value
        # Decide boundary ownership on the same tick lattice used by
        # MusicXML. Float noise can otherwise retain an empty pitched tail.
        on_tick,end_tick=draft_q(on),draft_q(end)
        start_tick,stop_tick=max(draft_lo,on_tick),min(draft_hi,end_tick)
        if not (draft_lo<=on_tick<draft_hi if grace else start_tick<stop_tick):continue
        crosses_boundary=on_tick<draft_lo or end_tick>draft_hi
        sources[source]+=1;clipped+=int(crosses_boundary)
        staff=(native['rows'][n['id']]['staff'] if native is not None and not preserve_written
               else prior['staff'] if prior else (1 if n['pitch']>=60 else 2))
        pitch=Pitch();pitch.midi=n['pitch'];node=ET.Element('note')
        if grace:ET.SubElement(node,'grace',{'slash':'yes'})
        p=ET.SubElement(node,'pitch');ET.SubElement(p,'step').text=pitch.step
        if pitch.accidental:ET.SubElement(p,'alter').text=str(int(pitch.accidental.alter))
        ET.SubElement(p,'octave').text=str(pitch.octave)
        if prior and previous and previous.get('pitch')==n['pitch'] and prior.get('written_pitch'):
            node.remove(p);node.append(ET.fromstring(prior['written_pitch']))
        # Attack decorations stay attached only to a surviving original attack.
        if prior and previous and previous.get('pitch')==n['pitch'] and on_tick>=draft_lo and prior.get('point_notations'):
            notation=ET.SubElement(node,'notations')
            for raw in prior['point_notations']:notation.append(ET.fromstring(raw))
        voice=prior.get('voice') if prior else ('1' if staff==1 else '5')
        ET.SubElement(node,'voice').text=voice
        ET.SubElement(node,'staff').text=str(staff)
        if grace:ET.SubElement(node,'type').text='eighth'
        notation=node.find('notations')
        if notation is not None:node.remove(notation);node.append(notation)
        events.append(dict(start=start_tick,end=stop_tick,staff=staff,node=node,pitch=n['pitch'],grace=grace,id=n['id'],
                           source_seconds=float(n['start']),source_end_seconds=float(n['end']),prior=prior,
                           preserve_written=preserve_written,protected=n.get('origin')=='manual',clipped=crosses_boundary))
    sequence_audit=({'applied':False,'deferred_reason':'confirmed_local_bar_phase_replaces_old_global_grid_origin'}
                    if local_phase else None)
    group_audit=roll_audit=None
    if native is not None:
        projected=native['projected']
        sequence_audit=dict(projected['model_sequence_alignment'],applied=True)
        group_audit=dict(projected['onset_group_alignment'],applied=True)
        event_ids={native['rows'][e['id']]['input_id']:i for i,e in enumerate(events)}
        roll_audit=dict(projected['compact_model_rolls'],applied=True,groups=[])
        for group in projected['compact_model_rolls']['groups']:
            if all(i in event_ids for i in group['input_ids']):
                roll_audit['groups'].append(dict(group,input_ids=[event_ids[i] for i in group['input_ids']]))
    if native is None and not same_clock and events and not local_phase and not current_midi_draft:
        # A local edit draft reuses written rhythm, not a second inference.
        # Reuse formal projection functions with cached WRITTEN priors. These
        # are not the original DuET model positions/durations, so this remains
        # a local draft rather than a claim of complete formal-export parity.
        # Unmatched/clipped/manual notes and unaffected context are protected.
        from .duet_notation import infer_beat_ratios,align_model_onset_groups,compact_model_rolls
        from .notation_model_rhythm import preserve_model_sequences
        model_positions=[e['prior']['start'] if e['prior'] else oldclock.to_beat(e['source_seconds']) for e in events]
        limit=math.ceil(max(model_positions))+2
        model_grid=[oldclock.to_seconds(q) for q in range(limit+1)]
        ratios,uncertain=infer_beat_ratios(model_grid,clock)
        protected=[i for i,e in enumerate(events) if e['protected'] or e['prior'] is None or e['clipped'] or e['preserve_written']]
        onsets=[e['source_seconds'] for e in events];pitches=[e['pitch'] for e in events]
        durations=[e['prior']['duration'] if e['prior'] else 0 for e in events]
        aligned,group_audit=align_model_onset_groups(model_positions,onsets,pitches,durations,clock,
            protected_input_ids=protected,protected_anchor_seconds=[a['seconds'] for a in doc.get('anchors',[])])
        # Manual attacks, clipped context and unchanged affine clock regions
        # keep their already established written coordinates.
        for i in protected:aligned[i]=float(events[i]['start'])
        targets,sequence_audit=preserve_model_sequences(model_positions,
            [e['source_seconds'] for e in events],[e['pitch'] for e in events],
            durations,clock,ratios,aligned,uncertain_intervals=uncertain,
            protected_input_ids=protected,excluded_input_ids=group_audit['preserved_run_input_ids'],
            protected_anchor_seconds=[a['seconds'] for a in doc.get('anchors',[])],staffs=[e['staff'] for e in events])
        targets,roll_audit=compact_model_rolls(model_positions,onsets,[e['source_end_seconds'] for e in events],
            pitches,[e['staff'] for e in events],durations,targets,clock,protected_input_ids=protected,
            protected_anchor_seconds=[a['seconds'] for a in doc.get('anchors',[])])
        within_range=all(lo<=target and e['end']+draft_q(target)-e['start']<=draft_q(hi)
                         for e,target in zip(events,targets))
        sequence_audit['applied']=within_range
        group_audit['applied']=within_range;roll_audit['applied']=within_range
        if within_range:
            for e,target in zip(events,targets):
                delta=draft_q(target)-e['start'];e['start']+=delta;e['end']+=delta
        else:sequence_audit['deferred_reason']='projection_would_cross_preview_boundary'
    preserved_arpeggiation=_restore_arpeggiation(events,roll_audit['groups'] if roll_audit and roll_audit['applied'] else ())
    # Explicit note re-attacks terminate an earlier same-pitch written hold;
    # pedal tails are never used as a duration source.
    by_pitch=defaultdict(list)
    for event in events:
        if not event['grace']:by_pitch[(event['staff'],event['pitch'])].append(event)
    for notes in by_pitch.values():
        notes.sort(key=lambda n:n['start'])
        for left,right in zip(notes,notes[1:]):
            if left['start']==right['start']:raise ValueError('选区含同谱表同拍位重复同音，请先核对音符。')
            left['end']=min(left['end'],right['start'])
    if prior_free:
        slur_audit={'warnings':[]}
        initial_key,keys,harmonies,clefs=[],[],[],{}
        direction_audit={'directions':[],'warnings':[],'pedal_boundary':{}}
    else:
        from .score_range_slurs import restore_slurs
        slur_audit=restore_slurs(xml,events)
        initial_key,keys,harmonies,clefs=_context_markers(xml,oldclock,clock,lo,hi)
        from .score_range_directions import collect_directions
        direction_audit=collect_directions(xml,oldclock,clock,lo,hi,doc)
    directions=direction_audit['directions']
    direction_audit['warnings'].extend(slur_audit['warnings'])
    if any(e['prior'] and e['prior'].get('unhandled_notations') for e in events):
        direction_audit['warnings'].append('原谱部分装饰音记号尚未保留，请对照正式谱核对。')
    if any(e['prior'] and e['prior'].get('has_lyrics') for e in events):
        direction_audit['warnings'].append('局部编辑草稿暂不显示歌词。')
    if projection_only:
        from .score_projection_fingerprint import engraving_inputs
        return engraving_inputs(starts,lengths,meters,events,initial_key,keys,harmonies,clefs,doc['tempo'],lo,directions=directions,warnings=direction_audit['warnings'])
    root=ET.Element('score-partwise',{'version':'4.0'});work=ET.SubElement(root,'work')
    ET.SubElement(work,'work-title').text='局部记谱草稿（未重新推理）'
    plist=ET.SubElement(root,'part-list');sp=ET.SubElement(plist,'score-part',{'id':'P1'})
    ET.SubElement(sp,'part-name').text='Piano';part=ET.SubElement(root,'part',{'id':'P1'})
    for i,(start,length,meter) in enumerate(zip(starts,lengths,meters)):
        stop=draft_q(start+length);start=draft_q(start);length=stop-start;measure=ET.SubElement(part,'measure',{'number':str(i+1)})
        if prior_free and abs(float(length)-meter[0]*4/meter[1])>1e-6:
            measure.set('implicit','yes')
        total=int(length*divisions);attrs=ET.Element('attributes')
        ET.SubElement(attrs,'divisions').text=str(divisions)
        if i==0 or meter!=meters[i-1]:
            time=ET.SubElement(attrs,'time');ET.SubElement(time,'beats').text=str(meter[0]);ET.SubElement(time,'beat-type').text=str(meter[1])
        if i==0:
            attrs.extend(deepcopy(initial_key))
            ET.SubElement(attrs,'staves').text='2'
            for staff,sign,line in ((1,'G',2),(2,'F',4)):
                c=clefs.get(str(staff))
                if c is None:
                    c=ET.Element('clef',{'number':str(staff)});ET.SubElement(c,'sign').text=sign;ET.SubElement(c,'line').text=str(line)
                attrs.append(c)
        first_lane=True
        for staff in (1,2):
            markers=[(0,attrs)] if staff==1 else []
            if staff==1:
                for q,key in keys:
                    if start<=q<start+length:
                        a=ET.Element('attributes');a.append(key);markers.append((round((q-float(start))*divisions),a))
                for q,h in harmonies:
                    if start<=q<start+length:
                        h=deepcopy(h);ET.SubElement(h,'offset').text=str(round((q-float(start))*divisions));markers.append((0,h))
                for q,direction in directions:
                    if start<=q<stop or (i==len(starts)-1 and abs(q-float(stop))<1e-8):
                        if staff!=1:continue  # direction carries its own display staff
                        direction=deepcopy(direction)
                        off=ET.Element('offset');off.text=str(round((q-float(start))*divisions))
                        # MusicXML offset precedes voice/staff/sound.
                        index=next((j for j,n in enumerate(direction) if n.tag in ('voice','staff','sound')),len(direction))
                        direction.insert(index,off);markers.append((0,direction))
            notes=[]
            for event in events:
                if event['staff']!=staff:continue
                a=max(start,event['start']);b=min(start+length,event['end'])
                if event['grace']:
                    if not start<=event['start']<start+length:continue
                elif b<=a:continue
                node=deepcopy(event['node'])
                if not event['grace']:
                    for kind,enabled in (('stop',a>event['start']),('start',b<event['end'])):
                        if enabled:
                            ET.SubElement(node,'tie',{'type':kind})
                            notation=node.find('notations')
                            if notation is None:notation=ET.SubElement(node,'notations')
                            ET.SubElement(notation,'tied',{'type':kind})
                notes.append(_Event(int((a-start)*divisions),int((b-start)*divisions),node))
            from .notation_range_rhythm import range_replacement
            from .notation_smart_voice import set_lane,_hide_covered_rests
            voices=sorted({e.node.findtext('voice') for e in notes}) or ['1' if staff==1 else '5']
            if len(voices)>2:raise ValueError('缓存谱面包含超过两个独立声部，不能安全生成双手草稿。')
            rendered=[]
            for index,voice in enumerate(voices):
                lane=[e for e in notes if e.node.findtext('voice')==voice]
                stream=_Stream(None,[],lane,markers if index==0 else [],total)
                replacement=range_replacement([stream],str(staff),divisions,meter[1]==8 and meter[0] in (6,9,12,15,18))
                set_lane(replacement,voice,('up' if index==0 else 'down') if len(voices)>1 else None)
                rendered.append(replacement)
            if len(rendered)>1:_hide_covered_rests(rendered,divisions)
            for replacement in rendered:
                if not first_lane:
                    backup=ET.SubElement(measure,'backup');ET.SubElement(backup,'duration').text=str(total)
                first_lane=False;measure.extend(replacement)
    # Preview measures start at zero, while annotations belong to the project clock.
    from .score_rhythm_annotations import apply_annotations
    rhythm_audit=apply_annotations(root,doc['tempo'],beat_offset=float(lo))
    ET.ElementTree(root).write(output,encoding='utf-8',xml_declaration=True)
    actual=sounding_signature(output);expected=Counter()
    for event in events:
        p=event['node'].find('pitch');key=('P1',str(event['staff']),*_sounding_pitch(p))
        expected[(key,event['start']-draft_q(lo),event['end']-draft_q(lo),event['grace'])]+=1
    if actual!=expected:raise ValueError('局部草稿音符与当前编辑不一致，已丢弃。')
    return {'current_midi_draft':current_midi_draft,'slur_audit':slur_audit,'notation_warnings':direction_audit['warnings'],'direction_audit':{'pedal_boundary':direction_audit['pedal_boundary'],'count':len(directions)},'rhythm_annotations':rhythm_audit,'duration_sources':dict(sources),'cross_boundary_notes':clipped,'note_count':len(events),
            'draft_grid':'reference_auto_9600_ticks','coordinate_encoding':{
                'divisions':divisions,'maximum_rounding_error_quarters':max(coordinate_errors,default=0),
                'measure_boundaries':'absolute_endpoints_then_difference','source_seconds_modified':False,
                'onset_grid_origin':'confirmed_local_barline' if local_phase else 'existing_reference_origin'},
            'note_ids':[event['id'] for event in events],
            'note_projection':[{'id':e['id'],'source_seconds':e['source_seconds'],'pitch':e['pitch'],
                'start_beat':float(e['start']),'end_beat':float(e['end']),'staff':e['staff']} for e in events],
            'model_sequence_alignment':sequence_audit,
            'onset_group_alignment':group_audit,'roll_alignment':roll_audit,
            'preserved_written_note_ids':[e['id'] for e in events if e['preserve_written']],
            'preserved_arpeggiation_note_ids':preserved_arpeggiation,
            'projection_prior':('verified_original_duet_prediction; full-input projection with unchanged-context final notation preserved before range clipping' if native is not None
                                else 'cached_written_notation; shared formal projection functions, not raw DuET inference output'),
            'projection_prior_identity':native['identity'] if native is not None else None,
            'warning':('复用同一原始预测，先按正式规则投影再截取；局部排版仍为草稿，和弦沿用基线。未运行模型。' if native is not None
                       else '局部草稿保留参考网格及缓存记谱时值，使用DuET节奏分区；新手工音长使用编辑值，未匹配音暂写八分。不是重新推理的正式谱。')}


def _formal_slice(xml,clock,starts,lengths,output):
    tree=ET.parse(xml);root=tree.getroot();initial={}
    rows=list(_measure_layout(root))
    # The verified clock maps the request into measures by approximate
    # coordinates. Once matched, clip strictly at the XML's actual rational
    # barlines, not a second approximation with a different denominator.
    matches=[i for i,row in enumerate(rows) if abs(float(row[2])-starts[0])<=1e-7]
    if len(matches)!=1:
        raise ValueError('正式谱小节与当前选区映射不一致。')
    first=matches[0]
    selected_rows=rows[first:first+len(starts)]
    if (len(selected_rows)!=len(starts) or len(starts)!=len(lengths) or
            any(abs(float(s)-a)>1e-7 or abs(float(length)-b)>1e-6
                for (_,_,s,length,_),a,b in zip(selected_rows,starts,lengths))):
        raise ValueError('正式谱小节与当前选区映射不一致。')
    lo=selected_rows[0][2]
    hi=selected_rows[-1][2]+selected_rows[-1][3]
    from .score_range_directions import collect_directions
    boundary_directions=collect_directions(xml,clock,clock,float(lo),float(hi),{})
    selected=[]
    def remember(attrs):
        for element in attrs:
            if element.tag=='key':
                staves=('1','2') if element.get('number') is None else (element.get('number'),)
                for staff in staves:
                    value=deepcopy(element);value.set('number',staff);initial[('key',staff)]=value
            else:
                initial[(element.tag,element.get('number',''))]=deepcopy(element)

    order={name:i for i,name in enumerate(('footnote','level','divisions','key','time','staves',
          'part-symbol','instruments','clef','staff-details','transpose','for-part','directive','measure-style'))}
    for part,measure,start,length,division in rows:
        if lo<=start<hi:
            clone=deepcopy(measure)
            if not selected:
                # A measure can put its only attributes block after notes, e.g.
                # after a backup to change the lower staff's clef. Preserve that
                # block in place and seed the excerpt from the true entry state.
                leading=[]
                for child in clone:
                    if child.tag in ('note','backup','forward'):
                        break
                    if child.tag=='attributes':
                        leading.append(child);remember(child)
                a=leading[0] if leading else ET.Element('attributes')
                if not leading:
                    clone.insert(next((i for i,n in enumerate(clone) if n.tag!='print'),len(clone)),a)
                for attrs in leading[1:]:
                    clone.remove(attrs)
                a[:]=[deepcopy(node) for node in sorted(initial.values(),key=lambda n:order.get(n.tag,len(order)))]
            selected.append(clone)
        for child in measure.findall('attributes'):
            remember(child)
    if len(selected)!=len(starts):raise ValueError('正式谱小节与当前选区映射不一致，请重新导出或使用编辑草稿。')
    part=root.find('part');part[:]=selected
    from .score_range_slurs import clip_unpaired_slurs
    clip_unpaired_slurs(root)
    clip_boundary_ties(root)
    # A local formal slice retains source directions; add only inherited edge
    # state, never a duplicate of a direction already present at that boundary.
    for q,direction in boundary_directions['directions']:
        left=abs(q-float(lo))<1e-8
        right=abs(q-float(hi))<1e-8
        if not ((left and (direction.find('direction-type/dynamics') is not None or direction.find('direction-type/pedal') is not None)) or
                (right and direction.find("direction-type/pedal[@type='stop']") is not None)):continue
        target=selected[0] if left else selected[-1]
        _,_,_,_,division=next(row for row in _measure_layout(root) if row[1] is target)
        offset=0 if left else round(float(lengths[-1])*division)
        candidate=deepcopy(direction)
        exists=False;cursor=0
        for child in target:
            if child.tag=='backup':cursor-=int(child.findtext('duration','0'))
            elif child.tag=='forward':cursor+=int(child.findtext('duration','0'))
            elif child.tag=='note' and child.find('chord') is None:cursor+=int(child.findtext('duration','0'))
            elif child.tag=='direction' and abs(cursor+float(child.findtext('offset','0'))-offset)<1e-8:
                same_staff=child.findtext('staff','1')==candidate.findtext('staff','1')
                same_dynamic=child.find('direction-type/dynamics') is not None and candidate.find('direction-type/dynamics') is not None
                cp=child.find('direction-type/pedal');np=candidate.find('direction-type/pedal')
                same_pedal=cp is not None and np is not None and cp.get('type')==np.get('type')
                exists=exists or (same_staff and (same_dynamic or same_pedal))
        if not exists:
            off=ET.Element('offset');off.text=str(offset)
            index=next((j for j,n in enumerate(candidate) if n.tag in ('voice','staff','sound')),len(candidate))
            candidate.insert(index,off)
            target.insert(next((j for j,n in enumerate(target) if n.tag!='attributes'),len(target)),candidate)
    tree.write(output,encoding='utf-8',xml_declaration=True);remove_generated_reference_clock(output)
    expected=Counter()
    for (key,start,end,grace),count in sounding_signature(xml).items():
        if lo<=start<hi if grace else start<hi and end>lo:
            expected[(key,max(start,lo)-lo,min(end,hi)-lo,grace)]+=count
    if sounding_signature(output)!=expected:raise ValueError('局部正式谱截取未保持边界音符，保留旧预览。')
    return {'note_count':sum(expected.values()),'duration_sources':{'final_notation':sum(expected.values())},
            'warning':'当前正式谱选区；跨边界持音按片段截取，分页仅属于此预览。'}


def preview_score_range(project,revision,start,end,*,context_bars=1,render_pdf=True,
                        cache_dir=None, exact_revision=False):
    from . import editing
    from .preview_cancel import check_cancelled
    check_cancelled()
    began=perf_counter()
    project=Path(project).resolve();doc=editing.current(project)
    if not doc or doc['revision']!=revision:raise ValueError('编辑版本已变化，请重新请求选区谱面预览。')
    if context_bars not in (0,1,2):raise ValueError('预览上下文只能为0至2小节。')
    from .score_artifacts import resolve_score_export
    base=resolve_score_export(project,source_key=doc.get('source_key'))
    if base is None:raise ValueError('先完成一次钢琴正式制谱，才能复用记谱结果预览编辑。')
    xml=base/'piano.musicxml';snapshot=base/'editor_snapshot.json'
    if not xml.is_file() or not snapshot.is_file():raise ValueError('先完成一次钢琴正式制谱，才能复用记谱结果预览编辑。')
    base_doc=_json(snapshot);clock=TempoMap(deepcopy(doc['tempo']))
    if base_doc.get('source_key')!=doc.get('source_key'):
        raise ValueError('当前转录来源已变化，请先为新来源正式制谱，再预览局部编辑。')
    starts,lengths,meters,a,b=_context(clock,float(start),float(end),context_bars)
    from .score_range_dependency import dependency_signature,source_stamp
    signature=dependency_signature(project,start,end,doc=doc,context_bars=context_bars)
    initial_source_stamp=source_stamp(project,base)
    proof_snapshot=None
    def still_valid():
        nonlocal signature
        check_cancelled()
        current=editing.current(project)
        unchanged=(current['revision']==revision and source_stamp(project,base)==initial_source_stamp)
        if exact_revision:
            return unchanged
        if unchanged or dependency_signature(project,start,end,doc=current,context_bars=context_bars)==signature:
            return True
        if proof_snapshot and context_bars==1 and source_stamp(project,base)==initial_source_stamp:
            from .score_range_dependency import compare_clock_projection
            try:proof=compare_clock_projection(project,proof_snapshot,start,end)
            except (OSError,ValueError,KeyError,TypeError,RuntimeError):return False
            if proof.get('equivalent'):
                signature=proof['signature']
                return True
        return False
    kind='formal_range' if base_doc['revision']==revision and not doc.get('score_stale') else 'edited_range_draft'
    if kind=='formal_range':starts,lengths,meters,a,b=_formal_context(xml,clock,float(start),float(end),context_bars)
    identity={'dependency_signature':signature,'version':VERSION,'revision':revision,'source_key':doc['source_key'],'kind':kind,
              'range':[float(start),float(end)],'context_bars':context_bars,
              'xml_sha256':hashlib.sha256(xml.read_bytes()).hexdigest(),'render_pdf':render_pdf,
              'projection_report_sha256':hashlib.sha256((base/'piano_notation_report.json').read_bytes()).hexdigest() if (base/'piano_notation_report.json').is_file() else None,
              'harmony_sha256':hashlib.sha256((base/'piano_harmony.json').read_bytes()).hexdigest() if (base/'piano_harmony.json').is_file() else None}
    if render_pdf:
        from .osmd_pdf import SELECTION_RENDERER
        identity['pdf_renderer']=SELECTION_RENDERER
    key=hashlib.sha256(json.dumps(identity,sort_keys=True).encode()).hexdigest()
    cache=Path(cache_dir) if cache_dir is not None else project/'cache'/'score_ranges'
    cache.mkdir(parents=True,exist_ok=True)
    dest=cache/key
    if (dest/'result.json').is_file():
        cached=_json(dest/'result.json')
        valid=all((dest/name).is_file() and hashlib.sha256((dest/name).read_bytes()).hexdigest()==digest
                  for name,digest in cached.get('artifacts',{}).items()) and bool(cached.get('artifacts'))
        rendered_ok = not render_pdf or (cached.get('pdf_render') or {}).get('status')=='ready'
        if valid and rendered_ok and cached.get('dependency_signature')==signature:
            if not still_valid():raise ValueError('编辑版本已变化，请重新请求选区谱面预览。')
            cached=dict(cached)
            cached['dependency_signature']=signature
            cached['dependency_source_stamp']=initial_source_stamp
            cached['validated_revision']=editing.current(project)['revision']
            cached['generation_timing']={'cache_hit':True,'total_seconds':perf_counter()-began}
            return cached
    with tempfile.TemporaryDirectory(prefix='pending-',dir=cache) as temp:
        pending=Path(temp);out=pending/'preview.musicxml'
        proof_snapshot=pending/'preview_input.json'
        _write(proof_snapshot,doc)
        notation_started=perf_counter()
        audit=(_formal_slice(xml,clock,starts,lengths,out) if kind=='formal_range'
               else _draft(xml,base_doc,doc,starts,lengths,meters,out,allow_current_midi_preview=True))
        check_cancelled()
        _compact_preview_layout(out)
        notation_seconds=perf_counter()-notation_started
        engraving_started=perf_counter()
        pdf=pending/'preview.pdf'
        output={'pdf':None,'pdf_render':None}
        if render_pdf:
            from .osmd_pdf import export_pdf_result
            output=export_pdf_result(out,pdf,[],layout_preset='selection')
        if not still_valid():raise ValueError('预览期间编辑版本已变化，已丢弃过期结果。')
        generation_timing={'cache_hit':False,'notation_seconds':notation_seconds,
                           'engraving_seconds':perf_counter()-engraving_started,'total_seconds':perf_counter()-began}
        result={'dependency_source_stamp':initial_source_stamp,'dependency_signature':signature,'validated_revision':editing.current(project)['revision'],
                'generation_timing':generation_timing,'revision':revision,'preview_kind':kind,'cache_key':key,
                'requested_range':{'start':float(start),'end':float(end)},
                'context_range':{'start':clock.to_seconds(starts[0]),'end':clock.to_seconds(starts[-1]+lengths[-1]),
                                 'first_measure':a+1,'last_measure':b,'start_beat':starts[0],'end_beat':starts[-1]+lengths[-1]},
                'musicxml':str(dest/'preview.musicxml'),'pdf':str(dest/'preview.pdf') if output['pdf'] else None,
                'pdf_render':output['pdf_render'],'source_export':str(base),'dependency_snapshot':str(dest/'preview_input.json'),'inference_run':False,
                'artifacts':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in pending.iterdir() if p.is_file()},**audit}
        _write(pending/'result.json',result)
        if dest.exists():
            # Only replace this derived cache's files, never project sources.
            for file in pending.iterdir():file.replace(dest/file.name)
            return result
        Path(temp).rename(dest)
    return result
