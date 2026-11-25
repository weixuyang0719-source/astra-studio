"""Identity-bound, read-only notation-grid prior from existing DuET artifacts.

No inference, rendering, cache mutation, or automatic tempo selection occurs.
This evidence concerns model quarter values, never permission to retime pulses.
"""
from collections import defaultdict
from pathlib import Path
import hashlib
import io
import json
import math
import sys
from copy import deepcopy


def _digest(data):return hashlib.sha256(data).hexdigest()


def validate_note_identity(notes, performance, *, tolerance_seconds=.001):
    """Match multiplicities per pitch/velocity, plus both raw-second endpoints."""
    expected=defaultdict(list);actual=defaultdict(list)
    for n in notes:
        start,end=float(n['start']),float(n['end'])
        if not math.isfinite(start+end) or start<0 or end<=start:
            raise ValueError('Invalid original note seconds')
        expected[int(n['pitch']),int(n['velocity'])].append((start,end))
    fields=['ref_pitch','ref_vel','ref_onset','ref_offset']
    if len({len(performance[f]) for f in fields})!=1:
        raise ValueError('Performance note arrays have different lengths')
    for p,v,s,e in zip(*(performance[f] for f in fields)):
        actual[int(p),int(v)].append((float(s),float(e)))
    if set(expected)!=set(actual):raise ValueError('Original notes and cached input pitch/velocity differ')
    maximum=0.
    for k,items in expected.items():
        if len(items)!=len(actual[k]):raise ValueError('Original note multiplicity differs from cached input')
        for a,b in zip(sorted(items),sorted(actual[k])):
            if not all(math.isfinite(x) for x in b):raise ValueError('Non-finite MIDI note seconds')
            maximum=max(maximum,abs(a[0]-b[0]),abs(a[1]-b[1]))
    if maximum>tolerance_seconds:raise ValueError('Cached input does not preserve original note seconds')
    return {'note_count':sum(map(len,expected.values())),
            'max_endpoint_difference_seconds':maximum,'tolerance_seconds':tolerance_seconds,
            'comparison':'all pitch/velocity multiplicities and onset/end seconds, not file age or note count alone'}


def extract_cached_notation_prior(project_path, cache_dir, input_midi, *, expected_audio_sha256):
    """Return prior+receipt after the complete project/audio/note/cache binding.

    Caller names an exact existing cache and matching input; no heuristic search
    or fallback occurs here. Current pinned vendor code must match cached code.
    """
    return _extract_notation_prior(project_path,cache_dir,input_midi,
        expected_audio_sha256=expected_audio_sha256)


def extract_pending_notation_prior(project_path,state,notes,cache_dir,input_midi,*,
                                   expected_audio_sha256,audio_binding=None):
    """Bind a prediction to a pending run without reading/publishing project.json.

    ``audio_binding`` names a caller-verified normalization receipt: original
    and normalized paths/hashes, method='ffmpeg_decode_audio'. This function
    rechecks both files and durations; it does not claim that hashes alone
    prove the semantic equivalence of an arbitrary audio transformation.
    """
    if not isinstance(state,dict) or not isinstance(notes,list):
        raise ValueError('Pending prior requires explicit state and original notes')
    return _extract_notation_prior(project_path,cache_dir,input_midi,
        expected_audio_sha256=expected_audio_sha256,pending_state=deepcopy(state),
        pending_notes=deepcopy(notes),audio_binding=deepcopy(audio_binding))


def _extract_notation_prior(project_path,cache_dir,input_midi,*,expected_audio_sha256,
                            pending_state=None,pending_notes=None,audio_binding=None):
    import numpy as np
    import pandas as pd
    import soundfile as sf
    from .duet_score import SOURCE_COMMIT, WEIGHTS_SHA256, CONFIG_SHA256, _source_hash, duet_source_dir
    pending=pending_state is not None
    project_path=Path(project_path).resolve(strict=not pending);cache=Path(cache_dir).resolve(strict=True)
    midi=Path(input_midi).resolve(strict=True);source=duet_source_dir()
    snapshots={}
    def take(path):
        path=Path(path).resolve(strict=True);data=path.read_bytes();snapshots[path]=_digest(data);return data
    project=pending_state if pending else json.loads(take(project_path))
    manifest=json.loads(take(cache/'astra-cache.json'))
    identity=manifest['identity']
    if (identity.get('source_commit')!=SOURCE_COMMIT or identity.get('source_sha256')!=_source_hash(source)
        or identity.get('weights_sha256')!=WEIGHTS_SHA256 or identity.get('model_config_sha256')!=CONFIG_SHA256):
        raise ValueError('Cached model/vendor identity differs from the current pinned extractor')
    audio=Path(project.get('source_audio') or project['source'])
    original_audio_hash=_digest(take(audio))
    original_source=Path(project['source'])
    source_bytes_reverified=pending or original_source.is_file()
    if source_bytes_reverified and _digest(take(original_source))!=project['source_sha256']:
        raise ValueError('Original source file changed from pending/project identity')
    candidate_audio_hash=expected_audio_sha256
    if audio_binding is None:
        if original_audio_hash!=expected_audio_sha256:raise ValueError('Project audio identity differs from candidate')
    else:
        if not pending or audio_binding.get('method')!='ffmpeg_decode_audio':
            raise ValueError('Unsupported or unscoped audio normalization binding')
        if (Path(audio_binding['original_path']).resolve()!=audio.resolve()
                or audio_binding.get('original_sha256')!=original_audio_hash):
            raise ValueError('Normalization original audio identity differs from pending source')
        normalized=Path(audio_binding['normalized_path']).resolve(strict=True)
        if (audio_binding.get('normalized_sha256')!=expected_audio_sha256
                or _digest(take(normalized))!=expected_audio_sha256):
            raise ValueError('Normalized audio identity differs from rhythm candidate')
        normalized_info=sf.info(str(normalized))
        if normalized_info.samplerate!=44100 or normalized_info.channels!=2:
            raise ValueError('Normalized rhythm source must be 44.1kHz stereo')
    declared_duration=project.get('duration')
    if (pending and isinstance(declared_duration,(int,float)) and not isinstance(declared_duration,bool)
            and math.isfinite(declared_duration) and declared_duration>0):
        audio_duration=float(declared_duration)
        duration_source='pending_decoded_duration'
    else:
        audio_info=sf.info(str(audio));audio_duration=audio_info.frames/audio_info.samplerate
        duration_source='original_audio_frames'
    if audio_binding is not None:
        normalized_duration=normalized_info.frames/normalized_info.samplerate
        if abs(normalized_duration-audio_duration)>.01:
            raise ValueError('Normalized audio duration differs from original timeline')
    if project.get('source_import',{}).get('converted'):
        imported=json.loads(take(audio.parent/'import.json'))
        if (imported.get('audio_sha256')!=original_audio_hash
            or imported.get('source_sha256')!=project['source_sha256']
            or project['source_import'].get('audio_sha256')!=original_audio_hash
            or project['source_import'].get('source_sha256')!=project['source_sha256']):
            raise ValueError('Converted original audio identity is not bound to project')
    elif project['source_sha256']!=original_audio_hash:
        raise ValueError('Original project source identity differs from audio')
    if pending:raw_notes=pending_notes
    else:
        note_path=project_path.parent/'notes'/('piano-'+project['analysis_keys']['piano']+'.json')
        raw_notes=json.loads(take(note_path))['notes']
    midi_bytes=take(midi)
    if identity.get('input_sha256')!=_digest(midi_bytes):raise ValueError('Cache input MIDI hash mismatch')
    artifacts={}
    for name in ('score.npz','frames.csv','report.json'):
        artifacts[name]=take(cache/name)
        if manifest.get('artifacts',{}).get(name)!=_digest(artifacts[name]):
            raise ValueError('Cached artifact hash mismatch: '+name)
    report=json.loads(artifacts['report.json'])
    if report.get('status')!='complete' or report.get('input_coverage')!=1.:
        raise ValueError('Incomplete cached model prediction')
    sys.path.insert(0,str(source/'src'))
    from duet.midi import read_performance_midi
    from duet.readable.config import Config
    from duet.readable import pipeline as upstream
    from duet.readable.meter import analyse
    from duet.readable.grid import build_grid,fix_rewinds
    perf=read_performance_midi(midi)
    cleanup=perf['midi_input_cleanup']
    if (cleanup['input_note_on_events']!=cleanup['retained_notes'] or
        any(cleanup.get(k,0) for k in ('unclosed_note_on_events','nonpositive_duration_notes','unmatched_note_off_events'))):
        raise ValueError('Input MIDI required note cleanup')
    note_match=validate_note_identity(raw_notes,perf)
    with np.load(io.BytesIO(artifacts['score.npz']),allow_pickle=False) as bundle:
        score={k:np.array(bundle[k],copy=True) for k in ('notes','bar_starts','bar_lengths','meters','voices','keys')}
    if len(score['notes'])!=len(perf['ref_pitch']) or not np.array_equal(score['notes'][:,0],perf['ref_pitch']):
        raise ValueError('Cached prediction note IDs/pitches do not match input MIDI order')
    frames=pd.read_csv(io.BytesIO(artifacts['frames.csv']))
    frame_seconds=np.asarray(frames.performance_time_sec,dtype=float)
    if not np.all(np.isfinite(frame_seconds)) or np.any(np.diff(frame_seconds)<=0):
        raise ValueError('Cached frame seconds are not finite and monotonic')
    cfg=Config.resolve(report.get('engraving',{}).get('config',{}));logs=[];on=perf['ref_onset']
    meter,q,qd,_,bars=analyse(score,on,logs.append)
    if bars is not None and cfg.snap_bar_lines:bars=upstream.snap_model_bars(bars,q,logs.append)
    if bars is None:
        fbeats=None
        if meter.pulse_q==1:
            from scipy.signal import find_peaks
            peaks,_=find_peaks(frames.p_any_beat.values,height=cfg.rewind_frame_conf,distance=cfg.beat_peak_distance)
            fbeats=(frames.performance_time_sec.values[peaks],frames.p_downbeat.values[peaks]>.5)
        q=fix_rewinds(on,q,float(meter.bar_q),cfg,logs.append,fbeats)
    grid,shift_at=build_grid(frames,on,q,meter.bar_q,cfg,logs.append,pulse=meter.pulse_q,
        bars=None if bars is None else (np.array([b[0] for b in bars]),np.array([b[3] for b in bars])))
    grid=np.asarray(grid,dtype=float)
    if len(grid)<2 or not np.all(np.isfinite(grid)) or np.any(np.diff(grid)<=0):
        raise ValueError('Model grid cannot be used as a monotonic notation prior')
    model_positions=np.asarray(q,dtype=float)+np.asarray([shift_at(float(t)) for t in on],dtype=float)
    if not np.all(np.isfinite(model_positions)) or not np.all(np.isfinite(qd)):
        raise ValueError('Model note coordinates are not finite')
    if (any(_digest(p.read_bytes())!=h for p,h in snapshots.items())
            or _source_hash(source)!=identity['source_sha256']):
        raise ValueError('Source changed during prior extraction')
    receipt={'source_files_sha256':{str(p):h for p,h in snapshots.items()},'source_files_unchanged':True,
        'scope':'pending_run_snapshot' if pending else 'committed_project_snapshot',
        'project_state_path':str(project_path),'project_state_read_from_disk':not pending,
        'pending_state_sha256':_digest(json.dumps(project,sort_keys=True).encode()) if pending else None,
        'original_notes_sha256':_digest(json.dumps(raw_notes,sort_keys=True).encode()),
        'original_audio_sha256':original_audio_hash,'candidate_audio_sha256':candidate_audio_hash,
        'original_source_bytes_reverified':source_bytes_reverified,
        'audio_binding':audio_binding,'duration_source':duration_source,
        'vendor_source_sha256':identity['source_sha256'],'prediction_sha256':_digest(artifacts['score.npz']),
        'input_sha256':_digest(midi_bytes),'frames_sha256':_digest(artifacts['frames.csv']),
        'note_identity':note_match,'model_grid_points':len(grid),'model_grid_logs':logs,
        'model_grid_extent_seconds':[float(grid[0]),float(grid[-1])],
        'supported_seconds_range':[max(0.,float(frame_seconds[0])),min(audio_duration,float(frame_seconds[-1]))],
        'method':'pinned DuET analyse/build_grid with cached original engraving config',
        'model_note_position_method':'analyse plus fix_rewinds if required, plus build_grid shift_at at original onset seconds',
        'inference_run':False,'prior_is_ground_truth':False,'project_published':False}
    binding=_digest(json.dumps(receipt,sort_keys=True).encode())
    return {'prior':{'audio_sha256':expected_audio_sha256,'source_sha256':binding,
                     'model_grid_seconds':grid.tolist(),
                     'model_note_onsets_seconds':np.asarray(on,dtype=float).tolist(),
                     'model_note_positions_quarters':model_positions.tolist(),
                     'model_note_durations_quarters':np.asarray(qd,dtype=float).tolist(),
                     'model_note_pitches':np.asarray(perf['ref_pitch'],dtype=int).tolist(),
                     'supported_seconds_range':receipt['supported_seconds_range']},'receipt':receipt}
