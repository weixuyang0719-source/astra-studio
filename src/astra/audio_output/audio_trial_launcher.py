"""Explicit standalone audio trial. No installer, no dependency installation."""
import argparse,json,sys,shutil,uuid
from pathlib import Path
from .audio_trial_preflight import assess_trial

def prepare_sources(paths,destination):
 import soundfile as sf
 destination=Path(destination).resolve();destination.mkdir(parents=True,exist_ok=False)
 rows=[]
 for index,value in enumerate(paths):
  source=Path(value).resolve(strict=True)
  if not source.is_file():raise ValueError('音频路径必须是文件。')
  info=sf.info(source)
  if info.format not in ('WAV','WAVEX') or info.samplerate!=48000 or info.channels not in (1,2):raise ValueError('此轮试用仅接受48kHz单声道/双声道WAV，其他格式请使用原应用。')
  target=destination/f'source-{index+1:02}.wav';shutil.copy2(source,target)
  rows.append(dict(key='original' if index==0 else f'stem-{index}',label='原曲' if index==0 else f'分轨 {index}',path=str(target),duration=float(info.duration),peaks=[]))
 (destination/'trial.json').write_text(json.dumps({'read_only_originals':True,'sources':rows},ensure_ascii=False,indent=2),encoding='utf-8')
 return rows

def main(argv=None,fallback=None,assessment=None):
 parser=argparse.ArgumentParser(description='Astra 独立音频试用，不修改正式工程')
 parser.add_argument('--check',action='store_true');parser.add_argument('--audio',action='append',default=[]);parser.add_argument('--session-root')
 args=parser.parse_args(argv);report=(assessment or assess_trial)()
 if args.check:print(json.dumps(report,ensure_ascii=False,indent=2));return 0 if report['can_import_candidate'] else 2
 if not report['can_import_candidate']:
  print('音频试用依赖不齐，未启动候选；改用原应用。缺少：'+', '.join(report['missing']),file=sys.stderr)
  if fallback is None:
   from astra.studio import main as fallback
  return fallback()
 if not args.audio:parser.error('请以 --audio 指定音频；不会自动打开或改写原工程。')
 base=Path(args.session_root) if args.session_root else Path(__file__).resolve().parent.parent/'sessions'
 try:sources=prepare_sources(args.audio,base/uuid.uuid4().hex)
 except (OSError,ValueError,RuntimeError) as exc:print('未启动试用：'+str(exc),file=sys.stderr);return 2
 from .audio_trial_facade import TrialFacade
 from . import trial_window
 trial_window.TRIAL_SESSION_ROOT=Path(sources[0]['path']).parent
 trial_window.StudioBridge=lambda parent:TrialFacade(parent,sources=sources,allow_raw=report['can_try_raw'])
 if not report['can_try_raw']:print('RAW依赖/版本尚未验证，本次仅兼容输出。',file=sys.stderr)
 return trial_window.main()

if __name__=='__main__':raise SystemExit(main())
