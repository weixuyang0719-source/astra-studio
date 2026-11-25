"""Bounded boundary-compressed counted beat HMM for explicit local constraints.

Same 16-phase observation window and normalized exponential transitions as
constrained_pulse_decode; deterministic within-period states are summed exactly.
Dense integer periods remove coarse-grid closure artifacts. This remains a
local proposal, with no network, meter inference, event editing or publication.
"""
import math
import numpy as np

MAX_FRAMES = 2001
MAX_INTERVALS = 24
MAX_WORK = 220_000_000

DECODER = 'constrained_pulse_dense'
VERSION = 1


def decode_boundary_path(probability, count, *, periods, anchors=None,
                         transition_lambda=100., observation_lambda=16):
    """Return local integer pulse frames and the unchanged counted-HMM score.

    This kernel also accepts the old 24-period grid for equivalence auditing.
    A final boundary includes its transition and its beat emission, exactly as
    the phase-state HMM does. Initial prior is uniform over preceding periods.
    """
    p = np.asarray(probability, dtype=float)
    periods = np.asarray(periods)
    if (p.ndim != 1 or not 2 <= len(p) <= MAX_FRAMES or
            not np.isfinite(p).all() or np.any(p < 0) or np.any(p > 1)):
        raise ValueError('Invalid or oversized local probability range')
    if type(count) is not int or not 1 <= count <= MAX_INTERVALS:
        raise ValueError('Local boundary decoder supports 1 to 24 intervals')
    if (periods.ndim != 1 or not len(periods) or len(periods) > 181 or
            periods.dtype.kind not in 'iu' or np.any(periods < 1) or
            np.any(np.diff(periods) <= 0)):
        raise ValueError('Periods must be a bounded increasing integer grid')
    if not math.isfinite(transition_lambda) or transition_lambda < 0 or observation_lambda != 16:
        raise ValueError('Invalid transition or observation parameters')
    n, width = len(p)-1, len(periods)
    if count * (n+1) * width * width > MAX_WORK:
        raise ValueError('Local boundary decoder exceeds work budget')
    pins = {0: 0, count: n}
    for index, frame in (anchors or {}).items():
        if type(index) is not int or type(frame) is not int or not 0 <= index <= count or not 0 <= frame <= n:
            raise ValueError('Invalid fixed boundary')
        if index in pins and pins[index] != frame:
            raise ValueError('Conflicting fixed boundary')
        pins[index] = frame
    ordered = sorted(pins.items())
    for (i, a), (j, b) in zip(ordered, ordered[1:]):
        if not (j-i)*periods[0] <= b-a <= (j-i)*periods[-1]:
            raise ValueError('Fixed boundaries and count have no legal period path')

    p = np.clip(p, 1e-7, 1-1e-7)
    nonbeat = np.log((1-p)/(observation_lambda-1))
    beat = np.log(p)
    prefix_nonbeat = np.r_[0., np.cumsum(nonbeat)]
    prefix_gain = np.r_[0., np.cumsum(beat-nonbeat)]
    ratio = periods[None, :]/periods[:, None]
    transition = np.exp(-transition_lambda*np.abs(ratio-1))
    transition[transition <= np.spacing(1)] = 0
    transition /= transition.sum(axis=1, keepdims=True)
    with np.errstate(divide='ignore'):
        transition = np.log(transition)
    initial = transition.max(axis=0)-math.log(width)
    final = transition.max(axis=1)+beat[-1]
    # BeatStateSpace uses positions arange(period)/period < 1/16.
    beat_width = (periods+observation_lambda-1)//observation_lambda
    score = np.full((n+1, width), -np.inf)
    back = np.full((count+1, n+1, width), -1, dtype=np.int16)
    columns = np.arange(width)
    for k in range(1, count+1):
        current = np.full_like(score, -np.inf)
        lo = max(k*int(periods[0]), n-(count-k)*int(periods[-1]))
        hi = min(k*int(periods[-1]), n-(count-k)*int(periods[0]))
        times = [pins[k]] if k in pins else range(lo, hi+1)
        for end in times:
            starts = end-periods
            valid = (starts >= 0) & (starts <= n)
            idx = columns[valid]
            start = starts[valid]
            if k == 1:
                incoming = np.where(start == 0, initial[idx], -np.inf)
                predecessors = np.full(len(idx), -1)
            else:
                candidates = score[start, :]+transition[:, idx].T
                predecessors = np.argmax(candidates, axis=1)
                incoming = candidates[np.arange(len(idx)), predecessors]
            emission = (prefix_nonbeat[end]-prefix_nonbeat[start] +
                prefix_gain[start+beat_width[idx]]-prefix_gain[start])
            current[end, idx] = incoming+emission
            back[k, end, idx] = predecessors
        score = current
    final_scores = score[n]+final
    column = int(np.argmax(final_scores))
    likelihood = float(final_scores[column])
    if not math.isfinite(likelihood):
        raise ValueError('Fixed boundaries and count have no legal period path')
    frames = [n]
    end = n
    for k in range(count, 0, -1):
        previous = int(back[k, end, column])
        end -= int(periods[column])
        frames.append(end)
        column = previous
    frames.reverse()
    assert frames[0] == 0 and all(frames[k] == v for k, v in pins.items())
    return dict(frame_indices=frames, log_likelihood=likelihood,
        parameters=dict(interval_frames=periods.tolist(), transition_lambda=transition_lambda,
            observation_lambda=observation_lambda, transition_normalization='row_sum',
            probability_clip=[1e-7, 1-1e-7]),
        traceback_bytes=back.nbytes, work_upper_bound=count*(n+1)*width*width)


def decode_pulse_times(beat, down, identity, digest, constraints):
    """Explicit user-count proposal; not an automatic acceptance mechanism.

    Every 10 ms period from 30 to 300 BPM is represented. Limits are checked
    before search; exceeding them must never silently select a coarser grid.
    The quality-review adapter may use the same kernel for an unconfirmed
    current-count hypothesis; it must correct these manual-purpose diagnostics
    before returning a result and pass the independent unchanged quality gate.
    """
    keys = {'version', 'kind', 'start_seconds', 'end_seconds', 'interval_count', 'anchors'}
    if (not isinstance(constraints, dict) or set(constraints)-keys or
            constraints.get('version') != 1 or constraints.get('kind') != 'pulse_times'):
        raise ValueError('逐帧拍点需要明确的局部拍数和范围约束。')

    def finite(value):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError('拍点约束必须为有限数值。')
        return float(value)

    fps = finite(identity.get('fps'))
    if fps != 100.:
        raise ValueError('逐帧局部解码仅支持当前本机拍点的 100 Hz 观测。')
    beat, down = np.asarray(beat, dtype=float), np.asarray(down, dtype=float)
    if (beat.ndim != 1 or down.shape != beat.shape or not len(beat) or
            not np.isfinite(beat).all() or not np.isfinite(down).all() or
            np.any(beat < 0) or np.any(beat > 1) or np.any(down < 0) or np.any(down > 1)):
        raise ValueError('无效的原始拍点概率。')
    start, end = finite(constraints.get('start_seconds')), finite(constraints.get('end_seconds'))
    count = constraints.get('interval_count')
    if type(count) is not int or not 1 <= count <= MAX_INTERVALS:
        raise ValueError('逐帧重找每次支持 1–24 个拍间隔，请缩短选区。')
    if not 0 <= start < end or end-start > 20:
        raise ValueError('逐帧重找每次支持至多 20 秒，请缩短选区。')
    first, last = round(start*fps), round(end*fps)
    if not 0 <= first < last < len(beat):
        raise ValueError('选区超出拍点观测缓存，或不足两个观测帧。')
    fixed = {0: start, count: end}
    anchors = constraints.get('anchors', [])
    if not isinstance(anchors, list):
        raise ValueError('内部确认拍点必须为数组。')
    for anchor in anchors:
        if not isinstance(anchor, dict) or set(anchor) != {'seconds', 'beat_index'}:
            raise ValueError('内部确认拍点需要 seconds 与 beat_index。')
        index, at = anchor['beat_index'], finite(anchor['seconds'])
        if type(index) is not int or not 0 <= index <= count or not start <= at <= end:
            raise ValueError('确认拍点或拍序超出选区。')
        if index in fixed and abs(fixed[index]-at) > 1e-9:
            raise ValueError('相同拍序有冲突的固定时间。')
        # A duplicate within tolerance must not replace an exact saved endpoint.
        fixed.setdefault(index, at)
    ordered = sorted(fixed.items())
    frames = {k: round(v*fps)-first for k, v in ordered}
    if any(b <= a for a, b in zip(frames.values(), list(frames.values())[1:])):
        raise ValueError('固定拍点的时间／拍序倒退，或落在同一观测帧。')
    observed = beat[first:last+1]
    if np.max(observed) < .1 or np.ptp(observed) < .05:
        raise ValueError('选区缺少可区分的拍点声学证据，未生成强行均分的候选。')
    try:
        result = decode_boundary_path(observed, count, periods=np.arange(20, 201), anchors=frames)
    except ValueError as exc:
        if 'budget' in str(exc) or 'oversized' in str(exc):
            raise ValueError('逐帧重找超出本次计算预算，请缩短选区；不会退回粗粒度拍点。') from exc
        if 'no legal period path' in str(exc):
            raise ValueError('指定拍数和固定时间无法在 30–300 BPM 内闭合，请核对范围和拍数。') from exc
        raise
    indices = np.asarray(result['frame_indices'])+first
    seconds = (indices/fps).tolist()
    for index, at in fixed.items():
        seconds[index] = at
    if any(b <= a for a, b in zip(seconds, seconds[1:])):
        raise ValueError('帧离散化与固定秒位置冲突。')
    probabilities = beat[indices].tolist()
    weak = [i for i, value in enumerate(probabilities) if value < .2]
    warnings = ['按已确认拍数逐帧重找拍点；拍号不由此推断，请试听预览后再应用。']
    if weak:
        warnings.append(f'{len(weak)} 个拍点声学证据较弱，可能由拍数约束补足，需人工核对。')
    return dict(decoder=DECODER, constraint_decoder_version=VERSION,
        start_seconds=start, end_seconds=end, interval_count=count, beat_seconds=seconds,
        beat_probability=probabilities, downbeat_probability=down[indices].tolist(),
        activation_identity=identity, activation_sha256=digest, log_likelihood=result['log_likelihood'],
        parameters=dict(result['parameters'], fps=fps, min_bpm=30, max_bpm=300,
                        period_grid='every_integer_frame', num_tempi=181),
        warnings=warnings,
        constraint_diagnostics=dict(scope='local_pulse_times', probabilities_modified=False,
            interval_count_confirmed_by_user=True, requires_explicit_adoption=True,
            fixed_points=[dict(beat_index=i, seconds=at, sampled_seconds=round(at*fps)/fps,
                              sampling_error_seconds=round(at*fps)/fps-at) for i, at in ordered],
            observation_frame_indices=indices.tolist(), weak_pulse_indices=weak,
            traceback_bytes=result['traceback_bytes'], work_upper_bound=result['work_upper_bound'],
            outside_range='not_decoded_or_modified', meter_inferred=False, source_events_modified=False))


