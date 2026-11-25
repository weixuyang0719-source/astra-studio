"""Bounded notation interpretations of native-style pulse evidence.

A keeps decoded groups. B keeps EVERY identical pulse and merges two decoded
bars, assigning half a quarter to each pulse. Optional C is separately decoded
6/8-pulse groups, explicitly interpreted as 3/4 or 4/4, not compound 6/8 meter.
This module performs no network inference or additional decoding, and never
publishes a map or declares uncalibrated preferences musical ground truth.
"""
from copy import deepcopy
import math
import statistics

from .cubase_pulse_map import build_pulse_candidate_map
from .timing import TempoMap
from .boundary_meter import boundary_time_signature

VERSION=2


def _runs(result):
    meters=result['meters'];indices=[i for i in range(len(meters)) if i==0 or meters[i]!=meters[i-1]]
    return [(a,b,meters[a]) for a,b in zip(indices,indices[1:]+[len(meters)])]


def _phase_projection(tempo, report, result, bar_indices, numerator_scale):
    """Change only the selected notation grouping; no clock anchor edits."""
    unit=report['quarter_span_per_pulse'];origin=report['chosen_prefix_quarters']
    signatures=[]
    for index,_,group in _runs(result):
        numerator=group*numerator_scale
        if int(numerator)!=numerator or numerator<1:raise ValueError('Notation grouping is not an integer quarter meter')
        signatures.append({'beat':0. if index==0 else origin+index*unit,
            'numerator':int(numerator),'denominator':4,'origin':'metrical_interpretation','confirmed':False})
    tempo['time_signatures']=signatures
    first=bar_indices[0];q=origin+first*unit;opening=signatures[0]['numerator']
    pickup=q%opening
    if min(abs(pickup),abs(pickup-opening))<1e-9:pickup=0.
    if pickup:boundary_time_signature(pickup,(4,))
    tempo['pickup_beats']=pickup;tempo['first_full_bar_beat']=q
    tempo['opening_phase']={'origin':'metrical_interpretation_provisional','confirmed':False,
        'seconds':result['beat_seconds'][first],'beat':q,'pulse_index':first,
        'semantic_first_full_bar_confirmed':False}
    report['selected_notation_bar_origin_seconds']=result['beat_seconds'][first]
    report['notation_meter_denominator']=4
    report['source_projection_meter_denominator']=report['meter_denominator']
    report['meter_denominator']=4
    report['meter_interpretation']=(
        'same source pulses at two quarters each; split each decoded group into two common quarter-denominator bars'
        if unit==2. else
        'same source pulses at half a quarter each; pair consecutive decoded bars into quarter-denominator notation groups'
        if numerator_scale==1 else
        'explicitly re-decoded 6/8-pulse groups interpreted as 3/4 or 4/4; pulse counts are not compound meter labels')
    report['notated_meters']=[(s['numerator'],s['denominator']) for s in signatures]
    tempo['pulse_projection']=deepcopy(report)
    TempoMap(tempo)


def _note_diagnostics(tempo, bar_seconds, notes):
    """Descriptive engraving pressure, not a validated candidate ranker."""
    if notes is None:return {'available':False}
    clock=TempoMap(tempo);origin=tempo['grid_origin'];starts=[]
    for note in notes:
        s=float(note['start'])
        if not math.isfinite(s) or s<0:raise ValueError('Invalid reference note onset')
        starts.append(s)
    grids=[]
    for step in (1.,.5,.25,.125,1/3,1/6):
        errors=[]
        for s in starts:
            q=clock.to_beat(s);snap=max(0.,origin+round((q-origin)/step)*step)
            errors.append(abs(clock.to_seconds(snap)-s))
        ordered=sorted(errors)
        grids.append({'quarter_step':step,'notes':len(errors),
            'median_error_seconds':statistics.median(errors) if errors else None,
            'p90_error_seconds':ordered[min(len(ordered)-1,math.ceil(.9*len(ordered))-1)] if errors else None,
            'fraction_within_35ms':sum(e<=.035 for e in errors)/len(errors) if errors else None})
    crossings=0;near_boundary=0
    for note in notes:
        s=float(note['start']);end=float(note.get('end',s))
        if not math.isfinite(end) or end<s:raise ValueError('Invalid reference note end')
        bars=[b for b in bar_seconds if s+1e-6<b<end-1e-6]
        crossings+=len(bars)
        if bars and clock.to_beat(bars[0])-clock.to_beat(s)<.125:near_boundary+=1
    return {'available':True,'onset_grids':grids,'raw_note_bar_crossings':crossings,
        'raw_notes_starting_within_eighth_quarter_before_crossed_bar':near_boundary,
        'warning':'Raw release/pedal tails are not model notation durations; these counts do not predict required ties and cannot alone rank candidates.'}


def _prior(tempo, prior, audio_sha256):
    if prior is None:return {'available':False,'reason':'no_identity_bound_cached_notation_grid'}
    if prior.get('audio_sha256')!=audio_sha256 or not prior.get('source_sha256'):
        raise ValueError('Cached notation prior has no matching audio/artifact identity')
    from .duet_notation import infer_beat_ratios
    times=list(prior['model_grid_seconds'])
    ratios,review=infer_beat_ratios(times,TempoMap(tempo))
    support=prior.get('supported_seconds_range',[0.,TempoMap(tempo).seconds[-1]])
    if len(support)!=2 or not all(math.isfinite(float(v)) for v in support) or support[0]>=support[1]:
        raise ValueError('Notation prior has invalid source evidence bounds')
    outside={i for i in range(len(ratios)) if times[i]<support[0] or times[i+1]>support[1]}
    unresolved={r['model_beat'] for r in review}-outside
    resolved=[r for i,r in enumerate(ratios) if i not in unresolved and i not in outside]
    return {'available':True,'source_sha256':prior['source_sha256'],'interval_count':len(ratios),
        'unresolved_intervals':len(unresolved),'excluded_extrapolated_intervals':len(outside),
        'supported_seconds_range':support,
        'resolved_ratio_counts':{str(v):resolved.count(v) for v in (.5,1.,2.)},
        'unit_ratio_agreement':resolved.count(1.)/len(resolved) if resolved else None,
        'used_for':'notation quarter-value evidence only; no pulse relocation',
        'model_prior_is_ground_truth':False}


def _finish(key,kind,source,projection,bars,base,notes,prior,audio_sha256):
    tempo=projection['tempo'];report=projection['report'];seconds=source['beat_seconds']
    clock=TempoMap(tempo);origin=report['chosen_prefix_quarters'];unit=report['quarter_span_per_pulse']
    selected=[seconds[int(i)] if float(i).is_integer() else clock.to_seconds(origin+i*unit) for i in bars]
    probs=source.get('observations_at_pulses')
    p=[float(probs[int(i)][1]) for i in bars if float(i).is_integer()] if probs and len(probs)==len(seconds) else []
    report.update(interpretation_version=VERSION,interpretation_id=key,
        interpretation_kind=kind,notation_bar_origin_confirmed=False,
        preserves_base_pulse_seconds=seconds==base['beat_seconds'],
        base_pulse_count=len(base['beat_seconds']),candidate_pulse_count=len(seconds),
        ordinal_max_seconds_difference=max(abs(a-b) for a,b in zip(seconds,base['beat_seconds']))
            if len(seconds)==len(base['beat_seconds']) else None,
        source_group_counts=sorted(set(source['meters'])),
        bars_seconds=selected,bar_pulse_indices=bars,
        source_downbeat_seconds=deepcopy(source['downbeat_seconds']),
        bar_evidence={'available':bool(p),'observed_bar_positions':len(p),
            'interpolated_bar_positions_without_probability':sum(not float(i).is_integer() for i in bars),
            'mean_original_downbeat_probability':statistics.mean(p) if p else None,
            'mean_log_original_downbeat_probability':statistics.mean(math.log(max(v,1e-12)) for v in p) if p else None,
            'calibrated_confidence':False},
        meter_change_count=len(tempo['time_signatures'])-1,
        notation_pressure=_note_diagnostics(tempo,selected,notes),
        notation_prior=_prior(tempo,prior,audio_sha256),
        hmm_cross_state_space_score_used_for_selection=False,
        automatic_apply_allowed=False,effective_map_published=False)
    tempo['pulse_projection']=deepcopy(report)
    return {'id':key,'valid':True,'tempo':tempo,'report':report}


def build_metrical_candidates(base_result, *, audio_duration, audio_sha256,
                              notes=None, notes_audio_sha256=None,
                              notation_prior=None, regrouped_decoder_result=None):
    """A, B0/B1, optional C, D; at most five candidates, no hidden search.

    B phase 0/1 selects alternate original downbeats. At any meter change the
    preceding doubled bar must finish exactly; an incomplete pair is rejected,
    never silently joined across the meter change. Last partial tails are not
    evidence of a real meter change. C must be produced explicitly elsewhere.
    """
    if notes is not None and notes_audio_sha256!=audio_sha256:
        raise ValueError('Notation pressure notes have no matching audio identity')
    if set(base_result['meters'])- {3,4}:
        raise ValueError('A/B currently support native 3/4-pulse groups only')
    args=dict(audio_duration=audio_duration,audio_sha256=audio_sha256,prefix_policy='notational_prefix')
    a=build_pulse_candidate_map(base_result,**args)
    native_bars=[i for i,n in enumerate(base_result['beat_numbers']) if n==1]
    if not native_bars:raise ValueError('No decoded downbeat supports a grouping candidate')
    candidates=[_finish('A','native_groups_quarter_pulse',base_result,a,native_bars,base_result,notes,notation_prior,audio_sha256)]
    for phase in (0,1):
        bars=[];conflicts=[];runs=_runs(base_result)
        for run_index,(start,end,meter) in enumerate(runs):
            down=next((i for i in range(start,end) if base_result['beat_numbers'][i]==1),None)
            if down is None:conflicts.append({'reason':'no_downbeat_in_meter_segment','start_index':start});continue
            selected=down+phase*meter if run_index==0 else start
            if selected>=end:
                conflicts.append({'reason':'insufficient_initial_group','start_index':start});continue
            if run_index<len(runs)-1 and (end-selected)%(2*meter):
                conflicts.append({'reason':'unpaired_decoded_bar_before_meter_change','change_index':end,
                    'change_seconds':base_result['beat_seconds'][end],
                    'remaining_pulses':(end-selected)%(2*meter),'needed_pulses_per_bar':2*meter})
            bars.extend(range(selected,end,2*meter))
        key='B'+str(phase)
        if conflicts:
            candidates.append({'id':key,'valid':False,'report':{'interpretation_kind':'same_pulses_pair_bars',
                'phase_choice':phase,'boundary_conflicts':conflicts,'automatic_apply_allowed':False}})
            continue
        b=build_pulse_candidate_map(base_result,**args,quarters_per_pulse=.5)
        b['report'].update(decoded_bars_per_notated_bar=2,phase_choice=phase,
                           interpretation_changes_pulse_times=False)
        _phase_projection(b['tempo'],b['report'],base_result,bars,1)
        candidates.append(_finish(key,'same_pulses_pair_bars',base_result,b,bars,base_result,notes,notation_prior,audio_sha256))
    if regrouped_decoder_result is not None:
        source=regrouped_decoder_result
        if source.get('observation_provenance')!=base_result.get('observation_provenance'):
            raise ValueError('C must reuse the same identity-bound network observations')
        original_observations=base_result.get('observations_sha256')
        alternative_observations=source.get('observation_file_sha256')
        if original_observations and alternative_observations and original_observations!=alternative_observations:
            raise ValueError('C observation cache hash differs from A')
        if set(source['meters'])-{6,8}:raise ValueError('C requires explicitly re-decoded 6/8-pulse groups')
        c=build_pulse_candidate_map(source,**args,quarters_per_pulse=.5)
        bars=[i for i,n in enumerate(source['beat_numbers']) if n==1]
        if not bars:raise ValueError('C has no decoded downbeat')
        c['report'].update(decoded_bars_per_notated_bar=1,
            group_count_semantics='6/8 are pulse counts; selected notation is 3/4 or 4/4, not compound 6/8')
        _phase_projection(c['tempo'],c['report'],source,bars,.5)
        candidates.append(_finish('C','explicit_six_eight_pulse_redecode',source,c,bars,base_result,notes,notation_prior,audio_sha256))
    # D changes note-value interpretation only. A 3-pulse group spans six
    # quarters and is split at 1.5 pulses (interpolated musical bar boundary),
    # while a 4-pulse group splits at an existing ordinary pulse. Neither is a
    # newly acoustically detected downbeat or an extra pulse in the source.
    bars=[];boundaries=[]
    for index in native_bars:
        for position in (index,index+base_result['meters'][index]/2):
            if position>len(base_result['beat_seconds'])-1:continue
            bars.append(position)
            boundaries.append({'pulse_coordinate':position,
                'origin':('decoded_downbeat' if position==index else
                          'ordinary_pulse_reinterpreted_as_bar' if float(position).is_integer() else
                          'interpolated_musical_boundary'),
                'acoustically_detected_downbeat':position==index})
    d=build_pulse_candidate_map(base_result,**args,quarters_per_pulse=2.)
    d['report'].update(decoded_bars_per_notated_bar=.5,interpretation_changes_pulse_times=False,
        double_unit_grouping_complete=True)
    _phase_projection(d['tempo'],d['report'],base_result,bars,1)
    item=_finish('D','same_pulses_double_quarter_split_bars',base_result,d,bars,base_result,notes,notation_prior,audio_sha256)
    for boundary,seconds in zip(boundaries,item['report']['bars_seconds']):boundary['seconds']=seconds
    item['report']['bar_boundary_provenance']=boundaries
    item['tempo']['pulse_projection']=deepcopy(item['report'])
    candidates.append(item)
    return {'version':VERSION,'candidates':candidates,'selection':{
        'status':'metrical_level_review','automatic_apply_allowed':False,'selected_candidate_id':None,
        'reason':'Acoustic bar evidence and notation-pressure/prior signals are exposed separately; no calibrated universal ranking or silent BPM-range choice.'}}
