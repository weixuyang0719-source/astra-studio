import argparse
import json
from pathlib import Path
import subprocess
import sys
from . import pipeline


def doctor(skip_models=False):
    config=pipeline.load_config()
    root=pipeline.ROOT
    checks={}
    from .rhythm_provider import selected_provider
    provider=selected_provider(config)
    checks['rhythm_provider']=provider
    modules=[('separator','audio_separator'),('transkun','transkun')]
    if provider=='beat_this':modules.append(('beat_this','beat_this'))
    for name,module in modules:
        python=config.get('models',{}).get(name,{}).get('python',sys.executable)
        probe=subprocess.run([python,'-c',f"import importlib.util; print(bool(importlib.util.find_spec('{module}')))"],
            capture_output=True,text=True)
        checks[name]=probe.returncode==0 and probe.stdout.strip()=='True'
    if provider=='cubase_local':
        try:
            from .cubase_local_worker import _component,DLL_PATH
            checks['cubase_local']=bool(_component(config['models']['cubase_local'].get('component_path',DLL_PATH)))
        except (OSError,ValueError,ImportError) as exc:
            checks['cubase_local']=False
            checks['rhythm_error']=f'需要本机已核验的 Cubase 16 节拍组件：{exc}；不会自动改用另一算法。'
    try:
        import torch
        checks['torch']=torch.__version__
        checks['cuda']=torch.cuda.is_available()
        checks['gpu']=torch.cuda.get_device_name() if checks['cuda'] else None
        if checks['cuda']:
            tensor=torch.ones(32,device='cuda')
            checks['cuda_kernel']=float((tensor@tensor).cpu())==32.
            checks['cuda_architectures']=torch.cuda.get_arch_list()
    except ImportError:
        checks['cuda']=False
    from .adapters import SEPARATOR_ASSETS, XLANCE_PIANO_MODEL, XLANCE_PIANO_CONFIG
    models=Path(config.get('model_dir',root/'models'))
    assets=(*SEPARATOR_ASSETS,*(('final0.ckpt',) if provider=='beat_this' else ()))
    if config.get('models',{}).get('separator',{}).get('piano_model') == 'xlance':
        assets=(*assets,XLANCE_PIANO_MODEL,XLANCE_PIANO_CONFIG)
    checks['model_files']={name:(models/name).is_file() for name in assets}
    from .osmd_pdf import renderer_python
    checks['local_score_pdf']=renderer_python().is_file()
    print(json.dumps(checks,ensure_ascii=False,indent=2))
    return 0 if all(checks.get(k) for k in ('separator','transkun',provider,'cuda','cuda_kernel')) and (skip_models or all(checks['model_files'].values())) else 1


def main():
    parser=argparse.ArgumentParser(description='Astra 本地钢琴转谱与钢琴 / 吉他分轨（吉他仅音频）')
    commands=parser.add_subparsers(dest='command')
    commands.add_parser('gui')
    check=commands.add_parser('doctor')
    check.add_argument('--skip-models',action='store_true',help='仅检查环境，允许尚未下载权重')
    run=commands.add_parser('run')
    run.add_argument('audio')
    run.add_argument('--output',default=str(pipeline.ROOT/'outputs'))
    run.add_argument('--instruments',nargs='+',choices=['piano','guitar'],default=['piano','guitar'])
    run.add_argument('--no-stems',action='store_true',help='不保存钢琴分轨；吉他仅分轨模式始终保留音频')
    reexport=commands.add_parser('reexport')
    reexport.add_argument('project')
    inspect=commands.add_parser('inspect-midi',help='列出MIDI轨道/声道、原速度与拍号，不运行模型')
    inspect.add_argument('midi')
    imported=commands.add_parser('import-midi',help='选定MIDI钢琴轨道直接制谱，跳过音频模型')
    imported.add_argument('midi')
    imported.add_argument('--output',default=str(pipeline.ROOT/'outputs'))
    imported.add_argument('--tracks',nargs='+',type=int,required=True,help='轨道索引（从0开始，先运行inspect-midi）')
    imported.add_argument('--channels',nargs='+',type=int,help='可选声道索引0–15；默认所选轨道全部非鼓声道')
    imported.add_argument('--mode',choices=['midi','score','all'],default='all')
    imported.add_argument('--engine',choices=['duet','music21'],default='duet')
    imported.add_argument('--show-chords',action='store_true')
    imported.add_argument('--show-pedal',action='store_true')
    for command in (run,reexport,imported):
        command.add_argument('--device',choices=['prefer_gpu','gpu','cpu'],default='prefer_gpu',
                             help='默认优先GPU；GPU忙时请明确选择gpu或cpu')
    for command in (run,reexport):
        command.add_argument('--mode',choices=['midi','score','all'],default='all')
        command.add_argument('--tempo',help='tempo_map.json 或 DAW 导出的 MIDI Tempo Track')
        command.add_argument('--bpm',type=float)
        command.add_argument('--meter',default='4/4')
        command.add_argument('--beat-scale',type=float,choices=[.5,1.,2.],default=1.)
    args=parser.parse_args()
    if args.command in (None,'gui'):
        from .launcher import main as gui_main
        gui_main();return 0
    if args.command=='doctor':return doctor(args.skip_models)
    try:
        if args.command=='inspect-midi':
            from .midi_import import inspect_midi
            print(json.dumps(inspect_midi(args.midi),ensure_ascii=False,indent=2))
            return 0
        if args.command=='import-midi':
            from .midi_import import import_midi_project
            config=pipeline.load_config()
            config['score']={**config.get('score',{}),'piano_engine':args.engine,
                             'show_chords':args.show_chords,'show_pedal':args.show_pedal}
            if args.mode!='midi' and args.engine=='duet':
                from .device_policy import apply_device_policy
                config,device=apply_device_policy(config,args.device)
                print(device['message'])
            project=import_midi_project(args.midi,args.output,tracks=args.tracks,channels=args.channels,
                                        config=config,mode=args.mode)
            print(project)
            return 0
        options=dict(tempo_path=args.tempo,bpm=args.bpm,meter=args.meter,beat_scale=args.beat_scale,mode=args.mode)
        if args.command=='run':
            from .device_policy import apply_device_policy
            config,device=apply_device_policy(pipeline.load_config(),args.device)
            print(device['message'])
            project=pipeline.run(args.audio,args.output,args.instruments,config=config,keep_stems=not args.no_stems,**options)
        else:
            prior_state=pipeline.read_json(Path(args.project)/'project.json')
            previous=prior_state.get('config',{})
            score_options=None
            if ('piano' in prior_state.get('instruments',[]) and args.mode!='midi'
                    and previous.get('score',{}).get('piano_engine','duet')=='duet'):
                from .device_policy import apply_device_policy
                config,device=apply_device_policy(previous,args.device)
                print(device['message'])
                score_options=config['score']
            project=pipeline.reexport(args.project,score_options=score_options,**options)
        print(project)
    except Exception as exc:
        print(f'错误：{exc}',file=sys.stderr)
        return 1
    return 0


if __name__=='__main__':
    raise SystemExit(main())
