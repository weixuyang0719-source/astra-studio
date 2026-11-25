"""Run the optional isolated decoder without importing madmom in the AI env."""
from .release_paths import runtime_env_root
import json
import os
from pathlib import Path
import subprocess
import tempfile
import copy


def decode_cached_activations(path, python=None, *, constraints=None):
    root=runtime_env_root()
    python=Path(python or root/'.venv-rhythm'/'Scripts'/'python.exe')
    if not python.is_file():
        raise FileNotFoundError('自动变拍环境尚未安装，请运行 Install-Rhythm.cmd')
    env=os.environ.copy()
    env['PYTHONPATH']=os.pathsep.join(filter(None,(str(Path(__file__).resolve().parents[1]),env.get('PYTHONPATH'))))
    env['PYTHONIOENCODING']='utf-8'
    with tempfile.TemporaryDirectory(prefix='astra-rhythm-') as temp:
        result=Path(temp)/'result.json'
        command=[str(python),'-u','-m','astra.rhythm_worker',str(Path(path).resolve()),str(result)]
        if constraints is not None:
            constraint_path=Path(temp)/'constraints.json'
            constraint_path.write_text(json.dumps(constraints,ensure_ascii=False,allow_nan=False),encoding='utf-8')
            command.append(str(constraint_path))
        process=subprocess.run(command,
            env=env,capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=120,
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        if process.returncode:
            raise RuntimeError('自动变拍解码失败：'+(process.stderr or process.stdout)[-1200:])
        return json.loads(result.read_text(encoding='utf-8'))


def tempo_from_analysis(result,duration,beat_scale=1., *, provider_options=None):
    from .timing import from_beats, scale_tempo, TempoMap
    if result.get('provider') == 'cubase_local_reconstruction':
        from .cubase_pulse_map import build_pulse_candidate_map
        options=provider_options or {}
        candidate=build_pulse_candidate_map(
            result, audio_duration=duration, audio_sha256=result.get('source_sha256'),
            prefix_policy=options.get('prefix_policy','notational_prefix'),
            opening_phase_policy='provisional_first_downbeat',
            quarters_per_pulse=options.get('quarters_per_pulse',1.))
        if not candidate['report']['notation_ready']:
            raise ValueError('拍点候选尚不能用于记谱：'+', '.join(candidate['report']['notation_blocks']))
        tempo=candidate['tempo']
        # A selected provider supplies the complete clock; never pass it through
        # legacy beat repair, meter inference, or constant-tempo normalization.
        tempo['candidate_only']=False
        tempo['pulse_projection']['effective_map_published']=False
        return scale_tempo(tempo,beat_scale)
    from .meter_decode import METER_DECODER_VERSION, tempo_map_from_bar_result
    original=from_beats(result['beats'],result['downbeats'],duration,beat_scale)
    # Version the mapping policy even when compatibility/error handling uses
    # the original clock; this field is not a claim that DBN decoding succeeded.
    original['meter_decoder_version']=METER_DECODER_VERSION
    warning=result.get('decoder_warning')
    if result.get('bar_result') is not None:
        try:
            tempo=tempo_map_from_bar_result(result['bar_result'],duration,1.,
                                            allow_unconfirmed_opening=True)
            if (original['time_signatures'][0]['numerator'] == 2 and
                    original['meter_diagnostics'].get('initial_meter_supported')):
                # Compatibility concerns *grouping*, never permission to
                # discard a valid DBN pulse clock or later accepted changes.
                # Raw two-accent groups cannot distinguish true 2/4 from
                # half-bar accents. Preserve their provisional display only.
                diag=tempo['meter_diagnostics']
                decoder_initial=tempo['time_signatures'][0]['numerator']
                diag['decoder_opening_meter_confirmed']=diag['opening_meter_confirmed']
                diag['opening_meter_confirmed']=False
                diag['opening_phase_confirmed']=False
                tempo['time_signatures'][0]['numerator']=2
                # Stay on the valid DBN clock: do not project the legacy
                # repaired pulse index/phase into this different pulse series.
                tempo['pickup_beats']=tempo['pickup_beats'] % 2
                tempo['meter_policy']['opening_status']='ambiguous_two_beat_grouping'
                opening=diag['candidate_segments'][0]
                opening.update(applied=False, provisional_display=True,
                               reason='two_beat_grouping_outside_decoder')
                end_beat=(tempo['time_signatures'][1]['beat']
                          if len(tempo['time_signatures'])>1 else None)
                end_seconds=TempoMap(tempo).to_seconds(end_beat) if end_beat is not None else duration
                diag['uncertain_intervals'].append({
                    'start_seconds':0., 'end_seconds':end_seconds,
                    'reason':'two_beat_grouping_outside_decoder',
                    'requires_manual_review':True,
                    'retained_clock_is_not_confirmation':True})
                diag['two_beat_compatibility']={
                    'decoder_initial_numerator':decoder_initial,
                    'provisional_numerator':2, 'legacy_clock_used':False,
                    'legacy_pulse_repairs_not_applied':len(original['meter_diagnostics'].get('pulse_repairs',[])),
                    'source_support':copy.deepcopy(original['meter_diagnostics'].get('candidate_runs',[])),
                    'scope_end_seconds':end_seconds}
                tempo['warnings'].append(
                    '原强拍组存在双拍重音，当前解码器只覆盖 3/4 与 4/4；开头暂显 2/4，但拍号与首拍未确认。'
                    '保留全部解码拍点秒数及后续已确认变拍，请在卷帘确认开头分组；未套用旧整曲补拍规则。')
                tempo['meter_decoder_fallback']={
                    'reason':'supported_two_beat_meter_outside_decoder',
                    'scope':'opening_meter_and_phase_only', 'clock_source':'bar_decoder',
                    'candidate':copy.deepcopy(result['bar_result'])}
            elif not tempo['meter_diagnostics']['opening_meter_confirmed']:
                # A valid DBN pulse clock must not switch to the legacy
                # whole-song pulse repair merely because its opening is short
                # or has weak accents. Only the displayed meter/phase is
                # provisional; later change thresholds remain unchanged.
                tempo['meter_decoder_fallback']={
                    'reason':'Opening meter lacks three observed complete-bar downbeats',
                    'scope':'meter_and_phase_only', 'clock_source':'bar_decoder',
                    'candidate':tempo['meter_diagnostics']['decoded_result']}
            return scale_tempo(tempo,beat_scale)
        except ValueError as exc:
            warning=f'自动变拍证据不足，保留原拍点分析供核对：{exc}'
    tempo=original
    if warning:
        tempo['warnings'].append(warning)
        tempo['meter_decoder_fallback']={'reason':warning,'candidate':result.get('bar_result')}
    return tempo
