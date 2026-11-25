"""Resumable local pipeline. Expensive stages never depend on the tempo edits."""
from .release_paths import data_root
from contextlib import contextmanager
from pathlib import Path
import hashlib
import json
import math
import os
import shutil
import subprocess
import tempfile
import time
import wave
from .timing import TempoMap, fixed_tempo, from_beats, quantize_notes, scale_tempo, BEAT_MAPPING_VERSION
from .midi import write_midi, import_tempo_midi

ROOT=data_root()


def read_json(path):
    path=Path(path)
    if path.name == 'project.json':
        from .score_commit import read_project
        return read_project(path)
    return read_raw_json(path)


def read_raw_json(path):
    path=Path(path)
    if os.name=='nt' and path.name=='current.json' and path.parent.name=='editing':
        from .editor_head_io import open_reader
        with open_reader(path) as reader:
            return json.loads(reader.read().decode('utf-8-sig'))
    return json.loads(path.read_text(encoding='utf-8-sig'))


def _replace_editor_json(temporary, path):
    if os.name == 'nt':
        from .editor_head_io import publish
        publish(temporary,path,mutable=path.name=='current.json' and path.parent.name=='editing')
    else:
        temporary.replace(path)
        # Order the body (including a new revisions directory) before the head;
        # also persist the directory entry for editing on initial publication.
        for directory in (path.parent,path.parent.parent):
            fd=os.open(directory,os.O_RDONLY | getattr(os,'O_DIRECTORY',0))
            try: os.fsync(fd)
            finally: os.close(fd)


def save_json(path, data):
    path=Path(path)
    path.parent.mkdir(parents=True,exist_ok=True)
    head=path.name=='current.json' and path.parent.name=='editing'
    body=path.suffix=='.json' and path.parent.name=='revisions' and path.parent.parent.name=='editing'
    if head or body:
        # No indent selects CPython's _json encoder. Same JSON values, key order,
        # escaping policy, and nonfinite rejection; no change to the disk schema.
        payload=json.dumps(data,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode('utf-8')
        temporary=None
        preserve_temporary=False
        try:
            with tempfile.NamedTemporaryFile(dir=path.parent,delete=False) as handle:
                temporary=Path(handle.name)
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            _replace_editor_json(temporary,path)
        except Exception as error:
            preserve_temporary=bool(getattr(error,'preserve_editor_temporary',False))
            raise
        finally:
            if temporary is not None and not preserve_temporary: temporary.unlink(missing_ok=True)
        return
    with tempfile.NamedTemporaryFile(dir=path.parent,delete=False) as handle:
        temporary=Path(handle.name)
    try:
        temporary.write_text(json.dumps(data,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def fingerprint(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda:f.read(4*1024*1024),b''):
            h.update(block)
    return h.hexdigest()


def config_key(config):
    return hashlib.sha256(json.dumps(config,sort_keys=True,default=str).encode()).hexdigest()[:12]


def _analysis_keys(config, *, separator_key=None):
    from .rhythm_provider import analysis_identity
    models=config.get('models',{})
    options={name:models.get(name,{}) for name in ('separator','beat_this','transkun','basic_pitch')}
    model_dir=config.get('model_dir','models')
    separator=separator_key or config_key([1,options['separator'].get('model_dir',model_dir),options['separator']])
    return {
        'separator':separator,
        # Historical schema slot; identity includes the actual provider.
        'beat_this':config_key(analysis_identity(config)),
        'piano':config_key([1,separator,options['transkun']]),
        'guitar':config_key([1,separator,options['basic_pitch']]),
    }


def _meter_mapping_current(tempo):
    """Invalidate derived DBN maps, never their cached audio detections."""
    from .meter_decode import METER_DECODER_VERSION
    version = tempo.get('meter_decoder_version')
    diagnostics = tempo.get('meter_diagnostics') or {}
    fallback = tempo.get('meter_decoder_fallback') or {}
    is_dbn_mapping = (version is not None
                      or str(diagnostics.get('method', '')).startswith('dbn_')
                      or fallback.get('candidate') is not None)
    return not is_dbn_mapping or version == METER_DECODER_VERSION


def _project_state(project, digest):
    path=project/'project.json'
    if path.is_file():
        state=read_json(path)
        if state.get('source_sha256')==digest and state.get('latest_export'):
            return state
    return None


def _indexed_project(index, digest):
    try:
        project=Path(read_json(index)['project']).resolve()
        state=_project_state(project,digest)
        return (project,state) if state else (None,None)
    except (OSError,ValueError,KeyError,TypeError):
        return None,None


def _project_for_source(output_root, source, digest, indexed):
    prefix=source.stem[:60]+'-'+digest[:10]
    if indexed and indexed.parent==output_root:
        return indexed
    target=output_root/prefix
    if target.exists() or not output_root.exists():
        return target
    # Earlier versions put the whole model configuration in the directory name.
    for candidate in sorted(output_root.iterdir()):
        if candidate.is_dir() and candidate.name.startswith(prefix+'-') and _project_state(candidate,digest):
            return candidate
    return target


def _workflow_directory(output_root, value):
    """Resolve a task-owned direct child created by the desktop controller."""
    project=Path(value)
    owner_path=project/'.astra-workflow-owner.json'
    if (not project.is_absolute() or project.resolve()!=project or
            project.parent!=output_root or not project.is_dir() or owner_path.is_symlink()):
        raise ValueError('任务目录已变化，无法继续当前处理。')
    owner=read_json(owner_path)
    identifier=owner.get('id') if isinstance(owner,dict) else None
    if (not isinstance(identifier,str) or len(identifier)!=32 or
            any(c not in '0123456789abcdef' for c in identifier) or
            owner.get('version')!=1 or owner.get('project')!=str(project) or
            not project.name.endswith('-'+identifier[:12])):
        raise ValueError('无法确认当前任务目录归属。')
    return project


def _notes_path(project, instrument, key):
    return project/'notes'/f'{instrument}-{key}.json'


def _notes_bundle(project, state, instrument, key):
    keyed=_notes_path(project,instrument,key)
    if keyed.is_file():
        return read_json(keyed)
    if state and 'analysis_keys' not in state and _analysis_keys(state.get('config',{}))[instrument]==key:
        notes=project/'notes'/f'{instrument}.json'
        pedals=project/'notes'/f'{instrument}_pedals.json'
        if notes.is_file():
            return {'notes':read_json(notes),'pedals':read_json(pedals) if pedals.is_file() else []}
    return None


def _committed_tempo(project, state):
    latest=state.get('latest_export')
    snapshot=project/'exports'/Path(latest).name/'tempo_map.json' if latest else None
    return read_json(snapshot if snapshot and snapshot.is_file() else project/'tempo_map.json')


def _wav_frames(path):
    with wave.open(str(path),'rb') as handle:
        frames=handle.getnframes()
        if frames<=0:
            raise ValueError(f'WAV 音频没有采样帧：{path}')
        handle.setpos(frames-1)
        if len(handle.readframes(1))!=handle.getnchannels()*handle.getsampwidth():
            raise ValueError(f'WAV 音频不完整，实际采样帧少于文件头声明：{path}')
        return frames,handle.getframerate()


def _wave_complete(path, duration):
    try:
        with wave.open(str(path),'rb') as audio:
            frames=audio.getnframes()
            if frames<=0 or audio.getframerate()!=44100 or frames!=round(duration*44100):
                return False
            audio.setpos(frames-1)
            return len(audio.readframes(1))==audio.getsampwidth()*audio.getnchannels()
    except (EOFError,OSError,ValueError,wave.Error):
        return False


def _atomic_copy(source, target):
    with tempfile.NamedTemporaryFile(dir=target.parent,delete=False) as handle:
        temporary=Path(handle.name)
    try:
        shutil.copy2(source,temporary)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)


def stem_path(project, state, instrument):
    """Resolve the committed audio, never substitute a stale compatibility WAV."""
    if 'stem_paths' in state:

        value=state['stem_paths'].get(instrument)
        from .project_access import reference
        return reference(project,value)
    from .project_access import approved, reference
    legacy=Path(project)/f'{instrument}.wav'
    return reference(project,legacy) if approved(project) is not None else legacy


def _freeze_stem(project, instrument, key, source, duration):
    """Publish one immutable model-versioned WAV before committing its export."""
    target=project/'stems'/key/f'{instrument}.wav'
    if _wave_complete(target,duration):
        return target
    if target.exists():
        # A damaged old file may still be open. Never replace any published URI.
        target=target.with_name(f'{instrument}-{fingerprint(source)[:16]}.wav')
    if not _wave_complete(target,duration):
        target.parent.mkdir(parents=True,exist_ok=True)
        _atomic_copy(source,target)
    if not _wave_complete(target,duration):
        raise RuntimeError(f'无法保存完整的 {instrument} 分轨快照')
    return target


def _compatibility_stem(source, target):
    """A best-effort legacy alias. Hard links avoid duplicating large WAVs."""
    with tempfile.NamedTemporaryFile(dir=target.parent,delete=False) as handle:
        temporary=Path(handle.name)
    try:
        temporary.unlink()
        try:
            os.link(source,temporary)
        except OSError:
            shutil.copy2(source,temporary)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)


def _snapshot_legacy_stems(project, state):
    """Bind the last known legacy export before its root aliases can change."""
    if not state or 'stem_paths' in state or state.get('source_kind')=='midi':
        return {}
    keys=state.get('stem_keys',{})
    fallback=(state.get('analysis_keys') or _analysis_keys(state.get('config',{})))['separator']
    paths={}
    for instrument in state.get('instruments',[]):
        source=project/f'{instrument}.wav'
        if _wave_complete(source,state['duration']):
            paths[instrument]=str(_freeze_stem(project,instrument,keys.get(instrument,fallback),source,state['duration']))
    return paths


def _archive_staged_stems(project, separation, manifest, duration, log):
    """Protect completed retained audio before another model reuses staging.

    A legacy failed run can have a complete WAV and keyed notes but no committed
    stem_paths yet. Its manifest is the model identity; never relabel that WAV
    with the model selected by the next successful run.
    """
    key=manifest.get('key')
    if not isinstance(key,str) or len(key)!=12 or any(c not in '0123456789abcdef' for c in key):
        return {}
    retained=manifest.get('retain_instruments',manifest.get('instruments',[]))
    paths={}
    for instrument in manifest.get('instruments',[]):
        if instrument not in ('piano','guitar') or instrument not in retained:
            continue
        source=separation/f'{instrument}.wav'
        if _wave_complete(source,duration):
            paths[instrument]=_freeze_stem(project,instrument,key,source,duration)
    if paths:
        log('已保留完成分轨的模型版本快照；切换模型或重试不会清掉该结果')
    return paths


@contextmanager
def project_lock(project):
    from contextlib import ExitStack
    from .process_locks import owned_lock, LockUnavailable
    with ExitStack() as stack:
        try:
            stack.enter_context(owned_lock(Path(project)/'.running'))
        except LockUnavailable as exc:
            raise RuntimeError('检测到无法确认的旧任务锁，已保留工程数据；请关闭旧版程序后检查锁来源。') from exc
        except OSError as exc:
            raise RuntimeError('工程正在处理，或当前无法取得写入权限，请稍后重试。') from exc
        yield


def load_config():
    from .rhythm_provider import new_task_config
    path=ROOT/'config.json'
    return new_task_config(read_json(path) if path.exists() else {},ROOT)


def decode_audio(source, target, log):
    import imageio_ffmpeg
    if source.suffix.lower()=='.ncm':
        from .audio_import import prepare_audio_import
        source=Path(prepare_audio_import(source,ROOT/'.cache'/'imports',log)['path'])
    if source.suffix.lower()=='.wav':
        try:
            _wav_frames(source)
        except (EOFError,wave.Error):
            # Python 3.10's wave module only understands PCM WAV. FFmpeg handles other encodings.
            pass
    log('解码音频：保持原始起点，转为 44.1 kHz 立体声')
    temporary=target.with_name('source.partial.wav')
    command=[imageio_ffmpeg.get_ffmpeg_exe(),'-nostdin','-v','error','-xerror','-y','-i',str(source),
        '-vn','-ar','44100','-ac','2','-c:a','pcm_s16le',str(temporary)]
    try:
        proc=subprocess.run(command,capture_output=True,text=True,creationflags=0x08000000 if os.name=='nt' else 0)
        if proc.returncode:
            raise RuntimeError('音频解码失败：'+proc.stderr[-2000:])
        _wav_frames(temporary)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)


def validate_notes(notes):
    for n in notes:
        if (not all(math.isfinite(float(n[k])) for k in ('start','end','pitch','velocity')) or
            not 0<=n['start']<n['end'] or not 0<=n['pitch']<=127 or not 1<=n['velocity']<=127):
            raise ValueError('模型返回无效音符，停止导出以避免损坏 MIDI')
    return notes


def _pedals_in_duration(pedals,duration):
    # Transkun can predict a pedal interval in its padding beyond the real audio.
    for pedal in pedals:
        start,end=float(pedal['start']),float(pedal['end'])
        if not math.isfinite(start) or not math.isfinite(end):
            raise ValueError('模型返回无效踏板时间')
        start,end=max(0.,start),min(duration,end)
        if start<end:
            yield dict(pedal,start=start,end=end)


def _load_tempo(path,duration):
    data=import_tempo_midi(path,duration) if Path(path).suffix.lower() in ('.mid','.midi') else read_json(path)
    TempoMap(data)
    return data


def _export(project, state, tempo, mode, log, *, harmony_reuse_only=False, prepared_prediction=None, cache_only=False, notes_draft=None):
    from . import editing
    working_input=state.pop('_editor_input',None)
    editor=(working_input if working_input is not None else editing.current(project)) if 'piano' in state.get('instruments',[]) else None
    expected_revision=state.pop('_expected_editor_revision',None)
    if expected_revision is not None:editing._check_base(editor,expected_revision)
    if editor is not None:
        # Detection may produce a candidate, but cannot overwrite the working map/events.
        tempo=editor['tempo']
        state['_editor_revision']=editor['revision']
        state['_editor_tempo']=tempo
    TempoMap(tempo)
    if mode not in ('all','midi','score'):
        raise ValueError('未知输出模式')
    log('正在导出结果文件')
    # Each export has its own directory: old successful files cannot masquerade
    # as the current result after a partial error or a different output mode.
    if notes_draft is not None:
        from .provisional_score import draft_directory
        export=draft_directory(project,notes_draft)
    else:
        export=project/'exports'/time.strftime('%Y%m%d-%H%M%S')
        suffix=0
        while export.exists():
            suffix+=1;export=project/'exports'/(time.strftime('%Y%m%d-%H%M%S')+f'-{suffix}')
    export.mkdir(parents=True)
    has_piano='piano' in state['instruments']
    if has_piano:
        save_json(export/'tempo_map.json',tempo)
    report={'status':'running','editor_revision':editor['revision'] if editor else None,
        'warnings':list(tempo.get('warnings',[])) if has_piano else [],'files':[],'instruments':{},
        'stem_paths':{name:path for name,path in state.get('stem_paths',{}).items() if name in state['instruments']},
        'stem_keys':{name:key for name,key in state.get('stem_keys',{}).items() if name in state['instruments']}}
    save_json(export/'report.json',report)
    try:
        keys=state.get('analysis_keys') or _analysis_keys(state.get('config',{}))
        selected_input=state.get('selected_midi')
        if has_piano and state.get('source_kind')=='midi':
            from . import native_score_runtime as native_score
            if native_score.enabled():
                selected_input=str(native_score.selected_midi(project,state))
        if has_piano and mode in ('all','midi'):
            write_midi(export/'tempo.mid',[],tempo)
            report['files'].append('tempo.mid')
        for instrument in state['instruments']:
            if instrument=='guitar':
                report['instruments']['guitar']={'mode':'stem_only','notes':None,
                    'message':'吉他当前仅提供分轨、试听和音频导出；自动 MIDI 与制谱暂停。'}
                continue
            bundle=_notes_bundle(project,state,instrument,keys[instrument])
            if bundle is not None and state.get('source_kind')=='midi':
                # Imports retain the complete controller stream, not only binary pedal intervals.
                import mido
                seconds=0.;controls=[];other_events=[]
                for message in mido.MidiFile(selected_input):
                    seconds+=message.time
                    if message.type=='control_change':
                        controls.append({'seconds':seconds,'number':message.control,'value':message.value,'channel':message.channel})
                    elif message.type not in ('note_on','note_off','set_tempo','time_signature','track_name','end_of_track'):
                        payload=message.dict();payload.pop('time',None)
                        other_events.append({'seconds':seconds,'message':payload})
                bundle={**bundle,'controls':controls,'other_events':other_events}
            original_bundle=bundle
            if editor is not None and instrument=='piano':
                bundle={'notes':editor['notes'],'controls':editor['controls'],
                    'other_events':editor.get('other_events',[]),
                    'pedals':editing.pedal_intervals(editor['controls'],editor['duration'])}
            if bundle is None:
                raise FileNotFoundError(f'缺少 {instrument} 的原始音符缓存')
            notes=validate_notes(bundle['notes'])
            audio_harmony=None
            # Performance MIDI export must not require a notation/chord model cache.
            if mode in ('all','score') and instrument=='piano' and notes and state.get('source_kind')!='midi':
                from .harmony_export import prepare_audio_harmony
                audio_harmony=prepare_audio_harmony(project,state,log,reuse_only=harmony_reuse_only or cache_only,
                    **({'strict_cache_only':True} if cache_only else {}))
            quantized=quantize_notes(notes,TempoMap(tempo),state.get('config',{}).get('quantization','auto'))
            save_json(export/f'{instrument}_quantized_notes.json',quantized)
            shifts=[abs(n['quantization_shift_seconds']) for n in quantized]
            end_shifts=[{'pitch':n['pitch'],'raw_end_seconds':n['end'],
                'quantized_end_seconds':n['end']+n['quantization_end_shift_seconds'],
                'shift_seconds':n['quantization_end_shift_seconds']}
                for n in quantized if abs(n['quantization_end_shift_seconds'])>.08]
            report['instruments'][instrument]={'notes':len(notes),'large_quantization_shifts':sum(s>.08 for s in shifts),
                'large_quantization_end_shifts':len(end_shifts),'large_quantization_end_shift_details':end_shifts}
            if not notes:
                report['warnings'].append(f'{instrument} 未识别到音符，请试听分轨或检查输入')
            if mode in ('all','midi'):
                pedals=_pedals_in_duration(bundle['pedals'],state['duration'])
                original_clock=read_json(project/'source_tempo_map.json') if (project/'source_tempo_map.json').is_file() else None
                unchanged_import_clock=original_clock is not None and all(original_clock.get(k)==tempo.get(k) for k in
                    ('anchors','time_signatures','pickup_beats','grid_origin'))
                if state.get('source_kind')=='midi' and instrument=='piano' and editor is None and unchanged_import_clock:
                    _atomic_copy(Path(selected_input),export/f'{instrument}_raw.mid')
                else:
                    write_midi(export/f'{instrument}_raw.mid',notes,tempo,instrument,
                        pedals=pedals if not bundle.get('controls') else None,controls=bundle.get('controls'),events=bundle.get('other_events'))
                write_midi(export/f'{instrument}_quantized.mid',quantized,tempo,instrument,True,
                    pedals=pedals if not bundle.get('controls') else None,controls=bundle.get('controls'),events=bundle.get('other_events'))
                report['files'] += [f'{instrument}_raw.mid',f'{instrument}_quantized.mid']
                if editor is not None and original_bundle is not None:
                    # Keep a clearly identified read-only inference view alongside current performance.
                    write_midi(export/f'{instrument}_transcription.mid',original_bundle['notes'],tempo,instrument,
                        pedals=original_bundle.get('pedals'),controls=original_bundle.get('controls'),events=original_bundle.get('other_events'))
                    report['files'].append(f'{instrument}_transcription.mid')
                if mode=='midi' and instrument=='piano':
                    # Reviewed performance omissions must also be available in
                    # MIDI-only mode, without invoking the notation model. IDs
                    # belong to the immutable inference input, not today's raw
                    # export (whose tempo map may have been edited meanwhile).
                    revision_path=project/'notation_revisions'/'piano.json'
                    revisions=state.get('config',{}).get('score',{}).get('notation_revisions')
                    if revisions is None and revision_path.is_file():
                        revisions=read_json(revision_path)
                    if revisions is not None:
                        from .reviewed_midi import write_reviewed_midi
                        from .score_duet_export import _performance_inputs,performance_identity
                        review_pedals=list(_pedals_in_duration(bundle['pedals'],state['duration']))
                        # Stable musical input identity omits editor IDs/origin
                        # and normalizes equivalent controller/pedal streams.
                        # Opening the editor must not invent a nonexistent cache.
                        unchanged_import=(state.get('source_kind')=='midi' and
                            (editor is None or performance_identity(notes,review_pedals,bundle.get('controls'))==
                             performance_identity(original_bundle['notes'],original_bundle.get('pedals',[]),original_bundle.get('controls'))))
                        reviewed_source=(Path(selected_input) if unchanged_import else
                            _performance_inputs(project,notes,review_pedals,bundle.get('controls'))/'piano_raw.mid')
                        if not reviewed_source.is_file():
                            raise ValueError('原始演奏已变化或审核输入缓存缺失，请更新审核意见；不会把旧删音意见套到新人工稿。')
                        audit=write_reviewed_midi(reviewed_source,export/'piano_reviewed.mid',revisions)
                        if audit.get('output'):
                            save_json(export/'piano_reviewed_midi.json',audit)
                            save_json(export/'piano_notation_revisions.json',revisions)
                            report['files'].append('piano_reviewed.mid')
                            report['instruments'][instrument]['reviewed_midi']=audit
                            log('[review] 已复用审核意见导出修订 MIDI，无需运行制谱模型')
            if mode in ('all','score'):
                config=state.get('config',{})
                score_options=dict(config.get('score',{}),_audio_harmony=audio_harmony,
                    _harmony_reuse_only=harmony_reuse_only or cache_only)
                if editor is not None and instrument=='piano':
                    # User pins constrain notation fitting as well as tempo
                    # edits. Ordinary inferred tempo nodes are not fixed pins.
                    score_options['_notation_fixed_anchor_seconds']=[
                        float(a['seconds']) for a in editor.get('anchors',[])]
                if instrument=='piano' and score_options.get('piano_engine')=='duet' and notes:
                    from .score_duet_export import export_duet_score
                    title=Path(state['source']).stem.split(' - ',1)[-1]
                    result=export_duet_score(notes,list(_pedals_in_duration(bundle['pedals'],state['duration'])),
                        tempo,project,export,options=dict(score_options,grid=config.get('quantization','auto'),
                            export_reviewed_midi=mode=='all'),
                        title=title,log=log,
                        source_midi=selected_input if state.get('source_kind')=='midi' and editor is None else None,
                        controls=bundle.get('controls'),prepared_prediction=prepared_prediction)
                    report['instruments'][instrument]['score_engine']='duet'
                    report['instruments'][instrument]['prediction_cached']=result['prediction_cached']
                    report['instruments'][instrument]['voice_layout']=result.get('voice_layout',{})
                    if result.get('score_midi'):
                        if editor is not None:
                            from .score_midi import write_score_midi
                            result['score_midi_audit']=write_score_midi(result['musicxml'],tempo,result['score_midi'],
                                performance_notes=notes,controls=editor['controls'])
                            ornament=result.get('ornament_review')
                            if ornament:
                                from .notation_ornaments import validate_group_playback
                                result['score_midi_audit']['ornament_group_playback']=validate_group_playback(
                                    read_json(ornament['catalog']),ornament['engraving']['accepted_ids'],
                                    result['score_midi_audit'],tempo)
                            save_json(export/'piano_score_midi.json',result['score_midi_audit'])
                        report['files'].append(Path(result['score_midi']).name)
                        report['instruments'][instrument]['score_midi']=result.get('score_midi_audit')
                    if result.get('reviewed_midi'):
                        report['files'].append(Path(result['reviewed_midi']).name)
                        report['instruments'][instrument]['reviewed_midi']=result.get('reviewed_midi_audit')
                else:
                    from .score import export_score
                    result=export_score(quantized,tempo,instrument,export,
                        log=log,harmony_options=score_options)
                result['pdf_render']['source_key'] = keys.get(instrument)
                report['instruments'][instrument]['pdf_render']=result['pdf_render']
                report['warnings'].extend(result.get('warnings',[]))
                report['files'].extend(Path(result[k]).name for k in ('musicxml','pdf','harmony') if result.get(k))
            if mode=='all' or (mode=='midi' and state.get('config',{}).get('score',{}).get('harmony_engine') not in ('tsumugi','chordformer')):
                harmony_path=export/f'{instrument}_harmony.json'
                if notes and not harmony_path.is_file():
                    from .harmony import analyze_midi_harmony, apply_key_confirmation
                    harmony=analyze_midi_harmony(export/f'{instrument}_quantized.mid')
                    apply_key_confirmation(harmony,state.get('config',{}).get('score',{}).get('confirmed_key'))
                    if audio_harmony is not None:
                        from .harmony_export import apply_prediction
                        apply_prediction(harmony,audio_harmony,tempo)
                    save_json(harmony_path,harmony)
                    report['files'].append(harmony_path.name)
            harmony_path=export/f'{instrument}_harmony.json'
            if harmony_path.is_file():
                from .chord_markers import write_export_chord_markers
                counts=write_export_chord_markers(export,instrument,read_json(harmony_path).get('chords',[]),tempo,
                    source_kind=state.get('source_kind'),
                    enabled=state.get('config',{}).get('score',{}).get('show_chords',False))
                report['instruments'][instrument]['chord_marker_counts']=counts
                report['instruments'][instrument]['chord_markers']=max(counts.values(),default=0)
                for variant in ('score','reviewed'):
                    audit_path=export/f'{instrument}_{variant}_midi.json'
                    if audit_path.is_file():
                        report['instruments'][instrument][f'{variant}_midi']=read_json(audit_path)
            if audio_harmony is not None:
                model=audio_harmony.get('model',{})
                engine=model.get('engine','chordformer') if isinstance(model,dict) else 'chordformer'
                state.setdefault('config',{}).setdefault('score',{})['harmony_engine']=engine
        if editor is not None:
            save_json(export/'editor_snapshot.json',editor)
            options=state.pop('_editor_export_options',{})
            if options.get('purpose','mapped')!='mapped' or not options.get('include_time_signatures',True):
                purpose=options.get('purpose','mapped')
                name={'mapped':'piano_performance.mid','target_grid':'piano_target_grid.mid','seconds_120':'piano_seconds_120.mid'}[purpose]
                write_midi(export/name,editor['notes'],tempo,controls=editor['controls'],purpose=purpose,
                    events=editor.get('other_events'),
                    include_time_signatures=options.get('include_time_signatures',True))
                report['files'].append(name)
                report['midi_export_purpose']=purpose
                harmony_path=export/'piano_harmony.json'
                if harmony_path.is_file():
                    from .chord_markers import write_export_chord_markers
                    variant={'mapped':'performance','target_grid':'target_grid','seconds_120':'seconds_120'}[purpose]
                    counts=write_export_chord_markers(export,'piano',read_json(harmony_path).get('chords',[]),tempo,
                        source_kind=state.get('source_kind'),
                        enabled=state.get('config',{}).get('score',{}).get('show_chords',False),variants=(variant,))
                    entry=report['instruments']['piano'];entry.setdefault('chord_marker_counts',{}).update(counts)
                    entry['chord_markers']=max(entry['chord_marker_counts'].values(),default=0)
        report['show_chords']=(export/'piano_harmony.json').is_file() and state.get('config',{}).get('score',{}).get('show_chords',False)
        if mode=='midi' and not (export/'piano_harmony.json').is_file():
            report['harmony_status']='not_requested_for_performance_midi'
            if state.get('config',{}).get('score',{}).get('show_chords',False):
                report['warnings'].append('MIDI 已导出；本次不附带音频和弦标记。')
        report['status']='complete_with_warnings' if report['warnings'] else 'complete'
    except Exception as exc:
        report['status']='failed';report['error']=str(exc)
        # Keep the user-facing error short, but retain the actual failing stage
        # for diagnosis without rerunning expensive inference just for a trace.
        import traceback
        report['error_type']=type(exc).__name__
        try:
            detail=export/'export_error.txt'
            detail.write_text(traceback.format_exc(),encoding='utf-8')
            report['error_detail_file']=detail.name
        except OSError:
            pass
        save_json(export/'report.json',report)
        raise
    save_json(export/'report.json',report)
    for warning in report['warnings']:
        log('提示：'+warning)
    return export


def _bind_initial_local_score(project, state, export, report, log):
    """Bind a fresh committed score to its initial, unedited working document."""
    if report.get('editor_revision') or (export/'editor_snapshot.json').exists():
        return
    from . import editing
    try:
        doc = editing.ensure(project, state=state)
        if (not doc or doc.get('source_export') != str(export)
                or doc.get('operation', {}).get('type') != 'initialize'
                or doc.get('score_stale')):
            return
        # Write an immutable source snapshot before publishing its revision in
        # the report. Neither a pending save nor an older score can supply it.
        save_json(export/'editor_snapshot.json', doc)
        report['editor_revision'] = doc['revision']
        save_json(export/'report.json', report)
    except (OSError, ValueError, RuntimeError) as exc:
        log(f'MusicXML 已保留，但未能建立首个编辑修订的选区预览：{exc}')


def _commit_export(project, state, tempo, export, log, *, notes_draft=None):
    revision=state.pop('_editor_revision',None)
    capture=state.pop('_editor_export_guard',None)
    tempo=state.pop('_editor_tempo',tempo)
    if notes_draft is not None:
        # No app draft ever writes official pointers, including if Save wins the
        # race. The same live owner will validate the actual formal receipt.
        from .provisional_score import worker_receipt
        return worker_receipt(project, export, revision, capture, notes_draft, state)
    if revision:
        from . import editing, editor_checkpoint as cp
        with editing.edit_lock(project):
            if capture is not None:
                current=cp.permits_export(project,capture['guard'],capture['applied_sequence'])
            else:
                doc=editing.current(project)
                current=bool(doc and doc['revision']==revision)
            if not current:
                log('编辑已有更新：旧后台结果已保留为历史快照，未覆盖当前工程。')
                return project
            state['editor_exported_revision']=revision
            old=read_json(project/'project.json') if (project/'project.json').is_file() else {}
            if (export/'piano.musicxml').is_file():
                state['editor_score_revision']=revision
            elif 'editor_score_revision' in old:
                state['editor_score_revision']=old['editor_score_revision']
            return _commit_export(project,state,tempo,export,log)
    # Preserve the last committed chord provenance when a MIDI-only snapshot
    # becomes latest_export. Do not copy stale chord positions into that MIDI.
    previous=Path(state.get('latest_export') or '')
    if (export/'piano_harmony.json').is_file():
        state['latest_harmony_export']=str(export)
    elif (previous/'piano_harmony.json').is_file():
        state['latest_harmony_export']=str(previous)
    from .score_artifacts import resolve_pdf_export
    retained = resolve_pdf_export(project, state)
    if (export/'piano.pdf').is_file():
        state['latest_pdf_export'] = str(export)
    elif retained:
        state['latest_pdf_export'] = str(retained[0].parent)
    else:
        state.pop('latest_pdf_export', None)
    state['latest_export']=str(export)
    if (export/'piano.musicxml').is_file():
        state['latest_score_export']=str(export)
    save_json(project/'project.json',state)
    try:
        save_json(project/'tempo_map.json',tempo)
    except OSError as exc:
        # The export snapshot and project pointer are the committed copy.
        log(f'提示：无法更新项目根目录的速度图副本：{exc}')
    report = read_json(export/'report.json') if (export/'report.json').is_file() else {}
    render = report.get('instruments',{}).get('piano',{}).get('pdf_render') or {}
    if render and (export/'piano.musicxml').is_file():
        _bind_initial_local_score(project,state,export,report,log)
    if render.get('status') == 'failed':
        log(f'MusicXML 已导出，PDF 未生成。{export}')
    else:
        log(f'导出完成：{export}')
    return project


def run(input_path, output_root, instruments, mode, config, tempo_path=None, bpm=None,
        meter='4/4',beat_scale=1.,keep_stems=True,log=print,cache_only=False,
        separation_only=False,workflow_project=None,workflow_checkpoint=None,record_separation=None,
        workflow_cached_score=False):
    """Build a project, or prepare its immutable WAVs without committing an export."""
    from . import adapters
    source=Path(input_path).resolve()
    if not source.is_file():raise ValueError('音频文件不存在')
    if not instruments or any(i not in ('piano','guitar') for i in instruments):
        raise ValueError('请选择钢琴和/或吉他')
    instruments=list(dict.fromkeys(instruments))
    retain_stems=keep_stems or separation_only
    log('读取音频指纹，查找可复用分析')
    digest=fingerprint(source)
    keys=_analysis_keys(config)
    index=ROOT/'.cache'/'projects'/f'{digest}.json'
    indexed,indexed_state=_indexed_project(index,digest)
    if indexed is None:
        default_project=_project_for_source(ROOT/'outputs',source,digest,None)
        indexed_state=_project_state(default_project,digest)
        if indexed_state:
            indexed=default_project
    output=Path(output_root).resolve()
    if workflow_project is None:
        project=_project_for_source(output,source,digest,indexed)
        project.mkdir(parents=True,exist_ok=True)
    else:
        project=_workflow_directory(output,workflow_project)
    source_audio=source
    source_import=None
    # Prepare before taking the project lock: desktop cancellation tracks one
    # owned lock at a time, so the import-cache lock must not nest inside it.
    if source.suffix.lower()=='.ncm':
        from .audio_import import prepare_audio_import
        source_import=prepare_audio_import(source,ROOT/'.cache'/'imports',log)
        source_audio=Path(source_import['path'])
    with project_lock(project):
        if workflow_cached_score and not (cache_only and workflow_project and workflow_checkpoint and not separation_only):
            raise ValueError('Invalid cache-only workflow continuation')
        if cache_only and not separation_only and not workflow_cached_score:
            from .export_readiness import cached_run_ready
            check={'kind':'run','audio':str(source),'output':str(output_root),'instruments':instruments,
                   'keep_stems':keep_stems,'options':{'mode':mode,'tempo_path':tempo_path,'bpm':bpm,
                                                    'meter':meter,'beat_scale':beat_scale}}
            if not cached_run_ready(check,config=config):
                raise ValueError('分析缓存或编辑内容已变化，请重新开始并选择计算设备；本次未启动模型。')
        def emit(message):
            log(message)
            with (project/'process.log').open('a',encoding='utf-8') as f:f.write(str(message)+'\n')
        previous=_project_state(project,digest)
        from .stage_cache_reuse import resolve_keys, committed_origins
        keys, reused_origins=resolve_keys(project,previous,config,instruments,retain_stems)
        if any(origin['key'] != _analysis_keys(config)[stage] for stage,origin in reused_origins.items()):
            emit('复用同模型已完成分析，保留原计算设备记录；本次设备只用于尚未完成的阶段')
        if not separation_only:
            from . import editing
            starting_editor=editing.current(project)
        seed=project if previous else indexed
        seed_state=previous or indexed_state
        external=indexed_state if indexed!=project else None
        work=project/'cache'
        work.mkdir(exist_ok=True)
        normalized=work/'source.wav'

        def audio_path():
            if not normalized.exists():
                decode_audio(source_audio,normalized,emit)
            return normalized

        if seed_state:
            duration=seed_state['duration']
        else:
            frames,rate=_wav_frames(audio_path())
            duration=frames/rate
        seed_keys=(seed_state.get('analysis_keys') or _analysis_keys(seed_state.get('config',{}))) if seed_state else {}
        need=[]
        if not separation_only:
            if previous and 'analysis_keys' not in previous:
                for instrument in ('piano','guitar'):
                    cached=_notes_bundle(project,previous,instrument,seed_keys[instrument])
                    if cached is not None:
                        save_json(_notes_path(project,instrument,seed_keys[instrument]),cached)
            seed_tempo=_committed_tempo(seed,seed_state) if seed_state else None
            from .rhythm_provider import reusable_auto_map
            reuse_seed_tempo=bool(seed_state and seed_tempo.get('source')!='not_applicable' and (
                seed_state.get('tempo_kind','manual')!='auto'
                or seed_tempo.get('source') not in ('beat_this','cubase_local_reconstruction','duet_first_pass')
                or reusable_auto_map(seed_tempo,keys['beat_this'],seed_keys.get('beat_this'))))
            tempo_kind='manual'
            # Config keys name requested analysis. If no beat inference runs below,
            # publish only a key whose evidence actually exists, not that request.
            from .beat_provenance import retain_beat_cache
            actual_beat_key=retain_beat_cache(seed,seed_state,project)
            if 'piano' not in instruments:
                # Internal compatibility clock only: no detected BPM, no tempo
                # artifacts and no transcription jobs for a stem-only project.
                if seed_tempo and seed_tempo.get('source')!='not_applicable':
                    tempo=seed_tempo
                    tempo_kind=seed_state.get('tempo_kind','manual')
                else:
                    tempo=fixed_tempo(120,duration)
                    tempo['source']='not_applicable'
                    tempo_kind='none'
                emit('吉他仅分轨：跳过节拍分析、音符转录与制谱，保留音频')
            elif tempo_path:
                tempo=scale_tempo(_load_tempo(tempo_path,duration),beat_scale)
            elif bpm is not None:
                tempo=scale_tempo(fixed_tempo(bpm,duration,meter),beat_scale)
            elif reuse_seed_tempo:
                tempo=scale_tempo(seed_tempo,beat_scale)
                if tempo.get('pulse_evidence') is not None and seed!=project:
                    from .pulse_evidence_store import copy_reference
                    tempo=copy_reference(seed,project,tempo,digest)
                tempo_kind=seed_state.get('tempo_kind','manual') if beat_scale==1 else 'manual'
                emit('复用已提交的速度图；分轨模型切换不改变原曲节拍分析')
            elif (beat_scale==1 and mode in ('all','score','midi')
                  and config.get('score',{}).get('piano_engine')=='duet'):
                # DuET's first pass supplies beats, meter and bar 1 after the
                # notes exist (duet_beat_map); no acoustic beat model runs.
                tempo=fixed_tempo(120,duration);tempo['source']='duet_pending'
                tempo_kind='auto'
                emit('拍点、拍号与第一小节将由 DuET 初稿给出（不运行 Cubase 拍点分析）')
            else:
                tempo_kind='auto' if beat_scale==1 else 'manual'
                beat_file=work/f'beats-{keys["beat_this"]}.json'
                if (not beat_file.is_file() and seed and seed_keys.get('beat_this')==keys['beat_this']):
                    prior_beats=seed/'cache'/beat_file.name
                    if prior_beats.is_file():save_json(beat_file,read_json(prior_beats))
                if beat_file.is_file():
                    beat_result=read_json(beat_file)
                    emit('复用原曲节拍缓存，更新节拍坐标与拍号映射')
                else:
                    if cache_only:raise ValueError('节拍缓存已变化，本次未启动模型；请重新开始并选择计算设备。')
                    emit('分析原曲节拍与强拍')
                    beat_result=adapters.detect_beats(audio_path(),work/'beat',config,emit)
                    save_json(beat_file,beat_result)
                from .rhythm_decode import tempo_from_analysis
                from .rhythm_provider import selected_provider
                if selected_provider(config)=='cubase_local' and beat_result.get('provider')!='cubase_local_reconstruction':
                    raise ValueError('节拍缓存来源与所选本机算法不符；不会套用旧算法或覆盖已提交结果')
                tempo=tempo_from_analysis(beat_result,duration,beat_scale,
                    provider_options=config.get('models',{}).get(selected_provider(config),{}))
                if beat_result.get('provider')=='cubase_local_reconstruction':
                    from .pulse_evidence_store import store_result
                    tempo['pulse_evidence']=store_result(project,digest,beat_result)
                actual_beat_key=keys['beat_this']

            for instrument in instruments:
                if instrument=='guitar':
                    continue
                target=_notes_path(project,instrument,keys[instrument])
                if target.is_file():
                    continue
                cached=_notes_bundle(indexed,external,instrument,keys[instrument]) if external else None
                if cached is None:
                    need.append(instrument)
                else:
                    save_json(target,cached)

        stem_keys=dict(previous.get('stem_keys',{})) if previous else {}
        if previous and 'stem_keys' not in previous:
            stem_keys={name:seed_keys['separator'] for name in ('piano','guitar') if (project/f'{name}.wav').is_file()}
        stems={}
        required=list(dict.fromkeys((instruments if retain_stems else need)
                                    + (['guitar'] if 'guitar' in instruments else [])))
        separation=work/'separation'
        manifest=work/'separation.json'
        staged=read_json(manifest) if manifest.is_file() else {}
        if staged.get('key')==keys['separator']:
            retained=staged.get('retain_instruments',staged.get('instruments',[]))
            requested=instruments if retain_stems else (['guitar'] if 'guitar' in instruments else [])
            staged['retain_instruments']=list(dict.fromkeys([*retained,*requested]))
        # Do this even when this run needs no separation. Otherwise successful
        # reuse of model A would clean up a failed model B's only complete WAV.
        _archive_staged_stems(project,separation,staged,duration,emit)
        for instrument in required:
            # Previously selected model versions remain valid when switching
            # back; root WAVs are compatibility aliases, not cache identities.
            frozen=project/'stems'/keys['separator']/f'{instrument}.wav'
            if _wave_complete(frozen,duration):
                stems[instrument]=frozen
                continue
            local=stem_path(project,previous or {},instrument)
            if local and stem_keys.get(instrument)==keys['separator'] and _wave_complete(local,duration):
                stems[instrument]=local
                continue
            if external:
                candidate=stem_path(indexed,external,instrument)
                external_keys=external.get('analysis_keys') or _analysis_keys(external.get('config',{}))
                seed_stems=external.get('stem_keys',{instrument:external_keys['separator']})
                if candidate and seed_stems.get(instrument)==keys['separator'] and _wave_complete(candidate,duration):
                    stems[instrument]=candidate
                    continue
            candidate=separation/f'{instrument}.wav'
            if staged.get('key')==keys['separator'] and instrument in staged['instruments'] and _wave_complete(candidate,duration):
                stems[instrument]=candidate

        if workflow_checkpoint:
            if (workflow_checkpoint['source_fingerprint']!=digest
                    or workflow_checkpoint['separator_key']!=keys['separator']):
                raise ValueError('源音频或分轨参数已变化，保留的音轨不能用于本次续做。')
            for instrument,path in workflow_checkpoint['stems'].items():
                frozen=Path(path).resolve(strict=True)
                if instrument not in required or not frozen.is_relative_to(project/'stems') or not _wave_complete(frozen,duration):
                    raise ValueError('已保留分轨的文件或时间轴已变化。')
                stems[instrument]=frozen

        def checkpoint():
            frozen={name:str(_freeze_stem(project,name,keys['separator'],path,duration).resolve())
                    for name,path in stems.items()}
            result={'project':str(project.resolve()),'source':str(source),'duration':duration,
                    'instruments':instruments,'stem_paths':frozen,'source_fingerprint':digest,
                    'separator_key':keys['separator']}
            if record_separation:
                record_separation(result)
            return result

        missing=[name for name in required if name not in stems]
        if stems:
            labels={'piano':'钢琴','guitar':'吉他'}
            emit('复用已验证的分轨缓存：'+' / '.join(labels[name] for name in stems)+'（模型配置一致）')
        if missing:
            if cache_only:raise ValueError('分轨缓存已变化，本次未启动模型；请重新开始并选择计算设备。')
            emit('分离钢琴 / 吉他（显存按单模型顺序使用）')
            if staged.get('key')!=keys['separator']:
                manifest.unlink(missing_ok=True)
                if separation.is_dir():
                    shutil.rmtree(separation)
                staged={'key':keys['separator'],'instruments':[],
                    'retain_instruments':list(instruments if retain_stems else (['guitar'] if 'guitar' in instruments else []))}
            separator=config.get('models',{}).get('separator',{})
            # The managed XLance and SW requests use different checkpoints.
            # Reap the first model process and freeze its WAV before starting the next.
            separate_models=(record_separation and set(missing)=={'piano','guitar'}
                             and separator.get('piano_model')=='xlance')
            groups=[['piano'],['guitar']] if separate_models else [missing]
            if not separation_only and 'piano' in instruments:
                try:
                    from .duet_score import prewarm_duet_daemon
                    prewarm_duet_daemon(config)
                except Exception:
                    pass
            for group in groups:
                separated=adapters.separate(audio_path(),separation,dict(config,instruments=group),emit)
                for instrument in group:
                    if instrument not in separated or not _wave_complete(Path(separated[instrument]),duration):
                        raise RuntimeError(f'分轨模型未返回完整的 {instrument} 音频')
                    stems[instrument]=Path(separated[instrument])
                staged['instruments']=list(dict.fromkeys(staged['instruments']+group))
                save_json(manifest,staged)
                # Do not leave completed audio in a scratch directory when the next model fails.
                if record_separation:
                    checkpoint()
                else:
                    _archive_staged_stems(project,separation,staged,duration,emit)

        if separation_only:
            return checkpoint()
        if record_separation and not missing:
            checkpoint()

        for instrument in need:
            if cache_only:raise ValueError('音符缓存已变化，本次未启动模型；请重新开始并选择计算设备。')
            emit(f'转录 {instrument}：保留音符原始秒时间')
            adapter=adapters.transcribe_transkun if instrument=='piano' else adapters.transcribe_basic_pitch
            pedal_path=work/instrument/'pedals.json'
            pedal_path.unlink(missing_ok=True)
            notes=validate_notes(adapter(stems[instrument],work/instrument,config,emit))
            pedals=read_json(pedal_path) if pedal_path.is_file() else []
            save_json(_notes_path(project,instrument,keys[instrument]),{'notes':notes,'pedals':pedals})
        if not need and 'piano' in instruments:
            emit('复用原始音符缓存，无需再次转录')

        # Publish immutable audio before export. A locked old root alias can
        # then neither fail the completed run nor pair new notes with old audio.
        legacy_stem_paths=dict(previous.get('legacy_stem_paths',{})) if previous else {}
        legacy_snapshot=_snapshot_legacy_stems(project,previous)
        if legacy_snapshot and previous.get('latest_export'):
            legacy_stem_paths[previous['latest_export']]=legacy_snapshot
        stem_paths={}
        for instrument in instruments:
            if keep_stems or instrument=='guitar':
                frozen=_freeze_stem(project,instrument,keys['separator'],stems[instrument],duration)
                stem_paths[instrument]=str(frozen)
                stem_keys[instrument]=keys['separator']
            else:
                stem_keys.pop(instrument,None)
        state={'version':3,'source':str(source),'source_sha256':digest,'duration':duration,
            'instruments':instruments,'config':config,'keep_stems':keep_stems,
            'analysis_keys':dict(keys,beat_this=actual_beat_key),
            'tempo_kind':tempo_kind,'stem_keys':stem_keys,'stem_paths':stem_paths,
            'legacy_stem_paths':legacy_stem_paths}
        origin_keys={stage:key for stage,key in state['analysis_keys'].items()
                     if stage in ('separator','beat_this') or stage=='piano' and 'piano' in instruments}
        state['analysis_origins']=committed_origins(config,origin_keys,reused_origins,previous)
        if previous and previous.get('latest_export'):
            state['latest_export']=previous['latest_export']
            if previous.get('latest_score_export'):
                state['latest_score_export']=previous['latest_score_export']
        if seed_state and seed_state.get('user_confirmations'):
            state['user_confirmations']=seed_state['user_confirmations']
        if source_import:
            state.update(source_audio=str(source_audio),source_import=source_import)
        from . import editing
        prepared_prediction=None
        from .auto_pulse_resolution import eligible, resolve_before_export
        if tempo.get('source')=='duet_pending':
            from .duet_beat_map import resolve_first_pass
            resolved=resolve_first_pass(project,state,tempo,mode,normalized_audio=audio_path(),
                log=emit,**({'cache_only':True} if cache_only else {}))
            tempo=resolved['tempo'];prepared_prediction=resolved['prepared_prediction']
        elif eligible(state,tempo,mode,starting_editor):
            resolved=resolve_before_export(project,state,tempo,mode,
                normalized_audio=audio_path(),editor=starting_editor,log=emit,
                **({'cache_only':True} if cache_only else {}))
            current_editor=editing.current(project)
            changed_editor=(current_editor is not None and
                (current_editor.get('manual') or (starting_editor is not None
                 and current_editor['revision']!=starting_editor['revision'])))
            if changed_editor:
                emit('编辑已有更新：新拍值分析保留为候选，当前人工速度图不变。')
            else:
                tempo=resolved['tempo'];prepared_prediction=resolved['prepared_prediction']
        # A model subprocess is not the GUI's memory owner. New inference
        # stays in its candidate caches; it cannot implicitly checkpoint or
        # replace the working document of another process.
        working=editing.current(project)
        if working is not None:
            state['_expected_editor_revision']=working['revision']
        if prepared_prediction is None:
            export=_export(project,state,tempo,mode,emit,**({'cache_only':True} if cache_only else {}))
        else:
            export=_export(project,state,tempo,mode,emit,prepared_prediction=prepared_prediction,
                           **({'cache_only':True} if cache_only else {}))
        _commit_export(project,state,tempo,export,emit)
        # Updating/removing old convenience aliases is never part of commit.
        # Readers use the new export's immutable stem_paths even if this fails.
        for instrument in instruments:
            target=project/f'{instrument}.wav'
            try:
                if instrument in stem_paths:
                    _compatibility_stem(Path(stem_paths[instrument]),target)
                else:
                    target.unlink(missing_ok=True)
            except OSError as exc:
                emit(f'提示：{target.name} 兼容副本正被占用或无法更新；本次结果已绑定新分轨快照：{exc}')
        try:
            save_json(index,{'project':str(project)})
            normalized.unlink(missing_ok=True)
            manifest.unlink(missing_ok=True)
            if separation.is_dir():
                shutil.rmtree(separation)
        except OSError as exc:
            emit(f'提示：无法整理分析缓存：{exc}')
        return project


def _reexport_working_document(project, captured, revision, *, tempo_path=None, bpm=None, beat_scale=1.):
    """Resolve the same editor input for readiness and actual reexport.

    The desktop's frozen value is authoritative for this request. Formal
    checkpoint ancestry is checked separately from the working revision.
    """
    from . import editing
    guard=None
    if captured is None:
        editor=editing.current(project)
    else:
        from .live_owner import key
        if not isinstance(captured,dict):
            raise ValueError('导出快照与工作请求不匹配。')
        head,capture_guard=captured.get('head'),captured.get('guard')
        editor=captured.get('document')
        project_key=key(project)
        if (captured.get('project')!=project_key or not isinstance(head,dict)
                or head.get('project_id')!=project_key or head.get('revision')!=revision
                or not isinstance(revision,str) or not revision
                or not isinstance(editor,dict) or editor.get('source_key')!=head.get('source_key')
                or not isinstance(capture_guard,dict)
                or not all(isinstance(capture_guard.get(k),str) and capture_guard[k] for k in ('anchor','lineage'))
                or type(head.get('applied_sequence')) is not int or head['applied_sequence']<0
                or tempo_path or bpm is not None or beat_scale!=1.):
            raise ValueError('导出快照与工作请求不匹配。')
        guard=dict(guard=capture_guard,applied_sequence=head['applied_sequence'])
    if revision is not None:
        editing._check_base(editor,revision)
    return editor,guard


def reexport(project_dir,tempo_path=None,bpm=None,meter='4/4',beat_scale=1.,mode='all',log=print,quantization=None,score_options=None,
             editor_revision=None,export_options=None,*,provisional_score=None):
    project=Path(project_dir).resolve()
    with project_lock(project):
        state=read_json(project/'project.json')
        from . import editing
        # Reject invalid requests before a tempo override can publish an edit.
        if mode not in ('all','midi','score'):
            raise ValueError('未知输出模式')
        if isinstance(beat_scale,bool) or beat_scale not in (.5,1.,2.):
            raise ValueError('节拍倍率仅支持 0.5 / 1 / 2')
        if quantization is not None and quantization not in ('auto','straight','triplet','fine'):
            raise ValueError('量化应为 auto/straight/triplet/fine')
        if export_options and export_options.get('purpose','mapped') not in ('mapped','target_grid','seconds_120'):
            raise ValueError('未知 MIDI 导出用途')
        if score_options is not None:
            score_options=dict(score_options)
            if score_options.get('piano_engine') not in ('music21','duet'):
                raise ValueError('钢琴制谱引擎须为 music21 或 duet')
        options=dict(export_options or {})
        captured=options.pop('_working_input',None)
        clock_override=options.pop('_working_clock_override',False)
        editor,guard=_reexport_working_document(project,captured,editor_revision,
                                              tempo_path=tempo_path,bpm=bpm,beat_scale=beat_scale)
        if guard is not None:
            state['_editor_input']=editor
            state['_editor_export_guard']=guard
        draft=None
        if provisional_score is not None:
            from .provisional_score import draft_directory, pointer_marks
            if mode not in ('all','score') or guard is None or not editor.get('source_key'):
                raise ValueError('临时生成需要已绑定的音符修订；未更新正式谱。')
            draft_directory(project, provisional_score)
            draft=dict(token=provisional_score, source=editor['source_key'], before=pointer_marks(state))
        if options:
            state['_editor_export_options']=options
        guitar=stem_path(project,state,'guitar')
        if 'guitar' in state['instruments'] and (not guitar or not _wave_complete(guitar,state['duration'])):
            raise FileNotFoundError('缺少完整吉他分轨，请使用“重新分析”生成音频；仅重导出不会运行分轨模型')
        if tempo_path:tempo=_load_tempo(tempo_path,state['duration'])
        elif bpm is not None:tempo=fixed_tempo(bpm,state['duration'],meter)
        else:tempo=editor['tempo'] if editor is not None else _committed_tempo(project,state)
        tempo=scale_tempo(tempo,beat_scale)
        if editor is not None and (tempo_path or bpm is not None or beat_scale!=1):
            if tempo_path or bpm is not None:
                operation={'type':'replace_tempo','tempo':tempo}
            else:
                # Beat-value interpretation uses the working map and its pins,
                # not the last exported map. Seconds and meter remain separate.
                operation={'type':'tempo','action':'interpret','start':0.,
                           'end':editor['duration'],'factor':beat_scale}
            # Legacy batch clock-override command explicitly commits a working
            # tempo change. The desktop sends an already-applied frozen input
            # instead, and neither ordinary export path checkpoints the editor.
            editor=editing.edit(project,editor['revision'],operation)
            from .document_editor import save
            save(project,wait=True)
            tempo=editor['tempo']
        if editor is not None:
            # Bind both explicit and ordinary reexports to the revision we just
            # read/created. Our own override is not a concurrent user edit.
            state['_expected_editor_revision']=editor['revision']
        if clock_override or tempo_path or bpm is not None or beat_scale!=1:
            state['tempo_kind']='manual'
        if quantization is not None:
            state['config']=dict(state.get('config',{}),quantization=quantization)
        harmony_reuse_only=bool(clock_override or tempo_path or bpm is not None or beat_scale!=1)
        if score_options is not None:
            harmony_reuse_only=harmony_reuse_only or bool(score_options.pop('_harmony_reuse_only',False))
            state['config']=dict(state.get('config',{}),score={**state.get('config',{}).get('score',{}),**score_options})
        log('仅重新导出：复用分轨和原始音符；制谱预测按需缓存')
        if 'stem_paths' not in state:
            paths=_snapshot_legacy_stems(project,state)
            state['stem_paths']=paths
            if paths and state.get('latest_export'):
                state.setdefault('legacy_stem_paths',{})[state['latest_export']]=paths
        if draft is None:
            export=_export(project,state,tempo,mode,log,harmony_reuse_only=harmony_reuse_only)
            return _commit_export(project,state,tempo,export,log)
        export=_export(project,state,tempo,mode,log,harmony_reuse_only=harmony_reuse_only,notes_draft=draft['token'])
        return _commit_export(project,state,tempo,export,log,notes_draft=draft)
