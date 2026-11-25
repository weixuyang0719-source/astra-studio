"""Pure desktop run options, shared by cache preflight and execution."""
from copy import deepcopy
from pathlib import Path

from .release_paths import runtime_env_root


def prepare_run_config(request, config, root):
    if request.get('kind')!='run':raise ValueError('需要完整运行请求')
    result=deepcopy(config)
    instruments=request.get('instruments',[])
    if not instruments or any(i not in ('piano','guitar') for i in instruments):
        raise ValueError('请选择钢琴和/或吉他')
    guitar_only='piano' not in instruments
    provider=request.get('rhythm_provider',result.get('rhythm_provider','cubase_local'))
    if provider not in ('beat_this','cubase_local'):raise ValueError('节拍来源须为 beat_this 或 cubase_local')
    if not guitar_only:
        result['rhythm_provider']=provider
        if provider=='cubase_local':
            runtime=runtime_env_root(root)
            models=result.setdefault('models',{})
            models['cubase_local']={**models.get('cubase_local',{}),
                'python':str(runtime/'.venv'/'Scripts'/'python.exe'),
                'min_bpm':60.,'max_bpm':240.,'meter_stay':.95,'quarters_per_pulse':1.}
    engine=request.get('score_engine')
    if engine is not None and not guitar_only:
        if (engine not in ('music21','duet') or type(request.get('score_pedal',False)) is not bool
                or type(request.get('score_chords',False)) is not bool
                or request.get('score_voice_mode','simple') not in ('simple','detailed')):
            raise ValueError('无效的制谱选项')
        result['score']={**result.get('score',{}),'piano_engine':engine,
            'show_pedal':request.get('score_pedal',False),'show_chords':request.get('score_chords',False),
            'voice_mode':request.get('score_voice_mode','simple'),
            'duet_device':request.get('score_duet_device') if request.get('score_duet_device') in ('cpu','cuda') else 'cpu'}
    quantization=request.get('quantization')
    if quantization is not None:
        if quantization not in ('auto','straight','triplet','fine'):raise ValueError('量化应为 auto/straight/triplet/fine')
        result['quantization']=quantization
    model=request.get('piano_model')
    if model is not None and not guitar_only:
        if model not in ('xlance','sw'):raise ValueError('钢琴分轨模型须为 xlance 或 sw')
        models=result.setdefault('models',{})
        models['separator']={**models.get('separator',{}),'piano_model':model}
    return result
