"""Pinned, offline Tsumugi-Chords adapter with conditioning-bound caches."""
from __future__ import annotations
from .release_paths import data_root, runtime_env_root

import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

from .chordformer import _sha, _key, _cached, _validate, _worker_environment

ROOT = data_root()
SOURCE_COMMIT = '755b1e2a3838853c66321dfe29a5fc2c7bf0c325'
MODEL_REVISION = 'a6731e1eea7353773188421702ff67049a84ce7a'


def conditioning_from_export(base_export):
    """Reuse the committed notation's beats/keys; never load a 2.8GB key encoder."""
    from .timing import TempoMap
    from .score_tempo import _measure_layout
    base = Path(base_export)
    tempo = json.loads((base/'tempo_map.json').read_text(encoding='utf-8'))
    harmony = json.loads((base/'piano_harmony.json').read_text(encoding='utf-8'))
    clock = TempoMap(tempo)
    layout = _measure_layout(ET.parse(base/'piano.musicxml').getroot())
    if not layout:
        raise ValueError('请先生成钢琴谱面，再分析 Tsumugi 和弦')
    measures = [row for row in layout if row[0] is layout[0][0]]
    duration = float(clock.seconds[-1])
    info = {'duration': duration,
            'beats': [clock.to_seconds(b) for b in range(math.ceil(clock.to_beat(duration)))],
            'downbeats': [clock.to_seconds(float(row[2])) for row in measures
                         if float(row[2]) >= tempo.get('pickup_beats', 0)], 'keys': []}
    for region in harmony.get('key_regions', []):
        first, last = int(region['start_index']), int(region['end_index'])
        if not 0 <= first <= last < len(measures):
            raise ValueError('调性分段与当前谱面不一致，请先重导出')
        start, stop = float(measures[first][2]), float(measures[last][2]+measures[last][3])
        from music21.pitch import Pitch
        # Official decoder indexes a sharp-only pitch-class vocabulary. Score
        # spellings (Bb/Eb etc.) must not be passed to tuple.index verbatim.
        tonic=('C','C#','D','D#','E','F','F#','G','G#','A','A#','B')[Pitch(region['tonic'].replace('b','-')).pitchClass]
        info['keys'].append({'start': clock.to_seconds(start),
                             'end': min(duration, clock.to_seconds(stop)),
                             'key': tonic+':'+region['mode']})
    if not info['keys']:
        raise ValueError('当前谱面缺少调性分析，请先重导出再运行 Tsumugi')
    return info


def _identity(audio, conditioning):
    installed=runtime_env_root(ROOT)
    source, model = installed/'vendor/tsumugi-chords', installed/'models/tsumugi-chords'
    pin_path = source/'ASTRA-PIN.json'
    if not pin_path.is_file():
        raise FileNotFoundError('Tsumugi-Chords 未安装，请运行 scripts/install-tsumugi.py')
    pin = json.loads(pin_path.read_text(encoding='utf-8'))
    if pin.get('commit') != SOURCE_COMMIT or pin.get('revision') != MODEL_REVISION:
        raise ValueError('Tsumugi-Chords 安装版本不匹配')
    for folder, files in ((source,pin['source_files']), (model,pin['weights'])):
        for name, expected in files.items():
            path = (folder/name).resolve()
            if not path.is_relative_to(folder.resolve()) or _sha(path) != expected:
                raise ValueError('Tsumugi-Chords 文件校验失败：'+name)
    return {'version': 1, 'input_sha256': _sha(audio), 'source_commit': SOURCE_COMMIT,
            'model_revision': MODEL_REVISION, 'source_files': pin['source_files'],
            'weights': pin['weights'], 'conditioning': conditioning}


def cached_prediction(audio, cache_dir, cache_key):
    """Explicitly reuse the committed prediction with its ORIGINAL conditioning.

    Tempo repair remaps original-second labels only. It never pretends that the
    network has heard a new beat/key conditioning or silently selects another run.
    """
    if not isinstance(cache_key,str) or len(cache_key)!=64 or any(c not in '0123456789abcdef' for c in cache_key):
        raise ValueError('缺少有效的 Tsumugi 缓存身份，请先分析和弦')
    target = Path(cache_dir)/(cache_key+'.json')
    try:
        envelope = json.loads(target.read_text(encoding='utf-8'))
        identity = _identity(audio, envelope['identity']['conditioning'])
        result = _cached(target, identity)
        if result is None or _key(identity)!=cache_key: raise ValueError('cache mismatch')
    except (OSError,ValueError,KeyError,TypeError) as exc:
        raise ValueError('Tsumugi 和弦缓存缺失或来源已变，请先分析和弦；补拍不会重新推理') from exc
    return {**result, 'cached': True, 'cache_key': cache_key, 'cache_path': str(target),
            'conditioning_reused': True}


def analyze_audio_chords(audio_path, cache_dir, conditioning, device='cuda', log=print, force=False):
    device = {'gpu':'cuda','auto':'cuda'}.get(device,device)
    if device not in ('cpu','cuda'): raise ValueError('Tsumugi device 应为 cpu 或 cuda')
    audio = Path(audio_path).resolve(strict=True)
    identity = _identity(audio, conditioning); key = _key(identity)
    cache = Path(cache_dir).resolve(); cache.mkdir(parents=True, exist_ok=True)
    target = cache/(key+'.json')
    result = None if force else _cached(target, identity)
    if result is not None:
        log('[tsumugi] 复用音频、节拍及调性一致的和弦缓存')
        return {**result,'cached':True,'cache_key':key,'cache_path':str(target)}
    with tempfile.TemporaryDirectory(prefix='.tsumugi-',dir=cache) as temporary:
        pending=Path(temporary); output=pending/'result.json'; request=pending/'request.json'
        request.write_text(json.dumps({'root':str(runtime_env_root(ROOT)),'audio':str(audio),'output':str(output),
                                      'device':device,'conditioning':conditioning}),encoding='utf-8')
        executable=runtime_env_root(ROOT)/'.venv'/('Scripts/python.exe' if os.name=='nt' else 'bin/python')
        child=subprocess.Popen([str(executable if executable.is_file() else sys.executable),
            '-m','astra.tsumugi_worker',str(request)],cwd=ROOT,env=_worker_environment(device),
            stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,encoding='utf-8',errors='replace',
            creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        try:
            for line in child.stdout: log('[tsumugi] '+line.rstrip())
            if child.wait(): raise RuntimeError('Tsumugi 和弦推理失败，请查看日志')
        finally:
            if child.poll() is None: child.terminate(); child.wait()
            child.stdout.close()
        result=_validate(json.loads(output.read_text(encoding='utf-8')))
        if _identity(audio,conditioning)!=identity: raise ValueError('分析过程中音频或模型已变化')
        staged=pending/'cache.json'
        staged.write_text(json.dumps({'identity':identity,'result':result,
            'result_sha256':_key(result)},ensure_ascii=False),encoding='utf-8')
        os.replace(staged,target)
    return {**result,'cached':False,'cache_key':key,'cache_path':str(target)}
