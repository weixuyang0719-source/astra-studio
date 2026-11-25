"""Count-constrained mature beat DBN. No event edits or project persistence.

Repeat madmom's existing tempo/phase state space in count layers; its original
transition/observation probabilities are unchanged. Only crossing a beat
boundary increments count. Hard user constraints exclude states, never invent
high acoustic probabilities. The mature C Viterbi performs the actual search.
"""
import math

PULSE_CONSTRAINT_VERSION = 1
MAX_TRELLIS_CELLS = 30_000_000  # madmom stores uint32 back pointers (~120 MB).


def decode_pulse_times(beat, down, identity, digest, constraints):
    import numpy as np
    from madmom.features.beats_hmm import BeatStateSpace, BeatTransitionModel, RNNBeatTrackingObservationModel
    from madmom.ml.hmm import TransitionModel, ObservationModel, HiddenMarkovModel

    keys={'version','kind','start_seconds','end_seconds','interval_count','anchors','min_bpm','max_bpm'}
    if not isinstance(constraints,dict) or set(constraints)-keys or constraints.get('version')!=1 or constraints.get('kind')!='pulse_times':
        raise ValueError('需要 version=1、kind=pulse_times 的局部拍点约束。')
    def finite(value):
        if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value):
            raise ValueError('拍点约束必须为有限数值。')
        return float(value)
    start,end=finite(constraints.get('start_seconds')),finite(constraints.get('end_seconds'))
    count=constraints.get('interval_count')
    if type(count) is not int or not 1<=count<=64:
        raise ValueError('请明确指定 1–64 个四分音符间隔。')
    if not 0<=start<end or end-start>60:
        raise ValueError('局部拍点复核限 60 秒内的正长度选区。')
    fps=finite(identity.get('fps'))
    if not 1<=fps<=200:raise ValueError('激活帧率不在支持范围。')
    low=finite(constraints.get('min_bpm',30));high=finite(constraints.get('max_bpm',300))
    if not 20<=low<high<=400:raise ValueError('速度搜索范围需满足 20 ≤ 下限 < 上限 ≤ 400。')
    if end>(len(beat)-.5)/fps:raise ValueError('选区超出激活缓存。')
    first,last=int(round(start*fps)),int(round(end*fps))
    if first<0 or last>=len(beat) or first>=last:raise ValueError('选区不足两个有效激活帧。')
    points={0:start,count:end}
    supplied=constraints.get('anchors',[])
    if not isinstance(supplied,list):raise ValueError('内部确认拍点必须为数组。')
    for row in supplied:
        if not isinstance(row,dict) or set(row)!={'seconds','beat_index'}:
            raise ValueError('内部确认拍点需要 seconds 与 beat_index。')
        index=row['beat_index'];at=finite(row['seconds'])
        if type(index) is not int or not 0<=index<=count or not start<=at<=end:
            raise ValueError('确认拍点或拍序超出选区。')
        if index in points and abs(points[index]-at)>1e-9:
            raise ValueError('相同拍序有冲突的固定时间。')
        points[index]=at
    ordered=sorted(points.items())
    frames={index:int(round(at*fps))-first for index,at in ordered}
    if any(b<=a for a,b in zip(frames.values(),list(frames.values())[1:])):
        raise ValueError('固定拍点的时间／拍序倒退，或落在同一激活帧。')

    st=BeatStateSpace(60*fps/high,60*fps/low,num_intervals=24)
    min_gap,max_gap=int(st.intervals.min()),int(st.intervals.max())
    for (i,_),(j,_) in zip(ordered,ordered[1:]):
        length=frames[j]-frames[i]
        if not (j-i)*min_gap<=length<=(j-i)*max_gap:
            raise ValueError('指定拍数与固定时间不满足速度搜索范围。')
    layers=count+2  # layer 0 supplies the pre-first-frame transition only.
    size=st.num_states;total=size*layers;length=last-first+1
    cells=total*length
    if cells>MAX_TRELLIS_CELLS:
        raise ValueError('局部解码超出内存预算，请缩短选区或收窄速度搜索范围。')
    observed=np.asarray(beat[first:last+1],dtype=float)
    if not np.isfinite(observed).all() or np.max(observed)<.1 or np.ptp(observed)<.05:
        raise ValueError('选区缺少可区分的拍点声学证据，未生成强行均分的候选。')
    tm=BeatTransitionModel(st,transition_lambda=100)
    om=RNNBeatTrackingObservationModel(st,observation_lambda=16)
    # Direct sparse-to-edge expansion preserves every mature transition value.
    destinations=np.repeat(np.arange(size,dtype=np.uint32),np.diff(tm.pointers))
    sources=tm.states
    crosses=np.isin(destinations,st.first_states)
    dst=[];src=[];prob=[]
    for layer in range(layers):
        next_layer=np.minimum(layer+crosses.astype(int),layers-1)
        dst.append(destinations+next_layer*size)
        src.append(sources+layer*size)
        prob.append(tm.probabilities)
    expanded=TransitionModel.from_dense(np.concatenate(dst),np.concatenate(src),np.concatenate(prob))
    category=np.where(om.pointers==1,2,0).astype(np.uint32)
    category[st.first_states]=1

    class CountedObservation(ObservationModel):
        def __init__(self):
            super().__init__(np.concatenate([category+layer*3 for layer in range(layers)]).astype(np.uint32))

        def log_densities(self, observations):
            original=om.log_densities(observations)
            # Only 3 density columns per layer, not a frames x states matrix.
            values=np.tile(original[:,[0,1,1]],(1,layers))
            values[:,:3]=-np.inf
            values[:,(layers-1)*3:]=-np.inf
            for index,frame in frames.items():
                values[frame,:]=-np.inf
                values[frame,(index+1)*3+1]=original[frame,1]
            return np.ascontiguousarray(values,dtype=np.float64)

    initial=np.zeros(total,dtype=float)
    initial[st.last_states]=1./len(st.last_states)
    hmm=HiddenMarkovModel(expanded,CountedObservation(),initial)
    path,likelihood=hmm.viterbi(np.clip(observed,1e-7,1-1e-7))
    if len(path)!=length or not np.isfinite(likelihood):
        raise ValueError('固定拍点、总拍数和速度范围之间没有合法路径；未修改工程。')
    pulse_frames=np.flatnonzero(np.isin(path%size,st.first_states))
    if len(pulse_frames)!=count+1 or pulse_frames[0]!=0 or pulse_frames[-1]!=length-1:
        raise RuntimeError('计数路径没有覆盖精确的两端及指定拍数。')
    if not np.array_equal(path[pulse_frames]//size,np.arange(1,count+2)):
        raise RuntimeError('计数路径发生拍序跳转。')
    seconds=((pulse_frames+first)/fps).tolist()
    for index,at in points.items():
        if pulse_frames[index]!=frames[index]:raise RuntimeError('解码违反固定拍点。')
        seconds[index]=at  # Retain exact user seconds, not the nearest frame.
    if any(b<=a for a,b in zip(seconds,seconds[1:])):raise ValueError('帧离散化与固定秒位置冲突。')
    probabilities=np.asarray(beat)[pulse_frames+first].tolist()
    weak=[i for i,p in enumerate(probabilities) if p<.2]
    warnings=['候选按指定拍数约束重选拍点；拍号不由此操作改变，需试听预览后应用。']
    if weak:warnings.append(f'{len(weak)} 个拍点的声学证据较弱，可能为约束补足；请核对，不能视为模型确认。')
    return {'decoder':'constrained_pulse_dbn','constraint_decoder_version':PULSE_CONSTRAINT_VERSION,
        'start_seconds':start,'end_seconds':end,'interval_count':count,'beat_seconds':seconds,
        'beat_probability':probabilities,'downbeat_probability':np.asarray(down)[pulse_frames+first].tolist(),
        'activation_identity':identity,'activation_sha256':digest,'log_likelihood':float(likelihood),
        'warnings':warnings,'parameters':{'min_bpm':low,'max_bpm':high,'fps':fps,
            'num_tempi_requested':24,'num_tempi':len(st.intervals),'interval_frames':st.intervals.tolist(),
            'transition_lambda':100,'observation_lambda':16},
        'constraint_diagnostics':{'scope':'local_pulse_times','probabilities_modified':False,
            'log_domain_probability_clip':[1e-7,1-1e-7], 'interval_count_confirmed_by_user':True,
            'fixed_points':[{'beat_index':i,'seconds':at,'sampled_seconds':round(at*fps)/fps,
                             'sampling_error_seconds':round(at*fps)/fps-at} for i,at in ordered],
            'observation_frame_indices':(pulse_frames+first).tolist(),'weak_pulse_indices':weak,
            'trellis_cells':cells,'traceback_bytes':cells*4,'outside_range':'not_decoded_or_modified',
            'meter_inferred':False,'source_events_modified':False}}
