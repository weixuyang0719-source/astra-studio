"""Explicit side-by-side recovery of a completed-inference score snapshot."""
import hashlib
from pathlib import Path
import shutil
import wave

from . import task_products as tasks


def cached_score_config(request, config, root, *, prepared=False):
    """Reproduce the saved inference keys, without probing or using a GPU."""
    from .device_policy import apply_device_policy
    from .run_config import prepare_run_config
    device=request.get('execution_device')
    if device not in ('gpu','cpu'):
        raise ValueError('已完成的推理缓存缺少明确的计算设备记录。')
    if not prepared:
        config=prepare_run_config(request,config,root)
    return apply_device_policy(config,device,probe={'available':device=='gpu','busy':False})[0]


def _recorded_score(root, record, project):
    if record['state']!='failed' or record['phase']!='score' or (project/'project.json').exists():
        raise ValueError('这项任务没有可续做的未提交乐谱。')
    request=record['request']
    options=request.get('options',{})
    if (request.get('score_engine')!='duet' or request.get('rhythm_provider')!='cubase_local'
            or request.get('keep_stems') is not True or request.get('score_chords')
            or options.get('mode') not in ('all','score')
            or options.get('bpm') is not None or options.get('tempo_path')):
        raise ValueError('这项乐谱的选项不支持仅从已验证缓存恢复。')
    checkpoint=tasks._valid_checkpoint(root,record)
    if (not checkpoint or not tasks._checkpoint_complete(record,checkpoint)
            or 'piano' not in checkpoint['stems']):
        raise ValueError('无法验证原任务的完整分轨。')
    persisted=tasks._read(tasks._job_dir(root,record['id'])/'request.json')
    if (persisted.get('workflow_project')!=str(project)
            or persisted.get('audio')!=checkpoint['source']
            or not tasks._same_work(request,persisted)):
        raise ValueError('原任务请求与完成记录不一致。')
    return request,checkpoint


def history(root):
    result=[]
    for record in tasks._records(root):
        if record.get('phase')!='score' or record.get('state')!='failed':
            continue
        try:
            project=tasks._owned_project(record)
            with tasks._recovery_guard(root,record,project):
                _,checkpoint=_recorded_score(root,record,project)
                if not any((project/'cache'/'duet').glob('*/astra-cache.json')):
                    continue
                result.append(tasks._recovery_card(record,checkpoint['stems']))
        except (EOFError,OSError,ValueError,RuntimeError,TypeError,KeyError,wave.Error):
            continue
    return result


def _file_under(project,path):
    path=Path(path)
    if not path.is_relative_to(project) or path.resolve(strict=True)!=path:
        raise ValueError('缓存路径超出原任务目录。')
    tasks._file_signature(path)
    return path


def _verified_plan(project, request, checkpoint, config):
    from . import duet_score, pipeline
    from .score_duet_export import _performance_inputs
    from .timing import TempoMap
    from .rhythm_decode import tempo_from_analysis

    source=Path(checkpoint['source'])
    keys=pipeline._analysis_keys(config)
    if (keys['separator']!=checkpoint['separator_key']
            or pipeline.fingerprint(source)!=checkpoint['source_fingerprint']):
        raise ValueError('源音频或模型配置已变化。缓存仍留在原候选。')
    if config.get('score',{}).get('harmony_engine') in ('chordformer','tsumugi'):
        raise ValueError('原任务需要额外的和弦模型结果，无法仅复用当前缓存。')
    normal=_file_under(project,project/'cache'/'source.wav')
    beat_path=_file_under(project,project/'cache'/f'beats-{keys["beat_this"]}.json')
    beat=tasks._read(beat_path)
    if (beat.get('provider')!='cubase_local_reconstruction'
            or duet_score.file_hash(normal)!=beat.get('source_sha256')
            or not tasks._complete_wave(normal,checkpoint['duration'])):
        raise ValueError('无法验证原任务的节拍和 PCM 缓存。')
    options=request.get('options',{})
    TempoMap(tempo_from_analysis(beat,checkpoint['duration'],options.get('beat_scale',1.),
                                provider_options=config['models']['cubase_local']))
    notes_path=_file_under(project,pipeline._notes_path(project,'piano',keys['piano']))
    notes=tasks._read(notes_path)
    pipeline.validate_notes(notes['notes'])
    pedals=list(pipeline._pedals_in_duration(notes['pedals'],checkpoint['duration']))
    performance=_performance_inputs(project,notes['notes'],pedals,notes.get('controls'))
    midi=_file_under(project,performance/'piano_raw.mid')
    source_tempo=_file_under(project,performance/'source-tempo.json')
    prediction=duet_score.cached_prediction(midi,project/'cache'/'duet',title=source.stem.split(' - ',1)[-1])
    if not prediction:
        raise ValueError('缺少完整且匹配的 DuET 预测。缓存仍留在原候选。')
    duet_dir=Path(prediction['report']).parent
    manifest=_file_under(project,duet_dir/'astra-cache.json')
    files=[normal,notes_path,midi,source_tempo,manifest]
    files+= [_file_under(project,duet_dir/name) for name in duet_score.ARTIFACTS]
    fields={}
    for key in ('source','source_path','observations_path'):
        if beat.get(key):
            path=_file_under(project,beat[key])
            if key in ('source','source_path') and path!=normal:
                raise ValueError('旧节拍缓存关联了另一份 PCM。')
            if key=='observations_path' and duet_score.file_hash(path)!=beat.get('observations_sha256'):
                raise ValueError('旧节拍观测文件的校验不匹配。')
            files.append(path)
            fields[key]=path.relative_to(project)
    if 'observations_path' not in fields:
        raise ValueError('缺少经过校验的节拍观测文件。')
    stems={}
    for instrument,name in checkpoint['stems'].items():
        path=_file_under(project,name)
        if path!=project/'stems'/keys['separator']/f'{instrument}.wav':
            raise ValueError('分轨不属于保存的模型版本。')
        files.append(path)
        stems[instrument]=path.relative_to(project)
    return (set(files),beat_path,beat,fields,stems)


def copy_score(root,source_root,token):
    """Copy verified analysis artifacts only after the user opens this task."""
    from . import duet_score, pipeline
    root=Path(root).resolve(strict=True)
    source_root=Path(source_root).resolve(strict=True)
    if root==source_root:
        raise ValueError('请从独立的候选恢复上一版乐谱。')
    old=tasks._record(source_root,token)
    old_project=tasks._owned_project(old)
    folder=tasks._job_dir(root,token)
    provenance={'profile':str(source_root),'id':token,'kind':'completed_score_inference'}
    if folder.exists():
        existing=tasks._record(root,token)
        if existing.get('recovered_from')==provenance and existing['state']=='kept':
            tasks.open_kept(root,token)
            return token
        raise ValueError('此任务已存在于当前候选。')
    with tasks._recovery_guard(source_root,old,old_project):
        request,checkpoint=_recorded_score(source_root,old,old_project)
        config=cached_score_config(request,pipeline.load_config(),root)
        source=Path(checkpoint['source'])
        original_identity=tasks._file_signature(source)
        files,beat_path,beat,path_fields,stems=_verified_plan(old_project,request,checkpoint,config)
        before={path:duet_score.file_hash(path) for path in files|{beat_path}}
        resumed={**request,'workflow_phase':'separation','output':str(root/'outputs'),'workflow_cached_score':True}
        resumed.pop('workflow_origin',None)
        folder.mkdir(parents=True,exist_ok=False)
        started=tasks.begin_task(root,folder,resumed)
        project=Path(started['workflow_project'])
        try:
            for path in files:
                new=project/path.relative_to(old_project)
                new.parent.mkdir(parents=True,exist_ok=True)
                shutil.copyfile(path,new)
                if duet_score.file_hash(new)!=before[path]:
                    raise ValueError('复制的缓存与已验证的内容不同。')
            if (any(duet_score.file_hash(path)!=digest for path,digest in before.items())
                    or tasks._file_signature(source)!=original_identity):
                raise ValueError('旧任务的缓存或源音频在恢复时已变化。')
            for key,path in path_fields.items():
                beat[key]=str(project/path)
            pipeline.save_json(project/beat_path.relative_to(old_project),beat)
            tasks.record_checkpoint(folder,{
                'project':str(project),'source':str(source),'duration':checkpoint['duration'],
                'stems':{key:str(project/path) for key,path in stems.items()},
                'source_fingerprint':checkpoint['source_fingerprint'],'separator_key':checkpoint['separator_key'],
                'separator_provenance':checkpoint.get('separator_provenance',{}),
            })
            record=tasks._record(root,token)
            record.update(state='kept',released=True,recovered_from=provenance)
            record.pop('before',None)
            tasks._write(folder/tasks._LEDGER,record)
        except (EOFError,OSError,ValueError,RuntimeError,TypeError,KeyError,wave.Error):
            tasks.finish_task(root,folder,'failed')
            raise
    return token
