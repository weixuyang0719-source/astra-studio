"""Conservative, read-only readiness for a cache-only desktop reexport."""
from pathlib import Path
from copy import deepcopy
import math


def cached_reexport_ready(request, state=None):
    if request.get('kind')!='reexport':return False
    from . import pipeline,editing
    from .desktop_worker import chord_inference_needed
    try:
        project=Path(request['project'])
        state=state if state is not None else pipeline.read_json(project/'project.json')
        if chord_inference_needed(request,state):return False
        options=request.get('options') or {}
        captured=(request.get('editor_export_options') or {}).get('_working_input')
        editor=None
        if captured is not None:
            # The worker has no GUI owner. Compare the packet's own revision
            # and source, not the last formal save; an unsaved edit is valid.
            from . import editor_checkpoint as cp
            editor,guard=pipeline._reexport_working_document(project,captured,request.get('editor_revision'),
                tempo_path=options.get('tempo_path'),bpm=options.get('bpm'),beat_scale=options.get('beat_scale',1.))
            if not cp.permits_export(project,guard['guard'],guard['applied_sequence']):return False
        if 'piano' not in state.get('instruments',[]):return True
        if options.get('mode','all')=='midi':return True
        score=state.get('config',{}).get('score',{})
        if (request.get('score_engine') or score.get('piano_engine','duet'))!='duet':return True
        if captured is None:
            editor,_=pipeline._reexport_working_document(project,None,request.get('editor_revision'))
        source_midi=None
        if editor is not None:
            notes=editor['notes'];controls=editor['controls']
            pedals=editing.pedal_intervals(controls,editor['duration'])
        else:
            keys=state.get('analysis_keys') or pipeline._analysis_keys(state.get('config',{}))
            bundle=pipeline._notes_bundle(project,state,'piano',keys['piano'])
            if bundle is None:return False
            notes=bundle['notes'];controls=bundle.get('controls');pedals=bundle.get('pedals',[])
            if state.get('source_kind')=='midi':source_midi=state['selected_midi']
        if not notes:return True
        from .score_duet_export import _performance_inputs
        from .duet_score import cached_prediction
        pedals=list(pipeline._pedals_in_duration(pedals,state['duration']))
        midi=Path(source_midi) if source_midi else _performance_inputs(project,notes,pedals,controls)/'piano_raw.mid'
        if not midi.is_file():return False
        title=Path(state['source']).stem.split(' - ',1)[-1]
        return cached_prediction(midi,project/'cache'/'duet',title=title) is not None
    except (OSError,ValueError,KeyError,TypeError,AttributeError):
        # An uncertain cache never grants implicit CPU/GPU execution.
        return False


def cached_run_ready(request, config=None):
    """Read-only conservative full-run fast path; never permission to infer.

    An explicit config is the FINAL run config, not a partially merged UI
    request. Missing config uses the same pure helper as the desktop worker.
    Cross-project copies, migrations, and new beat decoding deliberately fall
    outside this first fast path. Execution must still use cache_only guards.
    """
    if request.get('kind')!='run':return False
    from . import pipeline,editing
    from .rhythm_provider import reusable_auto_map
    from .timing import TempoMap,fixed_tempo,scale_tempo
    from .auto_pulse_resolution import eligible
    try:
        if config is None:
            from .run_config import prepare_run_config
            config=prepare_run_config(request,pipeline.load_config(),pipeline.ROOT)
        else:config=deepcopy(config)
        instruments=request['instruments']
        if not instruments or any(i not in ('piano','guitar') for i in instruments):return False
        source=Path(request['audio']).resolve(strict=True);digest=pipeline.fingerprint(source)
        index=pipeline.ROOT/'.cache'/'projects'/f'{digest}.json'
        indexed,indexed_state=pipeline._indexed_project(index,digest)
        if indexed is None:
            default=pipeline._project_for_source(pipeline.ROOT/'outputs',source,digest,None)
            indexed_state=pipeline._project_state(default,digest)
            if indexed_state:indexed=default
        project=pipeline._project_for_source(Path(request['output']).resolve(),source,digest,indexed)
        previous=pipeline._project_state(project,digest)
        if previous is None or previous.get('source_kind')=='midi':return False
        if Path(previous['source']).resolve()!=source:return False
        if 'analysis_keys' not in previous:return False  # No legacy migration in a readiness probe.
        duration=float(previous['duration'])
        if not math.isfinite(duration) or duration<=0:return False
        if source.suffix.lower()=='.ncm':
            from .audio_import import VERSION
            folder=pipeline.ROOT/'.cache'/'imports'/'ncm'/digest
            imported=pipeline.read_json(folder/'import.json')
            if imported['audio_file'] not in ('audio.flac','audio.mp3'):return False
            if (imported['version']!=VERSION or imported['source_sha256']!=digest
                    or pipeline.fingerprint(folder/imported['audio_file'])!=imported['audio_sha256']):return False
            if Path(previous.get('source_audio','')).resolve()!=(folder/imported['audio_file']).resolve():return False
        from .stage_cache_reuse import resolve_keys
        keys,_=resolve_keys(project,previous,config,instruments,
                            request.get('keep_stems',True) or 'piano' not in instruments)
        seed_keys=previous['analysis_keys']
        options=request.get('options',{});mode=options.get('mode','all')
        if mode not in ('all','midi','score'):return False
        editor=editing.current(project) if 'piano' in instruments else None
        if request.get('editor_revision') is not None:editing._check_base(editor,request['editor_revision'])
        tempo_kind='manual';tempo=None
        if 'piano' in instruments:
            scale=options.get('beat_scale',1.)
            if options.get('tempo_path'):
                tempo=scale_tempo(pipeline._load_tempo(options['tempo_path'],duration),scale)
            elif options.get('bpm') is not None:
                tempo=scale_tempo(fixed_tempo(options['bpm'],duration,options.get('meter','4/4')),scale)
            else:
                seed_tempo=pipeline._committed_tempo(project,previous)
                reuse=(seed_tempo.get('source')!='not_applicable' and (
                    previous.get('tempo_kind','manual')!='auto'
                    or seed_tempo.get('source') not in ('beat_this','cubase_local_reconstruction','duet_first_pass')
                    or reusable_auto_map(seed_tempo,keys['beat_this'],seed_keys.get('beat_this'))))
                if not reuse:return False
                tempo=scale_tempo(seed_tempo,scale)
                tempo_kind=previous.get('tempo_kind','manual') if scale==1 else 'manual'
            TempoMap(tempo)
        bundle=None
        if 'piano' in instruments:
            bundle=pipeline._notes_bundle(project,previous,'piano',keys['piano'])
            if bundle is None:return False
            pipeline.validate_notes(bundle['notes'])
        stems={};keep=request.get('keep_stems',True) or 'piano' not in instruments
        required=instruments if keep else (['guitar'] if 'guitar' in instruments else [])
        for name in required:
            frozen=project/'stems'/keys['separator']/f'{name}.wav'
            if pipeline._wave_complete(frozen,duration):stems[name]=str(frozen);continue
            local=pipeline.stem_path(project,previous,name)
            if (local and previous.get('stem_keys',{}).get(name)==keys['separator']
                    and pipeline._wave_complete(local,duration)):
                stems[name]=str(local);continue
            return False  # Do not archive staged files or relocate external stems here.
        if 'piano' not in instruments:return True
        state={**previous,'instruments':instruments,'config':config,'analysis_keys':dict(keys),
               'tempo_kind':tempo_kind,'stem_paths':stems}
        # Reused manual maps retain their ACTUAL analysis key, not the newly
        # requested provider key. No beat model is invoked in this branch.
        state['analysis_keys']['beat_this']=seed_keys.get('beat_this')
        from .score_duet_export import _performance_inputs
        from .duet_score import cached_prediction
        title=source.stem.split(' - ',1)[-1]
        def duet_ready(notes,pedals,controls):
            if not notes:return True
            pedals=list(pipeline._pedals_in_duration(pedals,duration))
            midi=_performance_inputs(project,notes,pedals,controls)/'piano_raw.mid'
            return midi.is_file() and cached_prediction(midi,project/'cache'/'duet',title=title) is not None
        if eligible(state,tempo,mode,editor):
            # The automatic prior uses raw bundle identity even if later score
            # export uses an editor document. Both inputs must be checked.
            if not duet_ready(bundle['notes'],bundle.get('pedals',[]),bundle.get('controls')):return False
            beat=pipeline.read_json(project/'cache'/f'beats-{keys["beat_this"]}.json')
            audio=project/'cache'/'source.wav'
            if (beat.get('provider')!='cubase_local_reconstruction' or not audio.is_file()
                    or pipeline.fingerprint(audio)!=beat.get('source_sha256')):return False
        if editor is not None and editor.get('manual'):
            notes,controls=editor['notes'],editor['controls']
            pedals=editing.pedal_intervals(controls,editor['duration'])
        elif editor is not None:
            # refresh_candidate replaces an unedited viewer with this bundle.
            notes=bundle['notes'];controls=deepcopy(bundle.get('controls') or [])
            if not controls:
                for p in bundle.get('pedals',[]):
                    controls.extend({'seconds':p[key],'number':p['number'],'value':value,'channel':p.get('channel',0)}
                        for key,value in (('start',p.get('value',127)),('end',0)))
            pedals=editing.pedal_intervals(controls,duration)
        else:notes,controls,pedals=bundle['notes'],bundle.get('controls'),bundle.get('pedals',[])
        pipeline.validate_notes(notes)
        score=config.get('score',{})
        if mode!='midi' and score.get('piano_engine','duet')=='duet' and not duet_ready(notes,pedals,controls):return False
        if notes:
            stem=stems.get('piano');engine=score.get('harmony_engine')
            if not stem:
                if score.get('show_chords') or engine in ('chordformer','tsumugi'):return False
            elif engine=='tsumugi':
                from .tsumugi_chords import cached_prediction as tsumugi_cached
                report=pipeline.read_json(Path(previous['latest_export'])/'piano_harmony.json')
                if not tsumugi_cached(stem,project/'cache'/'tsumugi',report.get('audio_model',{}).get('cache_key')):return False
            elif score.get('show_chords') or engine=='chordformer':
                from .chordformer import cache_is_available
                if not cache_is_available(stem,project/'cache'/'chordformer'):return False
        return True
    except (OSError,ValueError,KeyError,TypeError,AttributeError,IndexError,RuntimeError):
        return False
