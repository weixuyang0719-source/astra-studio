"""Revision-bound acoustic pulse retiming, separate from meter regrouping."""
from copy import deepcopy
import math
from pathlib import Path

from . import editing
from .bar_phase_edit import _activation_file
from .tempo_edit import apply_tempo_operation
from .timing import TempoMap

EPS = 1e-7


def prepare(project, base_revision, request):
    doc = editing.current(project)
    editing._check_base(doc, base_revision)
    clock = TempoMap(deepcopy(doc['tempo']))
    from .active_pulse_evidence import is_native
    native=is_native(doc['tempo'])
    max_seconds,max_count=(20,24) if native else (60,64)
    start, end = request.get('start'), request.get('end')
    if any(isinstance(t, bool) or not isinstance(t, (int, float)) or not math.isfinite(t)
           for t in (start, end)):
        raise ValueError('请选择有效的音频范围。')
    if not 0 <= start < end <= doc['duration'] or end-start > max_seconds:
        raise ValueError(f'缓存拍点复核每次支持音频内至多 {max_seconds} 秒的选区。')
    count = request.get('interval_count')
    if type(count) is not int or not 1 <= count <= max_count:
        raise ValueError(f'请确认范围内 1–{max_count} 个四分音符间隔；拍点数比间隔数多一。')
    if native:
        from .counted_pulse_decode import MAX_WORK
        frames=round(end*100)-round(start*100)+1
        if count*frames*181*181>MAX_WORK:
            raise ValueError('选区与拍数超出逐帧重找的计算预算，请缩短选区或分段修正；不会退回粗粒度拍点。')
    constraints = {'version':1, 'kind':'pulse_times', 'start_seconds':float(start),
                   'end_seconds':float(end), 'interval_count':count, 'anchors':[]}
    return {'project':str(Path(project).resolve()), 'base_revision':base_revision,
            'tempo':deepcopy(doc['tempo']), 'fixed_anchors':[],
            'constraints':constraints}


def build_candidate(prepared, decoded):
    constraints = prepared['constraints']
    seconds = decoded.get('beat_seconds')
    if (decoded.get('decoder') not in ('constrained_pulse_dbn', 'constrained_pulse_dense') or
            decoded.get('interval_count') != constraints['interval_count'] or
            not isinstance(seconds, list) or len(seconds) != constraints['interval_count']+1):
        raise ValueError('拍点解码未满足确认拍数，候选未应用。')
    if any(isinstance(t, bool) or not isinstance(t, (int,float)) or not math.isfinite(t) for t in seconds):
        raise ValueError('拍点解码返回无效时间，候选未应用。')
    for point in constraints['anchors']:
        if abs(seconds[point['beat_index']]-point['seconds']) > EPS:
            raise ValueError('拍点候选改变了锁定锚点，候选未应用。')
    metadata = {k:deepcopy(decoded.get(k)) for k in ('decoder','constraint_decoder_version',
        'activation_sha256','activation_identity','log_likelihood','constraint_diagnostics',
        'probability_source','manual_constraint_decoder','parameters','raw_beat_seconds',
        'acoustic_refinement','log_likelihood_scope','decode_purpose')}
    operation = {'type':'tempo', 'action':'pulse_times',
                 'start':constraints['start_seconds'], 'end':constraints['end_seconds'],
                 'beat_seconds':deepcopy(seconds), 'analysis':metadata}
    payload = dict(operation,type='pulse_times');payload.pop('action')
    result = apply_tempo_operation(prepared['tempo'], payload, prepared['fixed_anchors'])
    result['operation'] = operation
    result['warnings'] = list(decoded.get('warnings', [])) + result['warnings']
    return result


def run(prepared):
    from .active_pulse_evidence import is_native, resolve
    if is_native(prepared['tempo']):
        from .manual_pulse_decode import decode_evidence
        evidence=resolve(prepared['project'],tempo=prepared['tempo'],load_probabilities=True)
        return build_candidate(prepared,decode_evidence(evidence,prepared['constraints'],pulse_strategy='dense_counted'))
    from .rhythm_decode import decode_cached_activations
    path, digest, identity = _activation_file(prepared['project'])
    decoded = decode_cached_activations(path, constraints=prepared['constraints'])
    if decoded.get('activation_sha256') != digest or decoded.get('activation_identity') != identity:
        raise ValueError('解码期间激活缓存已变化，请重新预览。')
    return build_candidate(prepared, decoded)
