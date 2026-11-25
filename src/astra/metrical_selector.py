"""Conservative quarter-value recommendation, separate from bar-phase choice.

Thresholds are explicit Astra policies, not calibrated model confidence. No
model inference, map publication, timing changes, or global majority repair.
"""
import math
from bisect import bisect_right,bisect_left
from statistics import mean,median
from collections import Counter
from .timing import TempoMap
from .duet_notation import infer_beat_ratios

VERSION=2
POLICY={'minimum_resolved_intervals':32,'minimum_supported_seconds':8.,
        'minimum_resolved_fraction':.85,'minimum_dominant_fraction':.95,
        'local_conflict_minimum_intervals':2,'unknown_run_max_intervals':8,
        'unknown_run_max_seconds':4.,'pulse_match_tolerance_seconds':.03,
        'minimum_common_pulse_coverage':.98,'minimum_group_cost_margin':.05}


def _runs(values):
    result=[];i=0
    while i<len(values):
        j=i+1
        while j<len(values) and values[j]==values[i]:j+=1
        result.append((i,j,values[i]));i=j
    return result


def _note_context(prior,clock,region,dominant):
    """Check actual model attack spacings, not interpolated grid repetitions.

    Like the existing notation context check, one chord is one observation;
    model jumps >2 quarters, nonpositive steps and ornament-sized steps cannot
    vote. These are separate observations within the same model, not an
    independent ground-truth annotator.
    """
    on=prior.get('model_note_onsets_seconds');positions=prior.get('model_note_positions_quarters')
    if on is None or positions is None:return {'available':False}
    if len(on)!=len(positions) or not all(math.isfinite(float(x)) for x in list(on)+list(positions)):
        raise ValueError('Invalid identity-bound notation note arrays')
    groups=[]
    for i in sorted(range(len(on)),key=lambda i:on[i]):
        if not groups or on[i]-on[groups[-1][0]]>.06:groups.append([])
        groups[-1].append(i)
    clusters=[]
    for ids in groups:
        position,count=Counter(round(positions[i],6) for i in ids).most_common(1)[0]
        clusters.append((median(on[i] for i in ids),position if count/len(ids)>=.6 else None))
    left,right=region['start_seconds'],region['end_seconds'];pairs=[];values=[]
    for a,b in zip(clusters,clusters[1:]):
        if b[0]<=left or a[0]>=right:continue
        dt=b[0]-a[0];dq=b[1]-a[1] if a[1] is not None and b[1] is not None else None
        dr=clock.to_beat(b[0])-clock.to_beat(a[0]);unit=None
        if dq is not None and .1<=dq<=2.01 and dt>=.08 and dr>0:
            raw=dq/dr;nearest=min((.5,1.,2.),key=lambda k:abs(math.log2(raw/k)))
            if abs(raw/nearest-1)<=.18:unit=nearest
        values.append(unit);pairs.append({'start_seconds':a[0],'end_seconds':b[0],
            'model_quarter_delta':dq,'native_quarter_delta':dr,'supported_quarters_per_pulse':unit})
    contradictions=[{'quarters_per_pulse':unit,'consecutive_attack_intervals':end-start,
                      'start_seconds':pairs[start]['start_seconds'],'end_seconds':pairs[end-1]['end_seconds']}
        for start,end,unit in _runs(values) if unit is not None and unit!=dominant and end-start>=2]
    return {'available':True,'note_attacks':sum(left<=t<=right for t in on),
            'attack_clusters':sum(left<=t<=right for t,_ in clusters),
            'pairs':pairs,'corroborated_local_conflicts':contradictions,
            'principle':'two consecutive non-ornament note intervals required; model grid interpolation alone cannot corroborate itself'}


def _unit_selection(bundle,prior,base):
    candidates={c['id']:c for c in bundle['candidates'] if c['valid']}
    if 'A' not in candidates:raise ValueError('Native candidate A is required')
    clock=TempoMap(candidates['A']['tempo']);report=candidates['A']['report']
    if report['quarter_span_per_pulse']!=1:raise ValueError('A must retain native quarter per pulse')
    origin=report['chosen_prefix_quarters']
    if any(abs(clock.to_seconds(origin+i)-s)>1e-8 for i,s in enumerate(base['beat_seconds'])):
        raise ValueError('Candidate A does not map the supplied native pulse evidence')
    audio=base.get('source_sha256') or base.get('observation_provenance',{}).get('audio_source_sha256')
    if prior.get('audio_sha256')!=audio or not prior.get('source_sha256'):
        raise ValueError('Notation prior identity differs from pulse source')
    times=prior['model_grid_seconds'];ratios,review=infer_beat_ratios(times,clock)
    support=prior.get('supported_seconds_range',[0.,clock.seconds[-1]])
    if len(support)!=2 or not all(math.isfinite(v) for v in support) or support[0]>=support[1]:
        raise ValueError('Invalid prior support bounds')
    unknown={r['model_beat'] for r in review};units=[];eligible=[]
    for i,r in enumerate(ratios):
        inrange=times[i]>=support[0] and times[i+1]<=support[1]
        eligible.append(inrange);units.append(1/r if inrange and i not in unknown else None)
    resolved=[u for u in units if u is not None];n=sum(eligible)
    dominant=max((.5,1.,2.),key=lambda u:resolved.count(u)) if resolved else None
    agreement=resolved.count(dominant)/len(resolved) if resolved else 0.
    ranges=[];conflicts=[];gaps=[]
    # Runs are kept local, including contradictory evidence which a global
    # ratio majority would otherwise silently erase.
    for start,end,unit in _runs(units):
        active=[i for i in range(start,end) if eligible[i]]
        if not active:continue
        start,end=active[0],active[-1]+1
        item={'start_seconds':times[start],'end_seconds':times[end],
              'model_interval_start':start,'model_interval_end':end,
              'interval_count':end-start,'quarters_per_pulse':unit,
              'kind':'unresolved' if unit is None else 'supported_quarter_value'}
        ranges.append(item)
        if unit is not None and unit!=dominant and end-start>=POLICY['local_conflict_minimum_intervals']:
            conflicts.append(item)
        if unit is None and (end-start>=POLICY['unknown_run_max_intervals'] or
                            times[end]-times[start]>=POLICY['unknown_run_max_seconds']):gaps.append(item)
    coverage=len(resolved)/n if n else 0.
    duration=sum(times[i+1]-times[i] for i,v in enumerate(units) if v is not None)
    ids=[k for k,c in candidates.items() if c['report']['quarter_span_per_pulse']==dominant]
    blockers=[]
    if len(resolved)<POLICY['minimum_resolved_intervals'] or duration<POLICY['minimum_supported_seconds']:
        blockers.append('insufficient_supported_intervals')
    if coverage<POLICY['minimum_resolved_fraction']:blockers.append('insufficient_resolved_coverage')
    if agreement<POLICY['minimum_dominant_fraction']:blockers.append('no_dominant_quarter_value')
    local_warnings=[];unverified_conflicts=[];unverified_gaps=[]
    for region in conflicts+gaps:
        context=_note_context(prior,clock,region,dominant)
        row=dict(region,note_context=context,
                 action='retain_primary_quarter_value_and_review_local_timing_not_retime_notes')
        local_warnings.append(row)
        if not context['available'] or context['corroborated_local_conflicts']:
            (unverified_conflicts if region in conflicts else unverified_gaps).append(row)
    if unverified_conflicts:blockers.append('supported_local_quarter_value_conflict')
    if unverified_gaps:blockers.append('long_unresolved_interval')
    if not ids:blockers.append('double_unit_grouping_pending' if dominant==2. else 'supported_value_has_no_existing_legal_candidate')
    automatic=not blockers
    return {'status':('primary_quarter_value_supported_with_local_review' if local_warnings else 'uniform_quarter_value_supported') if automatic else 'local_review_required',
        'automatic_selection_allowed':automatic,'selected_quarters_per_pulse':dominant if automatic else None,
        'dominant_quarters_per_pulse':dominant,'compatible_candidate_ids':ids,
        'blockers':blockers,'local_ranges':ranges,'conflicting_ranges':conflicts,'unresolved_gaps':gaps,
        'local_warnings':local_warnings,'all_local_timing_verified':not local_warnings,
        'resolved_counts':{str(u):resolved.count(u) for u in (.5,1.,2.)},
        'resolved_fraction':coverage,'dominant_fraction_of_resolved':agreement,
        'excluded_intervals':len(ratios)-n,'support_seconds_range':support,
        'supported_duration_seconds':duration,'prior_binding_sha256':prior['source_sha256'],
        'meaning':'quarter value only; no bar-phase confirmation and no pulse relocation',
        'uniform_value_outside_prior_support':'unobserved, not additional model evidence'}


def _matches(a,b):
    i=j=0;matches=[];tol=POLICY['pulse_match_tolerance_seconds']
    while i<len(a) and j<len(b):
        delta=a[i]-b[j]
        if abs(delta)<=tol:matches.append((i,j));i+=1;j+=1
        elif delta<0:i+=1
        else:j+=1
    return matches


def _conditional(rows):
    result=[]
    for row in rows:
        if len(row)!=3 or any(not math.isfinite(v) or v<0 or v>1 for v in row):
            raise ValueError('Invalid original three-class observations')
        mass=row[0]+row[1]
        result.append((row[1]/mass if mass>1e-12 else .5,mass))
    return result


def compare_grouping(left,right,left_source,right_source):
    """Compare two bar-label hypotheses on BOTH common observation clocks.

    The conditional Bernoulli loss uses every matched pulse (positive AND
    negative labels), not means over separately selected downbeats. Swapping
    which source provides observation rows must preserve any preference.
    Intro before both first candidate bars is excluded; its semantics stay open.
    """
    result={'left_id':left['id'],'right_id':right['id'],'automatic_selection_allowed':False,
            'bar_phase_confirmed':False,'method':'common_pulse_conditional_bernoulli_two_reference_clocks',
            'hmm_total_scores_compared':False,'cost_is_calibrated_confidence':False}
    if left_source.get('observation_provenance')!=right_source.get('observation_provenance'):
        raise ValueError('Grouping hypotheses do not share network provenance')
    if left['report']['quarter_span_per_pulse']!=right['report']['quarter_span_per_pulse']:
        return dict(result,available=False,reason='different_quarter_values')
    a=left_source['beat_seconds'];b=right_source['beat_seconds'];matches=_matches(a,b)
    coverage=len(matches)/max(len(a),len(b))
    result.update(common_pulses=len(matches),common_coverage=coverage,
                  maximum_matched_seconds_difference=max((abs(a[i]-b[j]) for i,j in matches),default=None))
    if coverage<POLICY['minimum_common_pulse_coverage']:
        return dict(result,available=False,reason='different_pulse_paths_insufficient_common_support')
    lr=left_source.get('observations_at_pulses',[]);rr=right_source.get('observations_at_pulses',[])
    if len(lr)!=len(a) or len(rr)!=len(b):return dict(result,available=False,reason='missing_source_observations')
    lp=_conditional(lr);rp=_conditional(rr)
    lb=set(left['report']['bar_pulse_indices']);rb=set(right['report']['bar_pulse_indices'])
    if not lb or not rb:return dict(result,available=False,reason='no_complete_bar_origin')
    core_start=max(a[min(lb)],b[min(rb)]);core_end=min(a[max(lb)],b[max(rb)])
    core=[(i,j) for i,j in matches if core_start<=a[i]<=core_end and core_start<=b[j]<=core_end
          and lp[i][1]>=.1 and rp[j][1]>=.1]
    if len(core)<16:return dict(result,available=False,reason='insufficient_common_interior_observations')
    def losses(pairs,reference):
        ll=[];rl=[]
        for i,j in pairs:
            p=(lp[i] if reference==0 else rp[j])[0];p=min(1-1e-7,max(1e-7,p))
            ll.append(-math.log(p if i in lb else 1-p));rl.append(-math.log(p if j in rb else 1-p))
        return {'left_mean_negative_log_likelihood':mean(ll),'right_mean_negative_log_likelihood':mean(rl),
                'right_minus_left':mean(rl)-mean(ll),'pulse_count':len(pairs)}
    costs=[losses(core,r) for r in (0,1)]
    margins=[v['right_minus_left'] for v in costs];threshold=POLICY['minimum_group_cost_margin']
    preferred=left['id'] if min(margins)>threshold else right['id'] if max(margins)<-threshold else None
    # Changes are seconds, never mixed quarter clocks.
    changes=sorted(set(TempoMap(c['tempo']).to_seconds(s['beat'])
        for c in (left,right) for s in c['tempo']['time_signatures'][1:]))
    boundaries=[]
    for seconds in changes:
        nearest=min(range(len(core)),key=lambda k:abs(a[core[k][0]]-seconds))
        local=core[max(0,nearest-8):nearest+9]
        boundaries.append({'seconds':seconds,'costs_by_observation_clock':[losses(local,r) for r in (0,1)]})
    # Interval comparisons describe path movement only. They do not penalize a
    # real rallentando or turn a changing seconds interval into a meter change.
    interval_delta=[abs(math.log((a[i2]-a[i])/(b[j2]-b[j]))) for (i,j),(i2,j2) in zip(matches,matches[1:])
                    if i2==i+1 and j2==j+1]
    local_conflicts=[v for v in boundaries if preferred is not None and any(
        c['right_minus_left']<-threshold if preferred==left['id'] else c['right_minus_left']>threshold
        for c in v['costs_by_observation_clock'])]
    return dict(result,available=True,core_seconds=[core_start,core_end],
        scored_common_pulses=len(core),costs_by_observation_clock=costs,
        acoustic_preference_id=preferred,meter_boundary_evidence=boundaries,
        local_boundary_conflicts=local_conflicts,
        mean_absolute_log_interval_ratio=mean(interval_delta) if interval_delta else None,
        caution='conditional acoustic preference only; no confirmed opening or calibrated complete-score probability')


def _meter_disagreement_episodes(left,right,end):
    def changes(candidate):
        clock=TempoMap(candidate['tempo'])
        return [(clock.to_seconds(s['beat']), (s['numerator'],s['denominator']))
                for s in candidate['tempo']['time_signatures']]
    a,b=changes(left),changes(right)
    points=sorted({0.,end}|{s for s,_ in a+b if 0<s<end})
    def at(rows,t):return next(m for s,m in reversed(rows) if s<=t+1e-8)
    episodes=[];start=None
    for x,y in zip(points,points[1:]):
        different=at(a,x)!=at(b,x)
        if different and start is None:start=x
        if not different and start is not None:
            episodes.append({'start_seconds':start,'end_seconds':x,'returned_to_shared_meter':True});start=None
    if start is not None:episodes.append({'start_seconds':start,'end_seconds':end,'returned_to_shared_meter':False})
    return episodes


def _bounded_grouping_selection(bundle,prior,base,alternative,unit,comparisons):
    output={'automatic_selection_allowed':False,'selected_candidate_id':None,
            'status':'separate_phase_review','comparisons':comparisons,
            'warnings':[],'local_review_ranges':[],'context_windows':[],
            'opening_phase_confirmed':False}
    if not unit['automatic_selection_allowed'] or unit['selected_quarters_per_pulse']!=.5 or alternative is None:
        output['reason']='half_quarter_not_established_or_no_cached_regrouping';return output
    choices={c['id']:c for c in bundle['candidates'] if c['valid']}
    if not all(k in choices for k in ('A','B0','C')):
        output['reason']='legal_baseline_or_regrouping_missing';return output
    left,right=choices['B0'],choices['C']
    comparison=next((c for c in comparisons if c['left_id']=='B0' and c['right_id']=='C'),None)
    if not comparison or not comparison.get('available') or comparison.get('acoustic_preference_id') not in ('B0','C'):
        output['reason']='no_consistent_common_observation_preference';return output
    a,b=base['beat_seconds'],alternative['beat_seconds'];matches=_matches(a,b)
    if len(matches)!=len(a) or len(matches)!=len(b):
        output['reason']='different_pulse_counts_require_local_timing_review';return output
    if abs(left['report']['bars_seconds'][0]-right['report']['bars_seconds'][0])>.03:
        output['reason']='opening_phase_would_change';return output
    winner=comparison['acoustic_preference_id'];chosen=choices[winner]
    episodes=_meter_disagreement_episodes(left,right,min(a[-1],b[-1]))
    if not episodes:
        output['reason']='no_closed_meter_episode_for_context_validation';return output
    lp=_conditional(base['observations_at_pulses']);rp=_conditional(alternative['observations_at_pulses'])
    lb=set(left['report']['bar_pulse_indices']);rb=set(right['report']['bar_pulse_indices'])
    core=[(i,j) for i,j in matches if lp[i][1]>=.1 and rp[j][1]>=.1]
    bars=chosen['report']['bars_seconds'];baseline=TempoMap(choices['A']['tempo'])
    def loss(start,end):
        pairs=[(i,j) for i,j in core if start<=a[i]<end and start<=b[j]<end]
        if not pairs:return None
        clocks=[]
        for probabilities,which in ((lp,0),(rp,1)):
            costs=[[],[]]
            for i,j in pairs:
                p=probabilities[i if which==0 else j][0];p=min(1-1e-7,max(1e-7,p))
                costs[0].append(-math.log(p if i in lb else 1-p));costs[1].append(-math.log(p if j in rb else 1-p))
            left_cost,right_cost=mean(costs[0]),mean(costs[1])
            clocks.append({'B0':left_cost,'C':right_cost,
                'winner_advantage':right_cost-left_cost if winner=='B0' else left_cost-right_cost})
        return {'pulse_count':len(pairs),'costs_by_observation_clock':clocks}
    failed=[]
    for episode in episodes:
        row=dict(episode);output['context_windows'].append(row)
        if not episode['returned_to_shared_meter']:
            row['accepted']=False;row['reason']='no_return_or_tail_context';failed.append(row);continue
        start,end=episode['start_seconds'],episode['end_seconds']
        before=bisect_right(bars,start+1e-8)-1;after=bisect_left(bars,end-1e-8)
        if before<4 or after+4>=len(bars):
            row['accepted']=False;row['reason']='fewer_than_four_complete_context_bars';failed.append(row);continue
        lo,hi=bars[before-4],bars[after+4]
        context_cost=loss(lo,hi);inside_cost=loss(start,end)
        post=[]
        for k in range(after,after+4):
            value=loss(bars[k],bars[k+1])
            post.append({'start_seconds':bars[k],'end_seconds':bars[k+1],
                         'evidence':value,'supports_winner':bool(value and value['pulse_count']>=2 and
                             all(c['winner_advantage']>.05 for c in value['costs_by_observation_clock']))})
        consecutive=max((e-s for s,e,v in _runs([p['supports_winner'] for p in post]) if v),default=0)
        note_context=_note_context(prior,baseline,{'start_seconds':lo,'end_seconds':hi},.5)
        pairs=note_context.get('pairs',[])
        valid=[p['supported_quarters_per_pulse'] for p in pairs if p['supported_quarters_per_pulse'] is not None]
        quarter_support=valid.count(.5)/len(valid) if valid else 0.
        note_ok=note_context['available'] and len(valid)>=8 and quarter_support>=.8 and not note_context['corroborated_local_conflicts']
        context_ok=bool(context_cost and context_cost['pulse_count']>=24 and
                        all(c['winner_advantage']>.05 for c in context_cost['costs_by_observation_clock']))
        accepted=context_ok and consecutive>=3 and note_ok
        row.update(context_seconds=[lo,hi],context_cost=context_cost,episode_cost=inside_cost,
                   post_return_bars=post,consecutive_supported_post_return_bars=consecutive,
                   notation_unit_support={'valid_attack_intervals':len(valid),'fraction_half_quarter':quarter_support,
                                          'corroborated_local_conflicts':note_context.get('corroborated_local_conflicts',[])},
                   accepted=accepted)
        if not accepted:row['reason']='context_phase_or_notation_continuity_not_supported';failed.append(row)
        if inside_cost and any(c['winner_advantage']<-.05 for c in inside_cost['costs_by_observation_clock']):
            warning={'start_seconds':start,'end_seconds':end,'kind':'local_accent_conflicts_with_supported_return_phase',
                     'selected_candidate_id':winner,
                     'message':'此段局部重音支持另一分组；前后完整小节与返回后的连续首拍支持当前分组，仍可局部复核。'}
            output['local_review_ranges'].append(warning)
    if failed:
        output['reason']='bounded_context_evidence_incomplete';return output
    output.update(automatic_selection_allowed=True,selected_candidate_id=winner,
                  status='bounded_return_phase_supported',reason='same_pulses_same_opening_and_complete_bar_context')
    if output['local_review_ranges']:
        output['warnings'].append('已根据前后完整小节选择分组；部分短段局部重音存在歧义，已保留复核位置。')
    return output


def select_metrical_interpretation(candidate_bundle,notation_prior,*,base_result,regrouped_result=None):
    """Return pure quarter-value decision and independently inspectable grouping."""
    unit=_unit_selection(candidate_bundle,notation_prior,base_result)
    eligible=[c for c in candidate_bundle['candidates'] if c['valid'] and
              c['report']['quarter_span_per_pulse']==unit['dominant_quarters_per_pulse']]
    comparisons=[]
    for i,left in enumerate(eligible):
        for right in eligible[i+1:]:
            ls=regrouped_result if left['id']=='C' else base_result
            rs=regrouped_result if right['id']=='C' else base_result
            if ls is not None and rs is not None:comparisons.append(compare_grouping(left,right,ls,rs))
    grouping=_bounded_grouping_selection(candidate_bundle,notation_prior,base_result,regrouped_result,unit,comparisons)
    return {'version':VERSION,'policy':dict(POLICY),'quarter_value':unit,
            'grouping':grouping,
            'source_events_modified':False,'map_published':False,'inference_run':False}
