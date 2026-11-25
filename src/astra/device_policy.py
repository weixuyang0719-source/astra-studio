"""Lightweight job device selection; safe to import without loading PyTorch."""
from .release_paths import runtime_env_root
from copy import deepcopy
from pathlib import Path
import csv
import os
import subprocess


DEVICES = ('prefer_gpu', 'gpu', 'cpu')


def probe_gpu_status(timeout=2.0):
    """Use nvidia-smi only. Call from a background task, not the GUI event loop.

    Busy is an instantaneous hint, not a reservation or a promise of free VRAM.
    Desktop display use alone is not treated as a heavy inference workload.
    """
    result = {'available': False, 'busy': False, 'name': None,
              'memory_total_mb': 0, 'memory_used_mb': 0, 'memory_free_mb': 0,
              'utilization_percent': 0, 'reason': ''}
    try:
        process = subprocess.run(
            ['nvidia-smi', '--query-gpu=name,memory.total,memory.used,utilization.gpu',
             '--format=csv,noheader,nounits'], capture_output=True, text=True,
            encoding='utf-8', errors='replace', timeout=timeout,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), check=False)
        if process.returncode:
            result['reason'] = '未能读取 NVIDIA GPU 状态，可选择 CPU。'
            return result
        row = next(csv.reader(process.stdout.splitlines()))
        name, total, used, utilization = (item.strip() for item in row[:4])
        total, used, utilization = int(total), int(used), int(utilization)
        free = max(0, total - used)
        busy = utilization >= 20 or free < 6144
        result.update(available=True, busy=busy, name=name, memory_total_mb=total,
                      memory_used_mb=used, memory_free_mb=free, utilization_percent=utilization,
                      reason=f'GPU 当前使用率 {utilization}%，可用显存 {free/1024:.1f}/{total/1024:.1f} GB。'
                             + ('可改用 CPU，或确认继续使用 GPU。' if busy else '优先使用 GPU。'))
    except (OSError, subprocess.TimeoutExpired, ValueError, IndexError, StopIteration):
        result['reason'] = 'GPU 状态暂不可用，可选择 CPU；未加载任何推理模型。'
    return result


def apply_device_policy(config, requested='prefer_gpu', *, probe=None, root=None, separator_only=False):
    """Return isolated config + decision; never silently use CPU for explicit GPU.

    A busy GPU requires the UI to submit an explicit gpu/cpu choice. Selecting
    CPU routes all supported models to CPU, including independently spawned
    separator, transcription, beat and notation workers.
    """
    if requested not in DEVICES:
        raise ValueError('计算设备须为 prefer_gpu、gpu 或 cpu')
    status = probe if probe is not None else probe_gpu_status()
    if requested == 'prefer_gpu' and status['available'] and status['busy']:
        raise ValueError('GPU 正在使用中，请选择本次使用 CPU 或明确继续 GPU。' + status['reason'])
    if requested == 'gpu' and not status['available']:
        raise ValueError('未发现可用 NVIDIA GPU；请选择 CPU。' + status['reason'])
    selected = 'cuda' if requested != 'cpu' and status['available'] else 'cpu'
    updated = deepcopy(config)
    updated['execution_device'] = requested
    models = updated.setdefault('models', {})
    for name in ('separator',) if separator_only else ('separator', 'transkun', 'beat_this', 'cubase_local', 'basic_pitch'):
        models[name] = {**models.get(name, {}), 'device': selected}
    if not separator_only:
        root = Path(root) if root else runtime_env_root()
        score_env = '.venv-score-gpu' if selected == 'cuda' else '.venv-score'
        python = root / score_env / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
        updated['score'] = {**updated.get('score', {}), 'device': selected, 'python': str(python)}
    decision = {'requested': requested, 'device': selected, 'gpu_status': status,
                'message': ('本次使用 GPU：分轨、转录、节拍与 DuET 按需依次加载。' if selected == 'cuda'
                            else '本次使用 CPU：所有支持的推理阶段均在 CPU 运行。')}
    if separator_only:
        decision['message'] = f'吉他分轨使用 {"GPU" if selected=="cuda" else "CPU"}。'
    return updated, decision
