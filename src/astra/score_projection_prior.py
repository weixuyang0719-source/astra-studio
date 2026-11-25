"""Identity-checked original DuET priors for cheap, inference-free previews."""
from pathlib import Path
from bisect import bisect_left, bisect_right
import math
import hashlib
import json


def _digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True).encode()).hexdigest()


def _map_prior_notes(notes, prior):
    """Match exact performance identities without scanning a whole song per note."""
    by_pitch={}
    for i,(onset,pitch) in enumerate(zip(prior['onsets'],prior['pitches'])):
        by_pitch.setdefault(pitch,[]).append((onset,i))
    for entries in by_pitch.values():entries.sort()
    used=set();mapping={}
    for note in notes:
        entries=by_pitch.get(note['pitch'],[]);onset=note['start']
        # Expand only the search boundary by one ULP; the original exact
        # absolute-difference predicate still decides eligibility below.
        lo=bisect_left(entries,(math.nextafter(onset-.001,-math.inf),-1))
        hi=bisect_right(entries,(math.nextafter(onset+.001,math.inf),math.inf))
        candidates=[i for t,i in entries[lo:hi] if i not in used
                    and abs(t-onset)<=.001 and abs(prior['offsets'][i]-note['end'])<=.001]
        if len(candidates)!=1:
            raise ValueError('原始记谱先验无法唯一对应当前音符，请重新制谱。')
        index=candidates[0];used.add(index);mapping[note['id']]=index
    return mapping


def _bound_cache_file(xml, recorded, expected, kind):
    """Prefer a moved project's own exact bytes; never select by name alone."""
    xml=Path(xml).resolve();recorded=Path(recorded);candidates=[]
    if xml.parent.parent.name=='exports':
        project=xml.parent.parent.parent;cache=project/'cache'
        folder,filename=('duet','score.npz') if kind=='prediction_file' else ('duet_inputs','piano_raw.mid')
        if cache.resolve().is_relative_to(project):
            local=cache/folder
            candidates.append(local/recorded.parent.name/filename)
            candidates.extend(sorted(local.glob('*/'+filename)))
            candidates=[p for p in candidates if p.resolve().is_relative_to(cache.resolve())]
    candidates.append(recorded)
    seen=set()
    for path in candidates:
        if path in seen:continue
        seen.add(path)
        if path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest()==expected:
            return path.resolve()
    return None


class StaleProjectionEvents(ValueError):
    """Verified baseline prediction cannot apply to the current MIDI events."""


def load_projected_prior(xml, base_doc, doc, clock):
    from .score_duet_export import _performance_digest,controller_events_digest
    from .duet_notation import project_model_notation
    report_path=Path(xml).with_name('piano_notation_report.json')
    if not report_path.is_file():return None
    report=json.loads(report_path.read_text(encoding='utf8'))
    prior=report.get('projection_prior');binding=report.get('projection_prior_identity')
    if prior is None or binding is None:
        raise ValueError('旧正式谱缺少可验证的原始记谱先验，请先复用现有 DuET 缓存重新制谱一次，再预览拍轴编辑。')
    if prior.get('kind')!='original_duet_prediction' or prior.get('version')!=1 or _digest(prior)!=binding.get('prior_sha256'):
        raise ValueError('原始记谱先验身份损坏，请重新制谱。')
    source=binding['source_events']
    # v1 bound the full editor CC objects. Verify that historical baseline
    # before migrating; UI IDs/origin alone never alter a source MIDI event.
    if binding.get('controls_identity')=='ordered_raw_events_v1':
        controls_hash=controller_events_digest(base_doc.get('controls'))
    else:
        controls_hash=_digest(base_doc.get('controls'))
    if controls_hash!=binding.get('controls_sha256'):
        raise ValueError('基线控制事件与原始记谱先验不匹配，请重新制谱。')
    baseline_cc=controller_events_digest(base_doc.get('controls'))
    if _performance_digest(base_doc['notes'],source['pedals'],base_doc.get('controls'))!=binding['performance_sha256']:
        raise ValueError('基线音符事件与原始记谱先验不匹配，请重新制谱。')
    stale_events=(_performance_digest(doc['notes'],source['pedals'],doc.get('controls'))!=binding['performance_sha256']
                  or controller_events_digest(doc.get('controls'))!=baseline_cc)
    resolved={}
    for field,digest in (('input_midi','input_sha256'),('prediction_file','prediction_sha256')):
        path=_bound_cache_file(xml,binding[field],binding[digest],field)
        if path is None or report['identity'][digest]!=binding[digest]:
            raise ValueError('原始 MIDI 或 DuET 缓存与记谱先验不匹配，请重新制谱。')
        resolved[field]=str(path)
    if stale_events:
        raise StaleProjectionEvents('音符或控制事件已变化，不能复用旧预测先验；请重新导出乐谱，或撤销事件修改后继续预览。')
    fields=('positions','durations','onsets','offsets','pitches','staffs')
    if any(len(prior[f])!=len(doc['notes']) for f in fields):
        raise ValueError('原始记谱先验音符数与当前输入不匹配。')
    # The model IDs follow MIDI order, not editor row order. Require a unique
    # source-time/pitch match; never guess from an already written beat.
    mapping=_map_prior_notes(doc['notes'],prior)
    protected={mapping[n['id']] for n in doc['notes'] if n.get('origin')=='manual'}
    revisions=report.get('notation_revisions',[])
    if revisions:
        raise ValueError('此正式谱含人工记谱修订，请通过缓存正式制谱复核新拍轴后再预览。')
    projected=project_model_notation(prior,clock,grid=report['identity'].get('grid','auto'),
        protected_input_ids=protected,protected_anchor_seconds=[a['seconds'] for a in doc.get('anchors',[])],
        manual_collision_input_ids=protected)
    rows={key:dict(input_id=i,start=projected['onsets'][i],duration=projected['durations'][i],
                   staff=prior['staffs'][i]+1,grace=prior['durations'][i]==0) for key,i in mapping.items()}
    # Formal post-layout rolls may consolidate only these explicitly accepted
    # input IDs. No unrelated note is moved by this preview interpretation.
    by_input={row['input_id']:row for row in rows.values()}
    for group in projected['compact_model_rolls']['groups']:
        for i in group['input_ids']:
            by_input[i].update(start=group['onset_quarters'],duration=group['duration_quarters'])
    return dict(rows=rows,projected=projected,identity={k:binding[k] for k in
                ('performance_sha256','controls_sha256','input_sha256','prediction_sha256','prior_sha256')},
                source_report=str(report_path),resolved_cache_files=resolved,
                controls_identity='ordered_raw_events_v1',verified_controls_sha256=baseline_cc)
