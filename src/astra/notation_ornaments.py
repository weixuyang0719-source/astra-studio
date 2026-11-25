"""Explicit, reversible engraving choices, never edits to performance events.

The first candidate is a short lead into a stable chord. It can equally be an
anticipated bass: no decision is accepted automatically. Free runs are not
classified from speed alone. Catalogs bind model, raw MIDI, clock and notation.
"""
from collections import Counter, defaultdict
from copy import deepcopy
from fractions import Fraction as F
from io import BytesIO
from pathlib import Path
import hashlib
import json
import os
import tempfile
import xml.etree.ElementTree as ET

from .notation_readability import sounding_signature, _sounding_pitch
from .notation_single_voice import _streams, _restore_missing_beams
from .score_midi import _pitch
from .score_tempo import _measure_layout
from .timing import TempoMap

VERSION = 1
CANDIDATE_POLICY_VERSION = 3


def _hash(value):
    if not isinstance(value, bytes):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    return hashlib.sha256(value).hexdigest()


def _atomic(path, data):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.ornament-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as out:
            out.write(data); out.flush(); os.fsync(out.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name): os.unlink(name)


def _layout(root):
    return [(part.get('id'), m.get('number'), str(start), str(extent),
             [ET.tostring(x, encoding='unicode') for x in m.findall('attributes/time')])
            for part, m, start, extent, _ in _measure_layout(root)]


def _records(root):
    records = []
    for part, measure, origin, _, div in _measure_layout(root):
        streams, _ = _streams(measure, div)
        if not streams: raise ValueError('Unsupported MusicXML cursor/division change')
        ordinal = {id(n): i for i, n in enumerate(measure.findall('note'))}
        for stream in streams:
            for event in stream.events:
                n = event.node; pitch = n.find('pitch')
                if pitch is None: continue
                key = (part.get('id'), n.findtext('staff', '1'), *_sounding_pitch(pitch))
                records.append(dict(part=part.get('id'), measure=measure.get('number'),
                    ordinal=ordinal[id(n)], staff=key[1], voice=n.findtext('voice', '1'),
                    start=origin+F(event.start, div), end=origin+F(event.end, div),
                    pitch=_pitch(key), key=key, node=n, measure_node=measure, stream=stream,
                    divisions=div, grace=n.find('grace') is not None))
    return records


def _semantic_from_records(root, records):
    # Chord visibility, title and page formatting do not invalidate a choice.
    return _hash({'layout': _layout(root), 'notes': [
        [r['part'], r['measure'], r['ordinal'], r['staff'], r['voice'], r['pitch'],
         str(r['start']), str(r['end']), r['grace'],
         [ET.tostring(x, encoding='unicode') for x in r['node'].findall('notations')]]
        for r in records]})


def _semantic(root):
    return _semantic_from_records(root, _records(root))


def _locator(record):
    return {k: (str(record[k]) if k in ('start', 'end') else record[k])
            for k in ('part', 'measure', 'ordinal', 'staff', 'voice', 'pitch', 'start', 'end')}


def _merge_generated_rests(measure, staff, voice, div):
    """Coalesce adjacent small silence pieces, never authored/hidden rests."""
    names = {F(1):('quarter',0), F(1,2):('eighth',0), F(1,4):('16th',0), F(1,8):('32nd',0),
             F(3,2):('quarter',1), F(3,4):('eighth',1), F(3,8):('16th',1)}
    previous = None
    for node in list(measure):
        safe = (node.tag == 'note' and node.find('rest') is not None and not node.attrib
                and node.findtext('staff','1') == staff and node.findtext('voice','1') == voice
                and all(c.tag in ('rest','duration','voice','type','dot','staff') for c in node))
        if not safe: previous=None; continue
        if previous is not None:
            ticks = int(previous.findtext('duration'))+int(node.findtext('duration'))
            name = names.get(F(ticks,div))
            if name:
                previous.find('duration').text=str(ticks); previous.find('type').text=name[0]
                for dot in list(previous.findall('dot')): previous.remove(dot)
                for _ in range(name[1]):previous.insert(list(previous).index(previous.find('type'))+1,ET.Element('dot'))
                measure.remove(node); continue
        previous=node


_DUET_MIDI_READER = None


def _read_duet_performance_midi(midi_path):
    """Load vendor/duet/src/duet/midi.py directly without executing duet/__init__.py (avoids importing torch)."""
    global _DUET_MIDI_READER
    if _DUET_MIDI_READER is None:
        import importlib.util
        from .duet_score import duet_source_dir
        midi_mod_path = duet_source_dir() / 'src' / 'duet' / 'midi.py'
        spec = importlib.util.spec_from_file_location('_astra_duet_midi_only', midi_mod_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _DUET_MIDI_READER = mod.read_performance_midi
    return _DUET_MIDI_READER(midi_path)


def build_catalog(xml_path, input_midi, prediction_npz, tempo, *, protected_input_ids=(), protected_note_onsets=(), context=None, manual_requests=()):
    """Read a verified DuET cache, no inference or project publication.

    ``context`` may bind source_key/editor_revision. The caller must regenerate
    after real edits and may not relabel old raw MIDI as a newer editor revision.
    """
    import numpy as np
    xml_path, midi, npz = map(lambda p: Path(p).resolve(strict=True), (xml_path, input_midi, prediction_npz))
    manifest = json.loads((npz.parent/'astra-cache.json').read_text(encoding='utf-8'))
    midi_hash, model_hash = _hash(midi.read_bytes()), _hash(npz.read_bytes())
    if manifest['identity']['input_sha256'] != midi_hash or manifest['artifacts']['score.npz'] != model_hash:
        raise ValueError('Ornament prediction/input identity mismatch')
    raw = _read_duet_performance_midi(midi)
    with np.load(npz, allow_pickle=False) as archive: model = archive['notes'].copy()
    pitches = list(map(int, raw['ref_pitch']))
    if len(model) != len(pitches) or list(map(int, model[:, 0])) != pitches:
        raise ValueError('Ornament prediction note order/pitches differ from input')
    performance = [dict(pitch=p, start=float(raw['ref_onset'][i]), end=float(raw['ref_offset'][i]),
                        velocity=int(raw['ref_vel'][i])) for i, p in enumerate(pitches)]
    protected_input_ids = set(protected_input_ids)
    for note in protected_note_onsets:
        matches = [i for i, n in enumerate(performance) if n['pitch'] == int(note['pitch'])
                   and abs(n['start']-float(note['seconds'])) <= .001]
        if not matches: raise ValueError('Manual ornament protection did not match serialized MIDI')
        protected_input_ids.update(matches)
    identity = dict(version=VERSION, candidate_policy_version=CANDIDATE_POLICY_VERSION,
                    input_sha256=midi_hash, prediction_sha256=model_hash,
                    reference_sha256=_hash(tempo.data if isinstance(tempo, TempoMap) else tempo),
                    context=deepcopy(context or {}), source_files=[
                        {'path':str(midi), 'sha256':midi_hash},
                        {'path':str(npz), 'sha256':model_hash}])
    catalog = _build_catalog(xml_path, performance, model, tempo, identity, protected_input_ids,manual_requests)
    if _hash(midi.read_bytes()) != midi_hash or _hash(npz.read_bytes()) != model_hash:
        raise ValueError('Ornament inputs changed while reading')
    return catalog


def _build_catalog(xml_path, performance, model, tempo, identity, protected_input_ids=(),manual_requests=()):
    """Array-level implementation shared by verified entrypoint and unit tests."""
    from bisect import bisect_left
    data = Path(xml_path).read_bytes(); root = ET.fromstring(data); clock = TempoMap(tempo.data if isinstance(tempo, TempoMap) else tempo)
    records = _records(root); protected = set(protected_input_ids)
    identity = dict(identity, notation_sha256=_semantic_from_records(root, records))
    by_pitch = defaultdict(list)
    perf_beats = [clock.to_beat(note['start']) for note in performance]
    by_pitch_staff = defaultdict(list)
    for i, note in enumerate(performance):
        p = int(note['pitch'])
        by_pitch[p].append(i)
        by_pitch_staff[(p, int(model[i][3]) + 1)].append((perf_beats[i], i))
    by_pitch_staff_beats = {}
    for key_ps, items in by_pitch_staff.items():
        items.sort()
        by_pitch_staff_beats[key_ps] = [b for b, _ in items]
    for r in records:
        key_ps = (r['pitch'], int(r['staff']))
        items = by_pitch_staff.get(key_ps)
        if not items:
            continue
        target_b = float(r['start'])
        beats_list = by_pitch_staff_beats[key_ps]
        pos = bisect_left(beats_list, target_b)
        lo = max(0, pos - 2)
        hi = min(len(items), pos + 3)
        matches = sorted((abs(items[k][0] - target_b), items[k][1]) for k in range(lo, hi))
        if matches and matches[0][0] <= .5 and (len(matches) == 1 or matches[1][0] - matches[0][0] > .04):
            r['input_id'] = matches[0][1]
    lanes = defaultdict(list)
    for r in records: lanes[r['part'], r['measure'], r['staff'], r['voice']].append(r)
    candidates = []
    for lane in lanes.values():
        groups = defaultdict(list)
        for r in lane: groups[r['start']].append(r)
        groups = sorted(groups.items())
        for index, (start, selected) in enumerate(groups[:-1]):
            # A single short lead is the narrow implemented class. Multinote
            # free flourishes need explicit grouping evidence and are not guessed.
            if len(selected) != 1: continue
            lead = selected[0]; anchor_start, anchor = groups[index+1]
            nodes = [lead, *anchor]
            if len(anchor) < 2 or len({n['pitch'] for n in nodes}) != len(nodes): continue
            if any('input_id' not in r or r['input_id'] in protected for r in nodes): continue
            if any(r['grace'] or r['node'].find('tie') is not None or
                   r['node'].find('notations') is not None or r['node'].find('time-modification') is not None
                   for r in nodes): continue
            i = lead['input_id']; raw = performance[i]; dur = lead['end']-start
            if not 0 < dur <= F(1,4) or lead['end'] != anchor_start: continue
            if not 0 < float(model[i][2]) <= .25 or raw['end']-raw['start'] > .15: continue
            if min(r['end']-r['start'] for r in anchor) < max(F(1,2), 2*dur): continue
            anchor_ids = [r['input_id'] for r in anchor]
            if len({float(model[j][1]) for j in anchor_ids}) != 1: continue
            if not all(float(model[j][1]) > float(model[i][1]) for j in anchor_ids): continue
            delay = min(performance[j]['start'] for j in anchor_ids)-raw['start']
            if not 0 < delay <= .15 or raw['end'] > min(performance[j]['start'] for j in anchor_ids)+.03: continue
            # A nearby same-pitch reattack is not an ornamental replacement.
            if any(j != i and abs(performance[j]['start']-raw['start']) <= .25 for j in by_pitch[lead['pitch']]): continue
            selected_locator, anchors = [_locator(lead)], [_locator(r) for r in anchor]
            candidate = dict(kind='grace_lead', label='短引音：装饰音或提前低音',
                explanation='可试写为附着后方和弦的小装饰音；也可能是提前低音或和弦错开，请比较试听后决定。不会自动采用。',
                measure=lead['measure'], staff=lead['staff'], start_beat=float(start), end_beat=float(anchor_start),
                input_ids=[i], anchor_input_ids=anchor_ids, selected=selected_locator, anchors=anchors,
                source_seconds=[dict(input_id=i, **raw)],
                evidence=dict(model_position=float(model[i][1]), model_duration=float(model[i][2]),
                    model_anchor_position=float(model[anchor_ids[0]][1]),
                    written_duration=str(dur), anchor_delay_seconds=delay,
                    interpretation='ambiguous_short_lead_not_automatic_grace'))
            candidate['id'] = _hash([identity, selected_locator, anchors, candidate['kind']])[:24]
            candidates.append(candidate)
        grouped = _short_group_candidates(groups, performance, model, protected, by_pitch, identity, clock)
        # Do not offer a single suffix and its containing group simultaneously.
        covered={i for c in grouped for i in c['input_ids']}
        candidates=[c for c in candidates if c['kind']!='grace_lead' or not covered.intersection(c['input_ids'])]
        candidates.extend(grouped)
    rejected=[]
    from .notation_free_run import candidate as free_candidate
    for request in manual_requests:
        try:
            item=free_candidate(records,performance,model,clock,identity,request,protected)
        except ValueError as error:
            rejected.append({'request':deepcopy(request),'reason':str(error)})
        else:
            if not any(c['id']==item['id'] for c in candidates):candidates.append(item)
    return dict(version=VERSION, identity=identity, base_xml_sha256=_hash(data),
                rejected_manual_requests=rejected,
                catalog_sha256=_hash([identity, candidates]), candidates=candidates,
                limitations=['Only isolated short leads or irregular 2–4 note lead groups into same-staff chords are proposed.',
                             'No automatic free-flourish, glissando, or tremolo classification.'])


def _short_group_candidates(groups, performance, model, protected, by_pitch, identity, clock):
    """Reviewable irregular leads, never a classifier based only on fast timing."""
    result=[]
    for k,(anchor_start,anchor) in enumerate(groups):
        if len(anchor)<2:continue
        for count in range(min(4,k),1,-1):
            sequence=groups[k-count:k]
            if any(len(nodes)!=1 for _,nodes in sequence):continue
            selected=[nodes[0] for _,nodes in sequence];nodes=selected+anchor
            if any('input_id' not in r or r['input_id'] in protected for r in nodes):continue
            if any(r['grace'] or r['node'].find('tie') is not None or r['node'].find('notations') is not None
                   or r['node'].find('time-modification') is not None for r in nodes):continue
            if len({r['pitch'] for r in nodes})!=len(nodes):continue
            start=selected[0]['start'];span=anchor_start-start
            durations=[r['end']-r['start'] for r in selected]
            if not 0<span<=F(1,2) or any(not 0<d<=F(1,4) for d in durations):continue
            if any(r['end']!=next_start for r,next_start in zip(selected,[r['start'] for r in selected[1:]]+[anchor_start])):continue
            if min(r['end']-anchor_start for r in anchor)<max(F(1,2),2*span):continue
            ids=[r['input_id'] for r in selected];anchor_ids=[r['input_id'] for r in anchor]
            if len({float(model[j][1]) for j in anchor_ids})!=1:continue
            positions=[float(model[i][1]) for i in ids]+[float(model[anchor_ids[0]][1])]
            steps=[b-a for a,b in zip(positions,positions[1:])]
            if any(not 0<d<=.25+1e-8 for d in steps):continue
            # Both model and existing notation must be irregular. Uniform fast
            # subdivisions (including genuine 32nds) remain ordinary rhythm.
            if max(steps)-min(steps)<1e-6 or len(set(durations))==1:continue
            if any(not 0<float(model[i][2])<=.25 or
                   abs(float(model[i][2])-step)>1e-6 for i,step in zip(ids,steps)):continue
            if any(F(str(v)).limit_denominator(192).denominator%3==0 for v in steps):continue
            if min(float(model[i][2]) for i in anchor_ids)<.5:continue
            first=performance[ids[0]]['start'];arrival=min(performance[i]['start'] for i in anchor_ids)
            if not 0<arrival-first<=.25:continue
            times=[performance[i]['start'] for i in ids]+[arrival]
            if any(b<=a for a,b in zip(times,times[1:])):continue
            if any(performance[i]['end']-performance[i]['start']>.15 or
                   performance[i]['end']>next_time+.03 for i,next_time in zip(ids,times[1:])):continue
            if k-count>0 and start-groups[k-count-1][0]<F(1,2):continue
            # The existing score-MIDI matcher requires a unique short source
            # within half a quarter of the grace anchor. Reject ambiguity now.
            if any(abs(clock.to_beat(performance[i]['start'])-float(anchor_start))>.5 for i in ids):continue
            if any(any(j!=i and abs(clock.to_beat(performance[j]['start'])-float(anchor_start))<=.5
                       for j in by_pitch[performance[i]['pitch']]) for i in ids):continue
            locators=[_locator(r) for r in selected];anchors=[_locator(r) for r in anchor]
            candidate=dict(kind='grace_group',label='短引奏：装饰音组或独立节奏',
                explanation='同手短音组可试写为连梁小装饰音；也可能是独立切分节奏，保留全部音高与原始演奏，仅在明确采用后改谱。',
                measure=selected[0]['measure'],staff=selected[0]['staff'],start_beat=float(start),end_beat=float(anchor_start),
                input_ids=ids,anchor_input_ids=anchor_ids,selected=locators,anchors=anchors,
                source_seconds=[dict(input_id=i,**performance[i]) for i in ids],
                evidence={'model_positions':positions,'model_steps':steps,'written_durations':list(map(str,durations)),
                          'group_seconds':arrival-first,'interpretation':'ambiguous_irregular_lead_group_explicit_adoption_only'})
            candidate['id']=_hash([identity,locators,anchors,candidate['kind']])[:24]
            result.append(candidate);break
    return result


def _validate_catalog(catalog):
    if catalog.get('version') != VERSION or catalog.get('catalog_sha256') != _hash([catalog['identity'], catalog['candidates']]):
        raise ValueError('Ornament catalog is stale or corrupt')


def validate_group_playback(catalog, accepted_ids, score_midi_audit, tempo):
    """New adopted groups may not silently become the generic 50ms fallback."""
    _validate_catalog(catalog);clock=TempoMap(tempo.data if isinstance(tempo,TempoMap) else tempo)
    rows=score_midi_audit.get('grace_notes',[]);checked=[];used=set()
    for candidate in catalog['candidates']:
        if candidate['id'] not in accepted_ids or candidate['kind']!='grace_group':continue
        for source in candidate['source_seconds']:
            matches=[index for index,row in enumerate(rows) if index not in used and
                     row['pitch']==source['pitch'] and row['method']=='matched_short_performance' and
                     abs(clock.to_seconds(row['start_beat'])-source['start'])<=.002 and
                     abs(clock.to_seconds(row['end_beat'])-source['end'])<=.002]
            if len(matches)!=1:
                raise ValueError('已采用装饰音组无法逐音保留原演奏时间，已停止导出；不会静默使用短音近似')
            used.add(matches[0]);checked.append(source['input_id'])
    return {'matched_group_input_ids':checked,'fallback_allowed':False,'source_seconds_tolerance':.002}


def create_review(path, catalog):
    _validate_catalog(catalog); path = Path(path)
    if path.exists(): raise ValueError('Ornament review already exists; do not overwrite decisions')
    state = dict(version=VERSION, catalog_sha256=catalog['catalog_sha256'], revision=0,
                 decisions={}, history=[], undo_stack=[])
    # Exclusive create makes two callers unable to replace an existing review.
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x', encoding='utf-8') as out: json.dump(state, out, ensure_ascii=False)
    return state


def _mutate(path, catalog, expected_revision, action):
    _validate_catalog(catalog); path = Path(path); lock = path.with_suffix(path.suffix+'.lock')
    try: fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    except FileExistsError: raise ValueError('Ornament review is being changed; retry with current revision') from None
    try:
        os.close(fd); state = json.loads(path.read_text(encoding='utf-8'))
        if state['catalog_sha256'] != catalog['catalog_sha256'] or state['revision'] != expected_revision:
            raise ValueError('Ornament review revision/catalog changed')
        before = deepcopy(state['decisions']); operation = action(state)
        state['history'].append(dict(revision=state['revision']+1, operation=operation,
                                     before=before, after=deepcopy(state['decisions'])))
        state['revision'] += 1
        _atomic(path, json.dumps(state, ensure_ascii=False, indent=2).encode())
        return state
    finally: lock.unlink(missing_ok=True)


def decide(path, catalog, expected_revision, candidate_id, decision):
    if decision not in ('accept', 'reject', 'clear'): raise ValueError('Unknown ornament decision')
    if candidate_id not in {c['id'] for c in catalog['candidates']}: raise ValueError('Unknown ornament candidate')
    def apply(state):
        state['undo_stack'].append(deepcopy(state['decisions']))
        if decision == 'clear': state['decisions'].pop(candidate_id, None)
        else: state['decisions'][candidate_id] = decision
        return dict(candidate_id=candidate_id, decision=decision)
    return _mutate(path, catalog, expected_revision, apply)


def undo(path, catalog, expected_revision):
    def apply(state):
        if not state['undo_stack']: raise ValueError('No ornament decision to undo')
        state['decisions'] = state['undo_stack'].pop()
        return 'undo'
    return _mutate(path, catalog, expected_revision, apply)


def render_review(base_xml, catalog, review, output_xml, *, current_context=None):
    """Render accepted choices from immutable base; never edit that base.

    Returns exact nonselected interval and measure-layout gates. Caller renders
    PDF and rebuilds score MIDI from this XML with original performance_notes.
    """
    _validate_catalog(catalog)
    bound_context = catalog['identity'].get('context')
    if bound_context and current_context != bound_context:
        raise ValueError('Project/editor/export context changed; regenerate ornament review')
    for source in catalog['identity'].get('source_files', []):
        if _hash(Path(source['path']).read_bytes()) != source['sha256']:
            raise ValueError('Original ornament source or prediction changed')
    base_xml, output_xml = Path(base_xml), Path(output_xml)
    if base_xml.resolve() == output_xml.resolve(): raise ValueError('Ornament output cannot overwrite its base')
    data = base_xml.read_bytes(); root = ET.fromstring(data)
    records = _records(root)
    if _semantic_from_records(root, records) != catalog['identity']['notation_sha256']:
        raise ValueError('Notation changed; regenerate ornament candidates before applying')
    if review['catalog_sha256'] != catalog['catalog_sha256']: raise ValueError('Ornament review belongs to another catalog')
    ids = {c['id'] for c in catalog['candidates']}
    if any(k not in ids or v not in ('accept', 'reject') for k,v in review['decisions'].items()):
        raise ValueError('Invalid ornament decisions')
    accepted = [c for c in catalog['candidates'] if review['decisions'].get(c['id']) == 'accept']
    if not accepted:
        _atomic(output_xml, data)
        data_hash = _hash(data)
        return dict(revision=review['revision'], accepted_ids=[],
                    output_sha256=data_hash, base_sha256=data_hash,
                    nonselected_events_unchanged=True, measure_boundaries_unchanged=True,
                    raw_performance_modified=False, score_midi_requires_rebuild=True,
                    free_run_source_input_ids=[],
                    grace_playback='Rebuild with write_score_midi(..., performance_notes=original_notes); inspect grace_notes match audit.')
    before = sounding_signature(BytesIO(data)); expected = before.copy(); layout = _layout(root)
    lookup = {_hash(_locator(r)): r for r in records}; used = set(); dirty = []; grace_groups=[]
    for candidate in accepted:
        if candidate['kind']=='free_run':continue
        if candidate['kind'] not in ('grace_lead','grace_group'): raise ValueError('Unsupported ornament transformation')
        selected = [lookup[_hash(x)] for x in candidate['selected']]
        anchor = lookup[_hash(candidate['anchors'][0])]
        group=[]
        for r in selected:
            key = _hash(_locator(r))
            if key in used: raise ValueError('Overlapping ornament choices')
            used.add(key); node = r['node']; measure = r['measure_node']
            ordinary = (r['key'], r['start'], r['end'], False)
            if expected[ordinary] != 1: raise ValueError('Ambiguous selected ornament interval')
            expected[ordinary] -= 1
            expected[(r['key'], anchor['start'], anchor['start'], True)] += 1
            grace = deepcopy(node)
            for child in list(grace):
                if child.tag in ('duration','tie','chord','time-modification','beam','dot','notations'):
                    grace.remove(child)
            grace.insert(0, ET.Element('grace', {'slash':'yes'})); grace.find('type').text = 'eighth'
            # Retain elapsed written time as silence. Moving only this display
            # note must not shift the principal chord or subsequent barline.
            rest = ET.Element('note'); ET.SubElement(rest, 'rest')
            for name in ('duration','voice','type','dot','staff'):
                for child in node.findall(name): rest.append(deepcopy(child))
            index = list(measure).index(node); measure.remove(node); measure.insert(index, rest)
            measure.insert(list(measure).index(anchor['node']), grace)
            group.append(grace)
            dirty.append((measure, r['staff'], r['voice'], r['divisions']))
        grace_groups.append(group)
    # Rebeam changed lanes with mature meter grouping. Unrelated voice/staff
    # notes and every time-bearing direction stay in their original positions.
    for measure, staff, voice, div in dirty:
        _merge_generated_rests(measure, staff, voice, div)
        nodes = [n for n in measure.findall('note') if n.findtext('staff','1') == staff and n.findtext('voice','1') == voice]
        for node in nodes:
            for beam in list(node.findall('beam')): node.remove(beam)
        meter = (4,4)
        for part, m, _, _, _ in _measure_layout(root):
            time = m.find('attributes/time')
            if time is not None:
                meter = ((0, 0) if time.find('senza-misura') is not None else
                         (int(time.findtext('beats')), int(time.findtext('beat-type'))))
            if m is measure: break
        _restore_missing_beams(nodes, div, meter)
    # Grace beams are independent from the ordinary rhythmic lane (MusicXML).
    # Restore them after the normal rebeam pass, retaining source pitch order.
    for group in grace_groups:
        if len(group)<2:continue
        for index,node in enumerate(group):
            for beam in list(node.findall('beam')):node.remove(beam)
            ET.SubElement(node,'beam',{'number':'1'}).text=('begin' if index==0 else 'end' if index==len(group)-1 else 'continue')
    from .notation_free_run import transform as transform_free_runs
    free_entries=transform_free_runs(root,accepted,lookup,expected,used)
    encoded = data if not accepted else ET.tostring(root, encoding='utf-8', xml_declaration=True)
    if sounding_signature(BytesIO(encoded)) != +expected or _layout(ET.fromstring(encoded)) != layout:
        raise ValueError('Ornament engraving changed an unselected event or measure boundary')
    _atomic(output_xml, encoded)
    return dict(revision=review['revision'], accepted_ids=[c['id'] for c in accepted],
                output_sha256=_hash(encoded), base_sha256=_hash(data),
                nonselected_events_unchanged=True, measure_boundaries_unchanged=True,
                raw_performance_modified=False, score_midi_requires_rebuild=True,
                free_run_source_input_ids=[e['input_id'] for e in free_entries],
                grace_playback='Rebuild with write_score_midi(..., performance_notes=original_notes); inspect grace_notes match audit.')
