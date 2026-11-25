"""Local model-rhythm projection, independent of performance-clock editing.

Confirmed beat ratios supply the model unit. Clear surrounding attacks supply
phase; ambiguous attacks near a grid midpoint do not independently rewrite a
regular rhythm. All outputs are notation coordinates, never source seconds.
"""
from bisect import bisect_right
from collections import Counter
from fractions import Fraction
import math
from statistics import median

VERSION=4


def _reference_bar(tempo, beat):
    from .duet_notation import reference_phase
    origin,region=reference_phase(tempo,beat)
    sig=tempo.data['time_signatures'][0]
    for item in tempo.data['time_signatures']:
        if float(item['beat'])>beat+1e-9:break
        sig=item
    size=sig['numerator']*4/sig['denominator']
    return region,math.floor((beat-origin+1e-9)/size)


def _sequence_rolls(priors,ids,targets,positions,onsets,pitches,durations,staffs,raw,tempo,ratios,protected,anchors):
    """Validate independently classified original-model rolls, never infer new ones."""
    from .duet_notation import scale_model_duration
    accepted=[];members=set(ids)
    if staffs is None:return accepted
    for prior in priors:
        group=list(prior['input_ids'])
        if (prior.get('source')!='duet_original_quantize' or len(group)<2
                or len(set(group))!=len(group) or not set(group)<=members
                or protected.intersection(group)):continue
        if len({int(staffs[i]) for i in group})!=1:continue
        q=float(positions[group[0]]);staff=int(staffs[group[0]])
        complete={i for i in range(len(positions)) if durations[i]>0 and int(staffs[i])==staff
                  and abs(float(positions[i])-q)<1e-7}
        if complete!=set(group) or len({int(pitches[i]) for i in group})!=len(group):continue
        if max(float(durations[i]) for i in group)-min(float(durations[i]) for i in group)>1e-8:continue
        target=targets[group[0]]
        if any(abs(targets[i]-target)>1e-8 for i in group):continue
        # A roll does not license crossing a bar, meter/phase change, or pin.
        if len({_reference_bar(tempo,v) for v in [target]+[raw[i] for i in group]})!=1:continue
        if any(min([float(onsets[i]) for i in group]+[tempo.to_seconds(target)])<=a<=
               max([float(onsets[i]) for i in group]+[tempo.to_seconds(target)]) for a in anchors):continue
        # Keep the original classifier's bound visible and enforce it again;
        # the new reference clock cannot create or enlarge a roll identity.
        spread=float(prior['model_spread_quarters']);limit=float(prior['model_spread_limit_quarters'])
        if not 0<spread<=limit+1e-9 or not math.isfinite(limit):continue
        reference_spread=max(raw[i] for i in group)-min(raw[i] for i in group)
        reference_limit=scale_model_duration(q,limit,ratios)
        if reference_spread>reference_limit+1e-9:continue
        residuals=[raw[i]-target for i in group]
        accepted.append(dict(prior,staff=staff,pitches=[int(pitches[i]) for i in group],
            source_seconds=[float(onsets[i]) for i in group],onset_quarters=target,
            duration_quarters=float(durations[group[0]]),
            reference_individual_residuals=residuals,reference_center_residual=median(residuals),
            reference_spread_quarters=reference_spread,reference_spread_limit_quarters=reference_limit))
    return accepted


def match_protected_notes(protected,onsets,pitches):
    """Conservative 1ms MIDI-serialization tolerance; protect all matches."""
    result=set()
    for note in protected:
        matches=[i for i,(t,p) in enumerate(zip(onsets,pitches))
                 if int(p)==note['pitch'] and abs(float(t)-note['seconds'])<=.001]
        if not matches:raise ValueError('手工音符未在制谱输入 MIDI 中匹配，拒绝无保护地重新记谱')
        result.update(matches)
    return result


def preserve_model_sequences(positions,onsets,pitches,durations,tempo,ratios,aligned,*,
                             grid='auto',uncertain_intervals=(),protected_input_ids=(),
                             protected_anchor_seconds=(),excluded_input_ids=(),staffs=None,
                             original_model_rolls=()):
    from .duet_notation import scale_model_duration,reference_phase
    result=list(map(float,aligned));protected=set(protected_input_ids)|set(excluded_input_ids)
    anchors=sorted(set(map(float,protected_anchor_seconds)))
    uncertain={int(item['model_beat']) for item in uncertain_intervals}
    units=[scale_model_duration(0,float(q),ratios) for q in positions]
    raw=[tempo.to_beat(float(t)) for t in onsets]
    step={'straight':.25,'auto':.25,'triplet':1/3,'fine':.125}[grid]
    buckets={}
    for i,q in enumerate(positions):
        if durations[i]>0:buckets.setdefault(round(float(q),7),[]).append(i)
    groups=[]
    for q,ids in buckets.items():
        times=[float(onsets[i]) for i in ids];region={bisect_right(anchors,t) for t in times}
        blocked=(any(i in protected for i in ids) or len({pitches[i] for i in ids})!=len(ids)
                 or len(region)!=1 or any(abs(t-a)<1e-7 for t in times for a in anchors))
        phases=[reference_phase(tempo,raw[i]) for i in ids]
        blocked |= len({phase[1] for phase in phases})!=1
        groups.append(dict(q=q,ids=ids,u=units[ids[0]],r=median(raw[i] for i in ids),
                           time=median(times),region=next(iter(region)) if len(region)==1 else -1,
                           phase_origin=phases[0][0],phase_region=phases[0][1],blocked=blocked))
    groups.sort(key=lambda g:(g['time'],g['q']))
    segments=[];current=[]
    for g in groups:
        offset=g['r']-g['u']
        separate=g['blocked']
        if current:
            prev=current[-1];delta=g['u']-prev['u']
            unknown=any(i in uncertain for i in range(math.floor(prev['q']),math.ceil(g['q'])))
            offsets=[v['r']-v['u'] for v in current]+[offset]
            separate |= (g['region']!=prev['region'] or not 0<delta<=2 or unknown
                         or g['phase_region']!=prev['phase_region']
                         or abs((g['r']-prev['r'])-delta)>.25
                         or max(offsets)-min(offsets)>.25
                         or g['u']-current[0]['u']>16)
        if separate:
            if current:segments.append(current)
            current=[]
        if not g['blocked']:current.append(g)
    if current:segments.append(current)
    from bisect import bisect_left
    raw_order = sorted(range(len(result)), key=lambda idx: raw[idx])
    raw_sorted = [raw[idx] for idx in raw_order]
    accepted=[];rejected=[];used_rolls=[]
    for segment in segments:
        ids=[i for g in segment for i in g['ids']]
        if len(segment)<5:continue
        offsets=[g['r']-g['u'] for g in segment]
        origin=segment[0]['phase_origin']
        local_step=step
        if grid=='auto':
            gaps=[Fraction(b['u']-a['u']).limit_denominator(48) for a,b in zip(segment,segment[1:])]
            triplet_gaps=sum(g.denominator%3==0 for g in gaps)
            if triplet_gaps>=2 and triplet_gaps>=len(gaps)/2:
                local_step=1/3
        # Only clearly placed attacks vote for phase. A common 0.14q early
        # performance must not vote itself onto a 1/32-note phase lattice.
        votes=Counter()
        for c in offsets:
            candidate=origin+round((c-origin)/local_step)*local_step
            if abs(c-candidate)<=local_step/3+1e-9:votes[round(candidate,9)]+=1
        if not votes:continue
        ordered=votes.most_common();phase,support=ordered[0]
        if support<2 or support/sum(votes.values())<.6 or (len(ordered)>1 and support==ordered[1][1]):
            rejected.append(dict(input_ids=ids,reason='ambiguous_local_phase',phase_votes=dict(votes)));continue
        targets={i:units[i]+phase for i in ids}
        residuals=[raw[i]-targets[i] for i in ids]
        gaps=[b['u']-a['u'] for a,b in zip(segment,segment[1:])]
        # A recurring long/short performance pattern is musical evidence, not
        # common anticipatory feel. Do not straighten a clear swing/shuffle
        # which the model represented as equal subdivisions.
        if gaps and max(gaps)-min(gaps)<1e-7:
            offsets_even=offsets[::2];offsets_odd=offsets[1::2]
            a,b=median(offsets_even),median(offsets_odd)
            if (len(offsets_odd)>=2 and abs(a-b)>=gaps[0]/4
                    and max(abs(c-a) for c in offsets_even)<=gaps[0]/8
                    and max(abs(c-b) for c in offsets_odd)<=gaps[0]/8):
                rejected.append(dict(input_ids=ids,reason='recurring_alternating_rhythm'));continue
        # Quarter-domain bounds are deliberately conservative and scale with
        # tempo. They are fit guards, not a claim that all music has this feel.
        rolls=_sequence_rolls(original_model_rolls,ids,targets,positions,onsets,pitches,durations,
                              staffs,raw,tempo,ratios,protected,anchors)
        rolled={i for roll in rolls for i in roll['input_ids']}
        checked=[raw[i]-targets[i] for i in ids if i not in rolled]+[r['reference_center_residual'] for r in rolls]
        if min(targets.values())<0 or max(abs(v) for v in checked)>min(.2,local_step*.8)+1e-9:
            rejected.append(dict(input_ids=ids,reason='reference_residual_too_large',
                checked_residual_range=[min(checked),max(checked)],
                individual_residual_range=[min(residuals),max(residuals)]));continue
        # Do not move notation through an explicitly fixed time boundary.
        if any((float(onsets[i])<a and targets[i]>tempo.to_beat(a)+1e-8)
               or (float(onsets[i])>a and targets[i]<tempo.to_beat(a)-1e-8)
               for i in ids for a in anchors):
            rejected.append(dict(input_ids=ids,reason='fixed_anchor_boundary'));continue
        changes=[dict(input_id=i,before=result[i],after=targets[i],source_seconds=float(onsets[i]))
                 for i in ids if abs(result[i]-targets[i])>1e-8]
        # An excluded same-pitch re-attack just outside the phrase remains an
        # independent attack, even when the model collapsed its predecessor.
        collision=False
        for i in ids:
            ri = raw[i]
            lo_j = bisect_left(raw_sorted, ri - .5 - 1e-12)
            hi_j = bisect_right(raw_sorted, ri + .5 + 1e-12)
            pi = pitches[i]
            si = None if staffs is None else staffs[i]
            qi = float(positions[i])
            oi = onsets[i]
            ti = targets[i]
            for k in range(lo_j, hi_j):
                j = raw_order[k]
                if j in targets or durations[j] <= 0:
                    continue
                same_pitch = pi == pitches[j]
                same_hand = staffs is None or si == staffs[j]
                if not (same_pitch or (same_hand and abs(qi - float(positions[j])) > 1e-7)):
                    continue
                if (onsets[j] < oi and result[j] >= ti - 1e-9) or (onsets[j] > oi and result[j] <= ti + 1e-9):
                    if abs(raw[j] - ri) <= .5:
                        collision = True
                        break
            if collision:
                break
        if collision:
            rejected.append(dict(input_ids=ids,reason='independent_attack_order'));continue
        used_rolls.extend(rolls)
        if not changes:continue
        for i,value in targets.items():result[i]=value
        accepted.append(dict(input_ids=ids,model_start=segment[0]['q'],model_end=segment[-1]['q'],
            reference_phase_origin=origin,reference_phase_region=segment[0]['phase_region'],
            phase_quarters=phase,phase_grid=local_step,clear_phase_support=support,phase_votes=dict(votes),
            reference_residual_range=[min(residuals),max(residuals)],
            checked_residual_range=[min(checked),max(checked)],original_model_rolls=rolls,changes=changes))
    return result,dict(method='local_model_sequence_v2',accepted=accepted,rejected=rejected,
                       original_model_rolls=used_rolls,
                       changed_note_count=sum(len(s['changes']) for s in accepted),
                       raw_timestamps_modified=False,protected_input_ids=sorted(protected),
                       protected_anchor_seconds=anchors)


def validate_protection(notes,anchors):
    result=[]
    for note in notes or []:
        if not isinstance(note,dict):raise ValueError('Invalid protected note')
        pitch=note.get('pitch');seconds=note.get('seconds')
        if isinstance(pitch,bool) or not isinstance(pitch,(int,float)) or not math.isfinite(pitch) or int(pitch)!=pitch or not 0<=pitch<=127:
            raise ValueError('Invalid protected pitch')
        if isinstance(seconds,bool) or not isinstance(seconds,(int,float)) or not math.isfinite(seconds) or seconds<0:
            raise ValueError('Invalid protected onset')
        result.append(dict(pitch=int(pitch),seconds=float(seconds)))
    pins=[]
    for value in anchors or []:
        if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or value<0:
            raise ValueError('Invalid protected anchor')
        pins.append(float(value))
    return sorted(result,key=lambda n:(n['seconds'],n['pitch'])),sorted(set(pins))
