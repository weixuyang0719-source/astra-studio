"""Independent reconstruction of the traced Cubase/AUFTAKT dynamic HMM.

Accepts real three-class observations, NOT audio and NOT Beat This channels.
No madmom, DLL execution, weights, host tempo postprocessing, or note retiming.
This is a numerical reconstruction, not yet verified binary-equivalent output.
"""
from __future__ import annotations

import math
import numpy as np

COMPONENT_SHA256='77f3fa1d1a15362dba8244e155788a997d0393c284d613d9633d11ca4d9a452d'


def period_candidates(min_bpm=60., max_bpm=240., hop_ms=10.):
    """27a3e0: log-BPM grid -> integer periods, adjacent duplicates removed."""
    if not (0<min_bpm<=max_bpm and hop_ms>0) or not all(map(math.isfinite,(min_bpm,max_bpm,hop_ms))):
        raise ValueError('Invalid tempo range or hop')
    count=min(math.floor(max_bpm-min_bpm+.5),50)
    slow=math.ceil(60000/(hop_ms*min_bpm));fast=math.floor(60000/(hop_ms*max_bpm))
    if fast<1 or slow<fast:raise ValueError('Tempo has no positive integer period')
    if count<=1:return [slow]
    low=60000/(hop_ms*slow);high=60000/(hop_ms*fast)
    values=[]
    for i in range(count):
        bpm=10**(math.log10(low)+i/(count-1)*(math.log10(high)-math.log10(low)))
        period=math.floor(60000/(hop_ms*bpm)+.5)
        if not values or values[-1]!=period:values.append(period)
    return values


def build_state_space(periods, meters=(3,4), *, transition_lambda=180., meter_stay=1.):
    """2792d0 layout and 27cf70 predecessor weights (not row-normalized)."""
    if (not periods or len(set(periods))!=len(periods) or
        any(type(p) is not int or p<1 for p in periods) or
        not meters or len(set(meters))!=len(meters) or
        any(type(m) is not int or not 1<=m<=10 for m in meters)):
        raise ValueError('Unique positive periods and meters 1..10 required')
    if not math.isfinite(transition_lambda) or transition_lambda<0 or not 0<=meter_stay<=1:
        raise ValueError('Invalid transition parameters')
    # Reject before allocating period-length Python arrays, including callers
    # supplying an explicit period grid or an unusually slow BPM lower bound.
    if sum(meters)*sum(periods)>65535:
        raise ValueError('State budget exceeds 65535')
    # Native predecessor maps are sorted by meter then rate (ascending).
    periods=sorted(periods,reverse=True);meters=sorted(meters)
    starts={};ends={};state_period=[];state_meter=[];state_number=[];within=[]
    for m in meters:
        for b in range(m):
            starts[m,b]=[];ends[m,b]=[]
            for p in periods:
                starts[m,b].append(len(within))
                state_period.extend([p]*p);state_meter.extend([m]*p)
                state_number.extend([b+1]*p);within.extend(range(p))
                ends[m,b].append(len(within)-1)
    count=len(within)
    if count>65535:raise ValueError('State budget exceeds 65535')
    state_period=np.asarray(state_period,dtype=np.int32)
    state_meter=np.asarray(state_meter,dtype=np.int16)
    state_number=np.asarray(state_number,dtype=np.int16)
    within=np.asarray(within,dtype=np.int32)
    classes=np.where(within==0,np.where(state_number==1,1,0),2).astype(np.int8)
    rates=np.float32(1)/np.asarray(periods,dtype=np.float32)
    ratio=(rates[:,None]/rates[None,:]).astype(np.float32)
    weights=np.exp(-np.float32(transition_lambda)*np.abs(ratio-np.float32(1))).astype(np.float32)
    groups=[]
    for m in meters:
        for b in range(m):
            target=np.asarray(starts[m,b],dtype=np.int32)
            if b:
                previous=np.asarray(ends[m,b-1],dtype=np.int32);w=weights.copy()
            else:
                previous=np.asarray([s for old in meters for s in ends[old,old-1]],dtype=np.int32)
                w=np.concatenate([weights*np.float32(meter_stay if old==m else 1-meter_stay)
                                  for old in meters],axis=0)
            groups.append(dict(target=target,previous=previous,weights=w))
    boundary=np.flatnonzero(within==0)
    slots=np.full(count,-1,dtype=np.int32);slots[boundary]=np.arange(len(boundary))
    return dict(periods=periods,meters=meters,period=state_period,meter=state_meter,
        beat_number=state_number,within=within,classes=classes,groups=groups,
        boundary=boundary,boundary_slot=slots,num_states=count)


def decode_observations(observations, *, provenance, hop_ms=10., min_bpm=60., max_bpm=240.,
                        meters=(3,4), transition_lambda=180., meter_stay=1.,
                        known_downbeats=(), periods_override=None, max_frames=120000):
    """Three classes [ordinary beat, downbeat, nonbeat] -> joint frame path.

    Every prior state starts with weight 1. Every frame maximizes predecessor
    weight, multiplies the matching observation, then L1-scales float32 values.
    Only boundary states need stored backpointers; deterministic inner states
    have predecessor state-1. Constraints are explicit masks, not fake evidence.
    """
    if not isinstance(provenance,dict) or provenance.get('kind') not in ('synthetic','native_three_class_capture','independent_network'):
        raise ValueError('Explicit observation provenance required; independent beat/downbeat are not three classes')
    if provenance['kind']=='native_three_class_capture' and (provenance.get('component_sha256')!=COMPONENT_SHA256
            or not provenance.get('capture_method') or not provenance.get('source_sha256')):
        raise ValueError('Native capture requires component/source hashes and capture method')
    original=np.asarray(observations)
    if (original.ndim!=2 or original.shape[1]!=3 or not len(original) or len(original)>max_frames or
        not np.isfinite(original).all() or np.any(original<0) or np.any(original>1) or
        not np.allclose(original.sum(axis=1),1.,rtol=0,atol=1e-5)):
        raise ValueError('Expected finite normalized N x 3 observations within frame budget')
    if not math.isfinite(hop_ms) or hop_ms<=0:raise ValueError('Invalid hop')
    if not isinstance(known_downbeats,(list,tuple)) or len(known_downbeats)>256:
        raise ValueError('At most 256 explicit known downbeats supported')
    obs=original.astype(np.float32,copy=True)
    # 27c1ca..27c1da and 27c5b9: only nonbeat receives 1/(40-1).
    obs[:,2]*=np.float32(1)/np.float32(39)
    space=build_state_space(period_candidates(min_bpm,max_bpm,hop_ms) if periods_override is None
                            else list(periods_override),meters,
                            transition_lambda=transition_lambda,meter_stay=meter_stay)
    frames=len(obs);n=space['num_states'];nb=len(space['boundary'])
    if frames*nb>32_000_000:raise ValueError('Boundary traceback memory budget exceeded (64 MB)')
    masks={};constraints=[]
    for row in known_downbeats:
        seconds=float(row['seconds'])
        if not math.isfinite(seconds):raise ValueError('Known downbeat outside observations')
        frame=math.floor(seconds*1000/hop_ms+.5)
        if not 0<=frame<frames:raise ValueError('Known downbeat outside observations')
        mask=space['classes']==1
        if row.get('meter') is not None:
            if row['meter'] not in space['meters']:raise ValueError('Known meter outside state candidates')
            mask &= space['meter']==row['meter']
        if row.get('bpm') is not None:
            bpm=float(row['bpm']);delta=float(row.get('tolerance_bpm',0))
            if not math.isfinite(bpm) or not math.isfinite(delta) or bpm<=0 or delta<0:raise ValueError('Invalid known-downbeat tempo')
            values=60000/(hop_ms*space['period'])
            mask &= (values>=bpm-delta-1e-7)&(values<=bpm+delta+1e-7)
        masks[frame]=mask & masks.get(frame,np.ones(n,dtype=bool))
        if not masks[frame].any():raise ValueError('Known downbeat constraints conflict')
        constraints.append(dict(requested_seconds=seconds,frame=frame,frame_seconds=frame*hop_ms*.001,
                                rounding_seconds=frame*hop_ms*.001-seconds,snap_applied=False))
    previous=np.ones(n,dtype=np.float32)
    traceback=np.empty((frames,nb),dtype=np.uint16)
    deterministic=np.arange(n,dtype=np.int32)-1
    for frame in range(frames):
        current=previous[deterministic].copy()
        for group in space['groups']:
            products=previous[group['previous'],None]*group['weights']
            chosen=np.argmax(products,axis=0)
            current[group['target']]=products[chosen,np.arange(len(group['target']))]
            traceback[frame,space['boundary_slot'][group['target']]]=group['previous'][chosen]
        current*=obs[frame,space['classes']]
        if frame in masks:current[~masks[frame]]=0
        # 2942b0 sums absolute float32 entries in double; 2944a0 applies the
        # float32 reciprocal. NumPy's reduction order may differ by ULPs.
        total=np.float32(np.abs(current).sum(dtype=np.float64))
        if not np.isfinite(total) or total<=0:raise ValueError('No positive path under observations/constraints')
        current*=np.float32(1)/total
        previous=current
    state=int(np.argmax(previous));path=np.empty(frames,dtype=np.int32)
    for frame in range(frames-1,-1,-1):
        path[frame]=state
        slot=space['boundary_slot'][state]
        state=int(traceback[frame,slot]) if slot>=0 else state-1
    classes=space['classes'][path];pulse=np.flatnonzero(classes!=2);down=np.flatnonzero(classes==1)
    # Float32 arithmetic matches traced storage, not a post-hoc peak search.
    seconds=(pulse.astype(np.float32)*np.float32(hop_ms)*np.float32(.001)).astype(float)
    down_seconds=(down.astype(np.float32)*np.float32(hop_ms)*np.float32(.001)).astype(float)
    return dict(decoder='cubase_dynamic_hmm_reconstruction',version=1,
        observation_provenance=dict(provenance),native_audio_frontend_available=False,
        binary_equivalence_verified=False,beat_seconds=seconds.tolist(),downbeat_seconds=down_seconds.tolist(),
        beat_numbers=space['beat_number'][path[pulse]].astype(int).tolist(),
        meters=space['meter'][path[pulse]].astype(int).tolist(),frame_indices=pulse.tolist(),
        observation_class_order=['ordinary_beat','downbeat','nonbeat'],
        observations_at_pulses=original[pulse].astype(float).tolist(),
        downbeat_frame_indices=down.tolist(),state_path=path.tolist(),
        first_detected_downbeat_seconds=float(down_seconds[0]) if len(down) else None,
        first_full_bar_seconds=None, # musical introduction semantics are not established by a frame path alone
        constraints=constraints,source_events_modified=False,
        parameters=dict(hop_ms=hop_ms,min_bpm=min_bpm,max_bpm=max_bpm,periods=space['periods'],
            meters=space['meters'],transition_lambda=transition_lambda,meter_stay=meter_stay,
            observation_nonbeat_divisor=39,initial_state_weights='all_one',
            transition_normalization='none',frame_normalization='float32 reciprocal of L1 sum',
            traceback_bytes=int(traceback.nbytes),num_states=n),
        reconstruction_limits=['original neural observations not supplied by this module',
            'expf/logarithm/reduction ULP parity not established',
            'known-downbeat masks use the existing state set; full native nearest-state and constraint-map preparation not verified',
            'host first-complete-bar and head/tail signature postprocessing intentionally not applied'])
