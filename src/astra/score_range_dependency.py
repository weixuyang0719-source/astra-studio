"""Local preview validity, independent of whole-score freshness.

Tempo/anchors remain deliberately global dependencies: current native-prior
projection can align model sequences over the full input. Do not pretend a
clock change outside the visible bars is proven independent yet.
"""
import hashlib
import json
from pathlib import Path
from copy import deepcopy
from functools import lru_cache
from collections import OrderedDict
from threading import Lock

def _hash(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()

@lru_cache(maxsize=16)
def _written_prior(xml_path,xml_hash,snapshot_path,snapshot_hash):
    from .score_range import _duration_priors
    return _duration_priors(Path(xml_path),json.loads(Path(snapshot_path).read_text(encoding='utf8')))


@lru_cache(maxsize=16)
def _bound_prior_paths(report_name,mtime,size):
    report=Path(report_name)
    binding=json.loads(report.read_text(encoding='utf8')).get('projection_prior_identity',{})
    from .score_projection_prior import _bound_cache_file
    paths=[]
    for kind,digest in (('input_midi','input_sha256'),('prediction_file','prediction_sha256')):
        if binding.get(kind) and binding.get(digest):
            path=_bound_cache_file(report.with_name('piano.musicxml'),binding[kind],binding[digest],kind)
            # Missing source is retained as a dependency, not silently forgotten.
            paths.append(path or Path(binding[kind]))
    return tuple(paths)

def _prior_paths(base):
    report=Path(base)/'piano_notation_report.json'
    if not report.is_file():return ()
    st=report.stat()
    return _bound_prior_paths(str(report),st.st_mtime_ns,st.st_size)

def dependency_signature(project,start,end,*,doc=None,context_bars=1,ignore_clock=False):
    from . import editing
    from .score_artifacts import resolve_score_export
    from .score_range import _context,_duration_priors,VERSION,_clock_content
    from .timing import TempoMap
    project=Path(project).resolve()
    doc=doc if doc is not None else editing.current(project)
    if not doc:raise ValueError('No editing document')
    base=resolve_score_export(project,source_key=doc.get('source_key'))
    if base is None:raise ValueError('No retained notation prior')
    snapshot=json.loads((base/'editor_snapshot.json').read_text(encoding='utf8'))
    clock=TempoMap(deepcopy(doc['tempo']))
    starts,lengths,meters,a,b=_context(clock,float(start),float(end),context_bars)
    lo,hi=clock.to_seconds(starts[0]),clock.to_seconds(starts[-1]+lengths[-1])
    # One-quarter guard protects notes snapped across the displayed boundary.
    left=clock.to_seconds(max(0,starts[0]-1))
    right=clock.to_seconds(min(clock.beats[-1],starts[-1]+lengths[-1]+1))
    xml_hash=hashlib.sha256((base/'piano.musicxml').read_bytes()).hexdigest()
    snapshot_hash=hashlib.sha256((base/'editor_snapshot.json').read_bytes()).hexdigest()
    prior=_written_prior(str(base/'piano.musicxml'),xml_hash,str(base/'editor_snapshot.json'),snapshot_hash)
    oldclock=TempoMap(deepcopy(snapshot['tempo']))
    def relevant(note):
        if note['start']<=right and note['end']>=left:return True
        written=prior.get(note['id'])
        return bool(written and oldclock.to_seconds(written['start'])<=right and
                    oldclock.to_seconds(written['start']+written['duration'])>=left)
    notes=[n for n in doc['notes'] if relevant(n)]
    # A deletion of an incoming written tie must also invalidate the preview.
    local_old_ids={n['id'] for n in snapshot['notes'] if relevant(n)}
    live_ids={n['id'] for n in doc['notes']}
    controls=[];before={}
    for control in sorted(doc.get('controls',[]),key=lambda c:c['seconds']):
        if control.get('number')!=64:continue
        if control['seconds']<left:before[control.get('channel',0)]=control
        elif control['seconds']<=right:controls.append(control)
    boundary_cc=[{'channel':channel,'value':c['value']} for channel,c in sorted(before.items())]
    artifacts={}
    for name in ('piano.musicxml','editor_snapshot.json','piano_notation_report.json','piano_harmony.json'):
        path=base/name
        artifacts[name]=hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
    for path in _prior_paths(base):
        artifacts[str(path)]=hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
    payload={'version':VERSION,'range':[start,end],'context_bars':context_bars,
             'context':[lo,hi,a,b,starts,lengths,meters],
             'source_key':doc.get('source_key'),'duration':doc.get('duration'),
             'notes':notes,'deleted_local_ids':sorted(local_old_ids-live_ids),
             'controls':controls,'boundary_cc64':boundary_cc,
             'pedals':[p for p in doc.get('pedals',[]) if p.get('start',0)<=right and p.get('end',0)>=left],
             'tempo':_clock_content(doc['tempo']),'anchors':doc.get('anchors',[]),
             'other_events':doc.get('other_events',[]),'notation_prior':artifacts}
    # Native-prior projection validates/aligns the entire performance and uses
    # manual origins globally. Remote notes/CC cannot take the local fast path.
    # The worker proof still compares actual projected graphs for equivalence.
    if not ignore_clock and _clock_content(doc['tempo'])!=_clock_content(snapshot['tempo']) and (base/'piano_notation_report.json').is_file():
        payload['global_projection_input']={'notes':doc['notes'],'controls':doc.get('controls',[])}
    if ignore_clock:
        payload.pop('tempo',None);payload.pop('anchors',None)
    return _hash(payload)


def source_stamp(project,export):
    """Cheap change detector; fingerprints still verify content after a change."""
    paths=[Path(project)/'project.json']
    if export:
        paths.extend(Path(export)/name for name in ('piano.musicxml','editor_snapshot.json','piano_notation_report.json','piano_harmony.json'))
        paths.extend(_prior_paths(export))
    def stamp(path):
        try:
            st=path.stat();return (str(path),st.st_mtime_ns,st.st_size)
        except OSError:return (str(path),None,None)
    return _hash([stamp(path) for path in paths])



_GRAPH_CACHE=OrderedDict()
_GRAPH_CACHE_LOCK=Lock()

def _projection_graph(base,base_doc,doc,start,end):
    from .score_range import _context,_draft
    from .timing import TempoMap
    files={}
    for name in ('piano.musicxml','piano_notation_report.json','piano_harmony.json'):
        path=base/name
        files[name]=hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
    for path in _prior_paths(base):
        files[str(path)]=hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
    key=_hash({'base':str(base),'files':files,'base_doc':base_doc,'doc':doc,'range':[start,end]})
    with _GRAPH_CACHE_LOCK:
        cached=_GRAPH_CACHE.get(key)
        if cached is not None:
            _GRAPH_CACHE.move_to_end(key)
            return deepcopy(cached)
    starts,lengths,meters,*_=_context(TempoMap(deepcopy(doc['tempo'])),start,end,1)
    graph=_draft(base/'piano.musicxml',base_doc,doc,starts,lengths,meters,None,projection_only=True)
    with _GRAPH_CACHE_LOCK:
        _GRAPH_CACHE[key]=deepcopy(graph);_GRAPH_CACHE.move_to_end(key)
        while len(_GRAPH_CACHE)>16:_GRAPH_CACHE.popitem(last=False)
    return graph

def compare_clock_projection(project,snapshot,start,end):
    """Worker-only proof: same dependencies and same locally generated notation.

Any unsupported prior, malformed timing, or projection ambiguity fails closed.
No PDF renderer, model inference, or project mutation is involved.
"""
    import tempfile
    import xml.etree.ElementTree as ET
    from . import editing
    from .score_range import _context,_draft,_compact_preview_layout
    from .score_artifacts import resolve_score_export
    from .timing import TempoMap
    old=json.loads(Path(snapshot).read_text(encoding='utf8'))
    current=editing.current(project)
    revision=current['revision']
    signature=dependency_signature(project,start,end,doc=current)
    if dependency_signature(project,start,end,doc=old,ignore_clock=True)!=dependency_signature(project,start,end,doc=current,ignore_clock=True):
        return {'equivalent':False,'revision':revision,'signature':signature,'reason':'local_dependencies_changed'}
    base=resolve_score_export(project,source_key=current.get('source_key'))
    base_doc=json.loads((base/'editor_snapshot.json').read_text(encoding='utf8'))
    # Reuse the actual projection stage and compare its complete engraving inputs.
    # Equal nodes prove downstream output equality without building two scores.
    try:
        graphs=[]
        for doc in (old,current):
            graphs.append(_projection_graph(base,base_doc,doc,start,end))
        if graphs[0]['signature']==graphs[1]['signature']:
            fresh=editing.current(project)['revision']==revision and dependency_signature(project,start,end)==signature
            return {'equivalent':fresh,'revision':revision,'signature':signature,
                    'reason':'same_engraving_inputs' if fresh else 'superseded',
                    'proof_method':'dependency_graph','dependency_nodes':graphs[1]['nodes']}
        if any(graphs[0]['nodes'][key]!=graphs[1]['nodes'][key] for key in ('sounding_events','preserved_note_marks')):
            # _draft enforces exact equality between these events and rendered
            # sounding_signature; different events therefore cannot yield the
            # same valid notation. _note also preserves non-tie/tuplet
            # decorations at attack/release; their differences are observable.
            # No second full engraving is needed.
            fresh=editing.current(project)['revision']==revision and dependency_signature(project,start,end)==signature
            return {'equivalent':False,'revision':revision,'signature':signature,
                    'reason':'local_notation_events_changed' if fresh else 'superseded',
                    'proof_method':'dependency_graph'}
    except (OSError,ValueError,KeyError,TypeError,RuntimeError):
        pass  # Unknown projection contract falls back to the existing full proof.
    outputs=[]
    with tempfile.TemporaryDirectory(prefix='astra-local-proof-') as temp:
        for index,doc in enumerate((old,current)):
            starts,lengths,meters,*_=_context(TempoMap(deepcopy(doc['tempo'])),start,end,1)
            out=Path(temp)/f'{index}.musicxml'
            _draft(base/'piano.musicxml',base_doc,doc,starts,lengths,meters,out)
            _compact_preview_layout(out)
            outputs.append(ET.canonicalize(out.read_text(encoding='utf8')))
    if editing.current(project)['revision']!=revision or dependency_signature(project,start,end)!=signature:
        return {'equivalent':False,'revision':revision,'reason':'superseded'}
    return {'equivalent':outputs[0]==outputs[1],'revision':revision,'signature':signature,
            'reason':'same_local_projection' if outputs[0]==outputs[1] else 'local_projection_changed',
            'proof_method':'full_notation_fallback'}
