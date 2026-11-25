"""Unpublished Notes exports: immutable evidence and a guarded pointer transaction."""
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
import json
import re

_PREFIX = 'notes-draft-'
_TOKEN = re.compile(r'[0-9a-f]{32}\Z')
_FIELDS = ('latest_export','latest_score_export','latest_pdf_export','latest_harmony_export',
           'editor_exported_revision','editor_score_revision')
_REQUIRED = ('report.json','editor_snapshot.json','tempo_map.json','piano.musicxml')
_STANDARD = ('piano.pdf','piano_harmony.json')
_LIMIT = 64 * 1024 * 1024
_SCORE_OPTIONS = ('piano_engine','show_pedal','show_chords','voice_mode','harmony_engine','confirmed_key')
_OPTIONS_MARK = 'notation_options'

def valid_token(value):
    return isinstance(value, str) and _TOKEN.fullmatch(value) is not None

def draft_directory_name(value):
    return value.startswith(_PREFIX) and valid_token(value[len(_PREFIX):])

def draft_directory(project, token):
    if not valid_token(token):
        raise ValueError('无效的临时制谱任务。')
    project = Path(project).resolve(strict=True)
    root = project/'exports'
    if root.is_symlink() or root.resolve() != root:
        raise ValueError('临时谱面保存目录已变化。')
    return root/(_PREFIX+token)

def notation_options(state):
    config=state.get('config') or {}
    if not isinstance(config,dict):
        raise ValueError('制谱选项无效。')
    score=config.get('score') or {}
    if not isinstance(score,dict):
        raise ValueError('制谱选项无效。')
    # Retain the same product options as an ordinary Update Score. Runtime
    # executable/device paths and any unrelated model settings stay local.
    retained={}
    for k in _SCORE_OPTIONS:
        if k not in score:
            continue
        v=score[k]
        if k=='confirmed_key' and isinstance(v,dict):
            v={a:v[a] for a in ('tonic','mode') if a in v}
            if any(not isinstance(a,str) or len(a)>100 for a in v.values()):
                raise ValueError('制谱调性选项无效。')
        elif v is not None and type(v) not in (str,bool):
            raise ValueError('制谱选项无效。')
        if isinstance(v,str) and len(v)>100:
            raise ValueError('制谱选项无效。')
        retained[k]=v
    result={'score':retained}
    if 'quantization' in config:
        result['quantization']=config['quantization']
    return result

def pointer_marks(state):
    return {**{k:state.get(k) for k in _FIELDS},_OPTIONS_MARK:notation_options(state)}

def _regular(folder, name):
    if not isinstance(name,str) or not name or Path(name).name!=name or name in ('.','..') or ':' in name:
        raise ValueError('临时制谱产物名称无效。')
    p=folder/name
    if p.is_symlink() or p.resolve().parent!=folder or not p.is_file():
        raise ValueError('临时制谱产物不是工程内的普通文件。')
    s=p.stat()
    if s.st_nlink!=1 or not 0<s.st_size<=_LIMIT:
        raise ValueError('临时制谱产物超限或路径无效。')
    return p, (s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns)

def _object(folder,name):
    path,_ = _regular(folder,name)
    with path.open('rb') as stream:
        data=stream.read(_LIMIT+1)
    if not 0<len(data)<=_LIMIT:
        raise ValueError('临时制谱元数据超限。')
    result=json.loads(data)
    if not isinstance(result,dict):
        raise ValueError('临时制谱元数据无效。')
    return result

def _names(folder,report):
    files=report.get('files')
    if not isinstance(files,list) or len(files)>96 or not all(isinstance(k,str) for k in files):
        raise ValueError('临时制谱产物清单无效。')
    return tuple(sorted(set(_REQUIRED).union(files,(n for n in _STANDARD if
                            (folder/n).exists() or (folder/n).is_symlink()))))

def _files(folder,names,digests=None):
    artifacts = []
    stamps = []
    total=0
    for name in names:
        p,stamp=_regular(folder,name)
        total+=stamp[2]
        if total>256*1024*1024:
            raise ValueError('临时制谱产物总大小超限。')
        h=sha256()
        with p.open('rb') as stream:
            for block in iter(lambda:stream.read(1024*1024),b''):
                h.update(block)
        if _regular(folder,name)[1]!=stamp:
            raise ValueError('临时制谱产物已变化。')
        value=h.hexdigest()
        if digests is not None and name in digests and digests[name]!=value:
            raise ValueError('临时制谱产物不再匹配。')
        artifacts.append((name,value))
        stamps.append((name,stamp))
    return tuple(artifacts),tuple(stamps)

def worker_receipt(project,export,revision,capture,draft,state):
    # The worker still holds project_lock. The editing lock and old pointer
    # test confirm the same project/checkpoint ancestry, without writing.
    from . import editor_checkpoint as cp
    from .editing import edit_lock
    from .score_commit import effective
    if not revision or not capture or export != draft_directory(project,draft['token']):
        raise ValueError('未核实临时谱面来源；正式谱未改变。')
    guard,sequence=capture['guard'],capture['applied_sequence']
    with edit_lock(project):
        record=cp.read(project)
        # A real same-note save may add only a VST attachment after capture.
        # Saving a different revision, untrusted checkpoint or source can
        # never authorize promotion of these bytes.
        ancestry=record is not None and (record.identity==guard['anchor'] or (
            record.index.get('checkpoint_owner')==guard['lineage'] and (
                record.index['applied_sequence']<=sequence or (
                    record.index['revision']==revision and json.loads(record.body).get('source_key')==draft['source']))))
        if not ancestry or pointer_marks(effective(project,_object(project,'project.json'),record))!=draft['before']:
            raise ValueError('工程已改变；临时谱未发布。')
        _regular(export,'piano.musicxml')
    return dict(kind='notes-score-draft',project=str(project),export=str(export),revision=revision,
                source=draft['source'],lineage=guard['lineage'],sequence=sequence,
                token=draft['token'],before=draft['before'],options=notation_options(state))

@dataclass(frozen=True)
class Receipt:
    project: Path
    export: Path
    source: str
    revision: str
    lineage: str
    sequence: int
    token: str
    before: tuple
    artifacts: tuple
    options: dict

def capture(row):
    project=Path(row['project']).resolve(strict=True)
    export=draft_directory(project,row['token'])
    if row['export']!=str(export) or export.is_symlink() or export.resolve(strict=True)!=export:
        raise ValueError('临时谱不属于当前工程。')
    report=_object(export,'report.json')
    snapshot=_object(export,'editor_snapshot.json')
    from .pipeline import read_json
    before=row['before']
    options=row['options']
    if (not isinstance(options,dict) or notation_options({'config':options})!=options
            or not isinstance(before,dict) or set(before)!=set(_FIELDS)|{_OPTIONS_MARK}
            or pointer_marks(read_json(project/'project.json'))!=before
            or report.get('status') not in ('complete','complete_with_warnings')
            or report.get('editor_revision')!=row['revision']
            or snapshot.get('revision')!=row['revision'] or snapshot.get('source_key')!=row['source']):
        raise ValueError('临时谱和当前工程版本不一致。')
    artifacts,stamps=_files(export,_names(export,report))
    # Metadata read before hashing must be the exact metadata we are binding.
    if report!=_object(export,'report.json') or snapshot!=_object(export,'editor_snapshot.json'):
        raise ValueError('临时制谱元数据已变化。')
    if any(_regular(export,k)[1]!=stamp for k,stamp in stamps):
        raise ValueError('临时制谱产物已变化。')
    return Receipt(project,export,row['source'],row['revision'],row['lineage'],row['sequence'],
                   row['token'],tuple(before.items()),artifacts,options)

def prepare_publication(receipt):
    from .score_commit import NATIVE_FILE
    from .native_score_runtime import prepare_native_score
    from .audio_output.inline_native_score import InlineNativeScoreBridge
    if draft_directory(receipt.project,receipt.token)!=receipt.export or receipt.export.resolve(strict=True)!=receipt.export:
        raise ValueError('制谱目录已变化。')
    artifacts=dict(receipt.artifacts)
    if _names(receipt.export,_object(receipt.export,'report.json'))!=tuple(artifacts):
        raise ValueError('制谱产物清单已变化。')
    _, original = _files(receipt.export,tuple(artifacts),artifacts)
    native = receipt.export/NATIVE_FILE
    # Generate using exactly the same MusicXML projection as the right pane.
    # If either native import, rendering, or save fails, no formal file changes.
    if NATIVE_FILE in artifacts:
        raise ValueError('原生谱面名称不能来自导出报告。')
    generated = InlineNativeScoreBridge._generated_score_bytes(receipt.export/'piano.musicxml', hashes=artifacts)
    prepare_native_score(generated, native)
    _, proof = _files(receipt.export,tuple(artifacts)+(NATIVE_FILE,),artifacts)
    if proof[:-1] != original:
        raise ValueError('生成谱面时乐谱或速度图发生变化。')
    return proof

def render(receipt,index,raster_scale,*,current):
    from tempfile import TemporaryDirectory
    from .native_score_runtime import render_range_page
    from .audio_output.inline_native_score import InlineNativeScoreBridge
    from .release_paths import data_root
    artifacts=dict(receipt.artifacts)
    def exact():
        if not current() or receipt.export.resolve(strict=True)!=receipt.export:
            raise ValueError('临时谱面已过期。')
        for name in ('piano.musicxml','tempo_map.json'):
            p,_=_regular(receipt.export,name)
            if sha256(p.read_bytes()).hexdigest()!=artifacts[name]:
                raise ValueError('临时谱面内容已变化。')
    exact()
    raw=InlineNativeScoreBridge._generated_score_bytes(receipt.export/'piano.musicxml')
    exact()
    with TemporaryDirectory(prefix='notes-score-preview-',dir=data_root()) as private:
        source=Path(private)/'score.musicxml'
        source.write_bytes(raw)
        page=render_range_page(source,sha256(raw).hexdigest(),index,raster_scale=raster_scale)
    exact()
    return page
