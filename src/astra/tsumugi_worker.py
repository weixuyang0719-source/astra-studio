"""Isolated official Tsumugi inference. All inputs and weights remain local."""
import json
import os
from pathlib import Path
import sys
from time import monotonic


def run(request):
    root=Path(request['root']); device=request['device']
    if device not in ('cpu','cuda'): raise ValueError('Invalid device')
    os.environ.update(HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1')
    sys.path.insert(0,str(root/'vendor/tsumugi-chords'))
    import torch
    import soundfile as sf
    from tsumugi_chords import HeadChords
    from .tsumugi_chords import SOURCE_COMMIT,MODEL_REVISION
    torch.set_num_threads(4)
    if device=='cuda':
        if not torch.cuda.is_available(): raise RuntimeError('CUDA 不可用，请选择 CPU')
        torch.cuda.reset_peak_memory_stats()
    began=monotonic(); tracker=HeadChords.load(root/'models/tsumugi-chords',device=device)
    print('Tsumugi-Chords v0.2.0：使用已确认节拍与调性，开始分析',flush=True)
    inferred=tracker.transcribe(request['audio'],request['conditioning'])
    if tracker._keys is not None: raise RuntimeError('Unexpected large key-model inference')
    duration=sf.info(request['audio']).duration
    segments=[{'start':float(c.start),'end':min(float(c.end),duration),'label':c.label,
               'symbol':c.symbol,'quality':c.quality} for c in inferred['chords'] if c.start<duration]
    # 25 Hz output frames can leave a final <40 ms rounding gap.
    if segments and 0<=duration-segments[-1]['end']<=.041: segments[-1]['end']=duration
    result={'duration_seconds':duration,'segments':segments,
        'model':{'name':'Tsumugi-Chords','engine':'tsumugi','version':'0.2.0',
                 'source_commit':SOURCE_COMMIT,'revision':MODEL_REVISION},
        'conditioning':request['conditioning'],
        'execution':{'device':device,'seconds':monotonic()-began,
                     'cuda_peak_allocated_bytes':torch.cuda.max_memory_allocated() if device=='cuda' else 0}}
    Path(request['output']).write_text(json.dumps(result,ensure_ascii=False),encoding='utf-8')
    print(f"完成：{len(segments)} 段，{result['execution']['seconds']:.1f} 秒",flush=True)


if __name__=='__main__':run(json.loads(Path(sys.argv[1]).read_text(encoding='utf-8')))
