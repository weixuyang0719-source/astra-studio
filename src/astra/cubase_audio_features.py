"""Static-audit-based feature conventions; not native-capture equivalence.

No installed-component parameters are embedded here. The caller supplies the
three band matrices returned by ``load_installed_network``. Only the audited
44.1 kHz path is implemented; NumPy FFT/cosine rounding can differ from native.
"""
from __future__ import annotations

import numpy as np

_SCALES = ((1024, 22, 1), (2048, 46, 1), (4096, 92, 2))
_HOP = 441
_CHUNK_FRAMES = 128


def _interior_hann(length: int) -> np.ndarray:
    phase = np.arange(length, dtype=np.float32)
    phase += np.float32(1)
    phase *= np.float32(6.2831854820251465)
    phase /= np.float32(length + 1)
    np.cos(phase, out=phase)
    np.subtract(np.float32(1), phase, out=phase)
    phase *= np.float32(0.5)
    return phase


def _frame_count(length: int) -> tuple[int, int]:
    residual = (length + 157) % _HOP
    return 1 + (length + residual) // _HOP, residual


def extract_features(samples, sample_rate, bands) -> tuple[np.ndarray, dict]:
    """Return float32 ``[frames,320]`` and JSON-compatible provenance.

    ``samples`` is floating-point [samples] or [samples,channels]. All channels
    are averaged in order. Inputs and band matrices are never modified. FFTs
    are processed in bounded batches; output and mono audio remain in memory.
    Empty input follows the audited flush arithmetic and produces one zero row.
    """
    if isinstance(sample_rate, (bool, np.bool_)) or sample_rate != 44100:
        raise ValueError("仅支持已核验的 44100 Hz 特征路径；请先明确重采样，不能视为原生等价")
    audio = np.asarray(samples)
    if (audio.ndim not in (1, 2) or not np.issubdtype(audio.dtype, np.floating)
            or (audio.ndim == 2 and audio.shape[1] == 0)):
        raise ValueError("音频必须是浮点 [采样] 或 [采样,声道] 数组")
    if not np.isfinite(audio).all():
        raise ValueError("音频含非有限值")
    matrices = list(bands)
    if len(matrices) != 3:
        raise ValueError("需要三个已核验形状的频带矩阵")
    for index, (length, count, _) in enumerate(_SCALES):
        matrix = np.asarray(matrices[index])
        if (matrix.shape != (length // 2 + 1, count)
                or not np.issubdtype(matrix.dtype, np.number)
                or np.iscomplexobj(matrix) or not np.isfinite(matrix).all()
                or (matrix < 0).any()):
            raise ValueError("频带矩阵形状或数值无效")
        matrices[index] = matrix.astype(np.float32, copy=False)
        if not np.isfinite(matrices[index]).all():
            raise ValueError("频带矩阵超出 float32 范围")

    channels = 1 if audio.ndim == 1 else audio.shape[1]
    mono = np.array(audio if audio.ndim == 1 else audio[:, 0], dtype=np.float32, copy=True)
    with np.errstate(over="ignore", invalid="ignore"):
        for channel in range(1, channels):
            mono += np.asarray(audio[:, channel], dtype=np.float32)
        mono *= np.float32(1 / channels)
    if not np.isfinite(mono).all():
        raise ValueError("音频混合后超出 float32 范围")

    frame_count, residual = _frame_count(len(mono))
    features = np.zeros((frame_count, 320), dtype=np.float32)
    column = 0
    for (length, count, lag), weights in zip(_SCALES, matrices):
        window = _interior_hann(length)
        offsets = np.arange(length, dtype=np.int64) - length // 2
        log_bands = features[:, column:column + count]
        for first in range(0, frame_count, _CHUNK_FRAMES):
            stop = min(first + _CHUNK_FRAMES, frame_count)
            indices = np.arange(first, stop, dtype=np.int64)[:, None] * _HOP + offsets
            valid = (indices >= 0) & (indices < len(mono))
            frames = np.zeros(indices.shape, dtype=np.float32)
            frames[valid] = mono[indices[valid]]
            frames *= window
            # NumPy's transform arithmetic is not the original FFT kernel.
            spectrum = np.fft.rfft(frames, axis=1, norm="backward").astype(np.complex64)
            magnitude = np.abs(spectrum)
            values = magnitude @ weights
            values += np.float32(1)
            np.log(values, out=values)
            values *= np.float32(0.4342944920063019)
            if not np.isfinite(values).all():
                raise ValueError("频谱特征溢出，拒绝传入网络")
            log_bands[first:stop] = values
        difference = features[:, column + count:column + 2 * count]
        if frame_count > lag:
            np.subtract(log_bands[lag:], log_bands[:-lag], out=difference[lag:])
            np.maximum(difference[lag:], np.float32(0), out=difference[lag:])
        column += 2 * count

    provenance = {
        "kind": "independent_audio_features",
        "sample_rate": 44100,
        "input_samples": len(mono),
        "input_channels": channels,
        "channel_mix": "float32_sequential_sum_then_divide",
        "frame_count": frame_count,
        "feature_width": 320,
        "hop_samples": _HOP,
        "frame0_center_sample": 0,
        "frame_center_formula": "j*441",
        "padding": "zero_outside_input",
        "window": "interior_hann_N_plus_2_drop_endpoints_float32",
        "window_lengths": [1024, 2048, 4096],
        "fft": "numpy_rfft_unnormalized_cast_complex64_magnitude",
        "fft_normalization": "backward_no_forward_division_no_one_sided_doubling",
        "compression": "float32_log(1+x)*0.4342944920063019",
        "feature_order": "log22,diff22,log46,diff46,log92,diff92",
        "difference_lags": [1, 1, 2],
        "difference_initial_history": "zero_vector",
        "fifo_initial_zeros": 157,
        "warmup_chunks": 4,
        "flush_residual_samples": residual,
        "flush_padding_samples": residual + 2048,
        "flush_frame_formula": "1+(L+((L+157)%441))//441",
        "flush_source": "static_RVA_28cea0_28dc40_28fd90",
        "fft_batch_frames": _CHUNK_FRAMES,
        "audio_feature_equivalence": False,
        "native_feature_capture_verified": False,
        "host_equivalence": False,
        "limitation": "Numerical approximation; no original native feature capture comparison.",
    }
    return features, provenance
