"""CPU-only mature beat/bar decoding in the isolated NumPy 1.x environment."""
import hashlib
import io
import json
import math
from pathlib import Path
import sys


DECODER_VERSION = 1
SOURCE_COMMIT = '27f032e8947204902c675e5e341a3faf5dc86dae'


def _constrained_bar_decode(beat, down, identity, digest, constraints):
    """Hard state constraints over real observations on an existing pulse grid.

    This deliberately cannot move pulses or repair their count. Ordinary map
    pins are not accepted as downbeats; the caller owns map/region preservation.
    """
    import numpy as np
    from madmom.features.downbeats import DBNBarTrackingProcessor
    from madmom.ml.hmm import ObservationModel, HiddenMarkovModel

    allowed_keys={'version','kind','start_seconds','end_seconds','beat_seconds',
                  'boundary_states','downbeats'}
    if (not isinstance(constraints,dict) or set(constraints)-allowed_keys or
            constraints.get('version')!=1 or constraints.get('kind')!='bar_phase'):
        raise ValueError('仅支持 version=1 的 bar_phase 局部约束；固定速度锚点不能冒充强拍。')

    def finite(value):
        if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value):
            raise ValueError('约束时间必须是有限数值。')
        return float(value)

    raw_seconds=constraints.get('beat_seconds',[])
    if not isinstance(raw_seconds,list):
        raise ValueError('局部拍点必须是秒位置数组。')
    seconds=[finite(t) for t in raw_seconds]
    if (not 2<=len(seconds)<=4097 or seconds[0]<0 or
            any(b<=a for a,b in zip(seconds,seconds[1:]))):
        raise ValueError('局部拍点须为 2–4097 个严格递增的真实秒位置（最多 4096 个间隔）。')
    start,end=finite(constraints.get('start_seconds')),finite(constraints.get('end_seconds'))
    if abs(start-seconds[0])>1e-7 or abs(end-seconds[-1])>1e-7:
        raise ValueError('范围两端必须精确位于提供的当前网格拍点。')
    boundaries=constraints.get('boundary_states',{})
    if not isinstance(boundaries,dict) or set(boundaries)!={'start','end'}:
        raise ValueError('必须提供两个边界的现有拍位与拍号。')
    explicit=constraints.get('downbeats')
    if not isinstance(explicit,list) or not explicit:
        raise ValueError('至少需要一个用户明确指定的强拍；固定锚点不是强拍证据。')

    fps=finite(identity.get('fps'))
    if fps<=0:raise ValueError('激活缓存的帧率无效。')
    # Check float bounds before casting, including huge finite user input.
    if seconds[-1]>(len(down)-.5)/fps:
        raise ValueError('局部拍点超出激活缓存。')
    frames=np.rint(np.asarray(seconds)*fps)
    if not np.isfinite(frames).all() or frames[0]<0 or frames[-1]>=len(down) or np.any(np.diff(frames)<=0):
        raise ValueError('局部拍点超出激活缓存，或多个拍点落在同一分析帧。')
    indices=frames.astype(np.int64)
    probabilities=down[indices].copy()
    parameters={'beats_per_bar':[3,4],'observation_weight':100,'meter_change_prob':.001,
                'sampling':'nearest_frame','fps':fps}
    processor=DBNBarTrackingProcessor(beats_per_bar=[3,4],observation_weight=100,meter_change_prob=.001)
    state_meters=np.asarray(processor.beats_per_bar)[processor.st.state_patterns]
    state_numbers=processor.st.state_positions.astype(int)+1
    allowed=np.ones((len(seconds),processor.st.num_states),dtype=bool)
    evidence=[]

    def constrain(index,number,meter,source):
        if type(number) is not int or not 1<=number<=4 or (meter is not None and
                (type(meter) is not int or meter not in (3,4) or number>meter)):
            raise ValueError('局部强拍解码当前只支持 3/4、4/4 的有效边界拍位。')
        mask=state_numbers==number
        if meter is not None:mask &= state_meters==meter
        allowed[index] &= mask
        if not allowed[index].any():
            raise ValueError('用户强拍与边界拍位／拍号约束冲突。')
        evidence.append({'index':index,'seconds':seconds[index],'source':source,
                         'beat_number':number,'meter':meter})

    for key,index in (('start',0),('end',len(seconds)-1)):
        row=boundaries[key]
        if not isinstance(row,dict) or set(row)!={'beat_number','meter'}:
            raise ValueError('边界需要明确的 beat_number 与 meter。')
        if row['meter'] is None:raise ValueError('边界拍号不能待定。')
        constrain(index,row['beat_number'],row['meter'],'boundary_'+key)
    for row in explicit:
        if not isinstance(row,dict) or set(row)-{'seconds','meter'}:
            raise ValueError('强拍约束只接受 seconds 与可选的 meter。')
        at=finite(row.get('seconds'))
        match=[i for i,t in enumerate(seconds) if abs(t-at)<=1e-7]
        if len(match)!=1:
            raise ValueError('用户强拍必须精确落在当前网格拍点；不会自动移动或吸附。')
        constrain(match[0],1,row.get('meter'),'explicit_user_downbeat')

    class MaskedObservation(ObservationModel):
        def __init__(self):
            super().__init__(np.arange(processor.st.num_states,dtype=np.uint32))

        def log_densities(self, observations):
            # Copy the mature model's densities, retaining every allowed value
            # exactly. -inf excludes states; it never boosts/fakes activations.
            values=processor.om.log_densities(observations)[:,processor.om.pointers].copy()
            values[~allowed]=-np.inf
            return np.ascontiguousarray(values,dtype=np.float64)

    observed=np.clip(probabilities,1e-7,1-1e-7)
    hmm=HiddenMarkovModel(processor.tm,MaskedObservation(),None)
    states,likelihood=hmm.viterbi(observed)
    if len(states)!=len(seconds) or not np.isfinite(likelihood):
        raise ValueError('固定边界与指定强拍之间不存在合法 3/4、4/4 路径；请扩大范围或核对边界。')
    if not allowed[np.arange(len(seconds)),states].all():
        raise RuntimeError('解码结果违反强拍约束。')
    _,unconstrained=processor.hmm.viterbi(observed)
    warnings=[]
    if any(probabilities[e['index']]<.5 for e in evidence if e['source']=='explicit_user_downbeat'):
        warnings.append('部分指定强拍的原始声学证据较弱；候选遵循人工约束，不代表模型已确认。')
    return {'decoder':'constrained_bar_dbn','decoder_version':DECODER_VERSION,
            'constraint_decoder_version':1,'source_commit':SOURCE_COMMIT,
            'start_seconds':start,'end_seconds':end,'parameters':parameters,
            'activation_identity':identity,'activation_sha256':digest,
            'beat_seconds':seconds,'beat_numbers':state_numbers[states].tolist(),
            'meters':state_meters[states].tolist(),'downbeat_probability':probabilities.tolist(),
            'beat_probability':beat[indices].tolist(),'log_likelihood':float(likelihood),
            'unconstrained_log_likelihood':float(unconstrained),'warnings':warnings,
            'constraint_diagnostics':{'constraints':evidence,
                'allowed_states':[np.flatnonzero(row).tolist() for row in allowed],
                'observation_frame_indices':indices.tolist(),
                'probabilities_modified':False,'pulse_seconds_changed':False,
                'log_domain_probability_clip':[1e-7,1-1e-7],
                'last_pulse_extrapolated':False,'scope':'bar_phase_only',
                'outside_range':'not_decoded_or_modified','fixed_pins_are_downbeats':False}}


def decode(path, constraints=None):
    import numpy as np
    from scipy.special import expit
    from madmom.features.beats import DBNBeatTrackingProcessor
    from madmom.features.downbeats import DBNBarTrackingProcessor

    path=Path(path)
    raw=path.read_bytes()
    with np.load(io.BytesIO(raw),allow_pickle=False) as saved:
        identity=json.loads(str(saved['identity'].item()))
        beat=expit(np.asarray(saved['beat'],dtype=np.float64).reshape(-1))
        down=expit(np.asarray(saved['downbeat'],dtype=np.float64).reshape(-1))
    if (len(beat)!=len(down) or not len(beat) or
            not np.isfinite(beat).all() or not np.isfinite(down).all()):
        raise ValueError('Invalid cached beat activations')
    fps=float(identity['fps'])
    digest=hashlib.sha256(raw).hexdigest()
    if constraints is not None:
        if isinstance(constraints,dict) and constraints.get('kind')=='pulse_times':
            from .constrained_pulse_decode import decode_pulse_times
            return decode_pulse_times(beat,down,identity,digest,constraints)
        return _constrained_bar_decode(beat,down,identity,digest,constraints)
    # Detect pulse without imposing a meter. The subsequent bar HMM can switch
    # meter within a song, unlike DBNDownBeatTrackingProcessor's global choice.
    parameters={'fps':fps,'min_bpm':30,'max_bpm':300,'num_tempi':60,
                'transition_lambda':100,'beats_per_bar':[3,4],
                'observation_weight':100,'meter_change_prob':.001,
                'sampling':'nearest_frame'}
    beats=DBNBeatTrackingProcessor(**{k:parameters[k] for k in
        ('fps','min_bpm','max_bpm','num_tempi','transition_lambda')})(beat)
    if len(beats)<3:raise ValueError('Too few decoded beats')
    indices=np.clip(np.rint(beats*fps).astype(int),0,len(down)-1)
    probabilities=np.clip(down[indices],1e-7,1-1e-7)
    processor=DBNBarTrackingProcessor(beats_per_bar=[3,4],observation_weight=100,meter_change_prob=.001)
    decoded=processor(np.column_stack((beats,probabilities)))
    states,likelihood=processor.hmm.viterbi(probabilities[:-1])
    meters=np.asarray(processor.beats_per_bar)[processor.st.state_patterns[states]]
    meters=np.append(meters,meters[-1])
    from .pulse_diagnostics import diagnose_pulse_path
    pulse_diagnostics=diagnose_pulse_path(beats,beat[indices],activations=beat,fps=fps)
    result={'decoder':'beat_dbn_then_bar_dbn','decoder_version':DECODER_VERSION,
            'source_commit':SOURCE_COMMIT,'parameters':parameters,
            'activation_identity':identity,'activation_sha256':digest,
            'beat_seconds':beats.tolist(),'beat_numbers':decoded[:,1].astype(int).tolist(),
            'meters':meters.astype(int).tolist(),'downbeat_probability':probabilities.tolist(),
            'beat_probability':beat[indices].tolist(),'log_likelihood':float(likelihood),
            'pulse_diagnostics':pulse_diagnostics}
    return result


if __name__=='__main__':
    constraints=json.loads(Path(sys.argv[3]).read_text(encoding='utf-8')) if len(sys.argv)>3 else None
    result=decode(sys.argv[1],constraints=constraints)
    Path(sys.argv[2]).write_text(json.dumps(result,ensure_ascii=False,allow_nan=False),encoding='utf-8')
