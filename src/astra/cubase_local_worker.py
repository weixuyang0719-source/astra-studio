"""Local installed-parameter inference with separate observation/decode caches.

No DLL execution, parameter persistence, project-map publication or fallback.
Only already-decoded 44.1 kHz audio is accepted. Backend and arithmetic belong
to observation identity; musical decoder settings never invalidate inference.
"""
from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
import time
from zipfile import BadZipFile

import numpy as np
import soundfile as sf

from .cubase_audio_features import extract_features, _frame_count
from .cubase_local_network import DLL_PATH, DLL_SHA256, MODEL_SHA256, load_installed_network
from .cubase_pulse_decoder import decode_observations, period_candidates

WORKER_VERSION = 1
FEATURE_VERSION = 1
DECODER_VERSION = 1
NETWORK_VERSION = 1
PRECISION = 'float32_no_tf32_full_bidirectional_sequence'


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _key(value):
    return hashlib.sha256(_json(value).encode('utf-8')).hexdigest()


def _hash(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _atomic_bytes(path, contents):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.pending-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(contents); stream.flush(); os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name): os.unlink(name)


def _atomic_json(path, value):
    _atomic_bytes(path, _json(value).encode('utf-8'))


def _probabilities(values, frames):
    values = np.asarray(values)
    if (values.shape != (frames, 3) or values.dtype != np.float32
            or not np.isfinite(values).all() or np.any(values < 0) or np.any(values > 1)
            or not np.allclose(values.sum(axis=1), 1, atol=1e-5, rtol=0)):
        raise ValueError('本机节拍观测必须是对应音频的有限归一化 float32 N×3 概率')
    return values


def _component(path):
    path = Path(path).resolve(strict=True)
    if _hash(path) != DLL_SHA256:
        raise ValueError('本机节拍组件版本尚未核验，不能复用其它版本缓存')
    return path


@contextmanager
def _precision(device, threads=2):
    if device == 'numpy':
        yield; return
    import torch
    previous = torch.backends.cuda.matmul.allow_tf32
    previous_threads = torch.get_num_threads()
    try:
        torch.set_num_threads(threads)
        torch.backends.cuda.matmul.allow_tf32 = False
        with torch.backends.cudnn.flags(allow_tf32=False):
            yield
    finally:
        torch.backends.cuda.matmul.allow_tf32 = previous
        torch.set_num_threads(previous_threads)


def _verify_backend(network, device):
    if not network.self_test:
        raise ValueError('本机节拍网络未通过内置自检')
    if device == 'numpy':
        return dict(backend=device, reference='embedded_fixture', **network.self_test)
    # The loader verified NumPy against the component's embedded fixture.
    # Check the selected execution backend separately, not just the loader.
    probe = (np.arange(16 * 320, dtype=np.float32).reshape(16, 320) % 113) / np.float32(113)
    expected = network.predict(probe)
    actual = network.predict_torch(probe, device=device)
    error = float(np.max(np.abs(actual - expected)))
    if actual.shape != expected.shape or not math.isfinite(error) or error > 1e-5:
        raise ValueError(f'本次 {device} 节拍后端自检未通过；不会静默切换设备')
    return dict(backend=device, reference='verified_numpy_deterministic_probe',
                frames=len(probe), max_absolute_error=error, tolerance=1e-5)


def _decoder_options(options, frames):
    values = dict(hop_ms=10., min_bpm=float(options.get('min_bpm', 60.)),
                  max_bpm=float(options.get('max_bpm', 240.)),
                  meters=list(options.get('meters', (3, 4))),
                  transition_lambda=float(options.get('transition_lambda', 180.)),
                  meter_stay=float(options.get('meter_stay', 1.)),
                  known_downbeats=json.loads(_json(options.get('known_downbeats', []))),
                  max_frames=int(options.get('max_frames', 120000)))
    periods = period_candidates(values['min_bpm'], values['max_bpm'], 10.)
    meters = values['meters']
    if (not meters or len(set(meters)) != len(meters)
            or any(type(m) is not int or not 1 <= m <= 10 for m in meters)
            or not math.isfinite(values['transition_lambda']) or values['transition_lambda'] < 0
            or not 0 <= values['meter_stay'] <= 1 or not 0 < frames <= values['max_frames']
            or sum(meters) * sum(periods) > 65535
            or frames * sum(meters) * len(periods) > 32_000_000):
        raise ValueError('本机节拍解码参数无效或超出内存预算')
    known = values['known_downbeats']
    if not isinstance(known, list) or len(known) > 256:
        raise ValueError('最多支持256个明确强拍约束')
    for row in known:
        seconds = float(row['seconds'])
        if (not math.isfinite(seconds) or not 0 <= math.floor(seconds * 100 + .5) < frames
                or row.get('meter', meters[0]) not in meters):
            raise ValueError('明确强拍约束不在音频或拍号候选范围内')
        if row.get('bpm') is not None:
            bpm, tolerance = float(row['bpm']), float(row.get('tolerance_bpm', 0))
            if not math.isfinite(bpm) or bpm <= 0 or not math.isfinite(tolerance) or tolerance < 0:
                raise ValueError('明确强拍速度约束无效')
    return values


def _read_observations(folder, identity, frames):
    from io import BytesIO
    manifest = json.loads((folder / 'manifest.json').read_text(encoding='utf-8'))
    if manifest['identity'] != identity: raise ValueError('观测缓存身份不符')
    digest = manifest['sha256']
    if len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
        raise ValueError('观测缓存摘要无效')
    path = folder / f'observations-{digest}.npz'
    contents = path.read_bytes()
    if hashlib.sha256(contents).hexdigest() != digest: raise ValueError('观测缓存已损坏')
    with np.load(BytesIO(contents), allow_pickle=False) as bundle:
        probabilities = _probabilities(bundle['probabilities'], frames).copy()
        provenance = json.loads(str(bundle['provenance'].item()))
    if (provenance.get('audio_source_sha256') != identity['source_sha256']
            or provenance.get('component_sha256') != identity['component_sha256']
            or provenance.get('network_backend') != identity['backend']
            or provenance.get('worker_observation_identity') != identity):
        raise ValueError('观测文件内部来源不符')
    return probabilities, provenance, path, digest


def redecode_cached_result(base_result, output_dir, options=None, log=print):
    """Decode verified existing network observations without audio/model access."""
    from .cubase_cached_decode import redecode_cached_result as run
    return run(base_result, output_dir, options, log)


def _write_observations(folder, identity, probabilities, provenance):
    from io import BytesIO
    data = BytesIO()
    np.savez_compressed(data, probabilities=probabilities, provenance=_json(provenance), hop_ms=10.)
    contents = data.getvalue(); digest = hashlib.sha256(contents).hexdigest()
    path = folder / f'observations-{digest}.npz'
    # Content-addressed payload first, atomic manifest last. Two producers of
    # the same key cannot publish a manifest referring to the other's ZIP hash.
    _atomic_bytes(path, contents)
    _atomic_json(folder / 'manifest.json', dict(identity=identity, sha256=digest))
    return path, digest


def analyze_audio(input_path, output_dir, options=None, log=print):
    """Return the decoder candidate plus absolute-source/provenance fields.

    Options: device='cuda'|'cpu'|'numpy'; origin_seconds (crop offset);
    source_sha256 (optional expected audio hash); component_path (installed
    audited DLL); decoder min_bpm/max_bpm/meters/transition_lambda/meter_stay/
    known_downbeats/max_frames. No option changes a project or source event.
    """
    options = dict(options or {}); started = time.perf_counter()
    source = Path(input_path).resolve(strict=True); output = Path(output_dir).resolve()
    if not source.is_file() or output == source or (output.exists() and not output.is_dir()):
        raise ValueError('节拍输出目录不能覆盖输入音频')
    device = options.get('device', 'cuda')
    if device not in ('cpu', 'cuda', 'numpy'): raise ValueError('请明确选择 CPU、CUDA 或 NumPy 后端')
    threads = options.get('threads', 2)
    if type(threads) is not int or not 1 <= threads <= 32: raise ValueError('网络线程数必须为1至32的整数')
    origin = float(options.get('origin_seconds', 0.))
    if not math.isfinite(origin) or origin < 0: raise ValueError('音频原点必须为非负有限秒数')
    source_hash = _hash(source)
    if options.get('source_sha256', source_hash) != source_hash:
        raise ValueError('输入音频与请求来源摘要不符')
    info = sf.info(source)
    if info.samplerate != 44100 or info.frames <= 0:
        raise ValueError('本机节拍分析需要非空44100 Hz音频；请由应用先解码/重采样')
    frames, _ = _frame_count(info.frames)
    decoder = _decoder_options(options, frames)
    component = _component(options.get('component_path', DLL_PATH))
    identity = dict(worker_version=WORKER_VERSION, feature_version=FEATURE_VERSION,
                    network_version=NETWORK_VERSION, source_sha256=source_hash,
                    component_sha256=DLL_SHA256, model_sha256=MODEL_SHA256,
                    sample_rate=44100, sample_frames=info.frames, channels=info.channels,
                    precision=PRECISION, backend=device, threads=threads if device != 'numpy' else None)
    observation_key = _key(identity); folder = output / 'observations' / observation_key
    observation_hit = False; feature_time = network_time = 0.
    try:
        probabilities, provenance, observation_file, observation_hash = _read_observations(folder, identity, frames)
        observation_hit = True
        log('[cubase_local] 复用已验证三类观测；不重新运行特征或网络')
    except (OSError, ValueError, KeyError, TypeError, EOFError, BadZipFile):
        log('[cubase_local] 验证本机组件与所选网络后端')
        network, bands, provenance = load_installed_network(component)
        with _precision(device, threads):
            backend_test = _verify_backend(network, device)
            samples, rate = sf.read(source, dtype='float32', always_2d=True)
            if len(samples) != info.frames or samples.shape[1] != info.channels or _hash(source) != source_hash:
                raise ValueError('音频在分析期间发生变化，已停止')
            log('[cubase_local] 计算44.1kHz多尺度音频特征')
            step = time.perf_counter(); features, feature_provenance = extract_features(samples, rate, bands)
            feature_time = time.perf_counter() - step
            log(f'[cubase_local] 使用 {device} FP32 运行三类节拍网络')
            step = time.perf_counter()
            probabilities = network.predict(features) if device == 'numpy' else network.predict_torch(features, device=device)
            probabilities = _probabilities(probabilities, frames)
            network_time = time.perf_counter() - step
        provenance.update(audio_source_sha256=source_hash, feature_provenance=feature_provenance,
                          network_backend=device, backend_self_test=backend_test,
                          precision=PRECISION, worker_observation_identity=identity)
        if _hash(source) != source_hash: raise ValueError('音频在分析期间发生变化，已停止')
        observation_file, observation_hash = _write_observations(folder, identity, probabilities, provenance)
    decode_identity = dict(decoder_version=DECODER_VERSION, observation_key=observation_key,
                           observations_sha256=observation_hash, parameters=decoder)
    decode_key = _key(decode_identity); decode_file = output / 'decodes' / f'{decode_key}.json'
    decode_hit = False; step = time.perf_counter()
    try:
        cached = json.loads(decode_file.read_text(encoding='utf-8'))
        if cached['identity'] != decode_identity or _key(cached['result']) != cached['result_sha256']:
            raise ValueError('解码缓存身份或内容损坏')
        result = cached['result']; decode_hit = True
        log('[cubase_local] 复用当前参数的节拍解码')
    except (OSError, ValueError, KeyError, TypeError):
        log('[cubase_local] 从三类观测解码节拍、强拍与拍号候选')
        result = decode_observations(probabilities, provenance=provenance, **decoder)
        _atomic_json(decode_file, dict(identity=decode_identity, result=result, result_sha256=_key(result)))
    if _hash(source) != source_hash: raise ValueError('音频在分析期间发生变化，已丢弃候选')
    result = json.loads(_json(result))
    result.update(provider='cubase_local_reconstruction', source=str(source), source_path=str(source),
                  source_sha256=source_hash, audio_duration=info.frames / 44100,
                  clock_origin_seconds=origin, first_full_bar_seconds=None,
                  effective_map_published=False, source_events_modified=False,
                  observations_path=str(observation_file.resolve()), observations_sha256=observation_hash,
                  cache=dict(observation_key=observation_key, decode_key=decode_key,
                             observations_hit=observation_hit, decode_hit=decode_hit),
                  timings=dict(features_and_model_load=feature_time, network=network_time,
                               decode=time.perf_counter() - step, total=time.perf_counter() - started))
    result['absolute_beat_seconds'] = [s + origin for s in result['beat_seconds']]
    result['absolute_downbeat_seconds'] = [s + origin for s in result['downbeat_seconds']]
    result['beats'] = list(result['beat_seconds']); result['downbeats'] = list(result['downbeat_seconds'])
    return result
