"""Isolated desktop job process and its local JSONL protocol."""

from contextlib import contextmanager
from pathlib import Path
import argparse
import ctypes
from ctypes import wintypes
import json
import os
import sys
import traceback


MANAGED_JOB = 'ASTRA_DESKTOP_JOB'


def _kernel32():
    api=ctypes.WinDLL('kernel32',use_last_error=True)
    signatures={
        'CreateJobObjectW':([ctypes.c_void_p,wintypes.LPCWSTR],wintypes.HANDLE),
        'OpenJobObjectW':([wintypes.DWORD,wintypes.BOOL,wintypes.LPCWSTR],wintypes.HANDLE),
        'SetInformationJobObject':([wintypes.HANDLE,ctypes.c_int,ctypes.c_void_p,wintypes.DWORD],wintypes.BOOL),
        'AssignProcessToJobObject':([wintypes.HANDLE,wintypes.HANDLE],wintypes.BOOL),
        'GetCurrentProcess':([],wintypes.HANDLE),
        'TerminateJobObject':([wintypes.HANDLE,wintypes.UINT],wintypes.BOOL),
        'QueryInformationJobObject':([wintypes.HANDLE,ctypes.c_int,ctypes.c_void_p,wintypes.DWORD,
                                      ctypes.POINTER(wintypes.DWORD)],wintypes.BOOL),
        'CloseHandle':([wintypes.HANDLE],wintypes.BOOL),
    }
    for name,(parameters,result) in signatures.items():
        function=getattr(api,name)
        function.argtypes=parameters
        function.restype=result
    return api


class WindowsJob:
    """Own a named Windows process tree without ever killing by a stale PID."""

    def __init__(self,name):
        self.api=_kernel32()
        ctypes.set_last_error(0)
        self.handle=self.api.CreateJobObjectW(None,name)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        if ctypes.get_last_error()==183:  # ERROR_ALREADY_EXISTS
            self.close()
            raise RuntimeError('桌面任务进程组已存在')

        size=ctypes.c_size_t

        class BasicLimits(ctypes.Structure):
            _fields_=[('process_time',ctypes.c_longlong),('job_time',ctypes.c_longlong),
                ('flags',wintypes.DWORD),('min_working_set',size),('max_working_set',size),
                ('active_processes',wintypes.DWORD),('affinity',size),
                ('priority',wintypes.DWORD),('scheduling',wintypes.DWORD)]

        class ExtendedLimits(ctypes.Structure):
            _fields_=[('basic',BasicLimits),('io_counters',ctypes.c_ulonglong*6),
                ('process_memory',size),('job_memory',size),('peak_process_memory',size),('peak_job_memory',size)]

        limits=ExtendedLimits()
        limits.basic.flags=0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.api.SetInformationJobObject(self.handle,9,ctypes.byref(limits),ctypes.sizeof(limits)):
            error=ctypes.get_last_error()
            self.close()
            raise ctypes.WinError(error)

    def terminate(self):
        if self.handle and not self.api.TerminateJobObject(self.handle,1):
            raise ctypes.WinError(ctypes.get_last_error())

    def active_processes(self):
        class Accounting(ctypes.Structure):
            _fields_=[('user_time',ctypes.c_longlong),('kernel_time',ctypes.c_longlong),
                      ('period_user_time',ctypes.c_longlong),('period_kernel_time',ctypes.c_longlong),
                      ('page_faults',wintypes.DWORD),('total',wintypes.DWORD),
                      ('active',wintypes.DWORD),('terminated',wintypes.DWORD)]
        accounting=Accounting()
        if not self.handle or not self.api.QueryInformationJobObject(
                self.handle,1,ctypes.byref(accounting),ctypes.sizeof(accounting),None):
            raise ctypes.WinError(ctypes.get_last_error())
        return accounting.active

    def close(self):
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle=None


def join_managed_job(name):
    """Join the controller's job before loading the pipeline or launching anything."""
    if os.name!='nt':
        os.setsid()
        return
    api=_kernel32()
    handle=api.OpenJobObjectW(0x0001,False,name)  # JOB_OBJECT_ASSIGN_PROCESS
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if not api.AssignProcessToJobObject(handle,api.GetCurrentProcess()):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        api.CloseHandle(handle)


STAGES=(
    ('读取音频指纹','检查本地缓存'),
    ('解码音频','解码音频'),
    ('分析原曲节拍','检测节拍与强拍'),
    ('分离钢琴','分离音轨'),
    ('转录 piano','转录钢琴'),
    ('转录 guitar','转录吉他'),
    ('[chordformer]','分析和弦'),
    ('[tsumugi]','分析和弦'),
    ('正在导出结果文件','导出结果'),
)


def chord_inference_needed(request, state=None):
    if request['kind']!='reexport' or 'tempo_base_export' in request:
        return False
    options=request.get('options',{})
    if options.get('tempo_path') or options.get('bpm') is not None or options.get('beat_scale',1)!=1:
        return False
    from . import pipeline

    project=Path(request['project'])
    if state is None:
        state=pipeline.read_json(project/'project.json')
    score=state.get('config',{}).get('score',{})
    wants_analysis=request.get('score_chords',score.get('show_chords',False)) or score.get('harmony_engine') in ('chordformer','tsumugi')
    if (not wants_analysis or 'piano' not in state.get('instruments',[])
            or state.get('source_kind')=='midi' or Path(state.get('source') or '').suffix.lower() in ('.mid','.midi')):
        return False
    stem=pipeline.stem_path(project,state,'piano')
    if stem is None or not stem.is_file():
        return False
    if score.get('harmony_engine')=='tsumugi':
        # Reexport only reuses a committed Tsumugi prediction; explicit analysis
        # is the entry point for running it with updated beats/keys.
        return False
    from .chordformer import cache_is_available

    return not cache_is_available(stem,project/'cache'/'chordformer')


def run_request(request, emit, job_directory):
    from . import pipeline, project_access
    project_access.install_job(request,job_directory)

    kind=request.get('kind')
    if kind not in ('run','reexport','midi_import','chord_visibility','harmony_analysis'):
        raise ValueError('未知桌面任务类型')
    options={key:value for key,value in request.get('options',{}).items()
        if key in ('mode','tempo_path','bpm','meter','beat_scale')}
    options.setdefault('mode','all')
    if options['mode'] not in ('midi','score','all'):
        raise ValueError('未知输出模式')
    quantization=request.get('quantization')
    if quantization is not None and quantization not in ('auto','straight','triplet','fine'):
        raise ValueError('量化应为 auto/straight/triplet/fine')

    def log(message):
        message=str(message)
        for prefix,stage in STAGES:
            if message.startswith(prefix):
                emit('stage',message=stage)
                break
        emit('log',message=message)

    original_lock=pipeline.project_lock

    @contextmanager
    def tracked_lock(project):
        metadata={'path':str((project/'.running').resolve()),'pid':os.getpid()}
        # Persist intent before acquiring the lock so a force-kill cannot hide its owner.
        pipeline.save_json(job_directory/'lock.json',metadata)
        emit('lock',**metadata)
        with original_lock(project):
            yield

    pipeline.project_lock=tracked_lock
    try:
        if kind=='chord_visibility':
            from .chord_visibility import export_chord_visibility
            emit('stage',message='更新和弦标记与谱面')
            return export_chord_visibility(request['project'],request['base_export'],request['score_chords'],log=log)
        config=pipeline.load_config()
        from .device_policy import apply_device_policy
        if kind=='harmony_analysis':
            from .harmony_export import export_audio_harmony
            if request.get('force') is not True:
                raise ValueError('请明确请求重新分析音频和弦。')
            _,device=apply_device_policy(config,request.get('execution_device','prefer_gpu'))
            label='GPU' if device['device']=='cuda' else 'CPU'
            device['message']=f'和弦分析使用 {label}。'
            emit('device',**device)
            log(device['message'])
            emit('stage',message='分析和弦')
            return export_audio_harmony(request['project'],request['base_export'],
                device=device['device'],force=True,log=log,engine=request.get('harmony_engine','chordformer'))
        previous={}
        instruments=request.get('instruments',[])
        if kind=='reexport':
            project_state=pipeline.read_json(Path(request['project'])/'project.json')
            previous=project_state.get('config',{}).get('score',{})
            instruments=project_state.get('instruments',instruments)
        guitar_only=kind!='midi_import' and 'guitar' in instruments and 'piano' not in instruments
        separation_only=kind=='run' and request.get('workflow_phase')=='separation' and not guitar_only
        provider=(request.get('rhythm_provider',config.get('rhythm_provider','cubase_local'))
                  if kind=='run' else request.get('rhythm_provider'))
        if provider is not None and provider not in ('beat_this','cubase_local'):
            raise ValueError('节拍来源须为 beat_this 或 cubase_local')
        engine=request.get('score_engine') or previous.get('piano_engine') or config.get('score',{}).get('piano_engine','duet')
        if kind=='run':
            from .run_config import prepare_run_config
            config=prepare_run_config(request,config,pipeline.ROOT)
        tempo_only=kind=='reexport' and 'tempo_base_export' in request
        needs_inference=not tempo_only and (kind=='run' or (not guitar_only and options['mode']!='midi' and engine=='duet')
            or chord_inference_needed(request,project_state if kind=='reexport' else None))
        cache_only=kind=='reexport' and request.get('cache_only_reexport') is True
        managed_cached_score=request.get('workflow_cached_score') is True
        cache_only_run=kind=='run' and request.get('cache_only_run') is True
        if managed_cached_score:
            if (kind!='run' or request.get('workflow_phase')!='score'
                    or not request.get('workflow_project') or not request.get('workflow_checkpoint',{}).get('complete')):
                raise ValueError('无法验证仅从已完成推理继续制谱的请求。')
            from .managed_score_recovery import cached_score_config
            config=cached_score_config(request,config,pipeline.ROOT,prepared=True)
            cache_only_run=True
            needs_inference=False
            log('复用上一版已校验的推理，当前只执行制谱后处理。')
        elif cache_only_run:
            from .export_readiness import cached_run_ready
            if not cached_run_ready(request,config=config):
                raise ValueError('编辑内容或分析缓存已变化，请重新开始并选择计算设备；本次未启动模型。')
            needs_inference=False
            log('复用已校验的完整分析缓存，无需 GPU 或新的模型推理')
        if cache_only:
            from .export_readiness import cached_reexport_ready
            if not cached_reexport_ready(request,project_state):
                raise ValueError('编辑内容或分析缓存已变化，请重新导出以选择计算设备；本次未启动模型。')
            needs_inference=False
            log('复用已校验的分析缓存，无需 GPU 或新的模型推理')
        if needs_inference:
            config,device=apply_device_policy(config,request.get('execution_device','prefer_gpu'),separator_only=guitar_only or separation_only)
            if separation_only:
                device['message']=f'本次分轨使用 {"GPU" if device["device"]=="cuda" else "CPU"}。'
            emit('device',**device)
            log(device['message'])
        score_options=None
        if guitar_only:
            options={'mode':'all'}
        elif kind=='run':
            pass  # prepare_run_config already applied the exact preflight options.
        elif request.get('score_engine') is not None:
            if (request['score_engine'] not in ('music21','duet') or type(request.get('score_pedal',False)) is not bool
                    or type(request.get('score_chords',False)) is not bool
                    or request.get('score_voice_mode','simple') not in ('simple','detailed')):
                raise ValueError('无效的制谱选项')
            score_options={**config.get('score',{}),'piano_engine':request['score_engine'],
                'show_pedal':request.get('score_pedal',False),'show_chords':request.get('score_chords',False),
                'voice_mode':request.get('score_voice_mode','simple')}
        elif kind=='reexport':
            # Apply the selected device even if a legacy client omitted UI score
            # options; the project's engine/confirmed key remain authoritative.
            score_options={**previous,'piano_engine':previous.get('piano_engine','duet'),
                           **({key:config['score'][key] for key in ('device','python')} if needs_inference else {})}
        if kind=='reexport':
            if cache_only and not guitar_only:
                score_options={**(score_options or previous),'_harmony_reuse_only':True}
            if tempo_only and not guitar_only:
                score_options={**previous,'_harmony_reuse_only':True}
                score_options.setdefault('piano_engine',engine)
            if score_options is not None and request.get('score_duet_device') in ('cpu','cuda'):
                # Execution device only (results are identical); applies to 更新谱面 too.
                score_options={**score_options,'duet_device':request['score_duet_device']}
            return pipeline.reexport(request['project'],quantization=quantization,score_options=score_options,log=log,
                editor_revision=request.get('editor_revision'),export_options=request.get('editor_export_options'),
                **({'provisional_score':request['provisional_score']} if 'provisional_score' in request else {}),**options)
        if score_options is not None:
            if request.get('score_duet_device') in ('cpu','cuda'):
                score_options={**score_options,'duet_device':request['score_duet_device']}
            config=dict(config,score=score_options)
        if quantization is not None:
            config=dict(config,quantization=quantization)
        if kind=='midi_import':
            from .midi_import import import_midi_project

            emit('stage',message='读取 MIDI 轨道与速度图')
            return import_midi_project(request['midi'],request['output'],tracks=request['tracks'],
                channels=request.get('channels'),config=config,mode=options['mode'],log=log)
        project_options={}
        if 'workflow_project' in request:
            from .release_paths import data_root
            from .task_products import active_project
            project=str(active_project(data_root(),job_directory))
            if project!=request['workflow_project']:
                raise ValueError('任务请求与已分配的目录不符。')
            project_options['workflow_project']=project
        if request.get('workflow_phase')=='separation' and 'workflow_project' in project_options:
            from .task_products import active_checkpoint, record_checkpoint

            def publish_separation(preview):
                stems=preview['stem_paths']
                record_checkpoint(job_directory,{'project':preview['project'],'source':preview['source'],
                    'duration':preview['duration'],'stems':stems,'source_fingerprint':preview['source_fingerprint'],
                    'separator_key':preview['separator_key']},partial=set(stems)!=set(request['instruments']))

            project_options['record_separation']=publish_separation
            if request.get('workflow_checkpoint'):
                project_options['workflow_checkpoint']=active_checkpoint(data_root(),job_directory,request['workflow_checkpoint'])
        if managed_cached_score:
            from .task_products import active_checkpoint
            project_options['workflow_checkpoint']=active_checkpoint(data_root(),job_directory,request['workflow_checkpoint'])
            project_options['workflow_cached_score']=True
        if separation_only:
            project_options['separation_only']=True
        output=pipeline.run(request['audio'],request['output'],request['instruments'],
            config=config,keep_stems=guitar_only or request.get('keep_stems',True),log=log,
            **({'cache_only':True} if cache_only_run else {}),**project_options,**options)
        return output
    finally:
        pipeline.project_lock=original_lock


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('request',type=Path)
    request_path=parser.parse_args().request
    with (request_path.parent/'events.jsonl').open('a',encoding='utf-8') as log_file:
        def emit(event_type,**fields):
            line=json.dumps({'type':event_type,**fields},ensure_ascii=False)
            log_file.write(line+'\n')
            log_file.flush()
            print(line,flush=True)

        try:
            managed=os.environ.get(MANAGED_JOB)
            if managed:
                join_managed_job(managed)
                emit('ready',pid=os.getpid())
                if sys.stdin.readline().strip()!='start':
                    return 1
            request=json.loads(request_path.read_text(encoding='utf-8'))
            result=run_request(request,emit,request_path.parent)
            if isinstance(result,dict) and result.get('kind')=='notes-score-draft':
                emit('provisionalScore',receipt=result)
            elif isinstance(result,dict):
                emit('separated',preview=result)
            else:
                emit('completed',project=str(result))
            return 0
        except Exception as exc:
            emit('log',message=traceback.format_exc())
            emit('failed',message=str(exc) or type(exc).__name__)
            return 1


if __name__=='__main__':
    raise SystemExit(main())
