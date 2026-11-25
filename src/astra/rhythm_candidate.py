"""Explicit whole-source pulse candidates; analysis never publishes a clock."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from uuid import uuid4

from . import editing, pipeline
from .timing import TempoMap

LABELS={'A':'原拍值分组','B0':'半拍值分组 · 起点 1','B1':'半拍值分组 · 起点 2',
        'C':'半拍值重新分组','D':'双拍值 · 拆分小节'}


def digest(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):h.update(block)
    return h.hexdigest()


def _input_binding(state):
    """Small metadata only; audio hashing remains in the worker/adoption gate."""
    source=Path(state['source']).resolve()
    if source.exists():
        if not source.is_file():raise ValueError('原曲路径不是文件；未使用其它缓存替代。')
        return {'input_path':str(source),'input_sha256':state.get('source_sha256'),
                'original_reverified':True,'import_manifest':None,'import_manifest_sha256':None}
    imported=state.get('source_import') or {}
    try:
        source_hash=state['source_sha256']
        if source.suffix.lower()!='.ncm' or not isinstance(source_hash,str) or len(source_hash)!=64:
            raise ValueError('缺少原 NCM 身份')
        audio=Path(state['source_audio']).resolve(strict=True)
        if (not audio.is_file() or audio.name not in ('audio.flac','audio.mp3') or
            audio.parent.name!=source_hash or audio.parent.parent.name!='ncm' or
            Path(imported['path']).resolve()!=audio):raise ValueError('缓存路径绑定不符')
        manifest=audio.parent/'import.json'
        record=pipeline.read_json(manifest)
        from .audio_import import VERSION
        for row in (record,imported):
            if (row['version']!=VERSION or row.get('converted') is not True or
                Path(row['original_path']).resolve()!=source or row['source_sha256']!=source_hash or
                row['audio_file']!=audio.name or row['audio_sha256']!=record['audio_sha256']):
                raise ValueError('导入清单身份不符')
        audio_hash=record['audio_sha256']
        if not isinstance(audio_hash,str) or len(audio_hash)!=64:raise ValueError('缺少缓存音频身份')
        return {'input_path':str(audio),'input_sha256':audio_hash,'original_reverified':False,
                'import_manifest':str(manifest),'import_manifest_sha256':digest(manifest)}
    except (KeyError,TypeError,OSError,ValueError) as exc:
        raise ValueError(f'原曲文件不可用，且没有与本工程严格绑定的 NCM 导入缓存：{exc}') from exc


def _verify_input(prepared):
    state=pipeline.read_json(Path(prepared['project'])/'project.json')
    if (str(Path(state['source']).resolve())!=prepared['source'] or
        state.get('source_sha256') not in (None,prepared['source_sha256'])):
        raise ValueError('原曲身份已变化，请重新分析候选。')
    current=_input_binding(state)
    for key in ('input_path','original_reverified','import_manifest','import_manifest_sha256'):
        if current[key]!=prepared[key]:raise ValueError('原曲或导入缓存绑定已变化，请重新分析候选。')
    actual=digest(prepared['input_path'])
    expected=current['input_sha256']
    if (expected and actual!=expected) or (prepared.get('input_sha256') and actual!=prepared['input_sha256']):
        raise ValueError('原曲或导入缓存内容与工程身份不符；当前图保持。')
    return actual


def prepare(project,base_revision,request):
    from .document_editor import binding
    project=Path(project).resolve();doc,ticket=binding(project,base_revision)
    state=pipeline.read_json(project/'project.json')
    if state.get('source_kind')=='midi':raise ValueError('MIDI 导入没有原曲音频可重新分析拍点。')
    source=Path(state['source']).resolve()
    binding=_input_binding(state)
    input_file=Path(binding['input_path'])
    if request.get('provider','cubase_local')!='cubase_local':
        raise ValueError('此候选入口当前仅提供本机独立拍点分析，不会静默切换模型。')
    device=request.get('execution_device','prefer_gpu')
    if device not in ('prefer_gpu','gpu','cpu'):raise ValueError('请选择有效计算设备。')
    if type(request.get('include_redecode',False)) is not bool:raise ValueError('重新分组开关必须明确开启或关闭。')
    return {'project':str(project),'base_revision':base_revision,'editor_ticket':ticket,'source':str(source),
        'source_sha256':state.get('source_sha256'),'source_stat':[input_file.stat().st_size,input_file.stat().st_mtime_ns],
        **binding,
        'doc':deepcopy(doc),'provider':'cubase_local','execution_device':device,
        'include_redecode':request.get('include_redecode',False)}


def replacement(doc,tempo,identity):
    """Keep every event and archive retired editor-pin metadata."""
    result=deepcopy(doc);candidate=deepcopy(tempo)
    candidate['candidate_only']=False
    candidate['adopted_candidate']=deepcopy(identity)
    # End-of-score is an explicit seconds decision, not a detector suggestion.
    if doc['tempo'].get('score_end'):candidate['score_end']=deepcopy(doc['tempo']['score_end'])
    result['tempo']=candidate
    from .legacy_fixed_points import retire_fixed_points
    result=retire_fixed_points(result)
    result['manual']=True
    result['summary']='已采用拍点候选；音符和CC64实际时间保持不变，可一次撤销。'
    editing._validate_document(result)  # legacy differential oracle only
    return result


def build_result(prepared,analysis,normalized_hash,regrouped=None,redecode_error=None,*,evidence_refs=None):
    from .cubase_metrical_candidates import build_metrical_candidates
    doc=prepared['doc']
    bundle=build_metrical_candidates(analysis,audio_duration=analysis['audio_duration'],
        audio_sha256=normalized_hash,regrouped_decoder_result=regrouped)
    from .document_editor import detached,preview_rhythm_inspection
    owner=detached(doc)
    candidates=[]
    for item in bundle['candidates']:
        row=deepcopy(item);row['label']=LABELS.get(row['id'],row['id'])
        row['adoptable']=False
        if row.get('valid'):
            evidence=(evidence_refs or {}).get('C' if row['id']=='C' else 'base')
            if evidence is not None:
                row['tempo']['pulse_evidence']=deepcopy(evidence)
            identity={'id':row['id'],'source_sha256':prepared['source_sha256'],
                'normalized_sha256':normalized_hash,'provider':analysis['provider'],
                'decoder':analysis['decoder'],'base_revision':prepared['base_revision'],
                'original_reverified':prepared['original_reverified'],'input_sha256':prepared['input_sha256']}
            if evidence is not None:identity['pulse_evidence']=deepcopy(evidence)
            clock=TempoMap(row['tempo']);bpms=[60*(b-a)/(t-s) for a,b,s,t in
                zip(clock.beats,clock.beats[1:],clock.seconds,clock.seconds[1:])]
            row['summary']={'bpm_min':min(bpms),'bpm_max':max(bpms),
                'meters':list(dict.fromkeys(f"{m['numerator']}/{m['denominator']}" for m in row['tempo']['time_signatures'])),
                'first_bar_seconds':row['report'].get('selected_notation_bar_origin_seconds',
                    row['tempo'].get('opening_phase',{}).get('seconds')),
                'first_bar_confirmed':False,'preserves_pulse_seconds':row['report']['preserves_base_pulse_seconds'],
                'provider':analysis['provider'],'decoder':analysis['decoder']}
            row['summary']['decode_source']='same_observations_separate_6_8_pulse_decode' if row['id']=='C' else 'same_3_4_pulse_decode'
            row['summary']['derived_bar_boundaries']=row['id']=='D'
            row['identity']=identity
            try:preview_rhythm_inspection(owner,doc,row['tempo'],identity);row['adoptable']=True
            except ValueError as exc:row['reason']=str(exc)
        else:row['reason']='此半拍值解释在变拍边界无法配成完整分组；不会强行拼接。'
        candidates.append(row)
    owner.close()
    return {'candidates':candidates,'summary':'新拍点候选尚未采用；当前生效图及演奏保留。',
        'warnings':['候选首个完整小节位置仍待确认；起点 1/2 不是两个模型。']+
            ([] if prepared['original_reverified'] else ['原文件不可用，使用已校验的 NCM 导入缓存；原文件本次未重新核验。'])+
            ([f'额外重新分组未产生候选：{redecode_error}'] if redecode_error else []),
        'provenance':{'provider':analysis['provider'],'decoder':analysis['decoder'],
            'source_sha256':prepared['source_sha256'],'normalized_sha256':normalized_hash,
            'original_reverified':prepared['original_reverified'],'input_sha256':prepared['input_sha256'],
            'import_manifest_sha256':prepared['import_manifest_sha256'],
            'observation_sha256':analysis.get('observations_sha256'),
            'network_runs':'one observation source shared by interpretations',
            'redecode_available':regrouped is not None,'redecode_requested':prepared.get('include_redecode',False)}}


def run(prepared):
    from . import adapters
    from .device_policy import apply_device_policy,probe_gpu_status
    from .release_paths import runtime_env_root
    source=Path(prepared['input_path']);actual=_verify_input(prepared)
    prepared['input_sha256']=actual
    if prepared['original_reverified']:prepared['source_sha256']=actual
    status=probe_gpu_status()
    if prepared['execution_device']=='prefer_gpu' and status['available'] and status['busy']:
        return {'needs_device_choice':True,'gpu_status':status,
                'summary':status['reason'],'warnings':[]}
    config=pipeline.load_config();config['rhythm_provider']='cubase_local'
    model=config.setdefault('models',{}).setdefault('cubase_local',{})
    model.update(python=str(runtime_env_root(pipeline.ROOT)/'.venv'/'Scripts'/'python.exe'),min_bpm=60.,max_bpm=240.,
                 quarters_per_pulse=1.,meter_stay=.95,meters=[3,4])
    config,decision=apply_device_policy(config,prepared['execution_device'],probe=status,root=pipeline.ROOT)
    folder=Path(prepared['project'])/'cache'/'rhythm-candidates'/prepared['source_sha256']
    folder.mkdir(parents=True,exist_ok=True)
    normalized=folder/'input.wav';manifest=folder/'input.json'
    valid=False
    if normalized.is_file() and manifest.is_file():
        try:
            saved=pipeline.read_json(manifest)
            valid=saved['source_sha256']==actual and saved['normalized_sha256']==digest(normalized)
        except (OSError,ValueError,KeyError):pass
    if not valid:
        pipeline.decode_audio(source,normalized,lambda _:None)
        pipeline.save_json(manifest,{'source_sha256':actual,'normalized_sha256':digest(normalized)})
    normalized_hash=digest(normalized)
    analysis=adapters.detect_beats(normalized,folder/'inference',config,lambda _:None)
    if _verify_input(prepared)!=actual or digest(normalized)!=normalized_hash:
        raise ValueError('分析期间源音频改变；候选已丢弃。')
    regrouped=None;redecode_error=None
    if prepared.get('include_redecode'):
        from .cubase_local_worker import redecode_cached_result
        try:regrouped=redecode_cached_result(analysis,folder/'regrouped',{'meters':[6,8]},log=lambda _:None)
        except (OSError,ValueError,RuntimeError,KeyError) as exc:redecode_error=str(exc)
    from .pulse_evidence_store import store_result
    refs={'base':store_result(prepared['project'],prepared['source_sha256'],analysis)}
    if regrouped is not None:
        refs['C']=store_result(prepared['project'],prepared['source_sha256'],regrouped)
    result=build_result(prepared,analysis,normalized_hash,regrouped,redecode_error,evidence_refs=refs)
    result['provenance']['device']=decision['device']
    path=folder/f'candidate-{uuid4().hex}.json'
    pipeline.save_json(path,result)
    result.update(candidate_file=str(path),candidate_sha256=digest(path))
    return result


def adopt(prepared,result,candidate_id):
    project=Path(prepared['project']);base=prepared['base_revision']
    path=Path(result['candidate_file']).resolve()
    if not path.is_relative_to(project/'cache'/'rhythm-candidates'):
        raise ValueError('候选文件不属于当前工程。')
    with pipeline.project_lock(project),editing.edit_lock(project):
        head=project/'editing'/'current.json'
        index=editing._base_index(head,base)
        actual_input=_verify_input(prepared)
        payload=path.read_bytes()
        actual_artifact=hashlib.sha256(payload).hexdigest()
        if actual_artifact!=result['candidate_sha256']:raise ValueError('候选文件已变化，请重新分析。')
        saved=json.loads(payload)
        row=next((r for r in saved['candidates'] if r['id']==candidate_id),None)
        if not row or not row.get('valid') or not row.get('adoptable'):
            raise ValueError('请选择有效且不与固定锚点冲突的候选。')
        if row['tempo'].get('pulse_evidence') is not None:
            from .active_pulse_evidence import resolve
            resolve(project,tempo=row['tempo'])
        from .document_editor import commit_locked
        provenance=saved['provenance']
        binding=dict(base_revision=base,source_sha256=prepared['source_sha256'],input_sha256=actual_input,
                     original_reverified=prepared['original_reverified'],
                     **{k:provenance[k] for k in ('normalized_sha256','provider','decoder')})
        cap=dict(row=row,binding=binding,observed_input_sha256=actual_input,
                 registered_candidate_sha256=result['candidate_sha256'],observed_candidate_sha256=actual_artifact,
                 resolved_pulse_evidence=row['tempo'].get('pulse_evidence'))
        return commit_locked(project,head,index,dict(type='adopt_rhythm_candidate',candidate_id=candidate_id),
                             trusted=dict(rhythm_candidate=cap),expected=prepared['editor_ticket'])['doc']
