"""Bounded, preview-only quality review on the active source's probabilities.

No new acoustic model or tempo engine: optional proposals reuse the existing
count-constrained decoder. Gates below are Astra review policy, not Cubase
parameters or musical truth. A proposed range is not an automatically fixed map.
"""
from copy import deepcopy
import math
import numpy as np
from .timing import TempoMap

VERSION = 3


def _probabilities(evidence):
    p=np.asarray(evidence['beat_probability'],dtype=float)
    fps=float(evidence['fps'])
    if p.ndim!=1 or not len(p) or not np.isfinite(p).all() or np.any(p<0) or np.any(p>1) or not math.isfinite(fps) or fps<=0:
        raise ValueError('Invalid source probabilities')
    return p,fps


def _sample(p,fps,seconds):
    indices=np.floor(np.asarray(seconds)*fps+.5).astype(int)
    if np.any(indices<0) or np.any(indices>=len(p)):
        raise ValueError('Review range exceeds source observation frames')
    return p[indices]


def quarter_points(tempo):
    """Sample musical quarters, not every detected half-quarter pulse."""
    clock=TempoMap(tempo)
    origin=float(tempo.get('first_full_bar_beat',tempo.get('grid_origin',0.)))
    first=math.ceil(clock.beats[0]-origin-1e-8)
    last=math.floor(clock.beats[-1]-origin+1e-8)
    if last-first>20000:raise ValueError('Too many quarter positions for review')
    return [(origin+i,clock.to_seconds(origin+i)) for i in range(first,last+1)]


def discover(evidence, tempo, *, max_ranges=3, scope=None):
    """Find a few weak multi-pulse runs with nearby stronger acoustic evidence."""
    if type(max_ranges) is not int or not 1<=max_ranges<=3:raise ValueError('Review supports at most three ranges')
    if scope is not None and (len(scope)!=2 or not all(math.isfinite(v) for v in scope) or not 0<=scope[0]<scope[1]):
        raise ValueError('Invalid review scope')
    p,fps=_probabilities(evidence);points=quarter_points(tempo)
    points=[v for v in points if 0<=round(v[1]*fps)<len(p)]
    # Scope discovery itself, not just its final cards: a long global weak run
    # must be searchable as a shorter local run with strong endpoints INSIDE
    # the user's selection. Never borrow an outside endpoint to fill a card.
    if scope is not None:
        points=[v for v in points if scope[0]<=v[1]<=scope[1]]
    if len(points)<5:return {'version':VERSION,'ranges':[],'automatic_correction':False}
    seconds=np.array([v[1] for v in points]);support=_sample(p,fps,seconds)
    weak=[]
    for i in range(1,len(points)-1):
        if support[i]>=.15:continue
        radius=min(.35,.45*min(seconds[i]-seconds[i-1],seconds[i+1]-seconds[i]))
        left=max(0,math.ceil((seconds[i]-radius)*fps));right=min(len(p)-1,math.floor((seconds[i]+radius)*fps))
        if left>right:continue
        frame=left+int(np.argmax(p[left:right+1]));shift=frame/fps-seconds[i]
        if p[frame]>=.45 and p[frame]-support[i]>=.3 and abs(shift)>=.025:
            weak.append({'index':i,'seconds':float(seconds[i]),'source_probability':float(support[i]),
                'nearby_peak_seconds':frame/fps,'nearby_probability':float(p[frame]),'delta_seconds':float(shift)})
    groups=[]
    for point in weak:
        if not groups or point['index']-groups[-1][-1]['index']>3:groups.append([])
        groups[-1].append(point)
    ranges=[]
    for group in groups:
        if len(group)<2:continue
        a,b=group[0]['index']-1,group[-1]['index']+1
        lower=max(0,a-4);upper=min(len(points)-1,b+4)
        while a>lower and support[a]<.5:a-=1
        while b<upper and support[b]<.5:b+=1
        if support[a]<.5 or support[b]<.5:continue
        if b-a<4:
            # The unchanged gate needs three supported INTERIOR pulses. A
            # three-interval card cannot possibly pass; borrow the smallest
            # nearby strong-endpoint context inside this selection instead.
            # Reuse the existing +/-four-quarter endpoint search bounds.
            contexts=[(left,right) for left in range(lower,a+1)
                for right in range(b,upper+1)
                if support[left]>=.5 and support[right]>=.5 and 4<=right-left<=24
                and seconds[right]-seconds[left]<=20]
            if not contexts:continue
            a,b=min(contexts,key=lambda pair:(pair[1]-pair[0],
                seconds[pair[1]]-seconds[pair[0]],pair[0]))
        if not 4<=b-a<=24 or seconds[b]-seconds[a]>20:continue
        if scope is not None and (seconds[a]<scope[0] or seconds[b]>scope[1]):continue
        ranges.append(dict(start_seconds=float(seconds[a]),end_seconds=float(seconds[b]),
            interval_count=b-a,count_confirmed=False,count_basis='current_quarter_clock_hypothesis',
            start_quarter=points[a][0],end_quarter=points[b][0],
            evidence=group,endpoint_support=[float(support[a]),float(support[b])],
            severity=float(sum(v['nearby_probability']-v['source_probability'] for v in group)),
            reason='multiple_weak_pulses_with_nearby_stronger_evidence'))
    # A bounded non-overlapping set prevents dozens of noisy review cards.
    chosen=[]
    for row in sorted(ranges,key=lambda v:-v['severity']):
        if any(row['start_seconds']<v['end_seconds'] and row['end_seconds']>v['start_seconds'] for v in chosen):continue
        chosen.append(row)
        if len(chosen)==max_ranges:break
    return dict(version=VERSION,ranges=sorted(chosen,key=lambda v:v['start_seconds']),
        automatic_correction=False,provider=evidence.get('provider'),
        activation_sha256=evidence.get('activation_sha256'),
        policy='Astra quality gates; strong peaks are not confirmed beats')


def _longest_true(values):
    best=run=0
    for value in values:
        run=run+1 if value else 0;best=max(best,run)
    return best


def evaluate(evidence, original_seconds, candidate_seconds, constraints):
    """Common-probability, continuity and exact closure gates; no path-score ranking."""
    p,fps=_probabilities(evidence)
    old=np.asarray(original_seconds,dtype=float);new=np.asarray(candidate_seconds,dtype=float)
    reason=[];count=constraints['interval_count']
    if len(new)!=count+1 or not np.isfinite(new).all() or np.any(np.diff(new)<=0):
        return {'accepted':False,'reasons':['invalid_candidate_count_or_order']}
    if abs(new[0]-constraints['start_seconds'])>1e-8 or abs(new[-1]-constraints['end_seconds'])>1e-8:
        reason.append('endpoint_not_exact')
    for pin in constraints.get('anchors',[]):
        if not 0<=pin['beat_index']<len(new) or abs(new[pin['beat_index']]-pin['seconds'])>1e-8:
            reason.append('confirmed_anchor_changed')
    before=_sample(p,fps,old);after=_sample(p,fps,new)
    gaps=np.diff(new);ratios=gaps[1:]/gaps[:-1]
    if np.any(ratios<.6) or np.any(ratios>1/.6):reason.append('abrupt_adjacent_period_change')
    strong_run=_longest_true(after[1:-1]>=.35)
    if strong_run<3:reason.append('insufficient_consecutive_supported_pulses')
    same_count=len(old)==len(new)
    comparison={'same_count':same_count,'old_source_probability':before.tolist(),
                'candidate_source_probability':after.tolist(),'supported_interior_run':strong_run,
                'interval_seconds':gaps.tolist()}
    if same_count:
        gain=after-before;displacement=abs(new-old)
        improved=(gain[1:-1]>=.2)&(displacement[1:-1]>=.025)
        comparison.update(mean_probability_gain=float(np.mean(gain[1:-1])),
            consecutive_improved_run=_longest_true(improved),displacement_seconds=(new-old).tolist())
        if _longest_true(improved)<2:reason.append('no_consecutive_multi_pulse_improvement')
        if np.mean(gain[1:-1])<.1:reason.append('insufficient_common_probability_gain')
        if np.any((before[1:-1]>=.6)&(after[1:-1]<.1)):reason.append('lost_previously_strong_pulse')
    else:
        # A changed confirmed count is a different hypothesis; don't manufacture
        # an index-aligned or raw-score comparison across those hypotheses.
        reason.append('changed_count_requires_direct_manual_review')
    return dict(accepted=not reason,reasons=reason,comparison=comparison,
                map_published=False,musical_truth_confirmed=False)


def refine(evidence, candidate_seconds, constraints):
    """One native acoustic pass, <=50 ms, fixed endpoints and fixed pins intact."""
    from .acoustic_snap import suggest_acoustic_snap
    points=list(candidate_seconds);original=list(points)
    if evidence.get('probability_source')!='auf_takt_three_class' or evidence.get('observations') is None:
        return {'beat_seconds':points,'changed':False,'suggestions':[],
                'reason':'native_three_class_observation_required'}
    pinned={0,len(points)-1,*[v['beat_index'] for v in constraints.get('anchors',[])]}
    suggestions=[]
    for i in range(1,len(points)-1):
        if i in pinned:continue
        # Use the unrefined neighbors for a deterministic single pass. Midpoints
        # ensure two independently snapped neighbors cannot exchange order.
        bounds=(max(original[i]-.05,(original[i-1]+original[i])/2),
                min(original[i]+.05,(original[i]+original[i+1])/2))
        bpm=120/(original[i+1]-original[i-1])
        suggestion=suggest_acoustic_snap(evidence['observations'],original[i],bpm,
            fps=evidence['fps'],source='auf_takt_three_class',bounds=bounds)
        suggestions.append({'beat_index':i,**suggestion})
        if suggestion['accepted']:points[i]=suggestion['candidate_seconds']
    p,fps=_probabilities(evidence)
    return dict(beat_seconds=points,changed=points!=original,suggestions=suggestions,
        original_seconds=original,max_displacement_seconds=.05,
        before_source_probability=_sample(p,fps,original).tolist(),
        after_source_probability=_sample(p,fps,points).tolist(),
        probability_source='auf_takt_three_class',requires_whole_range_review=True)


def propose(evidence, tempo, request, *, fixed_anchors=(), decode=None):
    """Reuse counted DP once. Returned suggestions never apply themselves.

    Endpoints are fixed even for an unconfirmed current-count hypothesis. A
    caller-supplied count is never silently changed. Explicit fixed interior
    seconds must have an unambiguous integer address relative to this range.
    """
    start=float(request['start_seconds']);end=float(request['end_seconds'])
    count=request['interval_count'];confirmed=request.get('count_confirmed',False)
    if type(count) is not int or not 3<=count<=24 or not 0<=start<end or end-start>20:
        raise ValueError('Local review requires 3–24 quarters and at most 20 seconds')
    if type(confirmed) is not bool:raise ValueError('Count confirmation must be explicit')
    clock=TempoMap(tempo);left=clock.to_beat(start);right=clock.to_beat(end)
    existing=right-left
    if abs(existing-round(existing))>1e-6:raise ValueError('Review endpoints must span complete current quarters')
    if not confirmed and count!=round(existing):raise ValueError('Unconfirmed review cannot change the current count')
    if start<clock.seconds[0] or end>clock.seconds[-1]:raise ValueError('Review outside active map')
    # Persisted editor pins are retired; only selected endpoints constrain this UI path.
    pins=[]
    constraints=dict(version=1,kind='pulse_times',start_seconds=start,end_seconds=end,
                     interval_count=count,anchors=pins)
    original=[clock.to_seconds(left+i) for i in range(round(existing)+1)]
    if decode is None:
        from .manual_pulse_decode import decode_evidence
        if evidence.get('probability_source')=='auf_takt_three_class':
            if count!=round(existing):
                raise ValueError('局部质量复核不改拍数；请使用确认拍数后的手动重找入口。')
            def decode(e,c):
                return decode_evidence(e,c,pulse_strategy='dense_counted',purpose='quality_review')
        else:
            decode=decode_evidence
    decoded=decode(evidence,constraints)
    if decoded.get('decode_purpose')=='quality_review' and decoded.get('acoustic_refinement') is not None:
        raise ValueError('质量复核需要未微调的原始路径；未执行重复微调。')
    if decoded.get('decode_purpose')=='quality_review' and confirmed:
        # Only the coordinator has the caller's explicit confirmation. The
        # worker independently labels its input as an unconfirmed hypothesis.
        decoded['warnings'][0]='按人工确认且与当前图相同的拍数寻找局部候选；仍需通过质量复核并试听后采用。'
    if (decoded.get('activation_sha256')!=evidence['activation_sha256'] or
            decoded.get('activation_identity')!=evidence['activation_identity']):
        raise ValueError('Candidate changed source observation identity')
    before_refinement=deepcopy(decoded)
    before_refinement.setdefault('constraint_diagnostics',{})['interval_count_confirmed_by_user']=confirmed
    before_refinement['constraint_diagnostics']['count_basis']=('explicit_user_confirmation' if confirmed else 'current_quarter_clock_hypothesis')
    initial=evaluate(evidence,original,decoded.get('beat_seconds',[]),constraints)
    refined=refine(evidence,decoded.get('beat_seconds',[]),constraints)
    review=evaluate(evidence,original,refined['beat_seconds'],constraints)
    # The upstream counted decoder historically labels all counts user-confirmed;
    # explicitly correct that label for this diagnostic hypothesis.
    decoded=deepcopy(decoded)
    decoded['beat_seconds']=refined['beat_seconds']
    p,fps=_probabilities(evidence)
    decoded['beat_probability']=_sample(p,fps,refined['beat_seconds']).tolist()
    if evidence.get('downbeat_probability') is not None:
        decoded['downbeat_probability']=_sample(np.asarray(evidence['downbeat_probability']),fps,refined['beat_seconds']).tolist()
    decoded['log_likelihood_scope']='pre_refinement_dp_path_only'
    decoded.setdefault('constraint_diagnostics',{})['interval_count_confirmed_by_user']=confirmed
    decoded['constraint_diagnostics']['count_basis']=('explicit_user_confirmation' if confirmed else 'current_quarter_clock_hypothesis')
    diag=decoded['constraint_diagnostics']
    diag['raw_decoder_frame_indices']=diag.get('observation_frame_indices')
    diag['observation_frame_indices']=[math.floor(t*fps+.5) for t in refined['beat_seconds']]
    diag['weak_pulse_indices']=[i for i,p in enumerate(decoded['beat_probability']) if p<.1]
    decoded['acoustic_refinement']=deepcopy(refined)
    return dict(version=VERSION,review=review,decoded=decoded,constraints=constraints,
        decoded_before_refinement=before_refinement,review_before_refinement=initial,refinement=refined,
        candidate_available=review['accepted'],count_confirmed=confirmed,
        requires_explicit_adoption=True,source_events_modified=False,map_published=False)
