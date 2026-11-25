"""Optional, revision-bound acoustic suggestion for one explicitly chosen point.

Only the confirmed candidate can move a map point. Events and fixed pins never
move; this is neither a whole-song decoder nor automatic downbeat detection.
"""
from bisect import bisect_right
from copy import deepcopy
import math
from pathlib import Path

from . import editing
from .acoustic_snap import suggest_acoustic_snap
from .active_pulse_evidence import resolve
from .tempo_edit import apply_tempo_operation
from .timing import TempoMap


def prepare(project, base_revision, request):
    doc = editing.current(project)
    editing._check_base(doc, base_revision)
    tempo, pins = deepcopy(doc['tempo']), []
    clock = TempoMap(tempo)
    source, target = request.get('seconds'), request.get('to_seconds')
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
           for v in (source, target)):
        raise ValueError('请选择已有映射点并输入有效目标秒数。')
    index = next((i for i, t in enumerate(clock.seconds) if abs(t-source)<1e-8), None)
    if index is None or index in (0, len(clock.seconds)-1):
        raise ValueError('请选择音频内部的已有映射点；端点不能吸附。')
    # Keep the selected point's music address and neighbours. Searching across
    # either neighbour would change ordering rather than refine this point.
    bounds = (clock.seconds[index-1]+1e-7, clock.seconds[index+1]-1e-7)
    if not bounds[0] <= target <= bounds[1]:
        raise ValueError('目标时间必须位于选中映射点的相邻点之间。')
    operation = {'type':'move_point', 'seconds':source, 'to_seconds':target}
    apply_tempo_operation(tempo, operation, pins)  # validate manual target first
    segment = max(0, min(len(clock.seconds)-2, bisect_right(clock.seconds, target)-1))
    bpm = 60*(clock.beats[segment+1]-clock.beats[segment])/(clock.seconds[segment+1]-clock.seconds[segment])
    return {'project':str(Path(project).resolve()), 'base_revision':base_revision,
            'tempo':tempo, 'fixed_anchors':pins, 'source':source, 'target':target,
            'bpm':bpm, 'bounds':bounds}


def build_candidate(prepared, suggestion, metadata=None):
    if not suggestion.get('accepted'):
        raise ValueError('附近音频拍点证据不足，未移动映射点；可取消声学吸附后按输入时间预览。')
    target = suggestion.get('candidate_seconds')
    if (isinstance(target, bool) or not isinstance(target, (int, float)) or
            not math.isfinite(target) or not prepared['bounds'][0] <= target <= prepared['bounds'][1]):
        raise ValueError('声学候选超出相邻映射点边界，未应用。')
    if abs(target-prepared['source'])<1e-8:
        raise ValueError('附近最强候选就是当前映射点，无需移动；也可取消声学吸附后手工指定。')
    analysis = dict(metadata or {}, suggestion=deepcopy(suggestion))
    operation = {'type':'tempo', 'action':'move_point', 'seconds':prepared['source'],
                 'to_seconds':target, 'analysis':analysis}
    result = apply_tempo_operation(prepared['tempo'], dict(operation, type='move_point'),
                                   prepared['fixed_anchors'])
    result['operation'] = operation
    result['summary'] = (f"声学建议：输入 {prepared['target']:.3f} 秒 → {target:.3f} 秒"
                         f"（{(target-prepared['target'])*1000:+.1f} 毫秒）。确认后只移动所选映射点；"
                         '原始演奏与 CC64 秒位置不变。')
    if (metadata or {}).get('provider') == 'cubase_local_reconstruction':
        result['warnings'].append('复用生效拍点图的同源三类网络观测；这是局部声学候选，不自动判断小节第一拍，也不会移动固定锚点。')
    else:
        result['warnings'].append('复用 Beat This 节拍概率，借鉴局部加权吸附；不是 Cubase 神经模型输出，也不判断这里是否为小节第一拍。')
    return result


def run(prepared):
    evidence = resolve(prepared['project'], prepared['tempo'], load_probabilities=True)
    probabilities = (evidence['observations'] if evidence['observations'] is not None
                     else evidence['beat_probability'])
    suggestion = suggest_acoustic_snap(probabilities, prepared['target'], prepared['bpm'],
        fps=evidence['fps'], source=evidence['probability_source'], bounds=prepared['bounds'])
    return build_candidate(prepared, suggestion,
        {'activation_sha256':evidence['activation_sha256'],
         'activation_identity':evidence['activation_identity'], 'provider':evidence['provider'],
         'evidence_ref':evidence['evidence_ref'], 'result_sha256':evidence['result_sha256'],
         'method':'local_acoustic_snap_v2'})
