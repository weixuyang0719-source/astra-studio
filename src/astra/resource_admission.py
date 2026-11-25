"""Admission for memory-heavy CUDA launches in a managed Windows desktop job."""
import csv
import ctypes
from ctypes import wintypes
import os
import subprocess
from time import monotonic, sleep

from .desktop_worker import MANAGED_JOB

_HOST_MIN_BYTES = 2 * 1024**3
_VRAM_MIN_MIB = 1024
_WAIT_SECONDS = 20.0
_SAMPLE_SECONDS = 0.8
WAITING_PREFIX = '[gpu-pressure:waiting] '
RESUMED_PREFIX = '[gpu-pressure:resumed] '


class _PerformanceInformation(ctypes.Structure):
    _fields_ = [('cb', wintypes.DWORD)] + [
        (name, ctypes.c_size_t) for name in (
            'commit_total', 'commit_limit', 'commit_peak', 'physical_total',
            'physical_available', 'system_cache', 'kernel_total', 'kernel_paged',
            'kernel_nonpaged', 'page_size')
    ] + [(name, wintypes.DWORD) for name in ('handle_count', 'process_count', 'thread_count')]


def _host_headroom():
    info = _PerformanceInformation()
    info.cb = ctypes.sizeof(info)
    try:
        read = ctypes.WinDLL('psapi', use_last_error=True).GetPerformanceInfo
        read.argtypes = [ctypes.POINTER(_PerformanceInformation), wintypes.DWORD]
        read.restype = wintypes.BOOL
        if read(ctypes.byref(info), info.cb) and info.commit_limit and info.page_size:
            return max(0, info.commit_limit - info.commit_total) * info.page_size
    except OSError:
        pass
    return None


def _visible_vram(output, mask):
    """Use memory from a uniquely identified device; never guess CUDA ordering."""
    rows = []
    for row in csv.reader(output.splitlines()):
        if len(row) != 2:
            return None
        identifier, available = (item.strip() for item in row)
        try:
            free_mib = int(available)
        except ValueError:
            return None
        if not identifier.startswith('GPU-') or free_mib < 0:
            return None
        rows.append((identifier, free_mib))
    if mask is not None:
        first = mask.split(',', 1)[0].strip()
        if first.startswith('GPU-'):
            rows = [row for row in rows if row[0].startswith(first)]
        elif first != '0' or len(rows) != 1:
            return None
    return rows[0][1] if len(rows) == 1 else None


def _gpu_headroom(timeout):
    try:
        result = subprocess.run(
            ['nvidia-smi', '--query-gpu=uuid,memory.free', '--format=csv,noheader,nounits'],
            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=timeout,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return _visible_vram(result.stdout, os.environ.get('CUDA_VISIBLE_DEVICES')) if not result.returncode else None


def _sample(timeout):
    host = _host_headroom()
    # Under severe commit pressure, do not spawn another process just to read VRAM.
    vram = None if host is not None and host < _HOST_MIN_BYTES else _gpu_headroom(timeout)
    return host, vram


def _shortages(values):
    limits = (_HOST_MIN_BYTES, _VRAM_MIN_MIB)
    return {i for i, (value, limit) in enumerate(zip(values, limits)) if value is not None and value < limit}


def _describe(values, blocked):
    host, vram = values
    labels = [
        ('系统提交内存余量不可读' if host is None else f'系统提交内存仅余 {host / 1024**3:.1f} GiB，需至少 2 GiB'),
        ('GPU 显存余量不可读' if vram is None else f'GPU 显存仅余 {vram} MiB，需至少 {_VRAM_MIN_MIB} MiB'),
    ]
    return '，'.join(labels[index] for index in sorted(blocked))


def _await_resources(model, log, *, sample=_sample, clock=monotonic, sleeper=sleep, wait=_WAIT_SECONDS):
    """Healthy or initially unknown telemetry proceeds. Known pressure must clear twice."""
    from .preview_cancel import check_cancelled
    deadline = clock() + wait
    pressured = set()
    consecutive_good = 0
    blocked_message = ''
    while (remaining := deadline - clock()) > 0:
        check_cancelled()
        values = sample(min(1.0, remaining))
        check_cancelled()
        low = _shortages(values)
        if not low and not pressured:
            return
        pressured.update(low)
        blocked = low | {index for index in pressured if values[index] is None}
        if blocked:
            consecutive_good = 0
            if not blocked_message:
                log(f'{WAITING_PREFIX}等待 GPU 启动余量（{model}）：{_describe(values, blocked)}。')
            blocked_message = _describe(values, blocked)
        else:
            consecutive_good += 1
            if consecutive_good == 2:
                log(f'{RESUMED_PREFIX}资源余量已恢复，开始加载 {model}。')
                return
        remaining = deadline - clock()
        if remaining > 0:
            sleeper(min(_SAMPLE_SECONDS, remaining))
    check_cancelled()
    raise RuntimeError(f'{model} 暂未启动。等待 {wait:g} 秒后仍无法确认所需资源余量：{blocked_message}。')


def wait_for_model_admission(device, model, log):
    """Apply only to explicit CUDA requests from this launcher's managed job."""
    if device == 'cuda' and os.name == 'nt' and os.environ.get(MANAGED_JOB):
        _await_resources(model, log)

