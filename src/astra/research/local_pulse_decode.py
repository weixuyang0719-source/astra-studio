"""Private research adapter; production shares only the validated counted kernel."""
import numpy as np
from ..counted_pulse_decode import (
    MAX_FRAMES, MAX_INTERVALS, MAX_WORK, decode_boundary_path,
)


def decode_evidence(evidence, constraints):
    """Private local proposal adapter; caller must run the shared review gate."""
    if evidence.get('probability_source') != 'auf_takt_three_class':
        raise ValueError('Dense local proposal requires verified native evidence')
    fps = float(evidence['fps'])
    start, end = float(constraints['start_seconds']), float(constraints['end_seconds'])
    first, last = round(start*fps), round(end*fps)
    if fps != 100. or not 0 <= first < last < len(evidence['beat_probability']):
        raise ValueError('Invalid local native observation range')
    fixed = {0: start, constraints['interval_count']: end}
    for anchor in constraints.get('anchors', []):
        index, at = anchor['beat_index'], anchor['seconds']
        if index in fixed and fixed[index] != at:
            raise ValueError('Conflicting fixed boundary')
        fixed[index] = at
    result = decode_boundary_path(evidence['beat_probability'][first:last+1], constraints['interval_count'],
        periods=np.arange(20, 201), anchors={k: round(v*fps)-first for k, v in fixed.items()})
    indices = np.array(result['frame_indices'])+first
    seconds = (indices/fps).tolist()
    for index, at in fixed.items():
        seconds[index] = at
    return dict(decoder='boundary_compressed_counted_hmm', version=1,
        beat_seconds=seconds, beat_probability=np.asarray(evidence['beat_probability'])[indices].tolist(),
        downbeat_probability=np.asarray(evidence['downbeat_probability'])[indices].tolist(),
        activation_sha256=evidence['activation_sha256'], activation_identity=evidence['activation_identity'],
        probability_source=evidence['probability_source'], log_likelihood=result['log_likelihood'],
        parameters=result['parameters'], map_published=False, source_events_modified=False,
        constraint_diagnostics=dict(observation_frame_indices=indices.tolist(),
            traceback_bytes=result['traceback_bytes'], work_upper_bound=result['work_upper_bound'],
            fixed_points=[{'beat_index':k,'seconds':v} for k,v in sorted(fixed.items())],
            endpoint_semantics='working_boundaries_not_independent_musical_confirmation',
            meter_inferred=False, probabilities_modified=False))
