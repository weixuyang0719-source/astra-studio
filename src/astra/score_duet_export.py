"""DuET notation with a persistent performance input and the project's clock."""
import json
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from copy import deepcopy

from .duet_score import ROOT, run_duet_score
from .release_paths import data_root, runtime_env_root
from .midi import write_midi
from .osmd_pdf import export_pdf_result


def _export_generated_score_pdf(xml, target, warnings, reference):
    """Use the same generated layout opening as the Score page for the PDF."""
    raw = xml.read_bytes()
    if b'astra-source-leading-silence' not in raw:
        return export_pdf_result(xml, target, warnings)
    from .score_opening_projection import project_score_opening
    from xml.etree.ElementTree import ParseError
    try:
        projected, origin = project_score_opening(raw, reference)
    except (OSError, ValueError, TypeError, LookupError, ParseError) as err:
        # An unprojectable print must not overwrite the canonical score/MIDI
        # or silently publish a PDF that contradicts the confirmed opening.
        warnings.append(f'Could not verify PDF score opening: {err}')
        return {'pdf': None, 'pdf_render': {'status': 'failed',
                'renderer': 'score-opening-projection', 'error': str(err)}}
    if not origin.beat:
        return export_pdf_result(xml, target, warnings)
    with tempfile.TemporaryDirectory(prefix='.score-opening-', dir=target.parent) as work:
        source = Path(work)/'piano.musicxml'
        source.write_bytes(projected)
        result = export_pdf_result(source, target, warnings)
    if result.get('pdf'):
        result['pdf_render'].update(canonical_sha256=origin.source_sha256,
                                    score_opening_beat=float(origin.beat),
                                    score_opening_seconds=origin.seconds)
    return result


def performance_identity(notes, pedals, controls=None):
    """Prediction depends on music, never UI IDs, locks or edit provenance.

    Keep the old audio-input key for unedited model notes/binary pedals so merely
    opening an editor does not invalidate a completed neural prediction.
    """
    identity={'notes':[{key: note[key] for key in ('pitch','start','end','velocity')} for note in notes],
              'pedals':sorted(
                  [{key: pedal[key] for key in ('number','start','end')} for pedal in pedals or []],
                  key=lambda pedal:(pedal['start'],pedal['end'],pedal['number']))}
    # Reconstructing held controllers emits intervals in release order, while
    # transcription bundles use onset order. That list order is not music and
    # must not invalidate inference merely by opening the editor. Raw CC order
    # (including multiple events at one tick) remains untouched below/export.
    if controls is not None:
        cc64=[{'seconds':float(c.get('seconds',c.get('time'))),
               'value':int(c['value']),'channel':int(c.get('channel',0))}
              for c in controls if c.get('number',c.get('control'))==64]
        expected=sorted((float(p[key]), value) for p in pedals or [] if p['number']==64
                        for key,value in (('start',127),('end',0)))
        actual=sorted((c['seconds'],c['value']) for c in cc64)
        if len({c['channel'] for c in cc64})>1 or actual!=expected:
            identity['cc64']=cc64
    return identity


def _performance_inputs(project, notes, pedals, controls=None):
    """Prefer the canonical key, retain a provably equivalent legacy input."""
    from .pipeline import config_key
    identity=performance_identity(notes,pedals,controls)
    root=Path(project)/'cache'/'duet_inputs'
    canonical=root/config_key(identity)
    if (canonical/'piano_raw.mid').is_file():
        return canonical
    # Older editor runs could cache release-ordered pedal intervals. This key
    # is derived from the exact current content under the old serialization,
    # not discovered by guessing among unrelated inputs or model predictions.
    legacy=dict(identity,pedals=[{k:p[k] for k in ('number','start','end')} for p in pedals or []])
    prior=root/config_key(legacy)
    return prior if (prior/'piano_raw.mid').is_file() else canonical


def _performance_digest(notes,pedals,controls):
    return hashlib.sha256(json.dumps(performance_identity(notes,pedals,controls),sort_keys=True).encode()).hexdigest()


def controller_events_digest(controls):
    """All ordered raw controller events, excluding editor identity/intent."""
    events=[{'seconds':float(c.get('seconds',c.get('time'))),
             'number':int(c.get('number',c.get('control'))),'value':int(c['value']),
             'channel':int(c.get('channel',0))} for c in controls or []]
    return hashlib.sha256(json.dumps(events,sort_keys=True).encode()).hexdigest()


def prepare_duet_prediction(notes,pedals,tempo_map,project,*,options=None,title=None,
                            log=print,source_midi=None,controls=None):
    """Prepare immutable performance input and one verified local prediction.

    This does not publish project state, an effective tempo map, or any exports.
    The returned receipt can be handed to export after automatic pulse-value
    selection, avoiding even a second prediction-worker invocation.
    """
    from .pipeline import save_json
    options=options or {}
    notes,pedals,controls,tempo_map=deepcopy((notes,pedals,controls,tempo_map))
    # Once made, this input follows the original performance forever. Changing
    # only the score clock or pedal visibility must not rerun model prediction.
    inputs=_performance_inputs(project,notes,pedals,controls)
    inputs.mkdir(parents=True,exist_ok=True)
    midi=inputs/'piano_raw.mid'
    if source_midi is not None:
        # Direct MIDI import supplies an immutable selected-track copy. Preserve
        # its PPQ, channel-local CC64, velocity and tempo for native inference.
        midi=Path(source_midi).resolve()
        if not midi.is_file():
            raise FileNotFoundError('缺少选定 MIDI 缓存，请重新导入源文件')
    elif not midi.exists():
        temporary=inputs/'piano_raw.pending.mid'
        write_midi(temporary,notes,tempo_map,'piano',pedals=pedals,controls=controls)
        temporary.replace(midi)
        save_json(inputs/'source-tempo.json',tempo_map)
    # With CUDA Graph step decoding, GPU runs ~4-8x faster than CPU with
    # bit-identical rows; honour explicit cpu override when configured.
    duet_device='cpu' if options.get('device')=='cpu' else (options.get('duet_device') or 'cuda')
    prediction=run_duet_score(midi,Path(project)/'cache'/'duet',
        device=duet_device,threads=(min(8,os.cpu_count() or 4) if duet_device=='cpu' else options.get('threads',2)),
        python=options.get('python'),title=title,log=log,
        cache_only=bool(options.get('_harmony_reuse_only')))
    manifest=Path(prediction['report']).parent/'astra-cache.json'
    return {'midi':str(midi.resolve()),'prediction':prediction,
            'performance_sha256':_performance_digest(notes,pedals,controls),
            'input_sha256':hashlib.sha256(midi.read_bytes()).hexdigest(),
            'prediction_manifest_sha256':hashlib.sha256(manifest.read_bytes()).hexdigest() if manifest.is_file() else None,
            'title':title,'scope':'pending_prediction','project_published':False}


def _reuse_prepared_prediction(prepared,notes,pedals,controls,title,source_midi=None):
    from .duet_score import _cached,_source_hash,duet_source_dir,SOURCE_COMMIT,WEIGHTS_SHA256,CONFIG_SHA256
    if prepared.get('performance_sha256')!=_performance_digest(notes,pedals,controls):
        raise ValueError('待导出的音符或控制器已变化，不能复用另一份预处理预测')
    midi=Path(prepared['midi']).resolve(strict=True)
    if source_midi is not None and midi!=Path(source_midi).resolve(strict=True):
        raise ValueError('预处理 MIDI 来源与本次导出不同')
    if prepared.get('title')!=title:
        raise ValueError('预处理标题与本次导出不同')
    if hashlib.sha256(midi.read_bytes()).hexdigest()!=prepared.get('input_sha256'):
        raise ValueError('预处理 MIDI 已变化')
    prediction=prepared['prediction'];directory=Path(prediction['report']).resolve().parent
    manifest=directory/'astra-cache.json';contents=manifest.read_bytes()
    if hashlib.sha256(contents).hexdigest()!=prepared.get('prediction_manifest_sha256'):
        raise ValueError('预处理预测清单已变化')
    identity=json.loads(contents)['identity']
    if (identity.get('input_sha256')!=prepared['input_sha256']
            or identity.get('source_commit')!=SOURCE_COMMIT
            or identity.get('source_sha256')!=_source_hash(duet_source_dir())
            or identity.get('weights_sha256')!=WEIGHTS_SHA256
            or identity.get('model_config_sha256')!=CONFIG_SHA256
            or not _cached(directory,identity)):
        raise ValueError('预处理预测不再匹配当前输入、模型或完整缓存')
    return midi,dict(prediction,cached=True)


def export_duet_score(notes, pedals, tempo_map, project, output, *, options=None, title=None, log=print, source_midi=None, controls=None, prepared_prediction=None, preview_only=False):
    from .pipeline import save_json
    options=options or {}
    if prepared_prediction is None:
        prepared=prepare_duet_prediction(notes,pedals,tempo_map,project,options=options,
            title=title,log=log,source_midi=source_midi,controls=controls)
        midi=Path(prepared['midi']);prediction=prepared['prediction']
    else:
        midi,prediction=_reuse_prepared_prediction(prepared_prediction,notes,pedals,controls,title,source_midi)
        log('[duet] 复用本次已验证预测，按统一生效速度图导出')
    # Explicit reviewed edits belong to this exact MIDI/prediction identity.
    # The notation worker rejects stale IDs, rather than applying them to a new import.
    revisions=options.get('notation_revisions')
    revision_path=Path(project)/'notation_revisions'/'piano.json'
    if revisions is None and not preview_only and revision_path.is_file():
        revisions=json.loads(revision_path.read_text(encoding='utf-8'))
    output=Path(output)
    # DuET writes on the current beat map. Its own first-pass map needs no new
    # run; any edited map is re-decoded with the map as a constraint (cached,
    # incremental). Quick preview uses the same path, so it shows the final score.
    notation_cache=Path(prediction['report']).parent
    dropped_revisions=None
    from .duet_beat_map import binding_matches
    if not binding_matches(tempo_map,hashlib.sha256((notation_cache/'score.npz').read_bytes()).hexdigest()):
        from .duet_constrained import constrained_prediction
        notation_cache=constrained_prediction(midi,notation_cache,tempo_map,project,options=options,log=log)
        constrained_sha=hashlib.sha256((notation_cache/'score.npz').read_bytes()).hexdigest()
        if revisions is not None and revisions.get('prediction_sha256')!=constrained_sha:
            dropped_revisions=len(revisions.get('notes',[]))
            revisions=None
            log(f'[review] {dropped_revisions} 处记谱审核修订针对的是旧写法；DuET 已按当前拍点重写，本次未套用，请在新谱上重新确认')
    with tempfile.TemporaryDirectory(prefix='duet-notation-',dir=output) as folder:
        folder=Path(folder)
        request={'raw_midi':str(midi),'duet_cache_dir':str(notation_cache),
            'tempo_map':tempo_map,'output_dir':str(folder/'result'),'title':title,
            'show_pedal':options.get('show_pedal',False),'grid':options.get('grid','auto')}
        # Inference inputs intentionally omit edit metadata. Carry manual
        # intent only into the cheap notation projection, never its model key.
        request['protected_note_onsets']=[{'pitch':int(n['pitch']),'seconds':float(n['start'])}
            for n in notes if n.get('origin')=='manual']
        request['protected_anchor_seconds']=list(options.get('_notation_fixed_anchor_seconds',[]))
        if revisions is not None:
            request['notation_revisions']=revisions
        request_path=folder/'request.json'
        save_json(request_path,request)
        runtime=runtime_env_root(ROOT)
        score_env='.venv-score-gpu' if runtime!=ROOT and options.get('device')=='cuda' else '.venv-score'
        from .model_worker_lease import windowless_python
        python=windowless_python(options.get('python') or str(runtime/score_env/'Scripts'/'python.exe'))
        if not Path(python).is_file():
            raise FileNotFoundError('请先运行 Install-Duet.cmd 安装本地制谱环境')
        env=os.environ.copy()
        env['PYTHONPATH']=os.pathsep.join(filter(None,(str(Path(__file__).resolve().parents[1]),env.get('PYTHONPATH'))))
        env['PYTHONIOENCODING']='utf-8'
        duet_cuda_cache=data_root()/'.cache'/'cuda-duet'
        duet_cuda_cache.mkdir(parents=True,exist_ok=True)
        env['CUDA_CACHE_PATH']=str(duet_cuda_cache)
        env['CUDA_CACHE_MAXSIZE']='1073741824'
        log('[duet] 按项目速度图整理乐谱，复用音符预测')
        from .preview_cancel import run_process, cancellation_active
        from .duet_score import run_duet_notation_via_daemon
        from .model_worker_lease import enabled as persistent_allowed
        src_root = str(Path(__file__).resolve().parents[1])
        duet_device='cpu' if options.get('device')=='cpu' else (options.get('duet_device') or 'cuda')
        daemon_res = None
        if not cancellation_active() and persistent_allowed():
            daemon_res = run_duet_notation_via_daemon(request, python, src_root, duet_device, env, log)
        if daemon_res is not None:
            if daemon_res[0]:
                raise RuntimeError('DuET 乐谱整理失败：' + str(daemon_res[1])[-2000:])
        else:
            process=run_process([python,'-u','-m','astra.duet_notation',str(request_path)],
                env=env,stdin=subprocess.DEVNULL,capture_output=True,text=True,encoding='utf-8',errors='replace',
                timeout=180,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            if process.returncode:
                raise RuntimeError('DuET 乐谱整理失败：'+(process.stderr or process.stdout)[-2000:])
        notation=json.loads((folder/'result'/'result.json').read_text(encoding='utf-8'))
        notation_report=json.loads(Path(notation['report']).read_text(encoding='utf-8'))
        if revisions is not None:
            save_json(output/'piano_notation_revisions.json',revisions)
        xml=output/'piano.musicxml'
        shutil.copy2(notation['musicxml'],xml)
        for field,name in (('report','piano_notation_report.json'),('review','piano_notation_review.json'),
                           ('timing_review','piano_timing_review.json')):
            if notation.get(field):
                shutil.copy2(notation[field],output/name)
        if notation_report.get('projection_prior'):
            notation_report['projection_prior_identity']={
                'version':2,'performance_sha256':_performance_digest(notes,pedals,controls),
                'source_events':performance_identity(notes,pedals,controls),
                'controls_sha256':controller_events_digest(controls),
                'controls_identity':'ordered_raw_events_v1',
                'input_midi':str(midi.resolve()),'input_sha256':notation_report['identity']['input_sha256'],
                'prediction_file':str(Path(prediction['report']).resolve().parent/'score.npz'),
                'prediction_sha256':notation_report['identity']['prediction_sha256'],
                'prior_sha256':hashlib.sha256(json.dumps(notation_report['projection_prior'],sort_keys=True).encode()).hexdigest()}
            save_json(output/'piano_notation_report.json',notation_report)
    from .notation_single_voice import simplify_staff_voices
    reviewed_midi = None
    reviewed_audit = None
    if revisions is not None and options.get('export_reviewed_midi', False):
        from .reviewed_midi import write_reviewed_midi
        reviewed_audit = write_reviewed_midi(midi, output/'piano_reviewed.mid', revisions)
        if (output/'piano_reviewed.mid').is_file():
            reviewed_midi = str(output/'piano_reviewed.mid')
            save_json(output/'piano_reviewed_midi.json', reviewed_audit)
            log('[review] 已另存审核修订 MIDI；原始演奏和量化 MIDI 保留，谱面时值修订仅用于乐谱')
    # Tempo playback uses balanced forward/backup directions for reader
    # compatibility. Temporarily detach only our own sequence so it cannot
    # prevent the conservative voice simplifier from recognizing note layouts.
    from .score_tempo import remove_generated_reference_clock, apply_reference_tempo
    remove_generated_reference_clock(xml)
    from .notation_rolls import apply_roll_groups
    roll_groups=notation_report.get('compact_model_rolls',{}).get('groups',[])
    # Two staves, locally independent stems when held notes overlap melody.
    # Apply model-confirmed rolls first: their staggered attacks can otherwise
    # masquerade as two independent lanes. Legacy strict is explicit only.
    if options.get('voice_mode')=='strict':
        voice_audit=simplify_staff_voices(xml,mode='simple')
        voice_audit['rolled_chords']=apply_roll_groups(xml,roll_groups)
    else:
        rolls=apply_roll_groups(xml,roll_groups,layout_mode='smart')
        if rolls.get('layout_passes'):
            # apply_roll_groups(..., layout_mode='smart') ends with a verified
            # simplify_staff_voices(xml, 'smart') pass over the final XML.
            voice_audit=dict(rolls['layout_passes'][-1])
        else:
            voice_audit=simplify_staff_voices(xml,mode='smart')
        voice_audit['rolled_chords']=rolls
    save_json(output/'piano_voice_layout.json',voice_audit)
    if voice_audit.get('mode') in ('simple','smart'):
        log(f'[score] 双手两行谱：{len(voice_audit.get("local_polyphonic_staff_bars",[]))} 个谱表小节保留独立持音与旋律，'
            f'隐藏 {voice_audit.get("covered_rests_hidden",0)} 个多余休止符')
    # Explicit engraving choices are project-persistent, but are revalidated
    # against this exact input/model and new score geometry on every export.
    # Never reuse an old annotated XML or silently discard an obsolete choice.
    from .notation_ornament_service import apply_to_export, read_overlay, assert_revision
    ornament_prediction=Path(prediction['report']).parent/'score.npz'
    ornament_audit=None
    if not preview_only and ornament_prediction.is_file():
        ornament_audit=apply_to_export(project,xml,midi,ornament_prediction,tempo_map,
            context=options.get('_ornament_context'),
            protected_note_onsets=request['protected_note_onsets'])
        log(f'[score] 装饰引音候选 {ornament_audit["candidate_count"]} 处；仅采用已确认且仍有效的记谱决定')
    elif not preview_only and read_overlay(project)['decisions']:
        raise ValueError('已有装饰记谱决定，但本次预测证据缺失；保留决定并停止，避免静默丢失')
    from .harmony import analyze_harmony, apply_key_confirmation
    from .notation_readability import sounding_signature
    log('[harmony] 分析钢琴调性和和弦，保留既有调号变化')
    annotated=output/'piano.harmony-pending.musicxml'
    original_signature=sounding_signature(xml)
    audio_harmony=options.get('_audio_harmony')
    harmony=analyze_harmony(xml,output_path=annotated,write_modes=True,
        write_chords=options.get('show_chords',False) and audio_harmony is None)
    if audio_harmony is not None:
        from .harmony_export import apply_prediction
        apply_prediction(harmony,audio_harmony,tempo_map,musicxml=annotated,
            show_chords=options.get('show_chords',False),notation_report=notation_report)
    if sounding_signature(annotated)!=original_signature:
        raise RuntimeError('和声标注改变了音符时间，已停止导出')
    annotated.replace(xml)
    harmony['musicxml_path']=str(xml)
    apply_key_confirmation(harmony,options.get('confirmed_key'))
    source_review=json.loads((output/'piano_notation_review.json').read_text(encoding='utf-8'))
    harmony['notation_review']=[{
        'kind':item['kind'],'bars':item.get('bars'),
        'message':('调号变化也可能是借用和弦，需要结合前后乐句核对。' if item['kind']=='key-borrowed'
                   else '临时升降号的等音拼写存在歧义，需要核对。'),
        'evidence':item.get('evidence'),'question':item.get('question')}
        for item in source_review.get('items',[]) if item.get('kind') in ('key-borrowed','spelling')]
    harmony['notation_review'] += [{'kind':'short_barline_tail','bars':item['measure'],
        'message':f"跨小节音尾延续 {item['end_beat_in_bar']:g} 拍，已保留时值；需要试听核对是否应在小节线上结束。"}
        for item in notation_report.get('readability',{}).get('short_barline_tails',[])]
    harmony_file=output/'piano_harmony.json'
    save_json(harmony_file,harmony)
    # All sources, including automatic Beat This clocks, use the same final
    # reference for XML playback and score MIDI. Do this after structural and
    # harmony edits so reader round-trips cannot move tempo events. Printed
    # marks remain separately controlled by notation_tempo_marks.
    final_signature=sounding_signature(xml)
    reference_clock=apply_reference_tempo(xml,tempo_map)
    if sounding_signature(xml)!=final_signature:
        raise RuntimeError('参考速度写入改变了谱面音符时间，已停止导出')
    # Explicitly group complete mixed triplets before all native consumers.
    from .notation_tuplet_groups import disambiguate_generated_triplets
    disambiguate_generated_triplets(xml)
    if preview_only:
        return {'musicxml':str(xml),'prediction_cached':prediction['cached']}
    from concurrent.futures import ThreadPoolExecutor
    from .score_midi import write_score_midi
    score_midi=output/'piano_score.mid'
    warnings=list(reference_clock.get('rhythm_annotations',{}).get('warnings',[]))
    if dropped_revisions:
        warnings.append(f'{dropped_revisions} 处记谱审核修订是针对旧写法做的；DuET 已按当前拍点重写谱面，这些修订本次未套用，修订文件保留未删。')
    if ornament_audit and any(item['decision']=='accept' for item in ornament_audit['pending_review']):
        warnings.append('部分已采用的装饰记谱决定不再满足本次输入/拍图候选条件，已保留历史待复核，本次未强行套用；见 piano_ornament_review.json。')
    uncertain=notation_report.get('uncertain_ratio_intervals',0)
    if uncertain:
        affected=notation_report.get('affected_note_count',0)
        warnings.append(f'DuET 有 {uncertain} 个实际涉及音符的拍区间仍需核对时值倍率（涉及 {affected} 个输入音符）；上下文已确认及无音符区间已另列，详见 piano_timing_review.json。')
    # Native score display/print and score MIDI are read-only consumers of xml;
    # run them concurrently to overlap MuseScore startup with MIDI writing.
    pdf_warnings=list(warnings)
    from .preview_cancel import cancellation_active
    if cancellation_active():
        score_midi_audit=write_score_midi(xml,tempo_map,score_midi,performance_notes=notes,
                                         pedals=pedals, controls=controls)
        render=_export_generated_score_pdf(xml,output/'piano.pdf',pdf_warnings,tempo_map)
    else:
        with ThreadPoolExecutor(max_workers=2) as pool:
            pdf_future=pool.submit(_export_generated_score_pdf,xml,output/'piano.pdf',pdf_warnings,tempo_map)
            score_midi_audit=write_score_midi(xml,tempo_map,score_midi,performance_notes=notes,
                                             pedals=pedals, controls=controls)
            render=pdf_future.result()
    warnings=pdf_warnings
    if ornament_audit:
        from .notation_ornaments import validate_group_playback
        ornament_catalog=json.loads(Path(ornament_audit['catalog']).read_text(encoding='utf-8'))
        score_midi_audit['ornament_group_playback']=validate_group_playback(
            ornament_catalog,ornament_audit['engraving']['accepted_ids'],score_midi_audit,tempo_map)
    score_midi_audit['reference_clock']=reference_clock
    save_json(output/'piano_score_midi.json',score_midi_audit)
    log('[score] 已导出记谱校正版 MIDI：左右手两轨，使用谱面拍位与参考变速')
    if ornament_audit:
        assert_revision(project,ornament_audit)
    return {'musicxml':str(xml),'warnings':warnings,**render,
            'score_engine':'duet','prediction_cached':prediction['cached'],
            'harmony':str(harmony_file),'voice_layout':voice_audit,
            'reviewed_midi':reviewed_midi,'reviewed_midi_audit':reviewed_audit,
            'score_midi':str(score_midi),'score_midi_audit':score_midi_audit,
            'ornament_review':ornament_audit}
