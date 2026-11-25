"""Resolve notation units before publishing any export, never inside rendering.

One immutable performance prediction supplies a notation-unit prior. It does
not replace detected pulse seconds, authorize overwriting a manual clock, or
silently run another acoustic beat model.
"""
from copy import deepcopy
from pathlib import Path
import hashlib

VERSION = 2


def _resolution_identity(state):
    from . import metrical_selector, cubase_metrical_candidates, duet_score
    # The same acoustic pulses with a newly selected transcription model are
    # new evidence. Do not reuse a quarter-value decision made for old notes.
    return {'analysis_keys':{key:state.get('analysis_keys',{}).get(key)
                             for key in ('beat_this','piano')},
            'selector_version':metrical_selector.VERSION,
            'candidate_version':cubase_metrical_candidates.VERSION,
            'notation_model':[duet_score.SOURCE_COMMIT,duet_score.WEIGHTS_SHA256,
                              duet_score.CONFIG_SHA256]}


def eligible(state, tempo, mode, editor=None):
    config=state.get('config',{})
    if (mode not in ('all','score','midi') or 'piano' not in state.get('instruments',[])
            or state.get('source_kind')=='midi'
            or config.get('rhythm_provider','beat_this')!='cubase_local'
            or config.get('score',{}).get('piano_engine')!='duet'
            or state.get('tempo_kind')!='auto'
            or tempo.get('source')!='cubase_local_reconstruction'
            or not config.get('models',{}).get('cubase_local',{}).get('auto_metrical',True)
            or (editor and (editor.get('manual') or editor.get('anchors')))):
        return False
    previous=tempo.get('metrical_resolution',{})
    return not (previous.get('coordinator_version')==VERSION
                and previous.get('status')=='applied'
                and previous.get('resolution_identity')==_resolution_identity(state))


def _hash(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):h.update(block)
    return h.hexdigest()


def resolve_before_export(project,state,tempo,mode,*,normalized_audio,editor=None,log=print,cache_only=False):
    """Return a detached clock, optional prepared prediction and audit summary.

    The caller must recheck its editor revision before applying this result.
    This function creates only inference/analysis caches, never a project or
    export pointer. Unresolved grouping keeps the existing clock visible.
    """
    if not eligible(state,tempo,mode,editor):
        return {'tempo':deepcopy(tempo),'prepared_prediction':None,'summary':None}
    from .pipeline import _notes_bundle, _pedals_in_duration, read_json, save_json, config_key
    from .score_duet_export import prepare_duet_prediction
    from .duet_cached_prior import extract_pending_notation_prior
    from .cubase_metrical_candidates import build_metrical_candidates
    from .metrical_selector import select_metrical_interpretation
    project=Path(project);audio=Path(normalized_audio)
    key=state['analysis_keys']['beat_this']
    base=read_json(project/'cache'/f'beats-{key}.json')
    if (base.get('provider')!='cubase_local_reconstruction'
            or _hash(audio)!=base.get('source_sha256')):
        raise ValueError('拍值核对的原音频与拍点缓存身份不一致')
    bundle=_notes_bundle(project,state,'piano',state['analysis_keys']['piano'])
    if not bundle or not bundle['notes']:
        return {'tempo':deepcopy(tempo),'prepared_prediction':None,'summary':None}
    options=dict(state['config'].get('score',{}))
    if cache_only:options['_harmony_reuse_only']=True
    title=Path(state['source']).stem.split(' - ',1)[-1]
    pedals=list(_pedals_in_duration(bundle.get('pedals',[]),state['duration']))
    log('[cubase_local] 先核对记谱拍值，再统一生成 MIDI 和乐谱')
    prepared=prepare_duet_prediction(bundle['notes'],pedals,tempo,project,
        options=options,title=title,controls=bundle.get('controls'),log=log)
    summary={'coordinator_version':VERSION,'analysis_key':key,'status':'review',
        'resolution_identity':_resolution_identity(state),
        'source_events_modified':False,'selected_candidate_id':None,
        'message':'拍值证据尚不足，保留当前拍点图。'}
    result=deepcopy(tempo)
    selected_evidence=base
    coordination_warnings=[]
    audit={'version':VERSION,'pending_run':True,'project_published':False,
           'base_observations_sha256':base.get('observations_sha256')}
    try:
        original=Path(state.get('source_audio') or state['source'])
        binding={'original_path':str(original),'original_sha256':_hash(original),
            'normalized_path':str(audio),'normalized_sha256':base['source_sha256'],
            'method':'ffmpeg_decode_audio'}
        extracted=extract_pending_notation_prior(project/'project.json',state,bundle['notes'],
            Path(prepared['prediction']['report']).parent,prepared['midi'],
            expected_audio_sha256=base['source_sha256'],audio_binding=binding)
        prior=extracted['prior']
        candidates=build_metrical_candidates(base,audio_duration=state['duration'],
            audio_sha256=base['source_sha256'],notes=bundle['notes'],
            notes_audio_sha256=base['source_sha256'],notation_prior=prior)
        selection=select_metrical_interpretation(candidates,prior,base_result=base)
        first_unit=selection['quarter_value']
        if first_unit.get('automatic_selection_allowed') and first_unit.get('selected_quarters_per_pulse')==.5:
            # A half-quarter pulse changes how many detected pulses a 3/4 or
            # 4/4 bar contains. Re-evaluate those states using the SAME network
            # observations; never run a second acoustic model or scan tempos.
            from .cubase_local_worker import redecode_cached_result
            log('[cubase_local] 核对变拍前后的完整小节分组；复用音频观测，不重复模型推理')
            try:
                regrouped=redecode_cached_result(base,project/'cache'/'metrical-regrouping',
                                                   {'meters':[6,8]},log=log)
                candidates=build_metrical_candidates(base,audio_duration=state['duration'],
                    audio_sha256=base['source_sha256'],notes=bundle['notes'],
                    notes_audio_sha256=base['source_sha256'],notation_prior=prior,
                    regrouped_decoder_result=regrouped)
                selection=select_metrical_interpretation(candidates,prior,base_result=base,
                                                          regrouped_result=regrouped)
                audit['regrouped_decode_cache']=regrouped.get('cache')
            except (OSError,ValueError,KeyError,RuntimeError) as exc:
                audit['regrouping_unavailable']=str(exc)
                coordination_warnings.append('半拍值已获得支持，但完整小节重分组未能核验；请比较变拍前后的小节线。')
        audit.update(prior=extracted,selection=selection,candidates=candidates)
        quarter=selection['quarter_value']
        unit=quarter.get('selected_quarters_per_pulse')
        candidate_id={1.:'A',.5:'B0',2.:'D'}.get(unit)
        grouping=selection.get('grouping',{})
        if unit==.5 and grouping.get('automatic_selection_allowed'):
            selected=grouping.get('selected_candidate_id')
            if selected in ('B0','C'):candidate_id=selected
        choice=next((c for c in candidates['candidates']
                     if c['id']==candidate_id and c['valid']),None)
        if quarter.get('automatic_selection_allowed') and choice is not None:
            # B0 retains the first decoded downbeat; B1 changes phase and is
            # never chosen just because a half-quarter value was established.
            result=deepcopy(choice['tempo']);result['candidate_only']=False
            selected_evidence=regrouped if candidate_id=='C' else base
            summary.update(status='applied',selected_candidate_id=candidate_id,
                quarters_per_pulse=unit,opening_phase_confirmed=False,
                message={1.:'记谱模型支持原拍值；保留原始首强拍相位。',
                         .5:'记谱模型支持半拍值；按同一批拍点成对分组，保留原始首强拍相位。',
                         2.:'记谱模型支持双拍值；按同一批拍点拆分小节，中间小节线为推导位置，未增加检测拍点。'}[unit])
            if candidate_id=='C':
                summary['message']='记谱模型支持半拍值；同源观测及前后完整小节支持重新分组，保留原始演奏秒时间。'
        else:
            summary['reason']=quarter.get('status','metrical_level_review')
            if quarter.get('dominant_quarters_per_pulse')==2. and choice is None:
                summary['reason']='double_unit_grouping_pending'
        summary['quarter_value']={k:deepcopy(quarter[k]) for k in (
            'status','selected_quarters_per_pulse','dominant_quarters_per_pulse',
            'automatic_selection_allowed','blockers') if k in quarter}
        summary['grouping']={k:deepcopy(grouping[k]) for k in
            ('status','automatic_selection_allowed','selected_candidate_id','local_review_ranges') if k in grouping}
        for region in grouping.get('local_review_ranges',[]):
            coordination_warnings.append(f"{region['start_seconds']:.1f}–{region['end_seconds']:.1f} 秒局部重音存在分组歧义；已根据前后完整小节选择分组，可在卷帘复核。")
        summary['local_timing_review']=[{k:deepcopy(region[k]) for k in
            ('start_seconds','end_seconds','kind','quarters_per_pulse') if k in region}
            for region in quarter.get('local_warnings',[])]
        summary['local_review_ranges']=len(summary['local_timing_review'])+len(grouping.get('local_review_ranges',[]))
        summary['all_local_timing_verified']=quarter.get('all_local_timing_verified',False) and not grouping.get('local_review_ranges')
        if summary['local_timing_review']:
            ranges='、'.join(f"{r['start_seconds']:.1f}–{r['end_seconds']:.1f} 秒"
                            for r in summary['local_timing_review'][:4])
            if len(summary['local_timing_review'])>4:ranges+='等'
            prefix='已采用主体拍值，但' if summary['status']=='applied' else '尚未采用新拍值，'
            warning=f'{prefix} {ranges} 的局部节奏仍需检查；未移动原始音符或踏板。'
            result.setdefault('warnings',[]).append(warning)
            summary['message']+=' '+warning
    except (OSError,ValueError,KeyError) as exc:
        # Prediction itself succeeded. A missing/uncertain independent prior
        # is not permission to fabricate a new clock or discard the score.
        summary.update(reason='prior_or_candidate_unavailable',message='拍值核对未能安全采用，保留当前拍点图。')
        audit['error']=str(exc)
    audit['summary']=summary
    from .pulse_evidence_store import store_result
    result['pulse_evidence']=store_result(project,state['source_sha256'],selected_evidence)
    folder=project/'cache'/'metrical-resolution'/config_key(audit)
    save_json(folder/'report.json',audit)
    summary['report_path']=str((folder/'report.json').resolve())
    result['metrical_resolution']=deepcopy(summary)
    result.setdefault('warnings',[]).extend(w for w in coordination_warnings if w not in result.get('warnings',[]))
    if summary['status']!='applied':
        result.setdefault('warnings',[]).append(summary['message'])
    log('[cubase_local] '+summary['message'])
    return {'tempo':result,'prepared_prediction':prepared,'summary':summary}
