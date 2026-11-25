"""Resolve the effective map's original acoustic evidence, never its edited clock.

Native evidence is pinned to one complete worker result and its content hash.
Missing native evidence is an error, not permission to use a legacy cache.
Original audio files need not remain online: the committed project source hash
is the identity boundary. This module does not claim to reverify audio bytes.
"""
from copy import deepcopy
import hashlib
import io
import json
import math
from pathlib import Path
import re

NATIVE = 'cubase_local_reconstruction'
VERSION = 1


def is_native(tempo):
    """Manual timing edits retain the original provider provenance."""
    return (tempo.get('source') == NATIVE or bool(tempo.get('pulse_source'))
            or (tempo.get('adopted_candidate') or {}).get('provider') == NATIVE
            or (tempo.get('pulse_evidence') or {}).get('provider') == NATIVE)


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _digest(value):
    return isinstance(value, str) and re.fullmatch('[a-f0-9]{64}', value) is not None


def _path(root, value):
    path = Path(value)
    path = (root / path).resolve() if not path.is_absolute() else path.resolve()
    if not path.is_relative_to(root):
        raise ValueError('拍点证据不在当前工程目录内。')
    return path


def _read(path, dependencies, limit=16 * 1024 * 1024):
    dependencies.append(path)
    if path.stat().st_size > limit:
        raise ValueError('拍点证据超过读取上限。')
    raw = path.read_bytes()
    if len(raw) > limit:
        raise ValueError('拍点证据超过读取上限。')
    return json.loads(raw), raw


def _positions(values):
    if (not isinstance(values, list) or not 1 <= len(values) <= 200000
        or any(isinstance(v, bool) or not isinstance(v, (int, float))
               or not math.isfinite(v) or v < 0 for v in values)
        or any(b <= a for a, b in zip(values, values[1:]))):
        raise ValueError('原始检测拍秒位置无效。')
    return list(values)


def _current_tempo(root, state, dependencies):
    index_path = root / 'editing' / 'current.json'; dependencies.append(index_path)
    if index_path.exists():
        index, _ = _read(index_path, dependencies)
        revision = index['revision']
        if not isinstance(revision, str) or not re.fullmatch('[A-Za-z0-9_-]{1,128}', revision):
            raise ValueError('编辑版本身份无效。')
        doc, _ = _read(root / 'editing' / 'revisions' / f'{revision}.json', dependencies)
        return doc['tempo']
    latest = state.get('latest_export')
    if latest:
        snapshot = root / 'exports' / Path(latest).name / 'tempo_map.json'
        dependencies.append(snapshot)
        if snapshot.is_file():
            return _read(snapshot, dependencies)[0]
    path = root / 'tempo_map.json'; dependencies.append(path)
    return _read(path, dependencies)[0] if path.is_file() else {}


def resolve(project, tempo=None, *, load_probabilities=False, dependencies=None):
    """Return source-bound pulse seconds and optionally original probabilities.

    ``dependencies`` is an optional list populated for a GUI metadata cache.
    Probabilities are arrays, never JSON serialized or turned into new pulses.
    Native: observations Nx3, beat_probability=1-nonbeat, downbeat_probability
    =class1. Legacy: observations=None, independent sigmoid logits; those two
    channels must not be treated as a normalized native observation vector.
    """
    deps = dependencies if dependencies is not None else []
    root = Path(project).resolve(); state, _ = _read(root / 'project.json', deps)
    tempo = _current_tempo(root, state, deps) if tempo is None else tempo
    ref = tempo.get('pulse_evidence')
    native = is_native(tempo)
    key = (state.get('analysis_keys') or {}).get('beat_this')
    valid_key = isinstance(key, str) and re.fullmatch('[A-Za-z0-9_-]{1,128}', key)
    if native:
        if not isinstance(ref, dict):
            raise ValueError('此本机拍点图尚未绑定原始检测证据；不会改用旧 Beat This 缓存。')
        if (ref.get('version') != VERSION or ref.get('provider') != NATIVE
                or not _digest(ref.get('project_source_sha256'))
                or ref['project_source_sha256'] != state.get('source_sha256')):
            raise ValueError('本机拍点证据与当前项目源身份不符。')
        path = _path(root, ref['result_path']); result, raw = _read(path, deps)
        if _sha(raw) != ref.get('result_sha256'):
            raise ValueError('原始拍点结果缓存已变化，请重新核对候选。')
        source = tempo.get('pulse_source') or {}
        if (result.get('provider') != NATIVE
                or not _digest(ref.get('normalized_sha256'))
                or result.get('source_sha256') != ref['normalized_sha256']
                or source.get('audio_sha256') != ref['normalized_sha256']
                or result.get('observations_sha256') != ref.get('observations_sha256')
                or not _digest(ref.get('observations_sha256'))
                or result.get('cache', {}).get('decode_key') != ref.get('decode_key')
                or not _digest(ref.get('decode_key'))
                or source.get('decoder') != result.get('decoder')
                or source.get('decoder_version') != result.get('version')
                or source.get('parameters') != result.get('parameters')
                or source.get('observation_provenance') != result.get('observation_provenance')
                or result.get('clock_origin_seconds', 0) != 0):
            raise ValueError('生效图与原始拍点、观测或解码身份不一致。')
        original = source.get('original_source_sha256')
        if original is not None and original != state.get('source_sha256'):
            raise ValueError('映射绑定了另一个项目源。')
        seconds = _positions(result.get('beat_seconds'))
        down = result.get('downbeat_seconds', [])
        if down and (not isinstance(down, list) or not set(_positions(down)).issubset(seconds)):
            raise ValueError('强拍不属于原始拍点序列。')
        value = dict(provider=NATIVE, source='cubase_local_cache', seconds=seconds,
            downbeat_seconds=down, cache_key=ref['decode_key'], result=result,
            evidence_ref=deepcopy(ref), result_path=str(path), result_sha256=_sha(raw),
            original_audio_bytes_reverified=False)
        if load_probabilities:
            _native_probabilities(root, result, value, deps)
        return value
    if ref is not None:
        raise ValueError('未知的拍点证据来源，不会自动切换模型。')
    if state.get('source_kind') == 'midi' or not valid_key:
        raise ValueError('此工程没有原始音频检测拍缓存。')
    path = root / 'cache' / f'beats-{key}.json'; result, raw = _read(path, deps)
    if result.get('provider') == NATIVE or result.get('decoder') == 'cubase_dynamic_hmm_reconstruction':
        raise ValueError('本机检测结果缺少生效图来源绑定，不能按旧模型读取。')
    value = dict(provider='beat_this', source='beat_this_cache',
        seconds=_positions(result.get('beats')), downbeat_seconds=result.get('downbeats', []),
        cache_key=key, result=result, result_path=str(path), result_sha256=_sha(raw),
        evidence_ref=None, legacy_identity='project_analysis_key', original_audio_bytes_reverified=False)
    if load_probabilities:
        _legacy_probabilities(root, result, value, deps)
    return value


def _native_probabilities(root, result, value, deps):
    from . import cubase_local_worker as w
    provenance = result['observation_provenance']
    identity = provenance.get('worker_observation_identity')
    if not isinstance(identity, dict) or identity.get('source_sha256') != result['source_sha256']:
        raise ValueError('缺少与原始音频绑定的正式观测身份。')
    path = _path(root, result['observations_path'])
    deps.extend([path, path.parent / 'manifest.json'])
    if path.stat().st_size > 128 * 1024 * 1024:
        raise ValueError('观测缓存超过读取上限。')
    count = identity.get('sample_frames')
    if type(count) is not int or count <= 0 or identity.get('sample_rate') != 44100:
        raise ValueError('观测源采样身份无效。')
    if abs(result.get('audio_duration', -1) - count / 44100) > 1e-8:
        raise ValueError('观测时长与原曲不符。')
    key = w._key(identity)
    if path.parent.name != key or result.get('cache', {}).get('observation_key') != key:
        raise ValueError('观测缓存目录身份不符。')
    frames, _ = w._frame_count(count)
    observations, verified, actual, digest = w._read_observations(path.parent, identity, frames)
    if actual.resolve() != path or digest != result['observations_sha256'] or verified != provenance:
        raise ValueError('观测概率与原始拍点结果不符。')
    value.update(observations=observations, beat_probability=1.-observations[:, 2],
        downbeat_probability=observations[:, 1], fps=100., probability_source='auf_takt_three_class',
        activation_path=str(path), activation_sha256=digest, activation_identity=identity)


def _legacy_probabilities(root, result, value, deps):
    import numpy as np
    from scipy.special import expit
    evidence = result.get('bar_result') or {}
    identity = evidence.get('activation_identity'); expected = evidence.get('activation_sha256')
    if not _digest(expected) or not isinstance(identity, dict):
        raise ValueError('历史节拍结果缺少激活身份，不能混用其它模型缓存。')
    path = root / 'cache' / 'beat' / 'activations.npz'; deps.append(path)
    if path.stat().st_size > 128 * 1024 * 1024:
        raise ValueError('节拍激活缓存超过读取上限。')
    raw = path.read_bytes()
    if _sha(raw) != expected:
        raise ValueError('节拍缓存已变化，请重新预览。')
    with np.load(io.BytesIO(raw), allow_pickle=False) as saved:
        if json.loads(str(saved['identity'].item())) != identity:
            raise ValueError('节拍缓存身份不一致。')
        beat = np.asarray(saved['beat'], dtype=float).reshape(-1)
        down = np.asarray(saved['downbeat'], dtype=float).reshape(-1) if 'downbeat' in saved else None
    fps = identity.get('fps')
    if (not len(beat) or not np.isfinite(beat).all() or isinstance(fps, bool)
            or not isinstance(fps, (int, float)) or not math.isfinite(fps) or fps <= 0
            or (down is not None and (len(down) != len(beat) or not np.isfinite(down).all()))):
        raise ValueError('节拍概率缓存无效。')
    value.update(observations=None, beat_probability=expit(beat),
        downbeat_probability=expit(down) if down is not None else None,
        fps=float(fps), probability_source='beat_this', activation_path=str(path),
        activation_sha256=expected, activation_identity=identity)
