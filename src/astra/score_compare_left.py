from dataclasses import dataclass,field
from collections import OrderedDict
from copy import deepcopy
from hashlib import sha256
from math import isfinite
from functools import lru_cache
import json
import math,stat
from xml.etree import ElementTree as ET
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter

_EXPORT_ARTIFACTS=('report.json','piano.musicxml','editor_snapshot.json','piano_notation_report.json','piano_harmony.json')
_PREVIEW_KINDS={'duet_current','duet_formal_slice','duet_cached_draft'}
_CURRENT_XML=OrderedDict()


def _canonical_stamp(path, *, missing=False, limit=64*1024*1024):
    # These are machine-generated files under this project's own export/cache.
    # Never follow right-editor artifacts, links, or paths picked by the web UI.
    if path.resolve(strict=False)!=path or path.is_symlink():
        raise ValueError('乐谱缓存路径已变化。')
    try:info=path.lstat()
    except FileNotFoundError:
        if missing:return None
        raise
    if (not stat.S_ISREG(info.st_mode) or getattr(info,'st_file_attributes',0)&0x400
            or not 0<info.st_size<=limit):
        raise ValueError('乐谱缓存不是可验证的本地文件。')
    return (info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns,info.st_ctime_ns)


@lru_cache(maxsize=32)
def _hash_artifact(path,stamp):
    h=sha256()
    with path.open('rb') as stream:
        for block in iter(lambda:stream.read(256*1024),b''):
            h.update(block)
    if _canonical_stamp(path)!=stamp:
        raise ValueError('乐谱缓存正在变化。')
    return h.hexdigest()


@dataclass(frozen=True)
class _MachinePrior:
    export: Path
    document: dict = field(repr=False)
    files: tuple = field(repr=False)
    identity: str

    def check(self):
        if any(_canonical_stamp(path,missing=True)!=stamp for path,stamp in self.files):
            raise ValueError('记谱缓存已变化，请刷新临时谱。')


def _machine_prior(source):
    """Bind only the committed, generated DuET artifacts, never the right editor."""
    from .score_artifacts import resolve_score_export
    from .score_duet_export import _performance_digest,controller_events_digest
    from .score_projection_prior import _digest
    from .score_range import _clock_content
    root=Path(source.project)
    metadata=root/'project.json'
    stamp=_canonical_stamp(metadata)
    from .pipeline import read_json
    state=read_json(metadata)
    committed={Path(value).name for field in ('latest_export','latest_score_export')
               if isinstance(value:=state.get(field),str) and value}
    if not committed:return None
    exported=resolve_score_export(root,state,source.source_key)
    if (exported is None or exported.parent!=root/'exports' or exported.name not in committed
            or exported.resolve(strict=True)!=exported):
        return None
    refs={}
    for name in _EXPORT_ARTIFACTS:
        p=exported/name
        refs[p]=_canonical_stamp(p,missing=name=='piano_harmony.json')
    report=json.loads((exported/'report.json').read_bytes())
    formal=json.loads((exported/'editor_snapshot.json').read_bytes())
    notation=json.loads((exported/'piano_notation_report.json').read_bytes())
    piano=report['instruments']['piano']
    identity=notation['identity'];binding=notation['projection_prior_identity']
    prior=notation['projection_prior']
    xml=exported/'piano.musicxml'
    xml_hash=_hash_artifact(xml,refs[xml])
    output_hashes=((piano.get('pdf_render') or {}).get('source_sha256'),
                   (piano.get('score_midi') or {}).get('source_xml_sha256'))
    if (piano.get('score_engine')!='duet' or report['editor_revision']!=formal['revision']
            or formal['source_key']!=source.source_key or notation.get('status')!='complete'
            or xml_hash not in output_hashes or binding.get('version')!=2
            or binding.get('controls_identity')!='ordered_raw_events_v1'
            or prior.get('kind')!='original_duet_prediction' or prior['version']!=1
            or _digest(prior)!=binding['prior_sha256']
            or _clock_content(identity['reference'])!=_clock_content(formal['tempo'])
            or controller_events_digest(formal.get('controls'))!=binding['controls_sha256']
            or _performance_digest(formal['notes'],binding['source_events']['pedals'],formal.get('controls'))!=binding['performance_sha256']):
        return None
    for field,hash_key,cache,name in (('input_midi','input_sha256','duet_inputs','piano_raw.mid'),
                                     ('prediction_file','prediction_sha256','duet','score.npz')):
        recorded=Path(binding[field])
        # Resolve moved/copied projects from their own cache. The recorded
        # path is provenance, not permission to read outside this project.
        if field=='input_midi' and recorded.name=='selected_source.mid':
            p=root/'selected_source.mid'
        elif (recorded.name==name and recorded.parent.parent.name==cache
              and recorded.parent.parent.parent.name=='cache'
              and recorded.parent.name not in ('','.','..')):
            p=root/'cache'/cache/recorded.parent.name/name
        else:
            return None
        refs[p]=_canonical_stamp(p)
        if identity[hash_key]!=binding[hash_key] or _hash_artifact(p,refs[p])!=binding[hash_key]:
            return None
    refs[metadata]=stamp
    pin=_MachinePrior(exported,formal,tuple(refs.items()),sha256(json.dumps(
        [(str(p.relative_to(root)),_hash_artifact(p,s) if s is not None else None)
         for p,s in refs.items()],ensure_ascii=False).encode()).hexdigest())
    pin.check()
    return pin


def _duet_left(source,doc,clock,start,end,chosen,path):
    from .score_range import _clock_content,_draft,_formal_context,_formal_slice
    from .score_range_dependency import _written_prior
    pin=_machine_prior(source)
    if pin is None:return None
    formal=pin.document;xml=pin.export/'piano.musicxml'
    same_clock=_clock_content(doc['tempo'])==_clock_content(formal['tempo'])
    same_revision=(doc['revision']==formal['revision'] and same_clock and
                    all(doc.get(key)==formal.get(key)
                        for key in ('notes','controls','other_events','duration','anchors')))
    if same_revision:
        context=_formal_context(xml,clock,start,end,0)
        audit=_formal_slice(xml,clock,*context[:2],path)
        kind='duet_formal_slice'
    else:
        # Get the exact pre-layout durations once per canonical source; reuse
        # the existing written-prior cache, not the right pane's document.
        files=dict(pin.files);snapshot=pin.export/'editor_snapshot.json'
        priors=_written_prior(str(xml),_hash_artifact(xml,files[xml]),
                              str(snapshot),_hash_artifact(snapshot,files[snapshot]))
        context=chosen
        from .score_projection_prior import StaleProjectionEvents
        try:
            audit=_draft(xml,formal,doc,*context[:3],path,allow_current_midi_preview=False,written_priors=priors)
        except StaleProjectionEvents:
            audit=_draft_with_remote_edits(xml,formal,doc,clock,context,path,priors)
            if audit is None:raise
        if not same_clock and audit.get('projection_prior_identity') is None:
            return None # no unproven reuse after broad clock changes
        kind='duet_cached_draft'
    pin.check()
    return kind,context,audit,pin


def _draft_with_remote_edits(xml,formal,doc,clock,context,path,priors):
    """Use the verified original model input only for an unaffected excerpt.

    The edited clock is real. A changed input note or controller near the
    excerpt must instead go through current DuET inference.
    """
    from .score_duet_export import controller_events_digest
    from .score_range import _draft
    starts,lengths= context[:2]
    lo=clock.to_seconds(starts[0])
    hi=clock.to_seconds(starts[-1]+lengths[-1])
    if doc['duration']!=formal['duration']:return None
    if controller_events_digest(doc.get('controls'))!=controller_events_digest(formal.get('controls')):
        return None
    base={n['id']:n for n in formal['notes']}
    current={n['id']:n for n in doc['notes']}
    changed={key for key in base.keys()|current.keys() if base.get(key)!=current.get(key)}
    # Preserve at least one original-model history window of musical context.
    margin=8.0
    if any(n['start']<hi+margin and n['end']>lo-margin
           for key in changed for n in (base.get(key),current.get(key)) if n):
        return None
    audit=_draft(xml,formal,dict(doc,notes=formal['notes']),*context[:3],path,
                 allow_current_midi_preview=False,written_priors=priors)
    # Explicitly reject any changed note which the projected notation would
    # carry into the slice, even if its raw source range was far away.
    if not audit.get('projection_prior_identity') or changed.intersection(audit['note_ids']):
        return None
    audit['reused_duet_context']=True
    return audit


def _duet_current(source, doc, clock, start, end, path, frozen_hash, check, progress):
    """Reuse full current engraving by frozen content; slice only after engraving."""
    from . import editing
    from .pipeline import read_json, _pedals_in_duration
    from .score_duet_export import export_duet_score, _performance_inputs, _performance_digest
    from .duet_score import cached_prediction
    from .score_range import _formal_context, _formal_slice

    project=Path(source.project)
    state=read_json(project/'project.json')
    config=state.get('config',{})
    options=dict(config.get('score',{}),grid=config.get('quantization','auto'),
                 _notation_fixed_anchor_seconds=[float(a['seconds']) for a in doc.get('anchors',[])])
    title=Path(state['source']).stem.split(' - ',1)[-1]
    key=(source.project,frozen_hash,title,json.dumps(options,sort_keys=True))
    full=_CURRENT_XML.get(key)
    inference_run=False
    def report(line):
        from .preview_cancel import check_cancelled
        check_cancelled()
        if '[harmony]' in line or '整理乐谱' in line:
            progress({'phase':'engraving'})
        elif line.startswith('[duet] {'):
            try:item=json.loads(line[len('[duet] '):])
            except ValueError:return
            done,total=item.get('completed_windows'),item.get('total_windows')
            if type(done) is int and type(total) is int and 0<=done<=total:
                progress({'phase':'prediction','completed':done,'total':total})
    with TemporaryDirectory(prefix='current-duet-',dir=_preview_folder()) as td:
        scratch=Path(td)
        if full is None:
            notes, controls=doc['notes'],doc['controls']
            pedals=list(_pedals_in_duration(editing.pedal_intervals(controls,doc['duration']),doc['duration']))
            # The model has already seen this exact performance if a formal
            # job prepared its content-addressed immutable input. Seed only a
            # validated prediction, and never write to the project's cache.
            original=_performance_inputs(project,notes,pedals,controls)/'piano_raw.mid'
            prepared=None
            if original.is_file():
                hit=cached_prediction(original,project/'cache'/'duet',title=title)
                if hit:
                    manifest=Path(hit['report']).parent/'astra-cache.json'
                    prepared=dict(midi=str(original),prediction=hit,
                        performance_sha256=_performance_digest(notes,pedals,controls),
                        input_sha256=sha256(original.read_bytes()).hexdigest(),
                        prediction_manifest_sha256=sha256(manifest.read_bytes()).hexdigest(),title=title)
            check()
            progress({'phase':'engraving' if prepared else 'prediction'})
            result=export_duet_score(notes,pedals,doc['tempo'],scratch,scratch,
                controls=controls,options=options,title=title,log=report,
                prepared_prediction=prepared,preview_only=True)
            check()
            inference_run=not result['prediction_cached']
            full=Path(result['musicxml']).read_bytes()
            if len(full)<=12_000_000:
                _CURRENT_XML[key]=full
                while len(_CURRENT_XML)>2:_CURRENT_XML.popitem(last=False)
        check()
        progress({'phase':'layout'})
        xml=scratch/'full.musicxml';xml.write_bytes(full)
        context=_formal_context(xml,clock,start,end,0)
        audit=_formal_slice(xml,clock,*context[:2],path)
    return context,audit,inference_run


@dataclass(frozen=True)
class LeftIdentity:
    project: str
    revision: str
    stamp: tuple
    source_key: str
    owner: object = field(repr=False)
    prior: _MachinePrior | None = field(default=None,repr=False,kw_only=True)

    def check(self):
        from . import live_owner
        from .preview_cancel import check_cancelled
        check_cancelled()
        if live_owner.retained(self.project) is not self.owner:
            raise ValueError("音符编辑会话已切换，请刷新预览。")
        with self.owner.lock:
            self.owner.check(self.revision, self.stamp)
            if self.owner.head['source_key'] != self.source_key:
                raise ValueError("音符来源已切换，请刷新预览。")
        if self.prior is not None:self.prior.check()

@dataclass(frozen=True)
class LeftSource(LeftIdentity):
    frozen: object = field(repr=False)


class CompareLeftPageCache:
    """Derived pages only. This cache cannot vouch for the current source."""
    def __init__(self, max_bytes=12*1024*1024, max_entries=24):
        self._pages = OrderedDict()
        self._size = 0
        self._max_bytes = max_bytes
        self._max_entries = max_entries

    def get(self, key):
        entry = self._pages.get(key)
        if entry is None:
            return None
        self._pages.move_to_end(key)
        return dict(entry[0])

    def put(self, key, page):
        size = len(page['dataUrl'])+1024
        if size > self._max_bytes or key in self._pages:
            return
        while self._pages and (self._size+size > self._max_bytes or len(self._pages) >= self._max_entries):
            self._size -= self._pages.popitem(last=False)[1][1]
        self._pages[key] = (dict(page),size)
        self._size += size


def _preview_folder():
    from .release_paths import data_root
    root=data_root().resolve()
    cache=root/'.cache'/'score-compare-left'
    if not cache.resolve().is_relative_to(root):
        raise ValueError("临时谱预览缓存路径无效。")
    cache.mkdir(parents=True,exist_ok=True)
    return cache


def _check(identity, still_current):
    identity.check()
    if still_current is not None and not still_current():
        raise ValueError("预览请求已更新。")


def replay_score_compare_left(result, *, page_index=0, raster_scale=1, page_cache=None,
                              still_current=None):
    """Engrave one page from exact frozen XML, never from MIDI or a formal score."""
    from .native_score_runtime import range_raster_scale, range_renderer_identity, render_range_page
    identity=result.get('_left_identity')
    if type(identity) is not LeftIdentity or result.get('preview_kind') not in _PREVIEW_KINDS:
        raise ValueError("临时谱面的来源无法确认。")
    _check(identity,still_current)
    raster_scale=range_raster_scale(raster_scale)
    if type(page_index) is not int or not 0 <= page_index < 2000:
        raise ValueError("预览页码无效。")
    xml=result.get('musicxml_bytes')
    digest=result.get('artifacts',{}).get('preview.musicxml')
    if type(xml) is not bytes or not 0 < len(xml) <= 1_500_000 or sha256(xml).hexdigest() != digest:
        raise ValueError("临时谱面已变化，未使用旧图片。")
    started=perf_counter()
    renderer=range_renderer_identity()
    key=(digest,renderer,page_index,raster_scale)
    page=page_cache.get(key) if page_cache is not None else None
    hit=page is not None
    if not hit:
        with TemporaryDirectory(prefix='raster-',dir=_preview_folder()) as td:
            path=Path(td)/'preview.musicxml'
            path.write_bytes(xml)
            _check(identity,still_current)
            page=render_range_page(path,digest,page_index,raster_scale=raster_scale)
    _check(identity,still_current)
    if renderer!=range_renderer_identity():
        raise ValueError("原生制谱程序已切换，请重新预览。")
    if not hit and page_cache is not None:
        page_cache.put(key,page)
    return dict(result,page=page,preview_timing={'page_cache_hit':hit,'page_seconds':perf_counter()-started})


def capture_score_compare_left(project, revision, *, expected_stamp=None):
    from . import live_owner
    project=str(Path(project).resolve(strict=True))
    if not isinstance(revision,str) or not revision:
        raise ValueError("缺少当前音符版本。")
    owner=live_owner.retained(project)
    if owner is None:
        raise ValueError("请先打开当前音符编辑会话。")
    frozen=owner.freeze(expected=expected_stamp,base=revision)
    head=frozen.head
    source=LeftSource(project,revision,tuple(head['stamp']),head['source_key'],owner,frozen)
    source.check()
    return source

def prepare_score_compare_left(source,start,end,*,page_index=0,render_page=True,still_current=None,
                               raster_scale=1,page_cache=None,progress=lambda _:None,fast_only=False):
    from .score_range import _clock_content,_context,_draft,_compact_preview_layout
    from .timing import TempoMap
    def check():
        _check(source,still_current)
    check()
    if any(type(v) not in (int,float) or not isfinite(v) for v in (start,end)) or not 0<=start<end:
        raise ValueError("请选择有效的左右定位点。")
    if type(page_index) is not int or not 0<=page_index<2000:
        raise ValueError("预览页码无效。")
    raw=source.frozen.body_bytes()
    if not 0<len(raw)<=64*1024*1024:
        raise ValueError("音符快照超出预览范围。")
    requested_end=float(end)
    frozen_hash=sha256(raw).hexdigest()
    doc=json.loads(raw)
    if doc['revision']!=source.revision or doc['source_key']!=source.source_key:
        raise ValueError("音符快照版本不一致。")
    tempo=deepcopy(doc['tempo'])
    clock_digest=sha256(json.dumps(_clock_content(tempo),sort_keys=True,ensure_ascii=True,
                                   separators=(',',':')).encode('utf-8')).hexdigest()
    clock=TempoMap(tempo)
    duration=min(float(doc['duration']),clock.seconds[-1])
    old_end=tempo.get('score_end')
    if old_end:
        duration=min(duration,float(old_end['seconds']))
    if not 0<duration or end>duration+1e-8:
        raise ValueError("选区超出了曲谱范围。")
    if duration<clock.seconds[-1]:
        if old_end:
            tempo['score_end']=dict(old_end,seconds=duration)
        else:
            # The map runs past the audio: end the preview on the map's own
            # next beat after the audio, so the last bar is whole beats.
            origin=float(tempo.get('grid_origin',0.0))
            beat=clock.to_beat(duration)
            snapped=origin+math.ceil(beat-origin-1e-9)
            if clock.to_seconds(snapped)>clock.seconds[-1]+1e-9:
                snapped=origin+math.floor(beat-origin+1e-9)
            tempo['score_end']={'origin':'editor_score_end','seconds':clock.to_seconds(snapped),
                                'source':'preview_audio_end'}
    doc['tempo']=tempo
    end=min(float(end),duration)
    chosen=_context(clock,float(start),end,0)
    selected=chosen
    pin=None
    inference_run=False
    from .preview_cancel import PreviewCancelled
    with TemporaryDirectory(prefix='left-',dir=_preview_folder()) as td:
        path=Path(td)/'preview.musicxml'
        try:
            prepared=_duet_left(source,doc,clock,float(start),end,chosen,path)
        except PreviewCancelled:
            raise
        except (OSError,ValueError,RuntimeError,KeyError,TypeError,ET.ParseError):
            prepared=None # absent, stale or incompatible machine prior
        check() # never convert cancellation/owner replacement into fallback
        if prepared is None:
            if fast_only:
                raise ValueError('This preview requires full generation')
            kind='duet_current'
            (starts,lengths,meters,a,b),audit,inference_run=_duet_current(
                source,doc,clock,float(start),end,path,frozen_hash,check,progress)
        else:
            kind,(starts,lengths,meters,a,b),audit,pin=prepared
        _compact_preview_layout(path)
        with path.open('rb') as f: xml=f.read(1_500_001)
        if not 0<len(xml)<=1_500_000:
            raise ValueError("临时谱太大，请缩小选区。")
        digest=sha256(xml).hexdigest()
        check()
        if pin is not None:pin.check()
    cache_key=sha256(json.dumps(['compare-left-v3',source.project,source.revision,source.stamp,source.source_key,
                                frozen_hash,float(start),requested_end,a,b,kind,pin.identity if pin else None],
                                 ensure_ascii=False).encode()).hexdigest()
    identity=LeftIdentity(source.project,source.revision,source.stamp,source.source_key,source.owner,prior=pin)
    result=dict(preview_kind=kind,approximate=False,can_adopt=False,
                musicxml_bytes=xml, artifacts={'preview.musicxml':digest},
                cache_key=cache_key, source_stamp=source.stamp, source_key=source.source_key,
                source_sha256=frozen_hash, validated_revision=source.revision, revision=source.revision,
                requested_range=dict(start=float(start),end=requested_end),
                selected_bars=dict(first=selected[3]+1,last=selected[4]),
                context_range=dict(start=clock.to_seconds(starts[0]),end=clock.to_seconds(starts[-1]+lengths[-1]),
                                   first_measure=a+1,last_measure=b,start_beat=starts[0],end_beat=starts[-1]+lengths[-1]),
                compare_target=dict(start_seconds=clock.to_seconds(starts[0]),
                                    end_seconds=clock.to_seconds(starts[-1]+lengths[-1]),
                                    requested_start_seconds=float(start),
                                    requested_end_seconds=float(end),
                                    start_beat=float(starts[0]),
                                    end_beat=float(starts[-1]+lengths[-1]),
                                    first_measure=a+1,last_measure=b,
                                    clock_digest=clock_digest),
                page=None,inference_run=inference_run,pdf=None,source_export=None,_left_identity=identity,**audit)
    if not render_page:
        return result
    return replay_score_compare_left(result,page_index=page_index,raster_scale=raster_scale,page_cache=page_cache,
                                     still_current=still_current)
