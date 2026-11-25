"""Audio chord inference and notation remain separate, versioned operations."""
from pathlib import Path


def score_alignment(tempo, notation_report=None):
    """Bind labels to the same reference clock used to construct this score.

    DuET's model bar indices are deliberately never used here. Its constrained
    writer maps performance onsets into the exported reference clock. Refuse
    a stale notation/reference pair rather than attaching shifted labels.
    """
    from .timing import TempoMap
    if notation_report is not None:
        reference = notation_report.get('identity', {}).get('reference')
        if reference is None or any(reference.get(field) != tempo.get(field)
                                    for field in ('anchors', 'time_signatures', 'pickup_beats')):
            raise ValueError('谱面与节拍修正版本不一致，请先重导出；未写入可能错位的和弦')
    clock = TempoMap(tempo)
    return {'verified': True, 'source': 'exported_reference_clock',
            'anchors': [{'seconds': float(s), 'score_quarters': float(b)}
                        for s, b in zip(clock.seconds, clock.beats)]}


def prepare_audio_harmony(project, state, log=print, *, reuse_only=False, strict_cache_only=False):
    """Never infer during tempo repair; reuse inference independent of tempo."""
    from . import pipeline
    from .chordformer import analyze_audio_chords, cache_is_available
    if state.get('source_kind') == 'midi' or 'piano' not in state.get('instruments', []):
        return None
    options = state.get('config', {}).get('score', {})
    reuse_only=reuse_only or strict_cache_only
    wants_analysis=options.get('show_chords') or options.get('harmony_engine') in ('chordformer','tsumugi')
    audio = pipeline.stem_path(Path(project), state, 'piano')
    if not audio or not audio.is_file():
        if strict_cache_only and wants_analysis:
            raise ValueError('钢琴分轨缓存缺失；本次未启动模型，请重新开始并选择计算设备。')
        if options.get('harmony_engine') in ('chordformer','tsumugi'):
            raise FileNotFoundError('缺少当前钢琴分轨，无法核对和弦缓存来源')
        return None
    if options.get('harmony_engine') == 'tsumugi':
        from .tsumugi_chords import cached_prediction
        # Committed labels retain their original conditioning on every reexport.
        # Explicit Analyze is the sole action which hears the revised beat/key map.
        # MIDI-only exports deliberately carry no newly inferred harmony.
        # Resolve the prior committed provenance, still validating its audio cache.
        bases=[Path(state.get(k) or '') for k in
               ('latest_export','latest_harmony_export','latest_score_export')]
        base=next((p for p in bases if (p/'piano_harmony.json').is_file()),bases[0])
        report=pipeline.read_json(base/'piano_harmony.json')
        prediction=cached_prediction(audio,Path(project)/'cache'/'tsumugi',
                                      report.get('audio_model',{}).get('cache_key'))
        if strict_cache_only and prediction is None:
            raise ValueError('和弦缓存已变化，本次未启动模型；请重新分析和弦。')
        log('[tsumugi] 复用已提交的和弦判断，按当前谱面重新定位；不重新运行模型')
        return prediction
    cache = Path(project) / 'cache' / 'chordformer'
    # Avoid hashing weights/audio on old projects without any model cache and
    # with chord display off. Active model projects always retain their report.
    if not cache.exists() and not options.get('show_chords') and options.get('harmony_engine') != 'chordformer':
        return None
    available = cache_is_available(audio, cache)
    if not available and reuse_only:
        if strict_cache_only and wants_analysis:
            raise ValueError('和弦缓存已变化，本次未启动模型；请重新开始并选择计算设备。')
        if options.get('harmony_engine') == 'chordformer':
            raise ValueError('和弦模型缓存缺失或来源已改变，请先点“分析和弦”；节拍补正不会重新运行模型')
        log('[chordformer] 尚未分析和弦，本次仅修正谱面；可稍后主动分析和弦')
        return None
    if not available and not options.get('show_chords') and options.get('harmony_engine') != 'chordformer':
        return None
    return analyze_audio_chords(audio, cache, device=options.get('device', 'cuda'), log=log,
                                cache_only=reuse_only)


def apply_prediction(report, prediction, tempo, *, musicxml=None, show_chords=False,
                     notation_report=None):
    from .audio_harmony import apply_audio_harmony
    alignment = score_alignment(tempo, notation_report)
    result = apply_audio_harmony(report, prediction['segments'], tempo,
        musicxml=musicxml, show_chords=show_chords, score_alignment=alignment)
    result['audio_model'] = {k: prediction[k] for k in ('model', 'cache_key', 'cached', 'execution',
                            'conditioning','conditioning_reused') if k in prediction}
    result['audio_source_scope'] = 'piano_stem'
    return result


def export_audio_harmony(project, base_export, device='cuda', force=False, log=print, engine='chordformer'):
    """Run the model on demand, then commit labels without rerunning notation."""
    from . import pipeline
    from .chord_visibility import export_chord_visibility
    from .chordformer import analyze_audio_chords
    from .audio_harmony import tag_legacy_generated_harmonies
    project, base = Path(project).resolve(), Path(base_export).resolve()
    if engine not in ('chordformer','tsumugi'):
        raise ValueError('未知和弦模型，请选择 Tsumugi-Chords 或 ChordFormer')

    def update(pending, state, tempo, emit):
        if state.get('source_kind') == 'midi' or 'piano' not in state.get('instruments', []):
            raise ValueError('音频和弦模型需要钢琴分轨；直接 MIDI 项目不运行此模型')
        base_report = pipeline.read_json(base / 'report.json')
        audio = base_report.get('stem_paths', {}).get('piano') or pipeline.stem_path(project, state, 'piano')
        if not audio or not Path(audio).is_file():
            raise FileNotFoundError('缺少此版本的钢琴分轨，请先生成分轨')
        path = pending/'piano_harmony.json'
        report = pipeline.read_json(path) if path.is_file() else {}
        xml = pending/'piano.musicxml'
        notation_path = pending/'piano_notation_report.json'
        notation = pipeline.read_json(notation_path) if notation_path.is_file() else None
        # Validate the score/clock before changing even our private snapshot.
        score_alignment(tempo, notation)
        if engine=='tsumugi':
            from .tsumugi_chords import analyze_audio_chords as tsumugi_analyze, conditioning_from_export
            prediction=tsumugi_analyze(audio,project/'cache'/'tsumugi',conditioning_from_export(base),
                                      device=device,force=force,log=emit)
        else:
            prediction = analyze_audio_chords(audio, project/'cache'/'chordformer',
                                             device=device, force=force, log=emit)
        if xml.is_file() and str(report.get('method','')).startswith(('music21_', 'audio_model_')):
            tag_legacy_generated_harmonies(xml, report)
        apply_prediction(report, prediction, tempo, musicxml=xml if xml.is_file() else None,
                         show_chords=True, notation_report=notation)
        pipeline.save_json(path, report)
        # A fresh generation must not restore visibility indices from the old
        # label sequence. The new symbols are visible by explicit user action.
        (pending/'piano_chord_visibility.json').unlink(missing_ok=True)
        state.setdefault('config', {}).setdefault('score', {}).update(
            harmony_engine=engine, show_chords=True)

    return export_chord_visibility(project, base, True, log,
        _update_harmony=update, _operation='harmony_analysis')
