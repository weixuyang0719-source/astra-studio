"""Opt-in, non-commercial MD Beat This experiment. Never mutates a project.

The upstream network receives user-selected beat tokens BEFORE prediction.
No training, no post-hoc pretend conditioning, no switch of the default provider.
"""
from __future__ import annotations
from .release_paths import runtime_env_root
import argparse
import hashlib
import json
import math
from pathlib import Path
import sys
import time

VERSION = 2
UPSTREAM_COMMIT = 'ab0011254b12c5fcaa4ae157506e912ddf41b257'
CHECKPOINT_SHA256 = '970d40b0b1f834029684c9c19609fa405d1dbb1daabf973d3c4d894776dfd3f0'
FPS = 50
BORDER = 6
MAX_SCOPE_SECONDS = 24.0
LICENSE = 'CC-BY-NC-4.0; research/non-commercial only'


def sha256(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def finite(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f'{name} must be a finite number')
    return float(value)


def validate_request(request, duration):
    """Seconds are absolute source-audio seconds, never inferred musical beats."""
    if request.get('research_noncommercial') is not True:
        raise ValueError('This experimental model requires research_noncommercial=true')
    start = finite(request.get('start_seconds'), 'start_seconds')
    end = finite(request.get('end_seconds'), 'end_seconds')
    if not 0 <= start < end <= duration or end - start > MAX_SCOPE_SECONDS:
        raise ValueError('Choose a nonempty range within the audio, at most 24 seconds')
    known = request.get('known_beats', [])
    down = request.get('known_downbeats', [])
    if not isinstance(known, list) or not isinstance(down, list):
        raise ValueError('Known positions must be lists of absolute seconds')
    known = sorted(set([start, end] + [finite(t, 'known beat') for t in known]))
    down = sorted(set(finite(t, 'known downbeat') for t in down))
    if any(t < start or t > end for t in known + down):
        raise ValueError('Known positions must be inside the selected range')
    if any(t not in known for t in down):
        raise ValueError('Every known downbeat must explicitly also be a known beat')
    count = request.get('interval_count')
    if count is not None and (type(count) is not int or not 1 <= count <= 128):
        raise ValueError('interval_count must be an integer from 1 to 128')
    if count is not None and len(known) > count + 1:
        raise ValueError('More known beats than requested intervals allow')
    steps = request.get('steps', 8)
    if type(steps) is not int or not 1 <= steps <= 20:
        raise ValueError('steps must be an integer from 1 to 20')
    context_mode = request.get('context_mode', 'full_window')
    if context_mode not in ('full_window', 'short_2s', 'symmetric_10s'):
        raise ValueError('Unknown audio context mode')
    return dict(context_mode=context_mode, start_seconds=start, end_seconds=end, known_beats=known,
                known_downbeats=down, interval_count=count, steps=steps)


def audio_context_bounds(start, end, sample_rate, frames, mode='full_window'):
    """Use real musical context, not zero padding, within the 1500-frame budget."""
    duration=frames/sample_rate
    if mode=='short_2s':
        return int(max(0.,start-2.)*sample_rate),min(frames,math.ceil((end+2.)*sample_rate))
    if mode=='symmetric_10s' and end-start+20<=29.8:
        return int(max(0.,start-10.)*sample_rate),min(frames,math.ceil((end+10.)*sample_rate))
    if mode not in ('full_window','symmetric_10s'):raise ValueError('Unknown audio context mode')
    length=min(duration,29.8)  # 1491 STFT centres + 6 leading frames < 1500
    left=max(0.,min((start+end-length)/2,duration-length))
    return int(left*sample_rate),min(frames,math.ceil((left+length)*sample_rate))


def condition_frames(known, down, crop_start, valid_frames, crop_end=None):
    rows = []
    used = set()
    for seconds in known:
        frame = round((seconds - crop_start) * FPS) + BORDER
        # A legal endpoint can round one frame beyond the final STFT centre.
        # Clamp only with explicit audio bounds; retain and audit exact user seconds.
        if (frame == valid_frames and crop_end is not None
                and crop_start <= seconds <= crop_end
                and 0 <= seconds - (crop_start+(valid_frames-1-BORDER)/FPS) <= 1/FPS+1e-9):
            frame = valid_frames-1
        if frame < BORDER or frame >= valid_frames:
            raise ValueError('Known beat is outside the model audio frames')
        if any(abs(frame - other) <= 3 for other in used):
            raise ValueError('Known beats are too close for this 50 fps model; no points were merged')
        used.add(frame)
        rows.append(dict(seconds=seconds, frame=frame,
                         model_seconds=crop_start+(frame-BORDER)/FPS,
                         quantization_error_seconds=crop_start+(frame-BORDER)/FPS-seconds,
                         downbeat=seconds in down))
    return rows


def conditioned_unmask(models, spect, rows, valid_frames, steps=8, audit=True):
    """Extend upstream arbitrary-location conditioning; keep every known token immutable.

    Uses upstream prediction/scheduling helpers, not a different acoustic model.
    Unknown frames stay MASK, not NO_EVENT. No exact-count claim is made.
    """
    import torch
    from md_beat_this import diffusion as d
    from md_beat_this.model.beat_tracker import MASK, PAD, NO_EVENT, EVENT
    length = spect.shape[1]
    tokens = {ch: torch.full((1, length), MASK, dtype=torch.long, device=spect.device)
              for ch in ('beat', 'downbeat')}
    immutable = {ch: torch.zeros(length, dtype=torch.bool, device=spect.device) for ch in tokens}
    for ch in tokens:
        tokens[ch][0, valid_frames:] = PAD
        immutable[ch][valid_frames:] = True
    # Positive locations plus a narrow exclusion neighbourhood, as upstream peak picking.
    for row in rows:
        i = row['frame']
        for ch in (('beat', 'downbeat') if row['downbeat'] else ('beat',)):
            lo, hi = max(0, i-3), min(valid_frames, i+4)
            tokens[ch][0, lo:hi] = NO_EVENT
            tokens[ch][0, i] = EVENT
            immutable[ch][lo:hi] = True
    locked = {ch: value.clone() for ch, value in tokens.items()}
    masked = {ch: tokens[ch][0] == MASK for ch in tokens}
    trace = []
    comparison = None
    with torch.inference_mode():
        if audit and rows:
            blank = {ch: torch.full_like(value, MASK) for ch, value in tokens.items()}
            for ch in blank:
                blank[ch][0, valid_frames:] = PAD
            without = d.ensemble_logits(models, spect, blank)
            with_known = d.ensemble_logits(models, spect, tokens)
            unknown = ~immutable['beat']
            unknown[valid_frames:] = False
            delta = (with_known['beat'] - without['beat'])[unknown]
            comparison = dict(unknown_frames=int(unknown.sum()),
                changed_unknown_logit_frames=int((delta.abs() > 1e-6).sum()),
                max_absolute_logit_change=float(delta.abs().max()) if delta.numel() else 0.,
                mean_absolute_logit_change=float(delta.abs().mean()) if delta.numel() else 0.,
                explanation='Same audio and weights, only input tokens differ; not an accuracy score')
        for step in range(steps):
            for ch in tokens:
                if not torch.equal(tokens[ch][0, immutable[ch]], locked[ch][0, immutable[ch]]):
                    raise RuntimeError('An input condition was changed')
            trace.append(dict(step=step+1,
                known_beat_input=[int(tokens['beat'][0, row['frame']]) for row in rows],
                known_downbeat_input=[int(tokens['downbeat'][0, row['frame']]) for row in rows if row['downbeat']],
                remaining_unknown=int(masked['beat'].sum())))
            logits = d.ensemble_logits(models, spect, tokens)
            prediction = d.predict_tokens(logits, 3)
            # A downbeat cannot invent a beat in an explicitly excluded neighbour.
            blocked = immutable['beat'] & (locked['beat'][0] == NO_EVENT)
            prediction['downbeat'][blocked] = NO_EVENT
            selected = {}
            for ch in tokens:
                chosen = d.select_frames(logits[ch], masked[ch], steps-step)
                chosen = d.spare_suppressed_frames(chosen, prediction[ch], masked[ch], logits[ch], 3)
                selected[ch] = chosen
                tokens[ch][0, chosen] = prediction[ch][chosen]
                masked[ch] &= ~chosen
            new_down = selected['downbeat'] & (tokens['downbeat'][0] == EVENT)
            forced = new_down & masked['beat']
            if forced.any():
                tokens['beat'][0, forced] = EVENT
                masked['beat'] &= ~forced
                selected['beat'] |= forced
                other = selected['beat'] & ~forced & (tokens['beat'][0] == EVENT)
                conflict = d.near_events(forced, other, 3) & ~immutable['beat']
                tokens['beat'][0, conflict] = NO_EVENT
                selected['beat'] &= ~conflict
            for ch in tokens:
                newly_positive = selected[ch] & (tokens[ch][0] == EVENT)
                neighbours = d.near_events(newly_positive, masked[ch], 3)
                tokens[ch][0, neighbours] = NO_EVENT
                masked[ch] &= ~neighbours
            if not any(mask.any() for mask in masked.values()):
                break
        for ch in tokens:
            tokens[ch][0, masked[ch]] = prediction[ch][masked[ch]]
            if not torch.equal(tokens[ch][0, immutable[ch]], locked[ch][0, immutable[ch]]):
                raise RuntimeError('Condition preservation failed')
        downbeat = tokens['downbeat'][0] == EVENT
        beat = (tokens['beat'][0] == EVENT) | downbeat
        if any(not beat[row['frame']] for row in rows):
            raise RuntimeError('Known beat disappeared')
    return beat, downbeat, dict(trace=trace, input_condition_effect=comparison,
        known_tokens_entered_network=True, no_training_performed=True,
        unknown_frames_not_assumed_negative=True, immutable_tokens_preserved=True)


def _load_upstream(root):
    directory = Path(root)/'vendor'/'md_beat_this_trial'/f'md_beat_this-{UPSTREAM_COMMIT}'
    if not (directory/'md_beat_this'/'diffusion.py').is_file():
        raise FileNotFoundError('MD Beat This trial source not installed; existing provider is unchanged')
    sys.path.insert(0, str(directory))
    return directory


def run_trial(request, root=None):
    import numpy as np
    import soundfile as sf
    import torch
    root = Path(root) if root else runtime_env_root()
    _load_upstream(root)
    from md_beat_this.inference import load_models
    from md_beat_this.preprocessing import prepare_signal, LogMelSpect
    audio = Path(request['audio_path']).resolve(strict=True)
    source_hash = sha256(audio)
    if request.get('source_sha256') and request['source_sha256'] != source_hash:
        raise ValueError('Source audio changed; prepare the trial again')
    info = sf.info(audio)
    validated = validate_request(request, info.duration)
    start, end = validated['start_seconds'], validated['end_seconds']
    crop_start_frame, crop_end_frame = audio_context_bounds(start,end,info.samplerate,info.frames,validated['context_mode'])
    signal, sr = sf.read(audio, start=crop_start_frame, stop=crop_end_frame,
                         dtype='float32', always_2d=True)
    crop_start = crop_start_frame/sr
    device = request.get('device', 'cuda' if torch.cuda.is_available() else 'cpu')
    if device not in ('cpu', 'cuda'):
        raise ValueError('device must be cpu or cuda')
    checkpoint = root/'models'/'md_beat_this_trial'/'run1.ckpt'
    if not checkpoint.is_file() or sha256(checkpoint) != CHECKPOINT_SHA256:
        raise ValueError('Official run1 checkpoint missing or checksum mismatch')
    began = time.monotonic()
    models = load_models(str(checkpoint), device, num_models=1)
    mono = prepare_signal(signal, sr)
    with torch.inference_mode():
        spect = LogMelSpect(device=device)(torch.tensor(mono, device=device))
        spect = torch.nn.functional.pad(spect, (0, 0, BORDER, 0))
        valid_frames = len(spect)
        if valid_frames > 1500:
            raise ValueError('Audio context exceeds this trial 30-second budget')
        spect = torch.nn.functional.pad(spect, (0, 0, 0, 1500-valid_frames)).unsqueeze(0)
        rows = condition_frames(validated['known_beats'], validated['known_downbeats'], crop_start, valid_frames, crop_end=crop_end_frame/sr)
        beat, downbeat, audit = conditioned_unmask(models, spect, rows, valid_frames,
            validated['steps'], audit=request.get('audit', True))
    exact = {row['frame']:row['seconds'] for row in rows}
    def times(sequence):
        return [exact.get(i, crop_start+(i-BORDER)/FPS)
                for i in sequence.nonzero()[:, 0].cpu().tolist()
                if BORDER <= i < valid_frames and start-1e-9 <= exact.get(i,crop_start+(i-BORDER)/FPS) <= end+1e-9]
    beats, downbeats = times(beat), times(downbeat)
    actual_count = len(beats)-1
    expected_count = validated['interval_count']
    count_matches = expected_count is None or actual_count == expected_count
    warnings = ['Research trial; model output is a suggestion, not verified musical truth.',
        'Beat labels describe model perceptual pulses, not arbitrary eighth-note subdivisions.',
        'The network does not accept an exact interval count; mismatch is reported, never silently forced.',
        '50 fps prediction grid; known user positions retain their exact original seconds.']
    if not count_matches:
        warnings.append(f'Requested {expected_count} intervals but model proposed {actual_count}; do not apply as counted fill.')
    return dict(version=VERSION, provider='md_beat_this_trial', license=LICENSE,
        upstream_commit=UPSTREAM_COMMIT, checkpoint_sha256=CHECKPOINT_SHA256,
        source_sha256=source_hash, audio_path=str(audio), request=validated,
        beat_seconds=beats, downbeat_seconds=downbeats, conditioning=rows,
        interval_count=actual_count, requested_interval_count=expected_count,
        count_matches=count_matches, count_is_model_condition=False,
        model_input_conditioning=True, training=False, project_modified=False,
        original_provider_preserved=True, suggested_only=True,
        audio_context=dict(mode=validated['context_mode'],start_seconds=crop_start,end_seconds=crop_end_frame/sr,real_audio_seconds=(crop_end_frame-crop_start_frame)/sr,valid_frames=valid_frames),
        model_fps=FPS, elapsed_seconds=time.monotonic()-began,
        device=device, model_count=1, audit=audit, warnings=warnings,
        project_binding=request.get('project_binding'))


def prepare_project(project, request):
    """Read current project; use explicitly selected points, never legacy doc.anchors."""
    from . import editing
    project = Path(project).resolve()
    state = json.loads((project/'project.json').read_text(encoding='utf8'))
    doc = editing.current(project)
    if doc is None:
        raise ValueError('Open the project in the editor first; no editing state was created')
    result = dict(request, audio_path=state['source'], source_sha256=state.get('source_sha256'),
        project_binding=dict(project=str(project), revision=doc['revision'],
            document_sha256=hashlib.sha256(json.dumps(doc,sort_keys=True,ensure_ascii=False,allow_nan=False).encode()).hexdigest()))
    validate_request(result, doc['duration'])
    return result


def main():
    parser = argparse.ArgumentParser(description='MD Beat This non-commercial conditioned preview; never changes project')
    parser.add_argument('request', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--project', type=Path)
    args = parser.parse_args()
    request = json.loads(args.request.read_text(encoding='utf-8-sig'))
    if args.project:
        request = prepare_project(args.project, request)
    if args.output.exists():
        raise FileExistsError('Trial output already exists; choose a new output path')
    result = run_trial(request)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x', encoding='utf8') as out:
        json.dump(result, out, ensure_ascii=False, indent=2, allow_nan=False)
    print(json.dumps({'output':str(args.output),'beats':len(result['beat_seconds']),
        'count_matches':result['count_matches'],'elapsed_seconds':result['elapsed_seconds']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
