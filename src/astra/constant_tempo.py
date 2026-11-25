"""Explicit constant-tempo fitting, never automatic flattening of variable songs."""
import numpy as np
from .timing import TempoMap


def assess_bar_phase(beats, downbeats, bpm, phase, numerator):
    """Find sustained whole-quarter slips hidden by wrapped pulse residuals.

    Missing pulse detections do not change a real downbeat's grid residue, and
    missing downbeats may span an integer number of complete bars. A new phase
    needs at least three complete bars; isolated false accents are insufficient.
    This is a guard for flattening, not an automatic beat or meter correction.
    """
    period=60/float(bpm)
    values=np.asarray(beats,dtype=float)
    down=np.unique(np.asarray(downbeats,dtype=float))
    runs=[];current=None
    for second in down:
        quarter=(second-phase)/period+1
        integer=int(round(quarter))
        if abs(quarter-integer)>.15:
            current=None
            continue
        residue=integer%int(numerator)
        delta=integer-current['last_quarter'] if current else 0
        if current is None or residue!=current['phase'] or delta<=0 or delta%numerator:
            current={'phase':residue,'first_seconds':float(second),'last_seconds':float(second),
                     'first_quarter':integer,'last_quarter':integer,'downbeat_count':1}
            runs.append(current)
        else:
            current['last_seconds']=float(second)
            current['last_quarter']=integer
            current['downbeat_count']+=1
    supported=[run for run in runs if run['downbeat_count']>=4]
    changes=[]
    for before,after in zip(supported,supported[1:]):
        if before['phase']==after['phase']:
            continue
        local=values[(values>=before['last_seconds']-1e-7)&(values<=after['first_seconds']+1e-7)]
        gaps=np.diff(local)
        # Integer-period gaps alone can be detector omissions. Several
        # non-integer gaps provide evidence of a genuine local tempo change;
        # otherwise the source of the phase change remains ambiguous.
        fractional=[float(gap) for gap in gaps if abs(gap/period-round(gap/period))>.12]
        changes.append({'start_seconds':before['last_seconds'],'end_seconds':after['first_seconds'],
            'from_phase':before['phase'],'to_phase':after['phase'],
            'shift_quarters_mod_bar':(after['phase']-before['phase'])%numerator,
            'non_integer_pulse_gaps_seconds':fractional,
            'evidence':'local_tempo_change' if len(fractional)>=2 else 'whole_beat_pause_or_downbeat_phase_ambiguity'})
    return {'method':'supported_bar_phase_v1','status':'phase_jump' if changes else 'no_supported_phase_jump',
            'numerator':int(numerator),'supported_runs':supported,'transitions':changes}


def estimate_constant_tempo(beats, downbeats, duration):
    values=np.asarray(beats,dtype=float)
    if len(values)<32 or not np.isfinite(values).all() or np.any(np.diff(values)<=0) or values[-1]-values[0]<20:
        raise ValueError('恒速估算至少需要 20 秒和 32 个有效节拍；请直接填写 BPM 或导入速度图。')
    period=float(np.median(np.diff(values)))
    center=60/period
    low,high=max(30.,center*.85),min(300.,center*1.15)
    def strength(bpm):return abs(np.exp(2j*np.pi*values*bpm/60).mean())
    coarse=np.arange(low,high,.02)
    guess=float(max(coarse,key=strength))
    fine=np.arange(guess-.025,guess+.025,.0005)
    bpm=float(max(fine,key=strength))
    if abs(bpm-round(bpm))<.005 and strength(round(bpm))>=strength(bpm)-.002:
        bpm=float(round(bpm))
    coherence=float(strength(bpm));period=60/bpm
    phase=float(np.angle(np.exp(2j*np.pi*values/period).mean())/(2*np.pi)%1*period)
    residual=(values-phase+period/2)%period-period/2
    p95=float(np.percentile(abs(residual),95))
    segments=np.array_split(residual,4)
    drift=max(float(np.median(x)) for x in segments)-min(float(np.median(x)) for x in segments)
    down=np.asarray(downbeats,dtype=float)
    down=down[np.isfinite(down)]
    indices=np.rint((down-phase)/period).astype(int)+1
    diffs=np.diff(indices)
    candidates=[n for n in (2,3,4,5,6,7) if np.count_nonzero(diffs==n)>0]
    numerator=max(candidates,key=lambda n:(np.count_nonzero(diffs==n),n==4)) if candidates else 4
    phase_review=assess_bar_phase(values,down,bpm,phase,numerator)
    if phase_review['transitions']:
        transition=phase_review['transitions'][0]
        reason='存在局部渐慢或变速证据' if transition['evidence']=='local_tempo_change' else '整拍停顿或强拍位置尚不明确'
        raise ValueError(f'全曲不符合可靠的恒速网格：{transition["start_seconds"]:.2f}–'
                         f'{transition["end_seconds"]:.2f} 秒前后出现持续小节相位跳变，{reason}。'
                         '单拍相位吻合不能证明小节对齐；请保留变速图并修正局部节拍。')
    if coherence<.9 or p95>min(.05,period*.12) or drift>.035:
        raise ValueError('全曲不符合可靠的恒速网格。可能存在变速或节拍误检，请保留变速图或手动填写已知 BPM。')
    votes=np.bincount(indices%numerator,minlength=numerator) if len(indices) else np.array([1]+[0]*(numerator-1))
    pickup=int(votes.argmax())
    # Same grid shifted by one integer beat avoids an extreme initial tempo.
    if phase<period*.1:
        # Moving the beat-1 anchor later by one period subtracts one from all
        # subsequent beat coordinates, including the downbeat phase.
        phase+=period;pickup=(pickup-1)%numerator
    end=max(float(duration),float(values[-1])+period)
    result={'version':1,'source':'constant_fit','anchors':[{'seconds':0.,'beat':0.},
        {'seconds':phase,'beat':1.},{'seconds':end,'beat':1+(end-phase)/period}],
        'time_signatures':[{'beat':0.,'numerator':int(numerator),'denominator':4}],
        'pickup_beats':pickup,'grid_origin':1.,'lead_in_alignment':True,
        'constant_bpm':bpm,'fit':{'coherence':coherence,'p95_seconds':p95,'segment_phase_drift_seconds':drift,
                                'bar_phase_review':phase_review},
        'warnings':['恒定 BPM 由全曲节拍相位拟合；首个短前导段只用于保持音频起点，小节起点仍建议试听核对。']}
    TempoMap(result)
    return result
